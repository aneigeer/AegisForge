"""W08 T05 / AC-13、AC-14、AC-15：恢复控制器的循环、无进展、一次重试与反证。

全部用例不连数据库：`audit` 用记录型桩、`tasks` 用只实现 `get_task` /
`transition` 的桩，判定逻辑与真实装配一致（AC-16 的真实库断言在
`tests/integration/test_w08_recovery_e2e.py`）。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from core.audit.events import AuditEventType
from core.errors import RequestInvalidError
from core.recovery.controller import RecoveryController
from core.recovery.models import RecoveryAction, RecoveryOutcome
from core.tasks.models import TaskStatus
from core.tasks.state_machine import can_transition

TASK_ID = "task_w08_recovery_unit"
RUN_ID = "run_w08_recovery_unit"


# ---------------------------------------------------------------------------
# 桩：记录型审计 + 真实状态机语义的任务桩
# ---------------------------------------------------------------------------


class _RecordingAudit:
    """`AuditService.emit_required` 的记录型替身（不落库）。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any) -> Any:
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [event.event_type for event in self.events]

    def by_type(self, event_type: AuditEventType) -> list[Any]:
        return [event for event in self.events if event.event_type == event_type.value]


class _FakeTasks:
    """只提供 `get_task` / `transition`，并且**真的**按状态机校验迁移。"""

    def __init__(self, status: TaskStatus = TaskStatus.EXECUTING) -> None:
        self.task = SimpleNamespace(task_id=TASK_ID, run_id=RUN_ID, status=status)
        self.transitions: list[tuple[str, TaskStatus]] = []
        self.rejected: list[tuple[TaskStatus, TaskStatus]] = []

    def get_task(self, task_id: str) -> Any:
        assert task_id == TASK_ID
        return self.task

    def transition(self, task_id: str, target: TaskStatus) -> Any:
        assert task_id == TASK_ID
        if not can_transition(self.task.status, target):
            self.rejected.append((self.task.status, target))
            raise AssertionError(f"非法迁移 {self.task.status} -> {target}")
        self.transitions.append((task_id, target))
        self.task.status = target
        return self.task


def _controller(
    *,
    status: TaskStatus = TaskStatus.EXECUTING,
    audit: _RecordingAudit | None = None,
    tasks: _FakeTasks | None = None,
    **kwargs: Any,
) -> tuple[RecoveryController, _RecordingAudit, _FakeTasks]:
    audit = audit or _RecordingAudit()
    tasks = tasks or _FakeTasks(status)
    controller = RecoveryController(
        audit=audit, tasks=tasks, task_id=TASK_ID, run_id=RUN_ID, **kwargs
    )
    return controller, audit, tasks


# ---------------------------------------------------------------------------
# AC-13（前半）：同一 call_key 第 1 次不触发、第 2 次触发 AGENT_LOOP_DETECTED
# ---------------------------------------------------------------------------


def test_loop_boundary_first_call_continues_second_stops() -> None:
    controller, audit, _ = _controller()

    first = controller.observe_call("step:diagnose:orders")
    assert isinstance(first, RecoveryAction)
    assert first.action == "CONTINUE"
    assert first.repeats == 1
    assert audit.by_type(AuditEventType.AGENT_LOOP_DETECTED) == []

    second = controller.observe_call("step:diagnose:orders")
    assert second.action == "STOP_LOOP"
    assert second.repeats == 2
    assert second.explanation.strip()

    events = audit.by_type(AuditEventType.AGENT_LOOP_DETECTED)
    assert len(events) == 1, "第 2 次必须且只能触发一次循环事件"
    payload = dict(events[0].payload)
    assert payload["call_key"] == "step:diagnose:orders"
    assert payload["count"] == 2
    assert payload["threshold"] == 2
    assert len(payload["repeated_call_keys"]) == 2
    assert payload["explanation"].strip()

    # 复用既有事件，不新建同类：事件类型必须已存在于冻结枚举里。
    assert events[0].event_type == AuditEventType.AGENT_LOOP_DETECTED.value


