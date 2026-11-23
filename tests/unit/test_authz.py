"""W05 授权守卫单元测试：权限矩阵、租户隔离、所有者控制。"""

from __future__ import annotations

import pytest

from core.errors import ErrorCode, ForbiddenError, TaskNotFoundError
from core.security.authz import (
    ensure_permission,
    ensure_task_access,
    ensure_tenant,
    require_owner_control,
)
from core.security.models import Principal
from core.security.roles import Permission, Role
from core.tasks.models import Task, utc_now


def make_principal(
    role: Role,
    *,
    user_id: str = "user_owner",
    tenant_id: str = "tenant_a",
) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(role,))


def make_task(
    *,
    tenant_id: str = "tenant_a",
    created_by: str = "user_owner",
) -> Task:
    now = utc_now()
    return Task(
        task_id="task_1",
        run_id="run_1",
        goal="排查慢查询",
        tenant_id=tenant_id,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 权限矩阵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [Role.VIEWER, Role.AUDITOR, Role.APPROVER],
    ids=lambda role: role.value,
)
def test_read_only_roles_lack_adapter_invoke(role: Role) -> None:
    principal = make_principal(role)
    assert principal.has_permission(Permission.ADAPTER_INVOKE) is False

    with pytest.raises(ForbiddenError) as exc_info:
        ensure_permission(principal, Permission.ADAPTER_INVOKE)

    error = exc_info.value
    assert error.code is ErrorCode.FORBIDDEN
    assert error.code.value == "FORBIDDEN"
    assert error.details == {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "permission": "adapter:invoke",
    }


@pytest.mark.parametrize(
    "role",
    [Role.OPERATOR, Role.ADMINISTRATOR],
    ids=lambda role: role.value,
)
def test_operator_and_administrator_have_adapter_invoke(role: Role) -> None:
    principal = make_principal(role)
    assert principal.has_permission(Permission.ADAPTER_INVOKE) is True
    assert ensure_permission(principal, Permission.ADAPTER_INVOKE) is None


def test_administrator_has_no_approval_decide_permission() -> None:
    admin = make_principal(Role.ADMINISTRATOR)
    assert admin.has_permission(Permission.APPROVAL_DECIDE) is False

    with pytest.raises(ForbiddenError) as exc_info:
        ensure_permission(admin, Permission.APPROVAL_DECIDE)

    assert exc_info.value.code is ErrorCode.FORBIDDEN
    assert exc_info.value.details == {
        "user_id": admin.user_id,
        "tenant_id": admin.tenant_id,
        "permission": "approval:decide",
    }


def test_viewer_can_read_tasks() -> None:
    viewer = make_principal(Role.VIEWER)
    assert ensure_permission(viewer, Permission.TASK_READ) is None


# ---------------------------------------------------------------------------
# 租户隔离
# ---------------------------------------------------------------------------


def test_ensure_tenant_rejects_mismatch_with_safe_details() -> None:
    principal = make_principal(Role.OPERATOR, tenant_id="tenant_a")

    with pytest.raises(ForbiddenError) as exc_info:
        ensure_tenant(principal, "tenant_b")

    assert exc_info.value.code is ErrorCode.FORBIDDEN
    assert exc_info.value.details == {
        "requested_tenant": "tenant_b",
        "principal_tenant": "tenant_a",
    }


def test_ensure_tenant_accepts_same_tenant() -> None:
    principal = make_principal(Role.OPERATOR, tenant_id="tenant_a")
    assert ensure_tenant(principal, "tenant_a") is None


def test_cross_tenant_task_is_hidden_as_not_found() -> None:
    principal = make_principal(Role.OPERATOR, tenant_id="tenant_a")
    task = make_task(tenant_id="tenant_b")

    with pytest.raises(TaskNotFoundError) as exc_info:
        ensure_task_access(principal, task)

    error = exc_info.value
    assert error.code is ErrorCode.TASK_NOT_FOUND
    assert error.code.value == "TASK_NOT_FOUND"
    assert error.status_code == 404


def test_cross_tenant_task_not_found_even_for_administrator() -> None:
    admin = make_principal(Role.ADMINISTRATOR, tenant_id="tenant_a")
    task = make_task(tenant_id="tenant_b")

    with pytest.raises(TaskNotFoundError):
        ensure_task_access(admin, task, permission=Permission.TASK_CONTROL)


# ---------------------------------------------------------------------------
# 所有者控制
# ---------------------------------------------------------------------------


def test_task_owner_can_control_own_task() -> None:
    principal = make_principal(Role.OPERATOR, user_id="user_owner")
    task = make_task(created_by="user_owner")
    assert require_owner_control(principal, task) is None


def test_non_owner_operator_cannot_control_task() -> None:
    principal = make_principal(Role.OPERATOR, user_id="user_other")
    task = make_task(created_by="user_owner")

    with pytest.raises(ForbiddenError) as exc_info:
        require_owner_control(principal, task)

    assert exc_info.value.code is ErrorCode.FORBIDDEN
    assert exc_info.value.details["task_id"] == "task_1"


def test_administrator_can_control_task_without_ownership() -> None:
    admin = make_principal(Role.ADMINISTRATOR, user_id="user_admin")
    task = make_task(created_by="user_owner")
    assert require_owner_control(admin, task) is None


def test_approver_can_read_task_but_cannot_control_it() -> None:
    approver = make_principal(Role.APPROVER, user_id="user_approver")
    task = make_task(created_by="user_owner")

    assert (
        ensure_task_access(
            approver,
            task,
            permission=Permission.TASK_READ,
        )
        is None
    )

    with pytest.raises(ForbiddenError):
        require_owner_control(approver, task)
