"""W10 T03：聚类 + 分层 Bootstrap 置信区间（**标准库**，AC-10）。

设计 ``02-design.md`` §6 / 主理人裁定 W10-Q5：

- cluster key = ``(engine, scenario, fault_seed)``；按 ``(engine, scenario)``
  **分层**，层内对 cluster 有放回重采样，层间样本量固定（**不跨层借用**）；
- ``random.Random(BOOTSTRAP_SEED)`` 固定种子 → 同 seed 两跑**逐位相等**；
- CI 端点 = 2.5% / 97.5% 分位，**复用** ``core/verification/perf.py::percentile``
  （禁止重写；``percentile([])`` 的 ``REQUEST_INVALID`` 口径沿用）；
- cluster 数 < ``BOOTSTRAP_MIN_CLUSTERS`` → ``lower=upper=None``（不伪造区间）；
- 输入指纹**复用** ``core/security/canonical.py::canonical_digest``（禁止重写摘要）。

禁止引入 ``numpy`` / ``scipy`` / ``statistics.bootstrap`` 或任何第三方实现。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from core.errors import BenchmarkInvalidError
from core.security.canonical import canonical_digest
from core.verification.perf import percentile

BOOTSTRAP_RESAMPLES: int = 2000
BOOTSTRAP_CONFIDENCE: float = 0.95
BOOTSTRAP_SEED: int = 20261115
BOOTSTRAP_MIN_CLUSTERS: int = 2


@dataclass(frozen=True)
class ConfidenceInterval:
    """Bootstrap 结果；``lower``/``upper`` 为 ``None`` 表示区间不可估（不伪造）。"""

    point: float | None
    lower: float | None
    upper: float | None
    resamples: int
    seed: int
    method: str = "cluster-stratified-percentile"
    input_digest: str | None = None


def _canonical_input(
    values: Sequence[float], clusters: Sequence[tuple[str, str, int]]
) -> str:
    """复用 ``canonical_digest`` 计算输入指纹（float 先转十进制字符串）。"""

    payload = [
        [repr(float(value)), [str(cluster[0]), str(cluster[1]), int(cluster[2])]]
        for value, cluster in zip(values, clusters)
    ]
    return canonical_digest(payload)


def cluster_bootstrap(
    values: Sequence[float],
    *,
    clusters: Sequence[tuple[str, str, int]],
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> ConfidenceInterval:
    """按 ``(engine, scenario)`` 分层、层内对 cluster 有放回重采样求 CI。

    ``values[i]`` 属于 ``clusters[i]``；同 cluster 的多个观测在每次重采样中
    **整体**被选中或不被选中（聚类口径，重复 seed 不产生独立样本）。
    """

    if len(values) != len(clusters):
        raise BenchmarkInvalidError(
            "cluster_bootstrap 的 values 与 clusters 长度必须一致",
            details={"values": len(values), "clusters": len(clusters)},
        )
    if resamples < 0:
        raise BenchmarkInvalidError(
            "resamples 不得为负", details={"resamples": resamples}
        )
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, tuple) or len(cluster) != 3:
            raise BenchmarkInvalidError(
                "cluster key 必须是 (engine, scenario, fault_seed) 三元组",
                details={"index": index},
            )
        if not isinstance(cluster[2], int):
            raise BenchmarkInvalidError(
                "cluster key 的 fault_seed 必须是 int", details={"index": index}
            )

    digest = _canonical_input(values, clusters)
    point: float | None = (
        sum(float(value) for value in values) / len(values) if values else None
    )
    unique_clusters = sorted(set(clusters))
    if len(unique_clusters) < BOOTSTRAP_MIN_CLUSTERS or resamples == 0 or not values:
        # cluster 数 < 2 / 无重采样 / 空样本：CI = None（不伪造区间）。
        return ConfidenceInterval(
            point=point,
            lower=None,
            upper=None,
            resamples=0,
            seed=seed,
            input_digest=digest,
        )

    strata: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    values_by_cluster: dict[tuple[str, str, int], list[float]] = {}
    for cluster in unique_clusters:
        strata.setdefault((cluster[0], cluster[1]), []).append(cluster)
        values_by_cluster[cluster] = []
    for value, cluster in zip(values, clusters):
        values_by_cluster[cluster].append(float(value))

    rng = random.Random(seed)
    lower_pct = (1.0 - BOOTSTRAP_CONFIDENCE) / 2.0 * 100.0  # 2.5（百分位写法）
    upper_pct = (1.0 + BOOTSTRAP_CONFIDENCE) / 2.0 * 100.0  # 97.5
    boot_means: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        # 分层顺序固定（键排序），层间样本量固定为本层 cluster 数，不跨层借用。
        for stratum in sorted(strata):
            layer = strata[stratum]
            for cluster in rng.choices(layer, k=len(layer)):
                sample.extend(values_by_cluster[cluster])
        if not sample:  # pragma: no cover - 有 cluster 必有观测，防御性保留
            continue
        boot_means.append(sum(sample) / len(sample))

    if not boot_means:  # pragma: no cover - 同上
        return ConfidenceInterval(
            point=point,
            lower=None,
            upper=None,
            resamples=0,
            seed=seed,
            input_digest=digest,
        )
    # 设计 §6「不伪造区间」：层内只有一个 cluster 或常量样本时，重采样无变异
    # （所有 boot_mean 相等），此时不得报出 lower==upper==point 的伪区间。
    if len(set(boot_means)) == 1:
        return ConfidenceInterval(
            point=point,
            lower=None,
            upper=None,
            resamples=len(boot_means),
            seed=seed,
            input_digest=digest,
        )
    # 唯一分位数入口：复用 W08 perf.py，不重写、不引第三方。
    lower = percentile(boot_means, lower_pct)
    upper = percentile(boot_means, upper_pct)
    return ConfidenceInterval(
        point=point,
        lower=lower,
        upper=upper,
        resamples=len(boot_means),
        seed=seed,
        input_digest=digest,
    )


__all__ = [
    "BOOTSTRAP_CONFIDENCE",
    "BOOTSTRAP_MIN_CLUSTERS",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "ConfidenceInterval",
    "cluster_bootstrap",
]
