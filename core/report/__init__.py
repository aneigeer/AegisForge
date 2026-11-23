"""W09 T03 结构化报告包（纯领域层，不连库、不持 DSN）。

公开面只导出报告模型与封闭状态枚举；闸门、仓储、导出、组装器按模块路径
显式导入，避免包初始化阶段产生 `core.report` 内部循环导入。

`RedactionGate` 的接入点在 `core/redaction/gate.py`（T02），本包不改写它。
"""

from __future__ import annotations

from core.report.models import (
    FORBIDDEN_COMPLETION_TERMS,
    ActionState,
    FactInputs,
    FactStatus,
    Recommendation,
    StructuredReport,
    VerificationState,
    derive_fact_status_from_inputs,
)

__version__ = "0.1.0"

__all__ = [
    "FORBIDDEN_COMPLETION_TERMS",
    "ActionState",
    "FactInputs",
    "FactStatus",
    "Recommendation",
    "StructuredReport",
    "VerificationState",
    "derive_fact_status_from_inputs",
]
