"""W10 T06 单测：Baseline 报告 / 失败归因 / 反编造（AC-17 + 口径文档）。

零 DB、零网络；冻结资产只读；不放宽断言、不加 skip/xfail。

覆盖：

- AC-17① 四类归因之和 == ``total_cases``（``build_baseline_report`` 内断言）；
- AC-17② ``model_commit is None`` ⇒ ``metrics is None`` ∧ ``blocked_reason``
  非空，由模型校验器强制、构造不可绕过；
- AC-17③ 环境故障 case → ``Attribution.ENVIRONMENT_FAULT``，且不计入
  ``false_diagnosis_rate`` 分子（``D_env`` 排除集，用例显式构造并断言）；
- ``FailureCase`` 只有 4 个判定事实字段；``EnvironmentBlock.code`` 为 5 项闭集；
- ``assert_no_fabricated_metrics``：分母 0 有数值 / 模型缺席有 metrics /
  「预计 / 估计 / 约」措辞 / 真值字段名 / SQL 原文一律 ``BENCHMARK_INVALID``；
- ``baseline_report.json``：诚实版实例通过 ``BaselineReport.model_validate``
  与 ``assert_no_fabricated_metrics``；``benchmarks/baseline_v0/results.json``
  冻结资产零改动；
- ``docs/w10/metrics-definition.md``：14 项逐条有分子/分母/排除规则/版本/单位，
  与 ``metrics-v1.json`` 逐字一致、无实测数值；
- 复用点 / 依赖方向 / Oracle 隔离 / 表述禁令静态守护。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.manifest import RunStatus
from core.benchmark.metrics import (
    ALL_METRIC_NAMES,
    CORE_METRIC_NAMES,
    METRICS_VERSION,
    SUPPLEMENTARY_METRIC_NAMES,
    Attribution,
    MetricsEngine,
)
from core.benchmark.report import (
    BLOCKED_REASON_MODEL_ENV,
    ENVIRONMENT_BLOCK_CODES,
    FABRICATION_PHRASES,
    FORBIDDEN_REPORT_LITERALS,
    REPORT_VERSION,
    BaselineReport,
    EnvironmentBlock,
    FailureCase,
    SftGate,
    assert_no_fabricated_metrics,
    build_baseline_report,
)
from core.benchmark.runner import (
    KNOWN_ENV_BLOCKERS,
    CaseResult,
    InjectionState,
    RunResult,
)
from core.errors import BenchmarkInvalidError, ErrorCode
from core.evidence.models import CollectionStatus

ROOT = Path(__file__).resolve().parents[2]
REPORT_MODULE = ROOT / "core" / "benchmark" / "report.py"
REPORT_JSON = ROOT / "benchmarks" / "w10" / "baseline_report.json"
METRICS_JSON = ROOT / "benchmarks" / "w10" / "metrics-v1.json"
DEFINITION_DOC = ROOT / "docs" / "w10" / "metrics-definition.md"
FROZEN_RESULTS = ROOT / "benchmarks" / "baseline_v0" / "results.json"

FORBIDDEN_TRUTH_LITERALS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
)


def _case(case_id: str, seed: int, **fields: Any) -> CaseResult:
    payload: dict[str, Any] = {
        "case_id": case_id,
        "engine": "mysql",
        "scenario": "slow_query",
        "fault_seed": seed,
        "repetition": 1,
        "execution_mode": "HARNESS",
        "injection_state": InjectionState.INJECTED,
        "collection_status": CollectionStatus.SUCCESS,
        "tool_sequence": (),
        "tool_sequence_digest": "",
    }
    payload.update(fields)
    return CaseResult.model_validate(payload)


def _ac17_cases() -> list[CaseResult]:
    """正常 5 + 环境故障 2 + 注入失败 1 + 正确拒绝 1（含模型侧可比 case）。"""

    cases: list[CaseResult] = []
    for index in range(1, 4):
        cases.append(
            _case(
                f"ok-{index}",
                4100 + index,
                task_status="COMPLETED",
                verification_state="PASS",
                schema_valid=True,
                rca_match=True,
            )
        )
    # 诊断错误：进入 false_diagnosis_rate 分子（1 条）。
    cases.append(
        _case(
            "diag-1",
            4104,
            task_status="COMPLETED",
            schema_valid=True,
            rca_match=False,
        )
    )
    # 正确拒绝：不进 failure_cases。
    cases.append(
        _case(
            "abstain-1",
            4105,
            abstained=True,
            abstention_correct=True,
            abstention_expected=True,
        )
    )
    # 2 条环境故障（ENV-PG5432）+ 1 条注入失败 → D_env，绝不进 false_diagnosis_rate。
    for index in (6, 7):
        cases.append(
            _case(
                f"env-{index}",
                4200 + index,
                engine="postgresql",
                env_blocked=True,
                excluded_reason="ENV-PG5432",
                injection_state=InjectionState.UNKNOWN,
                collection_status=CollectionStatus.UNKNOWN,
                schema_valid=True,
                rca_match=False,
            )
        )
    cases.append(
        _case(
            "injection-failed",
            4301,
            injection_state=InjectionState.FAILED,
            collection_status=CollectionStatus.SUCCESS,
            schema_valid=True,
            rca_match=False,
        )
    )
    return cases


def _build(cases: list[CaseResult]) -> tuple[BaselineReport, Any]:
    metrics = MetricsEngine().compute(cases)
    result = RunResult(
        run_id="run_w10_t06",
        status=RunStatus.COMPLETED,
        cases=tuple(cases),
    )
    return build_baseline_report(result=result, metrics=metrics), metrics


# --------------------------------------------------------------------------- #
# 常量 / 模型契约
# --------------------------------------------------------------------------- #


def test_report_constants_and_closed_models() -> None:
    assert REPORT_VERSION == "w10-baseline-v1"
    assert METRICS_VERSION == "metrics-v1"
    assert set(ENVIRONMENT_BLOCK_CODES) == KNOWN_ENV_BLOCKERS
    assert len(ENVIRONMENT_BLOCK_CODES) == 5
    assert set(FABRICATION_PHRASES) == {"预计", "估计", "约"}
    assert {member.value for member in Attribution} == {
        "CORRECT_ABSTENTION",
        "CAPABILITY_FAILURE",
        "DIAGNOSIS_ERROR",
        "ENVIRONMENT_FAULT",
    }
    with pytest.raises(ValidationError):
        BaselineReport.model_validate({"report_version": "w10-baseline-v2"})
    with pytest.raises(ValidationError):
        FailureCase.model_validate(
            {
                "case_id": "x",
                "attribution": "ENVIRONMENT_FAULT",
                "reason": "ENVIRONMENT_FAULT",
                "tool_sequence_digest": "abc",
                "expected_root_cause": "leak",
            }
        )
    with pytest.raises(ValidationError):
        EnvironmentBlock.model_validate(
            {"code": "ENV-UNKNOWN", "statement": "x"}
        )


def test_failure_case_has_exactly_four_fields() -> None:
    assert set(FailureCase.model_fields) == {
        "case_id",
        "attribution",
        "reason",
        "tool_sequence_digest",
    }
    assert "fault_seed" not in FailureCase.model_fields
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in FailureCase.model_fields


# --------------------------------------------------------------------------- #
# AC-17① 四类归因之和
# --------------------------------------------------------------------------- #


def test_ac17_attribution_counts_sum_to_total_cases() -> None:
    cases = _ac17_cases()
    report, metrics = _build(cases)
    assert set(report.attribution_counts) == {
        member.value for member in Attribution
    }
    assert sum(report.attribution_counts.values()) == report.total_cases == 8
    assert report.total_cases == metrics.total_cases
    assert report.attribution_counts == metrics.attribution_counts
    assert report.attribution_counts["ENVIRONMENT_FAULT"] == 3
    assert report.attribution_counts["CORRECT_ABSTENTION"] == 1


def test_ac17_build_rejects_inconsistent_attribution_counts() -> None:
    cases = _ac17_cases()
    metrics = MetricsEngine().compute(cases)
    broken = metrics.__class__(
        version=metrics.version,
        values=dict(metrics.values),
        denominators=dict(metrics.denominators),
        numerators=dict(metrics.numerators),
        excluded_count=metrics.excluded_count,
        total_cases=999,
        effective_samples=metrics.effective_samples,
        attribution_counts=dict(metrics.attribution_counts),
    )
    result = RunResult(run_id="run_bad", status=RunStatus.COMPLETED, cases=tuple(cases))
    with pytest.raises(BenchmarkInvalidError) as caught:
        build_baseline_report(result=result, metrics=broken)
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID


# --------------------------------------------------------------------------- #
# AC-17② 模型缺席不编造
# --------------------------------------------------------------------------- #


def test_ac17_no_model_means_no_metrics_and_blocked_reason() -> None:
    report, _ = _build(_ac17_cases())
    assert report.model_commit is None  # baseline_manifest.model_commit = null
    assert report.tokenizer_commit is None
    assert report.metrics is None
    assert report.confidence_intervals is None
    assert report.cost is None
    assert report.blocked_reason == BLOCKED_REASON_MODEL_ENV
    assert report.blocked_reason
    # harness 可重放 ≠ 模型评测完成：status 必须 BLOCKED。
    assert report.status is RunStatus.BLOCKED
    assert report.model_name == "Qwen3-8B"
    assert report.harness_version == "44e4f9d"
    assert report.testset_version == "testset-v0-preview"
    assert_no_fabricated_metrics(report)


@pytest.mark.parametrize("metric_value", [0.5, 0.0, 1.0])
def test_ac17_validator_rejects_metrics_when_model_absent(
    metric_value: float,
) -> None:
    base, _ = _build(_ac17_cases())
    payload = base.model_dump(mode="json")
    payload["metrics"] = {"task_success_rate": metric_value}
    with pytest.raises(ValidationError) as caught:
        BaselineReport.model_validate(payload)
    assert "metrics 必须为 None" in str(caught.value)


def test_ac17_validator_rejects_empty_blocked_reason() -> None:
    base, _ = _build(_ac17_cases())
    payload = base.model_dump(mode="json")
    payload["blocked_reason"] = None
    with pytest.raises(ValidationError) as caught:
        BaselineReport.model_validate(payload)
    assert "blocked_reason 必须非空" in str(caught.value)
    payload["blocked_reason"] = "   "
    with pytest.raises(ValidationError):
        BaselineReport.model_validate(payload)


def test_assert_no_fabricated_metrics_catches_model_construct_bypass() -> None:
    """``model_copy`` 绕过 pydantic 校验器 → 独立反编造闸门必须拦住。"""

    base, _ = _build(_ac17_cases())
    forged = base.model_copy(update={"metrics": {"task_success_rate": 0.9}})
    with pytest.raises(BenchmarkInvalidError) as caught:
        assert_no_fabricated_metrics(forged)
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID


# --------------------------------------------------------------------------- #
# AC-17③ 环境故障不计入模型失败
# --------------------------------------------------------------------------- #


def test_ac17_environment_faults_are_attributed_and_excluded() -> None:
    cases = _ac17_cases()
    report, metrics = _build(cases)
    env_ids = {c.case_id for c in cases if c.env_blocked}

    # 环境故障 case 的 attribution 必为 ENVIRONMENT_FAULT。
    for case in cases:
        if case.env_blocked:
            assert MetricsEngine.attribute(case) is Attribution.ENVIRONMENT_FAULT
    failure_by_id = {item.case_id: item for item in report.failure_cases}
    for env_id in env_ids:
        assert failure_by_id[env_id].attribution is Attribution.ENVIRONMENT_FAULT
        assert failure_by_id[env_id].reason.startswith("ENVIRONMENT_FAULT")
    assert report.attribution_counts["ENVIRONMENT_FAULT"] == 3

    # 显式构造并断言：3 条 D_env case 均带 rca_match=False，却未进分子/分母。
    assert all(
        case.rca_match is False
        for case in cases
        if case.env_blocked or case.injection_state is InjectionState.FAILED
    )
    assert metrics.numerators["false_diagnosis_rate"] == 1
    assert metrics.denominators["false_diagnosis_rate"] == 4
    assert report.metric_denominators["false_diagnosis_rate"] == 4
    assert report.attribution_counts["DIAGNOSIS_ERROR"] == 1

    # 环境阻塞单列，且代码在闭集内。
    assert [block.code for block in report.environment_blocked] == [
        "ENV-PG5432",
        "ENV-MODEL",
    ]
    pg = report.environment_blocked[0]
    assert set(pg.affected_cases) == env_ids
    assert pg.statement
    model_block = report.environment_blocked[1]
    assert model_block.code == "ENV-MODEL"
    assert "BLOCKED_MODEL_ENV_MISSING" in model_block.statement


def test_failure_cases_never_carry_oracle_truth_or_sql() -> None:
    report, _ = _build(_ac17_cases())
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in report.failure_cases],
        ensure_ascii=False,
    )
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in serialized
    for literal in FORBIDDEN_REPORT_LITERALS:
        assert literal not in serialized
    assert "fault_seed" not in serialized
    # 正确拒绝的 case 不进 failure_cases。
    assert "abstain-1" not in {item.case_id for item in report.failure_cases}
    # tool_sequence_digest 只传摘要，不传工具序列原文。
    for item in report.failure_cases:
        assert item.tool_sequence_digest == ""


def test_safe_failure_reason_never_copies_case_exception_text() -> None:
    """case.failure_reason 原文可能含参数值 → 只登记判定事实。"""

    cases = [
        _case(
            "boom-1",
            4401,
            failure_reason="RuntimeError: SELECT * FROM orders WHERE id=42",
        )
    ]
    report, _ = _build(cases)
    item = next(f for f in report.failure_cases if f.case_id == "boom-1")
    assert item.reason == "CASE_EXCEPTION"
    assert "SELECT" not in item.reason
    assert "42" not in item.reason
    assert_no_fabricated_metrics(report)


# --------------------------------------------------------------------------- #
# assert_no_fabricated_metrics
# --------------------------------------------------------------------------- #


def test_assert_no_fabricated_metrics_rejects_zero_denominator_with_value() -> None:
    base, _ = _build(_ac17_cases())
    forged = base.model_copy(
        update={
            "model_commit": "sha256:probe",
            "metrics": {"task_success_rate": 0.75},
            "metric_denominators": {"task_success_rate": 0},
        }
    )
    with pytest.raises(BenchmarkInvalidError) as caught:
        assert_no_fabricated_metrics(forged)
    assert "分母为 0" in caught.value.message


def test_assert_no_fabricated_metrics_rejects_unknown_metric_and_missing_denoms() -> None:
    base, _ = _build(_ac17_cases())
    with pytest.raises(BenchmarkInvalidError):
        assert_no_fabricated_metrics(
            base.model_copy(
                update={
                    "model_commit": "sha256:probe",
                    "metrics": {"fabricated_metric": 0.5},
                    "metric_denominators": {"fabricated_metric": 3},
                }
            )
        )
    with pytest.raises(BenchmarkInvalidError):
        assert_no_fabricated_metrics(
            base.model_copy(
                update={
                    "model_commit": "sha256:probe",
                    "metrics": {"task_success_rate": 0.5},
                    "metric_denominators": None,
                }
            )
        )


@pytest.mark.parametrize("note", ["task_p95_ms 预计 120ms", "指标估计 0.85", "约 95%"])
def test_assert_no_fabricated_metrics_rejects_estimate_wording(note: str) -> None:
    base, _ = _build(_ac17_cases())
    forged = base.model_copy(update={"notes": (note,)})
    with pytest.raises(BenchmarkInvalidError) as caught:
        assert_no_fabricated_metrics(forged)
    assert "估计措辞" in caught.value.message


@pytest.mark.parametrize(
    "note",
    [
        "expected_root_cause=缺少复合索引",
        "injection_params={...}",
        "SELECT * FROM orders",
        "mysql://dg_ro:pw@host/db",
        "note fault_seed=17",
    ],
)
def test_assert_no_fabricated_metrics_rejects_truth_and_sql(note: str) -> None:
    base, _ = _build(_ac17_cases())
    forged = base.model_copy(update={"notes": (note,)})
    with pytest.raises(BenchmarkInvalidError) as caught:
        assert_no_fabricated_metrics(forged)
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID


def test_assert_no_fabricated_metrics_rejects_non_finite_value() -> None:
    base, _ = _build(_ac17_cases())
    forged = base.model_copy(
        update={
            "model_commit": "sha256:probe",
            "metrics": {"task_success_rate": float("nan")},
            "metric_denominators": {"task_success_rate": 5},
        }
    )
    with pytest.raises(BenchmarkInvalidError):
        assert_no_fabricated_metrics(forged)


# --------------------------------------------------------------------------- #
# baseline_report.json（诚实版实例）
# --------------------------------------------------------------------------- #


def test_baseline_report_json_is_honest_and_valid() -> None:
    """落盘报告必须**按自身状态自洽**：未上机 → BLOCKED 全 null；已上机 → 有实测值。

    P1-T2 起该文件由真实 AGENT 运行覆盖。两种状态各有一套判据，
    共同底线：四类归因之和 == total_cases、分母齐全、metric 有值当且仅当分母 > 0。
    """
    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    report = BaselineReport.model_validate(payload)
    assert set(payload) == set(BaselineReport.model_fields)
    assert report.report_version == REPORT_VERSION
    assert set(report.attribution_counts) == {member.value for member in Attribution}
    assert sum(report.attribution_counts.values()) == report.total_cases
    assert set(report.metric_denominators or {}) == set(ALL_METRIC_NAMES)
    assert_no_fabricated_metrics(report)

    if report.model_commit is None:
        # 未上机：一切模型侧指标必须为 null，不得出现任何数值（含 0）。
        assert report.status is RunStatus.BLOCKED
        assert report.tokenizer_commit is None
        assert report.metrics is None
        assert report.confidence_intervals is None
        assert report.cost is None
        assert report.blocked_reason == BLOCKED_REASON_MODEL_ENV
        assert report.total_cases == 0
        assert "ENV-MODEL" in [block.code for block in report.environment_blocked]
        assert report.failure_cases == ()
        assert all(value == 0 for value in report.metric_denominators.values())
        return

    # 已上机（AGENT 实测）：status 必须是真实终态，指标只收真实测得值。
    assert report.status is not RunStatus.NOT_RUN
    assert report.status is not RunStatus.BLOCKED
    assert report.blocked_reason is None
    assert report.metrics is not None
    assert report.total_cases > 0
    for name in ALL_METRIC_NAMES:
        denominator = report.metric_denominators[name]
        if name in report.metrics:
            assert denominator > 0, f"{name} 有数值但分母为 0（编造）"
    # 置信区间不可估时必须显式说明（不伪造 lower==upper）。
    intervals = report.confidence_intervals or {}
    for name, interval in intervals.items():
        if interval.get("lower") is None or interval.get("upper") is None:
            assert any("置信区间不可估" in note for note in report.notes), name
    assert report.cost is None or report.cost.get("cost_units_unit") == "tokens"


def test_baseline_report_json_has_no_fabricated_or_forbidden_text() -> None:
    text = REPORT_JSON.read_text(encoding="utf-8")
    for phrase in (*FABRICATION_PHRASES, *FORBIDDEN_TRUTH_LITERALS):
        assert phrase not in text, phrase
    for literal in FORBIDDEN_REPORT_LITERALS:
        assert literal not in text
    for banned in (
        "上游已通过",
        "回归全绿",
        "环境已清零",
        "Alpha 已就绪",
        "门禁通过",
        "W10 开工",
    ):
        assert banned not in text


def test_frozen_baseline_v0_results_json_is_untouched() -> None:
    """T06 禁止改 results.json；冻结资产仍保持 NOT_RUN / scores=null（R-08）。"""

    payload = json.loads(FROZEN_RESULTS.read_text(encoding="utf-8"))
    assert payload["status"] == "NOT_RUN"
    assert payload["scores"] is None
    assert payload["metrics"] is None


# --------------------------------------------------------------------------- #
# metrics-definition.md（Q-W10-D：14 项逐条 + 三处逐字一致 + 无数值）
# --------------------------------------------------------------------------- #


def test_metrics_definition_doc_has_all_14_metrics() -> None:
    text = DEFINITION_DOC.read_text(encoding="utf-8")
    assert DEFINITION_DOC.is_file()
    for name in (*CORE_METRIC_NAMES, *SUPPLEMENTARY_METRIC_NAMES):
        assert f"`{name}`" in text, name
    assert len(CORE_METRIC_NAMES) == 8
    assert len(SUPPLEMENTARY_METRIC_NAMES) == 6
    assert len(ALL_METRIC_NAMES) == 14


def test_metrics_definition_doc_matches_frozen_json_word_for_word() -> None:
    """14 项定义字段逐字出现在文档中（三处逐字一致的机器可复核形式）。"""

    text = DEFINITION_DOC.read_text(encoding="utf-8")
    payload = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
    assert payload["version"] == METRICS_VERSION
    for row in payload["definitions"]:
        for field in ("numerator", "denominator", "exclusions", "version", "unit"):
            assert row[field] in text, f"{row['name']}.{field} 未逐字出现"
    assert tuple(payload["core_metric_names"]) == CORE_METRIC_NAMES
    assert tuple(payload["supplementary_metric_names"]) == SUPPLEMENTARY_METRIC_NAMES


def test_metrics_definition_doc_has_denominator_units_and_no_values() -> None:
    text = DEFINITION_DOC.read_text(encoding="utf-8")
    # 每项都有分母 / 排除规则 / 版本 / 单位列。
    for header in ("分子", "分母", "排除规则", "版本", "单位", "当前实测值"):
        assert header in text
    # cost_per_verified_task 分母声明 + 数值留空（P1-W10-03）。
    assert "|{c : task_status COMPLETED ∧ verification PASS}|" in text
    assert "本口径的数值栏刻意留空" in text
    assert "成本来源当前为 `UNKNOWN`" in text
    # 口径去重（8 → 6）与 HARNESS/ENV-MODEL 缺席口径。
    assert "不重复实现、不重复计数" in text
    assert "execution_mode=HARNESS" in text
    assert "BLOCKED_MODEL_ENV_MISSING" in text
    assert "模型侧实测指标一律 `null`" in text
    # 全文 14 行「当前实测值」列一律留空，不出现实测数值。
    value_rows = [
        row
        for row in text.splitlines()
        if row.startswith("| ")
        and any(f"`{name}`" in row for name in ALL_METRIC_NAMES)
        and "留空" in row
    ]
    assert len(value_rows) == 14, len(value_rows)
    assert "14 项" in text
    # 不得新增 PRD 未列指标。
    assert "不新增 PRD 未列指标" in text
    assert "预计 / 估计 / 约" not in text
    # 表述禁令（W07 权威短语的例外只在设计件，本文件不复述）。
    for banned in (
        "上游已通过",
        "回归全绿",
        "环境已清零",
        "Alpha 已就绪",
        "门禁通过",
        "W10 开工",
    ):
        assert banned not in text


def test_definition_doc_and_report_use_the_same_frozen_metric_set() -> None:
    payload = json.loads(METRICS_JSON.read_text(encoding="utf-8"))
    report_payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    assert set(report_payload["metric_denominators"]) == set(
        row["name"] for row in payload["definitions"]
    ) == set(ALL_METRIC_NAMES)


# --------------------------------------------------------------------------- #
# 复用点 / 依赖方向 / 表述禁令静态守护
# --------------------------------------------------------------------------- #


def test_report_module_reuses_t02_t03_t04_and_has_no_forbidden_imports() -> None:
    source = REPORT_MODULE.read_text(encoding="utf-8")
    for expected in (
        "from core.benchmark.manifest import RunManifest",
        "from core.benchmark.runner import",
        "from core.benchmark.metrics import",
        "from core.benchmark.oracle import scan_texts_for_oracle_leak",
        "from core.benchmark.dataset import SplitManifest",
        "from core.benchmark.tokens import TokenStats",
        "from core.errors import BenchmarkInvalidError",
    ):
        assert expected in source, expected
    for forbidden in (
        "def percentile(",
        "def canonical_digest(",
        "class RunResult",
        "class MetricSet",
        "class Attribution",
        "class RunStatus",
    ):
        assert forbidden not in source, forbidden
    # 「INSERT INTO」在本模块只作为 FORBIDDEN_REPORT_LITERALS 的检测模式出现；
    # 用 AST 断言真实不存在任何 DB 客户端调用（禁止直接 INSERT / 越权写）。
    tree = ast.parse(source)
    db_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "fetch_all", "fetch_one", "insert"}
    ]
    assert db_calls == [], "report.py 不得直接访问数据库"
    banned = {"apps", "workers", "benchmarks", "numpy", "scipy"}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & banned), modules & banned
    for forbidden in ("core.gateway", "core.approval", "core.agent"):
        assert f"from {forbidden}" not in source


def test_report_module_expression_ban() -> None:
    source = REPORT_MODULE.read_text(encoding="utf-8")
    # 只允许在「反编造词表常量 / 禁止项说明」中出现这些字面；不得作为宣称性表述。
    for banned in (
        "上游已通过",
        "回归全绿",
        "环境已清零",
        "Alpha 已就绪",
        "门禁通过",
    ):
        assert banned not in source, banned
    # 真值字面只允许出现在 FORBIDDEN_REPORT_LITERALS 这一份守卫清单里；
    # 报告产物侧由 test_baseline_report_json / assert_no_fabricated_metrics 守护。
    assert "FORBIDDEN_REPORT_LITERALS: tuple[str, ...]" in source
    assert source.count('"expected_root_cause"') == 1
    assert source.count('"injection_params"') == 1
    assert source.count('"truth_assertions"') == 1


def test_sft_gate_does_not_change_report_judgement() -> None:
    """W10-Q7：SftGate 只做记录，门禁判定不看它。"""

    cases = _ac17_cases()
    metrics = MetricsEngine().compute(cases)
    result = RunResult(run_id="run_sft", status=RunStatus.COMPLETED, cases=tuple(cases))
    without = build_baseline_report(result=result, metrics=metrics, sft=None)
    gate = SftGate(
        shortcoming_located=True,
        reproducible_gain=True,
        sample_count=20,
        harness_version="44e4f9d",
        testset_version="testset-v0-preview",
        baseline_harness_version="44e4f9d",
        baseline_testset_version="testset-v0-preview",
    )
    with_sft = build_baseline_report(result=result, metrics=metrics, sft=gate)
    assert with_sft.status is without.status
    assert with_sft.metrics == without.metrics
    assert with_sft.blocked_reason == without.blocked_reason
    assert with_sft.attribution_counts == without.attribution_counts
    assert with_sft.total_cases == without.total_cases
    assert with_sft.failure_cases == without.failure_cases
    assert with_sft.environment_blocked == without.environment_blocked
    assert with_sft.metric_denominators == without.metric_denominators
    # 差别只在一条记录性 note。
    assert len(with_sft.notes) == len(without.notes) + 1
    assert any("SftGate 记录" in note for note in with_sft.notes)
    assert_no_fabricated_metrics(with_sft)
