"""P2-T3 单元测试：工具结果可用性判定（PARTIAL 不再误判为失败）。

验收口径：**截断的只读工具必须让 AgentLoop 继续推理**，而不是以 `tool_partial`
提前收口；带 error 的 PARTIAL 与 FAILED 仍然失败（fail closed）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.agent.loop import _is_usable_tool_result
from core.errors import ErrorCode
from core.tools.contracts import ToolError, ToolResult, ToolSource, ToolStatus


def _result(status: ToolStatus, *, error: ToolError | None = None) -> ToolResult:
    return ToolResult(
        tool_name="get_query_stats",
        status=status,
        started_at="2026-11-20T00:00:00+00:00",
        data={"available": True, "statements": [1, 2, 3]},
        source=ToolSource(
            engine="postgresql",
            db_version="16.4",
            database_id="lab_pg_01",
            collected_at="2026-11-20T00:00:00+00:00",
            adapter_version="1.0",
        ),
        warnings=["结果已截断，未返回完整数据"] if status == ToolStatus.PARTIAL else [],
        duration_ms=1.0,
        error=error,
    )


def test_success_is_usable() -> None:
    assert _is_usable_tool_result(_result(ToolStatus.SUCCESS)) is True


def test_partial_without_error_is_usable() -> None:
    """截断但有数据 → 可继续（这正是 P2-T3 要修的误判）。"""
    assert _is_usable_tool_result(_result(ToolStatus.PARTIAL)) is True


def test_partial_with_error_is_not_usable() -> None:
    error = ToolError(code=ErrorCode.DB_QUERY_ERROR, message="查询失败")
    assert _is_usable_tool_result(_result(ToolStatus.PARTIAL, error=error)) is False


@pytest.mark.parametrize(
    "status", [ToolStatus.ERROR, ToolStatus.TIMEOUT, ToolStatus.DENIED, ToolStatus.NOT_SUPPORTED]
)
def test_failed_family_is_not_usable(status: ToolStatus) -> None:
    assert _is_usable_tool_result(_result(status)) is False
