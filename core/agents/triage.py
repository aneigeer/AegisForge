"""研判与上下文定级 Agent（AGENT_TRIAGE）。

职责（``five_core_agents[AGENT_TRIAGE]``）：

- 联动资产三级标签（业务等级 / 暴露面 / 敏感数据密度）评估真实业务风险；
- 调用各域插件只读 Probe 收集发现项，再用 V2.0 :class:`ContextualRiskScorer`
  统一打分，输出 P0~P3 标准化工单；
- 过滤内网隔离低敏感度噪音告警（``suppressed``），噪音工单不进入修复流水线；
- 工单只携带**脱敏后**的证据文本，原文留在控制面进程内。
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.agents.base import DOMAIN_AGENT_IDS, AgentContext, BaseAgent, asset_from_dict
from core.orchestrator.state_graph import NodeOutcome, SecOpsState
from core.platform.ten_modules import ContextualRiskScorer, VulnerabilityFinding
from core.plugins.contracts import scoring_record
from core.plugins.registry import evidence_tool_name, probe_tool_name

TICKET_OPEN = "OPEN"
TICKET_SUPPRESSED = "SUPPRESSED"

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class TriageAgent(BaseAgent):
    """把原始探测发现项收敛为可执行的优先级工单。"""

    agent_id = "AGENT_TRIAGE"

    def __init__(self, context: AgentContext, *, scorer: ContextualRiskScorer | None = None) -> None:
        super().__init__(context)
        self._scorer = scorer or ContextualRiskScorer()

    def run(self, state: SecOpsState) -> NodeOutcome:
        assets = [asset_from_dict(item) for item in state.target_assets]
        asset_map = {asset.asset_id: asset for asset in assets}
        collected: list[dict[str, Any]] = []
        probe_meta: dict[str, Any] = {}

        for asset in assets:
            tool = probe_tool_name(asset.domain)
            result = self.ctx.tools.call(tool, {"asset_id": asset.asset_id}, workflow_run_id=state.workflow_run_id)
            if not result.ok:
                probe_meta[asset.asset_id] = {"error": result.error_code, "tool": tool}
                continue
            probe_meta[asset.asset_id] = {
                "tool": result.data.get("probe_tool", tool),
                "read_only": result.data.get("read_only", True),
                "budget": result.data.get("budget", {}),
                "finding_count": len(result.data.get("findings", [])),
            }
            collected.extend(result.data.get("findings", []))

        scored = self._score(collected, asset_map)
        by_id = {item["finding_id"]: item for item in collected}
        tickets: list[dict[str, Any]] = []

        for finding in scored:
            source = by_id.get(finding.finding_id, {})
            domain = str(source.get("domain", asset_map[finding.asset_id].domain))
            ticket_id = "TKT-" + hashlib.sha256(
                f"{state.workflow_run_id}:{finding.finding_id}".encode("utf-8")
            ).hexdigest()[:8].upper()
            status = TICKET_SUPPRESSED if finding.suppressed_as_noise else TICKET_OPEN

            masked_evidence, modality = "", ""
            if status == TICKET_OPEN:
                masked_evidence, modality = self._masked_evidence(state, domain, finding.finding_id)

            tickets.append(
                {
                    "ticket_id": ticket_id,
                    "finding_id": finding.finding_id,
                    "asset_id": finding.asset_id,
                    "domain": domain,
                    "title": finding.title,
                    "category": finding.category,
                    "rule_id": finding.rule_id,
                    "cve_id": finding.cve_id,
                    "location": source.get("location", ""),
                    "base_cvss": finding.base_cvss,
                    "priority": finding.contextual_priority,
                    "score": finding.contextual_score,
                    "suppressed": finding.suppressed_as_noise,
                    "status": status,
                    "masked_evidence": masked_evidence,
                    "evidence_modality": modality,
                    "owner_agent": DOMAIN_AGENT_IDS.get(domain, ""),
                    "long_tx_count": 0,
                    "history": [],
                }
            )

        tickets.sort(key=lambda item: (_PRIORITY_ORDER.get(item["priority"], 9), -float(item["score"])))
        metrics = dict(state.metrics)
        metrics["triage"] = {
            "assets_probed": len(assets),
            "findings_total": len(collected),
            "tickets_open": sum(1 for item in tickets if item["status"] == TICKET_OPEN),
            "tickets_suppressed": sum(1 for item in tickets if item["status"] == TICKET_SUPPRESSED),
            "by_priority": {
                priority: sum(1 for item in tickets if item["priority"] == priority)
                for priority in ("P0", "P1", "P2", "P3")
            },
            "probe": probe_meta,
        }

        self.worm(
            "AGENT_TRIAGE_TICKETS_ISSUED",
            {
                "tickets": len(tickets),
                "open": metrics["triage"]["tickets_open"],
                "suppressed": metrics["triage"]["tickets_suppressed"],
                "by_priority": metrics["triage"]["by_priority"],
            },
        )
        return NodeOutcome(
            patches={"triage_tickets": tickets, "metrics": metrics},
            events=[
                {
                    "event": "TRIAGE_SCORED",
                    "detail": {
                        "tickets": len(tickets),
                        "open": metrics["triage"]["tickets_open"],
                        "suppressed": metrics["triage"]["tickets_suppressed"],
                    },
                }
            ],
        )

    # -- 内部 ----------------------------------------------------------
    def _score(
        self,
        findings: list[dict[str, Any]],
        asset_map: dict[str, Any],
    ) -> list[VulnerabilityFinding]:
        """复用 V2.0 定级算法：任何业务域都投影到同一套权重与阈值。"""
        projected: list[VulnerabilityFinding] = []
        for item in findings:
            projected.append(
                VulnerabilityFinding(
                    finding_id=str(item["finding_id"]),
                    asset_id=str(item["asset_id"]),
                    category=str(item["category"]),
                    rule_id=str(item["rule_id"]),
                    cve_id=item.get("cve_id"),
                    title=str(item["title"]),
                    raw_evidence="",
                    base_cvss=float(item["base_cvss"]),
                )
            )
        if not projected:
            return []
        records = {asset_id: scoring_record(asset) for asset_id, asset in asset_map.items()}
        return self._scorer.score_and_prioritize(projected, records)

    def _masked_evidence(self, state: SecOpsState, domain: str, finding_id: str) -> tuple[str, str]:
        tool = evidence_tool_name(domain)
        run_id = f"{state.workflow_run_id}:{finding_id}"
        result = self.ctx.tools.call(
            tool,
            {"finding_id": finding_id, "workflow_run_id": run_id},
            workflow_run_id=state.workflow_run_id,
        )
        if not result.ok:
            return "", ""
        return str(result.data.get("masked_text", "")), str(result.data.get("modality", ""))


__all__ = ["TICKET_OPEN", "TICKET_SUPPRESSED", "TriageAgent"]
