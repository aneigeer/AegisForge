"""W08 恢复控制领域层（T05）：循环 / 无进展 / 反证 / 一次重试 / UNKNOWN 收敛。

包面只导出控制器与三个契约模型；此层不引用 `core.tools.registry`、
`core.gateway.*` 或任何 DSN，恢复器结构上不具备重放写的能力。
"""

from __future__ import annotations

from core.recovery.controller import (
    LOOP_DETECTED,
    MAX_REPAIR_RETRIES_LIMIT,
    MODEL_OUTPUT_INVALID,
    NO_PROGRESS,
    RecoveryController,
)
from core.recovery.models import (
    CONVERGED_STATUSES,
    Hypothesis,
    HypothesisStatus,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryOutcome,
    normalize_targets,
)

__all__ = [
    "CONVERGED_STATUSES",
    "Hypothesis",
    "HypothesisStatus",
    "LOOP_DETECTED",
    "MAX_REPAIR_RETRIES_LIMIT",
    "MODEL_OUTPUT_INVALID",
    "NO_PROGRESS",
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryController",
    "RecoveryOutcome",
    "normalize_targets",
]
