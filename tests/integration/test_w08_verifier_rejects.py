"""W08 T03 / AC-08：验证判定不得放行（真实控制库）。

覆盖三件事，全部落在**可断言的事实**上：

1. `FAIL`：任务不得 `COMPLETED`，审计必须留 `VERIFICATION_FAILED`，
   证据充分时出边到 `FAILED` 且处置为 `SAFE_TO_COMPENSATE`（`approval_required=True`）；
2. `UNKNOWN`：任务不得 `COMPLETED`，只能落 `NEEDS_RECONCILIATION` 或
   `MANUAL_INTERVENTION`，且 `UNKNOWN` 绝不产出 `SAFE_TO_COMPENSATE`；
3. `VERIFIER_ERROR`：Verifier 内部异常不得让任务停在 `EXECUTING`。

装配口径：`tasks` / `audit` / `results` 用**真实**控制库（TaskService、AuditService、
VerificationResultRepository），`plan_source` / `step_records` / `readback` 用最小桩——
它们是执行侧与审批侧的证据来源，本卡不修改 `core/approval/*` 与执行器。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from adapters.mysql.lab_repair import TOOL_NAME, LabOrderNoteUpdateTool
from core.approval.disclosure import build_disclosure
from core.audit.service import AuditService
from core.execution.step_records import StepObserved
from core.security.policy import RiskResolution
from core.tasks.models import TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry
from core.verification.models import VerificationResult
from core.verification.readback import ReadbackSample, TargetObject
from core.verification.repository import VerificationResultRepository
from core.verification.verifier import Verifier

pytestmark = pytest.mark.integration

ORDER_ID = 2001
TENANT_ID = "tenant_local"
STARTED_AT = datetime(2026, 11, 12, 4, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 证据桩：审批快照 / StepObserved / 只读回读
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ApprovalSnapshot:
    """`ApprovalDetail` 的最小可读形态（`from_disclosure` 只读 `disclosure`）。"""

    disclosure: dict[str, Any]
    action_descriptor: dict[str, Any]
    tool_name: str = TOOL_NAME


class _PlanSource:
    """`plan_source.get(approval_id) -> ApprovalDetail` 的桩。"""

    def __init__(self, snapshot: _ApprovalSnapshot) -> None:
        self._snapshot = snapshot

    def get(self, approval_id: str) -> _ApprovalSnapshot:  # noqa: ARG002 - 只按入参取
        return self._snapshot


class _StepRecords:
    """只追加证据表的桩：`None` 表示「没有 Observed 行」。"""

    def __init__(self, observed: StepObserved | None) -> None:
        self._observed = observed

    def get_observed(self, tool_call_id: str) -> StepObserved | None:  # noqa: ARG002
        return self._observed


class _Readback:
    """只读回读端口的桩；`fail=True` 模拟采集失败（必须收敛为 UNKNOWN）。"""

    def __init__(self, value: Any, *, fail: bool = False) -> None:
        self._value = value
        self._fail = fail

    @property
    def read_only(self) -> bool:
        return True

    def client_identity(self) -> int:
        return id(self)

    def read_target(self, target: TargetObject) -> ReadbackSample:
        if self._fail:
            raise RuntimeError("readback channel unavailable")
        return ReadbackSample(
            value=self._value, sample_id="readback_w08_reject", collected_at=utc_now()
        )


# ---------------------------------------------------------------------------
# 快照构造（真实工具契约 → 真实披露快照断言）
# ---------------------------------------------------------------------------


def _resolution() -> RiskResolution:
    return RiskResolution(
        effective_risk="R2",
        factors=("definition.risk_level=R2",),
        notes=(),
        model_hint=None,
        model_hint_accepted=False,
        operation_known=True,
        side_effect_known=True,
    )


def _descriptor(note: str) -> dict[str, Any]:
    return {
        "parameters": {
            "database": "dg_lab",
            "table": "seed_orders",
            "order_id": ORDER_ID,
            "keys": f"order_id={ORDER_ID}",
            "note": note,
            "expected_note": None,
        },
        "target_objects": [
            {
                "object_type": "row",
                "database": "dg_lab",
                "schema": None,
                "name": "seed_orders",
                "keys": [f"order_id={ORDER_ID}"],
            }
        ],
    }


def _snapshot(note: str) -> _ApprovalSnapshot:
    definition = LabOrderNoteUpdateTool().definition
    disclosure = build_disclosure(
        resolution=_resolution(),
        definition=definition,
        captured_at=STARTED_AT,
        policy_version="w06-v1",
    )
    return _ApprovalSnapshot(
        disclosure=disclosure, action_descriptor=_descriptor(note)
    )


def _observed(
    task: Any, tool_call_id: str, result: dict[str, Any]
) -> StepObserved:
    return StepObserved(
        observed_id=f"obs_{uuid4().hex}",
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=tool_call_id,
        tool_name=TOOL_NAME,
        outcome="success",
        started_at=STARTED_AT,
        result=result,
    )


# ---------------------------------------------------------------------------
# 真实控制库装配
# ---------------------------------------------------------------------------


@pytest.fixture
def w08_task_stack(settings, control_client):
    """真实 TaskService / AuditService / VerificationResultRepository。"""
    repository = TaskRepository(control_client)
    service = TaskService(repository, ToolRegistry())
    audit = AuditService(repository)
    results = VerificationResultRepository(control_client)
    task = service.create_task("W08 T03 状态推进与判定")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)
    return {
        "settings": settings,
        "control_client": control_client,
        "service": service,
        "audit": audit,
        "results": results,
        "task": task,
    }


def _verifier(
    stack: dict[str, Any],
    *,
    snapshot: _ApprovalSnapshot,
    observed: StepObserved | None,
    readback: _Readback,
) -> Verifier:
    return Verifier(
        plan_source=_PlanSource(snapshot),
        readback=readback,
        step_records=_StepRecords(observed),
        results=stack["results"],
        tasks=stack["service"],
        audit=stack["audit"],
        settings=stack["settings"],
    )


def _events(stack: dict[str, Any], task_id: str) -> list[Any]:
    events, _ = stack["audit"].query(
        tenant_id=TENANT_ID, task_id=task_id, limit=200
    )
    return events


def _payload(event: Any) -> dict[str, Any]:
    return dict(getattr(event, "payload", {}) or {})


def _by_type(events: list[Any], event_type: str) -> list[Any]:
    return [
        event
        for event in events
        if getattr(event.event_type, "value", event.event_type) == event_type
    ]


# ---------------------------------------------------------------------------
# 1) FAIL
# ---------------------------------------------------------------------------


def test_fail_verdict_never_completes_and_records_verification_failed(
    w08_task_stack,
) -> None:
    """断言全 FAIL：不得 COMPLETED；证据充分 → FAILED + SAFE_TO_COMPENSATE。"""
    stack = w08_task_stack
    task = stack["task"]
    tool_call_id = f"tc_w08_fail_{uuid4().hex[:12]}"
    observed = _observed(
        task,
        tool_call_id,
        {
            "before": None,
            "after": "w08-wrong-value",
            "affected_rows": 0,
            "idempotent_noop": False,
        },
    )
    verifier = _verifier(
        stack,
        snapshot=_snapshot("w08-expected-value"),
        observed=observed,
        readback=_Readback("w08-wrong-value"),
    )

    result = verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )

    assert isinstance(result, VerificationResult)
    assert result.verdict == "FAIL"
    assert result.claimed_success is False
    assert result.disposition == "SAFE_TO_COMPENSATE"
    assert result.approval_required is True
    assert {item.verdict for item in result.outcomes} == {"FAIL"}

    current = stack["service"].get_task(task.task_id)
    assert current.status is not TaskStatus.COMPLETED
    assert current.status is TaskStatus.FAILED

    # A3b：EXECUTING → VERIFYING 这一跳必须真的发生（否则验证永不触发）。
    state_changes = [
        _payload(event)
        for event in _by_type(_events(stack, task.task_id), "TASK_STATE_CHANGED")
    ]
    assert any(
        item.get("from") == "EXECUTING" and item.get("to") == "VERIFYING"
        for item in state_changes
    ), state_changes

    # 事件顺序：PLANNED → RESULT → FAILED
    types = [
        getattr(event.event_type, "value", event.event_type)
        for event in _events(stack, task.task_id)
    ]
    planned = types.index("VERIFICATION_PLANNED")
    recorded = types.index("VERIFICATION_RESULT")
    failed = types.index("VERIFICATION_FAILED")
    assert planned < recorded < failed, types

    failure = _payload(_by_type(_events(stack, task.task_id), "VERIFICATION_FAILED")[0])
    assert failure["verdict"] == "FAIL"
    assert failure["disposition"] == "SAFE_TO_COMPENSATE"
    assert set(failure["failed_assertions"]) == {
        item.assertion_id for item in result.outcomes if item.verdict == "FAIL"
    }
    assert failure["unknown_assertions"] == []

    rows = stack["results"].list_by_tool_call(TENANT_ID, tool_call_id)
    assert len(rows) == 1
    assert rows[0].result_id == result.result_id
    assert rows[0].verdict == "FAIL"
    assert rows[0].claimed_success is False


# ---------------------------------------------------------------------------
# 2) UNKNOWN
# ---------------------------------------------------------------------------


def test_unknown_verdict_goes_to_reconciliation_never_completes(w08_task_stack) -> None:
    """无 Observed 行 + 回读失败：全部 UNKNOWN，落核对态且禁止自动补偿。"""
    stack = w08_task_stack
    task = stack["task"]
    tool_call_id = f"tc_w08_unknown_{uuid4().hex[:12]}"
    verifier = _verifier(
        stack,
        snapshot=_snapshot("w08-expected-value"),
        observed=None,
        readback=_Readback(None, fail=True),
    )

    result = verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )

    assert result.verdict == "UNKNOWN"
    assert result.claimed_success is False
    assert result.disposition == "MANUAL_INTERVENTION"
    assert result.approval_required is False
    assert all(item.verdict == "UNKNOWN" for item in result.outcomes)
    assert all(item.reason for item in result.outcomes), "UNKNOWN 必须带稳定原因码"

    current = stack["service"].get_task(task.task_id)
    assert current.status is not TaskStatus.COMPLETED
    assert current.status in {
        TaskStatus.NEEDS_RECONCILIATION,
        TaskStatus.MANUAL_INTERVENTION,
    }

    failures = [
        _payload(event)
        for event in _by_type(_events(stack, task.task_id), "VERIFICATION_FAILED")
    ]
    assert failures, "verdict != PASS 必须额外发 VERIFICATION_FAILED"
    assert failures[0]["verdict"] == "UNKNOWN"
    assert failures[0]["disposition"] == "MANUAL_INTERVENTION"
    assert failures[0]["failed_assertions"] == []
    assert failures[0]["unknown_assertions"], "UNKNOWN 必须列出具体断言"

    rows = stack["results"].list_by_tool_call(TENANT_ID, tool_call_id)
    assert len(rows) == 1
    assert rows[0].disposition == "MANUAL_INTERVENTION"
    assert rows[0].approval_required is False


# ---------------------------------------------------------------------------
# 3) VERIFIER_ERROR
# ---------------------------------------------------------------------------


def test_verifier_error_never_leaves_task_executing(w08_task_stack) -> None:
    """计划构造失败（披露缺验证计划）→ 不得停留 EXECUTING，必须留 VERIFIER_ERROR。"""
    stack = w08_task_stack
    task = stack["task"]
    tool_call_id = f"tc_w08_error_{uuid4().hex[:12]}"
    broken = _ApprovalSnapshot(
        disclosure={"verification_plan": {"assertions": []}},
        action_descriptor=_descriptor("w08-expected-value"),
    )
    verifier = _verifier(
        stack,
        snapshot=broken,
        observed=None,
        readback=_Readback("w08-expected-value"),
    )

    result = verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )

    assert result.verdict == "UNKNOWN"
    assert result.claimed_success is False

    current = stack["service"].get_task(task.task_id)
    assert current.status is not TaskStatus.EXECUTING, "Verifier 异常不得停在 EXECUTING"
    assert current.status is not TaskStatus.COMPLETED
    assert current.status in {
        TaskStatus.NEEDS_RECONCILIATION,
        TaskStatus.MANUAL_INTERVENTION,
    }

    failures = [
        _payload(event)
        for event in _by_type(_events(stack, task.task_id), "VERIFICATION_FAILED")
    ]
    assert failures, "VERIFIER_ERROR 必须留 VERIFICATION_FAILED"
    assert failures[0]["reason"] == "VERIFIER_ERROR"
    assert failures[0]["verdict"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# 对照：PASS 才能 COMPLETED（避免「永远不放行」被误当成通过）
# ---------------------------------------------------------------------------


def test_pass_verdict_completes_task(w08_task_stack) -> None:
    """三路证据一致 → PASS → COMPLETED（否则上面的「不 COMPLETED」断言无意义）。"""
    stack = w08_task_stack
    task = stack["task"]
    tool_call_id = f"tc_w08_pass_{uuid4().hex[:12]}"
    note = "w08-verified-ok"
    observed = _observed(
        task,
        tool_call_id,
        {
            "before": None,
            "after": note,
            "affected_rows": 1,
            "idempotent_noop": False,
        },
    )
    verifier = _verifier(
        stack,
        snapshot=_snapshot(note),
        observed=observed,
        readback=_Readback(note),
    )

    result = verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )

    assert result.verdict == "PASS"
    assert result.claimed_success is True
    assert result.disposition is None
    assert result.approval_required is False
    assert stack["service"].get_task(task.task_id).status is TaskStatus.COMPLETED
    assert not _by_type(_events(stack, task.task_id), "VERIFICATION_FAILED")


def test_already_verifying_task_is_not_advanced_twice(w08_task_stack) -> None:
    """A3b 幂等：已是 VERIFYING 时不得再涨一次 `state_version`。"""
    stack = w08_task_stack
    task = stack["service"].transition(stack["task"].task_id, TaskStatus.VERIFYING)
    version_before = task.state_version
    tool_call_id = f"tc_w08_idem_{uuid4().hex[:12]}"
    note = "w08-verified-ok"
    verifier = _verifier(
        stack,
        snapshot=_snapshot(note),
        observed=_observed(
            task,
            tool_call_id,
            {
                "before": None,
                "after": note,
                "affected_rows": 1,
                "idempotent_noop": False,
            },
        ),
        readback=_Readback(note),
    )

    result = verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )

    current = stack["service"].get_task(task.task_id)
    assert result.verdict == "PASS"
    assert current.status is TaskStatus.COMPLETED
    # 只应发生 VERIFYING → COMPLETED 一次迁移；重复推进 VERIFYING 会多涨一版。
    assert current.state_version == version_before + 1

    verifying_hops = [
        _payload(event)
        for event in _by_type(_events(stack, task.task_id), "TASK_STATE_CHANGED")
        if _payload(event).get("to") == "VERIFYING"
    ]
    assert len(verifying_hops) == 1, verifying_hops
