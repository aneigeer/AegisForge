"""审批领域层集成：创建 → 裁决 → 九项校验 → 原子消费（P0-04/P0-05/P0-06/P0-09）。

本文件只验证 `ApprovalService` 自身（不经过 Gateway/HTTP），因此刻意使用
仅存在于测试代码里的桩工具（`side_effect=write` + 已声明 `target_objects`），
真实写工具的 `target_objects` 抽取属 W07。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.approval.models import ApprovalStatus
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.config import Settings
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalDigestMismatchError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ForbiddenError,
    RequestInvalidError,
    StateConflictError,
)
from core.security.canonical import CanonicalizationError
from core.security.models import Principal
from core.security.roles import Role
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from core.tools.registry import ToolRegistry
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

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

TARGETS = [
    {
        "object_type": "table",
        "database": "dg_lab",
        "schema": None,
        "name": "orders",
        "keys": ["id=1001"],
    }
]
# 目标对象由 extract_target_objects 从参数抽取，因此参数里必须带齐声明字段。
PARAMS = {
    "database": "dg_lab",
    "table": "orders",
    "keys": ["id=1001"],
    "id": 1001,
    "status": "PAID",
}


def _definition(version: str = "1.0") -> ToolDefinition:
    return ToolDefinition(
        name="mysql_update_rows",
        description="测试桩：更新订单状态",
        version=version,
        risk_level="R2",
        side_effect="write",
        idempotent=False,
        operation_class="repair",
        permissions=["mysql:write"],
        required_role="dg_rw",
        # P0-03b：写工具声明目标对象抽取规则；创建与消费两侧都用它重算。
        target_objects=[
            TargetObjectSpec(
                object_type="table",
                database_field="database",
                name_field="table",
                keys_field="keys",
            )
        ],
        input_schema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "table": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "id": {"type": "integer"},
                "status": {"type": "string"},
                "expected_rows": {"type": "integer", "default": 1},
            },
            "required": ["database", "table", "keys", "id", "status"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )


class _StubTool:
    """仅测试代码使用的桩工具：不进入生产注册表。"""

    def __init__(self, version: str = "1.0") -> None:
        self.definition = _definition(version)

    def run(self, context, params):  # pragma: no cover - 本文件不执行工具
        raise AssertionError("集成用例不应执行桩工具")


@pytest.fixture()
def created_tasks(admin_client, monkeypatch):
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
def registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_StubTool())
    return registry


@pytest.fixture()
def task_repository(control_client) -> TaskRepository:
    return TaskRepository(control_client)


@pytest.fixture()
def audit(control_client) -> AuditService:
    return AuditService(TaskRepository(control_client))


@pytest.fixture()
def service(control_client, task_repository, audit, registry) -> ApprovalService:
    return ApprovalService(
        ApprovalRepository(control_client),
        task_repository,
        PrerequisiteProbe(task_repository),
        audit,
        settings=pytest.importorskip("core.config").Settings(),
        registry=registry,
    )


@pytest.fixture()
def optional_spec_service(control_client, task_repository, audit) -> ApprovalService:
    """写工具只声明**可选**抽取规则的服务：用于验证"抽不出目标对象即拒绝"。"""
    definition = ToolDefinition(
        name="mysql_update_rows",
        description="测试桩：可选目标对象",
        version="1.0",
        risk_level="R2",
        side_effect="write",
        idempotent=False,
        operation_class="repair",
        permissions=["mysql:write"],
        required_role="dg_rw",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        target_objects=[
            TargetObjectSpec(
                object_type="table",
                database_field="database",
                name_field="table",
                keys_field="keys",
                required=False,
            )
        ],
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )

    class _OptionalStub:
        def __init__(self) -> None:
            self.definition = definition

        def run(self, context, params):  # pragma: no cover - 用例不执行工具
            raise AssertionError("用例不应执行工具")

    registry = ToolRegistry()
    registry.register(_OptionalStub())
    return ApprovalService(
        ApprovalRepository(control_client),
        task_repository,
        PrerequisiteProbe(task_repository),
        audit,
        settings=pytest.importorskip("core.config").Settings(),
        registry=registry,
    )


@pytest.fixture()
def task(task_repository, created_tasks):
    service = TaskService(task_repository, ToolRegistry())
    created = service.create_task("审批集成用例")
    service.transition(created.task_id, TaskStatus.PLANNING)
    return task_repository.get_task(created.task_id)


@pytest.fixture()
def operator() -> Principal:
    return Principal(
        user_id="user_operator",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR,),
        session_id="sess_operator",
    )


@pytest.fixture()
def approver() -> Principal:
    return Principal(
        user_id="user_approver",
        tenant_id="tenant_local",
        roles=(Role.APPROVER,),
        session_id="sess_approver",
    )


@pytest.fixture()
def worker() -> Principal:
    return Principal(
        user_id="user_worker",
        tenant_id="tenant_local",
        roles=(Role.SERVICE_WORKER,),
        session_id="sess_worker",
    )


def _create(service: ApprovalService, task, principal: Principal, **overrides):
    kwargs = {
        "principal": principal,
        "task_id": task.task_id,
        "step_id": None,
        "tool_name": "mysql_update_rows",
        "params": dict(PARAMS),
        "target_objects": [dict(TARGETS[0])],
    }
    kwargs.update(overrides)
    return service.create(**kwargs)


def _approved(service: ApprovalService, task, operator, approver, **overrides):
    detail = _create(service, task, operator, **overrides)
    return service.approve(detail.approval_id, principal=approver, reason="LGTM")


def _consume(service, task, worker, approval_id, *, definition=None, params=None, env=None, policy_version=None):
    return service.consume(
        approval_id=approval_id,
        principal=worker,
        task=task,
        definition=definition or _definition(),
        params=PARAMS if params is None else params,
        environment_row=env or _env_row(),
        # 与控制库登记值的唯一来源一致：settings.policy_version
        policy_version=policy_version or Settings().policy_version,
        envelope_id="env_test",
        tool_call_id="call_test",
    )


def _env_row(**overrides):
    row = {
        "environment_id": "env_mysql_local",
        "tenant_id": "tenant_local",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "network_id": "lab_net",
        "image_digest": None,
        "config_version": "w03-local",
        "status": "LAB",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 正常链路
# ---------------------------------------------------------------------------


def test_create_returns_full_disclosure(service, task, operator) -> None:
    detail = _create(service, task, operator, operator_note="仅测试")
    assert detail.status is ApprovalStatus.PENDING
    assert detail.action_digest and detail.parameters_digest and detail.target_objects_digest
    assert detail.prerequisites_digest
    assert detail.environment_fingerprint["db_engine"] == "mysql"
    assert detail.target_objects[0]["name"] == "orders"
    assert detail.normalized_operation["side_effect"] == "write"
    assert detail.missing_required_fields() == []
    assert detail.remaining_seconds > 0
    # 操作备注不参与摘要
    other = _create(service, task, operator, operator_note="完全不同的备注")
    assert other.action_digest == detail.action_digest


def test_full_lifecycle_consume_once(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    grant = _consume(service, task, worker, detail.approval_id)

    assert grant.approval_id == detail.approval_id
    assert grant.action_digest == detail.action_digest
    assert grant.consumed_by == "user_worker"
    assert service.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.CONSUMED

    with pytest.raises(ApprovalAlreadyConsumedError):
        _consume(service, task, worker, detail.approval_id)


def test_pending_approval_cannot_be_consumed(service, task, operator, worker) -> None:
    detail = _create(service, task, operator)
    with pytest.raises(Exception) as excinfo:
        _consume(service, task, worker, detail.approval_id)
    assert excinfo.value.code.value == "APPROVAL_NOT_APPROVED"


def test_unknown_approval_is_not_found(service, task, worker) -> None:
    with pytest.raises(ApprovalNotFoundError):
        _consume(service, task, worker, "apr_missing")


def test_cross_tenant_approval_lookup_is_not_found(service, task, operator) -> None:
    detail = _create(service, task, operator)
    other = Principal(
        user_id="user_other",
        tenant_id="tenant_other",
        roles=(Role.APPROVER,),
    )
    with pytest.raises(ApprovalNotFoundError):
        service.get(detail.approval_id, tenant_id=other.tenant_id)


# ---------------------------------------------------------------------------
# 职责分离与权限
# ---------------------------------------------------------------------------


def test_requester_cannot_approve_own_request(service, task) -> None:
    both = Principal(
        user_id="user_both",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR, Role.APPROVER),
        session_id="sess_both",
    )
    detail = _create(service, task, both)
    with pytest.raises(ForbiddenError) as excinfo:
        service.approve(detail.approval_id, principal=both)
    assert excinfo.value.details["reason"] == "SEGREGATION_OF_DUTY"


def test_segregation_violation_is_audited(service, task, audit) -> None:
    both = Principal(
        user_id="user_both",
        tenant_id="tenant_local",
        roles=(Role.OPERATOR, Role.APPROVER),
    )
    detail = _create(service, task, both)
    with pytest.raises(ForbiddenError):
        service.approve(detail.approval_id, principal=both)

    events, _ = audit.query(
        tenant_id="tenant_local",
        task_id=task.task_id,
        event_types=["POLICY_DECISION"],
        limit=10,
    )
    assert len(events) == 1
    assert events[0].payload["reason"] == "SEGREGATION_OF_DUTY"
    assert events[0].payload["deny_code"] == "FORBIDDEN"
    # 审批状态未被改动
    assert service.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.PENDING


def test_approver_without_decide_permission_is_forbidden(service, task, operator) -> None:
    detail = _create(service, task, operator)
    auditor = Principal(
        user_id="user_auditor",
        tenant_id="tenant_local",
        roles=(Role.AUDITOR,),
    )
    with pytest.raises(ForbiddenError):
        service.approve(detail.approval_id, principal=auditor)


def test_operator_without_create_permission_is_forbidden(service, task) -> None:
    viewer = Principal(user_id="user_viewer", tenant_id="tenant_local", roles=(Role.VIEWER,))
    with pytest.raises(ForbiddenError):
        _create(service, task, viewer)


# ---------------------------------------------------------------------------
# 六类失效：每次都必须指出具体 channel
# ---------------------------------------------------------------------------


def test_parameters_change_is_located(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(
            service,
            task,
            worker,
            detail.approval_id,
            params={**PARAMS, "status": "REFUNDED"},
        )
    assert excinfo.value.details["channel"] == "parameters"


def test_snapshot_tamper_is_located(
    service, control_client, task, operator, approver, worker
) -> None:
    """验证"库内快照被改能被发现"（不是"抽取规则变更"用例）。

    具备 UPDATE 授权的账号改写 `action_descriptor_json` 里的目标对象，
    消费时重算的目标对象与快照不符 → `channel=target_objects`。
    "抽取规则原地漂移"由 `test_target_object_rule_change_is_reported_as_target_objects_channel`
    （tests/integration/test_approval_consume.py）单独覆盖。
    """
    detail = _approved(service, task, operator, approver)
    control_client.execute(
        "UPDATE dg_system.approvals SET action_descriptor_json = JSON_SET("
        "action_descriptor_json, '$.target_objects[0].name', 'orders_archive') "
        "WHERE approval_id = %s",
        (detail.approval_id,),
    )
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(service, task, worker, detail.approval_id)
    # 快照被改 → 展示全文无法复算出登记的 action_digest → channel=action
    assert excinfo.value.details["channel"] == "action"
    assert excinfo.value.details["reason"] == "DESCRIPTOR_DIGEST_MISMATCH"


def test_environment_change_is_located(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    other_task = task.model_copy(update={"environment_id": "env_pg_local"})
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(service, other_task, worker, detail.approval_id, env=_env_row(db_engine="postgresql"))
    assert excinfo.value.details["channel"] == "environment"


def test_environment_fingerprint_change_is_located(
    service, task, operator, approver, worker
) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(
            service,
            task,
            worker,
            detail.approval_id,
            env=_env_row(config_version="w06-local"),
        )
    assert excinfo.value.details["channel"] == "environment_fingerprint"


def test_policy_version_change_is_located(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(service, task, worker, detail.approval_id, policy_version="w06-v2")
    assert excinfo.value.details["channel"] == "policy_version"


def test_tool_version_change_is_located(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(service, task, worker, detail.approval_id, definition=_definition("1.1"))
    assert excinfo.value.details["channel"] == "tool_version"


def test_prerequisites_tamper_is_located(
    service, control_client, task, operator, approver, worker
) -> None:
    detail = _approved(service, task, operator, approver)
    control_client.execute(
        "UPDATE dg_system.approvals SET prerequisites_digest = %s WHERE approval_id = %s",
        ("0" * 64, detail.approval_id),
    )
    with pytest.raises(ApprovalDigestMismatchError) as excinfo:
        _consume(service, task, worker, detail.approval_id)
    assert excinfo.value.details["channel"] == "prerequisites"


def test_digest_mismatch_does_not_consume(
    service, task, operator, approver, worker
) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(ApprovalDigestMismatchError):
        _consume(service, task, worker, detail.approval_id, params={**PARAMS, "id": 2, "keys": ["id=2"]})
    # 拒绝不是"消费失败即作废"：审批仍为 APPROVED，可用正确参数消费一次
    assert service.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.APPROVED
    _consume(service, task, worker, detail.approval_id)


# ---------------------------------------------------------------------------
# 过期、撤销、拒绝
# ---------------------------------------------------------------------------


def test_expired_approval_is_rejected_and_marked(
    service, control_client, task, operator, approver, worker, audit
) -> None:
    detail = _approved(service, task, operator, approver)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    control_client.execute(
        "UPDATE dg_system.approvals SET expires_at = %s WHERE approval_id = %s",
        (past.replace(tzinfo=None), detail.approval_id),
    )
    with pytest.raises(ApprovalExpiredError):
        _consume(service, task, worker, detail.approval_id)
    assert service.get(detail.approval_id, tenant_id="tenant_local").status is ApprovalStatus.EXPIRED

    events, _ = audit.query(
        tenant_id="tenant_local",
        task_id=task.task_id,
        event_types=["APPROVAL_EXPIRED"],
        limit=10,
    )
    assert events


def test_revoke_blocks_consumption(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    revoked = service.revoke(detail.approval_id, principal=operator, reason="改需求")
    assert revoked.status is ApprovalStatus.REVOKED
    with pytest.raises(Exception) as excinfo:
        _consume(service, task, worker, detail.approval_id)
    assert excinfo.value.code.value == "APPROVAL_REVOKED"


def test_consumed_approval_cannot_be_revoked(service, task, operator, approver, worker) -> None:
    detail = _approved(service, task, operator, approver)
    _consume(service, task, worker, detail.approval_id)
    with pytest.raises(StateConflictError):
        service.revoke(detail.approval_id, principal=operator, reason="太晚了")


def test_reject_requires_reason_and_blocks_consumption(
    service, task, operator, approver, worker
) -> None:
    detail = _create(service, task, operator)
    with pytest.raises(RequestInvalidError):
        service.reject(detail.approval_id, principal=approver, reason="   ")
    rejected = service.reject(detail.approval_id, principal=approver, reason="影响面不明确")
    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.decision_reason == "影响面不明确"
    with pytest.raises(Exception) as excinfo:
        _consume(service, task, worker, detail.approval_id)
    assert excinfo.value.code.value == "APPROVAL_REJECTED"


def test_illegal_transition_reports_state_conflict(
    service, task, operator, approver
) -> None:
    detail = _approved(service, task, operator, approver)
    with pytest.raises(StateConflictError):
        service.approve(detail.approval_id, principal=approver)


def test_expire_due_marks_pending_and_approved(
    service, control_client, task, operator, approver
) -> None:
    pending = _create(service, task, operator)
    approved = _approved(service, task, operator, approver)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)
    for approval_id in (pending.approval_id, approved.approval_id):
        control_client.execute(
            "UPDATE dg_system.approvals SET expires_at = %s WHERE approval_id = %s",
            (past, approval_id),
        )
    assert service.expire_due(limit=10) >= 2
    assert service.get(pending.approval_id, tenant_id="tenant_local").status is ApprovalStatus.EXPIRED
    assert service.get(approved.approval_id, tenant_id="tenant_local").status is ApprovalStatus.EXPIRED


# ---------------------------------------------------------------------------
# 创建期门禁
# ---------------------------------------------------------------------------


def test_write_tool_without_extractable_target_objects_is_rejected(
    optional_spec_service, task, operator
) -> None:
    """写工具声明了可选抽取规则但参数里没有目标 → 不得建立审批（P0-03b）。"""
    with pytest.raises(Exception) as excinfo:
        _create(optional_spec_service, task, operator, target_objects=[], params={"limit": 1})
    assert excinfo.value.code.value == "APPROVAL_PREREQUISITE_FAILED"


def test_illegal_parameters_are_rejected(service, task, operator) -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        _create(service, task, operator, params={k: PARAMS[k] for k in ("database", "table", "keys", "id")})
    assert excinfo.value.code.value == "REQUEST_INVALID"


def test_ttl_above_maximum_is_rejected(service, task, operator) -> None:
    with pytest.raises(RequestInvalidError):
        _create(service, task, operator, ttl_s=10_000)


def test_unknown_tool_is_rejected(service, task, operator) -> None:
    with pytest.raises(Exception) as excinfo:
        _create(service, task, operator, tool_name="mysql_drop_table")
    assert excinfo.value.code.value == "TOOL_NOT_FOUND"


def test_list_filters_by_status_and_task(service, task, operator, approver) -> None:
    first = _create(service, task, operator)
    _approved(service, task, operator, approver)
    pending_details, _ = service.list(
        tenant_id="tenant_local", status=ApprovalStatus.PENDING, task_id=task.task_id, limit=10
    )
    assert [item.approval_id for item in pending_details] == [first.approval_id]


def test_approve_events_are_append_only_chain_entries(
    service, task, operator, approver, audit
) -> None:
    detail = _approved(service, task, operator, approver)
    events, _ = audit.query(
        tenant_id="tenant_local",
        task_id=task.task_id,
        event_types=["APPROVAL_REQUESTED", "APPROVAL_APPROVED"],
        limit=10,
    )
    assert [event.event_type for event in events] == ["APPROVAL_REQUESTED", "APPROVAL_APPROVED"]
    assert events[0].payload["approval_id"] == detail.approval_id
    assert events[1].payload["from"] == "PENDING"
    assert events[1].payload["to"] == "APPROVED"
    assert audit.verify(tenant_id="tenant_local", task_id=task.task_id).ok is True
