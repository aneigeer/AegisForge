"""W02 集成：Adapter 固定工具经 ToolRegistry / Executor 执行。"""

from __future__ import annotations

import pytest

from adapters.mysql.adapter import MySQLAdapter
from adapters.postgres_mcp.adapter import PostgresMcpAdapter
from adapters.postgres_mcp.client import PostgresMcpClient
from adapters.registry import build_adapter_registry
from core.tools.contracts import ToolContext, ToolStatus
from core.tools.executor import ToolExecutor

pytestmark = pytest.mark.integration


def test_mysql_adapter_tools_run_through_executor(ro_client, settings) -> None:
    adapter = MySQLAdapter(ro_client)
    registry = build_adapter_registry(adapter)
    executor = ToolExecutor(registry)
    context = ToolContext(client=ro_client)

    result = executor.execute("get_connection_snapshot", {"limit": 10}, context)
    assert result.status in {ToolStatus.SUCCESS, ToolStatus.PARTIAL}
    assert result.source.engine == "mysql"
    assert result.source.adapter_version
    assert result.observed_at
    assert result.capability_ref

    truncated = executor.execute("get_table_stats", {"limit": 1}, context)
    assert truncated.truncated is True
    assert truncated.status is ToolStatus.PARTIAL
    assert any("截断" in warning for warning in truncated.warnings)


def test_postgres_adapter_tools_run_through_executor(pg_ro_client, settings) -> None:
    if settings.pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN")
    client = PostgresMcpClient(settings.pg_ro_dsn)
    try:
        adapter = PostgresMcpAdapter(client)
        registry = build_adapter_registry(adapter)
        executor = ToolExecutor(registry)
        context = ToolContext(client=pg_ro_client)

        result = executor.execute("get_connection_snapshot", {"limit": 10}, context)
        assert result.status in {ToolStatus.SUCCESS, ToolStatus.PARTIAL}
        assert result.source.engine == "postgresql"
        assert result.source.adapter_version
        assert result.capability_ref

        missing = executor.execute("run_shell", {}, context)
        assert missing.status is ToolStatus.DENIED
    finally:
        client.close()