def test_loop_threshold_is_configurable() -> None:
    controller, audit, _ = _controller(max_duplicate_calls=3)

    assert controller.observe_call("call-a").action == "CONTINUE"
    assert controller.observe_call("call-a").action == "CONTINUE"
    assert audit.by_type(AuditEventType.AGENT_LOOP_DETECTED) == []

    third = controller.observe_call("call-a")
    assert third.action == "STOP_LOOP"
    assert third.repeats == 3
    assert len(audit.by_type(AuditEventType.AGENT_LOOP_DETECTED)) == 1


def test_different_call_keys_do_not_trigger_loop() -> None:
    controller, audit, _ = _controller()

    for key in ("call-a", "call-b", "call-c"):
        assert controller.observe_call(key).action == "CONTINUE"
    assert audit.by_type(AuditEventType.AGENT_LOOP_DETECTED) == []


# ---------------------------------------------------------------------------
# AC-13（后半）：连续 2 次相同 digest → AGENT_NO_PROGRESS 且 explanation 非空
# ---------------------------------------------------------------------------


def test_same_evidence_twice_stops_with_no_progress() -> None:
    controller, audit, _ = _controller()

    first = controller.observe_evidence("digest-same")
    assert first.action == "CONTINUE"
    assert audit.by_type(AuditEventType.AGENT_NO_PROGRESS) == []

    second = controller.observe_evidence("digest-same")
    assert second.action == "STOP_NO_PROGRESS"
    assert second.repeats == 2
    assert second.explanation.strip()

    events = audit.by_type(AuditEventType.AGENT_NO_PROGRESS)
    assert len(events) == 1
    payload = dict(events[0].payload)
    assert payload["evidence_digest"] == "digest-same"
    assert payload["repeats"] == 2
    assert payload["threshold"] == 2
    assert payload["explanation"].strip()


def test_changed_evidence_resets_streak_and_threshold_is_configurable() -> None:
    controller, audit, _ = _controller(no_progress_threshold=3)

    assert controller.observe_evidence("digest-1").action == "CONTINUE"
    assert controller.observe_evidence("digest-2").action == "CONTINUE"
    # digest 变化 → 连击重置为 1，不得因为「历史上出现过 2 次」就误判。
    assert controller.observe_evidence("digest-2").action == "CONTINUE"
    assert controller.observe_evidence("digest-2").action == "STOP_NO_PROGRESS"
    assert len(audit.by_type(AuditEventType.AGENT_NO_PROGRESS)) == 1


def test_every_action_has_non_empty_explanation() -> None:
    controller, _, _ = _controller()
    actions = [
        controller.observe_call("k1"),
        controller.observe_call("k1"),
        controller.observe_evidence("d1"),
        controller.observe_evidence("d1"),
        controller.handle_invalid_output(1),
        controller.handle_invalid_output(2),
    ]
    for action in actions:
        assert isinstance(action.explanation, str)
        assert action.explanation.strip(), f"{action.action} 的 explanation 为空"
        assert action.reason.strip()


# ---------------------------------------------------------------------------
# AC-14：非法输出只重试一次
# ---------------------------------------------------------------------------


def test_invalid_output_retries_exactly_once_then_stops() -> None:
    controller, audit, _ = _controller()

    retry = controller.handle_invalid_output(attempt=1)
    assert retry.action == "RETRY"
    assert retry.reason == "MODEL_OUTPUT_INVALID"
    assert retry.retry_count == 1
    assert controller.retry_count == 1
    assert audit.by_type(AuditEventType.AGENT_RECOVERY_STOPPED) == []

    stopped = controller.handle_invalid_output(attempt=2)
    assert stopped.action == "STOP"
    assert stopped.reason == "MODEL_OUTPUT_INVALID"
    assert stopped.retry_count == 1
    assert controller.retry_count == 1, "重试次数不得超过一次"

    events = audit.by_type(AuditEventType.AGENT_RECOVERY_STOPPED)
    assert len(events) == 1
    payload = dict(events[0].payload)
    assert payload["reason"] == "MODEL_OUTPUT_INVALID"
    assert payload["retry_count"] == 1
    assert payload["attempt"] == 2
    assert payload["tool_calls_delta"] == 0
    assert payload["explanation"].strip()


