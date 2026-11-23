"""MySQL Native Adapter：固定 7 个只读工具的 DbEngine 适配层。"""

from __future__ import annotations

from typing import Any

from adapters.base import (
    AdapterToolSpec,
    CapabilityReport,
    CapabilityStatus,
    DbEngine,
)
from adapters.mysql.tools import GetTransactionMetadataTool
from core.errors import DBGuardianError, ErrorCode
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from database.mysql.capabilities import detect_capabilities
from database.mysql.connection import MysqlClient
from database.observers.activity import GetActivityTool
from database.observers.base import ObserverTool
from database.observers.explain import ExplainQueryTool
from database.observers.locks import GetBlockingTreeTool, GetLocksTool
from database.observers.query_stats import GetQueryStatsTool
from database.observers.table_stats import GetTableStatsTool
from database.observers.utils import utc_now_iso

_ADAPTER_NAME = "mysql-native"
_TOOL_VERSION = "1.0.1"
_ROLE = "dg_ro"
_ROW_LIMIT = 500

_REQUIRED_CAPABILITIES: dict[str, list[str]] = {
    "get_connection_snapshot": [
        "performance_schema.threads",
        "performance_schema.events_statements_current",
    ],
    "get_query_stats": ["performance_schema.events_statements_summary_by_digest"],
    "get_lock_wait_edges": [
        "performance_schema.data_lock_waits",
        "performance_schema.data_locks",
    ],
    "get_lock_details": ["performance_schema.data_locks"],
    "get_transaction_metadata": ["information_schema.innodb_trx"],
    "explain_query": [],
    "get_table_stats": [],
}


def _build_tools() -> dict[str, ObserverTool]:
    return {
        "get_connection_snapshot": GetActivityTool(),
        "get_query_stats": GetQueryStatsTool(),
        "get_lock_wait_edges": GetBlockingTreeTool(),
        "get_lock_details": GetLocksTool(),
        "get_transaction_metadata": GetTransactionMetadataTool(),
        "explain_query": ExplainQueryTool(),
        "get_table_stats": GetTableStatsTool(),
    }


def _capability_statuses(detected: Any) -> dict[str, CapabilityStatus]:
    statuses: dict[str, CapabilityStatus] = {
        "performance_schema": (
            CapabilityStatus.AVAILABLE
            if detected.performance_schema_enabled
            else CapabilityStatus.NOT_SUPPORTED
        )
    }
    for table, exists in detected.tables.items():
        statuses[f"performance_schema.{table}"] = (
            CapabilityStatus.AVAILABLE if exists else CapabilityStatus.NOT_SUPPORTED
        )
    for name, enabled in detected.consumers.items():
        statuses[f"performance_schema.consumer.{name}"] = (
            CapabilityStatus.AVAILABLE
            if str(enabled).strip().upper() == "YES"
            else CapabilityStatus.PARTIAL
        )
    privilege_status = (
        CapabilityStatus.AVAILABLE if detected.process_privilege else CapabilityStatus.DENIED
    )
    statuses["process_privilege"] = privilege_status
    statuses["information_schema.innodb_trx"] = privilege_status
    return statuses


