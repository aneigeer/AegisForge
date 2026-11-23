from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel, ConfigDict

from core.tools.contracts import ToolContext, ToolResult, ToolStatus
from database.mysql.capabilities import detect_capabilities
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry
from database.observers.base import ObserverTool

pytestmark = pytest.mark.integration


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SlowJoinTool(ObserverTool):
    name = "test_slow_join"
    description = "测试专用：触发语句超时"
    input_model = _NoParams
    timeout_ms = 10_000
    output_schema = {"type": "object"}

    def _execute(self, context: ToolContext, params: BaseModel):
        context.client.execute(
            "SELECT SUM(a.amount_cents + b.amount_cents + c.amount_cents + d.amount_cents "
            "+ e.amount_cents) AS s FROM dg_lab.seed_orders a "
            "JOIN dg_lab.seed_orders b JOIN dg_lab.seed_orders c "
            "JOIN dg_lab.seed_orders d JOIN dg_lab.seed_orders e"
        )
        return {}, []


@pytest.fixture(scope="session")
def registry():
    return build_observer_registry()


def _run(
    registry,
    client: MysqlClient,
    name: str,
    params: dict | None = None,
) -> ToolResult:
    tool = registry.get(name)
    context = ToolContext(client=client, task_id="task_w02", tool_call_id="call_w02")
    return tool.run(context, params or {})


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("get_db_version", {}),
        ("get_db_settings", {}),
        ("get_activity", {"include_idle": True}),
        ("get_locks", {"limit": 10}),
        ("get_blocking_tree", {"limit": 10}),
        ("get_query_stats", {"limit": 10}),
        ("get_table_stats", {"limit": 10}),
        ("get_index_stats", {"limit": 50}),
        ("get_database_size", {}),
        (
            "explain_query",
            {"sql": "SELECT order_id FROM dg_lab.seed_orders WHERE order_id = 2001"},
        ),
    ],
)
def test_observer_tools_return_unified_contract(registry, ro_client, name, params) -> None:
    result = _run(registry, ro_client, name, params)
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.tool_name == name
    assert result.task_id == "task_w02"
    assert result.tool_call_id == "call_w02"
    assert result.duration_ms >= 0
    assert result.source.engine == "mysql"
    assert result.source.db_version.startswith("8.")
    assert result.source.collected_at.endswith("+00:00")
    assert isinstance(result.data, dict)


def test_get_db_version_content(registry, ro_client) -> None:
    result = _run(registry, ro_client, "get_db_version")
    assert result.status == ToolStatus.SUCCESS
    assert result.data["version"].startswith("8.")
    assert result.data["current_user"].startswith("dg_ro@")
    assert result.data["current_database"] == "dg_lab"
    assert result.data["performance_schema_enabled"] == 1


def test_get_db_settings_only_returns_allowlisted(registry, ro_client) -> None:
    result = _run(
        registry,
        ro_client,
        "get_db_settings",
        {"names": ["max_connections", "sql_mode", "datadir", "password", "version"]},
    )
    assert result.status == ToolStatus.SUCCESS
    assert set(result.data["settings"]) <= {"max_connections", "sql_mode"}
    assert "max_connections" in result.data["settings"]
    assert set(result.data["excluded"]) == {"datadir", "password", "version"}
    assert any("白名单" in warning for warning in result.warnings)


def test_get_activity_includes_own_session(registry, ro_client) -> None:
    result = _run(registry, ro_client, "get_activity", {"include_idle": True})
    assert result.status == ToolStatus.SUCCESS
    assert result.data["row_count"] >= 1
    sessions = result.data["sessions"]
    own = [session for session in sessions if str(session["user"]).startswith("dg_ro")]
    assert own
    assert own[0]["processlist_id"] is not None
    assert "sql_text" in own[0]


def test_get_table_stats_for_sandbox_database(registry, ro_client) -> None:
    result = _run(registry, ro_client, "get_table_stats", {"database_name": "dg_lab"})
    assert result.status == ToolStatus.SUCCESS
    names = {table["table_name"] for table in result.data["tables"]}
    assert {"seed_customers", "seed_orders"} & names
    seed = next(table for table in result.data["tables"] if table["table_name"] == "seed_orders")
    assert seed["total_bytes"] > 0
    assert seed["total_bytes"] == seed["data_bytes"] + seed["index_bytes"]


def test_get_index_stats_for_orders(registry, ro_client) -> None:
    result = _run(
        registry,
        ro_client,
        "get_index_stats",
        {"database_name": "dg_lab", "table_name": "seed_orders"},
    )
    assert result.status == ToolStatus.SUCCESS
    index_names = {index["index_name"] for index in result.data["indexes"]}
    assert "PRIMARY" in index_names
    for index in result.data["indexes"]:
        assert index["columns"]
        assert "read_unused" in index


def test_get_query_stats_shape(registry, ro_client) -> None:
    result = _run(registry, ro_client, "get_query_stats", {"limit": 5})
    assert result.status == ToolStatus.SUCCESS
    assert isinstance(result.data["statements"], list)
    for statement in result.data["statements"]:
        assert "digest_text" in statement
        assert statement["total_time_ms"] is None or statement["total_time_ms"] >= 0


