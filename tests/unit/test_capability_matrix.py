"""W02 双引擎固定工具矩阵：名称、风险、引擎与 Schema 约束。"""

from __future__ import annotations

from adapters.base import CapabilityReport, CapabilityStatus, DbEngine
from adapters.mysql.adapter import MySQLAdapter
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS
from core.tools.contracts import ToolDefinition
from database.mysql.connection import MysqlClient

EXPECTED_TOOLS = {
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
}


def _mysql_specs() -> dict[str, ToolDefinition]:
    client = MysqlClient(
        "mysql://dg_ro:placeholder@127.0.0.1:3306/dg_lab",
        database_id="lab_mysql_01",
        read_only=True,
    )
    capabilities = {
        key: CapabilityStatus.AVAILABLE
        for key in (
            "performance_schema.threads",
            "performance_schema.events_statements_current",
            "performance_schema.events_statements_summary_by_digest",
            "performance_schema.data_lock_waits",
            "performance_schema.data_locks",
            "information_schema.innodb_trx",
        )
    }
    report = CapabilityReport(
        engine=DbEngine.MYSQL,
        database_id="lab_mysql_01",
        server_version="8.0.46",
        adapter_version="1.0",
        checked_at="2026-11-07T00:00:00+00:00",
        capabilities=capabilities,
    )
    adapter = MySQLAdapter(client, capability_report=report)
    return {spec.definition.name: spec.definition for spec in adapter.tool_specs()}


def test_both_engines_expose_same_seven_tool_names() -> None:
    assert set(_mysql_specs()) == EXPECTED_TOOLS
    assert set(TOOL_DEFINITIONS) == EXPECTED_TOOLS


def test_mysql_tool_definitions_are_readonly_and_mysql_only() -> None:
    for definition in _mysql_specs().values():
        assert definition.side_effect == "read"
        assert definition.risk_level == "R0"
        assert definition.db_engines == ["mysql"]
        assert definition.operation_class == "observe"
        assert definition.input_schema.get("additionalProperties") is False


def test_postgres_tool_definitions_are_readonly_and_pg_only() -> None:
    for definition in TOOL_DEFINITIONS.values():
        assert definition.side_effect == "read"
        assert definition.risk_level == "R0"
        assert definition.db_engines == ["postgresql"]
        assert definition.operation_class == "observe"
        assert definition.input_schema.get("additionalProperties") is False
