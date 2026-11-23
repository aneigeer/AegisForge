"""W07：**唯一** LAB 执行栈装配点。

为什么必须只有一个装配点：

- 写工具一旦出现在通用装配路径（`build_adapter_registry()` / `build_observer_registry()`）里，
  就等于给所有调用方（诊断链路、适配器 API）开了写能力；这两条既有调用点
  （`core/diagnosis/runner.py`、`apps/api/routes/adapters.py`）**必须保持只读**。
- 因此写工具只在本模块的 `build_lab_registry()` 里注册，且必须同时满足
  `environment.status == "LAB"` 与 `db_engine == "mysql"` 两个服务端事实。

装配纪律：

- `build_lab_registry()` 只做**注册**，不做任何策略判断（策略层再拒一次：PROD 目标
  在 `core/security/policy.py` 已返回 `PROD_TARGET_NOT_ALLOWED`）。
- `build_lab_stack()` 返回一个显式字典（不返回隐式单例），调用方对每个依赖可见。
"""

from __future__ import annotations

from typing import Any, Mapping

from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from adapters.mysql.lab_repair import LabOrderNoteUpdateTool
from adapters.mysql.lab_status_repair import (
    ADVANCE_TOOL_NAME,
    REVERT_TOOL_NAME,
    LabOrderStatusAdvanceTool,
    LabOrderStatusRevertTool,
)
from adapters.mysql.prepare_probe import MysqlPrepareProbe
from core.approval.prerequisites import PrerequisiteProbe
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.audit.service import AuditService
from core.config import Settings
from core.execution.step_records import StepRecordRepository
from core.tasks.repository import TaskRepository
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry

LAB_ENVIRONMENT_STATUS = "LAB"
LAB_WRITE_ENGINE = "mysql"


def is_lab_mysql_target(
    environment_row: Mapping[str, Any] | None, db_engine: str | None
) -> bool:
    """写能力放行条件（服务端事实，两个条件同时成立）：LAB + mysql。"""
    if environment_row is None or db_engine is None:
        return False
    status = str(environment_row.get("status") or "").strip().upper()
    engine = str(db_engine).strip().lower()
    return status == LAB_ENVIRONMENT_STATUS and engine == LAB_WRITE_ENGINE


def build_lab_registry(
    base: ToolRegistry | None = None,
    environment_row: Mapping[str, Any] | None = None,
    db_engine: str | None = None,
) -> ToolRegistry:
    """复制 `base` 并把**受控写工具集合**注册进 LAB+mysql 的注册表。

    - `base` 为 None 时以只读观察器集合为底。
    - 非 LAB / 非 mysql / 未给环境事实 → 不注册写工具（fail closed）。
    """
    registry = ToolRegistry()
    origin = base if base is not None else build_observer_registry()
    for name in origin.names():
        registry.register(origin.get(name))
    if is_lab_mysql_target(environment_row, db_engine):
        # P2-T3：三个写工具各管一件事，且都带 preflight/爆炸半径/补偿声明：
        # note 单列更新、状态前进、状态回退（补偿专用，必须新审批）。
        for name, factory in (
            (LAB_WRITE_TOOL_NAME, LabOrderNoteUpdateTool),
            (ADVANCE_TOOL_NAME, LabOrderStatusAdvanceTool),
            (REVERT_TOOL_NAME, LabOrderStatusRevertTool),
        ):
            if name not in registry.names():
                registry.register(factory())
    return registry


def build_lab_stack(
    *,
    settings: Settings,
    control_client: MysqlClient,
    environment_row: Mapping[str, Any],
    ro_client: MysqlClient,
    rw_client: MysqlClient,
    base_registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """装配一套完整的 LAB 执行栈（审批 + 执行 + 证据）。

    返回字典中的键是稳定契约（测试与 Worker 装配共用）：
    `registry` / `prepare_probe` / `repository` / `audit` / `step_records` /
    `approvals` / `executor` / `environment_row` / `db_engine`。
    """
    db_engine = str(environment_row.get("db_engine") or "")
    registry = build_lab_registry(
        base_registry if base_registry is not None else build_observer_registry(),
        environment_row,
        db_engine,
    )
    repository = TaskRepository(control_client)
    audit = AuditService(repository)
    step_records = StepRecordRepository(control_client, audit=audit)
    prepare_probe = MysqlPrepareProbe(ro_client, rw_client)
    probe = PrerequisiteProbe(repository, prepare_probe=prepare_probe)
    approvals = ApprovalService(
        ApprovalRepository(control_client),
        repository,
        probe,
        audit,
        settings=settings,
        registry=registry,
        step_records=step_records,
    )
    executor = ToolExecutor(
        registry, repository, step_records=step_records, audit=audit
    )
    return {
        "registry": registry,
        "prepare_probe": prepare_probe,
        "repository": repository,
        "audit": audit,
        "step_records": step_records,
        "approvals": approvals,
        "executor": executor,
        "environment_row": dict(environment_row),
        "db_engine": db_engine,
    }


__all__ = [
    "LAB_ENVIRONMENT_STATUS",
    "LAB_WRITE_ENGINE",
    "LAB_WRITE_TOOL_NAME",
    "build_lab_registry",
    "build_lab_stack",
    "is_lab_mysql_target",
]
