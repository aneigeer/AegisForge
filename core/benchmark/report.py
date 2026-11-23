"""W10 T06：Baseline 报告 + 失败归因 + 条件性 SFT 闸门（AC-17 / AC-18）。

口径来源：``docs/w10/03-task-cards.md`` T06（``:459-547``）、
``docs/w10/02-design.md`` §4.7 / §5.3 / §5.4 / §15；主理人裁定
W10-Q3（模型缺席如实 ``null``）/ W10-Q7（SFT 不纳入门禁）/ Q-W10-D（文档产出人）。

依赖方向（设计 §2.2 / §2.4，单向、可静态断言）：

- **复用、不重写**：T02 的 ``RunResult`` / ``RunStatus`` / ``RunManifest`` /
  ``load_suite`` / ``KNOWN_ENV_BLOCKERS``，T03 的 ``MetricSet`` / ``Attribution`` /
  ``MetricsEngine`` / ``ALL_METRIC_NAMES``，T01 的 ``scan_texts_for_oracle_leak``，
  T04 的 ``SplitManifest`` / ``TokenStats``（口径引用）；
- **禁止** import ``apps/**``、``workers/**``、``core/gateway/**``、
  ``core/approval/**``、``core/agent/**``；禁止顶层 import ``benchmarks.*``。

红线：

1. ``model_commit is None`` ⇒ ``metrics is None`` ∧ ``blocked_reason`` 非空，
   由模型校验器强制，不可绕过构造（AC-17②）；``assert_no_fabricated_metrics``
   再独立复核一遍（覆盖 ``model_construct`` 绕过路径）。
2. 分母为 0 却出现数值、``model_commit`` 缺席却出现指标、以及「预计/估计/约」
   充当实测值 → ``BENCHMARK_INVALID``（R-02 / 卡禁止项 7）。
3. ``FailureCase`` / ``EnvironmentBlock`` / 报告文本**绝不**携带 Oracle 真值、
   ``injection_params``、SQL 原文或参数值；只外泄判定事实与公开定位字段。
4. ``SftGate`` 的 ``dpo_allowed`` / ``rl_allowed`` / ``full_14b_allowed`` 恒
   ``False``（``ClassVar`` + 只读 property），实例赋值 / 字典注入都改不动
   （AC-18③）；``SftGate`` **只做记录**，不改变报告判定、不纳入 W10→W11 门禁口径。
5. 表述禁令：遵循 ``docs/w10/02-design.md`` §17 与
   ``docs/w10-w12-lead-rulings.md`` C-7 的冻结口径清单；不作任何阶段性
   开工、上游门禁签署、回归收口、环境清理或发布就绪的宣称。
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.benchmark.dataset import SplitManifest
from core.benchmark.manifest import RunManifest
from core.benchmark.metrics import (
    ALL_METRIC_NAMES,
    METRICS_VERSION,
    Attribution,
    MetricSet,
    MetricsEngine,
    is_env_excluded,
)
from core.benchmark.oracle import scan_texts_for_oracle_leak
from core.benchmark.runner import (
    KNOWN_ENV_BLOCKERS,
    DEFAULT_SUITE_PATH,
    CaseResult,
    RunResult,
    RunStatus,
    load_suite,
)
from core.benchmark.stats import cluster_bootstrap
from core.benchmark.tokens import TokenStats
from core.errors import BenchmarkInvalidError

# 报告结构版本（冻结）。
REPORT_VERSION: Literal["w10-baseline-v1"] = "w10-baseline-v1"

# 环境阻塞代码闭集（设计 §5.2 规则 6 / 卡 :522）；与 runner.KNOWN_ENV_BLOCKERS 同源。
ENVIRONMENT_BLOCK_CODES: tuple[str, ...] = (
    "ENV-PG5432",
    "ENV-MODEL",
    "ENV-RESIDUE",
    "ENV-1045",
    "ENV-NV1",
)
EnvironmentBlockCode = Literal[
    "ENV-PG5432", "ENV-MODEL", "ENV-RESIDUE", "ENV-1045", "ENV-NV1"
]

# 模型缺席时的诚实阻塞原因（ENV-MODEL / W10-Q3）。
BLOCKED_REASON_MODEL_ENV: str = (
    "BLOCKED_MODEL_ENV_MISSING：模型环境不可用，Baseline 未上机，"
    "模型侧实测指标未测得，一律 null（ENV-MODEL）"
)

# 环境阻塞项的固定说明（不含 Oracle 真值 / SQL 原文 / 参数值）。
_ENV_STATEMENTS: dict[str, str] = {
    "ENV-PG5432": (
        "PostgreSQL LAB 5432 拒绝连接；相关只读用例记 NOT_EXECUTED，"
        "归环境故障，不计入模型失败。"
    ),
    "ENV-MODEL": (
        "模型环境 BLOCKED_MODEL_ENV_MISSING（model_commit=null）；"
        "Baseline 未上机，模型侧指标未测得，如实留空。"
    ),
    "ENV-RESIDUE": (
        "控制库历史残留只追加、不清理；集成用例仅按本任务 id 断言，"
        "不统计全表，处置挂 W11。"
    ),
    "ENV-1045": (
        "DG_ADMIN_DSN root 认证失败 1045；测试不依赖 admin 清理，"
        "不修凭据、不放宽 dg_ctl。"
    ),
    "ENV-NV1": (
        "容器运行态 NOT_VERIFIED；env_image_digest 填 UNKNOWN，"
        "不臆造 digest、不声称容器已验证。"
    ),
}

# 失败原因的安全词汇表：只放判定事实，绝不复制 case.failure_reason 原文
# （原文可能含异常消息 / SQL 片段 / 参数值）。
_REASON_ENVIRONMENT_FAULT = "ENVIRONMENT_FAULT"
_REASON_CASE_EXCEPTION = "CASE_EXCEPTION"
_REASON_CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
_REASON_DIAGNOSIS_ERROR = "DIAGNOSIS_ERROR"

# 反编造扫描：报告文本中禁止出现「预计 / 估计 / 约」等充当实测值的措辞。
FABRICATION_PHRASES: tuple[str, ...] = ("预计", "估计", "约")

# 报告文本中禁止出现的 Oracle 真值 / SQL 原文标记（隔离红线）。
FORBIDDEN_REPORT_LITERALS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
    "SELECT ",
    "INSERT INTO",
    "mysql://",
    "postgresql://",
    "password=",
)

# T04 口径引用（设计 §2.2：report.py → dataset.py / tokens.py）。
_REFERENCED_T04_MODELS: tuple[str, str] = (
    SplitManifest.__name__,
    TokenStats.__name__,
)


class FailureCase(BaseModel):
    """一条失败案例；**只有**四个判定事实字段，不含任何真值 / SQL / 参数值。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    attribution: Attribution
    reason: str = Field(min_length=1)
    tool_sequence_digest: str


