"""V5.0 / RSI-STEP-02：失败归因 + 元经验感知的自我改进提案器。

两件事：

1. **失败归因**（:meth:`SelfImprovementProposer.attribute_failures`）：把真实评测
   的逐用例结果与 Critic 打回码聚簇成可解释的失败类别，并给出可执行的变异建议；
2. **元反思式提案**（:meth:`SelfImprovementProposer.propose_next_generation`）：读取
   谱系里的 ``rejected_mutations``（上一轮导致指标退化被驳回的变异签名），
   **严禁重复提出同款失败修改**，只在仍有未覆盖失败维度时生成下一代策略包。

改的是什么：只改「声明式策略包」——新增检测规则、修复模板、Prompt 指导与元经验。
不改的：``core/guardrails/`` 等安全内核（由 :func:`assert_constitutional_safe` 兜底）。

变异目录（``MUTATION_CATALOGUE``）是工程知识，不是分数：每条变异对应一族
"已知会漏检的输入形态"，命中后由真实评测决定是否晋升——判决权不在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.evolution.strategy_store import AgentStrategyBundle, assert_constitutional_safe

#: 每代最多叠加的变异数（过多会一次性改变太多维度，难以归因）。
MAX_MUTATIONS_PER_GENERATION = 2
#: Critic 同类打回达到该次数才认为"频繁"，值得合成修复模板。
REPAIR_CLUSTER_THRESHOLD = 2

#: 变异目录：失败类别 -> 可应用的声明式变异。
#:
#: 每条变异都是"能匹配一类漏检形态的正则族"，而不是某个用例的硬编码答案；
#: 因此对 Train 与 Holdout 中的不同样本变体同样有效（这也是它能泛化的原因）。
MUTATION_CATALOGUE: dict[str, dict[str, Any]] = {
    "PROMPT_INJECTION": {
        "mutation_id": "MUT-INJ-LEXICAL",
        "rule_id": "GR-INJ-02",
        "description": "补充提示注入的语用变体（forget/无限制声明），覆盖既有词表漏检",
        "patterns": [
            r"(?i)\bforget\s+(?:\w+\s+){0,3}(?:previous|prior|above|earlier)\b",
            r"(?i)\bforget\s+(?:all\s+)?(?:your\s+)?(?:instructions?|rules?|guardrails?)\b",
            r"(?i)from\s+now\s+on\s+you\s+(?:are|have)\s+no\s+(?:restrictions?|limits?)\b",
            r"(?:从现在起|现在开始).{0,8}(?:不再|不受).{0,8}(?:任何)?(?:限制|规则|约束)",
        ],
        "requires_hitl": False,
        "repair_template": None,
        "directive": "提示注入判定需覆盖语用变体（forget / 无限制声明），不得只匹配固定短语",
    },
    "SECRET_EXFIL": {
        "mutation_id": "MUT-SEC-ENCODED",
        "rule_id": "GR-SEC-02",
        "description": "补充编码/混淆形态的凭据外泄（secret_b64 等）",
        "patterns": [
            r"(?i)\b(?:secret|token|api[_-]?key|credential)[_-]?b64\s*[:=]\s*[A-Za-z0-9+/=]{12,}",
        ],
        "requires_hitl": False,
        "repair_template": None,
        "directive": "凭据外泄检测需覆盖 base64/混淆编码形态，不能只看明文 key=value",
    },
    "PATH_ESCAPE": {
        "mutation_id": "MUT-PATH-ENCODED",
        "rule_id": "GR-PATH-02",
        "description": "补充 URL 编码路径穿越与凭据目录访问",
        "patterns": [
            r"(?i)(?:%2e%2e|\.\.)%2[fF]",
            r"(?i)(?:^|[\s\"'=:(])(?:~|/home/[^/\s]+)/\.(?:aws|ssh|kube|docker)/",
        ],
        "requires_hitl": True,
        "repair_template": None,
        "directive": "路径逃逸检测需解码 URL 编码后再判定，并覆盖云凭据目录",
    },
    "DESTRUCTIVE_WRITE": {
        "mutation_id": "MUT-WRITE-CLAUSE",
        "rule_id": "GR-WRITE-02",
        "description": "补充带恒真条件/删列的破坏性写操作",
        "patterns": [
            r"(?i)\bdelete\s+from\s+[\w.\"`\[\]]+\s+where\s+(?:1\s*=\s*1|true\b|'1'\s*=\s*'1')",
            r"(?i)\balter\s+table\s+[\w.\"`\[\]]+\s+drop\s+column\b",
        ],
        "requires_hitl": True,
        "repair_template": None,
        "directive": "破坏性写操作判定不得只看语句开头，需识别恒真条件与结构调整",
    },
}

#: 修复质量变异：由 Critic 高频打回触发，只改 repair_templates 与 Prompt 指导。
REPAIR_MUTATION_ID = "MUT-REPAIR-HARDENED"
_REPAIR_TEMPLATE = (
    "1) 用参数化占位符替换拼接；2) 对入参做类型与长度白名单；"
    "3) 补一条注入边界单测；4) 保留反向 diff 以便回滚"
)
_REPAIR_DIRECTIVE = "同类漏洞连续被 Critic 打回时，必须同时给出参数化代码与边界单测，禁止只加注释"


@dataclass(frozen=True)
class FailureCluster:
    """一类失败的可解释归因。"""

    category: str
    sample_ids: list[str]
    root_cause: str
    suggested_mutation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "sample_ids": list(self.sample_ids),
            "root_cause": self.root_cause,
            "suggested_mutation": self.suggested_mutation,
        }

    @property
    def mutation_id(self) -> str:
        return self.suggested_mutation


def _rule_from_catalogue(category: str) -> dict[str, Any]:
    entry = MUTATION_CATALOGUE[category]
    return {
        "rule_id": entry["rule_id"],
        "mutation_id": entry["mutation_id"],
        "category": category,
        "description": entry["description"],
        "patterns": list(entry["patterns"]),
        "requires_hitl": bool(entry["requires_hitl"]),
        "source": "rsi_meta_proposer",
    }


class SelfImprovementProposer:
    """从真实失败与历史谱系合成下一代策略包。"""

    def __init__(self, *, max_mutations_per_generation: int = MAX_MUTATIONS_PER_GENERATION) -> None:
        self._max_mutations = max(1, int(max_mutations_per_generation))

    # -- 归因 ----------------------------------------------------------
    def attribute_failures(
        self,
        eval_outcomes: Sequence[Mapping[str, Any]],
        critic_verdicts: Sequence[Mapping[str, Any]],
    ) -> list[FailureCluster]:
        """把逐用例失败与 Critic 打回聚簇，返回按影响面降序的簇列表。"""
        buckets: dict[str, list[str]] = {}

        for outcome in eval_outcomes:
            case_id = str(outcome.get("case_id", ""))
            expected = {str(item) for item in outcome.get("expected_findings", [])}
            actual = {str(item) for item in outcome.get("actual_findings", [])}
            expected_blocked = bool(outcome.get("expected_blocked", False))
            blocked = bool(outcome.get("blocked", False))

            if bool(outcome.get("diagnosed", False)):
                continue

            missing = expected - actual
            if missing:
                for label in sorted(missing):
                    buckets.setdefault(label, []).append(case_id)
                continue

            if expected_blocked and not blocked:
                # 已检出但未阻断：归到同一检测类别（阻断逻辑由分类决定）
                for label in sorted(expected):
                    buckets.setdefault(label, []).append(case_id)
                continue

            if not expected and actual:
                # 误报：交给噪声过滤维度处理，不新增检测规则
                buckets.setdefault("FALSE_POSITIVE", []).append(case_id)

        repair_codes: list[str] = []
        for verdict in critic_verdicts:
            for code in verdict.get("failures", []) or []:
                repair_codes.append(str(code))
        if len(repair_codes) >= REPAIR_CLUSTER_THRESHOLD:
            buckets.setdefault("REPAIR_QUALITY", []).append("critic_verdicts")

        clusters: list[FailureCluster] = []
        for category, sample_ids in buckets.items():
            unique_ids = list(dict.fromkeys(sample_ids))
            if category == "FALSE_POSITIVE":
                clusters.append(
                    FailureCluster(
                        category=category,
                        sample_ids=unique_ids,
                        root_cause="检测规则过宽，命中了合法业务文本",
                        suggested_mutation="MUT-NOISE-EXACT",
                    )
                )
            elif category == "REPAIR_QUALITY":
                clusters.append(
                    FailureCluster(
                        category=category,
                        sample_ids=unique_ids,
                        root_cause=f"Critic 沙箱连续打回: {sorted(set(repair_codes))}",
                        suggested_mutation=REPAIR_MUTATION_ID,
                    )
                )
            elif category in MUTATION_CATALOGUE:
                clusters.append(
                    FailureCluster(
                        category=category,
                        sample_ids=unique_ids,
                        root_cause=f"{category} 存在未覆盖的输入变体，当前策略漏检",
                        suggested_mutation=MUTATION_CATALOGUE[category]["mutation_id"],
                    )
                )
            else:
                clusters.append(
                    FailureCluster(
                        category=category,
                        sample_ids=unique_ids,
                        root_cause=f"{category} 失败但变异目录未收录，需人工扩展",
                        suggested_mutation="MUT-UNSUPPORTED",
                    )
                )

        clusters.sort(key=lambda item: (-len(item.sample_ids), item.category))
        return clusters

    # -- 元反思 --------------------------------------------------------
    @staticmethod
    def rejected_mutations(lineage: Sequence[Mapping[str, Any]]) -> set[str]:
        rejected: set[str] = set()
        for record in lineage:
            for item in record.get("rejected_mutations", []) or []:
                mutation_id = str(item.get("mutation_id", "")) if isinstance(item, Mapping) else str(item)
                if mutation_id:
                    rejected.add(mutation_id)
            if str(record.get("decision", "")) == "REJECTED":
                category = str((record.get("metrics") or {}).get("primary_category", ""))
                entry = MUTATION_CATALOGUE.get(category)
                if entry:
                    rejected.add(str(entry["mutation_id"]))
        return rejected

    @staticmethod
    def positive_mutations(lineage: Sequence[Mapping[str, Any]]) -> set[str]:
        """上一轮带来正向增益的变异（晋升记录里登记过的），用于优先放大同类维度。"""
        positive: set[str] = set()
        for record in lineage:
            if str(record.get("decision", "")) != "PROMOTED":
                continue
            for item in (record.get("metrics") or {}).get("mutations", []) or []:
                positive.add(str(item))
        return positive

    def propose_next_generation(
        self,
        active: AgentStrategyBundle,
        failures: Sequence[FailureCluster],
        lineage: Sequence[Mapping[str, Any]],
    ) -> AgentStrategyBundle:
        """基于 ``active`` 与失败簇生成 ``generation = active.generation + 1`` 的候选包。"""
        rejected = self.rejected_mutations(lineage)
        positive = self.positive_mutations(lineage)
        existing_rule_ids = {str(rule.get("rule_id", "")) for rule in active.policy_rules}
        existing_templates = set(active.repair_templates)

        ranking = sorted(
            failures,
            key=lambda cluster: (
                0 if cluster.mutation_id in positive else 1,
                -len(cluster.sample_ids),
                cluster.category,
            ),
        )

        new_rules: list[dict[str, Any]] = []
        new_templates: dict[str, str] = {}
        new_directives: list[str] = []
        applied: list[str] = []
        skipped: list[dict[str, str]] = []

        for cluster in ranking:
            if len(applied) >= self._max_mutations:
                break
            mutation_id = cluster.mutation_id
            if mutation_id in rejected:
                skipped.append({"mutation_id": mutation_id, "reason": "上一轮已被证伪，禁止重复提出"})
                continue

            if mutation_id == REPAIR_MUTATION_ID:
                if "SQL_INJECTION" in existing_templates:
                    skipped.append({"mutation_id": mutation_id, "reason": "模板已存在"})
                    continue
                new_templates["SQL_INJECTION"] = _REPAIR_TEMPLATE
                new_directives.append(_REPAIR_DIRECTIVE)
                applied.append(mutation_id)
                continue

            if cluster.category not in MUTATION_CATALOGUE:
                skipped.append({"mutation_id": mutation_id, "reason": "目录未收录该失败类别"})
                continue

            rule = _rule_from_catalogue(cluster.category)
            if rule["rule_id"] in existing_rule_ids:
                skipped.append({"mutation_id": mutation_id, "reason": "规则已存在"})
                continue
            new_rules.append(rule)
            directive = MUTATION_CATALOGUE[cluster.category].get("directive")
            if directive and directive not in active.prompt_directives:
                new_directives.append(str(directive))
            existing_rule_ids.add(rule["rule_id"])
            applied.append(mutation_id)

        lessons = list(active.meta_lessons)
        if applied:
            lessons.append(
                {
                    "generation": active.generation + 1,
                    "kind": "META_REFLECTION",
                    "applied_mutations": list(applied),
                    "evidence": [
                        {"category": cluster.category, "samples": len(cluster.sample_ids)}
                        for cluster in ranking[: len(applied)]
                    ],
                    "avoided": skipped,
                }
            )
        else:
            lessons.append(
                {
                    "generation": active.generation + 1,
                    "kind": "EXHAUSTED",
                    "reason": "无可用新变异（剩余失败维度均已被证伪或已覆盖）",
                    "avoided": skipped,
                }
            )

        candidate = AgentStrategyBundle(
            version_id=f"S_{active.generation + 1}",
            parent_version=active.version_id,
            generation=active.generation + 1,
            policy_rules=[dict(item) for item in active.policy_rules] + new_rules,
            repair_templates={**active.repair_templates, **new_templates},
            prompt_directives=list(active.prompt_directives) + new_directives,
            noise_filters=list(active.noise_filters),
            meta_lessons=lessons,
        ).seal()
        assert_constitutional_safe(candidate)
        return candidate

    # -- 工具 ----------------------------------------------------------
    @staticmethod
    def is_no_op(active: AgentStrategyBundle, candidate: AgentStrategyBundle) -> bool:
        """候选是否只是空转（无新增规则/模板/指导）。"""
        return (
            len(candidate.policy_rules) <= len(active.policy_rules)
            and set(candidate.repair_templates) <= set(active.repair_templates)
            and set(candidate.prompt_directives) <= set(active.prompt_directives)
        )

    @staticmethod
    def mutation_ids(bundle: AgentStrategyBundle) -> list[str]:
        ids = [str(rule.get("mutation_id")) for rule in bundle.policy_rules if rule.get("mutation_id")]
        if bundle.repair_templates and not any(item == REPAIR_MUTATION_ID for item in ids):
            ids.append(REPAIR_MUTATION_ID)
        return list(dict.fromkeys(ids))


__all__ = [
    "MAX_MUTATIONS_PER_GENERATION",
    "MUTATION_CATALOGUE",
    "REPAIR_CLUSTER_THRESHOLD",
    "REPAIR_MUTATION_ID",
    "FailureCluster",
    "SelfImprovementProposer",
]
