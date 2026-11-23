"""W09 T05 / AC-18：真实控制库下的降级事件与「零工具调用」断言。

纪律（``03-task-cards.md:6-12``）：

- 只依赖 ``control_client``（dg_ctl），**不依赖** ``DG_ADMIN_DSN``、不依赖 PG 5432；
- 只在 ``task_events`` 追加事件、在 ``tasks``/``checkpoints`` 追加任务行，不清理、不 DELETE；
- 控制库不可用时 **skipped 带原因**（任务卡 ``:603`` 允许），不写「通过」；
- 不加 ``xfail``、不放宽断言。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.agent.loop import AgentLoop
from core.audit.service import AuditService
from core.errors import DBGuardianError
from core.evidence.models import (  # noqa: F401 - 仅用于类型/语义对照
    CollectionStatus,
)
from core.models.degradation import DegradeReason, DegradationController
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.report.brief import DegradedBrief
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

_TENANT = "tenant_local"


class _NeverPlanner:
    def create_plan(self, bundle: Any) -> Any:
        raise AssertionError("降级路径不得调用 planner")

    def assess(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("降级路径不得调用 planner")

    def propose_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("降级路径不得调用 planner")

    def validate_step(self, step: Any) -> None:
        raise AssertionError("降级路径不得调用 planner")


@pytest.fixture
def control_db(control_client):
    """控制库可用性探针：不可用一律 skip（带原因），绝不 failed。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


def _tool_call_count(control_db: Any, task_id: str) -> int:
    rows = control_db.fetch_all(
        "SELECT COUNT(*) AS c FROM dg_system.tool_calls WHERE task_id = %s",
        (task_id,),
    )
    return int(rows[0]["c"]) if rows else 0


def _event_types(events: list[Any]) -> list[str]:
    return [getattr(item.event_type, "value", item.event_type) for item in events]


def test_disabled_provider_run_emits_model_degraded_and_no_tool_calls(
    control_db: Any,
) -> None:
    repository = TaskRepository(control_db)
    service = TaskService(repository, ToolRegistry())
    task = service.create_task("W09 T05 降级审计", tenant_id=_TENANT)
    audit = AuditService(repository)
    controller = DegradationController(audit=audit, provider="disabled")
    loop = AgentLoop(
        service,
        ToolRegistry(),
        None,
        _NeverPlanner(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        context_factory=lambda task_id: None,
        degradation=controller,
        redaction_gate=RedactionGate(DataRedactionEngine()),
    )

    before = _tool_call_count(control_db, task.task_id)
    result = loop.run(task.task_id)
    after = _tool_call_count(control_db, task.task_id)

    assert isinstance(result, DegradedBrief)
    assert result.degraded is True
    assert result.model_used is False
    assert result.planning_suspended is True
    assert after == before, "降级路径不得产生任何工具调用"

    events, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["MODEL_DEGRADED", "AGENT_PLAN_CREATED"],
        limit=50,
    )
    degraded = [
        item
        for item in events
        if _event_types([item])[0] == "MODEL_DEGRADED"
    ]
    planned = [
        item
        for item in events
        if _event_types([item])[0] == "AGENT_PLAN_CREATED"
    ]
    assert len(degraded) == 1
    assert planned == [], "暂停新规划时不得产生 AGENT_PLAN_CREATED"
    payload = degraded[0].payload
    assert payload["reason"] == DegradeReason.MODEL_DISABLED.value
    assert payload["planning_suspended"] is True
    assert payload["model_used"] is False
    assert payload["retry_count"] == 0
    assert "model_version" in payload and payload["model_version"] is None
    assert "pin_target" in payload and payload["pin_target"] is None


def test_invalid_output_chain_emits_three_events_in_order(control_db: Any) -> None:
    repository = TaskRepository(control_db)
    service = TaskService(repository, ToolRegistry())
    task = service.create_task("W09 T05 非法输出事件链", tenant_id=_TENANT)
    audit = AuditService(repository)
    controller = DegradationController(audit=audit, provider="fake")

    assert controller.repair_attempt(
        1, task_id=task.task_id, run_id=task.run_id
    ) == "RETRY"
    before = _tool_call_count(control_db, task.task_id)
    assert controller.repair_attempt(
        2, task_id=task.task_id, run_id=task.run_id
    ) == "STOP"
    after = _tool_call_count(control_db, task.task_id)

    assert after == before, "非法输出降级路径不得产生工具调用"
    events, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=[
            "AGENT_RECOVERY_STARTED",
            "AGENT_RECOVERY_STOPPED",
            "MODEL_DEGRADED",
        ],
        limit=50,
    )
    ordered = sorted(events, key=lambda item: int(getattr(item, "seq", 0)))
    assert _event_types(ordered) == [
        "AGENT_RECOVERY_STARTED",
        "AGENT_RECOVERY_STOPPED",
        "MODEL_DEGRADED",
    ]
    stopped = ordered[1].payload
    assert stopped["outcome"] == "STOP_INVALID_OUTPUT"
    assert stopped["reason"] == DegradeReason.MODEL_OUTPUT_INVALID.value
    assert stopped["retry_count"] == 1
    assert stopped["tool_calls_delta"] == 0
    degraded = ordered[2].payload
    assert degraded["reason"] == DegradeReason.MODEL_OUTPUT_INVALID.value
    assert degraded["planning_suspended"] is True
    assert degraded["model_used"] is False
