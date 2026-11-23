"""FastAPI 入口（W01 最小骨架，W04 接入 Agent 循环与启动恢复）。"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from core import __version__
from core.config import get_settings
from core.errors import ConfigError, DBGuardianError
from core.logging import configure_logging, get_logger
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry
from database.postgres.connection import PgClient

from apps.api.errors import (
    TRACE_ID_HEADER,
    error_response,
    new_trace_id,
    validation_error_response,
)
from apps.api.routes.adapters import router as adapters_router
from apps.api.routes.eval import router as eval_router
from apps.api.routes.knowledge import router as knowledge_router
from apps.api.routes.model_providers import router as model_providers_router
from apps.api.routes.agent import router as agent_router
from apps.api.routes.approvals import router as approvals_router
from apps.api.routes.audit import router as audit_router
from apps.api.routes.auth import router as auth_router
from apps.api.routes.tasks import router as tasks_router
from apps.api.routes.v1 import router as v1_router

logger = get_logger(__name__)

# 启动恢复失败的结构化告警标记。
ALERT_STARTUP_RECOVERY_FAILED = "STARTUP_RECOVERY_FAILED"
# X-Trace-Id 只允许 ASCII 字母数字与 -_.，长度 1..64。
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _is_valid_trace_id(value: str) -> bool:
    """校验客户端传入的 X-Trace-Id，拒绝空值、超长与非法字符集。"""
    return bool(_TRACE_ID_PATTERN.fullmatch(value))


def create_app() -> FastAPI:
    settings = None
    try:
        settings = get_settings()
        configure_logging(settings.log_level, json_format=settings.log_json)
    except DBGuardianError as exc:
        # 允许 /healthz 在配置缺失时仍可访问，便于诊断部署问题。
        logger.warning("配置加载失败：%s", exc)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # W04：启动时恢复未完成任务；EXECUTING 任务只标记待人工核对，不自动续跑。
        if settings is not None and settings.recover_on_startup:
            try:
                client = MysqlClient(
                    settings.control_mysql_dsn(),
                    database_id="dg_system",
                    read_only=False,
                    connect_timeout_s=settings.connect_timeout_s,
                    statement_timeout_ms=settings.statement_timeout_ms,
                    max_result_rows=settings.max_result_rows,
                )
                service = TaskService(TaskRepository(client), build_observer_registry())
                recovered = service.recover_unfinished_tasks(
                    max_tasks=settings.recover_max_tasks,
                    budget_s=settings.recover_budget_s,
                )
                application.state.recovery_status = "ok"
                logger.info(
                    "启动恢复完成",
                    extra={"recovered_tasks": len(recovered)},
                )
            except Exception:  # 数据库不可用不应阻止 API 启动
                application.state.recovery_status = "failed"
                logger.exception(
                    "启动恢复失败，API 继续启动",
                    extra={"alert": ALERT_STARTUP_RECOVERY_FAILED},
                )
        else:
            application.state.recovery_status = "skipped"
        yield

    app = FastAPI(
        title="DBGuardian API",
        version=__version__,
        description="MySQL 8 专业数据库诊断、故障恢复与持续进化 Agent（Alpha）",
        lifespan=lifespan,
    )
    # 启动恢复状态初始值；lifespan 会覆盖为 ok / failed / skipped。
    app.state.recovery_status = "skipped"

    @app.middleware("http")
    async def attach_trace_id(request: Request, call_next):
        trace_id = request.headers.get(TRACE_ID_HEADER)
        if not trace_id or not _is_valid_trace_id(trace_id):
            trace_id = new_trace_id()
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[TRACE_ID_HEADER] = trace_id
        return response

    @app.exception_handler(DBGuardianError)
    async def handle_dbguardian_error(request: Request, exc: DBGuardianError):
        return error_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ):
        return validation_error_response(exc, request)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        return {
            "status": "ok",
            "service": "dbguardian",
            "version": __version__,
            "recovery_status": str(
                getattr(request.app.state, "recovery_status", "unknown")
            ),
        }

    app.include_router(auth_router)
    app.include_router(approvals_router)
    app.include_router(audit_router)
    app.include_router(tasks_router)
    app.include_router(agent_router)
    app.include_router(adapters_router)
    app.include_router(model_providers_router)
    app.include_router(knowledge_router)
    app.include_router(eval_router)
    # P4-T3：Prometheus 文本导出（无外部依赖；只暴露白名单指标）。
    from fastapi import Response

    from core.telemetry.metrics import METRICS

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        return Response(
            content=METRICS.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # P3-T3：单页控制台（静态资源，无需构建链）。
    from pathlib import Path as _Path

    from fastapi.staticfiles import StaticFiles

    web_dir = _Path(__file__).resolve().parents[1] / "web"
    if web_dir.is_dir():
        app.mount("/console", StaticFiles(directory=str(web_dir), html=True), name="console")
    app.include_router(v1_router)

    @app.get("/healthz/db")
    async def healthz_db() -> dict[str, str]:
        current = settings or get_settings()
        client = MysqlClient(
            current.ro_dsn,
            database_id=current.database_id,
            read_only=True,
            connect_timeout_s=current.connect_timeout_s,
            statement_timeout_ms=current.statement_timeout_ms,
        )
        # 仅探测连通性；数据库指纹（user/database/server_version 等）一律不回显。
        client.identity()
        return {"status": "ok"}

    @app.get("/healthz/pg")
    async def healthz_pg() -> dict[str, str]:
        current = settings or get_settings()
        if current.pg_ro_dsn is None:
            raise ConfigError(
                "未配置 PostgreSQL LAB", details={"field": "pg_ro_dsn"}
            )
        client = PgClient(
            current.pg_ro_dsn,
            database_id=current.pg_database_id,
            read_only=True,
            connect_timeout_s=current.connect_timeout_s,
            statement_timeout_ms=current.statement_timeout_ms,
            max_result_rows=current.max_result_rows,
        )
        # 仅探测连通性；数据库指纹（user/database/server_version 等）一律不回显。
        client.identity()
        return {"status": "ok"}

    return app


app = create_app()
