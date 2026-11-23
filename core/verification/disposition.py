"""W08 结果处置三分法 + 九格矩阵（AC-12 / 设计 §3.2）。

九格矩阵 = `verdict` × `disposition` 的允许/拒绝，而非 `verdict` × 证据充分性；
「证据充分性」退化为 `SAFE_TO_COMPENSATE` 的附加前置条件（裁决 A4）。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.errors import PolicyDeniedError, RequestInvalidError
from core.verification.models import Disposition, Verdict


@dataclass(frozen=True)
class DispositionDecision:
    """`resolve_disposition()` 的默认分派结果。

    `disposition=None` 仅用于 `PASS`（无需处置）；`SAFE_TO_COMPENSATE` 恒带
    `approval_required=True`（AC-12）。
    """

    verdict: Verdict
    disposition: Disposition | None
    approval_required: bool
    evidence_sufficient: bool
    reason: str


def is_allowed(verdict: Verdict, disposition: Disposition) -> bool:
    """九格矩阵（设计 §3.2）：

    - `PASS` × 三种 disposition → 全拒绝（PASS 无需处置）；
    - `FAIL` × 三种 → 全允许（`SAFE_TO_COMPENSATE` 的附加前置条件由
      `resolve_disposition()` 保证：证据充分 + `approval_required=True`）；
    - `UNKNOWN` × `SAFE_TO_COMPENSATE` → **拒绝并抛 `POLICY_DENIED`**；
    - `UNKNOWN` × `HUMAN_ADVICE_ONLY` / `MANUAL_INTERVENTION` → 允许。
    """
    if verdict not in ("PASS", "FAIL", "UNKNOWN"):
        raise RequestInvalidError("非法 verdict", details={"verdict": verdict})
    if verdict == "PASS":
        return False
    if verdict == "UNKNOWN" and disposition == "SAFE_TO_COMPENSATE":
        raise PolicyDeniedError(
            "UNKNOWN 结果不得判为可自动补偿（SAFE_TO_COMPENSATE）",
            details={
                "reason": "UNKNOWN_NOT_COMPENSABLE",
                "verdict": verdict,
                "disposition": disposition,
            },
        )
    return True


def resolve_disposition(
    verdict: Verdict, *, evidence_sufficient: bool
) -> DispositionDecision:
    """默认分派（设计 §3.2）：

    - `PASS` → 无需处置（`disposition=None`）；
    - `FAIL` + 证据充分 → `SAFE_TO_COMPENSATE`（`approval_required=True`）；
    - `FAIL` + 证据不足 → `HUMAN_ADVICE_ONLY`；
    - `UNKNOWN` → `MANUAL_INTERVENTION`（UNKNOWN 本义即证据不足）。

    `UNKNOWN` + `evidence_sufficient=True` 是矛盾输入（等于有人想按「证据充分」
    自动补偿）：按 PRD P0-W08-05 的机器验证口径显式抛 `POLICY_DENIED`，
    绝不产出 `SAFE_TO_COMPENSATE`。
    """
    if verdict not in ("PASS", "FAIL", "UNKNOWN"):
        raise RequestInvalidError("非法 verdict", details={"verdict": verdict})
    if verdict == "PASS":
        return DispositionDecision(
            verdict=verdict,
            disposition=None,
            approval_required=False,
            evidence_sufficient=evidence_sufficient,
            reason="PASS_NO_DISPOSITION",
        )
    if verdict == "FAIL":
        if evidence_sufficient:
            return DispositionDecision(
                verdict=verdict,
                disposition="SAFE_TO_COMPENSATE",
                approval_required=True,
                evidence_sufficient=True,
                reason="FAIL_EVIDENCE_SUFFICIENT",
            )
        return DispositionDecision(
            verdict=verdict,
            disposition="HUMAN_ADVICE_ONLY",
            approval_required=False,
            evidence_sufficient=False,
            reason="FAIL_EVIDENCE_INSUFFICIENT",
        )
    if evidence_sufficient:
        raise PolicyDeniedError(
            "UNKNOWN 结果不得按「证据充分」自动补偿",
            details={
                "reason": "UNKNOWN_NOT_COMPENSABLE",
                "verdict": verdict,
                "evidence_sufficient": True,
            },
        )
    return DispositionDecision(
        verdict=verdict,
        disposition="MANUAL_INTERVENTION",
        approval_required=False,
        evidence_sufficient=False,
        reason="UNKNOWN_MANUAL_INTERVENTION",
    )


__all__ = ["DispositionDecision", "is_allowed", "resolve_disposition"]
