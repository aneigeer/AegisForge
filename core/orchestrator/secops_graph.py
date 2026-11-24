"""SecOps 多 Agent 协作图装配（V2.5 起，Phase 3/4 平滑扩展）。

图拓扑（确定性 DAG，唯一回环是 Critic 自愈边）：

```
supervisor_intake -> triage -> dispatch
dispatch --(router: 下一个 OPEN 工单)--> specialist_<domain> | evolution
specialist_<domain> -> critic
critic --(router, 自愈边)--> specialist_<domain> | hitl
critic --(router, 超限/拒绝)--> hitl
hitl -> execute -> dispatch
evolution -> finalize -> END
```

控制面节点（``hitl`` / ``execute`` / ``finalize``）不是 Agent，只做人工断点、
受控执行与收尾统计；它们同样遵循"无审批不执行"的红线。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from core.agents.base import AgentContext, asset_from_dict
from core.agents.critic import CriticAgent
from core.agents.evolution import EvolutionAgent
from core.agents.specialists.appsec_agent import AppSecSpecialistAgent
from core.agents.specialists.db_agent import DbSpecialistAgent
from core.agents.specialists.host_agent import HostSpecialistAgent
from core.agents.specialists.k8s_agent import K8sSpecialistAgent
from core.agents.supervisor import (
    SupervisorAgent,
    TICKET_ABORTED,
    TICKET_BLOCKED,
    TICKET_EXECUTED,
    TICKET_REJECTED,
)
from core.agents.triage import TriageAgent
from core.orchestrator.state_graph import (
    END,
    CompiledStateGraph,
    GraphBudget,
    GraphRun,
    NodeOutcome,
    SecOpsState,
    StateGraph,
)
from core.platform.ten_modules import AuditEcosystemExporter, EvolutionMode, RbacRole, ToggleableEvolutionEngine
from core.plugins.contracts import TargetAsset, proposal_from_dict
from core.plugins.registry import DomainPluginRegistry
from core.privacy_gateway.multimodal import MultimodalPrivacyGateway

#: 领域 -> 人工审批所需角色（纯角色标识，不含任何人名）。
REQUIRED_ROLES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "database": ("高级 DBA", "安全工程师"),
    "k8s": ("安全工程师",),
    "host": ("安全工程师",),
    "appsec": ("安全工程师",),
}

#: 自动放行的优先级白名单（低风险高置信度才允许无人审批）。
AUTO_APPROVE_PRIORITIES = ("P3",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecOpsOrchestrator:
    """把插件、Agent 与状态机装配成一个可执行的 SecOps 工作流。"""

    def __init__(
        self,
        *,
        plugins: DomainPluginRegistry,
        gateway: MultimodalPrivacyGateway | None = None,
        audit: AuditEcosystemExporter | None = None,
        engine: ToggleableEvolutionEngine | None = None,
        memory: Any | None = None,
        budget: GraphBudget | None = None,
        sandbox: Any | None = None,
        patch_verifier: Callable[..., Any] | None = None,
        repo_root: Any | None = None,
    ) -> None:
        self.plugins = plugins
        self.gateway = gateway or MultimodalPrivacyGateway()
        self.audit = audit or AuditEcosystemExporter()
        self.engine = engine or ToggleableEvolutionEngine(EvolutionMode.SHADOW)
        self.memory = memory
        self.budget = budget or GraphBudget()
        self.sandbox = sandbox
        self.patch_verifier = patch_verifier
        self.repo_root = repo_root
        self.tools = plugins.build_tool_client(audit=self.audit, actor_role=RbacRole.SEC.value)
        self._graphs: dict[str, CompiledStateGraph] = {}
        if sandbox is not None:
            for plugin in plugins.plugins():
                attach = getattr(plugin, "attach_sandbox", None)
                if callable(attach):
                    attach(sandbox)
        if patch_verifier is not None:
            self._attach_patch_verifier(patch_verifier, repo_root)

    def _attach_patch_verifier(self, verifier: Callable[..., Any], repo_root: Any | None) -> None:
        """把隔离工作区校验器注入支持该能力的插件（当前为 appsec）。"""
        for plugin in self.plugins.plugins():
            attach = getattr(plugin, "attach_isolated_verifier", None)
            if callable(attach):
                attach(verifier, repo_root=repo_root)

    # -- 对外入口 ------------------------------------------------------
    def run(
        self,
        *,
        tenant_id: str,
        assets: list[TargetAsset],
        source_trigger: str = "INSPECTION_SCHEDULE",
        workflow_run_id: str | None = None,
        preflight_probes: dict[str, dict[str, Any]] | None = None,
    ) -> GraphRun:
        run_id = workflow_run_id or "wf-secops-" + hashlib.sha256(
            f"{tenant_id}:{source_trigger}:{_now()}".encode("utf-8")
        ).hexdigest()[:12]

        materialized = list(assets)
        self.plugins.register_assets(materialized)
        for asset_id, probe in (preflight_probes or {}).items():
            plugin = self.plugins.get(next((a.domain for a in materialized if a.asset_id == asset_id), ""))
            setter = getattr(plugin, "set_probe", None)
            if callable(setter):
                setter(asset_id, probe)

        graph = self.build_graph(run_id=run_id, tenant_id=tenant_id)
        self._graphs[run_id] = graph
        state = SecOpsState(
            workflow_run_id=run_id,
            tenant_id=tenant_id,
            source_trigger=source_trigger,
            target_assets=[asset.public_view() for asset in materialized],
        )
        return graph.invoke(state)

    def graph_for(self, workflow_run_id: str) -> CompiledStateGraph:
        graph = self._graphs.get(workflow_run_id)
        if graph is None:
            raise KeyError(f"未找到 workflow_run_id={workflow_run_id} 的执行图")
        return graph

    def resume(
        self,
        workflow_run_id: str,
        *,
        decision: str,
        patches: dict[str, Any] | None = None,
    ) -> GraphRun:
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError("HITL 决策只能是 APPROVED 或 REJECTED")
        return self.graph_for(workflow_run_id).resume(workflow_run_id, decision=decision, patches=patches)

    # -- 图装配 --------------------------------------------------------
    def build_graph(self, *, run_id: str, tenant_id: str) -> CompiledStateGraph:
        context = AgentContext(
            workflow_run_id=run_id,
            tenant_id=tenant_id,
            tools=self.tools,
            gateway=self.gateway,
            plugins=self.plugins,
            audit=self.audit,
            max_self_correction_rounds=self.budget.max_self_correction_rounds,
            max_branches=self.budget.max_branches,
        )
        supervisor = SupervisorAgent(context, budget=self.budget)
        triage = TriageAgent(context)
        critic = CriticAgent(context, max_rounds=self.budget.max_self_correction_rounds)
        evolution = EvolutionAgent(context, engine=self.engine, memory=self.memory)
        specialists = self._specialists(context)

        graph = StateGraph(name="aegisforge-secops-v3", budget=self.budget)
        graph.add_node(
            "supervisor_intake",
            supervisor.intake,
            role=supervisor.agent_id,
            description="识别受影响资产域并构造确定性执行计划",
        )
        graph.add_node("triage", triage.run, role=triage.agent_id, description="上下文定级 P0~P3 工单")
        graph.add_node("dispatch", supervisor.dispatch, role=supervisor.agent_id, description="按优先级分发领域专家")
        for domain, agent in specialists.items():
            graph.add_node(
                f"specialist_{domain}",
                agent.run,
                role=agent.agent_id,
                description=f"{domain} 域三层修复方案生成与自愈重提交",
            )
        graph.add_node("critic", critic.run, role=critic.agent_id, description="沙箱验证 + 红队复核 + 自愈打回")
        graph.add_node("hitl", self._make_hitl_node(context), role="HITL_GATE", description="人工审批断点")
        graph.add_node("execute", self._make_execute_node(context), role="HITL_EXECUTOR", description="受控执行与回执")
        graph.add_node("evolution", evolution.run, role=evolution.agent_id, description="Shadow Mode 蒸馏与记忆晋升")
        graph.add_node("finalize", self._make_finalize_node(context), role="GRAPH_FINALIZER", description="收尾统计")

        graph.set_entry_point("supervisor_intake")
        graph.add_edge("supervisor_intake", "triage")
        graph.add_edge("triage", "dispatch")
        graph.add_conditional_edge("dispatch", supervisor.route_specialist)
        for domain in specialists:
            graph.add_edge(f"specialist_{domain}", "critic")
        graph.add_conditional_edge("critic", supervisor.route_after_critic, self_correction=True)
        graph.add_edge("hitl", "execute")
        graph.add_edge("execute", "dispatch")
        graph.add_edge("evolution", "finalize")
        graph.add_edge("finalize", END)
        graph.set_self_correction_overflow_node("hitl")
        return graph.compile()

    def _specialists(self, context: AgentContext) -> dict[str, Any]:
        """按已装配插件构造领域专家（Phase 3 接入 k8s / host，Phase 4 接入 appsec）。"""
        available: dict[str, Any] = {
            "database": DbSpecialistAgent(context),
            "k8s": K8sSpecialistAgent(context),
            "host": HostSpecialistAgent(context),
            "appsec": AppSecSpecialistAgent(context),
        }
        return {domain: agent for domain, agent in available.items() if self.plugins.get(domain) is not None}

    # -- 控制面节点 ----------------------------------------------------
    def _make_hitl_node(self, context: AgentContext) -> Callable[[SecOpsState], NodeOutcome]:
        def node(state: SecOpsState) -> NodeOutcome:
            proposal = state.domain_patches.get(state.active_domain) or {}
            ticket = state.current_ticket() or {}
            if not proposal:
                state.mark_ticket(str(ticket.get("ticket_id", "")), TICKET_ABORTED)
                return NodeOutcome(
                    patches={
                        "hitl_status": "REJECTED",
                        "triage_tickets": state.triage_tickets,
                    },
                    events=[{"event": "HITL_SKIPPED_NO_PROPOSAL", "detail": {"ticket_id": state.active_ticket_id}}],
                )

            proposal_id = str(proposal.get("proposal_id", ""))
            verdict = state.critic_verdicts.get(proposal_id) or {}
            if verdict.get("verdict") == "FILTERED":
                return NodeOutcome(
                    events=[{"event": "HITL_SKIPPED_FALSE_POSITIVE", "detail": {"proposal_id": proposal_id}}]
                )

            priority = str(ticket.get("priority", "P3"))
            exhausted = bool(verdict.get("exhausted"))
            requires_restart = bool(proposal.get("impact", {}).get("requires_restart", False))
            needs_human = (
                exhausted
                or verdict.get("verdict") != "PASSED"
                or requires_restart
                or priority not in AUTO_APPROVE_PRIORITIES
            )

            hitl_id = "HITL-" + hashlib.sha256(f"{state.workflow_run_id}:{proposal_id}".encode("utf-8")).hexdigest()[:8].upper()
            record = {
                "hitl_id": hitl_id,
                "kind": "SELF_CORRECTION_EXHAUSTED" if exhausted else "CHANGE_APPROVAL",
                "ticket_id": str(ticket.get("ticket_id", "")),
                "proposal_id": proposal_id,
                "asset_id": str(proposal.get("asset_id", "")),
                "domain": str(proposal.get("domain", state.active_domain)),
                "priority": priority,
                "status": "PENDING_APPROVAL",
                "required_roles": list(REQUIRED_ROLES_BY_DOMAIN.get(state.active_domain, ("安全工程师",))),
                "approval_digest": str(proposal.get("approval_digest", "")),
                "standard_fix": str(proposal.get("standard_fix", "")),
                "rollback": dict(proposal.get("rollback") or {}),
                "impact": dict(proposal.get("impact") or {}),
                "failures": list(verdict.get("failures", [])),
                "round": int(verdict.get("round", state.self_correction_round or 1)),
                "max_rounds": int(verdict.get("max_rounds", self.budget.max_self_correction_rounds)),
                "reason": str(verdict.get("escalation_reason", "")),
                "created_at": _now(),
            }
            state.hitl_tickets.append(record)

            if needs_human:
                state.hitl_status = "PENDING"
                context.audit.append_worm_event(
                    "HITL_SUSPENDED",
                    RbacRole.DBA.value,
                    {
                        "workflow_run_id": state.workflow_run_id,
                        "hitl_id": hitl_id,
                        "proposal_id": proposal_id,
                        "priority": priority,
                        "kind": record["kind"],
                        "required_roles": record["required_roles"],
                        "approval_digest": record["approval_digest"],
                        "round": record["round"],
                    },
                )
                return NodeOutcome(
                    patches={"hitl_tickets": state.hitl_tickets, "hitl_status": "PENDING"},
                    request_suspend=True,
                    suspend_reason=f"{record['kind']}:{priority}:{hitl_id}",
                    events=[
                        {
                            "event": "HITL_SUSPENDED",
                            "detail": {
                                "hitl_id": hitl_id,
                                "priority": priority,
                                "kind": record["kind"],
                                "required_roles": record["required_roles"],
                            },
                        }
                    ],
                )

            record["status"] = "AUTO_APPROVED"
            state.hitl_status = "APPROVED"
            context.audit.append_worm_event(
                "HITL_AUTO_APPROVED",
                RbacRole.DBA.value,
                {
                    "workflow_run_id": state.workflow_run_id,
                    "hitl_id": hitl_id,
                    "proposal_id": proposal_id,
                    "priority": priority,
                    "approval_digest": record["approval_digest"],
                },
            )
            return NodeOutcome(
                patches={"hitl_tickets": state.hitl_tickets, "hitl_status": "APPROVED"},
                events=[{"event": "HITL_AUTO_APPROVED", "detail": {"hitl_id": hitl_id, "priority": priority}}],
            )

        return node

    def _make_execute_node(self, context: AgentContext) -> Callable[[SecOpsState], NodeOutcome]:
        def node(state: SecOpsState) -> NodeOutcome:
            assets = {item["asset_id"]: asset_from_dict(item) for item in state.target_assets}
            observations: list[dict[str, Any]] = []

            if state.hitl_status != "APPROVED":
                for record in state.hitl_tickets:
                    if record.get("status") == "PENDING_APPROVAL":
                        record["status"] = "REJECTED_BY_HUMAN"
                        state.mark_ticket(record.get("ticket_id", ""), TICKET_REJECTED)
                return NodeOutcome(
                    patches={"hitl_tickets": state.hitl_tickets, "triage_tickets": state.triage_tickets},
                    events=[
                        {
                            "event": "HITL_REJECTED",
                            "detail": {
                                "hitl_ids": [
                                    item["hitl_id"]
                                    for item in state.hitl_tickets
                                    if item.get("status") == "REJECTED_BY_HUMAN"
                                ]
                            },
                        }
                    ],
                )

            for record in state.hitl_tickets:
                if record.get("status") not in ("PENDING_APPROVAL", "AUTO_APPROVED", "PENDING_EXECUTION"):
                    continue
                proposal = state.domain_patches.get(record["domain"]) or {}
                verdict = state.critic_verdicts.get(record["proposal_id"]) or {}
                if verdict.get("verdict") != "PASSED" or not proposal:
                    record["status"] = "EXECUTION_BLOCKED"
                    state.mark_ticket(record.get("ticket_id", ""), TICKET_BLOCKED)
                    observations.append(
                        {
                            "hitl_id": record["hitl_id"],
                            "proposal_id": record["proposal_id"],
                            "executed": False,
                            "blocked": True,
                            "detail": "只有通过 Critic 沙箱与红队复核的方案才允许执行",
                        }
                    )
                    continue

                asset = assets.get(record["asset_id"])
                plugin = context.plugins.get(record["domain"])
                if asset is None or plugin is None:
                    record["status"] = "EXECUTION_BLOCKED"
                    state.mark_ticket(record.get("ticket_id", ""), TICKET_BLOCKED)
                    continue

                receipt = plugin.executor().execute(
                    asset,
                    proposal_from_dict(proposal),
                    approval_token=str(record["approval_digest"]),
                )
                record["status"] = "EXECUTED" if receipt.executed else "EXECUTION_BLOCKED"
                state.mark_ticket(
                    record.get("ticket_id", ""),
                    TICKET_EXECUTED if receipt.executed else TICKET_BLOCKED,
                )
                observations.append(
                    {
                        "hitl_id": record["hitl_id"],
                        "ticket_id": record.get("ticket_id", ""),
                        "proposal_id": record["proposal_id"],
                        "domain": record["domain"],
                        "asset_id": record["asset_id"],
                        **receipt.to_dict(),
                    }
                )
                context.audit.append_worm_event(
                    "HITL_EXECUTION_COMPLETED" if receipt.executed else "HITL_EXECUTION_BLOCKED",
                    RbacRole.DBA.value,
                    {
                        "workflow_run_id": state.workflow_run_id,
                        "hitl_id": record["hitl_id"],
                        "proposal_id": record["proposal_id"],
                        "executed": receipt.executed,
                        "blocked": receipt.blocked,
                        "rollback_armed": receipt.rollback_armed,
                        "timeout_sec": receipt.timeout_sec,
                    },
                )

            return NodeOutcome(
                patches={
                    "hitl_tickets": state.hitl_tickets,
                    "triage_tickets": state.triage_tickets,
                    "execution_observations": list(state.execution_observations) + observations,
                    "hitl_status": "NOT_RAISED",
                },
                events=[
                    {
                        "event": "EXECUTION_SETTLED",
                        "detail": {
                            "observed": len(observations),
                            "executed": sum(1 for item in observations if item.get("executed")),
                        },
                    }
                ],
            )

        return node

    def _make_finalize_node(self, context: AgentContext) -> Callable[[SecOpsState], NodeOutcome]:
        def node(state: SecOpsState) -> NodeOutcome:
            metrics = dict(state.metrics)
            metrics["final"] = {
                "tickets": len(state.triage_tickets),
                "hitl_tickets": len(state.hitl_tickets),
                "executed": sum(1 for item in state.execution_observations if item.get("executed")),
                "self_correction_rounds_used": max(
                    (int(item.get("round", 0)) for item in state.critic_verdicts.values()), default=0
                ),
                "evolution_candidates": len(state.evolution_candidates),
                "worm_entries": len(context.audit.entries),
                "worm_integrity_ok": context.audit.verify_integrity(),
                "hitl_status": state.hitl_status,
            }
            return NodeOutcome(
                patches={"metrics": metrics},
                events=[{"event": "GRAPH_FINALIZED", "detail": metrics["final"]}],
            )

        return node

    # -- 瘦桌面端视图 --------------------------------------------------
    @staticmethod
    def thin_client_view(run: GraphRun, topology: dict[str, Any]) -> dict[str, Any]:
        """只含状态与角色，不含修复原文/证据/凭据，供桌面端渲染。"""
        state = run.state
        return {
            "workflow_run_id": run.workflow_run_id,
            "status": run.status,
            "final_node": run.final_node,
            "steps": run.steps,
            "agents": [
                {"node": item["name"], "role": item["role"], "description": item["description"]}
                for item in topology.get("nodes", [])
            ],
            "critic_rounds": {
                "current": state.get("self_correction_round", 0),
                "max": topology.get("budget", {}).get("max_self_correction_rounds", 0),
            },
            "tickets": [
                {
                    "ticket_id": item["ticket_id"],
                    "domain": item["domain"],
                    "priority": item["priority"],
                    "status": item["status"],
                    "owner_agent": item.get("owner_agent", ""),
                }
                for item in state.get("triage_tickets", [])
            ],
            "hitl": [
                {
                    "hitl_id": item["hitl_id"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "priority": item["priority"],
                    "required_roles": item["required_roles"],
                    "round": item["round"],
                }
                for item in state.get("hitl_tickets", [])
            ],
            "transitions_tail": [
                {
                    "seq": item["seq"],
                    "node": item["node"],
                    "event": item["event"],
                    "round": item["self_correction_round"],
                }
                for item in run.transitions[-12:]
            ],
        }


__all__ = ["AUTO_APPROVE_PRIORITIES", "REQUIRED_ROLES_BY_DOMAIN", "SecOpsOrchestrator"]
