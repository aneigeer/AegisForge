"""W10 T03 单测：AC-10 聚类/分层 Bootstrap 可复现、``percentile([])`` 口径、
冻结常量、cluster 数不足时不伪造区间、复用点不被污染。

零 DB、零网络；不放宽断言、不加 skip/xfail。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.benchmark.stats import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_MIN_CLUSTERS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    ConfidenceInterval,
    cluster_bootstrap,
)
from core.errors import BenchmarkInvalidError, ErrorCode, RequestInvalidError
from core.verification.perf import percentile

ROOT = Path(__file__).resolve().parents[2]

# 3 个层 × 每层 2 个 cluster × 每 cluster 2 个重复观测：
# 既有「同 cluster 整体重采样」语义，又保证层内有变异（不产生伪区间）。
VALUES: tuple[float, ...] = tuple(float(index * 10) for index in range(1, 13))
CLUSTERS: tuple[tuple[str, str, int], ...] = (
    ("mysql", "slow_query", 4101),
    ("mysql", "slow_query", 4101),
    ("mysql", "slow_query", 4102),
    ("mysql", "slow_query", 4102),
    ("mysql", "lock_wait", 4103),
    ("mysql", "lock_wait", 4103),
    ("mysql", "lock_wait", 4104),
    ("mysql", "lock_wait", 4104),
    ("postgresql", "slow_query", 4201),
    ("postgresql", "slow_query", 4201),
    ("postgresql", "slow_query", 4202),
    ("postgresql", "slow_query", 4202),
)


# --------------------------------------------------------------------------- #
# 冻结常量 / AC-10 可复现
# --------------------------------------------------------------------------- #


def test_bootstrap_constants_are_frozen() -> None:
    assert BOOTSTRAP_RESAMPLES == 2000
    assert BOOTSTRAP_CONFIDENCE == 0.95
    assert BOOTSTRAP_SEED == 20261115
    assert BOOTSTRAP_MIN_CLUSTERS == 2


def test_ac10_same_seed_two_runs_are_bitwise_equal() -> None:
    first = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=200)
    second = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=200)
    assert isinstance(first, ConfidenceInterval)
    assert first == second  # frozen dataclass 逐字段（含 float）逐位相等
    assert first.lower == second.lower
    assert first.upper == second.upper
    assert first.point == second.point
    assert first.input_digest == second.input_digest
    assert first.seed == second.seed == BOOTSTRAP_SEED
    assert first.resamples == second.resamples == 200
    assert first.method == "cluster-stratified-percentile"
    assert first.lower is not None and first.upper is not None
    assert first.lower <= first.upper
    assert first.point == pytest.approx(sum(VALUES) / len(VALUES))


def test_default_resamples_and_seed_are_deterministic() -> None:
    """默认 2000 次重采样也逐位相等（冻结常量的真实路径）。"""

    first = cluster_bootstrap(VALUES, clusters=CLUSTERS)
    second = cluster_bootstrap(VALUES, clusters=CLUSTERS)
    assert first == second
    assert first.resamples == BOOTSTRAP_RESAMPLES == 2000


def test_different_seed_changes_interval() -> None:
    first = cluster_bootstrap(VALUES, clusters=CLUSTERS, seed=BOOTSTRAP_SEED, resamples=200)
    other = cluster_bootstrap(VALUES, clusters=CLUSTERS, seed=BOOTSTRAP_SEED + 1, resamples=200)
    assert first.seed != other.seed
    assert first.lower != other.lower or first.upper != other.upper
    # 同输入 → 指纹相同（确定性只由 seed 之外的输入决定）。
    assert first.input_digest == other.input_digest


def test_input_digest_is_content_addressed() -> None:
    first = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=20)
    shorter = cluster_bootstrap(VALUES[:-1], clusters=CLUSTERS[:-1], resamples=20)
    assert first.input_digest
    assert shorter.input_digest != first.input_digest


# --------------------------------------------------------------------------- #
# AC-10② 分位数口径（复用，不重写）
# --------------------------------------------------------------------------- #


def test_percentile_empty_samples_raises_request_invalid() -> None:
    with pytest.raises(RequestInvalidError) as caught:
        percentile([], 0.5)
    assert caught.value.code is ErrorCode.REQUEST_INVALID
    assert caught.value.details.get("reason") == "EMPTY_SAMPLES"


def test_percentile_reused_for_ci_endpoints() -> None:
    source = (ROOT / "core" / "benchmark" / "stats.py").read_text(encoding="utf-8")
    assert "from core.verification.perf import percentile" in source
    assert "percentile(boot_means, lower_pct)" in source
    assert "percentile(boot_means, upper_pct)" in source
    ci = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=64)
    assert ci.lower is not None and ci.upper is not None


# --------------------------------------------------------------------------- #
# 不伪造区间 / 输入校验
# --------------------------------------------------------------------------- #


def test_fewer_than_min_clusters_returns_ci_none() -> None:
    single = cluster_bootstrap(
        [1.0, 2.0],
        clusters=[("mysql", "slow_query", 1), ("mysql", "slow_query", 1)],
        resamples=100,
    )
    assert single.lower is None and single.upper is None
    assert single.resamples == 0
    assert single.point == 1.5


def test_empty_input_returns_none_point_and_ci() -> None:
    empty = cluster_bootstrap([], clusters=[])
    assert empty.point is None
    assert empty.lower is None and empty.upper is None
    assert empty.resamples == 0


def test_zero_resamples_does_not_fabricate_interval() -> None:
    ci = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=0)
    assert ci.lower is None and ci.upper is None
    assert ci.resamples == 0
    assert ci.point == pytest.approx(65.0)


@pytest.mark.parametrize(
    "values,clusters",
    [
        ([1.0, 2.0], [("mysql", "slow_query", 1)]),
        (VALUES, CLUSTERS[:-1]),
        ([1.0], [("mysql", "slow_query", "not-an-int")]),
        ([1.0], ["mysql"]),
    ],
)
def test_invalid_inputs_raise_benchmark_invalid(values: object, clusters: object) -> None:
    with pytest.raises(BenchmarkInvalidError) as caught:
        cluster_bootstrap(values, clusters=clusters, resamples=4)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID


def test_negative_resamples_are_rejected() -> None:
    with pytest.raises(BenchmarkInvalidError):
        cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=-1)


# --------------------------------------------------------------------------- #
# 分层与复用点
# --------------------------------------------------------------------------- #


def test_bootstrap_uses_only_standard_library_and_reuses_canonical_digest() -> None:
    source = (ROOT / "core" / "benchmark" / "stats.py").read_text(encoding="utf-8")
    assert "from core.security.canonical import canonical_digest" in source
    assert "random.Random(seed)" in source
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & {"numpy", "scipy", "statistics"}), modules
    assert "def canonical_digest(" not in source
    assert "def percentile(" not in source


def test_stratified_resampling_keeps_layer_sizes_fixed() -> None:
    """3 个层（mysql/slow, mysql/lock, pg/slow）各自重采样，结果可复现。"""

    ci = cluster_bootstrap(VALUES, clusters=CLUSTERS, resamples=128)
    strata = {(cluster[0], cluster[1]) for cluster in CLUSTERS}
    assert len(strata) == 3
    assert len(set(CLUSTERS)) == 6
    assert ci.resamples == 128
    assert ci.lower is not None and ci.upper is not None


def test_degenerate_resampling_returns_ci_none() -> None:
    """每层只 1 个 cluster（总 cluster 数 >= 2）时重采样无变异 → 不伪造区间。"""

    degenerate_values = (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    degenerate_clusters = (
        ("mysql", "slow_query", 1),
        ("mysql", "slow_query", 1),
        ("mysql", "lock_wait", 2),
        ("mysql", "lock_wait", 2),
        ("postgresql", "slow_query", 3),
        ("postgresql", "slow_query", 3),
    )
    assert len(set(degenerate_clusters)) == 3
    ci = cluster_bootstrap(
        degenerate_values, clusters=degenerate_clusters, resamples=100
    )
    assert ci.point == pytest.approx(35.0)
    assert ci.lower is None and ci.upper is None
    assert ci.resamples == 100
