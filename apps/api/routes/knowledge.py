"""P3-T3：知识（Wiki）工作台的读取与人工确认 API。

设计边界：

- **确认人是服务端主体**：`confirmed_by` 只取 `Principal.user_id`，请求体里不接受；
  因此"同一人包办两级"在 API 层就不可能伪造（服务层再拒一次）。
- **阶段 2 需要复核角色**：只有 `Approver` 或 `Administrator` 能执行 stage-2，
  把 P3-T1 里"只校验不同主体"的口径补上角色维度。
- **租户隔离**：一律按 `principal.tenant_id` 过滤，跨租户 404（不泄露存在性）。
- **不提供写内容接口**：内容只能由 `WikiCompiler` 从诊断沉淀，API 不能凭空造知识。

RBAC：读 `knowledge:read`，确认/晋升/撤销 `knowledge:manage`。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_control_client, get_principal
from core.approval.repository import ApprovalRepository  # noqa: F401  (保持依赖显式)
from core.audit.service import AuditService
from core.errors import KnowledgeNotFoundError, PolicyDeniedError
from core.memory.models import KnowledgeEntry, ValidationLevel
from core.memory.repository import KnowledgeRepository
from core.memory.service import MemoryService
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission, Role
from core.tasks.repository import TaskRepository

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal[1, 2]


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=200)


def _service(control_client: Any) -> MemoryService:
    repository = TaskRepository(control_client)
    return MemoryService(
        KnowledgeRepository(control_client),
        gate=RedactionGate(DataRedactionEngine()),
        audit=AuditService(repository),
    )


def _summary(entry: KnowledgeEntry) -> dict[str, Any]:
    return {
        "knowledge_id": entry.knowledge_id,
        "revision": entry.revision,
        "kind": entry.kind.value,
        "status": entry.status.value,
        "validation_level": entry.validation_level.value,
        "db_engine": entry.db_engine,
        "version_min": entry.version_min,
        "version_max": entry.version_max,
        "effective_from": entry.effective_from,
        "effective_until": entry.effective_until,
        "reviewed_by": entry.reviewed_by,
        "reviewed_at": entry.reviewed_at,
        "confirmations": [
            {
                "stage": item.stage,
                "confirmed_by": item.confirmed_by,
                "confirmed_at": item.confirmed_at,
            }
            for item in entry.confirmations
        ],
        "confirmer_ids": sorted(entry.confirmer_ids),
        "is_multi_confirmed": entry.is_multi_confirmed,
        "is_indexable": entry.is_indexable,
        "content": entry.content,
    }


def _latest(service: MemoryService, tenant_id: str, knowledge_id: str) -> KnowledgeEntry:
    entry = service.repository.latest(knowledge_id, tenant_id=tenant_id)
    if entry is None:
        # 不区分"不存在"与"他人租户"：统一 404，避免侧信道。
        raise KnowledgeNotFoundError("知识条目不存在", details={"knowledge_id": knowledge_id})
    return entry


@router.get("", response_model=list)
def list_knowledge(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> list[dict]:
    ensure_permission(principal, Permission.KNOWLEDGE_READ)
    service = _service(control_client)
    latest: dict[str, KnowledgeEntry] = {}
    for entry in service.repository.list_by_tenant(principal.tenant_id, limit=1000):
        current = latest.get(entry.knowledge_id)
        if current is None or entry.revision > current.revision:
            latest[entry.knowledge_id] = entry
    items = sorted(latest.values(), key=lambda item: (item.knowledge_id, item.revision))
    if status:
        items = [item for item in items if item.status.value == status]
    return [_summary(entry) for entry in items[:limit]]


@router.get("/{knowledge_id}", response_model=dict)
def get_knowledge(
    knowledge_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.KNOWLEDGE_READ)
    service = _service(control_client)
    return _summary(_latest(service, principal.tenant_id, knowledge_id))


@router.post("/{knowledge_id}/confirm", response_model=dict)
def confirm_knowledge(
    knowledge_id: str,
    body: ConfirmBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.KNOWLEDGE_MANAGE)
    if body.stage == 2:
        # 阶段 2 是"复核级"动作：只有 Approver/Administrator 可做（角色维度补强）。
        if not {Role.APPROVER, Role.ADMINISTRATOR} & set(principal.roles or ()):
            raise PolicyDeniedError(
                "阶段 2 确认需要 Approver 或 Administrator 角色",
                details={
                    "permission": Permission.KNOWLEDGE_MANAGE.value,
                    "roles": [role.value for role in (principal.roles or ())],
                },
            )
    service = _service(control_client)
    base = _latest(service, principal.tenant_id, knowledge_id)
    updated = service.confirm(
        knowledge_id,
        base.revision,
        stage=body.stage,
        confirmed_by=principal.user_id,
        principal=principal,
    )
    return _summary(updated)


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 晋升时显式声明验证等级（不能沿用 UNVERIFIED，否则 review 会拒绝）。
    validation_level: Literal["SINGLE_CASE", "REGRESSION", "FIELD"] = "SINGLE_CASE"


@router.post("/{knowledge_id}/review", response_model=dict)
def review_knowledge(
    knowledge_id: str,
    body: ReviewBody | None = None,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.KNOWLEDGE_MANAGE)
    service = _service(control_client)
    base = _latest(service, principal.tenant_id, knowledge_id)
    level = ValidationLevel((body or ReviewBody()).validation_level)
    indexed = service.review(
        knowledge_id,
        base.revision,
        reviewed_by=principal.user_id,
        validation_level=level,
    )
    return _summary(indexed)


@router.post("/{knowledge_id}/revoke", response_model=dict)
def revoke_knowledge(
    knowledge_id: str,
    body: RevokeBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.KNOWLEDGE_MANAGE)
    service = _service(control_client)
    base = _latest(service, principal.tenant_id, knowledge_id)
    revoked = service.revoke(
        knowledge_id,
        base.revision,
        reason=body.reason,
        principal=principal,
    )
    return _summary(revoked)


__all__ = ["router"]
