"""W03 V2.2 集成测试：任务归属、事件哈希链、状态版本并发与暂停/取消/恢复语义。"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.errors import DatabaseQueryError, StateConflictError, TaskNotFoundError
from core.tasks.models import TaskEvent, TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def repository(control_client) -> TaskRepository:
    return TaskRepository(control_client)


@pytest.fixture
def service(repository: TaskRepository) -> TaskService:
    return TaskService(repository, ToolRegistry())


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(admin_client, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        try:
            admin_client.execute(
                "DELETE FROM dg_system.evidence WHERE task_id = %s", (task_id,)
            )
        except DatabaseQueryError:
            pass
        for table in ("task_events", "tool_calls", "task_steps", "checkpoints", "tasks"):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


def _recompute_hash(event: TaskEvent) -> str:
    canonical = {
        "task_id": event.task_id,
        "run_id": event.run_id,
        "tool_call_id": event.tool_call_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
        "seq": event.seq,
        "previous_event_hash": event.previous_event_hash,
    }
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_create_task_persists_tenant_environment_and_hash_chain(
    service: TaskService, repository: TaskRepository
) -> None:
    task = service.create_task("检查复制延迟", trace_id="trace-create")

    assert task.status == TaskStatus.PENDING
    assert task.tenant_id == "tenant_local"
    assert task.environment_id == "env_mysql_local"
    assert task.state_version == 1
    assert task.max_steps == 8
    assert task.max_tool_calls == 10
    assert task.max_tokens == 20_000

    stored = repository.get_task(task.task_id)
    assert stored.state_version == 1
    assert stored.tenant_id == "tenant_local"

    events = service.list_events(task.task_id)
    assert events
    assert [event.seq for event in events] == list(range(len(events)))
    assert events[0].event_type == "TASK_CREATED"
    assert events[0].trace_id == "trace-create"

    previous: str | None = None
    for event in events:
        assert event.previous_event_hash == previous
        assert event.event_hash == _recompute_hash(event)
        previous = event.event_hash


def test_state_version_conflict_then_success_with_fresh_version(
    service: TaskService, repository: TaskRepository
) -> None:
    task = service.create_task("版本冲突")
    stale_version = task.state_version
    assert stale_version == 1

    planned = service.transition(task.task_id, TaskStatus.PLANNING)
    assert planned.state_version == stale_version + 1

    with pytest.raises(StateConflictError) as conflict:
        repository.update_task_status(
            task.task_id,
            TaskStatus.OBSERVING,
            expected_state_version=stale_version,
        )
    error = conflict.value
    assert error.code.value == "STATE_CONFLICT"
    assert error.details["task_id"] == task.task_id
    assert error.details["expected"] == stale_version
    assert error.details["actual"] == stale_version + 1

    fresh = repository.get_task(task.task_id)
    updated = repository.update_task_status(
        task.task_id,
        TaskStatus.OBSERVING,
        expected_state_version=fresh.state_version,
    )
    assert updated.status == TaskStatus.OBSERVING
    assert updated.state_version == stale_version + 2


def test_event_cursor_pagination_is_stable(
    service: TaskService, repository: TaskRepository
) -> None:
    task = service.create_task("事件游标分页")
    service.transition(task.task_id, TaskStatus.PLANNING)
    service.transition(task.task_id, TaskStatus.OBSERVING)
    service.transition(task.task_id, TaskStatus.DIAGNOSING)

    full = repository.list_events(task.task_id, limit=100)
    assert len(full) >= 8

    first = repository.list_events(task.task_id, after_seq=0, limit=2)
    assert [event.seq for event in first] == [0, 1]
    again = repository.list_events(task.task_id, after_seq=0, limit=2)
    assert [event.seq for event in again] == [0, 1]

    second = repository.list_events(
        task.task_id, after_seq=first[-1].seq, limit=2
    )
    assert [event.seq for event in second] == [2, 3]

    collected = [event.seq for event in first + second]
    assert collected == sorted(set(collected))
    assert collected == [event.seq for event in full[:4]]


def test_pause_and_resume_semantics(service: TaskService) -> None:
    task = service.create_task("暂停恢复")
    service.transition(task.task_id, TaskStatus.PLANNING)

    paused = service.request_pause(task.task_id, trace_id="trace-pause")
    assert paused.pause_requested is True
    assert paused.status == TaskStatus.PAUSED

    resumed = service.resume(task.task_id, trace_id="trace-resume")
    assert resumed.pause_requested is False
    assert resumed.status == TaskStatus.PLANNING

    executing = service.create_task("执行中暂停")
    service.transition(executing.task_id, TaskStatus.PLANNING)
    service.transition(executing.task_id, TaskStatus.EXECUTING)

    marked = service.request_pause(executing.task_id)
    assert marked.pause_requested is True
    assert marked.status == TaskStatus.EXECUTING

    resumed_executing = service.resume(executing.task_id)
    assert resumed_executing.pause_requested is False
    assert resumed_executing.status == TaskStatus.EXECUTING


def test_cancel_semantics(service: TaskService) -> None:
    executing = service.create_task("取消执行中任务")
    service.transition(executing.task_id, TaskStatus.PLANNING)
    service.transition(executing.task_id, TaskStatus.EXECUTING)

    cancelling = service.cancel(executing.task_id, trace_id="trace-cancel")
    assert cancelling.cancel_requested is True
    assert cancelling.status == TaskStatus.CANCEL_REQUESTED

    pending = service.create_task("取消待执行任务")
    cancelled = service.cancel(pending.task_id)
    assert cancelled.cancel_requested is True
    assert cancelled.status == TaskStatus.CANCELLED

    events = {event.event_type for event in service.list_events(executing.task_id)}
    assert "TASK_CANCEL_REQUESTED" in events
    assert "TASK_CANCELLED" not in events


def test_recover_executing_task_requires_reconciliation(
    service: TaskService, repository: TaskRepository
) -> None:
    task = service.create_task("恢复执行中任务")
    service.transition(task.task_id, TaskStatus.PLANNING)
    service.transition(task.task_id, TaskStatus.EXECUTING)

    recovered = TaskService(repository, ToolRegistry()).recover_unfinished_tasks()
    by_id = {item.task_id: item for item in recovered}
    assert task.task_id in by_id
    recovered_task = by_id[task.task_id]
    assert recovered_task.status == TaskStatus.NEEDS_RECONCILIATION
    assert recovered_task.state_version > task.state_version

    events = repository.list_events(task.task_id)
    recovery_events = [
        event for event in events if event.event_type == "TASK_RECOVERED"
    ]
    assert recovery_events
    assert recovery_events[-1].payload["requires_reconciliation"] is True
    assert recovery_events[-1].payload["status"] == "NEEDS_RECONCILIATION"


def test_tenant_filtering_and_ownership_helpers(
    service: TaskService, repository: TaskRepository
) -> None:
    task = service.create_task("租户隔离")

    local_tasks = service.list_tasks(tenant_id="tenant_local", limit=200)
    assert task.task_id in {item.task_id for item in local_tasks}
    assert all(item.tenant_id == "tenant_local" for item in local_tasks)

    assert repository.list_tasks(tenant_id="tenant_absent", limit=200) == []
    assert repository.task_belongs_to(task.task_id, "tenant_local") is True
    assert repository.task_belongs_to(task.task_id, "tenant_absent") is False
    assert repository.environment_belongs_to("env_mysql_local", "tenant_local") is True
    assert repository.environment_belongs_to("env_mysql_local", "tenant_absent") is False

    with pytest.raises(TaskNotFoundError):
        repository.get_task(task.task_id, tenant_id="tenant_absent")
