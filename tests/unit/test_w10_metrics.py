"""W10 T03 单测：AC-06…AC-09、分母为 0 → None、冻结件自校验、两处一致。

零 DB、零网络；冻结资产只读；不放宽断言、不加 skip/xfail。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.manifest import RunStatus
from core.benchmark.metrics import (
    ALL_METRIC_NAMES,
    CORE_METRIC_NAMES,
    DEFAULT_DEFINITIONS,
    DEFAULT_METRICS_PATH,
    METRICS_VERSION,
    SUPPLEMENTARY_METRIC_NAMES,
    Attribution,
    MetricDefinition,
    MetricsEngine,
    is_env_excluded,
    load_metric_definitions,
    no_observable_fault,
    validate_definitions,
)
from core.benchmark.runner import CaseResult, InjectionState, RunResult
from core.errors import BenchmarkInvalidError, ErrorCode
from core.evidence.models import CollectionStatus
from core.tasks.models import TaskStatus

ROOT = Path(__file__).resolve().parents[2]

# AC-06：10 case（2 环境故障 + 1 注入失败）→ 逐指标分母**写死期望值**。
EXPECTED_DENOMINATORS: dict[str, int] = {
    "task_success_rate": 7,
    "unsafe_action_block_rate": 5,
    "schema_valid_rate": 5,
    "rca_top1": 5,
    "correct_abstention_rate": 5,
    "false_diagnosis_rate": 5,
    "false_repair_rate": 5,
    "recovery_success_rate": 5,
    "evidence_coverage": 5,
    "verified_task_success_rate": 7,
    "regression_rate": 0,
    "unsafe_action_escape_rate": 5,
    "task_p95_ms": 5,
    "cost_per_verified_task": 7,
}

_FULL_FIELDS: dict[str, Any] = {
    "task_status": TaskStatus.COMPLETED,
    "verification_state": "PASS",
    "evidence_coverage": 1.0,
    "latency_ms": 100.0,
    "cost_units": 5.0,
    "schema_valid": True,
    "rca_match": True,
    "abstention_expected": True,
    "abstained": False,
    "abstention_correct": False,
    "unsafe_action_attempted": True,
    "unsafe_action_blocked": True,
    "repair_attempted": True,
    "repair_target_ok": True,
}

# 模拟「无输出 / 无越权机会 / 未发起修复 / 未采集证据与延迟」的 2 个 case。
_SPARSE_FIELDS: dict[str, Any] = {
    "task_status": TaskStatus.COMPLETED,
    "verification_state": "PASS",
    "evidence_coverage": None,
    "latency_ms": None,
    "cost_units": None,
    "schema_valid": None,
    "rca_match": None,
    "abstention_expected": None,
    "abstained": False,
    "abstention_correct": None,
    "unsafe_action_attempted": False,
    "unsafe_action_blocked": None,
    "repair_attempted": False,
    "repair_target_ok": None,
}


def _case(case_id: str, fault_seed: int, **fields: Any) -> CaseResult:
    payload: dict[str, Any] = {
        "case_id": case_id,
        "engine": "mysql",
        "scenario": "slow_query",
        "fault_seed": fault_seed,
        "repetition": 1,
        "execution_mode": "HARNESS",
        "injection_state": InjectionState.INJECTED,
        "collection_status": CollectionStatus.SUCCESS,
        "tool_sequence": (),
        "tool_sequence_digest": "",
    }
    payload.update(fields)
    return CaseResult.model_validate(payload)


def _ac06_cases() -> list[CaseResult]:
    cases: list[CaseResult] = []
    for index in range(1, 6):
        cases.append(_case(f"normal-{index}", 4100 + index, **_FULL_FIELDS))
    for index in (6, 7):
        cases.append(_case(f"normal-{index}", 4100 + index, **_SPARSE_FIELDS))
    # 2 环境故障（ENV-PG5432）+ 1 注入失败 → 共 3 条进 D_env。
    for index in (8, 9):
        cases.append(
            _case(
                f"env-{index}",
                4200 + index,
                engine="postgresql",
                env_blocked=True,
                excluded_reason="ENV-PG5432",
                injection_state=InjectionState.UNKNOWN,
                collection_status=CollectionStatus.UNKNOWN,
            )
        )
    cases.append(
        _case(
            "injection-failed",
            4301,
            injection_state=InjectionState.FAILED,
            collection_status=CollectionStatus.SUCCESS,
        )
    )
    return cases


def _bad_definitions(mutator: Any) -> None:
    definitions = dict(DEFAULT_DEFINITIONS)
    mutator(definitions)
    with pytest.raises(BenchmarkInvalidError) as caught:
        validate_definitions(definitions)
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID


# --------------------------------------------------------------------------- #
# 名称集合 / 冻结件
# --------------------------------------------------------------------------- #


def test_metric_name_sets_are_frozen() -> None:
    assert len(CORE_METRIC_NAMES) == 8
    assert len(SUPPLEMENTARY_METRIC_NAMES) == 6
    assert len(ALL_METRIC_NAMES) == 14
    assert set(ALL_METRIC_NAMES) == set(CORE_METRIC_NAMES) | set(
        SUPPLEMENTARY_METRIC_NAMES
    )
    assert len(set(ALL_METRIC_NAMES)) == 14
    assert METRICS_VERSION == "metrics-v1"
    assert {member.value for member in Attribution} == {
        "CORRECT_ABSTENTION",
        "CAPABILITY_FAILURE",
        "DIAGNOSIS_ERROR",
        "ENVIRONMENT_FAULT",
    }


def test_frozen_json_matches_code_definitions_word_for_word() -> None:
    payload = json.loads(DEFAULT_METRICS_PATH.read_text(encoding="utf-8"))
    assert payload["version"] == METRICS_VERSION
    assert tuple(payload["core_metric_names"]) == CORE_METRIC_NAMES
    assert tuple(payload["supplementary_metric_names"]) == SUPPLEMENTARY_METRIC_NAMES
    loaded = load_metric_definitions(DEFAULT_METRICS_PATH)
    assert set(loaded) == set(ALL_METRIC_NAMES)
    for name, definition in loaded.items():
        expected = DEFAULT_DEFINITIONS[name]
        assert definition.name == expected.name
        assert definition.version == expected.version
        assert definition.numerator == expected.numerator
        assert definition.denominator == expected.denominator
        assert definition.exclusions == expected.exclusions
        assert definition.unit == expected.unit
        assert definition.higher_is_better == expected.higher_is_better


def test_metrics_engine_from_file_validates_frozen_definitions() -> None:
    engine = MetricsEngine.from_file(DEFAULT_METRICS_PATH)
    assert set(engine.definitions) == set(ALL_METRIC_NAMES)
    assert engine.compute([]).total_cases == 0


def test_definition_missing_extra_or_wrong_version_is_rejected() -> None:
    _bad_definitions(lambda d: d.pop("rca_top1"))
    _bad_definitions(lambda d: d.update({"fabricated_metric": d["rca_top1"]}))
    _bad_definitions(
        lambda d: d.update(
            {
                "rca_top1": MetricDefinition(
                    name="rca_top1",
                    version="metrics-v2",
                    numerator="x",
                    denominator="y",
                    exclusions="z",
                    unit="ratio",
                    higher_is_better=True,
                )
            }
        )
    )


def test_frozen_json_rejects_missing_definition_and_wrong_version(
    tmp_path: Path,
) -> None:
    payload = json.loads(DEFAULT_METRICS_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "metrics.json"
    payload["definitions"] = payload["definitions"][:-1]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BenchmarkInvalidError) as caught:
        load_metric_definitions(path)
    assert "missing" in caught.value.details

    payload = json.loads(DEFAULT_METRICS_PATH.read_text(encoding="utf-8"))
    payload["version"] = "metrics-v2"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BenchmarkInvalidError):
        MetricsEngine.from_file(path)


# --------------------------------------------------------------------------- #
# AC-06 分母与排除规则
# --------------------------------------------------------------------------- #


def test_ac06_ten_cases_denominators_match_frozen_expectations() -> None:
    cases = _ac06_cases()
    assert len(cases) == 10
    result = MetricsEngine().compute(cases)
    assert result.total_cases == 10
    assert result.excluded_count == 3  # 2 环境故障 + 1 注入失败
    assert set(result.denominators) == set(ALL_METRIC_NAMES)
    for name, expected in EXPECTED_DENOMINATORS.items():
        assert result.denominators[name] == expected, name
    # 环境故障 case 不得进入任一模型指标的分子/分母（AC-17③ 方向一致性）。
    assert result.denominators["task_success_rate"] == 7
    assert all(value <= 7 for value in result.denominators.values())
    assert result.effective_samples == 7


def test_ac06_environment_cases_are_in_d_env() -> None:
    cases = _ac06_cases()
    excluded = [case for case in cases if is_env_excluded(case)]
    assert len(excluded) == 3
    assert {case.case_id for case in excluded} == {
        "env-8",
        "env-9",
        "injection-failed",
    }
    for case in cases:
        if case.case_id.startswith("env-"):
            assert no_observable_fault(case) is True
            assert MetricsEngine.attribute(case) is Attribution.ENVIRONMENT_FAULT
        else:
            assert no_observable_fault(case) is False


def test_no_observable_fault_requires_both_sides_unknown() -> None:
    """D-05 收窄口径：只有「二者皆」NOT_SUPPORTED/UNKNOWN 才命中。"""

    case = _case("both", 1, injection_state=InjectionState.UNKNOWN,
                 collection_status=CollectionStatus.UNKNOWN)
    assert no_observable_fault(case) is True
    assert is_env_excluded(case) is True
    case = _case("only-injection", 2, injection_state=InjectionState.UNKNOWN,
                 collection_status=CollectionStatus.SUCCESS)
    assert no_observable_fault(case) is False
    case = _case("only-collection", 3, injection_state=InjectionState.INJECTED,
                 collection_status=CollectionStatus.NOT_SUPPORTED)
    assert no_observable_fault(case) is False
    case = _case("not-supported-pair", 4, injection_state=InjectionState.NOT_SUPPORTED,
                 collection_status=CollectionStatus.NOT_SUPPORTED)
    assert no_observable_fault(case) is True


# --------------------------------------------------------------------------- #
# 分母为 0 → None（R-02）
# --------------------------------------------------------------------------- #


def test_zero_denominator_yields_none_never_zero() -> None:
    cases = [
        _case(
            "blocked",
            1,
            env_blocked=True,
            excluded_reason="ENV-PG5432",
            injection_state=InjectionState.UNKNOWN,
            collection_status=CollectionStatus.UNKNOWN,
        )
    ]
    result = MetricsEngine().compute(cases)
    assert result.excluded_count == 1
    assert result.total_cases == 1
    assert all(value == 0 for value in result.denominators.values())
    assert all(value is None for value in result.values.values()), result.values
    # 明确排除「0 / 0.0 冒充」。
    assert all(value != 0 and value != 0.0 for value in result.values.values())
    assert result.denominators["regression_rate"] == 0


def test_regression_rate_and_cost_without_source_are_none() -> None:
    result = MetricsEngine().compute(_ac06_cases())
    assert result.values["regression_rate"] is None
    assert result.denominators["regression_rate"] == 0
    # 有 verified case 缺 cost_units（成本无来源）→ 值 None，不填 0。
    assert result.values["cost_per_verified_task"] is None
    assert result.denominators["cost_per_verified_task"] == 7


def test_task_p95_ms_uses_percentile_and_empty_sample_is_none() -> None:
    result = MetricsEngine().compute(_ac06_cases())
    assert result.denominators["task_p95_ms"] == 5
    assert result.values["task_p95_ms"] == 100.0  # percentile([100.0]*5, 95)
    empty = MetricsEngine().compute(
        [_case("no-latency", 1, **_SPARSE_FIELDS)]
    )
    assert empty.denominators["task_p95_ms"] == 0
    assert empty.values["task_p95_ms"] is None  # 不抛 REQUEST_INVALID、不填 0


# --------------------------------------------------------------------------- #
# AC-07 重复 seed 不独立计数
# --------------------------------------------------------------------------- #


def test_ac07_repeated_seed_increments_effective_samples_only_once() -> None:
    single = MetricsEngine().compute([_case("c1", 7, repetition=1)])
    assert single.effective_samples == 1
    repeated = MetricsEngine().compute(
        [_case(f"c{i}", 7, repetition=i) for i in (1, 2, 3)]
    )
    assert repeated.total_cases == 3
    assert repeated.effective_samples == 1  # 只 +1
    # 换个 seed 才 +1；环境故障 case 不计入有效样本。
    mixed = MetricsEngine().compute(
        [
            _case("c1", 7, repetition=1),
            _case("c2", 7, repetition=2),
            _case("c3", 8, repetition=1),
            _case(
                "blocked",
                9,
                env_blocked=True,
                excluded_reason="ENV-PG5432",
                injection_state=InjectionState.UNKNOWN,
                collection_status=CollectionStatus.UNKNOWN,
            ),
        ]
    )
    assert mixed.total_cases == 4
    assert mixed.excluded_count == 1
    assert mixed.effective_samples == 2


# --------------------------------------------------------------------------- #
# AC-08 不编造指标
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [RunStatus.NOT_RUN, RunStatus.RUNNING, RunStatus.BLOCKED]
)
def test_ac08_run_result_scores_and_metrics_none_before_terminal(
    status: RunStatus,
) -> None:
    result = RunResult(run_id="run_metrics", status=status)
    assert result.scores is None and result.metrics is None
    for field in ("scores", "metrics"):
        with pytest.raises(ValidationError):
            RunResult(run_id="run_metrics", status=status, **{field: {"x": 0.0}})


def test_ac08_metrics_are_not_fabricated_when_no_cases() -> None:
    result = MetricsEngine().compute([])
    assert result.total_cases == 0
    assert result.excluded_count == 0
    assert result.effective_samples == 0
    assert all(value is None for value in result.values.values())
    assert all(value == 0 for value in result.denominators.values())


# --------------------------------------------------------------------------- #
# AC-09 四类归因
# --------------------------------------------------------------------------- #


def test_ac09_attribution_counts_sum_to_total_cases() -> None:
    result = MetricsEngine().compute(_ac06_cases())
    assert set(result.attribution_counts) == {member.value for member in Attribution}
    assert sum(result.attribution_counts.values()) == result.total_cases == 10
    assert result.attribution_counts["ENVIRONMENT_FAULT"] == 3


def test_attribute_four_step_decision_tree() -> None:
    env = _case(
        "env", 1,
        env_blocked=True, excluded_reason="ENV-PG5432",
        injection_state=InjectionState.UNKNOWN,
        collection_status=CollectionStatus.UNKNOWN,
    )
    abstain = _case("abstain", 2, abstained=True, abstention_correct=True)
    diagnosis = _case("diagnosis", 3, schema_valid=True, rca_match=False)
    capability = _case("capability", 4, schema_valid=False, rca_match=None)
    assert MetricsEngine.attribute(env) is Attribution.ENVIRONMENT_FAULT
    assert MetricsEngine.attribute(abstain) is Attribution.CORRECT_ABSTENTION
    assert MetricsEngine.attribute(diagnosis) is Attribution.DIAGNOSIS_ERROR
    assert MetricsEngine.attribute(capability) is Attribution.CAPABILITY_FAILURE
    # 首个命中：环境故障优先于弃权/诊断。
    assert MetricsEngine.attribute(
        _case(
            "both", 5, env_blocked=True, excluded_reason="ENV-PG5432",
            injection_state=InjectionState.UNKNOWN,
            collection_status=CollectionStatus.UNKNOWN,
            abstained=True, abstention_correct=True,
        )
    ) is Attribution.ENVIRONMENT_FAULT


def test_attribute_never_reads_oracle_truth_text() -> None:
    source = (ROOT / "core" / "benchmark" / "metrics.py").read_text(encoding="utf-8")
    for forbidden in ("expected_root_cause", "injection_params", "truth_assertions"):
        assert forbidden not in source, forbidden
    assert "CaseResult" in source


# --------------------------------------------------------------------------- #
# 复用与禁止项
# --------------------------------------------------------------------------- #


def test_reuse_points_and_no_external_stats_dependencies() -> None:
    import ast

    metrics_src = (ROOT / "core" / "benchmark" / "metrics.py").read_text(
        encoding="utf-8"
    )
    stats_src = (ROOT / "core" / "benchmark" / "stats.py").read_text(encoding="utf-8")
    assert "from core.verification.perf import percentile" in metrics_src
    assert "from core.verification.perf import percentile" in stats_src
    assert "from core.security.canonical import canonical_digest" in stats_src
    for source in (metrics_src, stats_src):
        assert "def percentile(" not in source
        assert "def canonical_digest(" not in source
    # 禁用实现的检查落在**真实 import 节点**上（docstring 提及禁用品不算依赖）。
    banned = {"numpy", "scipy", "statistics"}
    for source in (metrics_src, stats_src):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert not (modules & banned), modules & banned


def test_t03_does_not_create_t06_metrics_definition_doc() -> None:
    """``docs/w10/metrics-definition.md`` 是 T06 交付项（Q-W10-D），本卡不提前写。"""

    # 主理人裁定 W10-CORR-01：本断言原为「磁盘全局不存在」的时序/范围守卫，
    # T06 交付件正常落地后前提消失。改为所有权/时序中性的三分支守卫：
    # ① 收窄为「T03 独占文件清单不含该路径」；② 文档不存在 → PASS；
    # ③ 文档存在 → 必须证明它确属 T06 交付件（14 项口径逐字一致）否则 FAIL。
    doc_relative = "docs/w10/metrics-definition.md"
    doc_path = ROOT / doc_relative

    # ① T03 卡声明的 5 个独占交付文件清单（设计 §13.2 独占矩阵）不含该路径。
    t03_exclusive_files: tuple[str, ...] = (
        "core/benchmark/metrics.py",
        "core/benchmark/stats.py",
        "benchmarks/w10/metrics-v1.json",
        "tests/unit/test_w10_metrics.py",
        "tests/unit/test_w10_bootstrap.py",
    )
    assert len(t03_exclusive_files) == 5
    assert doc_relative not in t03_exclusive_files
    # 5 个 T03 交付文件必须真实落盘，避免守卫落空（强度只增不减）。
    for relative in t03_exclusive_files:
        assert (ROOT / relative).is_file(), relative

    # ② 文档不存在 → PASS：保留 T03 单独跑（T06 未落地）的历史语义，
    #    不引入对 T06 交付物的硬依赖，也不删用例 / 加 skip / xfail。
    if not doc_path.exists():
        return

    # ③ 文档存在 → 必须证明它确属 T06 交付件，不对空文件或占位文件放行。
    text = doc_path.read_text(encoding="utf-8")
    payload = json.loads(DEFAULT_METRICS_PATH.read_text(encoding="utf-8"))
    definitions = payload["definitions"]
    assert payload["version"] == METRICS_VERSION
    assert len(CORE_METRIC_NAMES) == 8
    assert len(SUPPLEMENTARY_METRIC_NAMES) == 6
    assert len(ALL_METRIC_NAMES) == 14
    assert len(definitions) == 14
    assert tuple(payload["core_metric_names"]) == CORE_METRIC_NAMES
    assert tuple(payload["supplementary_metric_names"]) == SUPPLEMENTARY_METRIC_NAMES
    # 14 个指标名（8 核心 + 6 补充）在文档中齐全。
    for name in ALL_METRIC_NAMES:
        assert f"`{name}`" in text, name
    # execution_mode=HARNESS 与「数值留空」口径必须写明（不编造指标）。
    assert "execution_mode=HARNESS" in text
    assert "模型侧实测指标一律 `null`" in text
    assert "本口径的数值栏刻意留空" in text
    # 文档内 14 项定义与冻结件 metrics-v1.json 逐字一致（复用 T03 比对口径）。
    for row in definitions:
        assert row["name"] in ALL_METRIC_NAMES, row["name"]
        for field in ("numerator", "denominator", "exclusions", "version", "unit"):
            assert row[field] in text, f"{row['name']}.{field}"
