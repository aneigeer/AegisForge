"""独立验证领域层（W08）：契约模型、读回端口、判定链、性能与只追加仓储。

导出面按 T01/T02/T03 已落地的模块组织；补偿（T04）、恢复（T05）等尚未建立，
不提前引用，否则 `import core.verification` 会失败。
"""

from __future__ import annotations

from core.verification.compensation import CompensationService
from core.verification.disposition import (
    DispositionDecision,
    is_allowed,
    resolve_disposition,
)
from core.verification.models import (
    AssertionOutcome,
    Disposition,
    VerificationResult,
    Verdict,
)
from core.verification.perf import (
    WORKLOAD_SEQUENCE,
    PerfReport,
    evaluate_performance,
    percentile,
)
from core.verification.plan import (
    ResolvedExpected,
    VerificationPlan,
    aggregate,
    evaluate_assertion,
    evaluate_plan,
    plan_digest,
    resolve_expected,
)
from core.verification.readback import (
    READBACK_TARGETS,
    ReadbackPort,
    ReadbackSample,
    TargetObject,
    validate_target,
)
from core.verification.repository import TABLE, VerificationResultRepository
from core.verification.verifier import Verifier

__all__ = [
    "READBACK_TARGETS",
    "TABLE",
    "WORKLOAD_SEQUENCE",
    "AssertionOutcome",
    "CompensationService",
    "Disposition",
    "DispositionDecision",
    "PerfReport",
    "ReadbackPort",
    "ReadbackSample",
    "ResolvedExpected",
    "TargetObject",
    "VerificationPlan",
    "VerificationResult",
    "VerificationResultRepository",
    "Verdict",
    "Verifier",
    "aggregate",
    "evaluate_assertion",
    "evaluate_performance",
    "evaluate_plan",
    "is_allowed",
    "percentile",
    "plan_digest",
    "resolve_disposition",
    "resolve_expected",
    "validate_target",
]
