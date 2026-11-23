"""任务 API：创建、查询、暂停/取消、事件流与工具调用。"""

from __future__ import annotations

import inspect
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.api.deps import get_principal
from core.config import get_settings
from core.errors import PolicyDeniedError, TenantInvalidError
from core.security.authz import (
    ensure_permission,
    ensure_task_access,
    require_owner_control,
)
from core.security.models import EnvironmentContext, Principal, environment_tier
from core.security.policy import PolicyEngine
from core.security.roles import Permission
from core.tasks.models import Task, TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"


class ToolCallRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
    step_id: str | None = None


def _clients() -> tuple[TaskService, MysqlClient, object]:
    settings = get_settings()
    control_client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    ro_client = MysqlClient(
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    registry = build_observer_registry()
    service = TaskService(TaskRepository(control_client), registry)
    return service, ro_client, registry


def _tenant_task(
    task_id: str,
    service: TaskService,
    principal: Principal,
    permission: Permission,
) -> Task:
    task = service.get_task(task_id, tenant_id=principal.tenant_id)
    ensure_task_access(principal, task, permission=permission)
    return task


def _create_task_kwargs(service: TaskService, principal: Principal, environment_id: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "risk_level": "R0",
        "tenant_id": principal.tenant_id,
        "environment_id": environment_id,
        "created_by": principal.user_id,
    }
    # 允许测试中的收窄签名包装器；生产调用仍传递完整归属字段。
    try:
        supported = set(inspect.signature(service.create_task).parameters)
    except (TypeError, ValueError):
        supported = set(kwargs)
    return {key: value for key, value in kwargs.items() if key in supported}


@router.post("")
def create_task(
    request: CreateTaskRequest,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.TASK_CREATE)
    settings = get_settings()
    service, _, _ = _clients()
    environment_id = settings.default_environment_id
    if not service.repository.environment_belongs_to(environment_id, principal.tenant_id):
        raise TenantInvalidError(
            "环境不存在或不属于当前租户",
            details={"environment_id": environment_id},
        )
    kwargs = _create_task_kwargs(service, principal, environment_id)
    kwargs["risk_level"] = request.risk_level
    task = service.create_task(request.goal, **kwargs)
    return task.model_dump(mode="json")


@router.get("")
def list_tasks(
    status: TaskStatus | None = None,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
) -> list[dict[str, Any]]:
    ensure_permission(principal, Permission.TASK_READ)
    service, _, _ = _clients()
    tasks = service.list_tasks(status, limit, tenant_id=principal.tenant_id)
    return [task.model_dump(mode="json") for task in tasks]


@router.get("/{task_id}")
def get_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    service, _, _ = _clients()
    task = _tenant_task(task_id, service, principal, Permission.TASK_READ)
    return task.model_dump(mode="json")


@router.get("/{task_id}/events")
def list_events(
    task_id: str,
    limit: int = 200,
    principal: Principal = Depends(get_principal),
) -> list[dict[str, Any]]:
    service, _, _ = _clients()
    _tenant_task(task_id, service, principal, Permission.TASK_READ)
    return [event.model_dump(mode="json") for event in service.list_events(task_id, limit)]


@router.post("/{task_id}/pause")
def pause_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    service, _, _ = _clients()
    task = _tenant_task(task_id, service, principal, Permission.TASK_CONTROL)
    require_owner_control(principal, task)
    return service.request_pause(task_id).model_dump(mode="json")


@router.post("/{task_id}/resume")
def resume_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    service, _, _ = _clients()
    task = _tenant_task(task_id, service, principal, Permission.TASK_CONTROL)
    require_owner_control(principal, task)
    return service.resume(task_id).model_dump(mode="json")


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    service, _, _ = _clients()
    task = _tenant_task(task_id, service, principal, Permission.TASK_CONTROL)
    require_owner_control(principal, task)
    return service.cancel(task_id).model_dump(mode="json")


@router.post("/{task_id}/tools/{tool_name}")
def call_tool(
    task_id: str,
    tool_name: str,
    request: ToolCallRequest,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    settings = get_settings()
    ensure_permission(principal, Permission.TASK_RUN)
    service, ro_client, registry = _clients()
    task = _tenant_task(task_id, service, principal, Permission.TASK_RUN)

    definition = registry.get(tool_name).definition  # type: ignore[attr-defined]
    environment_row = service.repository.get_environment(
        task.environment_id, tenant_id=principal.tenant_id
    )
    environment = EnvironmentContext(
        environment_id=task.environment_id,
        tenant_id=principal.tenant_id,
        db_engine=definition.db_engines[0] if definition.db_engines else "mysql",
        tier=environment_tier(environment_row.get("status") if environment_row else None),
    )
    decision = PolicyEngine(policy_version=settings.policy_version).decide(
        principal, definition, environment
    )
    if not decision.allowed:
        raise PolicyDeniedError(
            "操作被策略拒绝",
            details=decision.to_public_dict(),
        )

    context = ToolContext(client=ro_client, task_id=task_id)
    result = service.run_tool(
        task_id,
        tool_name,
        request.params,
        context,
        step_id=request.step_id,
        idempotency_key=request.idempotency_key,
    )
    return result.model_dump(mode="json")
