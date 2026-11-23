"""W07：Prepare 探针的 MySQL 实现（7 条**固定 SQL** + 绑定参数）。

硬规则（可逐条核对）：

1. 本模块内**没有任何接受 SQL 文本的入口**：`observe()` 只吃
   `PrepareRequest`（冻结 dataclass，字段：environment_id / order_id /
   target_database / target_table）。
2. SQL 原文全部以模块级常量固化，参数一律 `%s` 绑定。
3. 标识符只允许服务端常量（`dg_lab` / `seed_orders`），且在
   `PrepareRequest.__post_init__` 里做白名单校验，客户端传不进来。
4. 任何一项探测失败 → 该字段为 `None`（上层按 `UNKNOWN` 阻断），**不猜测**。

两个连接：

- `dg_ro`：只读观测（行签名 / 表大小 / 阻塞锁 / 长事务 / 负载 / `@@datadir`）。
- `dg_rw`：**必须是执行写操作的那个账号**，用于核实 `read_only`、`super_read_only`
  与 `UPDATE ON dg_lab.*` 授权（⑨ `db_identity_privilege`）。
"""

from __future__ import annotations

import re
import shutil
from typing import Any, Callable, Iterable, Mapping

from core.approval.prerequisites import (
    ALLOWED_TARGET_DATABASES,
    ALLOWED_TARGET_TABLES,
    PrepareObservation,
    PrepareRequest,
)
from core.logging import get_logger
from core.security.canonical import normalize_timestamp
from core.tasks.models import utc_now
from database.mysql.connection import MysqlClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 固定 SQL（唯一允许执行的语句集；任何新增都必须同时更新本注释与测试）
# ---------------------------------------------------------------------------

# ② 行签名：命中行数 / NULL 行数 / 当前值摘要 / 值长度。
_SQL_TARGET_SIGNATURE = (
    "SELECT COUNT(*) AS matched_rows, SUM(note IS NULL) AS null_rows, "
    "SHA2(COALESCE(note, '<NULL>'), 256) AS note_digest, "
    "CHAR_LENGTH(COALESCE(note, '')) AS note_chars "
    f"FROM {ALLOWED_TARGET_DATABASES[0]}.{ALLOWED_TARGET_TABLES[0]} "
    "WHERE order_id = %s"
)

# ③ 表大小：行数估算 + 数据/索引长度。
_SQL_TABLE_SIZE = (
    "SELECT COALESCE(TABLE_ROWS, 0) AS table_rows, "
    "COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0) AS data_length_bytes "
    "FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
)

# ④ 磁盘余量：先取 datadir，再由本机 shutil 判余量（同机 LAB 才可判定）。
_SQL_DATADIR = "SELECT @@datadir AS datadir"

# ⑤ 目标表阻塞锁。
_SQL_BLOCKING_LOCKS = (
    "SELECT COUNT(*) AS blocking_locks FROM performance_schema.data_lock_waits AS w "
    "JOIN performance_schema.data_locks AS l "
    "ON l.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID "
    "WHERE l.OBJECT_SCHEMA = %s AND l.OBJECT_NAME = %s"
)

# ⑥ 长事务：当前最长 RUNNING 事务秒数。
_SQL_LONG_TRANSACTIONS = (
    "SELECT COALESCE(MAX(TIMESTAMPDIFF(SECOND, trx_started, NOW(6))), 0) AS longest_trx_s "
    "FROM information_schema.INNODB_TRX WHERE trx_state = 'RUNNING'"
)

# ⑦ 当前负载：并发运行线程 / 连接数 / 连接上限。
_SQL_SERVER_LOAD = (
    "SELECT "
    "(SELECT CAST(VARIABLE_VALUE AS UNSIGNED) FROM performance_schema.global_status "
    "WHERE VARIABLE_NAME = 'Threads_running') AS threads_running, "
    "(SELECT CAST(VARIABLE_VALUE AS UNSIGNED) FROM performance_schema.global_status "
    "WHERE VARIABLE_NAME = 'Threads_connected') AS threads_connected, "
    "(SELECT CAST(VARIABLE_VALUE AS UNSIGNED) FROM performance_schema.global_variables "
    "WHERE VARIABLE_NAME = 'max_connections') AS max_connections"
)