def test_further_invalid_output_never_retries_again() -> None:
    controller, _, _ = _controller()
    assert controller.handle_invalid_output(1).action == "RETRY"

    for attempt in (1, 2, 5):
        action = controller.handle_invalid_output(attempt)
        assert action.action == "STOP", "重试预算已用尽后不得再 RETRY"
        assert action.retry_count == 1


def test_invalid_output_attempt_must_be_positive() -> None:
    controller, _, _ = _controller()
    with pytest.raises(RequestInvalidError):
        controller.handle_invalid_output(0)


# ---------------------------------------------------------------------------
# AC-15：反证关闭且保留历史
# ---------------------------------------------------------------------------


def test_refute_marks_refuted_and_bumps_plan_version() -> None:
    controller, _, _ = _controller()
    hypothesis = controller.register_hypothesis("慢查询来自缺失索引")
    assert hypothesis.status == "OPEN"
    assert hypothesis.plan_version == 0

    refuted = controller.refute(hypothesis.hypothesis_id, reason="EXPLAIN 显示已走索引")

    assert refuted.status == "REFUTED"
    assert refuted.plan_version == 1
    assert controller.plan_version == 1
    assert refuted.reason == "EXPLAIN 显示已走索引"
    assert refuted.hypothesis_id == hypothesis.hypothesis_id
    assert refuted.refuted_at is not None

    # 不物理删除：原假设仍可按 id 查到当前（REFUTED）版本与完整历史。
    current = controller.get_hypothesis(hypothesis.hypothesis_id)
    assert current is refuted
    history = controller.history(hypothesis.hypothesis_id)
    assert len(history) == 2
    assert history[0].status == "OPEN", "旧版本必须原样保留"
    assert history[-1].status == "REFUTED"


def test_two_refutations_bump_plan_version_by_two_and_keep_history() -> None:
    controller, _, _ = _controller()
    first = controller.register_hypothesis("假设一")
    second = controller.register_hypothesis("假设二")

    controller.refute(first.hypothesis_id, reason="反证一")
    controller.refute(second.hypothesis_id, reason="反证二")

    assert controller.plan_version == 2
    assert controller.get_hypothesis(first.hypothesis_id).plan_version == 1
    assert controller.get_hypothesis(second.hypothesis_id).plan_version == 2
    assert len(controller.history(first.hypothesis_id)) == 2
    assert len(controller.history(second.hypothesis_id)) == 2

    # 重复反证同一假设是幂等的：不得重复涨版本。
    again = controller.refute(first.hypothesis_id, reason="再次反证")
    assert again.plan_version == 1
    assert controller.plan_version == 2
    assert len(controller.history(first.hypothesis_id)) == 2


def test_refute_unknown_hypothesis_is_rejected() -> None:
    controller, _, _ = _controller()
    with pytest.raises(RequestInvalidError):
        controller.refute("hyp_missing", reason="无此假设")


def test_refute_requires_reason() -> None:
    controller, _, _ = _controller()
    hypothesis = controller.register_hypothesis("假设")
    with pytest.raises(RequestInvalidError):
        controller.refute(hypothesis.hypothesis_id, reason="   ")
    assert controller.plan_version == 0
    assert controller.get_hypothesis(hypothesis.hypothesis_id).status == "OPEN"


# ---------------------------------------------------------------------------
# AC-16 的单元级对照：收敛判定与「不重放」结构证据
# ---------------------------------------------------------------------------


def test_reconcile_unknown_converges_by_evidence_sufficiency() -> None:
    controller, audit, tasks = _controller()

    sufficient = controller.reconcile_unknown(
        action_digest="digest-abc",
        idempotency_key="idem-abc",
        target_objects=("dg_lab.seed_orders#2001",),
        evidence_sufficient=True,
    )
    assert isinstance(sufficient, RecoveryOutcome)
    assert sufficient.status == "NEEDS_RECONCILIATION"
    assert sufficient.status is TaskStatus.NEEDS_RECONCILIATION
    assert sufficient.applied is True
    assert sufficient.tool_calls_delta == 0
    assert sufficient.replayed is False
    assert sufficient.explanation.strip()
    assert tasks.transitions == [(TASK_ID, TaskStatus.NEEDS_RECONCILIATION)]

    insufficient_controller, audit2, tasks2 = _controller(status=TaskStatus.VERIFYING)
    insufficient = insufficient_controller.reconcile_unknown(
        action_digest="digest-abc",
        idempotency_key="idem-abc",
        target_objects=(),
        evidence_sufficient=False,
    )
    assert insufficient.status == "MANUAL_INTERVENTION"
    assert insufficient.applied is True
    assert tasks2.transitions == [(TASK_ID, TaskStatus.MANUAL_INTERVENTION)]

    for recorder in (audit, audit2):
        events = recorder.by_type(AuditEventType.AGENT_RECOVERY_STOPPED)
        assert len(events) == 1
        payload = dict(events[0].payload)
        assert payload["tool_calls_delta"] == 0
        assert payload["outcome"] == "ESCALATE"
        assert payload["explanation"].strip()


