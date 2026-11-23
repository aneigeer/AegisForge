"""P2-T3 单元测试：ToolGateway 的 pre-flight / 爆炸半径守卫。

判定必须发生在**审批消费之前**（审批一次性，不能为"注定被拒"的写烧掉额度）。
本测试直接驱动 `_preflight_denial`，覆盖五条分支：

1. 读工具不触发；
2. 写工具缺失 `preflight` → `PREFLIGHT_UNAVAILABLE`（fail closed）；
3. dry-run 命中 0 行 → `PREFLIGHT_NO_TARGET`；
4. 预计影响超上限 → `BLAST_RADIUS_EXCEEDED`（含审计事件）；
5. 命中且在阈值内 → 放行（None）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.gateway.tool_gateway import ToolGateway
from core.tools.contracts import PreflightEstimate, ToolDefinition


class _Tool:
    def __init__(self, estimate: PreflightEstimate | None) -> None:
        self.definition = _definition()
        if estimate is not None:
            self.preflight = lambda context, params: estimate


class _Registry:
    def __init__(self, tool: _Tool) -> None:
        self._tool = tool

    def get(self, name: str) -> _Tool:
        return self._tool


class _Gateway:
    """只装配 `_preflight_denial` 需要的三件依赖。"""

    _preflight_denial = ToolGateway._preflight_denial
    _preflight_denied = ToolGateway._preflight_denied

    def __init__(self, tool: _Tool) -> None:
        self._registry = _Registry(tool)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._context_factory = lambda envelope, task: SimpleNamespace(task_id=task.task_id)

    def _emit(self, event_type: Any, envelope: Any, task: Any, payload: dict[str, Any]) -> None:
        self.events.append((str(event_type), payload))


def _definition(*, side_effect: str = "write", preflight_required: bool = True) -> ToolDefinition:
    return ToolDefinition.model_construct(
        name="mysql_lab_update_order_note",
        description="probe",
        version="1.0",
        risk_level="R2",
        side_effect=side_effect,
        permissions=["mysql:write"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        preflight_required=preflight_required,
        max_affected_rows=1,
    )


def _envelope() -> Any:
    return SimpleNamespace(
        tool_name="mysql_lab_update_order_note",
        worker_id="worker_1",
        envelope_id="env_1",
        actor_user_id="user_1",
        nonce="0123456789abcdef",
    )


def _task() -> Any:
    return SimpleNamespace(task_id="task_1", run_id="run_1", environment_id="env_lab")


def _now() -> datetime:
    return datetime(2026, 11, 20, tzinfo=timezone.utc)


def test_read_tool_skips_preflight() -> None:
    gateway = _Gateway(_Tool(PreflightEstimate(affected_rows=99)))
    gateway._registry = _Registry(_Tool(PreflightEstimate(affected_rows=99)))
    # 读工具：即使 estimate 超额也不触发
    result = gateway._preflight_denial(
        _envelope(),
        _definition(side_effect="read"),
        {},
        _task(),
        _now(),
    )
    assert result is None
    assert gateway.events == []


def test_missing_preflight_fails_closed() -> None:
    gateway = _Gateway(_Tool(None))
    result = gateway._preflight_denial(_envelope(), _definition(), {}, _task(), _now())
    assert result is not None
    assert result.status.value == "denied"
    assert result.error is not None and result.error.code == "PREFLIGHT_UNAVAILABLE"


def test_no_target_is_denied() -> None:
    gateway = _Gateway(_Tool(PreflightEstimate(affected_rows=0, detail="目标行不存在")))
    result = gateway._preflight_denial(_envelope(), _definition(), {}, _task(), _now())
    assert result is not None and result.error is not None
    assert result.error.code == "PREFLIGHT_NO_TARGET"
    assert gateway.events and gateway.events[0][1]["deny_code"] == "PREFLIGHT_NO_TARGET"


def test_blast_radius_exceeded_is_denied_with_audit() -> None:
    gateway = _Gateway(_Tool(PreflightEstimate(affected_rows=5)))
    result = gateway._preflight_denial(_envelope(), _definition(), {}, _task(), _now())
    assert result is not None and result.error is not None
    assert result.error.code == "BLAST_RADIUS_EXCEEDED"
    payload = gateway.events[0][1]
    assert payload["affected_rows"] == 5 and payload["max_affected_rows"] == 1


def test_within_threshold_passes() -> None:
    gateway = _Gateway(_Tool(PreflightEstimate(affected_rows=1)))
    assert gateway._preflight_denial(_envelope(), _definition(), {}, _task(), _now()) is None
    assert gateway.events == []
