"""W08 T03 / AC-05：回读必须走异路径（独立只读 client、事务外、窄接口）。

五条断言逐条落地：

1. `ReadbackPort.read_only is True`（真实 `MysqlReadbackAdapter` + 只读 client）；
2. `readback.client_identity()` 与执行 client 的 `id()` 不同（`ro_client` vs `rw_client`）；
3. 篡改 `step_observed.result` **不改变** `target_readback` 断言的判定；
4. `ReadbackPort.read_target` 只有 `(self, target)`——没有 SQL 字符串 / host / port / DSN；
5. `Verifier.verify` 形参没有 `model_output` / `llm` / `narrative`（模型自报无入口）。

第 3 条用真实回读适配器 + 真实 LAB 数据，`tasks` / `results` / `audit` 用最小桩，
以免把「异路径」这条断言绑在状态机与落表的实现细节上。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from adapters.mysql.lab_repair import TOOL_NAME, LabOrderNoteUpdateTool
from adapters.mysql.readback import MysqlReadbackAdapter
from core.approval.disclosure import build_disclosure
from core.execution.step_records import StepObserved
from core.security.policy import RiskResolution
from core.tasks.models import Task, TaskStatus, utc_now
from core.tasks.state_machine import can_transition, ensure_transition
from core.verification.models import VerificationResult
from core.verification.readback import ReadbackPort, TargetObject
from core.verification.verifier import Verifier

pytestmark = pytest.mark.integration

ORDER_ID = 2001
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
WRITE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"
STARTED_AT = datetime(2026, 11, 12, 5, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 最小桩（只覆盖本文件需要的依赖面）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ApprovalSnapshot:
    disclosure: dict[str, Any]
    action_descriptor: dict[str, Any]
    tool_name: str = TOOL_NAME


class _PlanSource:
    def __init__(self, snapshot: _ApprovalSnapshot) -> None:
        self._snapshot = snapshot

    def get(self, approval_id: str) -> _ApprovalSnapshot:  # noqa: ARG002
        return self._snapshot


class _StepRecords:
    def __init__(self, observed: StepObserved | None) -> None:
        self._observed = observed

    def get_observed(self, tool_call_id: str) -> StepObserved | None:  # noqa: ARG002
        return self._observed


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
        _ = actor_user_id
        self.events.append(event)
        return event


class _MemoryTasks:
    """用真实状态机做迁移，保证「出边合法性」不被桩掩盖。"""

    def __init__(self, task: Task) -> None:
        self._task = task

    def transition(self, task_id: str, target: TaskStatus) -> Task:
        assert task_id == self._task.task_id
        assert can_transition(self._task.status, target), (
            f"桩状态机拒绝非法迁移 {self._task.status} → {target}"
        )
        ensure_transition(self._task.status, target)
        self._task = self._task.model_copy(update={"status": target})
        return self._task


# ---------------------------------------------------------------------------
# 快照与任务构造
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


def _snapshot(note: str) -> _ApprovalSnapshot:
    disclosure = build_disclosure(
        resolution=_resolution(),
        definition=LabOrderNoteUpdateTool().definition,
        captured_at=STARTED_AT,
        policy_version="w06-v1",
    )
    return _ApprovalSnapshot(
        disclosure=disclosure,
        action_descriptor={
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
        },
    )


def _task() -> Task:
    now = utc_now()
    return Task(
        task_id=f"task_{uuid4().hex}",
        run_id=f"run_{uuid4().hex}",
        goal="W08 T03 回读异路径",
        status=TaskStatus.EXECUTING,
        created_at=now,
        updated_at=now,
        tenant_id="tenant_local",
        environment_id="env_mysql_local",
    )


def _observed(task: Task, tool_call_id: str, result: dict[str, Any]) -> StepObserved:
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


def _run_verify(
    settings: Any,
    adapter: MysqlReadbackAdapter,
    *,
    note: str,
    step_result: dict[str, Any],
) -> VerificationResult:
    task = _task()
    tool_call_id = f"tc_w08_readback_{uuid4().hex[:12]}"
    verifier = Verifier(
        plan_source=_PlanSource(_snapshot(note)),
        readback=adapter,
        step_records=_StepRecords(_observed(task, tool_call_id, step_result)),
        results=_MemoryResults(),
        tasks=_MemoryTasks(task),
        audit=_MemoryAudit(),
        settings=settings,
    )
    return verifier.verify(
        task, tool_call_id=tool_call_id, approval_id=f"appr_{uuid4().hex}"
    )


def _outcome(result: VerificationResult, assertion_id: str) -> Any:
    matches = [item for item in result.outcomes if item.assertion_id == assertion_id]
    assert len(matches) == 1, [item.assertion_id for item in result.outcomes]
    return matches[0]


# ---------------------------------------------------------------------------
# 1) 只读 + 2) 异实例
# ---------------------------------------------------------------------------


def test_readback_adapter_is_read_only_and_uses_distinct_client(
    settings, ro_client, rw_client, control_client
) -> None:
    adapter = MysqlReadbackAdapter(ro_client)

    assert adapter.read_only is True
    assert isinstance(adapter, ReadbackPort)
    # 回读 client 与执行 client（写账号）、控制库 client 都是不同实例。
    assert adapter.client_identity() == id(ro_client)
    assert adapter.client_identity() != id(rw_client), "回读不得复用执行连接"
    assert adapter.client_identity() != id(control_client)


def test_readback_adapter_rejects_writable_client(rw_client) -> None:
    """写账号不能冒充回读通道：构造期即拒绝（异路径是构造级约束）。"""
    with pytest.raises(ValueError):
        MysqlReadbackAdapter(rw_client)


# ---------------------------------------------------------------------------
# 3) 篡改 step_observed 不影响 target_readback 判定
# ---------------------------------------------------------------------------


def test_tampered_step_observed_does_not_change_target_readback_verdict(
    settings, ro_client, rw_client
) -> None:
    rows = rw_client.fetch_all(NOTE_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 bootstrap_mysql.py")
    before = rows[0].get("note")
    proof = "w08-readback-proof"
    rw_client.execute(WRITE_SQL, (proof, ORDER_ID))
    try:
        adapter = MysqlReadbackAdapter(ro_client)

        honest = _run_verify(
            settings,
            adapter,
            note=proof,
            step_result={
                "before": None,
                "after": proof,
                "affected_rows": 1,
                "idempotent_noop": False,
            },
        )
        # 执行侧证据被篡改：affected_rows 与 before/after 全是假的。
        tampered = _run_verify(
            settings,
            adapter,
            note=proof,
            step_result={
                "before": "tampered-before",
                "after": "tampered-after",
                "affected_rows": 99,
                "idempotent_noop": False,
            },
        )

        honest_target = _outcome(honest, "target_note_final")
        tampered_target = _outcome(tampered, "target_note_final")

        # target_readback 断言只认独立只读样本：篡改前后判定与实测值完全一致。
        assert honest_target.source == "target_readback"
        assert honest_target.verdict == "PASS"
        assert tampered_target.verdict == "PASS"
        assert tampered_target.observed == honest_target.observed == proof

        # 篡改确实生效于 step_observed 断言（否则上面的「不变」毫无意义）。
        assert honest.verdict == "PASS"
        assert tampered.verdict == "FAIL"
        assert _outcome(tampered, "affected_rows").verdict == "FAIL"
        assert _outcome(tampered, "snapshot_comparable").verdict == "FAIL"
    finally:
        rw_client.execute(WRITE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# 4) 窄接口 5) verify 形参无模型文本
# ---------------------------------------------------------------------------


def test_readback_port_accepts_only_target_object() -> None:
    parameters = list(inspect.signature(ReadbackPort.read_target).parameters)
    assert parameters == ["self", "target"], parameters

    adapter_parameters = list(
        inspect.signature(MysqlReadbackAdapter.read_target).parameters
    )
    assert adapter_parameters == ["self", "target"], adapter_parameters

    source = inspect.getsource(MysqlReadbackAdapter.read_target)
    for forbidden in ("sql:", "host", "port", "dsn", "query", "statement"):
        assert forbidden not in source, f"read_target 不得接受 {forbidden!r} 入参"


def test_verify_signature_has_no_model_text_channel() -> None:
    signature = inspect.signature(Verifier.verify)
    names = set(signature.parameters)

    assert names == {"self", "task", "tool_call_id", "approval_id"}, names
    assert not names & {"model_output", "llm", "narrative", "explanation"}

    # 依赖注入面同样不允许模型通道。
    init_names = set(inspect.signature(Verifier.__init__).parameters)
    assert init_names == {
        "self",
        "plan_source",
        "readback",
        "step_records",
        "results",
        "tasks",
        "audit",
        "settings",
    }, init_names
    assert not init_names & {"model_output", "llm", "narrative"}

    # `read_target` 的目标只能是封闭白名单里的 `TargetObject`。
    assert TargetObject.__dataclass_fields__.keys() == {
        "database",
        "table",
        "key_column",
        "key_value",
        "column",
    }