# ⑨-1 会话身份与只读开关（必须用执行写操作的那个账号）。
# 注：`current_user` 在本机 MySQL 8 里作为**未加引号的别名**会报 1064（保留字冲突），
# 因此三个别名一律用反引号包裹；语义与设计 §2.2 完全一致（列名不变）。
_SQL_IDENTITY = (
    "SELECT CURRENT_USER() AS `current_user`, @@read_only AS `read_only`, "
    "@@super_read_only AS `super_read_only`"
)

# ⑨-2 授权：固定语句，无参数位、无拼接；解析失败即 UNKNOWN。
_SQL_SHOW_GRANTS = "SHOW GRANTS"

# ⑨ 需要核实的目标库（服务端常量，不来自客户端）。
_REQUIRED_DML_TARGET = f"{ALLOWED_TARGET_DATABASES[0]}.*"

# 支持的固定语句清单（供测试断言"没有别的 SQL 出口"）。
PREPARE_PROBE_SQL: tuple[str, ...] = (
    _SQL_TARGET_SIGNATURE,
    _SQL_TABLE_SIZE,
    _SQL_DATADIR,
    _SQL_BLOCKING_LOCKS,
    _SQL_LONG_TRANSACTIONS,
    _SQL_SERVER_LOAD,
    _SQL_IDENTITY,
    _SQL_SHOW_GRANTS,
)

# 单行乐观更新能力：MySQL 8.0+ 的 `UPDATE ... WHERE ... LIMIT 1` + `<=>` 语义。
# 判定依据来自目标库自身版本（不是配置项、不是客户端声明）。
OPTIMISTIC_UPDATE_CAPABILITY = "optimistic_update"
_MIN_OPTIMISTIC_UPDATE_MAJOR = 8

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)")
_GRANT_TARGET_RE = re.compile(r"ON\s+`?([A-Za-z0-9_$]+)`?\.\*", re.IGNORECASE)


