"""审计服务集成：追加、查询、完整性定位与只追加授权（P0-11/P0-12/P0-13）。

真实控制库（`dg_ctl`）验证三件事：
1. `emit_required` 写入的事件可被按租户/任务/审批/类型查询；
2. 用高权账号篡改或删除事件后，`verify` 能给出**精确**断裂位置；
3. `approval_consumptions`/`step_prepared`/`step_observed` 的 UPDATE/DELETE
   在数据库层被拒绝（不是应用层自律），`approvals` 只允许 UPDATE、不允许 DELETE。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.audit.events import AuditEventType, make_event
from core.audit.integrity import (
    REASON_EVENT_HASH_MISMATCH,
    REASON_SEQ_GAP,
    verify_event_chain,
)
from core.audit.service import AuditService
from core.errors import DatabasePermissionError, TaskNotFoundError
from core.tasks.models import TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

# 逆 FK 顺序清理（新表必须排在 task_events 之前，否则外键失败）
_CLEANUP_TABLES = (
    "step_observed",
    "step_prepared",
    "approval_consumptions",
    "approvals",
    "task_events",
    "tool_calls",
    "task_steps",
    "checkpoints",
    "execution_envelopes",
    "worker_leases",
    "tasks",
)


@pytest.fixture()
def created_tasks(admin_client, monkeypatch):
    """追踪本用例创建的任务并在结束时按逆 FK 顺序清理。"""
    tracked: list[str] = []
    original = TaskService.create_task

    def tracked_create(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        tracked.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked_create)
    yield tracked
    for task_id in tracked:
        for table in _CLEANUP_TABLES:
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


@pytest.fixture()
def task_service(control_client) -> TaskService:
    return TaskService(TaskRepository(control_client), ToolRegistry())


@pytest.fixture()
def audit(control_client) -> AuditService:
    return AuditService(TaskRepository(control_client))


def _append(audit: AuditService, task, event_type: AuditEventType, payload: dict) -> None:
    audit.emit_required(
        make_event(
            event_type,
            task_id=task.task_id,
            run_id=task.run_id,
            payload={"tenant_id": task.tenant_id, "actor_user_id": "user_local", **payload},
        )
    )


def test_emit_and_query_isolated_by_tenant(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("审计查询用例")
    _append(audit, task, AuditEventType.APPROVAL_REQUESTED, {"approval_id": "apr_q1"})
    _append(audit, task, AuditEventType.APPROVAL_APPROVED, {"approval_id": "apr_q1"})

    events, cursor = audit.query(tenant_id=task.tenant_id, task_id=task.task_id, limit=50)
    assert cursor is None
    types = [event.event_type for event in events]
    assert "APPROVAL_REQUESTED" in types
    assert "APPROVAL_APPROVED" in types
    # TASK_CREATED / CHECKPOINT_SAVED 也应在同一链上（单一事件真相）
    assert "TASK_CREATED" in types
    assert events == sorted(events, key=lambda item: item.created_at)


def test_query_filters_by_approval_and_event_type(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("审计过滤用例")
    _append(audit, task, AuditEventType.APPROVAL_REQUESTED, {"approval_id": "apr_filter"})
    _append(audit, task, AuditEventType.APPROVAL_CONSUMED, {"approval_id": "apr_filter"})
    _append(audit, task, AuditEventType.APPROVAL_REQUESTED, {"approval_id": "apr_other"})

    events, _ = audit.query(
        tenant_id=task.tenant_id, approval_id="apr_filter", limit=50
    )
    assert {event.payload["approval_id"] for event in events} == {"apr_filter"}
    assert len(events) == 2

    requested, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["APPROVAL_REQUESTED"],
        limit=50,
    )
    assert all(event.event_type == "APPROVAL_REQUESTED" for event in requested)
    assert len(requested) == 2


def test_query_is_paginated_by_cursor(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("审计分页用例")
    for index in range(3):
        _append(audit, task, AuditEventType.AUDIT_WRITE_FAILED, {"index": index})

    first_page, cursor = audit.query(tenant_id=task.tenant_id, task_id=task.task_id, limit=2)
    assert len(first_page) == 2
    assert cursor is not None
    second_page, next_cursor = audit.query(
        tenant_id=task.tenant_id, task_id=task.task_id, limit=2, cursor=cursor
    )
    assert len(second_page) >= 1
    assert {event.event_id for event in first_page}.isdisjoint(
        {event.event_id for event in second_page}
    )
    assert next_cursor is None or isinstance(next_cursor, str)


def test_query_rejects_unknown_task_for_tenant(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("跨租户查询")
    with pytest.raises(TaskNotFoundError):
        audit.query(tenant_id="tenant_other", task_id=task.task_id, limit=10)


def test_verify_passes_for_intact_chain(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("完整性通过")
    _append(audit, task, AuditEventType.APPROVAL_REQUESTED, {"approval_id": "apr_ok"})

    result = audit.verify(tenant_id=task.tenant_id, task_id=task.task_id)
    assert result.ok is True
    assert result.checked >= 2  # TASK_CREATED + CHECKPOINT_SAVED + 本次事件
    assert result.broken_at_seq is None


def test_verify_detects_tampered_event_with_precise_seq(
    task_service, audit, admin_client, created_tasks
) -> None:
    task = task_service.create_task("篡改定位")
    for index in range(3):
        _append(audit, task, AuditEventType.AUDIT_INTEGRITY_FAILED, {"index": index})

    rows = admin_client.fetch_all(
        "SELECT seq, event_id FROM dg_system.task_events WHERE task_id = %s "
        "ORDER BY seq ASC",
        (task.task_id,),
    )
    target = rows[2]
    # 用高权账号绕过授权篡改载荷（模拟"具备 DDL 权限的同一账号"这一已知残余风险）
    admin_client.execute(
        "UPDATE dg_system.task_events SET payload_json = %s WHERE event_id = %s",
        ('{"tampered":true}', str(target["event_id"])),
    )

    result = audit.verify(tenant_id=task.tenant_id, task_id=task.task_id)
    assert result.ok is False
    assert result.broken_at_seq == int(target["seq"])
    assert result.reason == REASON_EVENT_HASH_MISMATCH
    assert result.affected_range[1] == int(rows[-1]["seq"])


def test_verify_locates_deleted_middle_event(
    task_service, audit, admin_client, created_tasks
) -> None:
    task = task_service.create_task("删除定位")
    for index in range(3):
        _append(audit, task, AuditEventType.AUDIT_WRITE_FAILED, {"index": index})

    rows = admin_client.fetch_all(
        "SELECT seq, event_id FROM dg_system.task_events WHERE task_id = %s ORDER BY seq ASC",
        (task.task_id,),
    )
    victim = rows[2]
    admin_client.execute(
        "DELETE FROM dg_system.task_events WHERE event_id = %s", (str(victim["event_id"]),)
    )

    result = audit.verify(tenant_id=task.tenant_id, task_id=task.task_id)
    assert result.ok is False
    assert result.reason == REASON_SEQ_GAP
    assert result.broken_at_seq == int(victim["seq"])
    assert result.affected_range == (
        int(victim["seq"]) - 1,
        int(rows[-1]["seq"]),
    )


def test_verify_rejects_cross_tenant_task(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("跨租户校验")
    with pytest.raises(TaskNotFoundError):
        audit.verify(tenant_id="tenant_other", task_id=task.task_id)


@pytest.mark.parametrize(
    "table", ["approval_consumptions", "step_prepared", "step_observed"]
)
def test_append_only_tables_reject_update_and_delete(control_client, table: str) -> None:
    """应用运行账号在数据库层无法改写/删除只追加证据（P0-12/T5）。"""
    with pytest.raises(DatabasePermissionError) as update_error:
        control_client.execute(f"UPDATE dg_system.{table} SET tenant_id = 'x' WHERE 1 = 0")
    assert update_error.value.details.get("mysql_error_code") == 1142

    with pytest.raises(DatabasePermissionError) as delete_error:
        control_client.execute(f"DELETE FROM dg_system.{table} WHERE 1 = 0")
    assert delete_error.value.details.get("mysql_error_code") == 1142


def test_approvals_allows_update_but_not_delete(control_client) -> None:
    """审批是状态表：允许条件 UPDATE，但无 DELETE（历史由 task_events 承担）。"""
    affected = control_client.execute(
        "UPDATE dg_system.approvals SET status = 'PENDING' WHERE 1 = 0"
    )
    assert affected == 0
    with pytest.raises(DatabasePermissionError):
        control_client.execute("DELETE FROM dg_system.approvals WHERE 1 = 0")


def test_task_events_remains_append_only(control_client) -> None:
    with pytest.raises(DatabasePermissionError):
        control_client.execute("UPDATE dg_system.task_events SET payload_json = '{}' WHERE 1 = 0")
    with pytest.raises(DatabasePermissionError):
        control_client.execute("DELETE FROM dg_system.task_events WHERE 1 = 0")


def test_query_filters_by_time_window(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("时间窗查询")
    _append(audit, task, AuditEventType.APPROVAL_EXPIRED, {"approval_id": "apr_time"})

    future = utc_now() + timedelta(seconds=5)
    events, _ = audit.query(tenant_id=task.tenant_id, since=future, limit=10)
    assert events == []
    past = utc_now() - timedelta(minutes=5)
    events, _ = audit.query(tenant_id=task.tenant_id, since=past, limit=10)
    assert events


def test_service_verify_matches_pure_function(task_service, audit, created_tasks) -> None:
    task = task_service.create_task("一致复用")
    _append(audit, task, AuditEventType.APPROVAL_REQUESTED, {"approval_id": "apr_same"})
    chain = audit._load_chain(task.task_id)  # noqa: SLF001 - 校验编排与纯函数必须一致
    assert verify_event_chain(chain).ok is True
    assert audit.verify(tenant_id=task.tenant_id, task_id=task.task_id).checked == len(chain)


def test_created_task_status_is_unchanged_by_audit(task_service, audit, created_tasks) -> None:
    """审计只追加，不改任务状态（避免用审计"顺手"推进业务状态）。"""
    task = task_service.create_task("状态不变")
    _append(audit, task, AuditEventType.AUDIT_WRITE_FAILED, {"approval_id": "apr_state"})
    assert task_service.get_task(task.task_id).status is TaskStatus.PENDING


def test_control_account_cannot_drop_tables(control_client, admin_client) -> None:
    """`dg_ctl` 的 DDL 已移除 DROP：删除表只能走管理员（人工运维）路径。

    用一个临时表做真实探测（CREATE 允许、DROP 必须被拒），避免拿审计表冒险。
    """
    table = "dg_system.w06_drop_probe_tmp"
    admin_client.execute(f"DROP TABLE IF EXISTS {table}")
    try:
        control_client.execute(
            f"CREATE TABLE {table} (probe_id INT PRIMARY KEY) ENGINE=InnoDB"
        )
        with pytest.raises(DatabasePermissionError) as excinfo:
            control_client.execute(f"DROP TABLE {table}")
        assert excinfo.value.details.get("mysql_error_code") == 1142
    finally:
        admin_client.execute(f"DROP TABLE IF EXISTS {table}")
