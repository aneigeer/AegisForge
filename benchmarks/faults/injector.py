"""W04 双引擎故障注入与重置。

约束：
- 只允许写入 dg_lab（MySQL）与 dg_lab_pg（PostgreSQL）的故障沙箱；
- 所有故障表名以 ``fault_`` 开头，PostgreSQL 故障表位于专用 ``fault_lab`` schema；
- 注入失败或权限不足必须抛出 :class:`FaultInjectionError`，不得伪造成功；
- 本模块不读取、不记录 Oracle 真值（``expected_root_cause`` 等）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.faults.models import DbEngineName, FaultCaseManifest, FaultScenario
from core.faults.store import load_manifest
from core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_DATABASES: dict[str, str] = {
    DbEngineName.MYSQL.value: "dg_lab",
    DbEngineName.POSTGRESQL.value: "dg_lab_pg",
}
PG_FAULT_SCHEMA = "fault_lab"
PG_READONLY_ROLE = "dg_ro_pg"
DEFAULT_CONNECTION_COUNT = 10
DEFAULT_LOCK_WAIT_TIMEOUT_S = 30
OBSERVE_TIMEOUT_S = 5.0

_MYSQL_DIGITS = (
    "(SELECT 0 AS n UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 "
    "UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 "
    "UNION ALL SELECT 8 UNION ALL SELECT 9)"
)


class FaultInjectionError(RuntimeError):
    """故障注入/重置的结构化错误；details 只含安全字段。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INJECTION_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class InjectionResult:
    scenario_id: str
    scenario: str
    db_engine: str
    database_id: str
    database: str
    injected: bool
    parameters: dict[str, Any]
    observed: dict[str, Any]
    injected_at: str

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ActiveFault:
    scenario_id: str
    hold_until: float
    cleanup: Callable[[], None]
    result: InjectionResult


_ACTIVE: dict[str, _ActiveFault] = {}
_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sandbox_table(scenario: str, seed: int) -> str:
    return f"fault_{scenario}_{seed}"


def _resolve_client(
    manifest: FaultCaseManifest,
    rw_client: Any | None,
    pg_rw_client: Any | None,
) -> Any:
    engine = manifest.db_engine
    if engine is DbEngineName.MYSQL:
        if rw_client is not None:
            return rw_client
        return _client_from_settings(DbEngineName.MYSQL)
    if engine is DbEngineName.POSTGRESQL:
        if pg_rw_client is not None:
            return pg_rw_client
        return _client_from_settings(DbEngineName.POSTGRESQL)
    raise FaultInjectionError("不支持的数据库引擎", code="ENGINE_UNSUPPORTED")


