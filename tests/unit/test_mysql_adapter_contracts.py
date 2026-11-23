"""W02 MySQL Native Adapter 契约测试（无需真实数据库）。"""

from __future__ import annotations

from typing import Any

from adapters.base import CapabilityStatus, DbEngine
from adapters.mysql import MySQLAdapter
from core.tools.contracts import ToolContext, ToolStatus

EXPECTED_TOOLS = {
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
}


class _FakeMysqlClient:
    database_id = "lab_mysql_01"

    def __init__(self, *, missing_tables: set[str] | None = None) -> None:
        self.missing_tables = missing_tables or set()

    def fetch_all(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        *,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        if "VERSION()" in sql:
            return [{"version": "8.0.46", "ps_enabled": 1}]
        if "information_schema.TABLES" in sql:
            return [
                {"table_name": name}
                for name in (params or ())
                if name not in self.missing_tables
            ]
        if "setup_consumers" in sql:
            return [{"name": "events_statements_current", "enabled": "YES"}]
        if "information_schema.innodb_trx" in sql:
            return [{"n": 0}]
        return []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        raise AssertionError("Adapter 不应在单元测试中执行写 SQL")


class _ExplodingClient(_FakeMysqlClient):
    def fetch_all(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("未知工具不得执行 SQL")


def _adapter(client: _FakeMysqlClient | None = None) -> MySQLAdapter:
    return MySQLAdapter(client or _FakeMysqlClient())


def _context(client: Any) -> ToolContext:
    return ToolContext(
        client=client,
        task_id="task_w02",
        run_id="run_w02",
        tool_call_id="call_w02",
    )


def test_tool_specs_expose_fixed_seven_readonly_tools() -> None:
    specs = _adapter().tool_specs()

    assert len(specs) == 7
    assert {spec.definition.name for spec in specs} == EXPECTED_TOOLS
    for spec in specs:
        definition = spec.definition
        assert spec.engine is DbEngine.MYSQL
        assert spec.available is True
        assert spec.unavailable_reason is None
        assert definition.version == "1.0.1"
        assert definition.risk_level == "R0"
        assert definition.side_effect == "read"
        assert definition.permissions == ["mysql:read"]
        assert definition.db_engines == ["mysql"]
        assert definition.allowed_environments == ["LAB"]
        assert definition.required_role == "dg_ro"
        assert definition.operation_class == "observe"
        assert definition.row_limit == 500
        assert definition.input_schema["additionalProperties"] is False
        assert isinstance(definition.required_capabilities, list)


def test_tool_specs_declare_expected_capability_dependencies() -> None:
    by_name = {spec.definition.name: spec for spec in _adapter().tool_specs()}

    assert by_name["get_connection_snapshot"].definition.required_capabilities == [
        "performance_schema.threads",
        "performance_schema.events_statements_current",
    ]
    assert by_name["get_lock_details"].definition.required_capabilities == [
        "performance_schema.data_locks"
    ]
    assert by_name["get_lock_wait_edges"].definition.required_capabilities == [
        "performance_schema.data_lock_waits",
        "performance_schema.data_locks",
    ]
    assert by_name["get_query_stats"].definition.required_capabilities == [
        "performance_schema.events_statements_summary_by_digest"
    ]
    assert by_name["get_transaction_metadata"].definition.required_capabilities == [
        "information_schema.innodb_trx"
    ]
    assert by_name["explain_query"].definition.required_capabilities == []
    assert by_name["get_table_stats"].definition.required_capabilities == []


def test_unknown_tool_is_denied_without_executing_sql() -> None:
    client = _ExplodingClient()
    result = _adapter(client).call_tool(
        "execute_arbitrary_sql", _context(client), {"sql": "DROP TABLE x"}
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"
    assert result.tool_name == "execute_arbitrary_sql"
    assert result.source.engine == "mysql"
    assert result.source.adapter_version == "1.0"


def test_probe_capabilities_report_shape_and_ref() -> None:
    adapter = _adapter()
    report = adapter.probe_capabilities()

    assert report.engine is DbEngine.MYSQL
    assert report.database_id == "lab_mysql_01"
    assert report.server_version == "8.0.46"
    assert report.adapter_version == "1.0"
    assert report.checked_at.endswith("+00:00")
    assert report.capability_ref == f"mysql:1.0:{report.checked_at}"
    assert report.warnings == []
    assert all(
        isinstance(status, CapabilityStatus) for status in report.capabilities.values()
    )
    assert report.capabilities["performance_schema"] is CapabilityStatus.AVAILABLE
    assert report.capabilities["process_privilege"] is CapabilityStatus.AVAILABLE
    assert (
        report.capabilities["information_schema.innodb_trx"]
        is CapabilityStatus.AVAILABLE
    )


def test_missing_capability_marks_tool_unavailable_instead_of_removing_it() -> None:
    adapter = _adapter(_FakeMysqlClient(missing_tables={"data_locks"}))
    adapter.probe_capabilities()
    specs = adapter.tool_specs()
    by_name = {spec.definition.name: spec for spec in specs}

    assert len(specs) == 7
    assert by_name["get_lock_details"].available is False
    assert by_name["get_lock_details"].unavailable_reason is not None
    assert "performance_schema.data_locks" in (
        by_name["get_lock_details"].unavailable_reason or ""
    )
    assert by_name["get_lock_wait_edges"].available is False
    assert by_name["get_table_stats"].available is True
