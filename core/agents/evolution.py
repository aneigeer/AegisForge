"""自主进化蒸馏 Agent（AGENT_EVOLUTION）。

职责（``five_core_agents[AGENT_EVOLUTION]``）：

- 复盘历史工单：沙箱放行模板、误报驳回、人工升级失败特征；
- 调用 V2.0 :class:`ToggleableEvolutionEngine` 蒸馏规则（STATIC 模式下自动跳过）；
- **强制 Shadow Mode 门槛**：影子评分未达阈值不得进入跨领域共享记忆库；
- 支持 Git 风格版本与一键回滚（引擎自带），本 Agent 只负责"是否允许晋升"的判定。
"""

from __future__ import annotations

from typing import Any

from core.agents.base import AgentContext, BaseAgent
from core.orchestrator.state_graph import NodeOutcome, SecOpsState
from core.platform.ten_modules import EvolutionMode, ToggleableEvolutionEngine

SHADOW_PASS_THRESHOLD = 0.95


class EvolutionAgent(BaseAgent):
    """把"这次工单学到什么"固化成可回滚的领域记忆。"""

    agent_id = "AGENT_EVOLUTION"

    def __init__(
        self,
        context: AgentContext,
        *,
        engine: ToggleableEvolutionEngine | None = None,
        memory: Any | None = None,
        shadow_threshold: float = SHADOW_PASS_THRESHOLD,
        shadow_evaluator: Any | None = None,
        strategy_store: Any | None = None,
    ) -> None:
        super().__init__(context)
        self._engine = engine or ToggleableEvolutionEngine(EvolutionMode.SHADOW)
        self._memory = memory
        self._shadow_threshold = shadow_threshold
        # V5.0：注入真实影子评估器后，shadow_score 来自实跑 Train/Holdout 的指标，
        # 不再使用 _classify 里的固定常量；未注入时保持 V3.0 行为完全不变。
        self._shadow_evaluator = shadow_evaluator
        self._strategy_store = strategy_store

    def run(self, state: SecOpsState) -> NodeOutcome:
        tickets = {item.get("finding_id"): item for item in state.triage_tickets}
        candidates: list[dict[str, Any]] = []

        for proposal_id, verdict in state.critic_verdicts.items():
            kind = verdict.get("verdict")
            if kind == "REJECTED" and not verdict.get("exhausted"):
                continue  # 自愈成功的中间轮次不产生蒸馏样本
            action, note, legacy_score = self._classify(verdict, tickets)
            shadow_score, shadow_source = self._shadow_decision(legacy_score)
            distilled = self._engine.distill_from_human_feedback(
                str(verdict.get("finding_id", "")), action, note
            )
            shadow_passed = shadow_score >= self._shadow_threshold
            promoted = False
            if distilled.get("distilled") and shadow_passed and self._memory is not None:
                promoted = bool(
                    self._memory.remember(
                        tenant_id=state.tenant_id,
                        domain=str(verdict.get("domain", "")),
                        kind=action,
                        text=str(distilled.get("rule_summary", note)),
                        workflow_run_id=state.workflow_run_id,
                    )
                )

            candidates.append(
                {
                    "candidate_id": f"cand-{proposal_id}",
                    "proposal_id": proposal_id,
                    "finding_id": verdict.get("finding_id", ""),
                    "domain": verdict.get("domain", ""),
                    "action": action,
                    "note": note,
                    "distilled": bool(distilled.get("distilled", False)),
                    "version": str(distilled.get("active_version", "")),
                    "mode": str(distilled.get("mode", self._engine.mode.value)),
                    "shadow_score": round(shadow_score, 4),
                    "shadow_passed": shadow_passed,
                    "shadow_source": shadow_source,
                    "promoted_to_memory": promoted,
                }
            )

        if candidates:
            self.worm(
                "AGENT_EVOLUTION_SHADOW_GATE",
                {
                    "candidates": len(candidates),
                    "promoted": sum(1 for item in candidates if item["promoted_to_memory"]),
                    "mode": self._engine.mode.value,
                    "shadow_threshold": self._shadow_threshold,
                    "versions": [item["version"] for item in candidates],
                },
            )

        metrics = dict(state.metrics)
        metrics["evolution"] = {
            "candidates": len(candidates),
            "promoted": sum(1 for item in candidates if item["promoted_to_memory"]),
            "mode": self._engine.mode.value,
            "active_version": self._engine.active_version,
        }
        return NodeOutcome(
            patches={
                "evolution_candidates": list(state.evolution_candidates) + candidates,
                "metrics": metrics,
            },
            events=[
                {
                    "event": "EVOLUTION_SHADOW_GATE",
                    "detail": metrics["evolution"],
                }
            ],
        )

    # -- 内部 ----------------------------------------------------------
    def _shadow_decision(self, legacy_score: float) -> tuple[float, str]:
        """真实影子分值优先；无评估器时退回 V3.0 的固定分值（保持向后兼容）。"""
        if self._shadow_evaluator is None:
            return legacy_score, "LEGACY_CONSTANT"
        try:
            if self._strategy_store is not None:
                bundle = self._strategy_store.get_active()
            else:
                from core.evolution.strategy_store import default_seed_bundle

                bundle = default_seed_bundle()
            score = float(self._shadow_evaluator.score(bundle))
        except Exception:  # noqa: BLE001 - 评估器故障不得伪造"通过"
            return 0.0, "SHADOW_EVALUATOR_ERROR"
        return score, "SHADOW_EVALUATOR"

    def _classify(
        self,
        verdict: dict[str, Any],
        tickets: dict[str, Any],
    ) -> tuple[str, str, float]:
        kind = verdict.get("verdict")
        if kind == "PASSED":
            return (
                "REMEDIATION_TEMPLATE",
                f"{verdict.get('domain')} 修复模板通过沙箱与红队复核（第 {verdict.get('round')} 轮）",
                1.0,
            )
        if kind == "FILTERED":
            return (
                "FALSE_POSITIVE_PATTERN",
                f"{verdict.get('domain')} 沙箱未复现，沉淀误报特征以抑制同类告警",
                0.98,
            )
        return (
            "HUMAN_ESCALATION_PATTERN",
            f"{verdict.get('domain')} 自愈 {verdict.get('round')} 轮仍失败，沉淀失败特征与人工处置路径",
            0.90,
        )


__all__ = ["EvolutionAgent", "SHADOW_PASS_THRESHOLD"]