class EnvironmentBlock(BaseModel):
    """环境阻塞单列项；``code`` 为 5 项闭集（设计 §5.2）。"""

    model_config = ConfigDict(extra="forbid")

    code: EnvironmentBlockCode
    affected_cases: tuple[str, ...] = ()
    statement: str = Field(min_length=1)


class BaselineReport(BaseModel):
    """冻结 Harness 上的 Baseline 报告（设计 §4.7 / 卡 :469-487）。"""

    model_config = ConfigDict(extra="forbid")

    report_version: Literal["w10-baseline-v1"] = REPORT_VERSION
    run_id: str | None
    status: RunStatus
    model_name: str
    model_commit: str | None
    tokenizer_commit: str | None
    suite_version: str
    harness_version: str
    testset_version: str
    metrics: dict[str, float] | None
    metric_denominators: dict[str, int] | None
    confidence_intervals: dict[str, Any] | None
    attribution_counts: dict[str, int]
    total_cases: int
    failure_cases: tuple[FailureCase, ...] = ()
    environment_blocked: tuple[EnvironmentBlock, ...] = ()
    blocked_reason: str | None = None
    cost: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _no_metrics_without_model(self) -> "BaselineReport":
        """AC-17②：``model_commit is None`` ⇒ ``metrics is None`` ∧ 阻塞原因非空。

        由 pydantic 在**构造时**强制，任何正常构造路径（含 ``model_validate``）
        都无法绕过；``assert_no_fabricated_metrics`` 再覆盖 ``model_construct``
        绕过路径，形成双闸门。
        """

        if self.model_commit is None:
            if self.metrics is not None:
                raise ValueError(
                    "model_commit 为 None 时 metrics 必须为 None（禁止编造指标）"
                )
            if not self.blocked_reason or not self.blocked_reason.strip():
                raise ValueError(
                    "model_commit 为 None 时 blocked_reason 必须非空（ENV-MODEL 如实登记）"
                )
        return self