def test_illegal_transition_is_not_faked() -> None:
    """`EXECUTING` 无法直接到 `MANUAL_INTERVENTION`：不得伪造状态变更。"""
    controller, audit, tasks = _controller(status=TaskStatus.EXECUTING)

    outcome = controller.reconcile_unknown(
        action_digest="digest-xyz",
        idempotency_key="idem-xyz",
        target_objects=("dg_lab.seed_orders#2001",),
        evidence_sufficient=False,
    )

    assert outcome.status == "MANUAL_INTERVENTION"
    assert outcome.applied is False
    assert tasks.transitions == []
    assert "NO_LEGAL_TRANSITION" in outcome.explanation
    payload = dict(audit.by_type(AuditEventType.AGENT_RECOVERY_STOPPED)[0].payload)
    assert payload["applied"] is False


def test_reconcile_unknown_rejects_free_text_targets() -> None:
    controller, _, _ = _controller()
    with pytest.raises(RequestInvalidError):
        controller.reconcile_unknown(
            action_digest="digest-abc",
            idempotency_key="idem-abc",
            target_objects="UPDATE seed_orders SET note='x'",  # type: ignore[arg-type]
            evidence_sufficient=True,
        )


def test_controller_has_no_execution_surface() -> None:
    """结构证据：构造参数与实例属性里都不存在 registry / gateway / DSN。"""
    params = set(inspect.signature(RecoveryController.__init__).parameters) - {"self"}
    assert params == {
        "audit",
        "tasks",
        "max_duplicate_calls",
        "no_progress_threshold",
        "max_repair_retries",
        "task_id",
        "run_id",
    }

    controller, _, _ = _controller()
    surface = " ".join(vars(controller))
    for forbidden in ("registry", "gateway", "dsn", "tool"):
        assert forbidden not in surface, f"恢复器不得持有 {forbidden}"

    for method in ("observe_call", "observe_evidence", "refute", "handle_invalid_output"):
        names = set(inspect.signature(getattr(RecoveryController, method)).parameters)
        assert not names & {"tool", "tool_name", "params", "sql", "registry", "gateway"}


# ---------------------------------------------------------------------------
# 阈值与上下文校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"max_duplicate_calls": 1}, "max_duplicate_calls"),
        ({"no_progress_threshold": 1}, "no_progress_threshold"),
        ({"max_repair_retries": 2}, "max_repair_retries"),
        ({"max_repair_retries": -1}, "max_repair_retries"),
        ({"max_duplicate_calls": True}, "max_duplicate_calls"),
    ],
)
def test_invalid_thresholds_are_rejected(kwargs: dict[str, Any], expected: str) -> None:
    with pytest.raises(RequestInvalidError) as excinfo:
        RecoveryController(audit=_RecordingAudit(), tasks=_FakeTasks(), **kwargs)
    assert expected in excinfo.value.details


def test_unbound_controller_skips_audit_but_still_decides() -> None:
    audit = _RecordingAudit()
    controller = RecoveryController(audit=audit, tasks=_FakeTasks())

    assert controller.observe_call("k").action == "CONTINUE"
    action = controller.observe_call("k")
    assert action.action == "STOP_LOOP"
    assert action.explanation.strip()
    assert audit.events == [], "无 task_id 时不得写审计脏行"
    # 未绑定时被跳过的是两条：`AGENT_LOOP_DETECTED` 与 W08 T06 的接管事件
    # `AGENT_RECOVERY_STARTED`（同一次接管只发一条，故不是 3 条）。
    assert controller.skipped_events == 2

    controller.bind(task_id=TASK_ID, run_id=RUN_ID)
    controller.observe_call("k")
    assert len(audit.by_type(AuditEventType.AGENT_LOOP_DETECTED)) == 1


