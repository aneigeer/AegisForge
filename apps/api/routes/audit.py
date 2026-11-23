"""审计 API（P0-13/P1）：按任务/审批/时间查询事件，并触发完整性校验。

- 两个端点都要求 `audit:read`；
- 完整性校验返回**同一结构**（`ok`/`checked`/`broken_at_seq`/`affected_range`/`reason`），
  绝不只返回"失败"两个字；
- 链的边界是 `task_id`；跨任务查询只能按时间排序，不是一条可校验的链。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_audit_service, get_principal
from core.audit.service import AuditService
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission

router = APIRouter(prefix="/v1/audit", tags=["audit"])


class IntegrityVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)


@router.get("/events")
def list_audit_events(
    task_id: str | None = None,
    approval_id: str | None = None,
    event_type: list[str] | None = Query(default=None),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = None,
    principal: Principal = Depends(get_principal),
    service: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.AUDIT_READ)
    events, next_cursor = service.query(
        tenant_id=principal.tenant_id,
        task_id=task_id,
        approval_id=approval_id,
        event_types=event_type,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [
            {
                "event_id": event.event_id,
                "task_id": event.task_id,
                "run_id": event.run_id,
                "tool_call_id": event.tool_call_id,
                "event_type": event.event_type,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
                "seq": event.seq,
                "previous_event_hash": event.previous_event_hash,
                "event_hash": event.event_hash,
                "trace_id": event.trace_id,
            }
            for event in events
        ],
        "next_cursor": next_cursor,
        # 明确告知调用方：跨任务查询不是单链，不能链式校验。
        "chain_scope": "per-task" if task_id else "cross-task(by-created_at)",
    }


@router.post("/integrity/verify")
def verify_integrity(
    request: IntegrityVerifyRequest,
    principal: Principal = Depends(get_principal),
    service: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.AUDIT_READ)
    result = service.verify(tenant_id=principal.tenant_id, task_id=request.task_id)
    payload = result.to_public_dict()
    payload["task_id"] = request.task_id
    return payload


__all__ = ["router"]