class SftGate(BaseModel):
    """条件性 SFT 记录闸门（设计 §4.7 / AC-18）。

    只做**条件性记录**，不改变 ``BaselineReport`` 的任何判定，也不纳入
    W10→W11 门禁口径（W10-Q7）。DPO / RL / 14B 完整训练全部推迟到 Alpha 之后，
    三个开关以类级 ``ClassVar`` + 只读 property 冻结，恒 ``False``。
    """

    model_config = ConfigDict(extra="forbid")

    shortcoming_located: bool
    reproducible_gain: bool = False
    sample_count: int = Field(ge=0, le=1000)
    harness_version: str
    testset_version: str
    baseline_harness_version: str
    baseline_testset_version: str

    DPO_ALLOWED: ClassVar[bool] = False
    RL_ALLOWED: ClassVar[bool] = False
    FULL_14B_ALLOWED: ClassVar[bool] = False

    @property
    def sft_allowed(self) -> bool:
        """AC-18①②：短板已定位 ∧ 可重复收益 ∧ 10..50 条 ∧ Harness/测试集逐字相等。

        越界（``sample_count < 10`` 或 ``> 50``）返回 ``False``，**不抛错**。
        """

        return (
            self.shortcoming_located
            and self.reproducible_gain
            and 10 <= self.sample_count <= 50
            and self.harness_version == self.baseline_harness_version
            and self.testset_version == self.baseline_testset_version
        )

    @property
    def dpo_allowed(self) -> bool:
        """恒 ``False``；返回字面常量，不读任何实例/类属性（杜绝注入路径）。"""

        return False

    @property
    def rl_allowed(self) -> bool:
        """恒 ``False``；返回字面常量，不读任何实例/类属性（杜绝注入路径）。"""

        return False

    @property
    def full_14b_allowed(self) -> bool:
        """恒 ``False``；返回字面常量，不读任何实例/类属性（杜绝注入路径）。"""

        return False


def _frozen_baseline_manifest() -> RunManifest:
    """从 T02 冻结资产构造版本清单（**不运行期新造版本字符串**）。

    ``suite_version`` 取自冻结测试集清单；``model_commit`` / ``tokenizer_commit``
    在 ``baseline_manifest.json`` 中当前为 ``null``（ENV-MODEL）。
    """

    suite = load_suite(DEFAULT_SUITE_PATH)
    return RunManifest.from_baseline(suite_version=suite.suite_version)


def _safe_failure_reason(case: CaseResult, attribution: Attribution) -> str:
    """把失败原因限制在安全词汇表内，绝不复制原文 / SQL / 参数值。"""

    if attribution is Attribution.ENVIRONMENT_FAULT:
        if case.excluded_reason in KNOWN_ENV_BLOCKERS:
            return f"{_REASON_ENVIRONMENT_FAULT}:{case.excluded_reason}"
        return _REASON_ENVIRONMENT_FAULT
    if case.failure_reason:
        # 异常原文可能携带参数值或 SQL 片段 → 只登记「发生了 case 级异常」这一事实。
        return _REASON_CASE_EXCEPTION
    if attribution is Attribution.DIAGNOSIS_ERROR:
        return _REASON_DIAGNOSIS_ERROR
    return _REASON_CAPABILITY_FAILURE


def _failure_cases(cases: Sequence[CaseResult]) -> tuple[FailureCase, ...]:
    """失败案例 = 冻结四值判定树中未被判为「正确拒绝」的 case。

    复用 T03 的 ``MetricsEngine.attribute``（不重写判定树）；环境故障 case 因此
    必带 ``Attribution.ENVIRONMENT_FAULT``，且不会进入 ``false_diagnosis_rate``
    分子（该指标由 ``D_env`` 排除集保证，见 ``MetricsEngine._evaluate``）。
    """

    rows: list[FailureCase] = []
    for case in cases:
        attribution = MetricsEngine.attribute(case)
        if attribution is Attribution.CORRECT_ABSTENTION:
            continue
        rows.append(
            FailureCase(
                case_id=case.case_id,
                attribution=attribution,
                reason=_safe_failure_reason(case, attribution),
                tool_sequence_digest=case.tool_sequence_digest,
            )
        )
    return tuple(rows)