def _client_from_settings(engine: DbEngineName) -> Any:
    from core.config import get_settings
    from database.mysql.connection import MysqlClient
    from database.postgres.connection import PgClient

    settings = get_settings()
    if engine is DbEngineName.MYSQL:
        return MysqlClient(
            settings.rw_dsn,
            database_id=settings.database_id,
            read_only=False,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
    if settings.pg_rw_dsn is None:
        raise FaultInjectionError(
            "未配置 DG_PG_RW_DSN，PostgreSQL 故障注入不可用",
            code="DB_NOT_CONFIGURED",
        )
    return PgClient(
        settings.pg_rw_dsn,
        database_id=settings.pg_database_id,
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


def _assert_writable_target(client: Any, manifest: FaultCaseManifest) -> None:
    expected = ALLOWED_DATABASES[manifest.db_engine.value]
    if getattr(client, "read_only", True):
        raise FaultInjectionError(
            "故障注入必须使用写账号（read_only=False）",
            code="WRITE_ACCOUNT_REQUIRED",
            details={"scenario_id": manifest.scenario_id},
        )
    actual = str(getattr(client, "database", ""))
    if actual != expected:
        raise FaultInjectionError(
            "故障注入目标库不在白名单内",
            code="DATABASE_NOT_ALLOWED",
            details={"expected": expected, "actual": actual},
        )


def inject(
    scenario_id: str,
    *,
    hold_seconds: float = 0.0,
    connection_count: int = DEFAULT_CONNECTION_COUNT,
    rw_client: Any | None = None,
    pg_rw_client: Any | None = None,
) -> InjectionResult:
    """注入指定故障并保持到 ``reset``（或 ``FaultSession`` 退出）。"""

    if hold_seconds < 0:
        raise FaultInjectionError("hold_seconds 必须 >= 0", code="INVALID_PARAMETER")
    if not 1 <= connection_count <= 100:
        raise FaultInjectionError("connection_count 必须在 1..100", code="INVALID_PARAMETER")

    manifest = load_manifest(scenario_id)
    client = _resolve_client(manifest, rw_client, pg_rw_client)
    _assert_writable_target(client, manifest)

    with _LOCK:
        if scenario_id in _ACTIVE:
            _release_active(scenario_id)

    engine = manifest.db_engine.value
    try:
        if engine == DbEngineName.MYSQL.value:
            parameters, observed, cleanup = _inject_mysql(
                manifest, client, connection_count
            )
        else:
            parameters, observed, cleanup = _inject_postgres(
                manifest, client, connection_count
            )
    except FaultInjectionError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转换为明确错误，不伪造成功
        raise FaultInjectionError(
            "故障注入失败",
            details={"scenario_id": scenario_id, "exception": type(exc).__name__},
        ) from exc

    result = InjectionResult(
        scenario_id=scenario_id,
        scenario=manifest.scenario.value,
        db_engine=engine,
        database_id=str(getattr(client, "database_id", "")),
        database=str(getattr(client, "database", "")),
        injected=True,
        parameters=parameters,
        observed=observed,
        injected_at=_now_iso(),
    )
    deadline = time.monotonic() + hold_seconds
    with _LOCK:
        _ACTIVE[scenario_id] = _ActiveFault(
            scenario_id=scenario_id,
            hold_until=deadline,
            cleanup=cleanup,
            result=result,
        )
    logger.info(
        "fault injected scenario=%s engine=%s database=%s",
        scenario_id,
        engine,
        result.database,
    )
    return result


def _release_active(scenario_id: str) -> None:
    active = _ACTIVE.pop(scenario_id, None)
    if active is None:
        return
    remaining = active.hold_until - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    try:
        active.cleanup()
    except Exception as exc:  # noqa: BLE001 - 重置失败必须显式报错
        raise FaultInjectionError(
            "故障清理失败",
            code="RESET_FAILED",
            details={"scenario_id": scenario_id, "exception": type(exc).__name__},
        ) from exc


def reset(
    scenario_id: str,
    *,
    rw_client: Any | None = None,
    pg_rw_client: Any | None = None,
) -> dict[str, Any]:
    """重置故障。进程内有活动会话时关闭会话并清理；否则按 Manifest 清理沙箱表。"""

    manifest = load_manifest(scenario_id)
    with _LOCK:
        active = scenario_id in _ACTIVE
    if active:
        _release_active(scenario_id)
        return {
            "scenario_id": scenario_id,
            "reset": True,
            "session_cleanup": True,
            "database": ALLOWED_DATABASES[manifest.db_engine.value],
        }

    client = _resolve_client(manifest, rw_client, pg_rw_client)
    _assert_writable_target(client, manifest)
    cleaned = _cleanup_persistent(manifest, client)
    return {
        "scenario_id": scenario_id,
        "reset": True,
        "session_cleanup": False,
        "database": ALLOWED_DATABASES[manifest.db_engine.value],
        "tables_cleaned": cleaned,
    }


def _cleanup_persistent(manifest: FaultCaseManifest, client: Any) -> list[str]:
    """无活动会话时（如 CLI 跨进程）按固定表名清理沙箱表。"""

    engine = manifest.db_engine
    if manifest.scenario is FaultScenario.CONNECTION_PRESSURE:
        return []
    scenario_name = manifest.scenario.value
    table = _sandbox_table(scenario_name, manifest.data_seed)
    if engine is DbEngineName.MYSQL:
        conn = client.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        except Exception as exc:  # noqa: BLE001
            raise FaultInjectionError(
                "清理故障沙箱表失败",
                code="RESET_FAILED",
                details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
            ) from exc
        finally:
            conn.close()
    else:
        try:
            client.execute(f"DROP TABLE IF EXISTS {PG_FAULT_SCHEMA}.{table}")
        except Exception as exc:  # noqa: BLE001
            raise FaultInjectionError(
                "清理故障沙箱表失败",
                code="RESET_FAILED",
                details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
            ) from exc
    return [table]


class FaultSession:
    """上下文管理器：进入时注入，退出时重置并清理。"""

    def __init__(
        self,
        scenario_id: str,
        *,
        hold_seconds: float = 0.0,
        connection_count: int = DEFAULT_CONNECTION_COUNT,
        rw_client: Any | None = None,
        pg_rw_client: Any | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self._hold_seconds = hold_seconds
        self._connection_count = connection_count
        self._rw_client = rw_client
        self._pg_rw_client = pg_rw_client
        self.result: InjectionResult | None = None

    def __enter__(self) -> InjectionResult:
        self.result = inject(
            self.scenario_id,
            hold_seconds=self._hold_seconds,
            connection_count=self._connection_count,
            rw_client=self._rw_client,
            pg_rw_client=self._pg_rw_client,
        )
        return self.result

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            reset(
                self.scenario_id,
                rw_client=self._rw_client,
                pg_rw_client=self._pg_rw_client,
            )
        except Exception:  # noqa: BLE001
            if exc_type is None:
                raise
            logger.exception("fault reset failed while handling exception scenario=%s", self.scenario_id)
        return False


# --------------------------------------------------------------------------- #
# 注入分发
# --------------------------------------------------------------------------- #


def _inject_mysql(
    manifest: FaultCaseManifest, client: Any, connection_count: int
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    if manifest.scenario is FaultScenario.SLOW_QUERY:
        return _mysql_slow_query(manifest, client)
    if manifest.scenario is FaultScenario.CONNECTION_PRESSURE:
        return _mysql_connection_pressure(manifest, client, connection_count)
    return _mysql_lock_wait(manifest, client)


def _inject_postgres(
    manifest: FaultCaseManifest, client: Any, connection_count: int
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    if manifest.scenario is FaultScenario.SLOW_QUERY:
        return _pg_slow_query(manifest, client)
    if manifest.scenario is FaultScenario.CONNECTION_PRESSURE:
        return _pg_connection_pressure(manifest, client, connection_count)
    return _pg_lock_wait(manifest, client)


# --------------------------------------------------------------------------- #
# MySQL 场景
# --------------------------------------------------------------------------- #


def _mysql_session_count(client: Any) -> int:
    identity = client.identity()
    user = str(identity.user).split("@", 1)[0]
    rows = client.fetch_all(
        "SELECT COUNT(*) AS c FROM information_schema.processlist "
        "WHERE USER = %s AND DB = %s",
        (user, "dg_lab"),
    )
    return int(rows[0]["c"])


def _mysql_slow_query(
    manifest: FaultCaseManifest, client: Any
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    rows = int(manifest.workload_config.get("rows", 20_000))
    table = _sandbox_table(FaultScenario.SLOW_QUERY.value, manifest.data_seed)
    conn = client.connect()

    def cleanup() -> None:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        finally:
            conn.close()

    select_rows = (
        "SELECT a.n + 10*b.n + 100*c.n + 1000*d.n + 10000*e.n AS n "
        f"FROM {_MYSQL_DIGITS} a CROSS JOIN {_MYSQL_DIGITS} b "
        f"CROSS JOIN {_MYSQL_DIGITS} c CROSS JOIN {_MYSQL_DIGITS} d "
        f"CROSS JOIN {_MYSQL_DIGITS} e "
        f"WHERE a.n + 10*b.n + 100*c.n + 1000*d.n + 10000*e.n BETWEEN 1 AND {rows}"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute(
                f"CREATE TABLE `{table}` ("
                "id INT NOT NULL, k INT NOT NULL, payload VARCHAR(64) NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cur.execute(
                f"INSERT INTO `{table}` (id, k, payload) "
                f"SELECT n, (n * {int(manifest.data_seed)}) % 9973, CONCAT('row-', n) "
                f"FROM ({select_rows}) seq"
            )
            inserted = int(cur.rowcount)
            if inserted <= 0:
                raise FaultInjectionError(
                    "慢查询沙箱数据写入为空",
                    details={"scenario_id": manifest.scenario_id, "table": table},
                )
            started = time.perf_counter()
            cur.execute(
                f"SELECT COUNT(*) AS c, SUM(k) AS s FROM `{table}` "
                "WHERE k > 0 AND payload >= 'row-'"
            )
            matched = cur.fetchone() or {}
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
    except FaultInjectionError:
        _safe_cleanup(cleanup)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "MySQL 慢查询故障注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    parameters = {"rows": rows, "query_profile": "no_index_aggregate"}
    observed = {
        "table": table,
        "rows_inserted": inserted,
        "rows_matched": int(matched.get("c") or 0),
        "query_duration_ms": duration_ms,
    }
    return parameters, observed, cleanup


def _mysql_connection_pressure(
    manifest: FaultCaseManifest, client: Any, connection_count: int
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    baseline = _mysql_session_count(client)
    held: list[Any] = []

    def cleanup() -> None:
        while held:
            conn = held.pop()
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不掩盖主错误
                pass

    try:
        for _ in range(connection_count):
            held.append(client.connect())
        after = _mysql_session_count(client)
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "MySQL 连接压力注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    if after - baseline < connection_count:
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "MySQL 连接数未按预期增加，注入失败",
            details={
                "scenario_id": manifest.scenario_id,
                "baseline": baseline,
                "after": after,
                "expected_delta": connection_count,
            },
        )

    parameters = {"sessions": connection_count, "mode": "idle"}
    observed = {
        "connections_opened": len(held),
        "connections_baseline": baseline,
        "connections_after": after,
    }
    return parameters, observed, cleanup


def _mysql_lock_wait(
    manifest: FaultCaseManifest, client: Any
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    seed = int(manifest.data_seed)
    table = _sandbox_table(FaultScenario.LOCK_WAIT.value, seed)
    wait_timeout = int(
        manifest.workload_config.get("wait_timeout_s", DEFAULT_LOCK_WAIT_TIMEOUT_S)
    )
    setup = client.connect()
    holder = client.connect()
    waiter = client.connect()
    started = threading.Event()
    thread_errors: list[str] = []
    thread: threading.Thread | None = None

    def wait_for_lock() -> None:
        try:
            with waiter.cursor() as cur:
                cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (wait_timeout,))
                cur.execute("BEGIN")
                started.set()
                cur.execute(f"SELECT id FROM `{table}` WHERE id = 1 FOR UPDATE")
            waiter.commit()
        except BaseException as exc:  # noqa: BLE001 - 线程需带回真实错误
            thread_errors.append(type(exc).__name__)

    def cleanup() -> None:
        if thread is not None:
            try:
                holder.rollback()
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=5)
        try:
            waiter.rollback()
        except Exception:  # noqa: BLE001
            pass
        for conn in (holder, waiter):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            with setup.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        finally:
            setup.close()

    try:
        with setup.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`")
            cur.execute(
                f"CREATE TABLE `{table}` ("
                "id INT NOT NULL PRIMARY KEY, k INT NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cur.execute(
                f"INSERT INTO `{table}` (id, k) VALUES (1, %s), (2, %s), (3, %s)",
                (seed % 1000, seed % 777, seed % 555),
            )
        with holder.cursor() as cur:
            cur.execute("BEGIN")
            locked = cur.execute(f"SELECT id FROM `{table}` WHERE id = 1 FOR UPDATE")
            if locked <= 0:
                raise FaultInjectionError(
                    "锁目标行不存在，无法形成锁等待",
                    details={"scenario_id": manifest.scenario_id, "table": table},
                )

        thread = threading.Thread(target=wait_for_lock, daemon=True)
        thread.start()
        if not started.wait(timeout=2.0):
            raise FaultInjectionError(
                "锁等待会话未启动",
                details={"scenario_id": manifest.scenario_id},
            )
        time.sleep(0.5)
        if not thread.is_alive():
            raise FaultInjectionError(
                "锁等待未形成，注入失败",
                details={
                    "scenario_id": manifest.scenario_id,
                    "thread_errors": thread_errors,
                },
            )
    except FaultInjectionError:
        _safe_cleanup(cleanup)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "MySQL 锁等待故障注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    parameters = {"table": table, "holder_row_id": 1, "wait_timeout_s": wait_timeout}
    observed = {"table": table, "wait_count": 1, "waiting_sessions": 1}
    return parameters, observed, cleanup


# --------------------------------------------------------------------------- #
# PostgreSQL 场景
# --------------------------------------------------------------------------- #


def _pg_session_count(client: Any) -> int:
    identity = client.identity()
    rows = client.fetch_all(
        "SELECT count(*) AS c FROM pg_stat_activity WHERE datname = %s AND usename = %s",
        ("dg_lab_pg", str(identity.user)),
    )
    return int(rows[0]["c"])


def _pg_prepare_schema(client: Any) -> None:
    try:
        client.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_FAULT_SCHEMA}")
        client.execute(f"GRANT USAGE ON SCHEMA {PG_FAULT_SCHEMA} TO {PG_READONLY_ROLE}")
    except Exception as exc:  # noqa: BLE001
        raise FaultInjectionError(
            "PostgreSQL 故障 schema 准备失败（权限不足或角色缺失）",
            code="DB_PERMISSION_DENIED",
            details={"exception": type(exc).__name__},
        ) from exc


def _pg_slow_query(
    manifest: FaultCaseManifest, client: Any
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    rows = int(manifest.workload_config.get("rows", 20_000))
    table = _sandbox_table(FaultScenario.SLOW_QUERY.value, manifest.data_seed)
    qualified = f"{PG_FAULT_SCHEMA}.{table}"

    def cleanup() -> None:
        client.execute(f"DROP TABLE IF EXISTS {qualified}")

    try:
        _pg_prepare_schema(client)
        client.execute(f"DROP TABLE IF EXISTS {qualified}")
        client.execute(
            f"CREATE TABLE {qualified} ("
            "id INTEGER NOT NULL, k INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        inserted = client.execute(
            f"INSERT INTO {qualified} (id, k, payload) "
            "SELECT g, (g * %s) %% 9973, 'row-' || g FROM generate_series(1, %s) AS g",
            (int(manifest.data_seed), rows),
        )
        if inserted <= 0:
            raise FaultInjectionError(
                "慢查询沙箱数据写入为空",
                details={"scenario_id": manifest.scenario_id, "table": qualified},
            )
        client.execute(f"GRANT SELECT ON TABLE {qualified} TO {PG_READONLY_ROLE}")
        started = time.perf_counter()
        matched = client.fetch_all(
            f"SELECT count(*) AS c, sum(k) AS s FROM {qualified} "
            "WHERE k > 0 AND payload >= 'row-'"
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
    except FaultInjectionError:
        _safe_cleanup(cleanup)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "PostgreSQL 慢查询故障注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    parameters = {"rows": rows, "query_profile": "no_index_aggregate", "schema": PG_FAULT_SCHEMA}
    observed = {
        "table": qualified,
        "rows_inserted": inserted,
        "rows_matched": int((matched[0].get("c") if matched else 0) or 0),
        "query_duration_ms": duration_ms,
    }
    return parameters, observed, cleanup


def _pg_connection_pressure(
    manifest: FaultCaseManifest, client: Any, connection_count: int
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    baseline = _pg_session_count(client)
    held: list[tuple[Any, Any]] = []

    def cleanup() -> None:
        while held:
            cm, _conn = held.pop()
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    try:
        for _ in range(connection_count):
            cm = client.connection()
            conn = cm.__enter__()
            held.append((cm, conn))
        after = _pg_session_count(client)
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "PostgreSQL 连接压力注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    if after - baseline < connection_count:
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "PostgreSQL 连接数未按预期增加，注入失败",
            details={
                "scenario_id": manifest.scenario_id,
                "baseline": baseline,
                "after": after,
                "expected_delta": connection_count,
            },
        )

    parameters = {"sessions": connection_count, "mode": "idle"}
    observed = {
        "connections_opened": len(held),
        "connections_baseline": baseline,
        "connections_after": after,
    }
    return parameters, observed, cleanup


def _pg_lock_wait(
    manifest: FaultCaseManifest, client: Any
) -> tuple[dict[str, Any], dict[str, Any], Callable[[], None]]:
    seed = int(manifest.data_seed)
    table = _sandbox_table(FaultScenario.LOCK_WAIT.value, seed)
    qualified = f"{PG_FAULT_SCHEMA}.{table}"
    started = threading.Event()
    thread_errors: list[str] = []
    thread: threading.Thread | None = None
    holder_cm = client.connection()
    holder = holder_cm.__enter__()
    waiter_cm = client.connection()
    waiter = waiter_cm.__enter__()
    holder.autocommit = False
    waiter.autocommit = False
    waiter_pid = int(waiter.info.backend_pid)

    def wait_for_lock() -> None:
        try:
            waiter.execute("SET statement_timeout = 0")
            started.set()
            waiter.execute(f"SELECT id FROM {qualified} WHERE id = 1 FOR UPDATE")
        except BaseException as exc:  # noqa: BLE001
            thread_errors.append(type(exc).__name__)

    def cleanup() -> None:
        if thread is not None:
            try:
                holder.rollback()
            except Exception:  # noqa: BLE001
                pass
            thread.join(timeout=5)
        try:
            waiter.rollback()
        except Exception:  # noqa: BLE001
            pass
        for cm in (holder_cm, waiter_cm):
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        client.execute(f"DROP TABLE IF EXISTS {qualified}")

    try:
        _pg_prepare_schema(client)
        client.execute(f"DROP TABLE IF EXISTS {qualified}")
        client.execute(
            f"CREATE TABLE {qualified} ("
            "id INTEGER NOT NULL PRIMARY KEY, k INTEGER NOT NULL)"
        )
        client.execute(
            f"INSERT INTO {qualified} (id, k) VALUES (1, %s), (2, %s), (3, %s)",
            (seed % 1000, seed % 777, seed % 555),
        )
        holder.execute(f"SELECT id FROM {qualified} WHERE id = 1 FOR UPDATE")
        thread = threading.Thread(target=wait_for_lock, daemon=True)
        thread.start()
        if not started.wait(timeout=2.0):
            raise FaultInjectionError(
                "PostgreSQL 锁等待会话未启动",
                details={"scenario_id": manifest.scenario_id},
            )
        deadline = time.monotonic() + OBSERVE_TIMEOUT_S
        observed_wait = 0
        while time.monotonic() < deadline:
            # 行锁等待在 PG12 中表现为 transactionid 锁等待，pg_locks 的 relation 为空；
            # 这里按等待会话自身的 wait_event 观测，避免依赖具体锁对象形态。
            rows = client.fetch_all(
                "SELECT count(*) AS c FROM pg_stat_activity a "
                "WHERE a.pid = %s AND a.wait_event_type = 'Lock'",
                (waiter_pid,),
            )
            observed_wait = int(rows[0]["c"] or 0) if rows else 0
            if observed_wait > 0:
                break
            time.sleep(0.2)
        if observed_wait <= 0:
            raise FaultInjectionError(
                "PostgreSQL 锁等待未被 pg_stat_activity 观测到，注入失败",
                details={"scenario_id": manifest.scenario_id, "thread_errors": thread_errors},
            )
    except FaultInjectionError:
        _safe_cleanup(cleanup)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_cleanup(cleanup)
        raise FaultInjectionError(
            "PostgreSQL 锁等待故障注入失败",
            details={"scenario_id": manifest.scenario_id, "exception": type(exc).__name__},
        ) from exc

    parameters = {"table": qualified, "holder_row_id": 1}
    observed = {"table": qualified, "wait_count": observed_wait, "waiting_sessions": 1}
    return parameters, observed, cleanup


def _safe_cleanup(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except Exception:  # noqa: BLE001 - 清理失败不覆盖原始注入错误
        pass


__all__ = [
    "ALLOWED_DATABASES",
    "DEFAULT_CONNECTION_COUNT",
    "FaultInjectionError",
    "FaultSession",
    "InjectionResult",
    "PG_FAULT_SCHEMA",
    "inject",
    "reset",
]
