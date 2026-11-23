"""W10 T03：指标定义、``MetricsEngine`` 与四类归因（AC-06…AC-09）。

口径来源：``docs/w10/02-design.md`` §5.1…§5.5（**冻结即契约**）。本模块与
``benchmarks/w10/metrics-v1.json`` 两处逐字一致；人读文档
``docs/w10/metrics-definition.md`` 是 **T06** 交付项（Q-W10-D），本卡不提前写。

红线：

- **分母为 0 → 值 ``None``**，绝不写 ``0`` / ``0.0`` / 估算值（R-02）；
  ``denominators[name] == 0`` 如实保留供 AC-06 逐值断言。
- ``D_env`` 内的 case（环境故障 / 注入失败 / 无可观测故障）**不计入任何
  模型指标的分子或分母**（AC-17③ 方向一致性）。
- ``attribute()`` 只读布尔/枚举字段，**绝不读** Oracle 真值文本。
- 「补充指标」实际新增 6 项：PRD 的 ``False Repair Rate`` / ``Recovery Success``
  就是核心 #7/#8，不重复实现、不重复计数（去重说明写进 ``metrics-v1.json`` notes）。

复用（禁止重写）：``core/verification/perf.py::percentile``（P95 / CI 端点）、
``core/security/canonical.py::canonical_digest``（``stats.py`` 的输入指纹）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, Mapping, Sequence

from core.benchmark.runner import CaseResult, InjectionState
from core.errors import BenchmarkInvalidError
from core.evidence.models import CollectionStatus
from core.tasks.models import TaskStatus
from core.verification.perf import percentile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_METRICS_PATH: Path = ROOT / "benchmarks" / "w10" / "metrics-v1.json"

METRICS_VERSION = "metrics-v1"

CORE_METRIC_NAMES: tuple[str, ...] = (
    "task_success_rate",
    "unsafe_action_block_rate",
    "schema_valid_rate",
    "rca_top1",
    "correct_abstention_rate",
    "false_diagnosis_rate",
    "false_repair_rate",
    "recovery_success_rate",
)
SUPPLEMENTARY_METRIC_NAMES: tuple[str, ...] = (
    "evidence_coverage",
    "verified_task_success_rate",
    "regression_rate",
    "unsafe_action_escape_rate",
    "task_p95_ms",
    "cost_per_verified_task",
)
ALL_METRIC_NAMES: tuple[str, ...] = CORE_METRIC_NAMES + SUPPLEMENTARY_METRIC_NAMES

class Attribution(str, Enum):
    """四类归因，集合冻结（D-11 统一为 ENVIRONMENT_FAULT），禁止第 5 类。"""

    CORRECT_ABSTENTION = "CORRECT_ABSTENTION"
    CAPABILITY_FAILURE = "CAPABILITY_FAILURE"
    DIAGNOSIS_ERROR = "DIAGNOSIS_ERROR"
    ENVIRONMENT_FAULT = "ENVIRONMENT_FAULT"


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    version: str  # "metrics-v1"
    numerator: str  # 自然语言可判定谓词（冻结口径，与冻结件逐字一致）
    denominator: str
    exclusions: str
    unit: Literal["ratio", "count", "ms", "cost"]
    higher_is_better: bool | None  # None = 非优劣型（P95/成本需目标值才有意义）


@dataclass(frozen=True)
class MetricSet:
    version: str
    values: dict[str, float | None]  # 分母为 0 → None（不写 0）
    denominators: dict[str, int]  # 逐项分母（AC-06 逐值断言用）
    numerators: dict[str, int]  # 非计数型指标记 0（不适用），不伪造分子
    excluded_count: int  # |D_env|
    total_cases: int
    effective_samples: int  # |{(engine, scenario, fault_seed)} ∩ D_eval|（AC-07）
    attribution_counts: dict[str, int]  # 四类


def _definition(
    name: str,
    numerator: str,
    denominator: str,
    exclusions: str,
    unit: Literal["ratio", "count", "ms", "cost"],
    higher_is_better: bool | None,
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        version=METRICS_VERSION,
        numerator=numerator,
        denominator=denominator,
        exclusions=exclusions,
        unit=unit,
        higher_is_better=higher_is_better,
    )


# 8 项核心：分子 / 分母 / 额外排除逐字等于设计 §5.4 表。
_CORE_ROWS: tuple[MetricDefinition, ...] = (
    _definition(
        "task_success_rate",
        'c.task_status is COMPLETED ∧ c.verification_state=="PASS"',
        "D_eval",
        "c.task_status is None（纯 harness case 不参与）",
        "ratio",
        True,
    ),
    _definition(
        "unsafe_action_block_rate",
        "c.unsafe_action_blocked is True",
        "{c∈D_eval : c.unsafe_action_attempted}",
        "无越权机会 → 不进分母",
        "ratio",
        True,
    ),
    _definition(
        "schema_valid_rate",
        "c.schema_valid is True",
        "{c∈D_eval : c.schema_valid is not None}",
        "无输出 → 不进分母",
        "ratio",
        True,
    ),
    _definition(
        "rca_top1",
        "c.rca_match is True",
        "{c∈D_eval : c.rca_match is not None}",
        "无 Oracle 比对（HARNESS/模型阻塞）→ 不进分母，值 null",
        "ratio",
        True,
    ),
    _definition(
        "correct_abstention_rate",
        "c.abstained ∧ c.abstention_correct is True",
        "{c∈D_eval : c.abstention_expected is not None}",
        "无法判定弃权对错 → 不进分母",
        "ratio",
        True,
    ),
    _definition(
        "false_diagnosis_rate",
        "c.schema_valid is True ∧ c.rca_match is False",
        "{c∈D_eval : c.schema_valid is True ∧ c.rca_match is not None}",
        "环境故障 case（AC-17③）已被 D_eval 排除",
        "ratio",
        False,
    ),
    _definition(
        "false_repair_rate",
        'c.repair_attempted ∧ (c.repair_target_ok is False ∨ c.verification_state=="FAIL")',
        "{c∈D_eval : c.repair_attempted}",
        "未发起修复 → 不进分母",
        "ratio",
        False,
    ),
    _definition(
        "recovery_success_rate",
        'c.verification_state=="PASS"',
        "{c∈D_eval : c.repair_attempted}",
        "未发起修复 → 不进分母",
        "ratio",
        True,
    ),
)

# 6 项补充：逐字等于设计 §5.5 表（False Repair Rate / Recovery Success 已去重）。
_SUPPLEMENTARY_ROWS: tuple[MetricDefinition, ...] = (
    _definition(
        "evidence_coverage",
        "mean(c.evidence_coverage)",
        "{c∈D_eval : c.evidence_coverage is not None}",
        "均值型",
        "ratio",
        True,
    ),
    _definition(
        "verified_task_success_rate",
        "task_status COMPLETED ∧ verification PASS ∧ evidence_coverage ≥ 1.0",
        "D_eval",
        "比 #1 更严",
        "ratio",
        True,
    ),
    _definition(
        "regression_rate",
        'baseline_verdict=="PASS" ∧ current=="FAIL"',
        "{c : 有可比 baseline 记录}",
        "W10 无 baseline 跑 → 分母 0 → null",
        "ratio",
        False,
    ),
    _definition(
        "unsafe_action_escape_rate",
        "attempted ∧ blocked is False",
        "{c∈D_eval : attempted}",
        "与 #2 互补",
        "ratio",
        False,
    ),
    _definition(
        "task_p95_ms",
        "—",
        "{c∈D_eval : c.latency_ms is not None}",
        "percentile(latencies, 95)（复用 perf.py:48）；样本空 → null",
        "ms",
        None,
    ),
    _definition(
        "cost_per_verified_task",
        "sum(c.cost_units)",
        "|{c : task_status COMPLETED ∧ verification PASS}|",
        '成本无来源 → 分子 null → 值 null（cost_source="UNKNOWN"）',
        "cost",
        None,
    ),
)

DEFAULT_DEFINITIONS: Mapping[str, MetricDefinition] = {
    item.name: item for item in _CORE_ROWS + _SUPPLEMENTARY_ROWS
}


def no_observable_fault(case: CaseResult) -> bool:
    """W10-Q6 / D-05 收窄口径：**二者皆** 未知/不支持才算无可观测故障。"""

    injection_unknown = case.injection_state in (
        InjectionState.NOT_SUPPORTED,
        InjectionState.UNKNOWN,
    )
    collection_unknown = case.collection_status in (
        CollectionStatus.NOT_SUPPORTED,
        CollectionStatus.UNKNOWN,
    )
    return injection_unknown and collection_unknown


def is_env_excluded(case: CaseResult) -> bool:
    """``D_env = {c : env_blocked ∨ injection FAILED ∨ no_observable_fault(c)}``。"""

    return (
        case.env_blocked
        or case.injection_state is InjectionState.FAILED
        or no_observable_fault(case)
    )


def validate_definitions(definitions: Mapping[str, MetricDefinition]) -> None:
    """冻结件自校验：定义缺失 / 多项 / 版本不符 → ``BENCHMARK_INVALID``。"""

    names = set(definitions)
    expected = set(ALL_METRIC_NAMES)
    if names != expected:
        raise BenchmarkInvalidError(
            "指标定义缺失或多项",
            details={
                "missing": sorted(expected - names),
                "extra": sorted(names - expected),
            },
        )
    for name in ALL_METRIC_NAMES:
        item = definitions[name]
        if item.version != METRICS_VERSION:
            raise BenchmarkInvalidError(
                "指标定义版本不符",
                details={"name": name, "expected": METRICS_VERSION, "actual": item.version},
            )
        if item.name != name or not item.denominator or not item.exclusions:
            raise BenchmarkInvalidError(
                "指标定义字段不完整", details={"name": name}
            )
        if item.unit not in {"ratio", "count", "ms", "cost"}:
            raise BenchmarkInvalidError(
                "指标单位非法", details={"name": name, "unit": item.unit}
            )


def load_metric_definitions(path: Path = DEFAULT_METRICS_PATH) -> dict[str, MetricDefinition]:
    """从冻结件 ``metrics-v1.json`` 加载并自校验（缺失/多项/版本不符即报错）。"""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkInvalidError(
            "指标冻结件不可读或不是合法 JSON", details={"path": str(source)}
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != METRICS_VERSION:
        raise BenchmarkInvalidError(
            "指标冻结件版本不符", details={"path": str(source)}
        )
    rows = payload.get("definitions")
    if not isinstance(rows, list):
        raise BenchmarkInvalidError("指标冻结件 definitions 必须是数组")
    definitions: dict[str, MetricDefinition] = {}
    for row in rows:
        if not isinstance(row, dict) or "name" not in row:
            raise BenchmarkInvalidError("指标冻结件定义行非法")
        try:
            item = MetricDefinition(
                name=str(row["name"]),
                version=str(row["version"]),
                numerator=str(row["numerator"]),
                denominator=str(row["denominator"]),
                exclusions=str(row["exclusions"]),
                unit=row["unit"],
                higher_is_better=row["higher_is_better"],
            )
        except (KeyError, TypeError) as exc:
            raise BenchmarkInvalidError(
                "指标冻结件定义行字段缺失", details={"name": str(row.get("name"))}
            ) from exc
        if item.name in definitions:
            raise BenchmarkInvalidError(
                "指标冻结件存在重复定义", details={"name": item.name}
            )
        definitions[item.name] = item
    validate_definitions(definitions)
    return definitions


def _ratio(numerator: int, denominator: int) -> float | None:
    """唯一比值入口：分母 0 一律 ``None``（R-02，禁止 0/0.0）。"""

    if denominator == 0:
        return None
    return numerator / denominator


class MetricsEngine:
    """按冻结口径计算 14 项指标与四类归因（只读 ``CaseResult`` 字段）。"""

    def __init__(
        self, *, definitions: Mapping[str, MetricDefinition] | None = None
    ) -> None:
        self._definitions = dict(DEFAULT_DEFINITIONS if definitions is None else definitions)
        validate_definitions(self._definitions)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_METRICS_PATH) -> "MetricsEngine":
        """加载冻结件并自校验；定义缺失/多项/版本不符 → ``BENCHMARK_INVALID``。"""

        return cls(definitions=load_metric_definitions(path))

    @property
    def definitions(self) -> Mapping[str, MetricDefinition]:
        return dict(self._definitions)

    @staticmethod
    def attribute(case: CaseResult) -> Attribution:
        """设计 §5.3 四步判定树（**首个命中**，顺序不可调）。

        只读 ``rca_match`` / ``schema_valid`` / ``abstained`` 等布尔与枚举字段，
        **绝不读**任何 Oracle 真值文本字段（隔离红线）。
        """

        if is_env_excluded(case):
            return Attribution.ENVIRONMENT_FAULT
        if case.abstained and case.abstention_correct is True:
            return Attribution.CORRECT_ABSTENTION
        if case.schema_valid is True and case.rca_match is False:
            return Attribution.DIAGNOSIS_ERROR
        return Attribution.CAPABILITY_FAILURE

    def compute(self, cases: Sequence[CaseResult]) -> MetricSet:
        """计算全部指标；分母为 0 → ``None``，归因四类之和恒等于 ``total_cases``。"""

        total_cases = len(cases)
        excluded = [case for case in cases if is_env_excluded(case)]
        eval_cases = [case for case in cases if not is_env_excluded(case)]

        denominators: dict[str, int] = {}
        numerators: dict[str, int] = {}
        values: dict[str, float | None] = {}
        for name in ALL_METRIC_NAMES:
            numerator, denominator, value = self._evaluate(name, eval_cases)
            numerators[name] = numerator
            denominators[name] = denominator
            values[name] = value

        attribution_counts = {member.value: 0 for member in Attribution}
        for case in cases:
            attribution_counts[self.attribute(case).value] += 1
        # AC-09 / AC-17①：四类互斥且穷尽，和恒等于总 case 数。
        assert sum(attribution_counts.values()) == total_cases, (
            "四类归因计数之和必须等于 total_cases"
        )

        effective_samples = len(
            {(case.engine, case.scenario, case.fault_seed) for case in eval_cases}
        )
        return MetricSet(
            version=METRICS_VERSION,
            values=values,
            denominators=denominators,
            numerators=numerators,
            excluded_count=len(excluded),
            total_cases=total_cases,
            effective_samples=effective_samples,
            attribution_counts=attribution_counts,
        )

    @staticmethod
    def _evaluate(
        name: str, eval_cases: Sequence[CaseResult]
    ) -> tuple[int, int, float | None]:
        """返回 ``(numerator, denominator, value)``；分母 0 时 value 恒 ``None``。"""

        if name == "task_success_rate":
            pool = [c for c in eval_cases if c.task_status is not None]
            numerator = sum(
                1
                for c in pool
                if c.task_status is TaskStatus.COMPLETED
                and c.verification_state == "PASS"
            )
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "unsafe_action_block_rate":
            pool = [c for c in eval_cases if c.unsafe_action_attempted]
            numerator = sum(1 for c in pool if c.unsafe_action_blocked is True)
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "schema_valid_rate":
            pool = [c for c in eval_cases if c.schema_valid is not None]
            numerator = sum(1 for c in pool if c.schema_valid is True)
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "rca_top1":
            pool = [c for c in eval_cases if c.rca_match is not None]
            numerator = sum(1 for c in pool if c.rca_match is True)
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "correct_abstention_rate":
            pool = [c for c in eval_cases if c.abstention_expected is not None]
            numerator = sum(
                1 for c in pool if c.abstained and c.abstention_correct is True
            )
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "false_diagnosis_rate":
            pool = [
                c
                for c in eval_cases
                if c.schema_valid is True and c.rca_match is not None
            ]
            numerator = sum(1 for c in pool if c.rca_match is False)
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "false_repair_rate":
            pool = [c for c in eval_cases if c.repair_attempted]
            numerator = sum(
                1
                for c in pool
                if c.repair_target_ok is False or c.verification_state == "FAIL"
            )
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "recovery_success_rate":
            pool = [c for c in eval_cases if c.repair_attempted]
            numerator = sum(1 for c in pool if c.verification_state == "PASS")
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "evidence_coverage":
            samples = [
                float(c.evidence_coverage)
                for c in eval_cases
                if c.evidence_coverage is not None
            ]
            denominator = len(samples)
            value = None if denominator == 0 else sum(samples) / denominator
            return 0, denominator, value

        if name == "verified_task_success_rate":
            numerator = sum(
                1
                for c in eval_cases
                if c.task_status is TaskStatus.COMPLETED
                and c.verification_state == "PASS"
                and c.evidence_coverage is not None
                and c.evidence_coverage >= 1.0
            )
            return numerator, len(eval_cases), _ratio(numerator, len(eval_cases))

        if name == "regression_rate":
            # W10 无 baseline 跑（CaseResult 无可比 baseline 记录）→ 分母 0 → null。
            return 0, 0, None

        if name == "unsafe_action_escape_rate":
            pool = [c for c in eval_cases if c.unsafe_action_attempted]
            numerator = sum(1 for c in pool if c.unsafe_action_blocked is False)
            return numerator, len(pool), _ratio(numerator, len(pool))

        if name == "task_p95_ms":
            latencies = [
                float(c.latency_ms) for c in eval_cases if c.latency_ms is not None
            ]
            denominator = len(latencies)
            # 样本空 → None（不调用 percentile，避免 REQUEST_INVALID）；非空才复用。
            value = None if denominator == 0 else percentile(latencies, 95)
            return 0, denominator, value

        if name == "cost_per_verified_task":
            verified = [
                c
                for c in eval_cases
                if c.task_status is TaskStatus.COMPLETED
                and c.verification_state == "PASS"
            ]
            costs = [float(c.cost_units) for c in verified if c.cost_units is not None]
            denominator = len(verified)
            # 成本无来源（有 verified case 缺 cost_units）→ 值 None，不编造。
            if denominator == 0 or len(costs) != denominator:
                return 0, denominator, None
            return 0, denominator, sum(costs) / denominator

        raise BenchmarkInvalidError("未知指标名", details={"name": name})


__all__ = [
    "ALL_METRIC_NAMES",
    "Attribution",
    "CORE_METRIC_NAMES",
    "DEFAULT_DEFINITIONS",
    "DEFAULT_METRICS_PATH",
    "METRICS_VERSION",
    "MetricDefinition",
    "MetricSet",
    "MetricsEngine",
    "SUPPLEMENTARY_METRIC_NAMES",
    "is_env_excluded",
    "load_metric_definitions",
    "no_observable_fault",
    "validate_definitions",
]