def _environment_blocks(cases: Sequence[CaseResult]) -> tuple[EnvironmentBlock, ...]:
    """按环境阻塞代码聚合单列项；代码必须在 5 项闭集内。"""

    grouped: dict[str, list[str]] = {}
    for case in cases:
        if not case.env_blocked:
            continue
        code = case.excluded_reason
        if code not in KNOWN_ENV_BLOCKERS:
            continue
        grouped.setdefault(str(code), []).append(case.case_id)
    return tuple(
        EnvironmentBlock(
            code=code,  # type: ignore[arg-type]
            affected_cases=tuple(sorted(grouped[code])),
            statement=_ENV_STATEMENTS[code],
        )
        for code in ENVIRONMENT_BLOCK_CODES
        if code in grouped
    )


def build_baseline_report(
    *,
    result: RunResult,
    metrics: MetricSet,
    sft: SftGate | None = None,
    manifest: RunManifest | None = None,
) -> BaselineReport:
    """由 ``RunResult`` + ``MetricSet`` 组装诚实 Baseline 报告（AC-17）。

    Args:
        result: T02 的评测运行结果（复用，不重写）。
        metrics: T03 的指标集合（复用，不重写）。
        sft: 可选条件性 SFT 记录（W10-Q7：仅记录，不改变报告判定）。

    Returns:
        ``BaselineReport``；``model_commit is None`` 时 ``metrics=None``、
        ``blocked_reason`` 非空，模型侧指标一律不编造。

    Raises:
        BenchmarkInvalidError: 四类归因计数之和 != ``total_cases``（AC-17①），
            或归因键集合不等于冻结的四值集合。
    """

    # P1-T2：AGENT 模式传入**本次运行**的 Manifest（含真实模型），缺省仍用
    # baseline_v0 冻结件（模型缺席 → BLOCKED），既有调用方行为不变。
    manifest = manifest if manifest is not None else _frozen_baseline_manifest()
    model_commit = manifest.model_version
    tokenizer_commit = manifest.tokenizer_version

    attribution_counts = {
        str(name): int(count) for name, count in metrics.attribution_counts.items()
    }
    total_cases = int(metrics.total_cases)
    expected_keys = {member.value for member in Attribution}
    if set(attribution_counts) != expected_keys:
        raise BenchmarkInvalidError(
            "attribution_counts 必须恰为四类归因",
            details={"actual": sorted(attribution_counts), "expected": sorted(expected_keys)},
        )
    # AC-17①：四类之和恒等于 total_cases（内部断言，不可静默放过）。
    if sum(attribution_counts.values()) != total_cases:
        raise BenchmarkInvalidError(
            "四类归因计数之和必须等于 total_cases",
            details={
                "sum": sum(attribution_counts.values()),
                "total_cases": total_cases,
            },
        )

    blocked_reason: str | None
    if model_commit is None:
        blocked_reason = BLOCKED_REASON_MODEL_ENV
        report_metrics: dict[str, float] | None = None
        confidence_intervals: dict[str, Any] | None = None
        cost: dict[str, Any] | None = None
        # harness 层可重放 ≠ 模型层 Baseline 已完成；模型缺席一律 BLOCKED，
        # 不得把 RunResult 的 COMPLETED 误写成模型评测完成（W10-Q3 / R-01）。
        report_status = RunStatus.BLOCKED
    else:
        blocked_reason = None
        report_status = result.status
        # 只收录真实测得的数值；分母为 0 的指标值在 MetricSet 中恒为 None。
        report_metrics = {
            str(name): float(value)
            for name, value in metrics.values.items()
            if value is not None
        }
        confidence_intervals = _confidence_intervals(result, manifest.seed)
        cost = _cost_summary(result.cases)

    # 分母如实保留（含 0），供反编造校验核对「分母为 0 却有数值」。
    metric_denominators = {
        str(name): int(value) for name, value in metrics.denominators.items()
    }

    environment_blocked = list(_environment_blocks(result.cases))
    if model_commit is None and not any(
        block.code == "ENV-MODEL" for block in environment_blocked
    ):
        # 模型环境本身就是阻塞项：如实单列 ENV-MODEL（W10-Q3 / 卡 :525）。
        environment_blocked.append(
            EnvironmentBlock(
                code="ENV-MODEL",
                affected_cases=(),
                statement=_ENV_STATEMENTS["ENV-MODEL"],
            )
        )

    notes: list[str] = [
        (
            f"execution_mode={result.execution_mode}；模型层 BLOCKED_MODEL_ENV_MISSING，"
            "模型侧实测指标未测得，一律 null（W10-Q3）。"
            if model_commit is None
            else f"execution_mode={result.execution_mode}；模型 {manifest.model_name} "
            f"（model_version={manifest.model_version}）实测，"
            "指标仅覆盖真实执行且非环境故障的 case。"
        ),
        f"指标口径版本 {METRICS_VERSION}；机器可读冻结件 "
        "benchmarks/w10/metrics-v1.json，人读口径 docs/w10/metrics-definition.md。",
        "环境故障 case 归 Attribution.ENVIRONMENT_FAULT，单列 environment_blocked，"
        "不计入任何模型指标的分子或分母（AC-17③）。",
        (
            "数据集出口引用 "
            f"{_REFERENCED_T04_MODELS[0]}（group split 泄漏实测）与 "
            f"{_REFERENCED_T04_MODELS[1]}（tokenizer_commit=null 时统计值全 null）。"
        ),
        "SftGate 仅作条件性记录，不改变本报告判定；DPO / RL / 14B 推迟到 Alpha 之后。",
    ]
    if model_commit is None:
        notes.append(
            "harness 层可重放；模型层 BLOCKED_MODEL_ENV_MISSING，故 status=BLOCKED，"
            "不把 RunResult 的 COMPLETED 当作模型评测完成。"
        )
    if confidence_intervals and any(
        interval.get("lower") is None or interval.get("upper") is None
        for interval in confidence_intervals.values()
    ):
        notes.append(
            "置信区间不可估：冻结测试集每（engine, scenario）只有 1 个 cluster"
            "（单一 data_seed），cluster bootstrap 按设计不报 lower==upper 的伪区间，"
            "故 lower/upper 保持 null；实测点值与样本量照实给出。"
        )
    if sft is not None:
        notes.append(
            f"SftGate 记录：sft_allowed={sft.sft_allowed}、"
            f"sample_count={sft.sample_count}（W10-Q7：仅记录）。"
        )

    return BaselineReport(
        report_version=REPORT_VERSION,
        run_id=result.run_id or None,
        status=report_status,
        model_name=manifest.model_name,
        model_commit=model_commit,
        tokenizer_commit=tokenizer_commit,
        suite_version=manifest.suite_version,
        harness_version=manifest.harness_version,
        testset_version=manifest.testset_version,
        metrics=report_metrics,
        metric_denominators=metric_denominators,
        confidence_intervals=confidence_intervals,
        attribution_counts=attribution_counts,
        total_cases=total_cases,
        failure_cases=_failure_cases(result.cases),
        environment_blocked=tuple(environment_blocked),
        blocked_reason=blocked_reason,
        cost=cost,
        notes=tuple(notes),
    )


