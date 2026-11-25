"""V5.0 / RSI-STEP-04：Train/Holdout 防作弊影子评估门控。

替代 :class:`core.agents.evolution.EvolutionAgent` 里写死的 0.90/0.98/1.0 分值：
晋升与否**只**由真实跑完两套离线基准后的 :func:`core.eval.repository.compare_metrics`
判决 + canary 泄漏计数决定。

三条硬条件（全部满足才 ``promoted=True``）：

1. :func:`core.evolution.strategy_store.assert_constitutional_safe` 通过；
2. **Holdout** 集上 ``compare_metrics`` 判为 ``CANDIDATE_BETTER``（该判决本身要求
   所有可比指标同向，因此隐含"无关键指标倒退"）；
3. Train 与 Holdout 上候选策略的 ``leakage_count`` 均为 0。

防过拟合：Train 与 Holdout 的 ``workload_group`` 强制零交集（构造期校验），
且 Holdout 使用训练集未见过的新变体，只针对 Train 特化的投机策略会在 Holdout 上失分。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.eval.agent_benchmark import (
    AgentEvalCase,
    AgentEvalMetrics,
    export_eval_cases_golden,
    load_agent_eval_suite,
    run_agent_eval_suite,
)
from core.eval.repository import compare_metrics
from core.evolution.runtime_adapter import build_strategy_aware_runner
from core.evolution.strategy_store import (
    AgentStrategyBundle,
    ConstitutionalViolationError,
    assert_constitutional_safe,
)
from core.guardrails import AgentGuardrailClient

#: 相对仓库根的默认套件位置（桌面端与 CI 共用）。
DEFAULT_TRAIN_SUITE = Path("benchmarks") / "rsi" / "suite-rsi-train.json"
DEFAULT_HOLDOUT_SUITE = Path("benchmarks") / "rsi" / "suite-rsi-holdout.json"

REASON_PROMOTED = "HOLDOUT_CANDIDATE_BETTER_WITH_ZERO_LEAKAGE"
REASON_HOLDOUT_NOT_BETTER = "HOLDOUT_NOT_CANDIDATE_BETTER"
REASON_TRAIN_REGRESSION = "TRAIN_REGRESSION"
REASON_LEAKAGE = "CANARY_LEAKAGE_DETECTED"
REASON_CONSTITUTIONAL = "CONSTITUTIONAL_VIOLATION"


@dataclass(frozen=True)
class ShadowEvalVerdict:
    """一次影子门控的裁决结果（全部字段来自实测，无写死分数）。"""

    promoted: bool
    train_verdict: str
    holdout_verdict: str
    baseline_metrics: dict[str, Any]
    candidate_metrics: dict[str, Any]
    leakage_count: int
    reason: str
    baseline_version: str = ""
    candidate_version: str = ""
    train_baseline_metrics: dict[str, Any] = field(default_factory=dict)
    train_candidate_metrics: dict[str, Any] = field(default_factory=dict)
    train_leakage: int = 0
    holdout_leakage: int = 0
    baseline_accuracy: float = 0.0
    candidate_accuracy: float = 0.0
    baseline_false_positive_rate: float = 0.0
    candidate_false_positive_rate: float = 0.0
    golden: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "train_verdict": self.train_verdict,
            "holdout_verdict": self.holdout_verdict,
            "baseline_metrics": dict(self.baseline_metrics),
            "candidate_metrics": dict(self.candidate_metrics),
            "leakage_count": self.leakage_count,
            "reason": self.reason,
            "baseline_version": self.baseline_version,
            "candidate_version": self.candidate_version,
            "train_baseline_metrics": dict(self.train_baseline_metrics),
            "train_candidate_metrics": dict(self.train_candidate_metrics),
            "train_leakage": self.train_leakage,
            "holdout_leakage": self.holdout_leakage,
            "baseline_accuracy": self.baseline_accuracy,
            "candidate_accuracy": self.candidate_accuracy,
            "baseline_false_positive_rate": self.baseline_false_positive_rate,
            "candidate_false_positive_rate": self.candidate_false_positive_rate,
            "golden": self.golden,
        }


class ShadowEvaluator:
    """在 Train/Holdout 上真实评测候选策略，并给出晋升/驳回裁决。"""

    def __init__(self, train_suite: Path, holdout_suite: Path) -> None:
        self._train_path = Path(train_suite)
        self._holdout_path = Path(holdout_suite)
        self._train: tuple[AgentEvalCase, ...] | None = None
        self._holdout: tuple[AgentEvalCase, ...] | None = None

    # -- 套件加载 ------------------------------------------------------
    @property
    def train_suite(self) -> Path:
        return self._train_path

    @property
    def holdout_suite(self) -> Path:
        return self._holdout_path

    def load_suites(self) -> tuple[tuple[AgentEvalCase, ...], tuple[AgentEvalCase, ...]]:
        if self._train is None or self._holdout is None:
            train = load_agent_eval_suite(self._train_path)
            holdout = load_agent_eval_suite(self._holdout_path)
            overlap = {case.workload_group for case in train} & {case.workload_group for case in holdout}
            if overlap:
                raise ValueError(f"Train 与 Holdout 的 workload_group 存在重叠，禁止用同一批样本自证: {sorted(overlap)}")
            self._train, self._holdout = train, holdout
        return self._train, self._holdout

    # -- 单次测量 ------------------------------------------------------
    def measure(
        self,
        bundle: AgentStrategyBundle,
        *,
        suite: str = "holdout",
    ) -> AgentEvalMetrics:
        """用该策略包跑一遍指定套件，返回实测指标。"""
        train, holdout = self.load_suites()
        cases = holdout if suite == "holdout" else train
        runner = build_strategy_aware_runner(bundle, guardrail=AgentGuardrailClient())
        return run_agent_eval_suite(cases, runner)

    def score(self, bundle: AgentStrategyBundle, *, suite: str = "holdout") -> float:
        """供 EvolutionAgent 使用的真实影子分值（Holdout 诊断准确率）。"""
        return float(self.measure(bundle, suite=suite).diagnosis_accuracy)

    # -- 门控裁决 ------------------------------------------------------
    def evaluate_candidate(
        self,
        baseline: AgentStrategyBundle,
        candidate: AgentStrategyBundle,
        *,
        export_golden_dir: Path | None = None,
    ) -> ShadowEvalVerdict:
        """跑完 Train/Holdout 后给出是否晋升；任何一步失败都不得放行。"""
        train_cases, holdout_cases = self.load_suites()

        try:
            assert_constitutional_safe(baseline)
            assert_constitutional_safe(candidate)
        except ConstitutionalViolationError as exc:
            return ShadowEvalVerdict(
                promoted=False,
                train_verdict="BLOCKED",
                holdout_verdict="BLOCKED",
                baseline_metrics={},
                candidate_metrics={},
                leakage_count=0,
                reason=f"{REASON_CONSTITUTIONAL}:{exc}",
                baseline_version=baseline.version_id,
                candidate_version=candidate.version_id,
            )

        base_runner = build_strategy_aware_runner(baseline, guardrail=AgentGuardrailClient())
        cand_runner = build_strategy_aware_runner(candidate, guardrail=AgentGuardrailClient())

        base_train = run_agent_eval_suite(train_cases, base_runner)
        cand_train = run_agent_eval_suite(train_cases, cand_runner)
        base_holdout = run_agent_eval_suite(holdout_cases, base_runner)
        cand_holdout = run_agent_eval_suite(holdout_cases, cand_runner)

        train_verdict = compare_metrics(
            base_train.to_repository_metrics(), cand_train.to_repository_metrics()
        )
        holdout_verdict = compare_metrics(
            base_holdout.to_repository_metrics(), cand_holdout.to_repository_metrics()
        )

        train_leakage = int(cand_train.leakage_count)
        holdout_leakage = int(cand_holdout.leakage_count)
        leakage_total = train_leakage + holdout_leakage

        problems: list[str] = []
        if leakage_total:
            problems.append(REASON_LEAKAGE)
        if holdout_verdict.verdict != "CANDIDATE_BETTER":
            problems.append(f"{REASON_HOLDOUT_NOT_BETTER}:{holdout_verdict.verdict}:{holdout_verdict.reason}")
        if train_verdict.verdict == "BASELINE_BETTER":
            problems.append(f"{REASON_TRAIN_REGRESSION}:{train_verdict.reason}")

        promoted = not problems
        reason = REASON_PROMOTED if promoted else " | ".join(problems)

        golden = None
        if export_golden_dir is not None:
            golden = export_eval_cases_golden(
                holdout_cases,
                export_golden_dir=Path(export_golden_dir),
                suite_version=f"rsi-{candidate.version_id}",
            )

        return ShadowEvalVerdict(
            promoted=promoted,
            train_verdict=train_verdict.verdict,
            holdout_verdict=holdout_verdict.verdict,
            baseline_metrics=base_holdout.to_repository_metrics(),
            candidate_metrics=cand_holdout.to_repository_metrics(),
            leakage_count=leakage_total,
            reason=reason,
            baseline_version=baseline.version_id,
            candidate_version=candidate.version_id,
            train_baseline_metrics=base_train.to_repository_metrics(),
            train_candidate_metrics=cand_train.to_repository_metrics(),
            train_leakage=train_leakage,
            holdout_leakage=holdout_leakage,
            baseline_accuracy=float(base_holdout.diagnosis_accuracy),
            candidate_accuracy=float(cand_holdout.diagnosis_accuracy),
            baseline_false_positive_rate=float(base_holdout.false_positive_rate),
            candidate_false_positive_rate=float(cand_holdout.false_positive_rate),
            golden=golden,
        )

    # -- 可观测性 ------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        train, holdout = self.load_suites()
        return {
            "train_suite": str(self._train_path),
            "holdout_suite": str(self._holdout_path),
            "train_cases": len(train),
            "holdout_cases": len(holdout),
            "train_workload_groups": sorted({case.workload_group for case in train}),
            "holdout_workload_groups": sorted({case.workload_group for case in holdout}),
            "workload_overlap": sorted(
                {case.workload_group for case in train} & {case.workload_group for case in holdout}
            ),
        }

    def to_json(self, verdict: ShadowEvalVerdict) -> str:
        return json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


__all__ = [
    "DEFAULT_HOLDOUT_SUITE",
    "DEFAULT_TRAIN_SUITE",
    "REASON_CONSTITUTIONAL",
    "REASON_HOLDOUT_NOT_BETTER",
    "REASON_LEAKAGE",
    "REASON_PROMOTED",
    "REASON_TRAIN_REGRESSION",
    "ShadowEvalVerdict",
    "ShadowEvaluator",
]
