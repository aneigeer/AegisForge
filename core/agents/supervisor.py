"""总控调度 Agent（AGENT_SUPERVISOR / Planner）。

职责（``five_core_agents[AGENT_SUPERVISOR]``）：

- 接收定时巡检或 SIEM 告警，识别受影响资产域并构造确定性执行计划；
- 按工单优先级**串行**分发到领域专家 Agent（串行是确定性保障：同输入必然同轨迹）；
- 维护跨栈分支预算，超出即截断并记录（防止单次联动把 Token 与沙箱配额打满）；
- 只有状态机读写权限，**没有任何生产写工具权限**。

Router 是纯函数：只读 ``state``，不修改状态，因此可以安全地被图重复调用。
"""

from __future__ import annotations

from core.agents.base import AgentContext, BaseAgent, asset_from_dict
from core.orchestrator.state_graph import GraphBudget, NodeOutcome, SecOpsState

TICKET_OPEN = "OPEN"
TICKET_IN_PROGRESS = "IN_PROGRESS"
TICKET_CRITIC_PASSED = "CRITIC_PASSED"
TICKET_FILTERED = "FILTERED_FALSE_POSITIVE"
TICKET_EXHAUSTED = "EXHAUSTED"
TICKET_EXECUTED = "EXECUTED"
TICKET_REJECTED = "REJECTED_BY_HUMAN"
TICKET_BLOCKED = "EXECUTION_BLOCKED"
TICKET_ABORTED = "ABORTED"


class SupervisorAgent(BaseAgent):
    """负责"看全局、排顺序、控预算"，不产出任何具体修复内容。"""

    agent_id = "AGENT_SUPERVISOR"

    def __init__(self, context: AgentContext, *, budget: GraphBudget | None = None) -> None:
        super().__init__(context)
        self._budget = budget or GraphBudget(max_branches=context.max_branches)

    # -- 节点：入口受理 --------------------------------------------------
    def intake(self, state: SecOpsState) -> NodeOutcome:
        assets = [asset_from_dict(item) for item in state.target_assets]
        domains = sorted({asset.domain for asset in assets if asset.domain})
        allowed = domains[: self._budget.max_branches]
        truncated = len(domains) > len(allowed)
        plan = {
            "trigger": state.source_trigger,
            "domains": allowed,
            "dropped_domains": domains[len(allowed):],
            "branch_count": len(allowed),
            "max_branches": self._budget.max_branches,
            "asset_count": len(assets),
            "max_steps": self._budget.max_steps,
            "max_self_correction_rounds": self._budget.max_self_correction_rounds,
        }
        notes = [] if not truncated else [f"分支预算 {self._budget.max_branches} 已满，忽略域 {plan['dropped_domains']}"]
        self.worm("AGENT_SUPERVISOR_PLAN_CREATED", plan)
        return NodeOutcome(
            patches={"supervisor_plan": plan},
            events=[
                {
                    "event": "SUPERVISOR_PLAN_CREATED",
                    "detail": {**plan, "truncated": truncated, "notes": notes},
                }
            ],
        )

    # -- 节点：分发 ------------------------------------------------------
    def dispatch(self, state: SecOpsState) -> NodeOutcome:
        tickets = state.triage_tickets
        pending = [item for item in tickets if item.get("status") == TICKET_OPEN]
        if not pending:
            metrics = dict(state.metrics)
            metrics["dispatch"] = {
                "tickets_total": len(tickets),
                "executed": sum(1 for item in tickets if item.get("status") in (TICKET_EXECUTED, TICKET_CRITIC_PASSED)),
                "filtered": sum(1 for item in tickets if item.get("status") == TICKET_FILTERED),
                "exhausted": sum(1 for item in tickets if item.get("status") == TICKET_EXHAUSTED),
                "rejected": sum(1 for item in tickets if item.get("status") == TICKET_REJECTED),
            }
            return NodeOutcome(
                patches={
                    "active_ticket_id": "",
                    "active_domain": "",
                    "active_finding_id": "",
                    "active_proposal_id": "",
                    "dispatch_cursor": len(tickets),
                    "metrics": metrics,
                },
                events=[{"event": "SUPERVISOR_DISPATCH_COMPLETE", "detail": metrics["dispatch"]}],
            )

        ticket = pending[0]
        index = tickets.index(ticket)
        state.mark_ticket(ticket["ticket_id"], TICKET_IN_PROGRESS)
        return NodeOutcome(
            patches={
                "active_ticket_id": ticket["ticket_id"],
                "active_finding_id": ticket["finding_id"],
                "active_domain": ticket["domain"],
                "active_proposal_id": "",
                "self_correction_round": 1,
                "pending_feedback": [],
                "dispatch_cursor": index,
                "triage_tickets": state.triage_tickets,
            },
            events=[
                {
                    "event": "SUPERVISOR_DISPATCHED",
                    "detail": {
                        "ticket_id": ticket["ticket_id"],
                        "domain": ticket["domain"],
                        "priority": ticket["priority"],
                        "owner_agent": ticket.get("owner_agent", ""),
                    },
                }
            ],
        )

    # -- Router（纯函数） ------------------------------------------------
    def route_specialist(self, state: SecOpsState) -> str:
        if not state.active_domain:
            return "evolution"
        return f"specialist_{state.active_domain}"

    def route_after_critic(self, state: SecOpsState) -> str:
        verdict = state.critic_verdicts.get(state.active_proposal_id)
        if not verdict:
            return "hitl"
        if verdict.get("verdict") == "REJECTED" and not verdict.get("exhausted"):
            return f"specialist_{state.active_domain}"
        return "hitl"


__all__ = [
    "SupervisorAgent",
    "TICKET_ABORTED",
    "TICKET_BLOCKED",
    "TICKET_CRITIC_PASSED",
    "TICKET_EXECUTED",
    "TICKET_EXHAUSTED",
    "TICKET_FILTERED",
    "TICKET_IN_PROGRESS",
    "TICKET_OPEN",
    "TICKET_REJECTED",
]
