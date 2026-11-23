"""PostgreSQL MCP 适配器：把 MCP Client 包装成统一 DatabaseAdapter。"""

from __future__ import annotations

from typing import Any

from adapters.base import AdapterToolSpec, CapabilityReport, CapabilityStatus, DbEngine
from adapters.postgres_mcp.client import PostgresMcpClient
from adapters.postgres_mcp.tools import ADAPTER_VERSION
from core.tools.contracts import ToolContext, ToolResult


class PostgresMcpAdapter:
    engine = DbEngine.POSTGRESQL

    def __init__(
        self,
        client: PostgresMcpClient,
        *,
        adapter_version: str = ADAPTER_VERSION,
        capability_report: CapabilityReport | None = None,
    ) -> None:
        self._client = client
        self.adapter_version = adapter_version
        self._capability_report = capability_report

    @property
    def database_id(self) -> str:
        return self._client.database_id

    def probe_capabilities(self) -> CapabilityReport:
        report = self._client.probe_capabilities()
        self._capability_report = report
        return report

    def tool_specs(self) -> list[AdapterToolSpec]:
        report = self._capability_report
        if report is None:
            try:
                report = self.probe_capabilities()
            except Exception:  # noqa: BLE001 - 探测失败不影响工具清单，但标记不可用
                report = None
        specs: list[AdapterToolSpec] = []
        for spec in self._client.tool_specs():
            name = spec.definition.name
            available = True
            reason: str | None = None
            if name == "get_query_stats" and report is not None:
                status = report.capabilities.get("pg_stat_statements", CapabilityStatus.UNKNOWN)
                if status not in {CapabilityStatus.AVAILABLE, CapabilityStatus.PARTIAL}:
                    available = False
                    reason = f"能力不可用：pg_stat_statements={status.value}"
            elif report is None:
                available = False
                reason = "能力未探测或探测失败，未执行工具"
            specs.append(
                AdapterToolSpec(
                    definition=spec.definition,
                    engine=self.engine,
                    available=available,
                    unavailable_reason=reason,
                )
            )
        return specs

    def call_tool(
        self, name: str, context: ToolContext, params: dict[str, Any] | None = None
    ) -> ToolResult:
        result = self._client.call_tool(name, params or {}, context=context)
        if result.source.engine != "postgresql":
            result = result.model_copy(
                update={
                    "source": result.source.model_copy(
                        update={
                            "engine": "postgresql",
                            "adapter_version": self.adapter_version,
                        }
                    )
                }
            )
        return result


__all__ = ["PostgresMcpAdapter"]