def test_reconcile_requires_task_context() -> None:
    controller = RecoveryController(audit=_RecordingAudit(), tasks=_FakeTasks())
    with pytest.raises(RequestInvalidError):
        controller.reconcile_unknown(
            action_digest="d",
            idempotency_key="i",
            target_objects=(),
            evidence_sufficient=True,
        )


# ---------------------------------------------------------------------------
# W08 T06 / 设计 §5 第 10 行：AGENT_RECOVERY_STARTED（接管事件，同一次只发一条）
# ---------------------------------------------------------------------------


def _started(audit: _RecordingAudit) -> list[Any]:
    return audit.by_type(AuditEventType.AGENT_RECOVERY_STARTED)


def test_loop_takeover_emits_started_once() -> None:
    controller, audit, _ = _controller()

    controller.observe_call("k")  # 第 1 次：CONTINUE，尚未接管
    assert _started(audit) == []

    controller.observe_call("k")  # 第 2 次：STOP_LOOP → 接管开始
    controller.observe_call("k")  # 同一次接管内继续判停：不得重复发

    events = _started(audit)
    assert len(events) == 1
    payload = dict(events[0].payload)
    assert payload["trigger"] == "LOOP_DETECTED"
    assert payload["call_key"] == "k"
    assert payload["plan_version"] == 0
    # 计数是"接管那一刻已观察到的条数"（第 2 次调用即判停），不随后续观测增长。
    assert payload["evidence_count"] == 2
    assert payload["tool_calls_delta"] == 0


def test_no_progress_takeover_emits_started_once() -> None:
    controller, audit, _ = _controller()

    controller.observe_evidence("d1")  # 证据变化
    controller.observe_evidence("d1")  # 连续相同 → 无进展
    controller.observe_evidence("d1")  # 仍无进展：同一次接管

    events = _started(audit)
    assert len(events) == 1
    payload = dict(events[0].payload)
    assert payload["trigger"] == "NO_PROGRESS"
    assert payload["call_key"] is None
    assert payload["evidence_count"] == 2


def test_unknown_outcome_takeover_emits_started_before_stopped() -> None:
    controller, audit, _ = _controller()

    controller.reconcile_unknown(
        action_digest="digest-started",
        idempotency_key="idem-started",
        target_objects=("dg_lab.seed_orders#2001",),
        evidence_sufficient=True,
    )

    events = _started(audit)
    assert len(events) == 1
    assert dict(events[0].payload)["trigger"] == "UNKNOWN_OUTCOME"
    # 顺序：接管在前、结束在后（同一条任务链上可读成"开始 → 结束"）。
    types = audit.types()
    assert types.index(AuditEventType.AGENT_RECOVERY_STARTED.value) < types.index(
        AuditEventType.AGENT_RECOVERY_STOPPED.value
    )


def test_new_takeover_after_progress_emits_again() -> None:
    """进展（CONTINUE）清空接管闸门；下一次判停属于新一次接管。"""
    controller, audit, _ = _controller()

    controller.observe_call("k1")
    controller.observe_call("k1")  # 接管 1
    controller.observe_call("k2")  # 新调用 = 有进展，闸门清零
    controller.observe_call("k2")  # 接管 2

    events = _started(audit)
    assert len(events) == 2
    assert [dict(item.payload)["call_key"] for item in events] == ["k1", "k2"]


def test_invalid_output_does_not_emit_started() -> None:
    """`trigger` 取值封闭在 LOOP_DETECTED / NO_PROGRESS / UNKNOWN_OUTCOME。"""
    controller, audit, _ = _controller()

    controller.handle_invalid_output(1)
    controller.handle_invalid_output(2)

    assert _started(audit) == []
    assert len(audit.by_type(AuditEventType.AGENT_RECOVERY_STOPPED)) == 1
