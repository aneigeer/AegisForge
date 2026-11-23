"""W08 AC-06/07：P50/P95 排序索引法、N=0 抛 REQUEST_INVALID、收益不足不宣称成功。"""

from __future__ import annotations

import pytest

from core.errors import ErrorCode, RequestInvalidError
from core.verification.perf import (
    WORKLOAD_SEQUENCE,
    PerfReport,
    evaluate_performance,
    percentile,
)

SAMPLES = [10, 20, 30, 40, 50]


# ---- AC-06 ---------------------------------------------------------------
def test_p50_p95_of_fixed_sequence() -> None:
    assert percentile(SAMPLES, 0.5) == 30
    assert percentile(SAMPLES, 0.95) == 50
    # 百分位写法（50/95）同样成立。
    assert percentile(SAMPLES, 50) == 30
    assert percentile(SAMPLES, 95) == 50


def test_percentile_is_order_independent() -> None:
    assert percentile([50, 10, 40, 20, 30], 0.5) == 30
    assert percentile([50, 10, 40, 20, 30], 0.95) == 50


def test_percentile_n1_degenerates() -> None:
    assert percentile([7], 0.5) == 7
    assert percentile([7], 0.95) == 7


def test_percentile_n0_raises_request_invalid() -> None:
    with pytest.raises(RequestInvalidError) as exc:
        percentile([], 0.5)
    assert exc.value.code is ErrorCode.REQUEST_INVALID


def test_percentile_rejects_out_of_range_p() -> None:
    # p > 1 视为百分位：150 即 1.5，超出 [0,1] 值域；负比例同样拒绝。
    with pytest.raises(RequestInvalidError):
        percentile(SAMPLES, 150)
    with pytest.raises(RequestInvalidError):
        percentile(SAMPLES, -0.1)


# ---- AC-07 ---------------------------------------------------------------
def test_gain_insufficient_never_claims_success() -> None:
    report = evaluate_performance([30] * 5, baseline=25)
    assert report.performance_gain_insufficient is True
    assert report.verdict != "PASS"
    assert report.claimed_success is False


def test_perf_report_with_gain_insufficient_true_is_deterministic() -> None:
    """`performance_gain_insufficient=True` → verdict != PASS 且 claimed_success False（AC-07 字面）。"""
    report = PerfReport(
        p50=30.0, p95=50.0, sample_count=5, baseline=100.0,
        performance_gain_insufficient=True,
    )
    assert report.verdict == "FAIL"
    assert report.claimed_success is False


def test_no_baseline_is_unknown_not_pass() -> None:
    report = evaluate_performance(SAMPLES, baseline=None)
    assert report.verdict == "UNKNOWN"
    assert report.claimed_success is False


def test_improved_p95_claims_success() -> None:
    report = evaluate_performance(SAMPLES, baseline=100)
    assert report.p50 == 30 and report.p95 == 50
    assert report.performance_gain_insufficient is False
    assert report.verdict == "PASS"
    assert report.claimed_success is True


def test_workload_sequence_is_frozen() -> None:
    assert isinstance(WORKLOAD_SEQUENCE, tuple)
    assert WORKLOAD_SEQUENCE
