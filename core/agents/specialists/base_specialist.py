"""领域专家 Agent 通用基类。

所有业务域的专家 Agent 共享同一套"生成—提交—自检—等打回"流程，
差异只在 ``domain``（决定调用哪个域的 ``<domain>_fix_generate`` 工具）
与 ``agent_id``（桌面端角色徽章）。

这样新增业务域时只需：
1. 实现插件（Probe/Masker/Harness/Executor）；
2. 声明 ``class XxxSpecialistAgent(DomainSpecialistAgent): domain = "xxx"``；
3. 在 ``SecOpsOrchestrator._specialists`` 注册。
"""

from __future__ import annotations

from typing import Any

from core.agents.base import AgentContext, BaseAgent
from core.agents.supervisor import TICKET_IN_PROGRESS
from core.orchestrator.state_graph import NodeOutcome, SecOpsState
from core.plugins.registry import fix_tool_name


class DomainSpecialistAgent(BaseAgent):
    """领域专家 Agent 的通用实现。"""

    domain: str = ""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        if not self.domain:
            raise ValueError("领域专家 Agent 必须声明 domain")

    def run(self, state: SecOpsState) -> NodeOutcome:
        ticket = state.current_ticket() or {}
        round_no = max(1, int(state.self_correction_round or 1))
        feedback = ",".join(str(code) for code in state.pending_feedback)
        tag = self.domain.upper()

        result = self.ctx.tools.call(
            fix_tool_name(self.domain),
            {
                "asset_id": str(ticket.get("asset_id", "")),
                "finding_id": str(ticket.get("finding_id", "")),
                "round": str(round_no),
                "feedback": feedback,
            },
            workflow_run_id=state.workflow_run_id,
        )
        if not result.ok:
            self.worm(
                f"AGENT_{tag}_PROPOSAL_FAILED",
                {
                    "ticket_id": state.active_ticket_id,
                    "round": round_no,
                    "error_code": result.error_code,
                    "error": result.error_message,
                },
            )
            return NodeOutcome(
                patches={
                    "domain_patches": {**state.domain_patches, self.domain: {}},
                    "pending_feedback": [result.error_code or "FIX_GENERATION_FAILED"],
                },
                events=[
                    {
                        "event": f"{tag}_AGENT_PROPOSAL_FAILED",
                        "detail": {"ticket_id": state.active_ticket_id, "error_code": result.error_code},
                    }
                ],
            )

        proposal: dict[str, Any] = dict(result.data)
        self._self_check(proposal)

        history = list(ticket.get("history", []))
        history.append(
            {
                "round": round_no,
                "proposal_id": proposal.get("proposal_id", ""),
                "feedback": feedback,
                "deferred": bool(proposal.get("impact", {}).get("deferred", False)),
            }
        )
        state.mark_ticket(str(ticket.get("ticket_id", "")), TICKET_IN_PROGRESS, history=history)

        self.worm(
            f"AGENT_{tag}_PROPOSAL_SUBMITTED",
            {
                "ticket_id": state.active_ticket_id,
                "proposal_id": proposal.get("proposal_id", ""),
                "round": round_no,
                "feedback": feedback,
                "approval_digest": proposal.get("approval_digest", ""),
                "lock_impact": proposal.get("impact", {}).get("lock_impact_level", ""),
                "deferred": bool(proposal.get("impact", {}).get("deferred", False)),
            },
        )
        return NodeOutcome(
            patches={
                "domain_patches": {**state.domain_patches, self.domain: proposal},
                "proposal_snapshots": {
                    **state.proposal_snapshots,
                    str(proposal.get("proposal_id", "")): proposal,
                },
                "active_proposal_id": str(proposal.get("proposal_id", "")),
                "active_domain": self.domain,
                "self_correction_round": round_no,
                "pending_feedback": [],
                "triage_tickets": state.triage_tickets,
            },
            events=[
                {
                    "event": f"{tag}_AGENT_PROPOSAL_SUBMITTED",
                    "detail": {
                        "proposal_id": proposal.get("proposal_id", ""),
                        "round": round_no,
                        "feedback": feedback,
                        "deferred": bool(proposal.get("impact", {}).get("deferred", False)),
                    },
                }
            ],
        )

    def _self_check(self, proposal: dict[str, Any]) -> None:
        """提交前自检：必然被打回的问题显式记录，仍交由 Critic 裁决。"""
        notes = list(proposal.get("notes", []))
        rollback = dict(proposal.get("rollback") or {})
        if not rollback.get("armed", False):
            notes.append("自检: 回滚计划未就绪，预期将被 Critic 打回")
        impact = dict(proposal.get("impact") or {})
        if impact.get("lock_impact_level") == "TABLE_EXCLUSIVE" and not impact.get("change_window"):
            notes.append("自检: 排他锁变更未声明变更窗口")
        proposal["notes"] = notes


__all__ = ["DomainSpecialistAgent"]
