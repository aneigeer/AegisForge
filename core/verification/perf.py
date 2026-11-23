"""W08 性能统计：P50/P95（排序索引法，无第三方）+ 收益不足不宣称成功（AC-06/07）。

绝对阈值不写死（PRD Q4：阈值需实测后由 W11 冻结），本周只记录分位数并用
「P95 不低于基线」作为「收益不足」的可判定判据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from core.errors import RequestInvalidError
from core.verification.models import Verdict

# 固定 workload 的测量阶段：预热 1 次 + 重复采样（次数由
# DG_VERIFICATION_PERF_SAMPLES 控制，默认 5）。序列本身不含任何 SQL 文本。
WORKLOAD_SEQUENCE: tuple[str, ...] = ("warmup", "measure")


@dataclass(frozen=True)
class PerfReport:
    """性能报告；`verdict` / `claimed_success` 为派生量，不能被人手工"注水"。

    只要 `performance_gain_insufficient=True`，`verdict` 必不为 `PASS`、
    `claimed_success` 必为 `False`（AC-07 的逻辑必然，而非构造巧合）。
    """

    p50: float
    p95: float
    sample_count: int
    baseline: float | None
    performance_gain_insufficient: bool

    @property
    def verdict(self) -> Verdict:
        if self.baseline is None:
            return "UNKNOWN"
        if self.performance_gain_insufficient:
            return "FAIL"
        return "PASS"

    @property
    def claimed_success(self) -> bool:
        return self.verdict == "PASS"


def percentile(samples: Sequence[float], p: float) -> float:
    """排序索引法（nearest-rank）求分位数。

    - `p ∈ (0, 1]` 视为比例；`p > 1` 视为百分位（如 50 → 0.5、95 → 0.95），
      两种写法都得到 `[10,20,30,40,50]` 的 P50=30、P95=50；
    - `N=1` 退化为该值；`N=0` 抛 `REQUEST_INVALID`（PRD AC-06 口径）；
    - `p` 越界抛 `REQUEST_INVALID`。
    """
    values = sorted(float(value) for value in samples)
    if not values:
        raise RequestInvalidError(
            "性能采样数 N=0，无法计算分位数",
            details={"reason": "EMPTY_SAMPLES", "sample_count": 0},
        )
    ratio = p / 100.0 if p > 1.0 else p
    if ratio < 0.0 or ratio > 1.0:
        raise RequestInvalidError("分位点 p 超出 [0,1] 值域", details={"p": p})
    # nearest-rank：rank = ceil(p * N)，index = rank - 1（clamp 到合法区间）。
    rank = math.ceil(ratio * len(values))
    index = max(0, min(rank - 1, len(values) - 1))
    return values[index]


def evaluate_performance(
    samples: Sequence[float], *, baseline: float | None
) -> PerfReport:
    """从采样序列生成性能报告。

    「收益不足」判据：有基线且 `p95 >= baseline`（P95 未改善）。
    「改善在噪声内」的绝对阈值待 W11 冻结，本周不打分（设计 R1）。
    """
    p50 = percentile(samples, 0.5)
    p95 = percentile(samples, 0.95)
    gain_insufficient = baseline is not None and p95 >= baseline
    return PerfReport(
        p50=p50,
        p95=p95,
        sample_count=len(samples),
        baseline=baseline,
        performance_gain_insufficient=gain_insufficient,
    )


__all__ = [
    "WORKLOAD_SEQUENCE",
    "PerfReport",
    "evaluate_performance",
    "percentile",
]
