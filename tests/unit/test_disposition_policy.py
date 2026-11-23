"""W08 AC-12：九格处置矩阵 + 默认分派 + UNKNOWN 不得自动补偿。"""

from __future__ import annotations

import pytest

from core.errors import ErrorCode, PolicyDeniedError
from core.verification.disposition import is_allowed, resolve_disposition
from core.verification.models import Disposition, Verdict

VERDICTS: tuple[Verdict, ...] = ("PASS", "FAIL", "UNKNOWN")
DISPOSITIONS: tuple[Disposition, ...] = (
    "SAFE_TO_COMPENSATE", "HUMAN_ADVICE_ONLY", "MANUAL_INTERVENTION",
)


def test_enums_are_closed() -> None:
    assert set(Verdict.__args__) == {"PASS", "FAIL", "UNKNOWN"}
    assert set(Disposition.__args__) == {
        "SAFE_TO_COMPENSATE", "HUMAN_ADVICE_ONLY", "MANUAL_INTERVENTION",
    }


def test_nine_grid_cell_by_cell() -> None:
    """8 格返回 bool；第 9 格（UNKNOWN×SAFE）抛 POLICY_DENIED。"""
    expected = {
        ("PASS", "SAFE_TO_COMPENSATE"): False,
        ("PASS", "HUMAN_ADVICE_ONLY"): False,
        ("PASS", "MANUAL_INTERVENTION"): False,
        ("FAIL", "SAFE_TO_COMPENSATE"): True,
        ("FAIL", "HUMAN_ADVICE_ONLY"): True,
        ("FAIL", "MANUAL_INTERVENTION"): True,
        ("UNKNOWN", "HUMAN_ADVICE_ONLY"): True,
        ("UNKNOWN", "MANUAL_INTERVENTION"): True,
    }
    for (verdict, disposition), want in expected.items():
        assert is_allowed(verdict, disposition) is want, (verdict, disposition)


def test_unknown_safe_to_compensate_raises_policy_denied() -> None:
    with pytest.raises(PolicyDeniedError) as exc:
        is_allowed("UNKNOWN", "SAFE_TO_COMPENSATE")
    assert exc.value.code is ErrorCode.POLICY_DENIED


def test_resolve_disposition_unknown_with_sufficient_evidence_raises() -> None:
    """PRD P0-W08-05：UNKNOWN 却「证据充分」= 试图自动补偿，显式拒绝。"""
    with pytest.raises(PolicyDeniedError) as exc:
        resolve_disposition("UNKNOWN", evidence_sufficient=True)
    assert exc.value.code is ErrorCode.POLICY_DENIED


def test_default_dispatch() -> None:
    fail_sufficient = resolve_disposition("FAIL", evidence_sufficient=True)
    assert fail_sufficient.disposition == "SAFE_TO_COMPENSATE"
    assert fail_sufficient.approval_required is True
    assert fail_sufficient.evidence_sufficient is True

    fail_insufficient = resolve_disposition("FAIL", evidence_sufficient=False)
    assert fail_insufficient.disposition == "HUMAN_ADVICE_ONLY"
    assert fail_insufficient.approval_required is False

    unknown = resolve_disposition("UNKNOWN", evidence_sufficient=False)
    assert unknown.disposition == "MANUAL_INTERVENTION"
    assert unknown.approval_required is False

    passed = resolve_disposition("PASS", evidence_sufficient=True)
    assert passed.disposition is None
    assert passed.approval_required is False


def test_every_safe_to_compensate_requires_approval() -> None:
    """任何 SAFE_TO_COMPENSATE 结果必带 approval_required=True（AC-12）。"""
    for evidence in (True, False):
        decision = resolve_disposition("FAIL", evidence_sufficient=evidence)
        if decision.disposition == "SAFE_TO_COMPENSATE":
            assert decision.approval_required is True
