"""W08 T06 / AC-21：验证确定性——同一 `tool_call_id` 连跑 10 轮，`verdict` 集合大小 ==1。

不确定性来源必须被排除在判定之外：时间戳（`collected_at`）、回读样本 id、
断言遍历顺序、摘要计算。这些都在结果里出现，但**不得**改变 `verdict`。

本文件全部离线（假 plan/readback/step_records，不连库），只锁"同一输入 → 同一判定"：
10 轮里 `verdict`、`plan_digest`、逐条断言的 `(assertion_id, verdict, observed)`
三元组必须完全一致；`collected_at` 允许变化（它是事实采集时间，不是判定输入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from adapters.mysql.lab_repair import TOOL_NAME, LabOrderNoteUpdateTool
from core.approval.disclosure import build_disclosure
from core.execution.step_records import StepObserved
from core.security.policy import RiskResolution
from core.tasks.models import Task, TaskStatus, utc_now
from core.verification.models import VerificationResult
from core.verification.readback import ReadbackSample, TargetObject
from core.verification.verifier import Verifier

TENANT = "tenant_local"
ORDER_ID = 2001
NOTE = "w08-deterministic-note"
TOOL_CALL_ID = "tc_w08_determinism_0001"
STARTED_AT = datetime(2026, 11, 12, 5, 0, 0, tzinfo=timezone.utc)
ROUNDS = 10


# ---------------------------------------------------------------------------
# 假依赖：同一输入永远给同一观测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    disclosure: dict[str, Any]
    action_descriptor: dict[str, Any]
    tool_name: str = TOOL_NAME


class _PlanSource:
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def get(self, approval_id: str) -> _Snapshot:  # noqa: ARG002
        self.calls += 1
        return self._snapshot


class _StepRecords:
    def __init__(self, observed: StepObserved | None) -> None:
        self._observed = observed

    def get_observed(self, tool_call_id: str) -> StepObserved | None:  # noqa: ARG002
        return self._observed


class _FixedReadback:
    """固定样本：值恒定，`sample_id` 恒定（否则等于把随机 id 掺进判定）。"""

    def __init__(self, value: str) -> None:
        self._value = value
        self.calls = 0

    def read_target(self, target: TargetObject) -> ReadbackSample:  # noqa: ARG002
        self.calls += 1
        return ReadbackSample(
            value=self._value, sample_id="readback_fixed", collected_at=STARTED_AT
        )


class _MemoryResults:
    def __init__(self) -> None:
        self.rows: list[VerificationResult] = []

    def insert(self, result: VerificationResult) -> str:
        self.rows.append(result)
        return result.result_id


class _MemoryAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event


class _MemoryTasks:
    """迁移桩：只改状态并涨版本，不做状态机校验（迁移合法性由 T03 用例覆盖）。"""

    def __init__(self, task: Task) -> None:
        self.task = task
        self.transitions: list[TaskStatus] = []

    def transition(self, task_id: str, target: TaskStatus) -> Task:
        assert task_id == self.task.task_id
        self.transitions.append(target)
        self.task = self.task.model_copy(
            update={"status": target, "state_version": self.task.state_version + 1}
        )
        return self.task


# ---------------------------------------------------------------------------
# 输入构造（与真实披露快照同源）
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


def _snapshot() -> _Snapshot:
    return _Snapshot(
        disclosure=build_disclosure(
            resolution=_resolution(),
            definition=LabOrderNoteUpdateTool().definition,
            captured_at=STARTED_AT,
            policy_version="w06-v1",
        ),
        action_descriptor={
            "parameters": {
                "database": "dg_lab",
                "table": "seed_orders",
                "order_id": ORDER_ID,
                "keys": f"order_id={ORDER_ID}",
                "note": NOTE,
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
        },
    )


def _task() -> Task:
    now = utc_now()
    return Task(
        task_id="task_w08_determinism",
        run_id="run_w08_determinism",
        goal="W08 T06 验证确定性",
        status=TaskStatus.EXECUTING,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id="env_mysql_local",
    )


def _observed() -> StepObserved:
    return StepObserved(
        observed_id="obs_w08_determinism",
        tenant_id=TENANT,
        task_id="task_w08_determinism",
        run_id="run_w08_determinism",
        tool_call_id=TOOL_CALL_ID,
        tool_name=TOOL_NAME,
        outcome="success",
        started_at=STARTED_AT,
        finished_at=STARTED_AT,
        result={
            "before": None,
            "after": NOTE,
            "affected_rows": 1,
            "idempotent_noop": False,
        },
    )


def _run_rounds(rounds: int = ROUNDS) -> tuple[list[VerificationResult], _PlanSource, _FixedReadback]:
    """同一 `tool_call_id` 连续验证 `rounds` 轮，返回每轮结果。"""
    tasks = _MemoryTasks(_task())
    plan_source = _PlanSource(_snapshot())
    readback = _FixedReadback(NOTE)
    verifier = Verifier(
        plan_source=plan_source,
        readback=readback,
        step_records=_StepRecords(_observed()),
        results=_MemoryResults(),
        tasks=tasks,
        audit=_MemoryAudit(),
        settings=None,
    )
    results: list[VerificationResult] = []
    for _ in range(rounds):
        results.append(
            verifier.verify(
                tasks.task, tool_call_id=TOOL_CALL_ID, approval_id="apr_w08_determinism"
            )
        )
    return results, plan_source, readback


# ---------------------------------------------------------------------------
# AC-21
# ---------------------------------------------------------------------------


def test_verdict_is_stable_across_ten_rounds() -> None:
    results, _, _ = _run_rounds()

    verdicts = {item.verdict for item in results}
    assert len(verdicts) == 1, verdicts
    assert verdicts == {"PASS"}
    assert len(results) == ROUNDS


def test_plan_digest_is_stable_across_ten_rounds() -> None:
    results, _, _ = _run_rounds()

    digests = {item.plan_digest for item in results}
    assert len(digests) == 1, digests
    assert results[0].plan_digest


def test_assertion_triplets_are_stable_across_ten_rounds() -> None:
    """逐条断言 `(assertion_id, verdict, observed)` 顺序与取值都不得漂移。"""
    results, _, _ = _run_rounds()

    triplets = {
        tuple(
            (item.assertion_id, item.verdict, item.observed) for item in result.outcomes
        )
        for result in results
    }
    assert len(triplets) == 1
    triplet = next(iter(triplets))
    # 断言覆盖两种异路径来源，否则"确定性"只是对单来源重复。
    sources = {item.source for item in results[0].outcomes}
    assert sources == {"step_observed", "target_readback"}


def test_disposition_is_stable_across_ten_rounds() -> None:
    results, _, _ = _run_rounds()

    assert len({(item.disposition, item.approval_required) for item in results}) == 1


def test_collected_at_may_vary_but_does_not_feed_the_verdict() -> None:
    """采集时间是事实、不是输入：允许每秒不同，但判定集合仍为 1。"""
    results, _, _ = _run_rounds()

    assert len({item.collected_at for item in results}) >= 1
    assert len({item.verdict for item in results}) == 1


def test_same_tool_call_reads_plan_and_readback_every_round() -> None:
    """确定性来自"同样的输入"，不是"缓存了第一次结果"。"""
    results, plan_source, readback = _run_rounds()

    assert plan_source.calls == ROUNDS
    assert readback.calls == ROUNDS
    assert len(results) == ROUNDS
