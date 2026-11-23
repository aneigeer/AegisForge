"""W02 单元测试：MCP 协议模型、输入校验与静态工具目录（不连接数据库）。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from adapters.postgres_mcp.protocol import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    InitializeResult,
    JsonRpcResponse,
    McpTool,
    ServerInfo,
    parse_request_line,
)
from adapters.postgres_mcp.server import PostgresMcpServer
from adapters.postgres_mcp.tools import (
    INTERNAL_CAPABILITIES_TOOL,
    TOOL_DEFINITIONS,
    ExplainQueryInput,
    TableStatsInput,
    tool_list_payload,
    validate_explain_sql,
    validate_identifier,
)
from core.tools.contracts import ToolResult, ToolStatus


class _FakeIdentity:
    server_version = "12.18"


class _FakeClient:
    def identity(self) -> _FakeIdentity:
        return _FakeIdentity()


def test_request_and_response_models_round_trip() -> None:
    request = parse_request_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "get_table_stats", "arguments": {"table": "orders"}},
            }
        )
    )
    assert request.id == 7
    assert request.method == "tools/call"
    assert request.params == {"name": "get_table_stats", "arguments": {"table": "orders"}}
    assert json.loads(request.to_json())["jsonrpc"] == "2.0"

    notification = parse_request_line(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert notification.id is None

    error = JsonRpcResponse(
        id=7,
        error={"code": -32601, "message": "Method not found"},  # type: ignore[arg-type]
    )
    payload = json.loads(error.to_json())
    assert payload["error"]["code"] == -32601
    assert "result" not in payload


def test_initialize_and_tool_models_use_mcp_field_names() -> None:
    init = InitializeResult(
        protocolVersion=PROTOCOL_VERSION,
        capabilities={"tools": {}},
        serverInfo=ServerInfo(name=SERVER_NAME, version="1.0"),
    )
    payload = init.to_payload()
    assert payload["protocolVersion"] == "2024-11-05"
    assert payload["capabilities"] == {"tools": {}}
    assert payload["serverInfo"] == {"name": SERVER_NAME, "version": "1.0"}

    tool = McpTool(
        name="get_table_stats",
        description="x",
        inputSchema={"type": "object", "additionalProperties": False},
    )
    assert tool.to_payload()["inputSchema"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET status = 'paid'",
        "DELETE FROM orders",
        "ANALYZE orders",
        "EXPLAIN ANALYZE SELECT 1",
        "SELECT 1; SELECT 2",
        "SELECT * INTO copied_orders FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "WITH x AS (DELETE FROM orders RETURNING order_id) SELECT * FROM x",
    ],
)
def test_explain_query_rejects_non_readonly_or_multi_statement(sql: str) -> None:
    with pytest.raises(ValidationError):
        ExplainQueryInput(sql=sql)
    with pytest.raises(ValueError):
        validate_explain_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "  select order_id from orders where status = 'paid'  ",
        "WITH recent AS (SELECT * FROM orders WHERE order_id > 2000) SELECT * FROM recent",
        "-- leading comment\nSELECT 1",
    ],
)
def test_explain_query_accepts_single_select_or_with(sql: str) -> None:
    assert ExplainQueryInput(sql=sql).sql


@pytest.mark.parametrize(
    "table",
    ["orders; DROP TABLE customers", "1orders", "orders-name", "a" * 64, "", "orders table"],
)
def test_table_stats_rejects_invalid_identifier(table: str) -> None:
    with pytest.raises(ValidationError):
        TableStatsInput(table=table)
    if table:
        with pytest.raises(ValueError):
            validate_identifier(table)


def test_table_stats_accepts_valid_identifier() -> None:
    assert TableStatsInput(table="orders_v2").table == "orders_v2"
    assert TableStatsInput(table="_private").table == "_private"


def test_static_tool_catalog_has_seven_readonly_tools() -> None:
    payload = tool_list_payload()
    names = [item["name"] for item in payload]
    assert len(payload) == 7
    assert set(names) == {
        "get_connection_snapshot",
        "get_query_stats",
        "get_lock_wait_edges",
        "get_lock_details",
        "get_transaction_metadata",
        "explain_query",
        "get_table_stats",
    }
    assert INTERNAL_CAPABILITIES_TOOL not in names
    for item in payload:
        schema = item["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, item["name"]


def test_tool_definitions_are_r0_readonly_pg_lab() -> None:
    assert len(TOOL_DEFINITIONS) == 7
    for definition in TOOL_DEFINITIONS.values():
        assert definition.risk_level == "R0"
        assert definition.side_effect == "read"
        assert definition.db_engines == ["postgresql"]
        assert definition.allowed_environments == ["LAB"]
        assert definition.operation_class == "observe"


def test_server_unknown_tool_returns_is_error_without_sql() -> None:
    server = PostgresMcpServer(_FakeClient(), database_id="lab_pg_01")  # type: ignore[arg-type]
    response = server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "run_any_sql",
                    "arguments": {"sql": "DROP TABLE customers"},
                },
            }
        )
    )
    assert response is not None
    result_payload = response["result"]
    assert result_payload["isError"] is True
    text = result_payload["content"][0]["text"]
    parsed = ToolResult.model_validate_json(text)
    assert parsed.tool_name == "run_any_sql"
    assert parsed.status == ToolStatus.ERROR
    assert parsed.error is not None
    assert parsed.error.code == "TOOL_NOT_FOUND"
    assert parsed.data == {}


def test_server_initialize_and_unknown_method() -> None:
    server = PostgresMcpServer(_FakeClient(), database_id="lab_pg_01")  # type: ignore[arg-type]
    init = server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    )
    assert init is not None
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["serverInfo"]["name"] == SERVER_NAME

    unknown = server.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    )
    assert unknown is not None
    assert unknown["error"]["code"] == -32601

    notification = server.handle_line(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert notification is None


def test_server_rejects_invalid_arguments_before_execution() -> None:
    server = PostgresMcpServer(_FakeClient(), database_id="lab_pg_01")  # type: ignore[arg-type]
    response = server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "explain_query", "arguments": {"sql": "ANALYZE orders"}},
            }
        )
    )
    assert response is not None
    payload = response["result"]
    assert payload["isError"] is True
    result = ToolResult.model_validate_json(payload["content"][0]["text"])
    assert result.status == ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code == "REQUEST_INVALID"
    assert result.data == {}


__all__: list[str] = []
