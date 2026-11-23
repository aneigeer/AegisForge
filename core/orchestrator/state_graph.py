"""V3.0 确定性 StateGraph 状态机引擎。

设计目标（对应 ``multi_agent_architecture.orchestration_pattern``）：

- **确定性**：节点是有界纯函数 ``state -> NodeOutcome``，路由由显式 Router 决定，
  不存在"自由对话"式跳转，因此同输入必然同轨迹、可回放、可审计。
- **有界**：步数预算、单节点访问次数预算、跨栈分支预算三重熔断；
  Critic 自愈回退边另有 ``max_self_correction_rounds <= 3`` 的图级别硬上限。
- **可挂起**：HITL 断点把状态快照与"恢复入口"一起落盘，审批后可精确续跑。
- **可审计**：每次节点跃迁追加一条 append-only 事件（含轮次与角色），供 WORM 链引用。

图级别守卫与 Agent 内部守卫是**两道独立防线**：即便某个 Agent 忘记检查轮次，
图也不会让自愈边无限回环（``SELF_CORRECTION_CAP_ENFORCED`` 事件 + 强制改道 HITL）。
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

#: 终止节点哨兵。
END = "__END__"

#: Self-Correction 硬上限：任何情况下不得超过 3 轮。
MAX_SELF_CORRECTION_ROUNDS = 3


class GraphDefinitionError(ValueError):
    """图定义非法（入口缺失、边指向未注册节点等），编译期失败。"""


class GraphRouteError(RuntimeError):
    """Router 返回了未注册的节点名。"""


class GraphBudgetExceeded(RuntimeError):
    """图执行超出步数/访问次数预算，强制中止（防止死循环与 Token 爆炸）。"""


# ---------------------------------------------------------------------------
# 共享图状态（对应 spec.shared_graph_state_schema）
# ---------------------------------------------------------------------------
@dataclass
class SecOpsState:
    """跨 Agent 共享的唯一状态载体。

    约定：所有字段必须可 JSON 序列化（快照、桌面端瘦视图、WORM 审计都依赖它）；
    脱敏映射表**不在**状态里，只以 ``workflow_run_id`` 关联 KMS 信封加密存储。
    """

    workflow_run_id: str
    tenant_id: str
    source_trigger: str = "INSPECTION_SCHEDULE"   # INSPECTION_SCHEDULE | SIEM_ALERT | CI_PR_GATE
    target_assets: list[dict[str, Any]] = field(default_factory=list)
    triage_tickets: list[dict[str, Any]] = field(default_factory=list)
    domain_patches: dict[str, dict[str, Any]] = field(default_factory=dict)
    critic_verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    self_correction_round: int = 0
    hitl_status: str = "NOT_RAISED"               # NOT_RAISED | PENDING | APPROVED | REJECTED
    execution_observations: list[dict[str, Any]] = field(default_factory=list)
    evolution_candidates: list[dict[str, Any]] = field(default_factory=list)
    # 按 proposal_id 归档的提案快照（控制面审计用；domain_patches 只保留各域最新一版）
    proposal_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- 控制面（由 Supervisor 维护，不对外暴露为业务语义） ----------------
    dispatch_cursor: int = 0
    active_ticket_id: str = ""
    active_finding_id: str = ""
    active_domain: str = ""
    active_proposal_id: str = ""
    pending_feedback: list[str] = field(default_factory=list)
    supervisor_plan: dict[str, Any] = field(default_factory=dict)
    hitl_tickets: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def patch(self, values: dict[str, Any]) -> None:
        """按字段名打补丁；未知字段直接失败，避免静默丢状态。"""
        for key, value in values.items():
            if not hasattr(self, key):
                raise AttributeError(f"SecOpsState 不存在字段 {key!r}")
            setattr(self, key, value)

    def clone(self) -> "SecOpsState":
        return copy.deepcopy(self)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add_note(self, text: str) -> None:
        self.notes.append(text)

    def ticket(self, ticket_id: str) -> dict[str, Any] | None:
        for item in self.triage_tickets:
            if item.get("ticket_id") == ticket_id:
                return item
        return None

    def current_ticket(self) -> dict[str, Any] | None:
        return self.ticket(self.active_ticket_id) if self.active_ticket_id else None

    def mark_ticket(self, ticket_id: str, status: str, **extra: Any) -> None:
        for item in self.triage_tickets:
            if item.get("ticket_id") == ticket_id:
                item["status"] = status
                item.update(extra)
                return


# ---------------------------------------------------------------------------
# 节点执行结果
# ---------------------------------------------------------------------------
@dataclass
class NodeOutcome:
    """单个节点的执行结果：状态补丁 + 可选跳转 + 可选挂起请求。"""

    patches: dict[str, Any] = field(default_factory=dict)
    next_node: str | None = None
    request_suspend: bool = False
    suspend_reason: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GraphBudget:
    """执行预算：三重熔断 + 自愈轮次硬上限。"""

    max_steps: int = 60
    max_node_visits: int = 8
    max_branches: int = 4
    max_self_correction_rounds: int = MAX_SELF_CORRECTION_ROUNDS

    def __post_init__(self) -> None:
        if self.max_self_correction_rounds > MAX_SELF_CORRECTION_ROUNDS:
            raise GraphDefinitionError(
                f"max_self_correction_rounds 不得超过硬上限 {MAX_SELF_CORRECTION_ROUNDS}"
            )
        if self.max_self_correction_rounds < 1:
            raise GraphDefinitionError("max_self_correction_rounds 至少为 1")
        if self.max_steps < 4:
            raise GraphDefinitionError("max_steps 至少为 4")
        if self.max_branches < 1:
            raise GraphDefinitionError("max_branches 至少为 1")


@dataclass(frozen=True)
class NodeSpec:
    name: str
    role: str
    description: str
    handler: Callable[[SecOpsState], NodeOutcome]


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str


@dataclass(frozen=True)
class SuspensionSnapshot:
    workflow_run_id: str
    node: str
    resume_node: str
    reason: str
    step: int
    state: SecOpsState


@dataclass
class GraphRun:
    """一次图执行的返回值。"""

    workflow_run_id: str
    status: str                 # COMPLETED | SUSPENDED | FAILED
    final_node: str
    steps: int
    suspended_at: str
    transitions: list[dict[str, Any]]
    state: dict[str, Any]

    @property
    def suspended(self) -> bool:
        return self.status == "SUSPENDED"


# ---------------------------------------------------------------------------
# 图定义
# ---------------------------------------------------------------------------
class StateGraph:
    """轻量确定性 DAG 状态机（节点注册 + 条件边 + 自愈回退边 + 挂起/恢复）。"""

    def __init__(self, *, name: str = "secops-graph", budget: GraphBudget | None = None) -> None:
        self.name = name
        self.budget = budget or GraphBudget()
        self._nodes: dict[str, NodeSpec] = {}
        self._static_edges: dict[str, str] = {}
        self._conditional_edges: dict[str, tuple[Callable[[SecOpsState], str], bool]] = {}
        self._entry: str = ""
        self._compiled = False
        self._transitions: list[dict[str, Any]] = []
        self._suspended: dict[str, SuspensionSnapshot] = {}
        self._overflow_node: str = ""

    # -- 构建 ----------------------------------------------------------
    def add_node(
        self,
        name: str,
        handler: Callable[[SecOpsState], NodeOutcome],
        *,
        role: str = "",
        description: str = "",
    ) -> "StateGraph":
        if self._compiled:
            raise GraphDefinitionError("图已编译，禁止再注册节点")
        if not name or name == END:
            raise GraphDefinitionError("节点名非法")
        if name in self._nodes:
            raise GraphDefinitionError(f"节点重复注册: {name}")
        self._nodes[name] = NodeSpec(name=name, role=role, description=description, handler=handler)
        return self

    def add_edge(self, source: str, target: str) -> "StateGraph":
        if self._compiled:
            raise GraphDefinitionError("图已编译，禁止再添加边")
        if source in self._static_edges or source in self._conditional_edges:
            raise GraphDefinitionError(f"节点 {source} 已有出边，禁止重复定义")
        self._static_edges[source] = target
        return self

    def add_conditional_edge(
        self,
        source: str,
        router: Callable[[SecOpsState], str],
        *,
        self_correction: bool = False,
    ) -> "StateGraph":
        """条件边；``self_correction=True`` 表示这是 Critic 打回专家的自愈回退边。"""
        if self._compiled:
            raise GraphDefinitionError("图已编译，禁止再添加边")
        if source in self._static_edges or source in self._conditional_edges:
            raise GraphDefinitionError(f"节点 {source} 已有出边，禁止重复定义")
        self._conditional_edges[source] = (router, self_correction)
        return self

    def set_entry_point(self, name: str) -> "StateGraph":
        if self._compiled:
            raise GraphDefinitionError("图已编译，禁止修改入口")
        self._entry = name
        return self

    def set_self_correction_overflow_node(self, name: str) -> "StateGraph":
        """自愈轮次超限后的强制改道目标（通常是人工介入节点）。"""
        self._overflow_node = name
        return self

    # -- 编译 ----------------------------------------------------------
    def compile(self) -> "CompiledStateGraph":
        problems: list[str] = []
        if not self._entry:
            problems.append("未设置入口节点")
        elif self._entry not in self._nodes:
            problems.append(f"入口节点未注册: {self._entry}")

        def _check_target(source: str, target: str) -> None:
            if target != END and target not in self._nodes:
                problems.append(f"边 {source} -> {target} 指向未注册节点")

        for source, target in self._static_edges.items():
            if source not in self._nodes:
                problems.append(f"边源节点未注册: {source}")
            _check_target(source, target)
        for source in self._conditional_edges:
            if source not in self._nodes:
                problems.append(f"条件边源节点未注册: {source}")
        if self._overflow_node and self._overflow_node not in self._nodes:
            problems.append(f"自愈超限改道节点未注册: {self._overflow_node}")
        if any(is_self for _, is_self in self._conditional_edges.values()) and not self._overflow_node:
            problems.append("存在自愈回退边但未设置 overflow 节点（自愈超限无处改道）")
        if problems:
            raise GraphDefinitionError("图定义非法：" + "；".join(problems))

        self._compiled = True
        return CompiledStateGraph(self)

    # -- 查询 ----------------------------------------------------------
    @property
    def transitions(self) -> list[dict[str, Any]]:
        return list(self._transitions)

    @property
    def suspended_runs(self) -> dict[str, SuspensionSnapshot]:
        return dict(self._suspended)

    def topology(self) -> dict[str, Any]:
        """瘦桌面端可见拓扑：节点角色 + 边类型，不含任何业务数据。"""
        return {
            "graph": self.name,
            "entry": self._entry,
            "nodes": [
                {"name": spec.name, "role": spec.role, "description": spec.description}
                for spec in self._nodes.values()
            ],
            "static_edges": [{"from": s, "to": t} for s, t in self._static_edges.items()],
            "conditional_edges": [
                {"from": s, "self_correction": flag} for s, (_, flag) in self._conditional_edges.items()
            ],
            "budget": asdict(self.budget),
        }

    # -- 内部：跃迁 ----------------------------------------------------
    def _resolve_next(self, node: str, state: SecOpsState) -> tuple[str, bool]:
        """返回 (下一节点, 是否命中自愈边)。"""
        if node in self._conditional_edges:
            router, is_self = self._conditional_edges[node]
            target = router(state)
            if target != END and target not in self._nodes:
                raise GraphRouteError(f"节点 {node} 的 Router 返回未注册节点: {target!r}")
            return target, is_self
        return self._static_edges.get(node, END), False

    def _append_transition(
        self,
        *,
        node: str,
        role: str,
        event: str,
        detail: dict[str, Any],
        round_no: int,
        step: int,
        workflow_run_id: str,
    ) -> dict[str, Any]:
        entry = {
            "seq": len(self._transitions) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workflow_run_id": workflow_run_id,
            "step": step,
            "node": node,
            "role": role,
            "event": event,
            "self_correction_round": round_no,
            "detail": detail,
        }
        self._transitions.append(entry)
        return entry

    def _run_from(self, state: SecOpsState, start: str) -> GraphRun:
        if not self._compiled:
            raise GraphDefinitionError("图未编译，禁止执行")
        current = start
        steps = 0
        visits: Counter[str] = Counter()
        transitions_before = len(self._transitions)

        while current != END:
            steps += 1
            if steps > self.budget.max_steps:
                raise GraphBudgetExceeded(
                    f"执行步数超出预算 {self.budget.max_steps}（可能死循环），已中止"
                )
            visits[current] += 1
            if visits[current] > self.budget.max_node_visits:
                raise GraphBudgetExceeded(
                    f"节点 {current} 访问次数超出预算 {self.budget.max_node_visits}，已中止"
                )
            spec = self._nodes[current]
            outcome = spec.handler(state) or NodeOutcome()

            if outcome.patches:
                state.patch(outcome.patches)
            for extra in outcome.events:
                self._append_transition(
                    node=current,
                    role=spec.role,
                    event=str(extra.get("event", "AGENT_NOTE")),
                    detail=dict(extra.get("detail", {})),
                    round_no=state.self_correction_round,
                    step=steps,
                    workflow_run_id=state.workflow_run_id,
                )

            if outcome.request_suspend:
                resume_node, _ = self._resolve_next(current, state)
                snapshot = SuspensionSnapshot(
                    workflow_run_id=state.workflow_run_id,
                    node=current,
                    resume_node=resume_node,
                    reason=outcome.suspend_reason,
                    step=steps,
                    state=state.clone(),
                )
                self._suspended[state.workflow_run_id] = snapshot
                self._append_transition(
                    node=current,
                    role=spec.role,
                    event="GRAPH_SUSPENDED",
                    detail={"reason": outcome.suspend_reason, "resume_node": resume_node},
                    round_no=state.self_correction_round,
                    step=steps,
                    workflow_run_id=state.workflow_run_id,
                )
                return GraphRun(
                    workflow_run_id=state.workflow_run_id,
                    status="SUSPENDED",
                    final_node=current,
                    steps=steps,
                    suspended_at=current,
                    transitions=self._transitions[transitions_before:],
                    state=state.as_dict(),
                )

            if outcome.next_node is not None:
                nxt, is_self = outcome.next_node, False
                if nxt != END and nxt not in self._nodes:
                    raise GraphRouteError(f"节点 {current} 返回未注册的 next_node: {nxt!r}")
            else:
                nxt, is_self = self._resolve_next(current, state)

            # 图级别自愈熔断：第 max_rounds 轮仍被路由回专家时，说明路由/Agent 未按
            # 约定在上限处收敛，此时强制改道人工，绝不带病放行。
            # 判定用 `>`（而非 `>=`）：允许第 max_rounds 轮完整执行并接受验证。
            if is_self and state.self_correction_round > self.budget.max_self_correction_rounds:
                overflow = self._overflow_node
                self._append_transition(
                    node=current,
                    role=spec.role,
                    event="SELF_CORRECTION_CAP_ENFORCED",
                    detail={
                        "round": state.self_correction_round,
                        "max_rounds": self.budget.max_self_correction_rounds,
                        "redirect_to": overflow,
                    },
                    round_no=state.self_correction_round,
                    step=steps,
                    workflow_run_id=state.workflow_run_id,
                )
                nxt = overflow

            self._append_transition(
                node=current,
                role=spec.role,
                event="NODE_COMPLETED",
                detail={"next": nxt},
                round_no=state.self_correction_round,
                step=steps,
                workflow_run_id=state.workflow_run_id,
            )
            current = nxt

        return GraphRun(
            workflow_run_id=state.workflow_run_id,
            status="COMPLETED",
            final_node=current,
            steps=steps,
            suspended_at="",
            transitions=self._transitions[transitions_before:],
            state=state.as_dict(),
        )

    def _resume(
        self,
        workflow_run_id: str,
        *,
        decision: str,
        patches: dict[str, Any] | None = None,
    ) -> GraphRun:
        snapshot = self._suspended.pop(workflow_run_id, None)
        if snapshot is None:
            raise KeyError(f"未找到挂起的 workflow_run_id={workflow_run_id}")
        state = snapshot.state.clone()
        state.hitl_status = decision
        if patches:
            state.patch(patches)
        self._append_transition(
            node=snapshot.node,
            role=self._nodes[snapshot.node].role if snapshot.node in self._nodes else "",
            event="GRAPH_RESUMED",
            detail={"decision": decision, "resume_node": snapshot.resume_node},
            round_no=state.self_correction_round,
            step=snapshot.step,
            workflow_run_id=workflow_run_id,
        )
        return self._run_from(state, snapshot.resume_node)


class CompiledStateGraph:
    """编译后的图句柄：只暴露执行/恢复/查询，禁止再改结构。"""

    def __init__(self, graph: StateGraph) -> None:
        self._graph = graph

    @property
    def graph(self) -> StateGraph:
        return self._graph

    def invoke(self, state: SecOpsState) -> GraphRun:
        return self._graph._run_from(state, self._graph._entry)

    def resume(self, workflow_run_id: str, *, decision: str, patches: dict[str, Any] | None = None) -> GraphRun:
        return self._graph._resume(workflow_run_id, decision=decision, patches=patches)

    def topology(self) -> dict[str, Any]:
        return self._graph.topology()

    @property
    def transitions(self) -> list[dict[str, Any]]:
        return self._graph.transitions


__all__ = [
    "CompiledStateGraph",
    "END",
    "EdgeSpec",
    "GraphBudget",
    "GraphBudgetExceeded",
    "GraphDefinitionError",
    "GraphRouteError",
    "GraphRun",
    "MAX_SELF_CORRECTION_ROUNDS",
    "NodeOutcome",
    "NodeSpec",
    "SecOpsState",
    "StateGraph",
    "SuspensionSnapshot",
]
