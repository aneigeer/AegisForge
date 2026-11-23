"""红队审查与沙箱验证 Agent（AGENT_CRITIC）。

双重职责：

1. **沙箱验证**：调用领域插件的 Harness 跑 PoC 复现 -> 修复 Dry-Run -> 回滚可逆性验证，
   并把生产只读预检（>30s 活跃长事务）作为硬拦截线；
2. **红队复核**：独立于插件的静态检查——回滚是否就绪、是否声明变更窗口、
   是否触碰 30s 阻塞预算、修复内容是否为空。插件报告"通过"不代表红队放行。

自愈闭环的硬约束（``INV-CRITIC-LOOP-BOUND``）：

- ``max_rounds`` 构造期即钳制在 3 以内，超出直接报错；
- 打回时把**结构化失败码**回传专家 Agent，轮次 +1；
- 轮次用尽仍失败 -> ``exhausted=True``，挂起转人工 HITL，并写入 WORM 审计链，
  **绝不带病放行**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.agents.base import AgentContext, BaseAgent
from core.agents.supervisor import TICKET_ABORTED, TICKET_CRITIC_PASSED, TICKET_EXHAUSTED, TICKET_FILTERED
from core.orchestrator.state_graph import MAX_SELF_CORRECTION_ROUNDS, NodeOutcome, SecOpsState
from core.platform.ten_modules import RbacRole
from core.plugins.contracts import PROD_BLOCK_BUDGET_MS
from core.plugins.registry import preflight_tool_name, sandbox_tool_name

VERDICT_PASSED = "PASSED"
VERDICT_REJECTED = "REJECTED"
VERDICT_FILTERED = "FILTERED"
VERDICT_BLOCKED = "BLOCKED"


class CriticAgent(BaseAgent):
    """生成—对抗验证闭环的"守门人"。"""

    agent_id = "AGENT_CRITIC"

    def __init__(self, context: AgentContext, *, max_rounds: int | None = None) -> None:
        super().__init__(context)
        configured = context.max_self_correction_rounds if max_rounds is None else int(max_rounds)
        if configured < 1:
            raise ValueError("max_rounds 至少为 1")
        if configured > MAX_SELF_CORRECTION_ROUNDS:
            raise ValueError(f"max_rounds 不得超过硬上限 {MAX_SELF_CORRECTION_ROUNDS}")
        self.max_rounds = configured

    # -- 节点 ----------------------------------------------------------
    def run(self, state: SecOpsState) -> NodeOutcome:
        proposal = state.domain_patches.get(state.active_domain) or {}
        ticket = state.current_ticket() or {}
        if not proposal:
            return self._blocked_without_proposal(state, ticket)

        proposal_id = str(proposal["proposal_id"])
        round_no = max(1, int(proposal.get("round", state.self_correction_round or 1)))
        long_tx = self._long_tx(state, proposal)

        result = self.ctx.tools.call(
            sandbox_tool_name(state.active_domain),
            {"proposal_id": proposal_id, "long_tx_count": str(long_tx)},
            workflow_run_id=state.workflow_run_id,
        )
        if not result.ok:
            verdict = self._verdict(
                state,
                proposal,
                verdict=VERDICT_BLOCKED,
                failures=[result.error_code or "SANDBOX_UNAVAILABLE"],
                round_no=round_no,
                sandbox={"error": result.error_message},
                red_team=[],
                logs=["沙箱不可用，无法完成对抗验证"],
            )
            return self._escalate(state, verdict, reason="沙箱验证不可用，必须人工介入")

        sandbox = dict(result.data)
        failures = list(sandbox.get("failures", []))
        red_team = self._red_team_checks(proposal)
        for code in red_team:
            if code not in failures:
                failures.append(code)

        if not sandbox.get("poc_reproduced", False):
            verdict_kind = VERDICT_FILTERED
        elif failures:
            verdict_kind = VERDICT_REJECTED
        else:
            verdict_kind = VERDICT_PASSED

        exhausted = verdict_kind == VERDICT_REJECTED and round_no >= self.max_rounds
        verdict = self._verdict(
            state,
            proposal,
            verdict=verdict_kind,
            failures=failures,
            round_no=round_no,
            sandbox=sandbox,
            red_team=red_team,
            logs=list(sandbox.get("logs", [])),
            exhausted=exhausted,
        )
        state.critic_verdicts[proposal_id] = verdict

        if verdict_kind == VERDICT_FILTERED:
            state.mark_ticket(ticket.get("ticket_id", ""), TICKET_FILTERED)
            self.worm("AGENT_CRITIC_FALSE_POSITIVE_FILTERED", {"proposal_id": proposal_id, "finding_id": proposal["finding_id"]})
            return NodeOutcome(
                patches={"critic_verdicts": state.critic_verdicts, "triage_tickets": state.triage_tickets},
                events=[{"event": "CRITIC_FILTERED_FALSE_POSITIVE", "detail": {"proposal_id": proposal_id}}],
            )

        if verdict_kind == VERDICT_PASSED:
            state.mark_ticket(ticket.get("ticket_id", ""), TICKET_CRITIC_PASSED)
            self.worm(
                "AGENT_CRITIC_PASSED",
                {
                    "proposal_id": proposal_id,
                    "finding_id": proposal["finding_id"],
                    "round": round_no,
                    "compatibility_score": sandbox.get("compatibility_score", 0.0),
                    "approval_digest": proposal.get("approval_digest", ""),
                },
            )
            return NodeOutcome(
                patches={"critic_verdicts": state.critic_verdicts, "triage_tickets": state.triage_tickets},
                events=[
                    {
                        "event": "CRITIC_PASSED",
                        "detail": {
                            "proposal_id": proposal_id,
                            "round": round_no,
                            "self_correction_used": round_no - 1,
                        },
                    }
                ],
            )

        if exhausted:
            return self._escalate(state, verdict, reason=f"Self-Correction 达到 {self.max_rounds} 轮上限仍失败")

        # 打回专家 Agent 触发 Self-Correction
        next_round = round_no + 1
        self.worm(
            "AGENT_CRITIC_SELF_CORRECTION_DISPATCHED",
            {
                "proposal_id": proposal_id,
                "finding_id": proposal["finding_id"],
                "from_round": round_no,
                "to_round": next_round,
                "max_rounds": self.max_rounds,
                "failures": failures,
            },
            actor_role=RbacRole.SEC.value,
        )
        return NodeOutcome(
            patches={
                "critic_verdicts": state.critic_verdicts,
                "self_correction_round": next_round,
                "pending_feedback": failures,
                "triage_tickets": state.triage_tickets,
            },
            events=[
                {
                    "event": "CRITIC_REJECTED_SELF_CORRECTION",
                    "detail": {
                        "proposal_id": proposal_id,
                        "round": round_no,
                        "next_round": next_round,
                        "max_rounds": self.max_rounds,
                        "failures": failures,
                    },
                }
            ],
        )

    # -- 内部 ----------------------------------------------------------
    def _long_tx(self, state: SecOpsState, proposal: dict[str, Any]) -> int:
        """先做生产只读预检；工具缺失时按 0 处理并记录，绝不阻塞主流程。"""
        result = self.ctx.tools.call(
            preflight_tool_name(state.active_domain),
            {"asset_id": str(proposal["asset_id"])},
            workflow_run_id=state.workflow_run_id,
        )
        if not result.ok:
            return 0
        return int(result.data.get("active_long_tx_count", 0))

    def _red_team_checks(self, proposal: dict[str, Any]) -> list[str]:
        """红队静态复核：与沙箱结果相互独立，任一条命中即拒绝。"""
        failures: list[str] = []
        rollback = dict(proposal.get("rollback") or {})
        impact = dict(proposal.get("impact") or {})

        if not rollback.get("armed", False):
            failures.append("MISSING_ROLLBACK")
        if not str(proposal.get("standard_fix", "")).strip():
            failures.append("EMPTY_FIX")
        if int(impact.get("estimated_block_ms", 0)) > PROD_BLOCK_BUDGET_MS and not impact.get("deferred", False):
            failures.append("PROD_PREFLIGHT_INTERCEPT")
        if impact.get("lock_impact_level") == "TABLE_EXCLUSIVE" and not impact.get("change_window"):
            failures.append("LOCK_ESCALATION_WITHOUT_WINDOW")
        return failures

    def _verdict(
        self,
        state: SecOpsState,
        proposal: dict[str, Any],
        *,
        verdict: str,
        failures: list[str],
        round_no: int,
        sandbox: dict[str, Any],
        red_team: list[str],
        logs: list[str],
        exhausted: bool = False,
    ) -> dict[str, Any]:
        return {
            "verdict_id": f"verdict-{proposal['proposal_id']}-r{round_no}",
            "proposal_id": str(proposal["proposal_id"]),
            "finding_id": str(proposal["finding_id"]),
            "asset_id": str(proposal["asset_id"]),
            "domain": str(proposal["domain"]),
            "verdict": verdict,
            "failures": list(failures),
            "red_team_findings": list(red_team),
            "round": round_no,
            "max_rounds": self.max_rounds,
            "exhausted": exhausted,
            "sandbox": sandbox,
            "logs": list(logs),
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "critic_role": self.role_label,
        }

    def _blocked_without_proposal(self, state: SecOpsState, ticket: dict[str, Any]) -> NodeOutcome:
        verdict = {
            "verdict_id": f"verdict-{state.active_ticket_id}-no-proposal",
            "proposal_id": "",
            "finding_id": str(ticket.get("finding_id", "")),
            "asset_id": str(ticket.get("asset_id", "")),
            "domain": str(state.active_domain),
            "verdict": VERDICT_BLOCKED,
            "failures": ["NO_PROPOSAL"],
            "red_team_findings": [],
            "round": max(1, int(state.self_correction_round or 1)),
            "max_rounds": self.max_rounds,
            "exhausted": True,
            "sandbox": {},
            "logs": ["领域专家未提交修复方案"],
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "critic_role": self.role_label,
        }
        state.critic_verdicts[str(state.active_ticket_id)] = verdict
        state.mark_ticket(ticket.get("ticket_id", ""), TICKET_ABORTED)
        self.worm(
            "AGENT_CRITIC_SELF_CORRECTION_EXHAUSTED",
            {"ticket_id": state.active_ticket_id, "reason": "NO_PROPOSAL", "max_rounds": self.max_rounds},
            actor_role=RbacRole.SEC.value,
        )
        return NodeOutcome(
            patches={
                "critic_verdicts": state.critic_verdicts,
                "triage_tickets": state.triage_tickets,
                "hitl_status": "PENDING",
            },
            events=[{"event": "CRITIC_BLOCKED_NO_PROPOSAL", "detail": {"ticket_id": state.active_ticket_id}}],
        )

    def _escalate(self, state: SecOpsState, verdict: dict[str, Any], *, reason: str) -> NodeOutcome:
        """自愈轮次耗尽：挂起转人工，并写入不可篡改审计链。"""
        proposal = state.domain_patches.get(state.active_domain) or {}
        ticket = state.current_ticket() or {}
        state.mark_ticket(ticket.get("ticket_id", ""), TICKET_EXHAUSTED)
        verdict = {**verdict, "exhausted": True, "escalation_reason": reason}
        state.critic_verdicts[str(verdict["proposal_id"])] = verdict

        self.worm(
            "AGENT_CRITIC_SELF_CORRECTION_EXHAUSTED",
            {
                "proposal_id": verdict.get("proposal_id", ""),
                "finding_id": verdict.get("finding_id", ""),
                "domain": verdict.get("domain", ""),
                "round": verdict.get("round", 0),
                "max_rounds": self.max_rounds,
                "failures": verdict.get("failures", []),
                "reason": reason,
                "action": "SUSPENDED_TO_HITL",
            },
            actor_role=RbacRole.SEC.value,
        )
        return NodeOutcome(
            patches={
                "critic_verdicts": state.critic_verdicts,
                "triage_tickets": state.triage_tickets,
                "hitl_status": "PENDING",
                "active_proposal_id": str(proposal.get("proposal_id", "")),
            },
            events=[
                {
                    "event": "CRITIC_SELF_CORRECTION_EXHAUSTED",
                    "detail": {
                        "proposal_id": verdict.get("proposal_id", ""),
                        "round": verdict.get("round", 0),
                        "max_rounds": self.max_rounds,
                        "reason": reason,
                    },
                }
            ],
        )


__all__ = ["CriticAgent", "VERDICT_BLOCKED", "VERDICT_FILTERED", "VERDICT_PASSED", "VERDICT_REJECTED"]
