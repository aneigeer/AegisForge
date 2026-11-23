"""API 依赖装配：控制库连接、仓储与服务。

每次请求按需构造 MysqlClient；连接在使用时建立并关闭，不跨请求复用。

W07 装配纪律：
- 审批侧注册表一律经 `build_lab_registry()`（写工具只在 LAB + mysql 时注册）；
  `build_adapter_registry()` / `build_observer_registry()` 的写集合恒为空。
- `PrerequisiteProbe` 注入 `MysqlPrepareProbe`（7 条固定 SQL），
  否则 `prepare_required` 工具会因 7 项 UNKNOWN 被 fail closed 拒绝创建审批。
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import Depends

from adapters.lab_assembly import build_lab_registry
from adapters.mysql.prepare_probe import MysqlPrepareProbe
from apps.api.security import get_principal
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.auth.service import AuthService
from core.config import Settings, get_settings
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.execution.step_records import StepRecordRepository
from core.security.models import Principal
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry


def _client_for(
    dsn: str,
    *,
    database_id: str,
    read_only: bool,
    settings: Settings,
) -> MysqlClient:
    return MysqlClient(
        dsn,
        database_id=database_id,
        read_only=read_only,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


def get_control_client() -> MysqlClient:
    settings = get_settings()
    return MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


def get_task_repository(
    client: MysqlClient = Depends(get_control_client),
) -> TaskRepository:
    return TaskRepository(client)


def get_evidence_repository(
    client: MysqlClient = Depends(get_control_client),
) -> EvidenceRepository:
    return EvidenceRepository(client)


def get_auth_service(
    client: MysqlClient = Depends(get_control_client),
    settings: Settings = Depends(get_settings),
) -> AuthService:
    return AuthService(client, settings)


def get_task_service(
    repository: TaskRepository = Depends(get_task_repository),
) -> TaskService:
    return TaskService(repository, build_observer_registry())


def get_audit_service(
    repository: TaskRepository = Depends(get_task_repository),
) -> AuditService:
    """审计查询/完整性校验服务（只读编排，写入仍走仓储的哈希链实现）。"""
    return AuditService(repository)


def get_approval_service(
    client: MysqlClient = Depends(get_control_client),
    repository: TaskRepository = Depends(get_task_repository),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(get_principal),
) -> ApprovalService:
    """审批服务：控制库仓储 + 服务端前置条件探测（含 LAB 固定 SQL 探针）+ LAB 注册表。

    注册表的写工具**只**由 `build_lab_registry()` 决定，且必须同时满足
    `environment.status == "LAB"` 与 `db_engine == "mysql"` 两个服务端事实；
    站点仍可通过 monkeypatch 本模块的 `build_observer_registry` 注入测试桩工具。
    """
    environment_row: Mapping[str, Any] | None = repository.get_environment(
        settings.default_environment_id, tenant_id=principal.tenant_id
    )
    db_engine = (
        str(environment_row.get("db_engine") or "") if environment_row else None
    )
    registry = build_lab_registry(
        build_observer_registry(), environment_row, db_engine
    )
    prepare_probe = MysqlPrepareProbe(
        _client_for(
            settings.ro_dsn.get_secret_value(),
            database_id=settings.database_id,
            read_only=True,
            settings=settings,
        ),
        _client_for(
            settings.rw_dsn.get_secret_value(),
            database_id=settings.database_id,
            read_only=False,
            settings=settings,
        ),
    )
    audit = AuditService(repository)
    return ApprovalService(
        ApprovalRepository(client),
        repository,
        PrerequisiteProbe(repository, prepare_probe=prepare_probe),
        audit,
        settings=settings,
        registry=registry,
        step_records=StepRecordRepository(client, audit=audit),
    )


def get_evidence_service(
    repository: EvidenceRepository = Depends(get_evidence_repository),
    task_repository: TaskRepository = Depends(get_task_repository),
) -> EvidenceService:
    return EvidenceService(repository, task_repository)


__all__ = [
    "get_approval_service",
    "get_audit_service",
    "get_auth_service",
    "get_control_client",
    "get_evidence_repository",
    "get_evidence_service",
    "get_principal",
    "get_task_repository",
    "get_task_service",
]
