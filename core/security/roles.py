"""RBAC 角色与权限矩阵（W05）。

原则：
- 权限由服务端角色决定，客户端提交的 role/tenant/approver 一律不参与授权；
- Administrator 不自动获得目标库写权限，也不自动获得审批权（职责分离）；
- ServiceWorker 只用于 Gateway 内部执行链，不具备审批/管理权限。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    VIEWER = "Viewer"
    OPERATOR = "Operator"
    APPROVER = "Approver"
    ADMINISTRATOR = "Administrator"
    SERVICE_WORKER = "ServiceWorker"
    AUDITOR = "Auditor"


class Permission(str, Enum):
    TASK_CREATE = "task:create"
    TASK_READ = "task:read"
    TASK_CONTROL = "task:control"
    TASK_RUN = "task:run"
    EVIDENCE_READ = "evidence:read"
    REPORT_READ = "report:read"
    REPORT_WRITE = "report:write"
    ADAPTER_READ = "adapter:read"
    ADAPTER_INVOKE = "adapter:invoke"
    APPROVAL_CREATE = "approval:create"
    APPROVAL_READ = "approval:read"
    APPROVAL_DECIDE = "approval:decide"
    AUDIT_READ = "audit:read"
    ENVIRONMENT_READ = "environment:read"
    ENVIRONMENT_MANAGE = "environment:manage"
    USER_MANAGE = "user:manage"
    WORKER_LEASE = "worker:lease"
    GATEWAY_EXECUTE = "gateway:execute"
    # P1-BYOK：租户模型供应商管理（密钥加密存储、SSRF 校验、激活切换）
    MODEL_PROVIDER_READ = "model_provider:read"
    MODEL_PROVIDER_MANAGE = "model_provider:manage"
    # P3-T3：知识（Wiki）读取与确认/撤销；确认是人工判断，写权限只给管理角色。
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_MANAGE = "knowledge:manage"
    # P5-T1：评测中台（记录实测运行、对比候选、上线前判定）
    EVAL_READ = "eval:read"
    EVAL_MANAGE = "eval:manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(
        {
            Permission.TASK_READ,
            Permission.EVIDENCE_READ,
            Permission.REPORT_READ,
            Permission.ENVIRONMENT_READ,
            Permission.KNOWLEDGE_READ,
            Permission.EVAL_READ,
        }
    ),
    Role.OPERATOR: frozenset(
        {
            Permission.TASK_READ,
            Permission.TASK_CREATE,
            Permission.TASK_CONTROL,
            Permission.TASK_RUN,
            Permission.EVIDENCE_READ,
            Permission.REPORT_READ,
            Permission.REPORT_WRITE,
            Permission.ADAPTER_READ,
            Permission.ADAPTER_INVOKE,
            Permission.APPROVAL_CREATE,
            Permission.ENVIRONMENT_READ,
        }
    ),
    Role.APPROVER: frozenset(
        {
            Permission.TASK_READ,
            Permission.EVIDENCE_READ,
            Permission.REPORT_READ,
            Permission.APPROVAL_READ,
            Permission.APPROVAL_DECIDE,
            Permission.ENVIRONMENT_READ,
            Permission.KNOWLEDGE_READ,
        }
    ),
    Role.ADMINISTRATOR: frozenset(
        {
            Permission.TASK_READ,
            Permission.TASK_CREATE,
            Permission.TASK_CONTROL,
            Permission.TASK_RUN,
            Permission.EVIDENCE_READ,
            Permission.REPORT_READ,
            Permission.REPORT_WRITE,
            Permission.ADAPTER_READ,
            Permission.ADAPTER_INVOKE,
            Permission.APPROVAL_CREATE,
            Permission.APPROVAL_READ,
            Permission.AUDIT_READ,
            Permission.ENVIRONMENT_READ,
            Permission.ENVIRONMENT_MANAGE,
            Permission.USER_MANAGE,
            Permission.MODEL_PROVIDER_READ,
            Permission.MODEL_PROVIDER_MANAGE,
            Permission.KNOWLEDGE_READ,
            Permission.KNOWLEDGE_MANAGE,
            Permission.EVAL_READ,
            Permission.EVAL_MANAGE,
        }
    ),
    Role.SERVICE_WORKER: frozenset(
        {
            Permission.TASK_READ,
            Permission.WORKER_LEASE,
            Permission.GATEWAY_EXECUTE,
        }
    ),
    Role.AUDITOR: frozenset(
        {
            Permission.TASK_READ,
            Permission.EVIDENCE_READ,
            Permission.REPORT_READ,
            Permission.AUDIT_READ,
            Permission.ENVIRONMENT_READ,
            Permission.KNOWLEDGE_READ,
        }
    ),
}


def permissions_for(roles: frozenset[Role] | set[Role] | tuple[Role, ...]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for role in roles:
        granted.update(ROLE_PERMISSIONS.get(role, frozenset()))
    return frozenset(granted)


def role_from_text(value: str) -> Role:
    """严格解析角色名；未知角色直接拒绝，避免静默降权/提权。"""
    try:
        return Role(value)
    except ValueError:
        raise ValueError(f"未知角色：{value}") from None


__all__ = [
    "Permission",
    "ROLE_PERMISSIONS",
    "Role",
    "permissions_for",
    "role_from_text",
]