def test_explain_returns_json_plan_and_masks_literals(registry, ro_client) -> None:
    result = _run(
        registry,
        ro_client,
        "explain_query",
        {"sql": "SELECT * FROM dg_lab.seed_orders WHERE order_id = 2001"},
    )
    assert result.status == ToolStatus.SUCCESS
    assert result.data["format"] == "json"
    assert isinstance(result.data["plan"], dict)
    assert "query_block" in result.data["plan"]
    assert result.data["query_preview"] is not None
    assert "2001" not in result.data["query_preview"]
    assert result.data["analyze"] is False


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("get_activity", {"min_duration_seconds": 86_400}),
        ("get_locks", {"table_name": "table_that_does_not_exist_zzz"}),
        ("get_query_stats", {"min_exec_count": 1_000_000_000}),
        ("get_table_stats", {"database_name": "db_that_does_not_exist_zzz"}),
        ("get_index_stats", {"database_name": "db_that_does_not_exist_zzz"}),
        ("get_database_size", {"database_name": "db_that_does_not_exist_zzz"}),
    ],
)
def test_empty_results_are_explicit(registry, ro_client, name, params) -> None:
    result = _run(registry, ro_client, name, params)
    assert result.status == ToolStatus.SUCCESS
    assert result.warnings
    assert any("无" in warning or "未" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dg_lab.seed_orders (order_id) VALUES (999)",
        "UPDATE dg_lab.seed_orders SET status = 'paid'",
        "SELECT 1; SELECT 2",
        "SELECT * INTO OUTFILE '/tmp/x' FROM dg_lab.seed_orders",
    ],
)
def test_explain_rejects_non_readonly_sql(registry, ro_client, sql) -> None:
    result = _run(registry, ro_client, "explain_query", {"sql": sql})
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "POLICY_DENIED"


def test_explain_rejects_analyze(registry, ro_client) -> None:
    result = _run(
        registry,
        ro_client,
        "explain_query",
        {"sql": "SELECT 1", "analyze": True},
    )
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "POLICY_DENIED"


def test_permission_denied_is_reported_as_denied(registry, settings) -> None:
    rw_client = MysqlClient(
        settings.rw_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
    )
    result = _run(registry, rw_client, "get_activity", {"include_idle": True})
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "DB_PERMISSION_DENIED"


def test_disconnect_is_reported_without_crash(registry) -> None:
    client = MysqlClient(
        "mysql://dg_ro:wrong@127.0.0.1:3399/dg_lab",
        database_id="lab_mysql_01",
        read_only=True,
        connect_timeout_s=1,
    )
    result = _run(registry, client, "get_db_version")
    assert result.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}
    assert result.error is not None
    assert result.error.code in {"DB_UNAVAILABLE", "DB_TIMEOUT"}
    assert result.source.db_version == "unknown"


def test_tool_timeout_status(registry, settings) -> None:
    client = MysqlClient(
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=50,
    )
    tool = _SlowJoinTool()
    result = tool.run(ToolContext(client=client, task_id="task_w02", tool_call_id="call_w02"))
    assert result.status == ToolStatus.TIMEOUT
    assert result.error is not None
    assert result.error.code == "DB_TIMEOUT"


def test_capabilities_report(ro_client) -> None:
    report = detect_capabilities(ro_client)
    assert report.server_version.startswith("8.")
    assert report.performance_schema_enabled is True
    assert report.process_privilege is True
    assert all(report.tables.values())
    assert report.warnings == []


def test_locks_and_blocking_tree_under_contention(registry, ro_client, rw_client) -> None:
    holder = rw_client.connect()
    waiter = rw_client.connect()
    started = threading.Event()
    errors: list[BaseException] = []

    def wait_for_lock() -> None:
        try:
            with waiter.cursor() as cur:
                cur.execute("SET SESSION innodb_lock_wait_timeout = 5")
                cur.execute("BEGIN")
                started.set()
                cur.execute(
                    "SELECT * FROM dg_lab.seed_orders WHERE order_id = 2001 FOR UPDATE"
                )
            waiter.commit()
        except BaseException as exc:  # noqa: BLE001 - 测试线程需要带回错误
            errors.append(exc)

    try:
        with holder.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SELECT * FROM dg_lab.seed_orders WHERE order_id = 2001 FOR UPDATE")

        thread = threading.Thread(target=wait_for_lock, daemon=True)
        thread.start()
        assert started.wait(timeout=2)
        time.sleep(0.4)

        locks = _run(
            registry,
            ro_client,
            "get_locks",
            {"database_name": "dg_lab", "table_name": "seed_orders"},
        )
        tree = _run(registry, ro_client, "get_blocking_tree", {"limit": 50})
        assert locks.status == ToolStatus.SUCCESS
        assert tree.status == ToolStatus.SUCCESS
        assert len(locks.data["locks"]) >= 2
        assert any(row["lock_data"] == "<redacted>" for row in locks.data["locks"])
        assert tree.data["wait_count"] >= 1
        edge = tree.data["edges"][0]
        assert edge["object_name"] == "seed_orders"
        assert edge["waiting_processlist_id"] is not None
        assert edge["blocking_processlist_id"] is not None
        assert edge["blocking_transaction_age_seconds"] is not None
        assert tree.data["roots"]

        with holder.cursor() as cur:
            cur.execute("ROLLBACK")
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not errors, errors
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        try:
            waiter.rollback()
        except Exception:
            pass
        holder.close()
        waiter.close()
