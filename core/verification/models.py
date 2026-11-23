"""W08 独立验证的契约模型（T01）。

G1「模型自报不进任何判定」的构造级保证：本模块模型一律 `extra="forbid"`，
`VerificationResult` **不含**任何自然语言 / 模型输出字段——判定只由结构化的
`AssertionOutcome.outcomes` 与封闭枚举 `verdict` / `disposition` 组成，
构造上就没有注入模型文本的入口。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# 判定与处置的封闭枚举：值集合固定，新增取值须走版本化评审。
Verdict = Literal["PASS", "FAIL", "UNKNOWN"]
Disposition = Literal["SAFE_TO_COMPENSATE", "HUMAN_ADVICE_ONLY", "MANUAL_INTERVENTION"]


class AssertionOutcome(BaseModel):
    """单条断言的采集与判定结果（只记录事实，不记录任何模型叙述）。"""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    metric: str
    expected: str
    tolerance: str
    source: Literal["step_observed", "target_readback", "audit_event"]
    observed: str | int | None
    verdict: Verdict
    reason: str | None
    evidence_ref: str | None
    collected_at: datetime


class VerificationResult(BaseModel):
    """一次验证的聚合结果。

    刻意不含 `model_output` / `narrative` / `explanation` 等自然语言字段：
    G1 要求「模型自报不进任何判定」，因此在模型层就没有承载入口。
    """

    model_config = ConfigDict(extra="forbid")

    result_id: str
    tenant_id: str
    task_id: str
    tool_call_id: str
    plan_digest: str
    verdict: Verdict
    outcomes: tuple[AssertionOutcome, ...]
    disposition: Disposition | None
    approval_required: bool
    claimed_success: bool
    collected_at: datetime


__all__ = [
    "AssertionOutcome",
    "Disposition",
    "VerificationResult",
    "Verdict",
]
