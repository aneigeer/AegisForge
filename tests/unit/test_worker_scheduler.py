"""W08 T06 / AC-17：核对进生产调度——`sweep()` 恰好 1 次、失败不中断、`reconciled` 累加。

全部用例**离线**：`sweep` 用可编排的替身（记录调用次数/入参/抛错），候选执行用
现有假 Worker，不连任何数据库。这里保护的是**调度语义**，不重复核对器本身的正确性
（那由 `tests/unit/test_reconciliation.py` 与 W06 冻结用例覆盖）。

关键口径：

1. `run_once()` 每轮**恰好**调用一次 `sweep()`，参数固定为
   `(tenant_id=stack.tenant_id, older_than_s=settings.reconcile_older_than_s)`；
2. `sweep()` 抛错 → 该轮 `clean=False`，但**候选照常执行**（循环不中断），
   并写一条 `RECONCILIATION_FAILED`（`task_id` 为调度伪任务，payload 只含
   可检索事实、不含错误原文）；
3. `reconciled` 只统计**真的推进了状态**的核对结论，且能跨轮累加；
4. 没有注入 `reconciler` 的历史栈保持原行为（`reconciled == 0`，不假装核对过）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.audit.events import AuditEventType
from core.errors import DatabaseQueryError
from core.execution.reconciler import ReconcileResult, SweepReport
from core.tasks.models import Task, TaskStatus, utc_now
from core.tools.contracts import ToolStatus
from workers.discovery import Candidate
from workers.runner import (
    REASON_RECONCILE_INTERVAL_NOT_DUE,
    REASON_RECONCILIATION_SWEEP_FAILED,
    SCHEDULER_TASK_ID,
    RunReport,
    WorkerRunner,
)

TENANT = "tenant_local"


# ---------------------------------------------------------------------------
# 假依赖（只覆盖 runner 实际触达的接口面）
# ---------------------------------------------------------------------------


def _task(task_id: str, *, status: TaskStatus = TaskStatus.WAITING_APPROVAL) -> Task:
    now = utc_now()
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        goal=f"goal {task_id}",
        status=status,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id="env_mysql_local",
    )


class _Repository:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = list(tasks)

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        for task in self._tasks:
            if task.task_id == task_id:
                return task
        raise AssertionError(f"未知任务 {task_id}")

    def replace(self, task: Task) -> None:
        self._tasks = [task if item.task_id == task.task_id else item for item in self._tasks]


class _TaskService:
    def __init__(self, repository: _Repository) -> None:
        self._repository = repository
        self.calls: list[tuple[str, TaskStatus]] = []

    def transition(self, task_id: str, target: TaskStatus, **kwargs: Any) -> Task:
        self.calls.append((task_id, target))
        task = self._repository.get_task(task_id)
        if task.status is target:
            return task
        updated = task.model_copy(
            update={"status": target, "state_version": task.state_version + 1}
        )
        self._repository.replace(updated)
        return updated


class _Worker:
    def __init__(self, *, status: ToolStatus = ToolStatus.SUCCESS) -> None:
        self.worker_id = "worker_scheduler_test"
        self.status = status
        self.submit_calls: list[dict[str, Any]] = []
        self.release_calls = 0

    def acquire_lease(self, task_id: str, environment_id: str, ttl_s: float) -> Any:
        return SimpleNamespace(lease_id="lease_test", task_id=task_id)

    def submit(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        step_id: str | None,
        state_version: int,
        tool_version: str | None = None,
        ttl_s: float = 60,
        approval_id: str | None = None,
    ) -> Any:
        self.submit_calls.append({"task_id": step_id, "approval_id": approval_id})
        return SimpleNamespace(status=self.status)

    def release(self) -> Any:
        self.release_calls += 1
        return SimpleNamespace(lease_id="lease_test")


class _Heartbeat:
    def __init__(self, *_: Any, **__: Any) -> None:
        self.lost = False

    def start(self) -> None:
        return None

    def stop(self, *, timeout_s: float = 5.0) -> None:
        return None


class _Audit:
    """`AuditService` 的最小替身。

    W09 T06 / KD-3 起 `try_emit` 多一个**可选** `tenant_id`（服务端组合根事实）：
    替身同步接住并留痕，供新增用例断言"调度事件带了租户列"。既有 22 个用例
    既不读 `tenants`、也不传该参数，行为逐字不变。
    """

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.tenants: list[str | None] = []

    def try_emit(
        self,
        event: Any,
        *,
        actor_user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        self.events.append(event)
        self.tenants.append(tenant_id)
        return True

    def types(self) -> list[str]:
        return [event.event_type for event in self.events]


@dataclass
class _Result:
    """`ReconcileResult` 的最小替身：runner 只读 `transitioned` 与结论三元组。"""

    transitioned: bool
    task_id: str | None = None
    tool_call_id: str | None = None
    conclusion: str | None = None


class _Reconciler:
    def __init__(
        self,
        *,
        results: list[_Result] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._results = results if results is not None else []
        self._error = error

    def sweep(
        self, *, tenant_id: str | None = None, older_than_s: float = 60.0
    ) -> list[_Result]:
        self.calls.append({"tenant_id": tenant_id, "older_than_s": older_than_s})
        if self._error is not None:
            raise self._error
        return self._results


class _Stack:
    """`RunnerStackPort` 的最小替身；`reconciler` 可选（历史栈没有该属性）。"""

    def __init__(
        self,
        *,
        tasks: list[Task],
        reconciler: _Reconciler | None = None,
        with_reconciler: bool = True,
        worker: _Worker | None = None,
        **settings: Any,
    ) -> None:
        values: dict[str, Any] = {
            "worker_poll_interval_s": 0.001,
            "worker_shutdown_grace_s": 5.0,
            "worker_lease_ttl_s": 30.0,
            "worker_heartbeat_interval_s": 10.0,
            "gateway_envelope_ttl_s": 60.0,
            "worker_actor_user_id": "system:worker",
            "worker_max_concurrency": 1,
            "reconcile_older_than_s": 90.0,
        }
        values.update(settings)
        self.settings = SimpleNamespace(**values)
        self.tenant_id = TENANT
        self.worker_id = "worker_scheduler_test"
        self.repository = _Repository(tasks)
        self.task_service = _TaskService(self.repository)
        self.audit = _Audit()
        self._worker = worker or _Worker()
        if with_reconciler or reconciler is not None:
            self.reconciler = reconciler or _Reconciler()

    def worker_for(self, actor_user_id: str) -> _Worker:
        return self._worker


def _approval(approval_id: str, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(approval_id=approval_id, task_id=task_id, requested_by="user_operator")


def _candidate(task: Task) -> Candidate:
    return Candidate(
        task=task,
        approval=_approval(f"apr_{task.task_id}", task.task_id),  # type: ignore[arg-type]
        tool_name="mysql_lab_update_order_note",
        params={"order_id": 2001},
        step_id=f"step_{task.task_id}",
    )


def _runner(stack: _Stack, candidates: list[Candidate]) -> WorkerRunner:
    return WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: list(candidates),
        heartbeat_factory=_Heartbeat,
    )


# ---------------------------------------------------------------------------
# 1) sweep 恰好 1 次
# ---------------------------------------------------------------------------


def test_run_once_calls_sweep_exactly_once() -> None:
    reconciler = _Reconciler(results=[])
    stack = _Stack(tasks=[], reconciler=reconciler)
    runner = _runner(stack, [])

    report = runner.run_once()

    assert len(reconciler.calls) == 1, reconciler.calls
    assert reconciler.calls[0] == {"tenant_id": TENANT, "older_than_s": 90.0}
    assert report.reconciled == 0
    assert report.clean is True


def test_sweep_runs_once_even_with_many_candidates() -> None:
    """核对是**每周期一次**，不随候选数量放大（否则每轮 N 次全表扫）。"""
    reconciler = _Reconciler()
    tasks = [_task(f"task_multi_{index}") for index in range(3)]
    stack = _Stack(tasks=tasks, reconciler=reconciler)
    runner = _runner(stack, [_candidate(task) for task in tasks])

    report = runner.run_once()

    assert report.executed == 3
    assert len(reconciler.calls) == 1


# ---------------------------------------------------------------------------
# 2) reconciled 只统计真的推进了状态的结论
# ---------------------------------------------------------------------------


def test_reconciled_counts_only_transitioned_results() -> None:
    reconciler = _Reconciler(
        results=[
            _Result(transitioned=True),
            _Result(transitioned=True),
            # 已收敛/证据不足且未迁移：不得计入 reconciled。
            _Result(transitioned=False),
        ]
    )
    stack = _Stack(tasks=[], reconciler=reconciler)

    report = _runner(stack, []).run_once()

    assert report.reconciled == 2
    # 核对收敛不代表"有副作用发生"，也不算不干净。
    assert report.clean is True


# ---------------------------------------------------------------------------
# 2b) RECONCILIATION_SWEPT（设计 §5 第 8 行）
# ---------------------------------------------------------------------------


def _swept_events(stack: _Stack) -> list[Any]:
    return [
        event
        for event in stack.audit.events
        if event.event_type == AuditEventType.RECONCILIATION_SWEPT.value
    ]


def test_sweep_success_emits_reconciliation_swept() -> None:
    reconciler = _Reconciler(
        results=[
            _Result(
                transitioned=True,
                task_id="task_a",
                tool_call_id="tc_a",
                conclusion="NEEDS_RECONCILIATION",
            ),
            _Result(
                transitioned=False,
                task_id="task_b",
                tool_call_id="tc_b",
                conclusion="NEEDS_RECONCILIATION",
            ),
        ]
    )
    stack = _Stack(tasks=[], reconciler=reconciler)

    _runner(stack, []).run_once()

    events = _swept_events(stack)
    assert len(events) == 1, "每轮恰好一条 RECONCILIATION_SWEPT"
    payload = events[0].payload
    assert payload["scanned"] == 2
    assert payload["converged"] == 1
    assert payload["conclusions"] == [
        {"task_id": "task_a", "tool_call_id": "tc_a", "conclusion": "NEEDS_RECONCILIATION"},
        {"task_id": "task_b", "tool_call_id": "tc_b", "conclusion": "NEEDS_RECONCILIATION"},
    ]
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0.0
    assert payload["tool_calls_delta"] == 0


def test_sweep_success_emits_even_when_result_is_empty() -> None:
    """空结果也必须发：这是"本轮确实扫过、无待收敛"的唯一证据。"""
    stack = _Stack(tasks=[], reconciler=_Reconciler(results=[]))

    _runner(stack, []).run_once()

    events = _swept_events(stack)
    assert len(events) == 1
    assert events[0].payload["scanned"] == 0
    assert events[0].payload["converged"] == 0
    assert events[0].payload["conclusions"] == []


def test_sweep_failure_does_not_emit_swept() -> None:
    """成功与失败是互斥事件：抛错轮只发 RECONCILIATION_FAILED。"""
    stack = _Stack(
        tasks=[],
        reconciler=_Reconciler(error=DatabaseQueryError("x", details={"reason": "T"})),
    )

    _runner(stack, []).run_once()

    assert _swept_events(stack) == []
    assert stack.audit.types() == [AuditEventType.RECONCILIATION_FAILED.value]


def test_swept_and_failed_share_scheduler_task_id() -> None:
    """两条调度事件必须挂在同一伪任务上（否则调度事实被拆成两处）。"""
    stack = _Stack(tasks=[], reconciler=_Reconciler(results=[]))

    _runner(stack, []).run_once()

    assert {event.task_id for event in stack.audit.events} == {SCHEDULER_TASK_ID}


def test_swept_events_are_per_cycle() -> None:
    stack = _Stack(tasks=[], reconciler=_Reconciler(results=[]))
    runner = WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: [],
        heartbeat_factory=_Heartbeat,
        poll_interval_s=0.0,
        max_cycles=3,
    )

    runner.run_forever()

    assert len(_swept_events(stack)) == 3


# ---------------------------------------------------------------------------
# 3) sweep 抛错：不中断、clean=False、写 RECONCILIATION_FAILED
# ---------------------------------------------------------------------------

def test_sweep_failure_does_not_stop_candidate_execution() -> None:
    error = DatabaseQueryError("控制库暂时不可用", details={"reason": "TEST"})
    stack = _Stack(
        tasks=[_task("task_after_failed_sweep")],
        reconciler=_Reconciler(error=error),
    )
    task = stack.repository.get_task("task_after_failed_sweep")
    runner = _runner(stack, [_candidate(task)])

    report = runner.run_once()

    assert report.executed == 1, "核对失败绝不能让本轮候选停摆"
    assert report.clean is False, "存在未收敛事实时不得宣称干净"
    assert report.failed == 1
    assert report.reconciled == 0


def test_sweep_failure_emits_reconciliation_failed_event() -> None:
    error = DatabaseQueryError("控制库暂时不可用", details={"reason": "TEST"})
    stack = _Stack(tasks=[], reconciler=_Reconciler(error=error))
    runner = _runner(stack, [])

    runner.run_once()

    assert stack.audit.types() == [AuditEventType.RECONCILIATION_FAILED.value]
    event = stack.audit.events[0]
    # 调度层事件挂在固定伪任务上：task_events.task_id 为 NOT NULL，不能写空行。
    assert event.task_id == SCHEDULER_TASK_ID
    payload = event.payload
    assert payload["code"] == "DB_QUERY_ERROR"
    assert payload["error_code"] == "DB_QUERY_ERROR"
    assert payload["reason"] == REASON_RECONCILIATION_SWEEP_FAILED
    assert payload["error_type"] == "DatabaseQueryError"
    assert payload["consecutive_failures"] == 1
    assert payload["tenant_id"] == TENANT
    assert payload["replay_attempted"] is False
    # 只落摘要，不落错误原文（原文可能带主机/DSN 片段）。
    assert len(payload["message_digest"]) == 64
    assert "控制库暂时不可用" not in str(payload)


def test_consecutive_sweep_failures_are_counted() -> None:
    error = DatabaseQueryError("控制库不可用", details={"reason": "TEST"})
    stack = _Stack(tasks=[], reconciler=_Reconciler(error=error))
    runner = _runner(stack, [])

    runner.run_once()
    runner.run_once()
    runner.run_once()

    failures = [event.payload["consecutive_failures"] for event in stack.audit.events]
    assert failures == [1, 2, 3]


def test_successful_sweep_resets_consecutive_failures() -> None:
    error = DatabaseQueryError("控制库不可用", details={"reason": "TEST"})
    reconciler = _Reconciler(error=error)
    stack = _Stack(tasks=[], reconciler=reconciler)
    runner = _runner(stack, [])

    runner.run_once()
    reconciler._error = None  # 控制库恢复
    runner.run_once()
    reconciler._error = error  # 再次故障：计数必须从 1 重新开始
    runner.run_once()

    failures = [
        event.payload["consecutive_failures"]
        for event in stack.audit.events
        if event.event_type == "RECONCILIATION_FAILED"
    ]
    assert failures == [1, 1]


# ---------------------------------------------------------------------------
# 4) 报告累加与对外字段
# ---------------------------------------------------------------------------


def test_merged_accumulates_reconciled() -> None:
    left = RunReport(cycles=1, executed=2, reconciled=3)
    right = RunReport(cycles=1, reconciled=4)

    total = left.merged(right)

    assert total.cycles == 2
    assert total.executed == 2
    assert total.reconciled == 7
    assert left.reconciled == 3, "merged 不得就地修改"


def test_public_dict_exposes_reconciled() -> None:
    payload = RunReport(reconciled=5).to_public_dict()

    assert payload["reconciled"] == 5
    assert payload["clean"] is True


def test_clean_semantics_ignore_reconciled() -> None:
    assert RunReport(reconciled=9).clean is True
    for field_name in ("denied", "skipped", "failed", "unknown_outcome"):
        assert getattr(RunReport(**{field_name: 1}), "clean") is False


def test_run_forever_accumulates_reconciled() -> None:
    reconciler = _Reconciler(results=[_Result(transitioned=True)])
    stack = _Stack(tasks=[], reconciler=reconciler)

    runner = WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: [],
        heartbeat_factory=_Heartbeat,
        poll_interval_s=0.0,
        max_cycles=3,
    )
    report = runner.run_forever()

    assert report.cycles == 3
    assert report.reconciled == 3
    assert len(reconciler.calls) == 3, "每轮恰好一次 sweep"


# ---------------------------------------------------------------------------
# 5) 历史栈（没有 reconciler 属性）保持原行为
# ---------------------------------------------------------------------------


def test_stack_without_reconciler_keeps_original_behaviour() -> None:
    stack = _Stack(tasks=[_task("task_legacy")], with_reconciler=False)
    assert not hasattr(stack, "reconciler"), "本用例必须模拟历史栈"

    task = stack.repository.get_task("task_legacy")
    report = _runner(stack, [_candidate(task)]).run_once()

    assert report.executed == 1
    assert report.reconciled == 0
    assert stack.audit.events == []


def test_reconciler_attribute_is_optional_not_required() -> None:
    """`RunnerStackPort` 不得把核对端口变成硬依赖（否则历史替身无法构造）。"""
    stack = _Stack(tasks=[], with_reconciler=False)

    assert WorkerRunner(stack, discover=lambda _stack, **_kwargs: []) is not None


# ---------------------------------------------------------------------------
# 6) 顺序：核对在候选发现之前
# ---------------------------------------------------------------------------


def test_sweep_happens_before_discovery() -> None:
    order: list[str] = []
    stack = _Stack(tasks=[_task("task_order")])

    class _OrderedReconciler(_Reconciler):
        def sweep(self, *, tenant_id: str | None = None, older_than_s: float = 60.0):
            order.append("sweep")
            return super().sweep(tenant_id=tenant_id, older_than_s=older_than_s)

    stack.reconciler = _OrderedReconciler()

    def discover(_stack: Any, **_kwargs: Any) -> list[Candidate]:
        order.append("discover")
        return []

    WorkerRunner(stack, discover=discover, heartbeat_factory=_Heartbeat).run_once()

    assert order == ["sweep", "discover"]


@pytest.mark.parametrize("older_than_s", (0.0, 120.0))
def test_sweep_passes_configured_window(older_than_s: float) -> None:
    reconciler = _Reconciler()
    stack = _Stack(tasks=[], reconciler=reconciler, reconcile_older_than_s=older_than_s)

    _runner(stack, []).run_once()

    assert reconciler.calls[0]["older_than_s"] == older_than_s


def test_lease_ttl_is_still_read_from_settings() -> None:
    """核对接入不得挤掉既有调度参数（回归：租约 TTL 仍按配置下发）。"""
    stack = _Stack(tasks=[_task("task_ttl")], worker_lease_ttl_s=12.5)
    worker = stack._worker
    task = stack.repository.get_task("task_ttl")
    recorded: list[float] = []

    original = worker.acquire_lease

    def spy(task_id: str, environment_id: str, ttl_s: float) -> Any:
        recorded.append(ttl_s)
        return original(task_id, environment_id, ttl_s)

    worker.acquire_lease = spy  # type: ignore[method-assign]
    _runner(stack, [_candidate(task)]).run_once()

    assert recorded == [12.5]


def test_task_status_after_successful_run_is_executing() -> None:
    stack = _Stack(tasks=[_task("task_exec")])
    task = stack.repository.get_task("task_exec")

    _runner(stack, [_candidate(task)]).run_once()

    assert stack.task_service.calls == [("task_exec", TaskStatus.EXECUTING)]
    assert utc_now() - timedelta(seconds=1) < utc_now()


# ===========================================================================
# W09 T06 追加（AC-20 / KD-2 / KD-3 / KD-4 / KD-5）
#
# 以上 22 个用例与断言逐字未改；本节只**新增**替身与用例。新增能力：
#   1) `sweep_detailed()` → `scanned_total` 与 `scanned`（= 结论条数）分离；
#   2) `RunReport.reconcile_scanned_total` / `reconcile_failed` 及其累加与对外字段；
#   3) `reconcile_interval_s` 节流（时钟可注入，未到间隔不发事件）；
#   4) 调度事件携带 `stack.tenant_id`。
# ===========================================================================


class _Clock:
    """可注入单调时钟：节流用例不 sleep。"""

    def __init__(self, *, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _conclusion(task_id: str, *, transitioned: bool) -> ReconcileResult:
    """真实 `ReconcileResult`（不使用替身）：顺带锁住它与 runner 的字段契约。"""
    return ReconcileResult(
        prepared_id=f"prep_{task_id}",
        tool_call_id=f"tc_{task_id}",
        task_id=task_id,
        conclusion="NEEDS_RECONCILIATION",
        task_status="NEEDS_RECONCILIATION",
        transitioned=transitioned,
        reason="PREPARED_WITHOUT_OBSERVED",
    )


class _DetailedReconciler(_Reconciler):
    """提供 `sweep_detailed()` 的核对端口（W09 T06 之后的真实口径）。"""

    def __init__(
        self,
        *,
        scanned_total: int = 0,
        results: list[_Result] | None = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(results=results, error=error)
        self._scanned_total = scanned_total
        self.detailed_calls: list[dict[str, Any]] = []

    def sweep_detailed(
        self, *, tenant_id: str | None = None, older_than_s: float = 60.0
    ) -> SweepReport:
        self.detailed_calls.append(
            {"tenant_id": tenant_id, "older_than_s": older_than_s}
        )
        if self._error is not None:
            raise self._error
        return SweepReport(
            scanned_total=self._scanned_total,
            conclusions=tuple(self._results),
            scanned_tenants=(tenant_id,) if tenant_id else (),
        )


def _swept_payload(stack: _Stack) -> dict[str, Any]:
    events = _swept_events(stack)
    assert len(events) == 1, "本轮应恰好一条 RECONCILIATION_SWEPT"
    return events[0].payload


# ---------------------------------------------------------------------------
# AC-20：5 行候选仅 2 行产出结论 → scanned_total == 5 且 conclusions == 2
# ---------------------------------------------------------------------------


def test_scanned_total_is_five_when_only_two_rows_produce_conclusions() -> None:
    reconciler = _DetailedReconciler(
        scanned_total=5,
        results=[
            _Result(
                transitioned=True,
                task_id="task_a",
                tool_call_id="tc_a",
                conclusion="NEEDS_RECONCILIATION",
            ),
            _Result(
                transitioned=False,
                task_id="task_b",
                tool_call_id="tc_b",
                conclusion="NEEDS_RECONCILIATION",
            ),
        ],
    )
    stack = _Stack(tasks=[], reconciler=reconciler)

    report = _runner(stack, []).run_once()

    payload = _swept_payload(stack)
    # `scanned` 保留 W08 语义（= 产出结论条数），只增不改。
    assert payload["scanned"] == 2
    assert payload["scanned_total"] == 5
    assert payload["scanned_total_estimated"] is False
    assert len(payload["conclusions"]) == payload["scanned"] == 2
    assert payload["converged"] == 1
    # 报告侧同口径。
    assert report.reconcile_scanned_total == 5
    assert report.reconciled == 1


def test_sweep_detailed_is_preferred_over_sweep() -> None:
    """有 `sweep_detailed()` 时不得再走 `sweep()`（否则拿不到真实行数）。"""
    reconciler = _DetailedReconciler(scanned_total=3, results=[])
    stack = _Stack(tasks=[], reconciler=reconciler)

    _runner(stack, []).run_once()

    assert len(reconciler.detailed_calls) == 1
    assert reconciler.calls == [], "sweep() 不应被调用"
    assert reconciler.detailed_calls[0] == {
        "tenant_id": TENANT,
        "older_than_s": 90.0,
    }


def test_scanned_total_is_estimated_for_reconciler_without_detailed_sweep() -> None:
    """历史替身只有 `sweep()`：如实退回结论条数并标记为估计值（不冒充真实行数）。"""
    reconciler = _Reconciler(results=[_Result(transitioned=True)])
    stack = _Stack(tasks=[], reconciler=reconciler)

    report = _runner(stack, []).run_once()

    payload = _swept_payload(stack)
    assert payload["scanned"] == 1
    assert payload["scanned_total"] == 1
    assert payload["scanned_total_estimated"] is True
    assert report.reconcile_scanned_total == 1


def test_scanned_total_is_accumulated_across_cycles() -> None:
    reconciler = _DetailedReconciler(scanned_total=5, results=[])
    stack = _Stack(tasks=[], reconciler=reconciler)
    runner = WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: [],
        heartbeat_factory=_Heartbeat,
        poll_interval_s=0.0,
        max_cycles=3,
    )

    report = runner.run_forever()

    assert report.cycles == 3
    assert report.reconcile_scanned_total == 15


# ---------------------------------------------------------------------------
# KD-5：reconcile_failed 是 failed 的子集，failed / clean 语义逐字不变
# ---------------------------------------------------------------------------


def test_sweep_failure_counts_reconcile_failed_as_subset_of_failed() -> None:
    error = DatabaseQueryError("控制库暂时不可用", details={"reason": "TEST"})
    stack = _Stack(tasks=[], reconciler=_DetailedReconciler(error=error))

    report = _runner(stack, []).run_once()

    assert report.failed == 1, "W08 契约：核对失败仍占 1 个 failed"
    assert report.reconcile_failed == 1
    assert report.clean is False
    assert report.reconcile_scanned_total == 0


def test_successful_sweep_does_not_count_reconcile_failed() -> None:
    stack = _Stack(tasks=[], reconciler=_DetailedReconciler(scanned_total=2))

    report = _runner(stack, []).run_once()

    assert report.failed == 0
    assert report.reconcile_failed == 0
    assert report.clean is True


def test_merged_and_public_dict_carry_new_reconcile_fields() -> None:
    left = RunReport(cycles=1, reconcile_failed=1, reconcile_scanned_total=5)
    right = RunReport(cycles=1, reconcile_failed=0, reconcile_scanned_total=7)

    total = left.merged(right)

    assert total.reconcile_failed == 1
    assert total.reconcile_scanned_total == 12
    assert left.reconcile_scanned_total == 5, "merged 不得就地修改"
    payload = total.to_public_dict()
    assert payload["reconcile_failed"] == 1
    assert payload["reconcile_scanned_total"] == 12
    # `failed` 语义不变：`reconcile_failed` 单独计不把报告变"不干净"。
    assert payload["clean"] is True


# ---------------------------------------------------------------------------
# KD-4：reconcile_interval_s 生效为跨周期节流（未到间隔不发事件、不计计数）
# ---------------------------------------------------------------------------


def test_reconcile_interval_not_due_skips_sweep_and_emits_no_event() -> None:
    clock = _Clock(start=1000.0)
    reconciler = _DetailedReconciler(scanned_total=4)
    stack = _Stack(tasks=[], reconciler=reconciler, reconcile_interval_s=60.0)
    runner = WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: [],
        heartbeat_factory=_Heartbeat,
        monotonic=clock,
    )

    first = runner.run_once()
    assert len(reconciler.detailed_calls) == 1
    assert first.reconcile_scanned_total == 4

    clock.advance(59.0)  # 未到 60s 间隔
    second = runner.run_once()

    assert len(reconciler.detailed_calls) == 1, "未到间隔不得再扫"
    assert len(_swept_events(stack)) == 1, "跳过的轮次不发事件"
    assert second.reconcile_scanned_total == 0
    assert second.reconciled == 0
    assert second.reconcile_failed == 0
    assert second.clean is True, "节流跳过不是失败"

    clock.advance(1.0)  # 恰好到 60s：恢复扫描
    third = runner.run_once()

    assert len(reconciler.detailed_calls) == 2
    assert len(_swept_events(stack)) == 2
    assert third.reconcile_scanned_total == 4


def test_reconcile_interval_zero_disables_throttle() -> None:
    """替身/历史栈没有 `reconcile_interval_s` → 缺省 0.0，每轮都扫（W08 行为）。"""
    clock = _Clock()
    reconciler = _Reconciler(results=[])
    stack = _Stack(tasks=[], reconciler=reconciler, reconcile_interval_s=0.0)
    runner = WorkerRunner(
        stack,
        discover=lambda _stack, **_kwargs: [],
        heartbeat_factory=_Heartbeat,
        monotonic=clock,
    )

    runner.run_once()
    runner.run_once()  # 时钟未前进也不节流

    assert len(reconciler.calls) == 2
    assert len(_swept_events(stack)) == 2


def test_reconcile_interval_reason_constant_is_stable() -> None:
    """日志 reason 是运维检索口径，不得随手改名。"""
    assert REASON_RECONCILE_INTERVAL_NOT_DUE == "RECONCILE_INTERVAL_NOT_DUE"


# ---------------------------------------------------------------------------
# KD-3：调度事件携带 stack.tenant_id（服务端组合根，非 payload 兜底）
# ---------------------------------------------------------------------------


def test_scheduler_events_carry_stack_tenant_id() -> None:
    stack = _Stack(tasks=[], reconciler=_Reconciler(results=[]))

    _runner(stack, []).run_once()

    assert stack.audit.tenants == [TENANT]
    event = stack.audit.events[0]
    assert event.task_id == SCHEDULER_TASK_ID
    assert event.payload["tenant_id"] == TENANT


def test_scheduler_failure_event_also_carries_stack_tenant_id() -> None:
    error = DatabaseQueryError("控制库不可用", details={"reason": "TEST"})
    stack = _Stack(tasks=[], reconciler=_Reconciler(error=error))

    _runner(stack, []).run_once()

    assert stack.audit.tenants == [TENANT]
    assert stack.audit.types() == [AuditEventType.RECONCILIATION_FAILED.value]
