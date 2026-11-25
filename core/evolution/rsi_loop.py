"""V5.0 / RSI-STEP-05：有界多代递归自我改进调度器。

闭环（每一代都基于**当前已晋升的最新策略**，而不是每次从 S_0 重来）：

```
S_g --(Train 评测)--> 失败归因 --(Meta-Proposer)--> S_{g+1} 候选
    --(Holdout 影子门控)--> 晋升 / 驳回+回滚 --> S_{g+1} --(下一代)--> ...
```

有界性（三条闸门，缺一不可）：

1. ``max_generations`` 硬上限 5（超出直接报错，不允许"无限自进化"）；
2. ``patience`` 连续驳回早停；
3. 候选必须通过宪法锁 + Holdout ``CANDIDATE_BETTER`` + 双套件零泄漏。

审计：每次晋升/驳回都写 WORM（``RSI_GENERATION_PROMOTED`` / ``RSI_GENERATION_REJECTED``），
并同步 :class:`ToggleableEvolutionEngine` 的版本栈，使既有回滚 API 可回退任意代。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.evolution.proposer import SelfImprovementProposer
from core.evolution.shadow_evaluator import ShadowEvaluator
from core.evolution.strategy_store import StrategyStore, canonical_json
from core.platform.ten_modules import (
    AuditEcosystemExporter,
    DistilledRuleVersion,
    EvolutionMode,
    RbacRole,
    ToggleableEvolutionEngine,
)

#: 代数硬上限：超过即拒绝执行（防止无界自我迭代）。
MAX_GENERATIONS_HARD_LIMIT = 5
#: 默认代数与早停耐心值。
DEFAULT_MAX_GENERATIONS = 3
DEFAULT_PATIENCE = 2


@dataclass(frozen=True)
class GenerationRecord:
    """单代执行记录（指标全部来自实测）。"""

    generation: int
    baseline_version: str
    candidate_version: str
    promoted: bool
    rolled_back: bool
    holdout_accuracy_before: float
    holdout_accuracy_after: float
    false_positive_before: float
    false_positive_after: float
    leakage_count: int
    reason: str
    mutations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "baseline_version": self.baseline_version,
            "candidate_version": self.candidate_version,
            "promoted": self.promoted,
            "rolled_back": self.rolled_back,
            "holdout_accuracy_before": self.holdout_accuracy_before,
            "holdout_accuracy_after": self.holdout_accuracy_after,
            "false_positive_before": self.false_positive_before,
            "false_positive_after": self.false_positive_after,
            "leakage_count": self.leakage_count,
            "reason": self.reason,
            "mutations": list(self.mutations),
        }


@dataclass(frozen=True)
class RsiRunReport:
    """一次受控 RSI 周期的完整报告。"""

    generations_executed: int
    initial_version: str
    final_version: str
    initial_holdout_accuracy: float
    final_holdout_accuracy: float
    monotonic_gain: float
    records: list[GenerationRecord]
    lineage_sha256: str
    engine_active_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "generations_executed": self.generations_executed,
            "initial_version": self.initial_version,
            "final_version": self.final_version,
            "initial_holdout_accuracy": self.initial_holdout_accuracy,
            "final_holdout_accuracy": self.final_holdout_accuracy,
            "monotonic_gain": self.monotonic_gain,
            "records": [record.to_dict() for record in self.records],
            "lineage_sha256": self.lineage_sha256,
            "engine_active_version": self.engine_active_version,
        }


def _clamp_generations(max_generations: int) -> int:
    value = int(max_generations)
    if value < 1:
        raise ValueError("max_generations 至少为 1")
    if value > MAX_GENERATIONS_HARD_LIMIT:
        raise ValueError(f"max_generations 不得超过硬上限 {MAX_GENERATIONS_HARD_LIMIT}")
    return value


def run_bounded_rsi_loop(
    store: StrategyStore,
    proposer: SelfImprovementProposer,
    evaluator: ShadowEvaluator,
    *,
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    patience: int = DEFAULT_PATIENCE,
    engine: ToggleableEvolutionEngine | None = None,
    audit: AuditEcosystemExporter | None = None,
) -> RsiRunReport:
    """执行受控多代 RSI 闭环，返回逐代实测记录与最终激活版本。"""
    generations = _clamp_generations(max_generations)
    patience = max(1, int(patience))
    engine = engine or ToggleableEvolutionEngine(EvolutionMode.SHADOW)
    audit = audit or AuditEcosystemExporter()

    active = store.get_active()
    initial_version = active.version_id
    initial_metrics = evaluator.measure(active, suite="holdout")
    initial_accuracy = float(initial_metrics.diagnosis_accuracy)

    records: list[GenerationRecord] = []
    consecutive_rejects = 0
    current_accuracy = initial_accuracy
    current_fp = float(initial_metrics.false_positive_rate)

    for generation in range(1, generations + 1):
        train_metrics = evaluator.measure(active, suite="train")
        failures = proposer.attribute_failures(list(train_metrics.per_case), [])
        candidate = proposer.propose_next_generation(active, failures, store.lineage())

        if proposer.is_no_op(active, candidate):
            records.append(
                GenerationRecord(
                    generation=generation,
                    baseline_version=active.version_id,
                    candidate_version=candidate.version_id,
                    promoted=False,
                    rolled_back=False,
                    holdout_accuracy_before=round(current_accuracy, 6),
                    holdout_accuracy_after=round(current_accuracy, 6),
                    false_positive_before=round(current_fp, 6),
                    false_positive_after=round(current_fp, 6),
                    leakage_count=0,
                    reason="NO_NEW_MUTATION:剩余失败维度均已被证伪或已覆盖",
                )
            )
            break

        store.save_candidate(candidate)
        # 候选先进入引擎影子版本栈；驳回时用既有 rollback_version() 摘除
        shadow_tag = f"rsi-{candidate.version_id}"
        engine.versions.append(
            DistilledRuleVersion(
                shadow_tag,
                EvolutionMode.SHADOW.value,
                f"RSI 候选策略 {candidate.version_id}（parent={active.version_id}）",
                candidate.version_id,
            )
        )

        verdict = evaluator.evaluate_candidate(active, candidate)
        mutations = proposer.mutation_ids(candidate)
        primary_category = failures[0].category if failures else ""

        if verdict.promoted:
            store.promote(
                candidate.version_id,
                reason=verdict.reason,
                metrics={
                    "holdout_accuracy": verdict.candidate_accuracy,
                    "train_verdict": verdict.train_verdict,
                    "holdout_verdict": verdict.holdout_verdict,
                    "leakage_count": verdict.leakage_count,
                    "mutations": mutations,
                    "primary_category": primary_category,
                },
            )
            audit.append_worm_event(
                "RSI_GENERATION_PROMOTED",
                RbacRole.SEC.value,
                {
                    "generation": generation,
                    "baseline_version": active.version_id,
                    "candidate_version": candidate.version_id,
                    "holdout_accuracy_before": verdict.baseline_accuracy,
                    "holdout_accuracy_after": verdict.candidate_accuracy,
                    "holdout_verdict": verdict.holdout_verdict,
                    "train_verdict": verdict.train_verdict,
                    "leakage_count": verdict.leakage_count,
                    "mutations": mutations,
                },
            )
            records.append(
                GenerationRecord(
                    generation=generation,
                    baseline_version=active.version_id,
                    candidate_version=candidate.version_id,
                    promoted=True,
                    rolled_back=False,
                    holdout_accuracy_before=round(verdict.baseline_accuracy, 6),
                    holdout_accuracy_after=round(verdict.candidate_accuracy, 6),
                    false_positive_before=round(verdict.baseline_false_positive_rate, 6),
                    false_positive_after=round(verdict.candidate_false_positive_rate, 6),
                    leakage_count=verdict.leakage_count,
                    reason=verdict.reason,
                    mutations=mutations,
                )
            )
            active = candidate
            current_accuracy = verdict.candidate_accuracy
            current_fp = verdict.candidate_false_positive_rate
            consecutive_rejects = 0
            continue

        # 驳回：登记负向元经验 + 摘除引擎影子版本 + 保持 active 不变（等价于自动回滚）
        store.record_rejection(
            candidate,
            reason=verdict.reason,
            metrics={
                "holdout_accuracy": verdict.candidate_accuracy,
                "holdout_verdict": verdict.holdout_verdict,
                "train_verdict": verdict.train_verdict,
                "primary_category": primary_category,
            },
            rejected_mutations=[{"mutation_id": item, "reason": verdict.reason} for item in mutations],
        )
        engine.rollback_version()
        audit.append_worm_event(
            "RSI_GENERATION_REJECTED",
            RbacRole.SEC.value,
            {
                "generation": generation,
                "baseline_version": active.version_id,
                "candidate_version": candidate.version_id,
                "holdout_verdict": verdict.holdout_verdict,
                "train_verdict": verdict.train_verdict,
                "reason": verdict.reason,
                "leakage_count": verdict.leakage_count,
                "mutations": mutations,
            },
        )
        records.append(
            GenerationRecord(
                generation=generation,
                baseline_version=active.version_id,
                candidate_version=candidate.version_id,
                promoted=False,
                rolled_back=True,
                holdout_accuracy_before=round(verdict.baseline_accuracy, 6),
                holdout_accuracy_after=round(verdict.baseline_accuracy, 6),
                false_positive_before=round(verdict.baseline_false_positive_rate, 6),
                false_positive_after=round(verdict.baseline_false_positive_rate, 6),
                leakage_count=verdict.leakage_count,
                reason=verdict.reason,
                mutations=mutations,
            )
        )
        consecutive_rejects += 1
        if consecutive_rejects >= patience:
            break

    final = store.get_active()
    final_accuracy = float(evaluator.measure(final, suite="holdout").diagnosis_accuracy)
    return RsiRunReport(
        generations_executed=len(records),
        initial_version=initial_version,
        final_version=final.version_id,
        initial_holdout_accuracy=round(initial_accuracy, 6),
        final_holdout_accuracy=round(final_accuracy, 6),
        monotonic_gain=round(final_accuracy - initial_accuracy, 6),
        records=records,
        lineage_sha256=store.lineage_sha256(),
        engine_active_version=engine.active_version,
    )


def report_digest(report: RsiRunReport) -> str:
    """报告内容摘要（供桌面端/审计核对，不参与判决）。"""
    import hashlib

    return hashlib.sha256(canonical_json(report.to_dict()).encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_MAX_GENERATIONS",
    "DEFAULT_PATIENCE",
    "MAX_GENERATIONS_HARD_LIMIT",
    "GenerationRecord",
    "RsiRunReport",
    "report_digest",
    "run_bounded_rsi_loop",
]
