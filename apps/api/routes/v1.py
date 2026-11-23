"""`/v1` API：Principal 任务、事件与 Evidence 基础接口。

授权边界：
- tenant_id 只来自服务端构造的 Principal.tenant_id；
- 请求 DTO 中的 tenant_id 仅做兼容字段接收并记录日志，绝不参与归属/授权；
- 跨租户资源统一返回 404（TASK_NOT_FOUND），不泄露存在性。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import (
    get_evidence_service,
    get_principal,
    get_task_repository,
    get_task_service,
)
from apps.api.errors import get_trace_id
from core.config import get_settings
from core.errors import RequestInvalidError, TenantInvalidError
from core.evidence.service import EvidenceService
from core.logging import get_logger
from core.security.authz import (
    ensure_permission,
    ensure_task_access,
    require_owner_control,
)
from core.security.models import Principal
from core.security.roles import Permission
from core.tasks.models import Task, TaskStatus
from core.tasks.repository import TASKS, TaskRepository
from core.tasks.service import TaskService

router = APIRouter(prefix="/v1", tags=["v1"])
logger = get_logger(__name__)


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"
    environment_id: str | None = Field(default=None, min_length=1, max_length=64)
    deadline_at: datetime | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_tool_calls: int | None = Field(default=None, ge=1, le=50)
    max_tokens: int | None = Field(default=None, ge=0, le=200_000)
    # 兼容字段：客户端提交的 tenant_id 会被忽略，归属永远取 Principal 租户。
    tenant_id: str | None = None


def _require_own_task(
    task_id: str,
    task_service: TaskService,
    principal: Principal,
    permission: Permission = Permission.TASK_READ,
) -> Task:
    # get_task 带 tenant_id 条件；跨租户与不存在都返回 TASK_NOT_FOUND（404）。
    task = task_service.get_task(task_id, tenant_id=principal.tenant_id)
    ensure_task_access(principal, task, permission=permission)
    return task


@router.post("/tasks", response_model=Task)
def create_task(
    body: CreateTaskRequest,
    http_request: Request,
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
    task_repository: TaskRepository = Depends(get_task_repository),
) -> Task:
    ensure_permission(principal, Permission.TASK_CREATE)
    settings = get_settings()
    tenant_id = principal.tenant_id
    if body.tenant_id is not None and body.tenant_id != tenant_id:
        logger.warning(
            "客户端提交的 tenant_id 已忽略，使用 Principal 租户",
            extra={"trace_id": get_trace_id(http_request)},
        )

    environment_id = body.environment_id or settings.default_environment_id
    if not task_repository.environment_belongs_to(environment_id, tenant_id):
        raise TenantInvalidError(
            "环境不存在或不属于当前租户",
            details={"environment_id": environment_id},
        )

    budget: dict[str, Any] = {}
    if body.max_steps is not None:
        budget["max_steps"] = body.max_steps
    if body.max_tool_calls is not None:
        budget["max_tool_calls"] = body.max_tool_calls
    if body.max_tokens is not None:
        budget["max_tokens"] = body.max_tokens

    return task_service.create_task(
        body.goal,
        tenant_id=tenant_id,
        environment_id=environment_id,
        created_by=principal.user_id,
        risk_level=body.risk_level,
        deadline_at=body.deadline_at,
        trace_id=get_trace_id(http_request),
        **budget,
    )


@router.get("/tasks")
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    principal: Principal = Depends(get_principal),
    task_repository: TaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    """按 created_at DESC 的 keyset 分页；cursor 格式 `<created_at>|<task_id>`。"""
    ensure_permission(principal, Permission.TASK_READ)
    tenant_id = principal.tenant_id
    conditions = ["tenant_id = %s"]
    params: list[Any] = [tenant_id]
    if status is not None:
        conditions.append("status = %s")
        params.append(status.value)
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            created_text, task_id = decoded.rsplit("|", 1)
            created_at = datetime.fromisoformat(created_text)
            if created_at.tzinfo is not None:
                created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, UnicodeDecodeError, binascii.Error):
            raise RequestInvalidError("无效的 task 游标", details={"reason": "cursor_invalid"})
        conditions.append("(created_at < %s OR (created_at = %s AND task_id < %s))")
        params.extend([created_at, created_at, task_id])
    params.append(limit + 1)

    rows = task_repository.client.fetch_all(
        f"SELECT * FROM {TASKS} WHERE {' AND '.join(conditions)} "
        "ORDER BY created_at DESC, task_id DESC LIMIT %s",
        tuple(params),
    )
    tasks = [TaskRepository._to_task(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and tasks:
        last = tasks[-1]
        raw_cursor = f"{last.created_at.isoformat()}|{last.task_id}"
        next_cursor = base64.urlsafe_b64encode(raw_cursor.encode("utf-8")).decode("ascii")
    return {
        "items": [task.model_dump(mode="json") for task in tasks],
        "next_cursor": next_cursor,
    }


@router.get("/tasks/{task_id}", response_model=Task)
def get_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    return _require_own_task(task_id, task_service, principal, Permission.TASK_READ)


@router.get("/tasks/{task_id}/events")
def list_task_events(
    task_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """after_seq=0 表示从链头读取；正数表示严格大于该 seq（cursor 语义）。"""
    _require_own_task(task_id, task_service, principal, Permission.TASK_READ)
    events = task_service.list_events(task_id, limit, after_seq=after_seq)
    # next_seq 为本次已读到的最后一个 seq；客户端应把 next_seq 作为下一次 after_seq。
    next_seq = events[-1].seq if events else after_seq
    return {
        "items": [event.model_dump(mode="json") for event in events],
        "next_seq": next_seq,
        "after_seq": after_seq,
    }


@router.get("/tasks/{task_id}/evidence")
def list_task_evidence(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> dict[str, Any]:
    tenant_id = principal.tenant_id
    _require_own_task(task_id, task_service, principal, Permission.EVIDENCE_READ)
    items, next_cursor = evidence_service.list(
        task_id, tenant_id, limit, cursor or None
    )
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/tasks/{task_id}/pause", response_model=Task)
def pause_task(
    task_id: str,
    http_request: Request,
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    task = _require_own_task(task_id, task_service, principal)
    require_owner_control(principal, task)
    return task_service.request_pause(
        task_id, trace_id=get_trace_id(http_request)
    )


@router.post("/tasks/{task_id}/resume", response_model=Task)
def resume_task(
    task_id: str,
    http_request: Request,
    expected_state_version: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
    evidence_service: EvidenceService = Depends(get_evidence_service),
) -> Task:
    tenant_id = principal.tenant_id
    task = _require_own_task(task_id, task_service, principal)
    require_owner_control(principal, task)
    latest = evidence_service.latest_observed_at(task_id, tenant_id)
    if latest is not None:
        observed = datetime.fromisoformat(latest)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if age > get_settings().resume_evidence_max_age_s:
            from core.errors import StateInvalidError

            raise StateInvalidError(
                "证据已超过可恢复时效，需要重新采集",
                details={"task_id": task_id, "evidence_age_seconds": age},
            )
    return task_service.resume(
        task_id,
        expected_state_version=expected_state_version,
        trace_id=get_trace_id(http_request),
    )


@router.post("/tasks/{task_id}/cancel", response_model=Task)
def cancel_task(
    task_id: str,
    http_request: Request,
    principal: Principal = Depends(get_principal),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    task = _require_own_task(task_id, task_service, principal)
    require_owner_control(principal, task)
    return task_service.cancel(task_id, trace_id=get_trace_id(http_request))
