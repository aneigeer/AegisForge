"""双引擎固定工具 API（W02）：Adapter -> Registry -> Executor 的生产装配。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from adapters.mysql.adapter import MySQLAdapter
from adapters.postgres_mcp.adapter import PostgresMcpAdapter
from adapters.postgres_mcp.client import PostgresMcpClient
from adapters.registry import build_adapter_registry
from apps.api.deps import get_principal, get_task_repository
from core.config import get_settings
from core.errors import ConfigError, PolicyDeniedError, ToolNotFoundError
from core.security.authz import ensure_permission
from core.security.models import EnvironmentContext, Principal, environment_tier
from core.security.policy import PolicyEngine
from core.security.roles import Permission
from core.tasks.repository import TaskRepository
from core.tools.contracts import ToolContext
from core.tools.executor import ToolExecutor
from database.mysql.connection import MysqlClient
from database.postgres.connection import PgClient

router = APIRouter(prefix="/adapters", tags=["adapters"])

Engine = Literal["mysql", "postgresql"]


class ToolCallBody(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _clients(engine: Engine) -> tuple[Any, Any]:
    settings = get_settings()
    if engine == "mysql":
        ro_client = MysqlClient(
            settings.ro_dsn,
            database_id=settings.database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
        return MySQLAdapter(ro_client), ro_client
    if settings.pg_ro_dsn is None:
        raise ConfigError("未配置 PostgreSQL LAB", details={"field": "pg_ro_dsn"})
    mcp_client = PostgresMcpClient(settings.pg_ro_dsn, timeout_s=settings.model_timeout_s)
    pg_client = PgClient(
        settings.pg_ro_dsn,
        database_id=settings.pg_database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    return PostgresMcpAdapter(mcp_client), pg_client


def _close(adapter: Any) -> None:
    client = getattr(adapter, "_client", None)
    if hasattr(client, "close"):
        client.close()


@router.get("/{engine}/tools")
def list_tools(
    engine: Engine,
    principal: Principal = Depends(get_principal),
) -> list[dict[str, Any]]:
    ensure_permission(principal, Permission.ADAPTER_READ)
    adapter, _ = _clients(engine)
    try:
        specs = adapter.tool_specs()
        return [
            {
                "name": spec.definition.name,
                "version": spec.definition.version,
                "engine": spec.engine.value,
                "available": spec.available,
                "unavailable_reason": spec.unavailable_reason,
                "definition": spec.definition.model_dump(mode="json"),
            }
            for spec in specs
        ]
    finally:
        _close(adapter)


@router.post("/{engine}/tools/{tool_name}")
def call_tool(
    engine: Engine,
    tool_name: str,
    body: ToolCallBody | None = None,
    principal: Principal = Depends(get_principal),
    task_repository: TaskRepository = Depends(get_task_repository),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.ADAPTER_INVOKE)
    settings = get_settings()
    adapter, context_client = _clients(engine)
    try:
        registry = build_adapter_registry(adapter)
        try:
            definition = registry.get(tool_name).definition  # type: ignore[attr-defined]
        except ToolNotFoundError:
            definition = None

        if definition is not None:
            environment_id = (
                settings.default_environment_id
                if engine == "mysql"
                else settings.default_pg_environment_id
            )
            environment_row = task_repository.get_environment(
                environment_id, tenant_id=principal.tenant_id
            )
            environment = EnvironmentContext(
                environment_id=environment_id,
                tenant_id=principal.tenant_id,
                db_engine=engine,
                tier=environment_tier(
                    environment_row.get("status") if environment_row else None
                ),
            )
            decision = PolicyEngine(policy_version=settings.policy_version).decide(
                principal, definition, environment
            )
            if not decision.allowed:
                raise PolicyDeniedError(
                    "操作被策略拒绝",
                    details=decision.to_public_dict(),
                )

        executor = ToolExecutor(registry)
        result = executor.execute(
            tool_name,
            (body or ToolCallBody()).params,
            ToolContext(client=context_client),
        )
        return result.model_dump(mode="json")
    finally:
        _close(adapter)
