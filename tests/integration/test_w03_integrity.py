"""W03 完整性：事件/证据数据库级只追加、复合租户外键、取消事件顺序。"""

from __future__ import annotations

import pytest

from core.errors import DatabasePermissionError, DatabaseQueryError
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


def _service(control_client) -> TaskService:
    return TaskService(TaskRepository(control_client), ToolRegistry())


@pytest.fixture(autouse=True)
def _cleanup(admin_client, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        for table in ("evidence", "task_events", "tool_calls", "task_steps", "checkpoints", "tasks"):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


def test_events_and_evidence_are_append_only_for_control_account(
    control_client, admin_client
) -> None:
    task = _service(control_client).create_task("只追加测试")
    events = admin_client.fetch_all(
        "SELECT event_id FROM dg_system.task_events WHERE task_id = %s LIMIT 1",
        (task.task_id,),
    )
    assert events
    event_id = str(events[0]["event_id"])

    with pytest.raises(DatabasePermissionError):
        control_client.execute(
            "UPDATE dg_system.task_events SET payload_json = '{}' WHERE event_id = %s",
            (event_id,),
        )
    with pytest.raises(DatabasePermissionError):
        control_client.execute(
            "DELETE FROM dg_system.task_events WHERE event_id = %s", (event_id,)
        )


def test_composite_tenant_fk_rejects_cross_tenant_evidence(
    control_client, admin_client
) -> None:
    task = _service(control_client).create_task("复合外键测试")
    with pytest.raises(DatabaseQueryError):
        control_client.execute(
            "INSERT INTO dg_system.evidence (evidence_id, tenant_id, task_id, "
            "environment_id, db_engine, db_version, adapter_version, source_tool, "
            "observed_at, collection_status, quality, completeness, payload_hash, "
            "redaction_version, reproducible, payload_json, created_at) "
            "VALUES ('ev_cross_tenant_test', 'tenant_other_fk', %s, %s, 'mysql', "
            "'8.0.46', '1.0', 'test', UTC_TIMESTAMP(6), 'SUCCESS', 'UNKNOWN', "
            "'UNKNOWN', SHA2('{}', 256), 'none', 0, '{}', UTC_TIMESTAMP(6))",
            (task.task_id, task.environment_id),
        )


def test_cancel_emits_requested_before_cancelled(control_client) -> None:
    service = _service(control_client)
    task = service.create_task("取消顺序测试")
    cancelled = service.cancel(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    events = service.list_events(task.task_id, limit=200, after_seq=-1)
    types = [event.event_type for event in events if event.event_type.startswith("TASK_CANCEL")]
    assert types == ["TASK_CANCEL_REQUESTED", "TASK_CANCELLED"]
