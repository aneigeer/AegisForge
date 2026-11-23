"""W02 MySQL Native Adapter 集成测试（需要真实 MySQL 8 实例）。"""

from __future__ import annotations

from typing import Any

import pytest

from adapters.base import CapabilityStatus, DbEngine
from adapters.mysql import MySQLAdapter
from core.errors import DatabasePermissionError
from core.tools.contracts import ToolContext, ToolStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def adapter(ro_client) -> MySQLAdapter:
    return MySQLAdapter(ro_client)


def _context(client: Any) -> ToolContext:
    return ToolContext(
        client=client,
        task_id="task_w02",
        run_id="run_w02",
        tool_call_id="call_w02",
    )


def test_adapter_identity_matches_client(adapter: MySQLAdapter, ro_client) -> None:
    assert adapter.engine is DbEngine.MYSQL
    assert adapter.database_id == ro_client.database_id
    assert adapter.adapter_version == "1.0"


def test_probe_capabilities_returns_valid_status_and_version(adapter: MySQLAdapter) -> None:
    report = adapter.probe_capabilities()

    assert report.engine is DbEngine.MYSQL
    assert report.database_id == adapter.database_id
    assert report.server_version.startswith("8.")
    assert report.checked_at.endswith("+00:00")
    assert report.capability_ref == f"mysql:{adapter.adapter_version}:{report.checked_at}"
    assert all(
        isinstance(status, CapabilityStatus) for status in report.capabilities.values()
    )
    assert report.capabilities["performance_schema"] is CapabilityStatus.AVAILABLE
    assert report.capabilities["performance_schema.threads"] is CapabilityStatus.AVAILABLE


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("get_connection_snapshot", {"include_idle": True, "limit": 20}),
        ("get_query_stats", {"limit": 10}),
        ("get_lock_wait_edges", {"limit": 10}),
        ("get_lock_details", {"limit": 10}),
        ("get_transaction_metadata", {"limit": 10}),
        (
            "explain_query",
            {"sql": "SELECT order_id FROM dg_lab.seed_orders WHERE order_id = 2001"},
        ),
        ("get_table_stats", {"database_name": "dg_lab", "limit": 10}),
    ],
)
def test_all_adapter_tools_return_unified_result(
    adapter: MySQLAdapter, ro_client, name: str, params: dict[str, Any]
) -> None:
    adapter.probe_capabilities()
    result = adapter.call_tool(name, _context(ro_client), params)

    assert result.status in {ToolStatus.SUCCESS, ToolStatus.PARTIAL}, (
        result.status,
        result.error,
        result.warnings,
    )
    if result.status is ToolStatus.PARTIAL:
        assert result.warnings
    assert result.tool_name == name
    assert result.source.engine == "mysql"
    assert result.source.adapter_version == "1.0"
    assert result.source.database_id == ro_client.database_id
    assert result.capability_ref is not None
    assert result.capability_ref.startswith("mysql:1.0:")
    assert result.observed_at is not None
    assert result.source_metadata["adapter"] == "mysql-native"
    assert result.source_metadata["capability_ref"] == result.capability_ref


def test_transaction_metadata_is_available(adapter: MySQLAdapter, ro_client) -> None:
    result = adapter.call_tool(
        "get_transaction_metadata", _context(ro_client), {"limit": 20}
    )

    assert result.status in {ToolStatus.SUCCESS, ToolStatus.PARTIAL}
    assert isinstance(result.data.get("transactions"), list)
    assert result.data.get("row_count") == len(result.data["transactions"])


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE dg_lab.seed_orders SET status = 'paid' WHERE order_id = 2001",
        "SELECT 1; SELECT 2",
        "DELETE FROM dg_lab.seed_orders WHERE order_id = 2001",
    ],
)
def test_explain_query_rejects_non_readonly_sql(
    adapter: MySQLAdapter, ro_client, sql: str
) -> None:
    result = adapter.call_tool("explain_query", _context(ro_client), {"sql": sql})

    assert result.status in {ToolStatus.DENIED, ToolStatus.ERROR}
    assert result.error is not None
    assert result.error.code in {"POLICY_DENIED", "REQUEST_INVALID"}


def test_extra_params_are_rejected(adapter: MySQLAdapter, ro_client) -> None:
    adapter.probe_capabilities()
    result = adapter.call_tool(
        "get_connection_snapshot",
        _context(ro_client),
        {"include_idle": True, "arbitrary_sql": "DROP TABLE dg_lab.seed_orders"},
    )

    assert result.status is not ToolStatus.SUCCESS
    assert result.error is not None
    assert result.error.code == "REQUEST_INVALID"


def test_readonly_account_write_is_still_denied_by_database(ro_client) -> None:
    with pytest.raises(DatabasePermissionError) as excinfo:
        ro_client.execute(
            "UPDATE dg_lab.seed_orders SET status = 'paid' WHERE order_id = 2001"
        )

    assert excinfo.value.code.value == "DB_PERMISSION_DENIED"


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("get_lock_details", {"limit": 501}),
        ("get_query_stats", {"limit": 201}),
    ],
)
def test_limit_above_tool_maximum_is_rejected(
    adapter: MySQLAdapter, ro_client, name: str, params: dict[str, Any]
) -> None:
    adapter.probe_capabilities()
    result = adapter.call_tool(name, _context(ro_client), params)

    assert result.status is ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code == "REQUEST_INVALID"