def _confidence_intervals(result: RunResult, seed: int) -> dict[str, Any] | None:
    """对 ``rca_top1`` 做 cluster bootstrap（按 engine/scenario/fault_seed 聚类）。

    只用**真实比对过**的 case（``rca_match is not None`` 且非环境故障）；
    样本不足 2 个 cluster 时返回 ``None``（不伪造区间）。
    """
    sampled = [
        case
        for case in result.cases
        if case.rca_match is not None and not is_env_excluded(case)
    ]
    if len({(case.engine, case.scenario, case.fault_seed) for case in sampled}) < 2:
        return None
    values = [1.0 if case.rca_match else 0.0 for case in sampled]
    clusters = [(case.engine, case.scenario, case.fault_seed) for case in sampled]
    interval = cluster_bootstrap(values, clusters=clusters, seed=seed)
    return {
        "rca_top1": {
            "point": interval.point,
            "lower": interval.lower,
            "upper": interval.upper,
            "resamples": interval.resamples,
            "seed": interval.seed,
            "method": interval.method,
            "input_digest": interval.input_digest,
        }
    }


def _cost_summary(cases: Sequence[CaseResult]) -> dict[str, Any] | None:
    """成本只汇总**实测 token**；没有任何 case 回报用量时返回 ``None``。"""
    measured = [case.cost_units for case in cases if case.cost_units is not None]
    if not measured:
        return None
    return {
        "cost_units_total": float(sum(measured)),
        "cost_units_unit": "tokens",
        "case_count": len(measured),
    }


