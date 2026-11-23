"""W07 收口单元测试：候选发现、参数还原 fail-closed、主循环语义。

**全部离线**：用假仓储/假审批/假 Worker 替换真实依赖，不连任何数据库。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.approval.models import ApprovalStatus
from core.audit.events import AuditEventType
from core.errors import DatabaseQueryError, StateConflictError, WorkerConflictError
from core.tasks.models import Task, TaskStatus, TaskStep, utc_now
from core.tools.contracts import ToolStatus
from workers.discovery import (
    ALERT_PARAMS_UNRESOLVED,
    CANDIDATE_TASK_STATUSES,
    discover_candidates,
    resolve_execution_params,
)
from workers.heartbeat import LeaseHeartbeat
from workers.runner import (
    OUTCOME_DENIED,
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_UNKNOWN,
    RunReport,
    WorkerRunner,
)

TENANT = "tenant_local"


# ---------------------------------------------------------------------------
# 假依赖
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.WAITING_APPROVAL,
    created_at_offset: int = 0,
    cancel_requested: bool = False,
    pause_requested: bool = False,
    state_version: int = 1,
) -> Task:
    now = utc_now() + timedelta(seconds=created_at_offset)
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        goal=f"goal {task_id}",
        status=status,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id="env_mysql_local",
        cancel_requested=cancel_requested,
        pause_requested=pause_requested,
        state_version=state_version,
    )


def _step(
    step_id: str,
    task: Task,
    *,
    tool_name: str = "mysql_lab_update_order_note",
    params: dict[str, Any] | None = None,
    status: str = "PENDING",
    step_index: int = 0,
) -> TaskStep:
    return TaskStep(
        step_id=step_id,
        task_id=task.task_id,
        run_id=task.run_id,
        step_index=step_index,
        title=f"step {step_id}",
        tool_name=tool_name,
        params=dict(params or {}),
        status=status,  # type: ignore[arg-type]
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@dataclass
class FakeApproval:
    """`ApprovalDetail` 的最小替身（只提供发现路径真正读取的字段）。"""

    approval_id: str
    task_id: str
    tool_name: str = "mysql_lab_update_order_note"
    step_id: str | None = None
    requested_by: str = "user_operator"
    created_at: Any = field(default_factory=utc_now)
    expires_at: Any = field(default_factory=lambda: utc_now() + timedelta(minutes=10))

    def is_expired(self, *, now: Any = None) -> bool:
        current = now or utc_now()
        return self.expires_at <= current


class FakeTaskRepository:
    def __init__(
        self,
        tasks: list[Task],
        steps: dict[str, list[TaskStep]],
        *,
        duplicate_results: bool = False,
    ) -> None:
        self._tasks = list(tasks)
        self._steps = steps
        self._duplicate = duplicate_results
        self.list_calls: list[dict[str, Any]] = []

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
    ) -> list[Task]:
        self.list_calls.append({"status": status, "limit": limit, "tenant_id": tenant_id})
        rows = [task for task in self._tasks if status is None or task.status is status]
        if self._duplicate:
            # 模拟"同一任务被两次查询返回"的漂移，用于验证按任务维度去重。
            rows = rows + rows
        return rows[:limit]

    def list_steps(self, task_id: str) -> list[TaskStep]:
        return list(self._steps.get(task_id, []))

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        for task in self._tasks:
            if task.task_id == task_id:
                return task
        raise StateConflictError("任务不存在", details={"task_id": task_id})

    def replace(self, task: Task) -> None:
        self._tasks = [task if item.task_id == task.task_id else item for item in self._tasks]


class FakeTaskService:
    """`TaskService.transition()` 的替身：迁移 + 涨版本 + 记录调用顺序。"""

    def __init__(self, repository: FakeTaskRepository, order_log: list[str]) -> None:
        self._repository = repository
        self._log = order_log
        self.calls: list[tuple[str, TaskStatus]] = []

    def transition(
        self, task_id: str, target: TaskStatus, **kwargs: Any
    ) -> Task:
        self._log.append("transition")
        self.calls.append((task_id, target))
        task = self._repository.get_task(task_id)
        if task.status is target:
            return task
        updated = task.model_copy(
            update={"status": target, "state_version": task.state_version + 1}
        )
        self._repository.replace(updated)
        return updated


class FakeApprovalService:
    def __init__(self, by_task: dict[str, list[FakeApproval]]) -> None:
        self._by_task = by_task
        self.calls: list[dict[str, Any]] = []

    def list(
        self,
        *,
        tenant_id: str,
        status: ApprovalStatus | None = None,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[FakeApproval], str | None]:
        self.calls.append(
            {"tenant_id": tenant_id, "status": status, "task_id": task_id, "limit": limit}
        )
        return list(self._by_task.get(str(task_id), [])), None


@dataclass
class FakeLease:
    task_id: str
    expires_at: Any


class FakeLeaseRepository:
    def __init__(self, active: dict[str, FakeLease] | None = None) -> None:
        self._active = dict(active or {})

    def get_active(self, task_id: str) -> FakeLease | None:
        return self._active.get(task_id)


class FakeLeaseService:
    def __init__(self, active: dict[str, FakeLease] | None = None) -> None:
        self.repository = FakeLeaseRepository(active)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def try_emit(self, event: Any, *, actor_user_id: str | None = None) -> bool:
        self.events.append(event)
        return True

    def types(self) -> list[str]:
        return [getattr(item.event_type, "value", str(item.event_type)) for item in self.events]


class FakeWorker:
    """按脚本产生结果的 Worker 替身；记录每次调用以断言"不重试"。"""

    def __init__(
        self,
        *,
        conflict: bool = False,
        submit_error: BaseException | None = None,
        status: ToolStatus = ToolStatus.SUCCESS,
    ) -> None:
        self.worker_id = "worker_test_01"
        self.conflict = conflict
        self.submit_error = submit_error
        self.status = status
        self.acquire_calls: list[dict[str, Any]] = []
        self.submit_calls: list[dict[str, Any]] = []
        self.heartbeat_calls = 0
        self.release_calls = 0

    def acquire_lease(self, task_id: str, environment_id: str, ttl_s: float) -> Any:
        self.acquire_calls.append(
            {"task_id": task_id, "environment_id": environment_id, "ttl_s": ttl_s}
        )
        if self.conflict:
            raise WorkerConflictError(
                "任务已有活动 Worker 租约",
                details={"task_id": task_id, "worker_id": "someone_else"},
            )
        return SimpleNamespace(lease_id="lease_test", task_id=task_id)

    def heartbeat(self, ttl_s: float) -> Any:
        self.heartbeat_calls += 1
        raise AssertionError("runner 不应在测试替身上直接调用 heartbeat")

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
        self.submit_calls.append(
            {
                "tool_name": tool_name,
                "params": params,
                "step_id": step_id,
                "state_version": state_version,
                "approval_id": approval_id,
                "ttl_s": ttl_s,
            }
        )
        if self.submit_error is not None:
            raise self.submit_error
        return SimpleNamespace(status=self.status)

    def release(self) -> Any:
        self.release_calls += 1
        return SimpleNamespace(lease_id="lease_test")


class FakeHeartbeat:
    """心跳替身：可预置 `lost`，并记录 start/stop。"""

    instances: list["FakeHeartbeat"] = []

    def __init__(
        self,
        worker: Any,
        *,
        interval_s: float,
        ttl_s: float,
        name: str = "fake-heartbeat",
        lost: bool = False,
    ) -> None:
        self.worker = worker
        self.interval_s = interval_s
        self.ttl_s = ttl_s
        self.lost = lost
        self.started = False
        self.stopped = False
        FakeHeartbeat.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self.stopped = True


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "worker_poll_interval_s": 0.01,
        "worker_shutdown_grace_s": 30.0,
        "worker_lease_ttl_s": 30.0,
        "worker_heartbeat_interval_s": 10.0,
        "gateway_envelope_ttl_s": 60.0,
        "worker_actor_user_id": "system:worker",
        "worker_max_concurrency": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeStack:
    def __init__(
        self,
        *,
        tasks: list[Task],
        steps: dict[str, list[TaskStep]] | None = None,
        approvals: dict[str, list[FakeApproval]] | None = None,
        active_leases: dict[str, FakeLease] | None = None,
        worker: Any = None,
        settings: SimpleNamespace | None = None,
        duplicate_tasks: bool = False,
    ) -> None:
        self.tenant_id = TENANT
        self.worker_id = "worker_test_01"
        self.order_log: list[str] = []
        self.repository = FakeTaskRepository(
            tasks, steps or {}, duplicate_results=duplicate_tasks
        )
        self.task_service = FakeTaskService(self.repository, self.order_log)
        self.approvals = FakeApprovalService(approvals or {})
        self.lease_service = FakeLeaseService(active_leases)
        self.audit = FakeAudit()
        self.settings = settings or _settings()
        self._worker = worker or FakeWorker()
        self.worker_for_calls: list[str] = []

    def worker_for(self, actor_user_id: str) -> Any:
        self.worker_for_calls.append(actor_user_id)
        return self._worker


def _candidate(
    task: Task,
    approval: FakeApproval,
    *,
    params: dict[str, Any] | None = None,
    step_id: str | None = None,
) -> Any:
    from workers.discovery import Candidate

    return Candidate(
        task=task,
        approval=approval,  # type: ignore[arg-type]
        tool_name=approval.tool_name,
        params=dict(params if params is not None else {"order_id": 2001}),
        step_id=step_id,
    )


# ---------------------------------------------------------------------------
# 候选发现
# ---------------------------------------------------------------------------


def test_candidate_task_statuses_is_waiting_approval_only() -> None:
    # rev.2：审批通过不改任务状态，候选的真实形态只有 WAITING_APPROVAL。
    assert set(CANDIDATE_TASK_STATUSES) == {TaskStatus.WAITING_APPROVAL}
    assert TaskStatus.EXECUTING not in CANDIDATE_TASK_STATUSES


def test_discover_ignores_already_executing_tasks() -> None:
    """已在 EXECUTING 的任务（可能正在执行/已消费）绝不能再被当候选。"""
    task = _task("task_executing", status=TaskStatus.EXECUTING)
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_e", task)]},
        approvals={task.task_id: [FakeApproval("apr_e", task.task_id)]},
    )

    assert discover_candidates(stack, limit=10) == []


def test_discover_candidates_orders_by_creation_and_dedupes() -> None:
    later = _task("task_b", created_at_offset=60)
    earlier = _task("task_a", created_at_offset=0)
    stack = FakeStack(
        tasks=[later, earlier],
        steps={
            later.task_id: [_step("step_b", later, params={"order_id": 2002})],
            earlier.task_id: [_step("step_a", earlier, params={"order_id": 2001})],
        },
        approvals={
            later.task_id: [FakeApproval("apr_b", later.task_id)],
            earlier.task_id: [FakeApproval("apr_a", earlier.task_id)],
        },
        # 模拟"同一个任务被查询返回两次"的状态漂移，必须按 task_id 去重成一条。
        duplicate_tasks=True,
    )

    candidates = discover_candidates(stack, limit=10)

    assert [item.task_id for item in candidates] == ["task_a", "task_b"]
    assert [item.approval_id for item in candidates] == ["apr_a", "apr_b"]
    assert stack.approvals.calls[0]["status"] is ApprovalStatus.APPROVED
    assert stack.approvals.calls[0]["tenant_id"] == TENANT


def test_discover_requires_approved_approval() -> None:
    task = _task("task_no_approval")
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_1", task)]},
        approvals={},
    )

    assert discover_candidates(stack, limit=10) == []
    # 仍必须按 APPROVED 过滤去问审批服务，而不是"先执行再校验"。
    assert [call["status"] for call in stack.approvals.calls] == [ApprovalStatus.APPROVED]


def test_discover_skips_expired_approval() -> None:
    task = _task("task_expired")
    expired = FakeApproval(
        "apr_expired",
        task.task_id,
        expires_at=utc_now() - timedelta(seconds=1),
    )
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_1", task)]},
        approvals={task.task_id: [expired]},
    )

    assert discover_candidates(stack, limit=10) == []


def test_discover_skips_task_with_live_lease() -> None:
    task = _task("task_busy")
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_1", task)]},
        approvals={task.task_id: [FakeApproval("apr_busy", task.task_id)]},
        active_leases={
            task.task_id: FakeLease(task.task_id, expires_at=utc_now() + timedelta(seconds=20))
        },
    )

    assert discover_candidates(stack, limit=10) == []


def test_discover_allows_task_with_expired_lease() -> None:
    task = _task("task_stale_lease")
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_1", task)]},
        approvals={task.task_id: [FakeApproval("apr_stale", task.task_id)]},
        active_leases={
            task.task_id: FakeLease(task.task_id, expires_at=utc_now() - timedelta(seconds=5))
        },
    )

    assert [item.task_id for item in discover_candidates(stack, limit=10)] == [
        "task_stale_lease"
    ]


def test_discover_skips_paused_and_cancelled_tasks() -> None:
    paused = _task("task_paused", pause_requested=True)
    cancelled = _task("task_cancelled", cancel_requested=True, created_at_offset=10)
    stack = FakeStack(
        tasks=[paused, cancelled],
        steps={
            paused.task_id: [_step("step_p", paused)],
            cancelled.task_id: [_step("step_c", cancelled)],
        },
        approvals={
            paused.task_id: [FakeApproval("apr_p", paused.task_id)],
            cancelled.task_id: [FakeApproval("apr_c", cancelled.task_id)],
        },
    )

    assert discover_candidates(stack, limit=10) == []


def test_discover_respects_limit_and_zero_limit() -> None:
    tasks = [_task(f"task_{index}", created_at_offset=index) for index in range(3)]
    steps = {task.task_id: [_step(f"step_{task.task_id}", task)] for task in tasks}
    approvals = {
        task.task_id: [FakeApproval(f"apr_{task.task_id}", task.task_id)] for task in tasks
    }
    stack = FakeStack(tasks=tasks, steps=steps, approvals=approvals)

    assert len(discover_candidates(stack, limit=2)) == 2
    assert discover_candidates(stack, limit=0) == []


# ---------------------------------------------------------------------------
# 参数还原（fail closed）
# ---------------------------------------------------------------------------


def test_resolve_params_prefers_approval_step_id() -> None:
    task = _task("task_step_hit")
    hit = _step("step_hit", task, params={"order_id": 2001, "note": "x"}, step_index=0)
    other = _step("step_other", task, params={"order_id": 2099}, step_index=1)
    stack = FakeStack(tasks=[task], steps={task.task_id: [hit, other]})

    resolved = resolve_execution_params(
        stack, task=task, approval=FakeApproval("apr_1", task.task_id, step_id="step_hit")
    )

    assert resolved is not None
    assert resolved.step_id == "step_hit"
    assert resolved.params == {"order_id": 2001, "note": "x"}
    assert resolved.tool_name == "mysql_lab_update_order_note"


def test_resolve_params_falls_back_to_last_unfinished_step() -> None:
    task = _task("task_fallback")
    steps = [
        _step("step_0", task, params={"order_id": 2001}, step_index=0, status="COMPLETED"),
        _step("step_1", task, params={"order_id": 2002}, step_index=1, status="PENDING"),
        _step("step_2", task, params={"order_id": 2003}, step_index=2, status="RUNNING"),
    ]
    stack = FakeStack(tasks=[task], steps={task.task_id: steps})

    resolved = resolve_execution_params(
        stack, task=task, approval=FakeApproval("apr_2", task.task_id, step_id=None)
    )

    assert resolved is not None
    assert resolved.step_id == "step_2"
    assert resolved.params == {"order_id": 2003}


def test_resolve_params_ignores_finished_and_other_tool_steps() -> None:
    task = _task("task_only_finished")
    steps = [
        _step("step_done", task, params={"order_id": 2001}, status="COMPLETED"),
        _step("step_other_tool", task, tool_name="other_tool", params={"a": 1}),
    ]
    stack = FakeStack(tasks=[task], steps={task.task_id: steps})

    assert (
        resolve_execution_params(
            stack, task=task, approval=FakeApproval("apr_3", task.task_id, step_id=None)
        )
        is None
    )
    assert stack.audit.types() == [AuditEventType.RECONCILIATION_REQUIRED.value]
    payload = stack.audit.events[0].payload
    assert payload["reason"] == "NO_UNFINISHED_STEP"
    assert payload["action"] == "DO_NOT_EXECUTE"
    assert payload["approval_id"] == "apr_3"


def test_resolve_params_fail_closed_when_step_id_missing() -> None:
    task = _task("task_step_gone")
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_present", task, params={"order_id": 2001})]},
    )

    resolved = resolve_execution_params(
        stack, task=task, approval=FakeApproval("apr_4", task.task_id, step_id="step_gone")
    )

    assert resolved is None
    assert stack.audit.events[0].payload["reason"] == "STEP_NOT_FOUND"


def test_resolve_params_fail_closed_on_step_tool_mismatch() -> None:
    task = _task("task_mismatch")
    other = _step("step_other", task, tool_name="another_tool", params={"order_id": 2001})
    stack = FakeStack(tasks=[task], steps={task.task_id: [other]})

    resolved = resolve_execution_params(
        stack, task=task, approval=FakeApproval("apr_5", task.task_id, step_id="step_other")
    )

    assert resolved is None
    assert stack.audit.events[0].payload["reason"] == "STEP_TOOL_MISMATCH"


def test_resolve_params_emits_alert_log(caplog: pytest.LogCaptureFixture) -> None:
    task = _task("task_alert")
    stack = FakeStack(tasks=[task], steps={task.task_id: []})

    with caplog.at_level("ERROR"):
        resolved = resolve_execution_params(
            stack, task=task, approval=FakeApproval("apr_6", task.task_id, step_id=None)
        )

    assert resolved is None
    record = next(item for item in caplog.records if item.getMessage().startswith("无法从控制库"))
    assert getattr(record, "alert", None) == ALERT_PARAMS_UNRESOLVED
    assert getattr(record, "reason", None) == "NO_UNFINISHED_STEP"


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def test_runner_executes_candidate_and_releases_lease() -> None:
    task = _task("task_ok")
    worker = FakeWorker()
    settings = _settings()
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_ok", task, params={"order_id": 2001})]},
        approvals={task.task_id: [FakeApproval("apr_ok", task.task_id, step_id="step_ok")]},
        worker=worker,
        settings=settings,
    )
    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )

    report = runner.run_once()

    assert report.executed == 1
    assert report.clean is True
    # rev.2：先迁移 WAITING_APPROVAL → EXECUTING，再领租约/建信封。
    assert stack.task_service.calls == [("task_ok", TaskStatus.EXECUTING)]
    assert worker.acquire_calls[0]["task_id"] == "task_ok"
    assert worker.acquire_calls[0]["ttl_s"] == settings.worker_lease_ttl_s
    assert worker.release_calls == 1
    submitted = worker.submit_calls[0]
    assert submitted["approval_id"] == "apr_ok"
    assert submitted["step_id"] == "step_ok"
    assert submitted["params"] == {"order_id": 2001}
    assert submitted["ttl_s"] == settings.gateway_envelope_ttl_s
    # 信封用的是**迁移后**的 state_version（1 → 2），否则 Gateway 必判版本过期。
    assert submitted["state_version"] == 2
    # 执行主体优先取审批申请人。
    assert stack.worker_for_calls == ["user_operator"]


def test_runner_transitions_before_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """信封必须在 `WAITING_APPROVAL → EXECUTING` 之后构造，且用迁移后的版本。

    用**真 `Worker`** + 假端口：`Worker.submit()` 内部才调用 `build_envelope`，
    因此 spy 掉 `core.worker.worker.build_envelope` 就能拿到真实调用顺序与入参。
    """
    import core.worker.worker as worker_module
    from core.security.models import LeaseStatus, WorkerLease
    from core.worker.worker import Worker

    task = _task("task_order", state_version=1)
    assert task.status is TaskStatus.WAITING_APPROVAL

    order: list[str] = []
    captured: list[Any] = []
    real_build = worker_module.build_envelope

    def spy_build(*args: Any, **kwargs: Any) -> Any:
        order.append("build_envelope")
        captured.append(dict(kwargs))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(worker_module, "build_envelope", spy_build)

    class _LeasePort:
        def __init__(self) -> None:
            self.lease = WorkerLease(
                lease_id="lease_order",
                worker_id="worker_order",
                task_id=task.task_id,
                tenant_id=TENANT,
                environment_id=task.environment_id,
                fencing_token=1,
                status=LeaseStatus.ACTIVE,
                issued_at=utc_now(),
                heartbeat_at=utc_now(),
                expires_at=utc_now() + timedelta(seconds=60),
            )

        def acquire(self, **kwargs: Any) -> WorkerLease:
            return self.lease

        def heartbeat(self, *args: Any, **kwargs: Any) -> WorkerLease:
            return self.lease

        def release(self, *args: Any, **kwargs: Any) -> WorkerLease:
            return self.lease

    class _GatewayPort:
        def __init__(self) -> None:
            self.envelopes: list[Any] = []
            self.params: list[dict[str, Any]] = []

        def execute(self, envelope: Any, params: dict[str, Any], *, now: Any = None) -> Any:
            self.envelopes.append(envelope)
            self.params.append(params)
            return SimpleNamespace(status=ToolStatus.SUCCESS)

    gateway = _GatewayPort()
    worker = Worker(
        "worker_order",
        TENANT,
        "user_operator",
        _LeasePort(),
        gateway,
        signing_key=b"test-signing-key-32-bytes-long!!",
    )

    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_order", task, params={"order_id": 2001})]},
        approvals={
            task.task_id: [
                FakeApproval("apr_order", task.task_id, step_id="step_order")
            ]
        },
        worker=worker,
    )
    # 把迁移动作也汇入同一个顺序日志，用来断言先后关系。
    original_transition = stack.task_service.transition

    def transition_and_log(task_id: str, target: TaskStatus, **kwargs: Any) -> Task:
        order.append("transition")
        return original_transition(task_id, target, **kwargs)

    stack.task_service.transition = transition_and_log  # type: ignore[method-assign]

    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )
    report = runner.run_once()

    assert report.executed == 1
    assert order == ["transition", "build_envelope"], order
    assert len(gateway.envelopes) == 1
    # 信封里带的是迁移后的 state_version（1 → 2），不是迁移前的 1。
    assert captured[0]["state_version"] == 2
    assert gateway.envelopes[0].state_version == 2
    assert task.state_version == 1, "原对象不应被就地修改"


def test_runner_skips_candidate_when_transition_fails() -> None:
    from core.errors import StateInvalidError

    task = _task("task_bad_transition")
    worker = FakeWorker()
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_bt", task)]},
        approvals={task.task_id: [FakeApproval("apr_bt", task.task_id)]},
        worker=worker,
    )

    class _FailingTaskService:
        def __init__(self) -> None:
            self.calls = 0

        def transition(self, task_id: str, target: TaskStatus, **kwargs: Any) -> Task:
            self.calls += 1
            raise StateInvalidError("非法任务状态迁移", details={"task_id": task_id})

    failing = _FailingTaskService()
    stack.task_service = failing  # type: ignore[assignment]

    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )
    report = runner.run_once()

    assert report.skipped == 1
    assert failing.calls == 1
    assert worker.acquire_calls == [], "迁移失败不得领取租约或提交工具"


def test_runner_skips_lease_conflict_without_retry() -> None:
    task = _task("task_conflict")
    worker = FakeWorker(conflict=True)
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_c", task)]},
        approvals={task.task_id: [FakeApproval("apr_c", task.task_id)]},
        worker=worker,
    )
    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )

    report = runner.run_once()

    assert report.skipped == 1
    assert report.clean is False
    assert len(worker.acquire_calls) == 1, "冲突后不得延迟重试"
    assert worker.submit_calls == []
    assert worker.release_calls == 0


def test_runner_does_not_retry_on_failure() -> None:
    task = _task("task_fail")
    worker = FakeWorker(
        submit_error=DatabaseQueryError("提交失败", details={"reason": "TEST"})
    )
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_f", task)]},
        approvals={task.task_id: [FakeApproval("apr_f", task.task_id)]},
        worker=worker,
    )
    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )

    report = runner.run_once()

    assert report.failed == 1
    assert len(worker.submit_calls) == 1, "写操作失败绝不重试"
    assert worker.release_calls == 1


def test_runner_skips_submit_when_heartbeat_lost() -> None:
    task = _task("task_lost")
    worker = FakeWorker()
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_l", task)]},
        approvals={task.task_id: [FakeApproval("apr_l", task.task_id)]},
        worker=worker,
    )

    def factory(worker_arg: Any, *, interval_s: float, ttl_s: float, name: str = "x") -> FakeHeartbeat:
        return FakeHeartbeat(worker_arg, interval_s=interval_s, ttl_s=ttl_s, name=name, lost=True)

    runner = WorkerRunner(
        stack,
        discover=lambda stack, limit: discover_candidates(stack, limit=limit),
        heartbeat_factory=factory,
    )

    report = runner.run_once()

    assert report.skipped == 1
    assert worker.submit_calls == [], "租约已丢失时不得提交新步骤"
    assert worker.release_calls == 1
    assert FakeHeartbeat.instances[-1].stopped is True


@pytest.mark.parametrize(
    ("status", "field"),
    [
        (ToolStatus.SUCCESS, "executed"),
        (ToolStatus.DENIED, "denied"),
        (ToolStatus.UNKNOWN_OUTCOME, "unknown_outcome"),
        (ToolStatus.TIMEOUT, "failed"),
        (ToolStatus.ERROR, "failed"),
    ],
)
def test_runner_classifies_tool_results(status: ToolStatus, field: str) -> None:
    task = _task(f"task_{status.value}")
    worker = FakeWorker(status=status)
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_x", task)]},
        approvals={task.task_id: [FakeApproval("apr_x", task.task_id)]},
        worker=worker,
    )
    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )

    report = runner.run_once()

    assert getattr(report, field) == 1
    # 未知结果必须被单独计数，不能并入 executed。
    if status is ToolStatus.UNKNOWN_OUTCOME:
        assert report.executed == 0
        assert report.clean is False


def test_runner_request_stop_prevents_new_work() -> None:
    task = _task("task_stop")
    worker = FakeWorker()
    stack = FakeStack(
        tasks=[task],
        steps={task.task_id: [_step("step_s", task)]},
        approvals={task.task_id: [FakeApproval("apr_s", task.task_id)]},
        worker=worker,
    )
    runner = WorkerRunner(
        stack, discover=lambda stack, limit: discover_candidates(stack, limit=limit)
    )
    runner.request_stop(15)
    assert runner.stop_requested is True

    report = runner.run_once()

    assert report.skipped == 1
    assert worker.acquire_calls == []


def test_run_forever_stops_after_request_stop() -> None:
    stack = FakeStack(tasks=[], steps={}, approvals={})
    cycles: list[int] = []

    def discover(stack_arg: Any, *, limit: int) -> list[Any]:
        cycles.append(1)
        return []

    runner = WorkerRunner(stack, discover=discover, poll_interval_s=0.001)
    runner.request_stop(signum=None)

    report = runner.run_forever()

    assert cycles == [], "停止请求后不应再发现候选"
    assert report.cycles == 0


def test_run_forever_respects_max_cycles() -> None:
    stack = FakeStack(tasks=[], steps={}, approvals={})
    calls: list[int] = []

    def discover(stack_arg: Any, *, limit: int) -> list[Any]:
        calls.append(1)
        return []

    runner = WorkerRunner(
        stack, discover=discover, poll_interval_s=0.0, max_cycles=3
    )

    report = runner.run_forever()

    assert report.cycles == 3
    assert len(calls) == 3


def test_run_report_clean_semantics() -> None:
    assert RunReport().clean is True
    assert RunReport(executed=2).clean is True
    for field_name in ("denied", "skipped", "failed", "unknown_outcome"):
        assert getattr(RunReport(**{field_name: 1}), "clean") is False


def test_runner_rejects_multi_concurrency() -> None:
    stack = FakeStack(tasks=[], steps={}, approvals={}, settings=_settings(worker_max_concurrency=2))
    with pytest.raises(ValueError, match="单并发"):
        WorkerRunner(stack)


# ---------------------------------------------------------------------------
# 心跳线程
# ---------------------------------------------------------------------------


class _HeartbeatWorker:
    def __init__(self, *, fail: bool = False) -> None:
        self.worker_id = "worker_hb"
        self.calls = 0
        self._fail = fail

    def heartbeat(self, ttl_s: float) -> Any:
        self.calls += 1
        if self._fail:
            raise WorkerConflictError("续租失败", details={"reason": "TEST"})
        return SimpleNamespace(ttl_s=ttl_s)


def _wait_until(predicate: Any, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_heartbeat_keeps_renewing_until_stopped() -> None:
    worker = _HeartbeatWorker()
    heartbeat = LeaseHeartbeat(worker, interval_s=0.005, ttl_s=1.0)

    heartbeat.start()
    assert _wait_until(lambda: worker.calls >= 2)
    heartbeat.stop()
    calls_at_stop = worker.calls
    time.sleep(0.02)

    assert heartbeat.lost is False
    assert worker.calls == calls_at_stop, "stop() 之后不得再续租"


def test_heartbeat_marks_lost_after_failure() -> None:
    worker = _HeartbeatWorker(fail=True)
    heartbeat = LeaseHeartbeat(worker, interval_s=0.005, ttl_s=1.0)

    heartbeat.start()
    assert _wait_until(lambda: heartbeat.lost is True)
    heartbeat.stop()

    assert heartbeat.lost is True
    assert _wait_until(lambda: not heartbeat.running)


def test_heartbeat_rejects_invalid_intervals() -> None:
    worker = _HeartbeatWorker()
    with pytest.raises(ValueError):
        LeaseHeartbeat(worker, interval_s=0.0, ttl_s=1.0)
    with pytest.raises(ValueError):
        LeaseHeartbeat(worker, interval_s=2.0, ttl_s=1.0)


def test_heartbeat_cannot_start_twice() -> None:
    heartbeat = LeaseHeartbeat(_HeartbeatWorker(), interval_s=0.05, ttl_s=1.0)
    heartbeat.start()
    try:
        with pytest.raises(RuntimeError):
            heartbeat.start()
    finally:
        heartbeat.stop()


def test_outcome_constants_are_distinct() -> None:
    assert len({OUTCOME_EXECUTED, OUTCOME_DENIED, OUTCOME_SKIPPED, OUTCOME_FAILED, OUTCOME_UNKNOWN}) == 5


# ---------------------------------------------------------------------------
# CLI 装配（不连库）
# ---------------------------------------------------------------------------


def test_build_parser_declares_documented_flags() -> None:
    from workers.cli import build_parser

    parser = build_parser()
    dests = {action.dest for action in parser._actions}

    assert {"once", "self_check", "dry_run", "worker_id", "tenant"} <= dests
    defaults = vars(parser.parse_args([]))
    assert defaults["once"] is False
    assert defaults["self_check"] is False
    assert defaults["dry_run"] is False
    assert defaults["worker_id"] is None
    assert defaults["tenant"] is None


def test_install_signal_handlers_forwards_to_request_stop() -> None:
    import signal

    from workers.cli import _install_signal_handlers

    calls: list[int | None] = []

    class _Runner:
        def request_stop(self, signum: int | None = None) -> None:
            calls.append(signum)

    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert _install_signal_handlers(_Runner()) is True
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
    finally:
        for sig, original in originals.items():
            signal.signal(sig, original)

    assert calls == [signal.SIGTERM]
