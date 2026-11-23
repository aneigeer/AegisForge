"""W02 契约测试：状态语义、双引擎元数据与 Adapter DTO。"""

from __future__ import annotations

from adapters.base import CapabilityReport, CapabilityStatus, DbEngine
from core.tools.contracts import (
    ToolDefinition,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from database.observers import build_observer_registry


def _source(engine: str = "mysql") -> ToolSource:
    return ToolSource(
        database_id="lab",
        engine=engine,  # type: ignore[arg-type]
        db_version="8.0.46",
        collected_at="2026-11-07T00:00:00+00:00",
        adapter_version="1.0",
    )


def test_tool_status_has_v22_semantics() -> None:
    assert {status.value for status in ToolStatus} == {
        "success",
        "partial",
        "error",
        "timeout",
        "denied",
        "not_supported",
        "unknown_outcome",
    }


def test_tool_result_supports_engine_and_capability_fields() -> None:
    result = ToolResult(
        tool_name="get_connection_snapshot",
        status=ToolStatus.PARTIAL,
        started_at="2026-11-07T00:00:00+00:00",
        duration_ms=12.0,
        data={"sessions": []},
        warnings=["performance_schema consumer 未开启"],
        source=_source("postgresql"),
        observed_at="2026-11-07T00:00:01+00:00",
        truncated=True,
        capability_ref="postgresql:1.0:2026-11-07T00:00:00+00:00",
        source_metadata={"sampling_window": "current"},
    )
    assert result.source.engine == "postgresql"
    assert result.truncated is True
    assert result.capability_ref


def test_tool_definition_defaults_and_double_engine_fields() -> None:
    definition = ToolDefinition(
        name="get_locks",
        description="锁详情",
        permissions=["mysql:read"],
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        required_capabilities=["performance_schema.data_locks"],
    )
    assert definition.operation_class == "observe"
    assert definition.row_limit == 500
    assert definition.allowed_environments == ["LAB"]


def test_observer_input_schemas_forbid_extra_fields() -> None:
    for definition in build_observer_registry().definitions():
        assert definition.input_schema.get("additionalProperties") is False, definition.name


def test_capability_report_exposes_ref() -> None:
    report = CapabilityReport(
        engine=DbEngine.POSTGRESQL,
        database_id="lab_pg_01",
        server_version="12.18",
        adapter_version="1.0",
        checked_at="2026-11-07T00:00:00+00:00",
        capabilities={"pg_stat_activity": CapabilityStatus.AVAILABLE},
    )
    assert report.capability_ref.startswith("postgresql:1.0:")
    assert report.capabilities["pg_stat_activity"] is CapabilityStatus.AVAILABLE
