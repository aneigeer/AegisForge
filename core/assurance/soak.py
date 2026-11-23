"""W11 T05：长稳报告（P0-W11-04 / AC-14）。

设计依据：``docs/w11/02-design.md`` §8、§11；裁定 W11-Q4（架构师冻结**具名常量**）、
Q-W11-C（``SOAK_SAMPLE_SIZE=200`` / ``SOAK_DURATION_S=3600`` / ``SOAK_SMOKE_TEST=False`` /
``BACKUP_APPEND_N=12`` 采纳）、W11-Q3（不把建议目标写成已验证 SLA）。

三条硬约束：

1. **计划值与实测值双字段**：``sample_size`` / ``duration_s`` 是**冻结计划值**；
   ``measured_sample_size`` / ``measured_duration_s`` 未实跑必须 ``None`` ——
   禁止把计划值回填成"实测"（纪律 8：未实测一律 ``null``）。
2. **不用短时 smoke 冒充**：``smoke_test`` 恒为 ``False``（有校验器强制）。
3. **P50/P95 只复用** ``core/verification/perf.py:48 percentile``（不重写、不引第三方）；
   样本为空 → ``None``（不编造分位数）。

本模块是**纯数据处理**：不连库、不起进程、不写文件。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from core.verification.perf import percentile

#: 冻结计划值（W11-Q4 / Q-W11-C：架构师具名常量，写进报告，不得回填为实测）。
SOAK_SAMPLE_SIZE = 200
SOAK_DURATION_S = 3600
SOAK_POLL_INTERVAL_S = 5.0
SOAK_SMOKE_TEST = False

#: 长稳期间必须覆盖的 6 项告警（集合相等，不多不少）。
ALERT_CATALOG: tuple[str, ...] = (
    "lease_lost",
    "audit_write_failed",
    "evidence_unavailable",
    "database_error",
    "model_degraded",
    "backup_failed",
)

SOAK_OVERCLAIMED = "SOAK_OVERCLAIMED"

PERCENTILE_P50 = 50.0
PERCENTILE_P95 = 95.0


class SoakStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class SoakReport(BaseModel):
    """长稳报告：计划值 / 实测值分列，未实跑一律 ``None``。"""

    model_config = ConfigDict(extra="forbid")

    sample_size: int = SOAK_SAMPLE_SIZE
    duration_s: int = SOAK_DURATION_S
    poll_interval_s: float = SOAK_POLL_INTERVAL_S
    smoke_test: bool = SOAK_SMOKE_TEST
    measured_sample_size: int | None = None
    measured_duration_s: float | None = None
    status: Literal["PLANNED", "RUNNING", "COMPLETED"] = "PLANNED"
    alerts: tuple[str, ...] = ALERT_CATALOG
    p50_ms: float | None = None
    p95_ms: float | None = None

    @model_validator(mode="after")
    def _plan_is_frozen_and_smoke_forbidden(self) -> "SoakReport":
        if self.sample_size <= 0:
            raise ValueError("sample_size 必须 > 0（AC-14）")
        if self.duration_s <= 0:
            raise ValueError("duration_s 必须 > 0（AC-14）")
        if self.smoke_test is not False:
            raise ValueError("smoke_test 必须为 False：不得用短时 smoke 冒充长稳（W11-Q4）")
        if set(self.alerts) != set(ALERT_CATALOG):
            raise ValueError(
                f"告警清单必须与 ALERT_CATALOG 集合相等（{ALERT_CATALOG}）"
            )
        if self.status == SoakStatus.PLANNED.value:
            # L-8：计划态不得夹带任何"实测"数值（否则等于用计划值冒充实测）。
            if (
                self.measured_sample_size is not None
                or self.measured_duration_s is not None
                or self.p50_ms is not None
                or self.p95_ms is not None
            ):
                raise ValueError(
                    "SOAK_OVERCLAIMED: status=PLANNED 时 measured_sample_size / "
                    "measured_duration_s / p50_ms / p95_ms 必须全为 None"
                    f"（实际 measured_sample_size={self.measured_sample_size}、"
                    f"measured_duration_s={self.measured_duration_s}、"
                    f"p50_ms={self.p50_ms}、p95_ms={self.p95_ms}）"
                )
        if self.status == SoakStatus.COMPLETED.value and (
            self.measured_sample_size is None or self.measured_duration_s is None
        ):
            raise ValueError(
                "SOAK_OVERCLAIMED: status=COMPLETED 必须有实测样本量与实测时长"
                "（不得回填计划值）"
            )
        return self


def build_soak_report(*, measured: Any | None = None) -> SoakReport:
    """构造长稳报告。

    Args:
        measured: ``None`` ⇒ ``PLANNED``（实测字段全 ``None``，本卡默认路径）；
            否则需提供映射，键为 ``sample_size`` / ``duration_s`` /
            ``latencies_ms``（可选），据此填**实测**字段并计算 P50/P95。

    Returns:
        :class:`SoakReport`。

    Raises:
        ValueError: ``measured`` 结构不合法（缺键/类型不符）。
        RequestInvalidError: ``percentile`` 对空样本/非法 p 的既有拒绝（本函数先判空，
            不会触发）。
    """
    if measured is None:
        # 未实跑：计划值照冻结常量写，实测值一律 None（**不回填**）。
        return SoakReport()

    if not isinstance(measured, Mapping):
        raise ValueError("measured 必须是映射（或 None 表示未实跑）")
    sample_size = measured.get("sample_size")
    duration_s = measured.get("duration_s")
    if not isinstance(sample_size, int) or sample_size <= 0:
        raise ValueError("measured.sample_size 必须是正整数")
    if not isinstance(duration_s, (int, float)) or float(duration_s) <= 0:
        raise ValueError("measured.duration_s 必须为正数")
    latencies = measured.get("latencies_ms") or ()
    samples = [float(item) for item in latencies]

    p50_ms: float | None = None
    p95_ms: float | None = None
    if samples:
        # 复用上游分位数实现（不重写、不引第三方）；空样本保持 None。
        p50_ms = float(percentile(samples, PERCENTILE_P50))
        p95_ms = float(percentile(samples, PERCENTILE_P95))

    return SoakReport(
        measured_sample_size=int(sample_size),
        measured_duration_s=float(duration_s),
        status=SoakStatus.COMPLETED.value,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
    )


def soak_plan_summary() -> dict[str, Any]:
    """冻结计划值与告警清单的结构化摘要（供报告与 RC 清单引用）。"""
    return {
        "sample_size": SOAK_SAMPLE_SIZE,
        "duration_s": SOAK_DURATION_S,
        "poll_interval_s": SOAK_POLL_INTERVAL_S,
        "smoke_test": SOAK_SMOKE_TEST,
        "alerts": list(ALERT_CATALOG),
        "measured_sample_size": None,
        "measured_duration_s": None,
        "p50_ms": None,
        "p95_ms": None,
        "status": SoakStatus.PLANNED.value,
    }


def assert_soak_not_overclaimed(report: SoakReport) -> None:
    """判据：未实测时不得出现任何"实测"数值或完成态（防止报告口径漂移）。"""
    if report.measured_sample_size is None or report.measured_duration_s is None:
        if report.status == SoakStatus.COMPLETED.value:
            raise ValueError("未实测却声明 COMPLETED：属于编造实测值")
        if report.p50_ms is not None or report.p95_ms is not None:
            raise ValueError("未实测却给出 P50/P95：属于编造指标")


__all__ = [
    "ALERT_CATALOG",
    "SOAK_OVERCLAIMED",
    "PERCENTILE_P50",
    "PERCENTILE_P95",
    "SOAK_DURATION_S",
    "SOAK_POLL_INTERVAL_S",
    "SOAK_SAMPLE_SIZE",
    "SOAK_SMOKE_TEST",
    "SoakReport",
    "SoakStatus",
    "assert_soak_not_overclaimed",
    "build_soak_report",
    "soak_plan_summary",
]
