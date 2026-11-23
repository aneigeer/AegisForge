"""W02 集成测试：真实 PG LAB 上启动 PostgreSQL MCP Server 并逐一调用固定工具。"""

from __future__ import annotations

import json

import pytest

from adapters.base import CapabilityStatus, DbEngine
from adapters.postgres_mcp.client import PostgresMcpClient
from core.tools.contracts import ToolContext, ToolStatus
from database.postgres.connection import PgClient

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
}


def _ok_or_partial(status: ToolStatus) -> bool:
    return status in {ToolStatus.SUCCESS, ToolStatus.PARTIAL}


def _assert_source(result) -> None:
    assert result.source.engine == "postgresql"
    assert result.source.adapter_version == "1.0"
    assert result.observed_at is not None
    assert result.capability_ref and result.capability_ref.startswith("postgresql:1.0:")
    assert result.source_metadata == {"adapter": "postgres-mcp"}


@pytest.fixture(scope="module")
def mcp_client(settings) -> PostgresMcpClient:
    if settings.pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN，跳过 PostgreSQL MCP 集成测试")
    client = PostgresMcpClient(settings.pg_ro_dsn, timeout_s=15.0)
    try:
        yield client
    finally:
        client.close()


def test_initialize_and_list_has_seven_tools(mcp_client: PostgresMcpClient) -> None:
    tools = mcp_client.list_tools()
    assert len(tools) >= 6
    assert len(tools) == 7
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool["inputSchema"]["additionalProperties"] is False

    specs = mcp_client.tool_specs()
    assert len(specs) == 7
    assert all(spec.engine == DbEngine.POSTGRESQL for spec in specs)
    assert all(spec.definition.risk_level == "R0" for spec in specs)


def test_get_connection_snapshot(mcp_client: PostgresMcpClient, pg_ro_client: PgClient) -> None:
    context = ToolContext(
        client=pg_ro_client,  # type: ignore[arg-type]
        task_id="task-w02",
        run_id="run-w02",
        tool_call_id="call-snapshot",
    )
    result = mcp_client.call_tool(
        "get_connection_snapshot", {"limit": 10}, context=context
    )
    assert _ok_or_partial(result.status), result.error
    assert result.task_id == "task-w02"
    assert result.run_id == "run-w02"
    assert result.tool_call_id == "call-snapshot"
    _assert_source(result)
    assert isinstance(result.data["sessions"], list)
    assert result.data["session_count"] <= 10


def test_get_lock_wait_edges_and_details(mcp_client: PostgresMcpClient) -> None:
    edges = mcp_client.call_tool("get_lock_wait_edges", {"limit": 20})
    assert _ok_or_partial(edges.status), edges.error
    assert isinstance(edges.data["edges"], list)
    assert isinstance(edges.data["root_pids"], list)
    assert edges.data["wait_count"] >= 0
    _assert_source(edges)

    details = mcp_client.call_tool("get_lock_details", {"limit": 20})
    assert _ok_or_partial(details.status), details.error
    assert isinstance(details.data["locks"], list)
    assert details.data["lock_count"] >= 0
    assert details.data["granted_count"] + details.data["waiting_count"] == details.data["lock_count"]


def test_get_transaction_metadata(mcp_client: PostgresMcpClient) -> None:
    result = mcp_client.call_tool("get_transaction_metadata", {"limit": 20})
    assert _ok_or_partial(result.status), result.error
    assert isinstance(result.data["transactions"], list)
    assert result.data["transaction_count"] >= 0


def test_get_table_stats(mcp_client: PostgresMcpClient) -> None:
    result = mcp_client.call_tool("get_table_stats", {"table": "orders"})
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.data["table"] == "orders"
    assert result.data["table_count"] >= 1
    assert result.data["estimated_rows"] >= 0
    assert result.data["total_bytes"] > 0
    assert result.data["total_size"]
    _assert_source(result)


def test_get_query_stats_is_explicit_on_pg12(
    mcp_client: PostgresMcpClient, pg_ro_client: PgClient
) -> None:
    installed = pg_ro_client.fetch_all(
        "SELECT count(*) AS installed FROM pg_extension WHERE extname = 'pg_stat_statements'"
    )
    has_extension = int(installed[0]["installed"] or 0) > 0

    result = mcp_client.call_tool("get_query_stats", {"limit": 20})
    _assert_source(result)
    if not has_extension:
        assert result.status == ToolStatus.NOT_SUPPORTED
        assert result.warnings
        assert result.data["available"] is False
        assert result.data["statements"] == []
    else:
        assert _ok_or_partial(result.status), result.error
        assert result.data["available"] is True


def test_explain_query_returns_json_plan(mcp_client: PostgresMcpClient) -> None:
    result = mcp_client.call_tool("explain_query", {"sql": "SELECT * FROM orders"})
    assert result.status == ToolStatus.SUCCESS, result.error
    assert "plan" in result.data
    assert result.data["plan_bytes"] > 0
    json.dumps(result.data["plan"])
    if not result.truncated:
        assert isinstance(result.data["plan"], list)
        assert result.data["plan"]


def test_explain_query_rejects_write_and_analyze_without_execution(
    mcp_client: PostgresMcpClient, pg_ro_client: PgClient
) -> None:
    before = int(pg_ro_client.fetch_all("SELECT count(*) AS c FROM orders")[0]["c"])

    update = mcp_client.call_tool(
        "explain_query", {"sql": "UPDATE orders SET status = 'paid'"}
    )
    assert update.status == ToolStatus.ERROR
    assert update.error is not None
    assert update.error.code == "REQUEST_INVALID"
    assert update.data == {}

    analyze = mcp_client.call_tool("explain_query", {"sql": "ANALYZE orders"})
    assert analyze.status == ToolStatus.ERROR
    assert analyze.error is not None
    assert analyze.error.code == "REQUEST_INVALID"
    assert analyze.data == {}

    after = int(pg_ro_client.fetch_all("SELECT count(*) AS c FROM orders")[0]["c"])
    assert after == before


def test_unknown_tool_returns_structured_error(mcp_client: PostgresMcpClient) -> None:
    result = mcp_client.call_tool("execute_any_sql", {"sql": "DROP TABLE customers"})
    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"
    assert result.data == {}


def test_unknown_table_name_is_rejected(mcp_client: PostgresMcpClient) -> None:
    result = mcp_client.call_tool("get_table_stats", {"table": "orders; DROP TABLE customers"})
    assert result.status == ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code == "REQUEST_INVALID"
    assert result.data == {}


def test_probe_capabilities_reports_required_fields(mcp_client: PostgresMcpClient) -> None:
    report = mcp_client.probe_capabilities()
    assert report.engine == DbEngine.POSTGRESQL
    assert report.server_version.startswith(("12.", "16."))
    assert report.adapter_version == "1.0"
    for key in ("pg_stat_activity", "pg_locks", "pg_blocking_pids", "pg_stat_statements"):
        assert key in report.capabilities
        assert isinstance(report.capabilities[key], CapabilityStatus)


def test_close_terminates_subprocess(settings) -> None:
    if settings.pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN，跳过 PostgreSQL MCP 集成测试")
    client = PostgresMcpClient(settings.pg_ro_dsn, timeout_s=15.0)
    pid = client.pid
    assert pid is not None
    assert client.is_running is True
    client.close()
    assert client.is_running is False
    assert client.pid == pid
