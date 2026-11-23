"""Agent 运行 API：创建任务后触发一次有预算的只读诊断循环。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.api.deps import get_principal
from core.agent.budget import AgentBudget
from core.agent.loop import AgentLoop
from core.agent.planner import Planner
from core.agent.trajectory import TrajectoryStore
from core.audit.service import AuditService
from core.config import get_settings
from core.context.builder import ContextBuilder
from core.models.degradation import DegradationController
from core.models.factory import build_model_gateway
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.security.authz import ensure_permission, ensure_task_access
from core.security.models import Principal
from core.security.roles import Permission
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry

router = APIRouter(prefix="/tasks", tags=["agent"])


class RunAgentRequest(BaseModel):
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_tool_calls: int | None = Field(default=None, ge=1, le=50)
    max_model_calls: int | None = Field(default=None, ge=1, le=100)
    max_tokens: int | None = Field(default=None, ge=0)
    max_seconds: float | None = Field(default=None, gt=0, le=3600)


def _build_loop(
    settings, request: RunAgentRequest, *, tenant_id: str | None = None
) -> AgentLoop:
    control_client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    base_registry = build_observer_registry()
    repository = TaskRepository(control_client)
    execution_mode = getattr(settings, "agent_execution_mode", "inline")
    if execution_mode == "remote_worker":
        from core.execution.dispatch_queue import (
            ReadonlyDispatchQueue,
            build_remote_readonly_registry,
        )

        queue = ReadonlyDispatchQueue(control_client)
        registry = build_remote_readonly_registry(
            queue,
            base_registry,
            tenant_id=tenant_id or settings.default_tenant_id,
            engine="mysql",
            requested_by="api",
            timeout_s=float(getattr(settings, "agent_dispatch_timeout_s", 30.0)),
        )
        context_factory = lambda task_id: ToolContext(client=None, task_id=task_id)
    else:
        ro_client = MysqlClient(
            settings.ro_dsn,
            database_id=settings.database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
        registry = base_registry
        context_factory = lambda task_id: ToolContext(client=ro_client, task_id=task_id)

    service = TaskService(repository, registry)
    # P3-T2：注入"已验证知识"加载器（只读引用；引擎/版本取服务端环境事实）。
    from core.memory.repository import KnowledgeRepository
    from core.memory.wiring import build_knowledge_loader

    knowledge_repo = KnowledgeRepository(control_client)

    def _engine_of(task: Any) -> tuple[str, str] | None:
        row = repository.get_environment(
            task.environment_id, tenant_id=task.tenant_id
        )
        if not row:
            return None
        return str(row.get("db_engine") or ""), str(row.get("db_version") or "")

    knowledge_loader = build_knowledge_loader(
        knowledge_repo.list_by_tenant, engine_of=_engine_of
    )

    # P1-BYOK-T1：优先用租户 active 供应商（密钥在仓库层解密）；未配置则回落全局网关。
    if tenant_id:
        from core.models.provider_registry import resolve_tenant_gateway

        gateway, _provider = resolve_tenant_gateway(settings, control_client, tenant_id)
    else:
        gateway = build_model_gateway(settings)
    # W09 T05：显式装配降级控制器与脱敏闸门（`provider=disabled` 时 run 直接返回
    # 规则简报；不加任何端点，只改本装配函数）。
    degradation = DegradationController.from_settings(
        settings, audit=AuditService(repository)
    )
    redaction_gate = RedactionGate(DataRedactionEngine())
    budget = AgentBudget(
        max_steps=request.max_steps or settings.agent_max_steps,
        max_tool_calls=request.max_tool_calls or settings.agent_max_tool_calls,
        max_model_calls=request.max_model_calls or settings.agent_max_model_calls,
        max_tokens=(
            settings.agent_max_tokens if request.max_tokens is None else request.max_tokens
        ),
        max_seconds=request.max_seconds or settings.agent_max_seconds,
    )
    planner = Planner(
        gateway,
        registry,
        max_steps=budget.max_steps,
        max_tool_calls=budget.max_tool_calls,
    )
    return AgentLoop(
        service,
        registry,
        gateway,
        planner,
        ContextBuilder(registry),
        context_factory=context_factory,
        budget=budget,
        trajectory_store=TrajectoryStore(settings.trajectory_dir),
        max_duplicate_calls=settings.agent_max_duplicate_calls,
        no_progress_threshold=settings.agent_no_progress_threshold,
        degradation=degradation,
        redaction_gate=redaction_gate,
        knowledge_loader=knowledge_loader,
    )


@router.post("/{task_id}/run")
def run_agent(
    task_id: str,
    principal: Principal = Depends(get_principal),
    request: RunAgentRequest | None = None,
) -> dict[str, Any]:
    ensure_permission(principal, Permission.TASK_RUN)
    settings = get_settings()
    control_client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    repository = TaskRepository(control_client)
    service = TaskService(repository, build_observer_registry())
    task = service.get_task(task_id, tenant_id=principal.tenant_id)
    ensure_task_access(principal, task, permission=Permission.TASK_RUN)

    loop = _build_loop(
        settings, request or RunAgentRequest(), tenant_id=principal.tenant_id
    )
    result = loop.run(task_id)
    # P3-T2：任务后沉淀为 PROPOSED 草稿（默认关闭；绝不自动晋升 INDEXED，
    # 落库必经 MemoryService.record → RedactionGate）。
    if bool(getattr(settings, "memory_auto_draft", False)):
        try:
            from core.memory.repository import KnowledgeRepository
            from core.memory.service import MemoryService
            from core.memory.wiki.compiler import WikiCompiler

            wiki_service = MemoryService(
                KnowledgeRepository(control_client),
                gate=RedactionGate(DataRedactionEngine()),
                audit=AuditService(repository),
            )
            env_row = repository.get_environment(
                task.environment_id, tenant_id=task.tenant_id
            )
            if env_row is not None and env_row.get("db_version"):
                WikiCompiler(wiki_service).compile_and_store(
                    tenant_id=principal.tenant_id,
                    goal=task.goal,
                    root_cause=result.conclusion,
                    evidence=result.evidence,
                    db_engine=str(env_row.get("db_engine") or "mysql"),
                    db_version=str(env_row["db_version"]),
                    principal=principal,
                )
        except Exception:  # noqa: BLE001 - 沉淀失败不得影响诊断结果
            pass
    return result.model_dump(mode="json")