def _to_int(value: Any) -> int | None:
    """把驱动返回的 Decimal / str / None 归一化为 int；无法归一化返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool_flag(value: Any) -> bool | None:
    """MySQL 的 `@@read_only` 返回 0/1；无法判定返回 None（按 UNKNOWN 处理）。"""
    number = _to_int(value)
    if number is None:
        return None
    return number != 0


def _parse_supports_optimistic_update(server_version: Any) -> bool:
    if not isinstance(server_version, str):
        return False
    match = _VERSION_RE.match(server_version.strip())
    if match is None:
        return False
    return int(match.group(1)) >= _MIN_OPTIMISTIC_UPDATE_MAJOR


def _grant_rows_to_text(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """`SHOW GRANTS` 每行只有一个以 "Grants for ..." 为键的列，取全部值。"""
    texts: list[str] = []
    for row in rows:
        for value in row.values():
            if isinstance(value, str):
                texts.append(value)
    return texts


def has_update_on_target(grant_texts: Iterable[str]) -> bool | None:
    """客户端解析 `GRANT ... ON `dg_lab`.*`：含 UPDATE 或 ALL PRIVILEGES 即通过。

    解析不到目标库授权 → False（缺 DML 授权）；一行授权都解析不到 → None（UNKNOWN）。
    """
    texts = [text for text in grant_texts if text]
    if not texts:
        return None
    for text in texts:
        upper = text.upper()
        if "GRANT " not in upper:
            continue
        match = _GRANT_TARGET_RE.search(text)
        if match is None:
            continue
        if match.group(1).lower() != ALLOWED_TARGET_DATABASES[0]:
            continue
        if "ALL PRIVILEGES" in upper or re.search(r"\bUPDATE\b", upper):
            return True
    return False


class MysqlPrepareProbe:
    """`PrepareProbePort` 的 MySQL 实现；只在 LAB 装配点被注入。"""

    def __init__(
        self,
        ro_client: MysqlClient,
        rw_client: MysqlClient,
        *,
        capability_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._ro = ro_client
        self._rw = rw_client
        self._capability_provider = capability_provider

    # ---- 端口实现 ------------------------------------------------------
    def observe(self, request: PrepareRequest) -> PrepareObservation:
        """按固定 SQL 采集一次完整观测；单项失败只让该项为 `None`。"""
        if not isinstance(request, PrepareRequest):
            # 端口不接受其它入参类型：SQL 文本/任意 Mapping 一律拒绝。
            raise TypeError("MysqlPrepareProbe.observe 只接受 PrepareRequest")
        signature = self._observe_signature(request)
        table_size = self._observe_table_size(request)
        datadir, free_mib = self._observe_disk()
        blocking_locks = self._observe_blocking_locks(request)
        longest_trx_s = self._observe_long_transactions()
        load = self._observe_server_load()
        identity = self._observe_identity()
        capabilities = self._observe_capabilities()
        return PrepareObservation(
            collected_at=normalize_timestamp(utc_now()),
            signature_matched_rows=signature["matched_rows"],
            signature_null_rows=signature["null_rows"],
            signature_note_digest=signature["note_digest"],
            signature_note_chars=signature["note_chars"],
            table_rows=table_size["table_rows"],
            table_data_length_bytes=table_size["data_length_bytes"],
            disk_free_mib=free_mib,
            disk_datadir=datadir,
            blocking_locks=blocking_locks,
            longest_trx_s=longest_trx_s,
            threads_running=load["threads_running"],
            threads_connected=load["threads_connected"],
            max_connections=load["max_connections"],
            available_capabilities=capabilities,
            account_current_user=identity["current_user"],
            account_read_only=identity["read_only"],
            account_super_read_only=identity["super_read_only"],
            account_update_on_target=identity["update_on_target"],
        )

    # ---- 单项采集 ------------------------------------------------------
    def _observe_signature(self, request: PrepareRequest) -> dict[str, Any]:
        empty = {
            "matched_rows": None,
            "null_rows": None,
            "note_digest": None,
            "note_chars": None,
        }
        try:
            rows = self._ro.fetch_all(_SQL_TARGET_SIGNATURE, (request.order_id,))
        except Exception:  # noqa: BLE001 - 探测失败按 UNKNOWN fail closed
            logger.warning("行签名探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return empty
        if not rows:
            return empty
        row = rows[0]
        matched = _to_int(row.get("matched_rows"))
        if matched is None:
            return empty
        if matched == 0:
            # 0 行：SUM/CHAR_LENGTH 均为 NULL，但"命中 0 行"本身是确定的 FAIL 事实。
            return {
                "matched_rows": 0,
                "null_rows": 0,
                "note_digest": None,
                "note_chars": 0,
            }
        digest = row.get("note_digest")
        return {
            "matched_rows": matched,
            "null_rows": _to_int(row.get("null_rows")),
            "note_digest": str(digest) if digest is not None else None,
            "note_chars": _to_int(row.get("note_chars")),
        }

    def _observe_table_size(self, request: PrepareRequest) -> dict[str, Any]:
        empty = {"table_rows": None, "data_length_bytes": None}
        try:
            rows = self._ro.fetch_all(
                _SQL_TABLE_SIZE,
                (request.target_database, request.target_table),
            )
        except Exception:  # noqa: BLE001
            logger.warning("表大小探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return empty
        if not rows:
            # 表不存在（或当前账号不可见）→ 明确 UNKNOWN，不按 0 放过。
            return empty
        row = rows[0]
        return {
            "table_rows": _to_int(row.get("table_rows")),
            "data_length_bytes": _to_int(row.get("data_length_bytes")),
        }

    def _observe_disk(self) -> tuple[str | None, int | None]:
        try:
            rows = self._ro.fetch_all(_SQL_DATADIR)
        except Exception:  # noqa: BLE001
            logger.warning("datadir 探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return None, None
        if not rows:
            return None, None
        datadir = rows[0].get("datadir")
        if not isinstance(datadir, str) or not datadir.strip():
            return None, None
        try:
            usage = shutil.disk_usage(datadir)
        except OSError:
            # 远程目标 / 路径不可访问：刻意 fail closed（UNKNOWN），不猜余量。
            logger.info(
                "datadir 不可访问，磁盘余量按 UNKNOWN 处理",
                extra={"alert": "PREPARE_PROBE_DISK_UNKNOWN"},
            )
            return datadir, None
        return datadir, int(usage.free // (1024 * 1024))

    def _observe_blocking_locks(self, request: PrepareRequest) -> int | None:
        try:
            rows = self._ro.fetch_all(
                _SQL_BLOCKING_LOCKS,
                (request.target_database, request.target_table),
            )
        except Exception:  # noqa: BLE001
            logger.warning("阻塞锁探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return None
        if not rows:
            return None
        return _to_int(rows[0].get("blocking_locks"))

    def _observe_long_transactions(self) -> int | None:
        try:
            rows = self._ro.fetch_all(_SQL_LONG_TRANSACTIONS)
        except Exception:  # noqa: BLE001
            logger.warning("长事务探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return None
        if not rows:
            return None
        return _to_int(rows[0].get("longest_trx_s"))

    def _observe_server_load(self) -> dict[str, int | None]:
        empty: dict[str, int | None] = {
            "threads_running": None,
            "threads_connected": None,
            "max_connections": None,
        }
        try:
            rows = self._ro.fetch_all(_SQL_SERVER_LOAD)
        except Exception:  # noqa: BLE001
            logger.warning("负载探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return empty
        if not rows:
            return empty
        row = rows[0]
        return {
            "threads_running": _to_int(row.get("threads_running")),
            "threads_connected": _to_int(row.get("threads_connected")),
            "max_connections": _to_int(row.get("max_connections")),
        }

    def _observe_identity(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "current_user": None,
            "read_only": None,
            "super_read_only": None,
            "update_on_target": None,
        }
        try:
            rows = self._rw.fetch_all(_SQL_IDENTITY)
        except Exception:  # noqa: BLE001
            logger.warning("身份探测失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return empty
        if not rows:
            return empty
        row = rows[0]
        current_user = row.get("current_user")
        identity: dict[str, Any] = {
            "current_user": str(current_user) if current_user is not None else None,
            "read_only": _to_bool_flag(row.get("read_only")),
            "super_read_only": _to_bool_flag(row.get("super_read_only")),
            "update_on_target": None,
        }
        try:
            grant_rows = self._rw.fetch_all(_SQL_SHOW_GRANTS)
        except Exception:  # noqa: BLE001
            logger.warning("SHOW GRANTS 失败", extra={"alert": "PREPARE_PROBE_PARTIAL"})
            return identity
        identity["update_on_target"] = has_update_on_target(
            _grant_rows_to_text(grant_rows)
        )
        return identity

    def _observe_capabilities(self) -> tuple[str, ...] | None:
        """能力矩阵：目标库版本 + 装配方注入的额外能力。

        版本探测失败 → `None`（上层判 UNKNOWN，不谎称具备能力）。
        """
        capabilities: set[str] = set()
        if self._capability_provider is not None:
            try:
                capabilities.update(str(item) for item in self._capability_provider())
            except Exception:  # noqa: BLE001
                logger.warning(
                    "能力供应器失败", extra={"alert": "PREPARE_PROBE_PARTIAL"}
                )
        try:
            identity = self._ro.identity()
        except Exception:  # noqa: BLE001
            # 版本不可知即无法判定 `optimistic_update`：按 UNKNOWN 处理（阻断）。
            return None
        if _parse_supports_optimistic_update(getattr(identity, "server_version", None)):
            capabilities.add(OPTIMISTIC_UPDATE_CAPABILITY)
        return tuple(sorted(capabilities))


__all__ = [
    "MysqlPrepareProbe",
    "OPTIMISTIC_UPDATE_CAPABILITY",
    "PREPARE_PROBE_SQL",
    "has_update_on_target",
]