class MySQLAdapter:
    """适配 MySQL 观察工具；不接受任意 SQL/host/port，只暴露固定只读工具。"""

    engine = DbEngine.MYSQL

    def __init__(
        self,
        client: MysqlClient,
        *,
        adapter_version: str = "1.0",
        capability_report: CapabilityReport | None = None,
    ) -> None:
        self._client = client
        self.adapter_version = adapter_version
        self.database_id = client.database_id
        self._tools = _build_tools()
        self._capability_report: CapabilityReport | None = capability_report

    @property
    def capability_ref(self) -> str | None:
        report = self._capability_report
        return report.capability_ref if report is not None else None

    def probe_capabilities(self) -> CapabilityReport:
        detected = detect_capabilities(self._client)
        report = CapabilityReport(
            engine=self.engine,
            database_id=self.database_id,
            server_version=detected.server_version,
            adapter_version=self.adapter_version,
            checked_at=utc_now_iso(),
            capabilities=_capability_statuses(detected),
            warnings=list(detected.warnings),
        )
        self._capability_report = report
        return report

    def tool_specs(self) -> list[AdapterToolSpec]:
        if self._capability_report is None:
            try:
                self.probe_capabilities()
            except DBGuardianError:
                # 探测失败时如实标记不可用，不默认放行。
                self._capability_report = None
        specs: list[AdapterToolSpec] = []
        for name, tool in self._tools.items():
            required = list(_REQUIRED_CAPABILITIES.get(name, []))
            available, reason = self._availability(required)
            specs.append(
                AdapterToolSpec(
                    definition=self._definition(tool.definition, name, required),
                    engine=self.engine,
                    available=available,
                    unavailable_reason=reason,
                )
            )
        return specs

    def call_tool(
        self, name: str, context: ToolContext, params: dict[str, Any] | None = None
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return self._unknown_tool_result(name)

        # 能力探测结果用于标注 capability_ref 与能力缺失降级；首次调用时惰性探测。
        if self._capability_report is None:
            try:
                self.probe_capabilities()
            except DBGuardianError:
                pass

        required = _REQUIRED_CAPABILITIES.get(name, [])
        available, reason = self._availability(required)
        if not available:
            return self._unavailable_result(name, context, reason or "能力缺失")

        result = tool.run(context, params)
        return self._enrich(name, result)

    def _definition(
        self, base: ToolDefinition, name: str, required: list[str]
    ) -> ToolDefinition:
        """以观察工具定义为基础生成 Adapter 工具元数据（校验字段全部显式）。"""
        return base.model_copy(
            update={
                "name": name,
                "version": _TOOL_VERSION,
                "risk_level": "R0",
                "side_effect": "read",
                "permissions": ["mysql:read"],
                "db_engines": ["mysql"],
                "allowed_environments": ["LAB"],
                "required_role": _ROLE,
                "operation_class": "observe",
                "row_limit": _ROW_LIMIT,
                "required_capabilities": required,
            }
        )

    def _availability(self, required: list[str]) -> tuple[bool, str | None]:
        if self._capability_report is None:
            return False, "能力未探测或探测失败，未执行工具"
        if not required:
            return True, None
        missing = [
            key
            for key in required
            if self._capability_report.capabilities.get(key)
            in {CapabilityStatus.DENIED, CapabilityStatus.NOT_SUPPORTED}
        ]
        if missing:
            return False, "能力缺失：" + ", ".join(missing)
        return True, None

    def _source(self, *, collected_at: str | None = None) -> ToolSource:
        report = self._capability_report
        return ToolSource(
            database_id=self.database_id,
            engine="mysql",
            db_version=report.server_version if report is not None else "unknown",
            collected_at=collected_at or utc_now_iso(),
            adapter_version=self.adapter_version,
        )

    def _source_metadata(self, capability_ref: str | None) -> dict[str, Any]:
        return {"adapter": _ADAPTER_NAME, "capability_ref": capability_ref}

    def _unknown_tool_result(self, name: str) -> ToolResult:
        started_at = utc_now_iso()
        cap_ref = self.capability_ref
        return ToolResult(
            tool_name=name,
            status=ToolStatus.DENIED,
            started_at=started_at,
            duration_ms=0.0,
            error=ToolError(
                code=ErrorCode.TOOL_NOT_FOUND.value,
                message="未知的 Adapter 工具，未执行任何 SQL",
                details={"tool_name": name},
            ),
            source=self._source(collected_at=started_at),
            observed_at=started_at,
            capability_ref=cap_ref,
            source_metadata=self._source_metadata(cap_ref),
        )

    def _unavailable_result(
        self, name: str, context: ToolContext, reason: str
    ) -> ToolResult:
        started_at = utc_now_iso()
        cap_ref = self.capability_ref
        return ToolResult(
            tool_name=name,
            status=ToolStatus.PARTIAL,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=started_at,
            duration_ms=0.0,
            warnings=[reason],
            source=self._source(collected_at=started_at),
            observed_at=started_at,
            capability_ref=cap_ref,
            source_metadata=self._source_metadata(cap_ref),
        )

    def _enrich(self, name: str, result: ToolResult) -> ToolResult:
        cap_ref = self.capability_ref
        observed_at = utc_now_iso()
        truncated = bool(
            result.truncated
            or result.data.get("truncated")
            or result.data.get("plan_truncated")
        )
        warnings = list(result.warnings)
        status = result.status
        if truncated and status == ToolStatus.SUCCESS:
            status = ToolStatus.PARTIAL
            if "结果已截断，未返回完整数据" not in warnings:
                warnings.append("结果已截断，未返回完整数据")
        source = result.source.model_copy(
            update={
                "engine": "mysql",
                "adapter_version": self.adapter_version,
            }
        )
        metadata = {**result.source_metadata, **self._source_metadata(cap_ref)}
        return result.model_copy(
            update={
                "tool_name": name,
                "status": status,
                "warnings": warnings,
                "source": source,
                "observed_at": observed_at,
                "truncated": truncated,
                "capability_ref": cap_ref,
                "source_metadata": metadata,
            }
        )


__all__ = ["MySQLAdapter"]
