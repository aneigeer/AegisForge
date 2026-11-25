"""V5.0 / RSI-STEP-03：策略包运行时热绑定（Strategy-Driven Runtime）。

因果链的关键一环：让 ``StrategyStore.get_active()`` 里晋升的规则、修复模板与
Prompt 指导**真的改变运行时行为**，而不是只写进一份 JSON 就算"进化完成"。

两个入口：

- :func:`build_policy_from_bundle`   把声明式规则合入基础安全策略（只增不减）；
- :func:`build_strategy_aware_runner` 生成可被 :func:`core.eval.agent_benchmark.run_agent_eval_suite`
  直接消费的运行器，同一测试集下 S_0 与 S_1 会产生可测的输出差异。

宪法约束：基础规则始终来自 :class:`DefaultAgentSafetyPolicy`，bundle 只能**追加**；
若 bundle 里出现同 ``rule_id`` 的规则，不会被用来覆盖基础规则。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.eval.agent_benchmark import AgentEvalCase, detect_findings
from core.guardrails.policies import BLOCKING_CATEGORIES, DefaultAgentSafetyPolicy, PolicyRule
from core.evolution.strategy_store import AgentStrategyBundle, assert_constitutional_safe
from core.privacy_gateway.multimodal import MODALITY_TEXT, MODALITY_YAML


def _policy_rule_from_dict(rule: dict[str, Any]) -> PolicyRule:
    patterns = tuple(re.compile(str(item)) for item in rule.get("patterns", []) or [])
    return PolicyRule(
        rule_id=str(rule["rule_id"]),
        category=str(rule["category"]),
        description=str(rule.get("description", "")),
        patterns=patterns,
        requires_hitl=bool(rule.get("requires_hitl", False)),
    )


def build_policy_from_bundle(bundle: AgentStrategyBundle) -> DefaultAgentSafetyPolicy:
    """把 bundle 的声明式规则叠加到基础安全策略之上（基础规则永不被替换）。"""
    assert_constitutional_safe(bundle)
    base = DefaultAgentSafetyPolicy()
    if not bundle.policy_rules:
        return base
    extra = [_policy_rule_from_dict(rule) for rule in bundle.policy_rules]
    return base.with_additional_rules(extra)


def _guardrail_modality(case: AgentEvalCase) -> str | None:
    return MODALITY_YAML if case.modality == MODALITY_YAML else MODALITY_TEXT


def build_strategy_aware_runner(
    bundle: AgentStrategyBundle,
    *,
    guardrail: Any | None = None,
    workflow_run_id: str = "rsi-eval",
) -> Callable[[AgentEvalCase], dict[str, Any]]:
    """构造按策略包驱动的评测运行器。

    ``guardrail`` 只用于出站脱敏（保证 ``leakage_count == 0``），阻断判定由
    bundle 策略决定——这样 S_0 与 S_1 的差异完全来自策略包本身，可归因、可比较。
    """
    policy = build_policy_from_bundle(bundle)

    def runner(case: AgentEvalCase) -> dict[str, Any]:
        labels = detect_findings(case.input_payload, modality=case.modality, policy=policy)
        hits = policy.evaluate(case.input_payload)
        blocked = any(hit.category in BLOCKING_CATEGORIES for hit in hits)

        masked = case.input_payload
        if guardrail is not None:
            decision = guardrail.guard_outbound(
                case.input_payload,
                workflow_run_id=f"{workflow_run_id}:{bundle.version_id}:{case.case_id}",
                modality=_guardrail_modality(case),
            )
            masked = decision.masked_payload

        template = bundle.repair_templates.get("SQL_INJECTION") if "SQL_INJECTION" in labels else None
        output: dict[str, Any] = {
            "case_id": case.case_id,
            "blocked": blocked,
            "detected": list(labels),
            "strategy_version": bundle.version_id,
            "answer": "已分析：" + masked,
        }
        if template:
            output["repair_template"] = template
        return {
            "blocked": blocked,
            "findings": list(labels),
            "output": output,
            "masked_payload": masked,
            # 本地确定性评测无真实模型调用，如实不报延迟与 Token
            "latency_ms": None,
            "tokens": None,
            "critic_recovered": None,
        }

    runner.__strategy_version__ = bundle.version_id  # type: ignore[attr-defined]
    return runner


def describe_bundle(bundle: AgentStrategyBundle) -> dict[str, Any]:
    """瘦视图：供桌面端展示当前激活策略（不含正则实现细节）。"""
    return {
        "version_id": bundle.version_id,
        "parent_version": bundle.parent_version,
        "generation": bundle.generation,
        "rule_count": len(bundle.policy_rules),
        "rule_ids": [str(rule.get("rule_id", "")) for rule in bundle.policy_rules],
        "repair_categories": sorted(bundle.repair_templates),
        "prompt_directive_count": len(bundle.prompt_directives),
        "mutation_ids": [str(rule.get("mutation_id")) for rule in bundle.policy_rules if rule.get("mutation_id")],
        "content_sha256": bundle.content_sha256,
    }


__all__ = [
    "build_policy_from_bundle",
    "build_strategy_aware_runner",
    "describe_bundle",
]

