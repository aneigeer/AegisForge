"""W11 T05 单元测试：长稳报告与冻结常量（AC-14）。

只跑纯逻辑（不连库、不写文件），覆盖：

- 冻结计划值 `SOAK_SAMPLE_SIZE=200` / `SOAK_DURATION_S=3600` / `SOAK_POLL_INTERVAL_S=5.0` /
  `SOAK_SMOKE_TEST=False` / `BACKUP_APPEND_N=12`；
- 告警清单 6 项**集合相等**；
- 未实跑时 `measured_*` / `p50_ms` / `p95_ms` 全 `None`、`status=PLANNED`；
- `smoke_test=True`、`sample_size<=0`、`duration_s<=0`、缺告警、谎报 `COMPLETED` 全部被校验器拒绝；
- P50/P95 复用 `core/verification/perf.py::percentile`，样本空 → `None`；
- 报告文档如实分列「目标值 / 实测值」，实测列全为 `null`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.assurance.backup import BACKUP_APPEND_N
from core.assurance.soak import (
    ALERT_CATALOG,
    SOAK_DURATION_S,
    SOAK_POLL_INTERVAL_S,
    SOAK_SAMPLE_SIZE,
    SOAK_SMOKE_TEST,
    SoakReport,
    SoakStatus,
    assert_soak_not_overclaimed,
    build_soak_report,
    soak_plan_summary,
)
from core.verification.perf import percentile

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPORT_PATH = _REPO_ROOT / "docs" / "w11" / "backup-restore-report.md"


# ---------------------------------------------------------------------------
# 冻结常量
# ---------------------------------------------------------------------------


def test_frozen_plan_constants() -> None:
    assert SOAK_SAMPLE_SIZE == 200
    assert SOAK_DURATION_S == 3600
    assert SOAK_POLL_INTERVAL_S == 5.0
    assert SOAK_SMOKE_TEST is False
    assert BACKUP_APPEND_N == 18


def test_alert_catalog_is_exactly_six() -> None:
    assert len(ALERT_CATALOG) == 6
    assert set(ALERT_CATALOG) == {
        "lease_lost",
        "audit_write_failed",
        "evidence_unavailable",
        "database_error",
        "model_degraded",
        "backup_failed",
    }
    assert len(set(ALERT_CATALOG)) == 6


# ---------------------------------------------------------------------------
# 默认报告（未实跑）
# ---------------------------------------------------------------------------


def test_default_report_is_planned_without_measurements() -> None:
    report = build_soak_report()
    assert report.sample_size == SOAK_SAMPLE_SIZE
    assert report.duration_s == SOAK_DURATION_S
    assert report.smoke_test is False
    assert report.status == SoakStatus.PLANNED.value
    assert report.measured_sample_size is None
    assert report.measured_duration_s is None
    assert report.p50_ms is None
    assert report.p95_ms is None
    assert set(report.alerts) == set(ALERT_CATALOG)
    assert_soak_not_overclaimed(report)


def test_plan_summary_matches_report_defaults() -> None:
    summary = soak_plan_summary()
    report = build_soak_report()
    assert summary["sample_size"] == report.sample_size
    assert summary["duration_s"] == report.duration_s
    assert summary["smoke_test"] is False
    assert summary["measured_sample_size"] is None
    assert summary["p50_ms"] is None
    assert summary["alerts"] == list(ALERT_CATALOG)


# ---------------------------------------------------------------------------
# 校验器：不得用 smoke / 计划值 / 缺项冒充
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"smoke_test": True},
        {"sample_size": 0},
        {"duration_s": 0},
        {"status": "COMPLETED"},
        {"alerts": ALERT_CATALOG[:-1]},
        {"alerts": (*ALERT_CATALOG, "extra_alert")},
    ],
    ids=["smoke", "zero_sample", "zero_duration", "completed_without_measured", "missing_alert", "extra_alert"],
)
def test_report_model_rejects_overclaims(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SoakReport(**payload)


def test_planned_status_rejects_measured_values() -> None:
    """L-8：`PLANNED` 夹带实测值 = 用计划值冒充实测，必须拒。"""
    with pytest.raises(ValidationError) as excinfo:
        SoakReport(measured_sample_size=200, measured_duration_s=3600, status="PLANNED")
    assert "SOAK_OVERCLAIMED" in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        SoakReport(measured_sample_size=200, status="PLANNED")
    assert "SOAK_OVERCLAIMED" in str(excinfo.value)

    for payload in (
        {"p50_ms": 12.0, "status": "PLANNED"},
        {"p95_ms": 30.0, "status": "PLANNED"},
        {"measured_duration_s": 1.0, "status": "PLANNED"},
    ):
        with pytest.raises(ValidationError) as excinfo:
            SoakReport(**payload)
        assert "SOAK_OVERCLAIMED" in str(excinfo.value)


def test_completed_status_accepts_measured_values() -> None:
    """反向对照：证明该守卫不是"恒拒"，只拒"计划态夹带实测"。"""
    report = SoakReport(
        measured_sample_size=200, measured_duration_s=3600.0, status="COMPLETED"
    )
    assert report.status == SoakStatus.COMPLETED.value
    assert report.measured_sample_size == 200
    assert report.measured_duration_s == 3600.0
    assert report.p50_ms is None and report.p95_ms is None


def test_report_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SoakReport(measured_qps=1)  # type: ignore[call-arg]


def test_assert_soak_not_overclaimed_rejects_fabricated_completion() -> None:
    fabricated = SoakReport.model_construct(
        sample_size=SOAK_SAMPLE_SIZE,
        duration_s=SOAK_DURATION_S,
        poll_interval_s=SOAK_POLL_INTERVAL_S,
        smoke_test=False,
        measured_sample_size=None,
        measured_duration_s=None,
        status="COMPLETED",
        alerts=ALERT_CATALOG,
        p50_ms=None,
        p95_ms=None,
    )
    with pytest.raises(ValueError):
        assert_soak_not_overclaimed(fabricated)


def test_assert_soak_not_overclaimed_rejects_percentiles_without_measurement() -> None:
    fabricated = SoakReport.model_construct(
        sample_size=SOAK_SAMPLE_SIZE,
        duration_s=SOAK_DURATION_S,
        poll_interval_s=SOAK_POLL_INTERVAL_S,
        smoke_test=False,
        measured_sample_size=None,
        measured_duration_s=None,
        status="PLANNED",
        alerts=ALERT_CATALOG,
        p50_ms=12.0,
        p95_ms=30.0,
    )
    with pytest.raises(ValueError):
        assert_soak_not_overclaimed(fabricated)


# ---------------------------------------------------------------------------
# 实跑路径（本卡不实跑；用受控样本验证分位数复用与空样本口径）
# ---------------------------------------------------------------------------


def test_measured_report_uses_percentile() -> None:
    samples = [10.0, 20.0, 30.0, 40.0, 50.0]
    report = build_soak_report(
        measured={"sample_size": len(samples), "duration_s": 12.5, "latencies_ms": samples}
    )
    assert report.status == SoakStatus.COMPLETED.value
    assert report.measured_sample_size == 5
    assert report.measured_duration_s == 12.5
    assert report.p50_ms == percentile(samples, 50.0) == 30.0
    assert report.p95_ms == percentile(samples, 95.0) == 50.0
    # 计划值不被实测值覆盖
    assert report.sample_size == SOAK_SAMPLE_SIZE
    assert report.duration_s == SOAK_DURATION_S
    assert_soak_not_overclaimed(report)


def test_measured_report_without_samples_keeps_percentiles_none() -> None:
    report = build_soak_report(measured={"sample_size": 3, "duration_s": 1.0})
    assert report.p50_ms is None
    assert report.p95_ms is None
    assert report.measured_sample_size == 3


@pytest.mark.parametrize(
    "measured",
    [
        {"duration_s": 1.0},
        {"sample_size": 0, "duration_s": 1.0},
        {"sample_size": 1, "duration_s": 0},
        {"sample_size": 1, "duration_s": 1.0, "latencies_ms": "not-a-list-of-numbers"},
        "not-a-mapping",
    ],
    ids=["no_sample_size", "zero_sample", "zero_duration", "bad_latency", "not_mapping"],
)
def test_build_soak_report_rejects_invalid_measured(measured: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        build_soak_report(measured=measured)


# ---------------------------------------------------------------------------
# 报告文档：目标值与实测值分列，实测列全 null
# ---------------------------------------------------------------------------


def test_report_document_lists_constants_and_nulls() -> None:
    text = _REPORT_PATH.read_text(encoding="utf-8")
    for token in (
        "SOAK_SAMPLE_SIZE",
        "SOAK_DURATION_S",
        "SOAK_POLL_INTERVAL_S",
        "SOAK_SMOKE_TEST",
        "BACKUP_APPEND_N",
        "200",
        "3600",
        "12",
    ):
        assert token in text, token
    assert "| 实测" in text or "实测值" in text
    assert "null" in text
    # 表述禁令：命中即失败，但**否定式**引用（"不写…"/"不得…"）不算违规。
    for banned in ("已验证 SLA", "环境已清零", "全部可自动恢复", "RPO 已测得"):
        for match in re.finditer(re.escape(banned), text):
            prefix = text[max(0, match.start() - 12) : match.start()]
            assert "不" in prefix or "禁" in prefix, (banned, prefix)
