"""W05 授权守卫：纯函数，不访问数据库。

约束：
- 权限只来自服务端构造的 Principal，客户端提交的角色/租户不参与授权；
- 跨租户任务按不存在处理，避免泄露任务存在性；
- 错误 details 只放安全字段，不携带凭据或角色全集以外的敏感信息。
"""

from __future__ import annotations

from core.errors import ForbiddenError, TaskNotFoundError
from core.security.models import Principal
from core.security.roles import Permission, Role
from core.tasks.models import Task


def _denied_permission(
    principal: Principal, permission: Permission | str
) -> ForbiddenError:
    value = permission.value if isinstance(permission, Permission) else str(permission)
    return ForbiddenError(
        f"当前身份缺少权限：{value}",
        details={
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "permission": value,
        },
    )


def ensure_permission(principal: Principal, permission: Permission | str) -> None:
    """校验 Principal 是否具备指定权限；缺失时抛出 ForbiddenError。"""
    if isinstance(permission, str):
        try:
            wanted = Permission(permission)
        except ValueError:
            raise _denied_permission(principal, permission) from None
    else:
        wanted = permission

    if not principal.has_permission(wanted):
        raise _denied_permission(principal, wanted)


def ensure_tenant(principal: Principal, tenant_id: str) -> None:
    """校验请求租户与 Principal 租户一致。

    服务层内部错误使用 403；对外 API 在加载任务时应使用 tenant 过滤并转为 404。
    """
    if principal.tenant_id != tenant_id:
        raise ForbiddenError(
            "跨租户访问被拒绝",
            details={
                "requested_tenant": tenant_id,
                "principal_tenant": principal.tenant_id,
            },
        )


def ensure_task_access(
    principal: Principal,
    task: Task,
    *,
    permission: Permission | str = Permission.TASK_READ,
    require_owner: bool = False,
) -> None:
    """任务访问校验：先校验权限，再校验租户隔离与所有者。

    - 跨租户一律抛 TaskNotFoundError，不泄露任务存在性；
    - require_owner=True 时仅任务所有者或 Administrator 可通过。
    """
    ensure_permission(principal, permission)

    if task.tenant_id != principal.tenant_id:
        raise TaskNotFoundError(
            "任务不存在",
            details={"task_id": task.task_id},
        )

    if (
        require_owner
        and task.created_by != principal.user_id
        and not principal.has_role(Role.ADMINISTRATOR)
    ):
        raise ForbiddenError(
            "仅任务所有者可执行该操作",
            details={
                "task_id": task.task_id,
                "user_id": principal.user_id,
            },
        )


def require_owner_control(principal: Principal, task: Task) -> None:
    """任务控制操作的便捷入口：需要 task:control 权限且必须为所有者/管理员。"""
    ensure_task_access(
        principal,
        task,
        permission=Permission.TASK_CONTROL,
        require_owner=True,
    )


__all__ = [
    "ensure_permission",
    "ensure_task_access",
    "ensure_tenant",
    "require_owner_control",
]