def _iter_strings(value: Any) -> Any:
    """递归产出序列化报告里的全部字符串（键与值），供反编造 / 泄漏扫描。"""

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_strings(item)


def assert_no_fabricated_metrics(report: BaselineReport) -> None:
    """扫描报告，拒绝任何编造指标 / 越界措辞 / 真值外泄（卡 :523 / 禁止项 7）。

    检查项：

    1. ``model_commit is None`` 却有 ``metrics`` → ``BENCHMARK_INVALID``；
    2. 分母为 0 却有数值 → ``BENCHMARK_INVALID``（R-02）；
    3. 指标值非有限数（NaN / inf）或分母集合缺失 → ``BENCHMARK_INVALID``；
    4. 「预计 / 估计 / 约」等措辞充当实测值 → ``BENCHMARK_INVALID``；
    5. Oracle 真值字段名 / SQL 原文标记出现在报告文本 → ``BENCHMARK_INVALID``；
    6. 复用 T01 ``scan_texts_for_oracle_leak`` 复核真值字段名（隔离红线）。

    Raises:
        BenchmarkInvalidError: 任一检查项不通过。
    """

    if report.model_commit is None and report.metrics is not None:
        raise BenchmarkInvalidError(
            "model_commit 为 None 时不得输出任何 metrics",
            details={"metrics_keys": sorted(report.metrics)},
        )
    if report.model_commit is None and report.status is not RunStatus.BLOCKED:
        raise BenchmarkInvalidError(
            "模型环境缺席时报告 status 必须为 BLOCKED，不得宣称评测完成",
            details={"status": report.status.value},
        )

    if report.metrics is not None:
        if report.metric_denominators is None:
            raise BenchmarkInvalidError(
                "存在 metrics 却缺少 metric_denominators，无法核对分母"
            )
        for name, value in report.metrics.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise BenchmarkInvalidError(
                    "指标值必须是有限数值", details={"name": name}
                )
            denominator = report.metric_denominators.get(name, 0)
            if denominator == 0:
                raise BenchmarkInvalidError(
                    "分母为 0 却有指标数值（编造指标）",
                    details={"name": name, "denominator": denominator},
                )
            if name not in ALL_METRIC_NAMES:
                raise BenchmarkInvalidError(
                    "报告出现 PRD 未列指标", details={"name": name}
                )

    serialized = report.model_dump(mode="json")
    texts = list(_iter_strings(serialized))
    joined = "\n".join(texts)
    for phrase in FABRICATION_PHRASES:
        if phrase in joined:
            raise BenchmarkInvalidError(
                "报告出现估计措辞充当实测值",
                details={"phrase": phrase},
            )
    for literal in FORBIDDEN_REPORT_LITERALS:
        if literal in joined:
            raise BenchmarkInvalidError(
                "报告出现 Oracle 真值 / SQL 原文标记",
                details={"literal": literal},
            )
    # 复用 T01 文本扫描（字段名大小写不敏感），只回字段名、不回原文。
    hits = scan_texts_for_oracle_leak(texts)
    if hits:
        raise BenchmarkInvalidError(
            "报告文本命中 Oracle 真值字段名",
            details={"leaked_fields": list(hits)},
        )


__all__ = [
    "ALL_METRIC_NAMES",
    "BLOCKED_REASON_MODEL_ENV",
    "ENVIRONMENT_BLOCK_CODES",
    "FABRICATION_PHRASES",
    "FORBIDDEN_REPORT_LITERALS",
    "METRICS_VERSION",
    "REPORT_VERSION",
    "Attribution",
    "BaselineReport",
    "EnvironmentBlock",
    "FailureCase",
    "MetricSet",
    "RunResult",
    "RunStatus",
    "SftGate",
    "assert_no_fabricated_metrics",
    "build_baseline_report",
]
