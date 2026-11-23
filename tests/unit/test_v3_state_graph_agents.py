"""V3.0 Phase 2 单元测试：插件契约、确定性 StateGraph、五类 Agent 对抗自愈闭环。

覆盖验收要求（``phase 2 acceptance_tests``）：

- Supervisor 路由与分支预算；
- Critic 首次打回 -> DB-Agent 第 2 轮自愈修正 -> 放行至 HITL；
- 第 3 轮仍失败 -> 强制熔断转人工 + WORM 审计事件；
- 插件四大接口、多模态网关零明文与结构校验、MCP 工具参数校验；
- 桌面端多 Agent 角色与自愈轮次可视化源码约束。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.base import AgentContext
from core.agents.critic import CriticAgent
from core.agents.supervisor import SupervisorAgent, TICKET_OPEN
from core.orchestrator.secops_graph import SecOpsOrchestrator
from core.orchestrator.state_graph import (
    END,
    MAX_SELF_CORRECTION_ROUNDS,
    GraphBudget,
    GraphBudgetExceeded,
    GraphDefinitionError,
    NodeOutcome,
    SecOpsState,
    StateGraph,
)
from core.orchestrator.tool_client import McpToolClient, McpToolRegistry, ToolSpec, sanitize_for_audit
from core.platform.ten_modules import (
    AuditEcosystemExporter,
    MappingExpiredError,
    MappingNotFoundError,
    SqlSyntaxValidationError,
)
from core.plugins.contracts import (
    DomainMasker,
    DomainPlugin,
    DomainProbe,
    DomainRollbackExecutor,
    DomainSandboxHarness,
    ImpactAssessment,
    PluginContractError,
    ProbeBudget,
    RemediationProposal,
    RollbackPlan,
    TargetAsset,
    assert_plugin_complete,
    proposal_from_dict,
)
from core.plugins.database.plugin import DatabasePlugin
from core.plugins.registry import DomainPluginRegistry
from core.privacy_gateway.multimodal import MultimodalPrivacyGateway

REPO_ROOT = Path(__file__).resolve().parents[2]

SQL_SAMPLE = (
    "ALTER TABLE orders_pay_vip SET (fillfactor = 90);\n"
    "SELECT * FROM orders_pay_vip WHERE phone = '13812345678';"
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _trade_asset(**overrides) -> TargetAsset:
    data = dict(
        asset_id="pg-prod-trade-01",
        domain="database",
        engine="postgresql",
        version="16.2",
        business_tier="CORE_TRADE",
        network_exposure="PUBLIC_DMZ",
        pii_density="HIGH",
        endpoint="10.x.x.19:5432",
    )
    data.update(overrides)
    return TargetAsset(**data)


def _bi_asset() -> TargetAsset:
    return TargetAsset(
        asset_id="pg-bi-report-03",
        domain="database",
        engine="postgresql",
        version="16.4",
        business_tier="INTERNAL_BI",
        network_exposure="INTERNAL_VPC",
        pii_density="LOW",
        endpoint="10.x.x.15:5432",
    )


def _plugins(**plugin_kwargs) -> DomainPluginRegistry:
    gateway = plugin_kwargs.pop("gateway", None) or MultimodalPrivacyGateway()
    return DomainPluginRegistry().register(DatabasePlugin(gateway=gateway, **plugin_kwargs))


def _orchestrator(**kwargs) -> SecOpsOrchestrator:
    plugins = kwargs.pop("plugins", None) or _plugins()
    return SecOpsOrchestrator(plugins=plugins, **kwargs)


def _context(plugins: DomainPluginRegistry, *, run_id: str = "wf-unit") -> AgentContext:
    return AgentContext(
        workflow_run_id=run_id,
        tenant_id="tenant-unit",
        tools=plugins.build_tool_client(),
        gateway=MultimodalPrivacyGateway(),
        plugins=plugins,
        audit=AuditEcosystemExporter(),
    )


def _stubborn_draft(finding, asset, round_no, feedback) -> RemediationProposal:
    """模拟"始终不补回滚脚本"的模型输出：用于验证硬上限熔断。"""
    return RemediationProposal(
        proposal_id=f"prop-stubborn-{round_no}",
        finding_id=finding.finding_id,
        asset_id=asset.asset_id,
        domain="database",
        plugin_id="plugin.database",
        mitigation="临时缓解",
        standard_fix="SELECT 1;",
        upgrade_path="升级路径",
        rollback=RollbackPlan(kind="SQL", payload="", timeout_sec=0, atomic=False, reversible=False),
        impact=ImpactAssessment(),
    )


def _approve_all(orch: SecOpsOrchestrator, run, *, limit: int = 6):
    guard = 0
    while run.status == "SUSPENDED" and guard < limit:
        run = orch.resume(run.workflow_run_id, decision="APPROVED")
        guard += 1
    return run


# ---------------------------------------------------------------------------
# 插件契约
# ---------------------------------------------------------------------------
def test_plugin_exposes_four_mandatory_interfaces() -> None:
    plugin = DatabasePlugin(gateway=MultimodalPrivacyGateway())
    assert isinstance(plugin, DomainPlugin)
    assert isinstance(plugin.probe(), DomainProbe)
    assert isinstance(plugin.masker(), DomainMasker)
    assert isinstance(plugin.harness(), DomainSandboxHarness)
    assert isinstance(plugin.executor(), DomainRollbackExecutor)
    assert_plugin_complete(plugin)
    assert plugin.domain_id() == "database"
    assert "probe:readonly_token_bucket" in plugin.capabilities()


def test_plugin_contract_fails_closed_when_interface_missing() -> None:
    class BrokenPlugin:
        def plugin_id(self) -> str:
            return "plugin.broken"

        def domain_id(self) -> str:
            return "broken"

        def capabilities(self) -> tuple[str, ...]:
            return ("probe:readonly",)

        def probe(self):
            return DatabasePlugin().probe()

        def masker(self):
            return None

        def harness(self):
            return DatabasePlugin().harness()

        def executor(self):
            return DatabasePlugin().executor()

    with pytest.raises(PluginContractError) as excinfo:
        assert_plugin_complete(BrokenPlugin())
    assert "masker" in str(excinfo.value)

    with pytest.raises(PluginContractError):
        DomainPluginRegistry().register(DatabasePlugin()).register(DatabasePlugin())


def test_probe_budget_token_bucket_and_cpu_breaker() -> None:
    budget = ProbeBudget(token_bucket_qps=20, burst=2)
    assert budget.allow() is True
    assert budget.allow() is True
    assert budget.allow() is False
    assert budget.denied == 1

    assert budget.observe_cpu_delta(2.5) == "DEGRADED"
    snapshot = budget.snapshot()
    assert snapshot["breaker_tripped"] is True
    assert snapshot["effective_qps"] == 10
    assert snapshot["read_only"] is True

    assert budget.observe_cpu_delta(0.2) == "NORMAL"
    assert budget.snapshot()["effective_qps"] == 20


def test_database_probe_report_is_read_only_and_carries_no_credentials() -> None:
    plugin = DatabasePlugin()
    plugin.register_assets([_trade_asset()])
    report = plugin.probe().collect(_trade_asset(), budget=ProbeBudget())
    assert report.read_only is True
    assert report.probe_tool == "database_probe_readonly"
    assert report.findings, "核心交易库必须产出至少一条发现项"
    public = report.findings[0].public_view()
    assert "10.24" not in str(public)
    assert "evidence" not in public
    assert public["location"].startswith("db://")


# ---------------------------------------------------------------------------
# 多模态脱敏网关
# ---------------------------------------------------------------------------
def test_multimodal_gateway_roundtrip_zero_plaintext_and_destroy() -> None:
    gateway = MultimodalPrivacyGateway()
    outcome = gateway.mask(SQL_SAMPLE, workflow_run_id="run-sql-1", ttl_seconds=600)

    assert outcome.modality == "sql"
    assert outcome.structure_valid is True
    assert outcome.mapping_persisted is True
    assert "orders_pay_vip" not in outcome.masked_text
    assert "13812345678" not in outcome.masked_text
    assert "__IDENT_" in outcome.masked_text

    ciphertext = gateway.redactor().persisted_ciphertext("run-sql-1")
    assert ciphertext
    assert "orders_pay_vip" not in ciphertext
    assert "13812345678" not in ciphertext

    restored = gateway.restore(outcome.masked_text, workflow_run_id="run-sql-1")
    assert "orders_pay_vip" in restored.restored_text
    assert "13812345678" not in restored.restored_text  # PII 永不还原为原文
    assert "[REDACTED_PII]" in restored.restored_text
    assert restored.destroyed is True

    with pytest.raises(MappingNotFoundError):
        gateway.restore(outcome.masked_text, workflow_run_id="run-sql-1")


def test_multimodal_gateway_validates_structure_before_restore() -> None:
    gateway = MultimodalPrivacyGateway()
    outcome = gateway.mask(SQL_SAMPLE, workflow_run_id="run-sql-2")
    broken = "ALTER TABLE __IDENT_001__ SET ("

    with pytest.raises(SqlSyntaxValidationError):
        gateway.restore(broken, workflow_run_id="run-sql-2")
    # 结构校验失败不得销毁映射：闭环仍可继续
    assert gateway.snapshot()["active_mappings"] == 1
    gateway.close("run-sql-2", reason="TEST_CLEANUP")


def test_multimodal_gateway_ttl_expiry_erases_mapping() -> None:
    now = [1000.0]
    gateway = MultimodalPrivacyGateway(clock=lambda: now[0])
    outcome = gateway.mask(SQL_SAMPLE, workflow_run_id="run-ttl", ttl_seconds=10)
    now[0] += 11

    with pytest.raises(MappingExpiredError):
        gateway.restore(outcome.masked_text, workflow_run_id="run-ttl")
    assert gateway.snapshot()["active_mappings"] == 0


# ---------------------------------------------------------------------------
# StateGraph
# ---------------------------------------------------------------------------
def test_state_graph_deterministic_path_and_topology() -> None:
    def make_graph() -> StateGraph:
        graph = StateGraph()
        graph.add_node("a", lambda s: NodeOutcome(patches={"notes": s.notes + ["a"]}), role="AGENT_A")
        graph.add_node("b", lambda s: NodeOutcome(patches={"notes": s.notes + ["b"]}), role="AGENT_B")
        graph.set_entry_point("a")
        graph.add_edge("a", "b")
        graph.add_edge("b", END)
        return graph

    graph = make_graph()
    compiled = graph.compile()
    first = compiled.invoke(SecOpsState(workflow_run_id="wf-det-1", tenant_id="t"))
    second = make_graph().compile().invoke(SecOpsState(workflow_run_id="wf-det-2", tenant_id="t"))

    assert first.status == "COMPLETED"
    assert first.state["notes"] == ["a", "b"]
    assert second.state["notes"] == first.state["notes"]
    assert [item["event"] for item in first.transitions] == [item["event"] for item in second.transitions]
    topology = compiled.topology()
    assert topology["entry"] == "a"
    assert [node["role"] for node in topology["nodes"]] == ["AGENT_A", "AGENT_B"]


def test_state_graph_self_correction_cap_forces_hitl() -> None:
    def gen(state: SecOpsState) -> NodeOutcome:
        return NodeOutcome()

    def critic(state: SecOpsState) -> NodeOutcome:
        return NodeOutcome(patches={"self_correction_round": int(state.self_correction_round) + 1})

    graph = StateGraph(budget=GraphBudget(max_steps=40, max_node_visits=10))
    graph.add_node("gen", gen, role="AGENT_SPECIALIST")
    graph.add_node("critic", critic, role="AGENT_CRITIC")
    graph.add_node("hitl", lambda s: NodeOutcome(), role="HITL_GATE")
    graph.set_entry_point("gen")
    graph.add_edge("gen", "critic")
    # 故意写成"永远打回"的坏 Router，验证图级别熔断独立生效
    graph.add_conditional_edge("critic", lambda s: "gen", self_correction=True)
    graph.add_edge("hitl", END)
    graph.set_self_correction_overflow_node("hitl")

    run = graph.compile().invoke(SecOpsState(workflow_run_id="wf-cap", tenant_id="t", self_correction_round=1))
    events = [item["event"] for item in run.transitions]
    assert "SELF_CORRECTION_CAP_ENFORCED" in events
    assert run.final_node == END
    assert run.state["self_correction_round"] == MAX_SELF_CORRECTION_ROUNDS + 1
    assert run.steps <= 12


def test_state_graph_budget_aborts_infinite_loop() -> None:
    graph = StateGraph(budget=GraphBudget(max_steps=20, max_node_visits=2))
    graph.add_node("spin", lambda s: NodeOutcome())
    graph.set_entry_point("spin")
    graph.add_edge("spin", "spin")

    with pytest.raises(GraphBudgetExceeded):
        graph.compile().invoke(SecOpsState(workflow_run_id="wf-spin", tenant_id="t"))


def test_state_graph_overflow_node_required_for_self_correction() -> None:
    graph = StateGraph()
    graph.add_node("a", lambda s: NodeOutcome())
    graph.add_node("b", lambda s: NodeOutcome())
    graph.set_entry_point("a")
    graph.add_conditional_edge("a", lambda s: "b", self_correction=True)

    with pytest.raises(GraphDefinitionError):
        graph.compile()


def test_state_graph_suspend_and_resume_snapshot() -> None:
    def gate(state: SecOpsState) -> NodeOutcome:
        return NodeOutcome(patches={"notes": ["gate"]}, request_suspend=True, suspend_reason="HITL")

    def after(state: SecOpsState) -> NodeOutcome:
        return NodeOutcome(patches={"notes": list(state.notes) + ["after"]})

    graph = StateGraph()
    graph.add_node("gate", gate, role="HITL_GATE")
    graph.add_node("after", after, role="AGENT_SUPERVISOR")
    graph.set_entry_point("gate")
    graph.add_edge("gate", "after")
    graph.add_edge("after", END)
    compiled = graph.compile()

    run = compiled.invoke(SecOpsState(workflow_run_id="wf-suspend", tenant_id="t"))
    assert run.status == "SUSPENDED"
    assert run.suspended_at == "gate"
    assert "wf-suspend" in compiled.graph.suspended_runs

    resumed = compiled.resume("wf-suspend", decision="APPROVED")
    assert resumed.status == "COMPLETED"
    assert resumed.state["hitl_status"] == "APPROVED"
    assert resumed.state["notes"] == ["gate", "after"]


# ---------------------------------------------------------------------------
# Supervisor / Triage
# ---------------------------------------------------------------------------
def test_supervisor_intake_enforces_branch_budget() -> None:
    plugins = _plugins()
    context = _context(plugins)
    supervisor = SupervisorAgent(context, budget=GraphBudget(max_branches=2))
    state = SecOpsState(
        workflow_run_id="wf-branch",
        tenant_id="t",
        target_assets=[
            _trade_asset(asset_id="a1", domain="database"),
            _trade_asset(asset_id="a2", domain="k8s"),
            _trade_asset(asset_id="a3", domain="host"),
        ],
    )
    outcome = supervisor.intake(state)
    plan = outcome.patches["supervisor_plan"]
    assert plan["branch_count"] == 2
    assert plan["domains"] == ["database", "host"]  # 域按字典序稳定排序，截断结果可复现
    assert plan["dropped_domains"] == ["k8s"]
    assert plan["max_branches"] == 2


def test_supervisor_dispatches_highest_priority_ticket() -> None:
    plugins = _plugins()
    supervisor = SupervisorAgent(_context(plugins))
    state = SecOpsState(
        workflow_run_id="wf-dispatch",
        tenant_id="t",
        triage_tickets=[
            {"ticket_id": "TKT-LOW", "finding_id": "f1", "asset_id": "a1", "domain": "database", "priority": "P2", "status": TICKET_OPEN},
            {"ticket_id": "TKT-HIGH", "finding_id": "f2", "asset_id": "a1", "domain": "database", "priority": "P0", "status": TICKET_OPEN},
        ],
    )
    outcome = supervisor.dispatch(state)
    assert outcome.patches["active_ticket_id"] == "TKT-LOW"  # 已按优先级排序，取队首
    state.patch(outcome.patches)
    assert supervisor.route_specialist(state) == "specialist_database"


def test_triage_produces_p0_and_suppresses_internal_noise() -> None:
    orch = _orchestrator()
    run = orch.run(tenant_id="t", assets=[_trade_asset(), _bi_asset()], workflow_run_id="wf-triage")

    tickets = run.state["triage_tickets"]
    assert any(item["priority"] == "P0" for item in tickets)
    assert any(item["suppressed"] for item in tickets)
    suppressed = [item for item in tickets if item["suppressed"]]
    assert all(item["status"] == "SUPPRESSED" for item in suppressed)
    assert run.state["metrics"]["triage"]["tickets_open"] >= 1
    # 工单只携带脱敏证据
    for item in tickets:
        if item["status"] == TICKET_OPEN:
            assert "13812345678" not in item["masked_evidence"]


# ---------------------------------------------------------------------------
# 端到端：生成 -> 打回 -> 自愈 -> 放行 -> HITL
# ---------------------------------------------------------------------------
def test_critic_rejects_then_db_agent_self_corrects_then_hitl() -> None:
    orch = _orchestrator()
    run = orch.run(
        tenant_id="t",
        assets=[_trade_asset()],
        workflow_run_id="wf-self-heal",
        preflight_probes={"pg-prod-trade-01": {"active_long_tx_count": 2}},
    )

    assert run.status == "SUSPENDED"
    assert run.state["self_correction_round"] == 2

    verdicts = list(run.state["critic_verdicts"].values())
    assert [item["verdict"] for item in verdicts] == ["REJECTED", "PASSED"]
    assert "PROD_PREFLIGHT_LONG_TX" in verdicts[0]["failures"]
    assert verdicts[0]["round"] == 1 and verdicts[1]["round"] == 2

    proposal = run.state["domain_patches"]["database"]
    assert proposal["round"] == 2
    assert proposal["impact"]["deferred"] is True
    assert proposal["impact"]["change_window"] == "LOW_PEAK"
    assert "CHANGE_WINDOW_DEFERRED" in proposal["feedback_applied"]
    assert proposal["rollback"]["armed"] is True

    hitl = run.state["hitl_tickets"][-1]
    assert hitl["status"] == "PENDING_APPROVAL"
    assert hitl["kind"] == "CHANGE_APPROVAL"
    assert "高级 DBA" in hitl["required_roles"]

    events = [item["event_type"] for item in orch.audit.entries]
    assert "AGENT_CRITIC_SELF_CORRECTION_DISPATCHED" in events
    assert "AGENT_CRITIC_PASSED" in events
    assert orch.audit.verify_integrity() is True


def test_self_correction_exhaustion_escalates_to_hitl_with_worm() -> None:
    orch = _orchestrator(plugins=_plugins(draft_provider=_stubborn_draft))
    run = orch.run(tenant_id="t", assets=[_trade_asset()], workflow_run_id="wf-exhaust")

    verdicts = list(run.state["critic_verdicts"].values())
    assert [item["round"] for item in verdicts] == [1, 2, 3]
    assert verdicts[-1]["exhausted"] is True
    assert run.state["self_correction_round"] == MAX_SELF_CORRECTION_ROUNDS

    hitl = run.state["hitl_tickets"][0]
    assert hitl["kind"] == "SELF_CORRECTION_EXHAUSTED"
    assert hitl["status"] == "PENDING_APPROVAL"
    assert hitl["round"] == 3
    assert "MISSING_ROLLBACK" in hitl["failures"]

    exhausted_events = [item for item in orch.audit.entries if item["event_type"] == "AGENT_CRITIC_SELF_CORRECTION_EXHAUSTED"]
    assert exhausted_events
    assert exhausted_events[0]["payload"]["action"] == "SUSPENDED_TO_HITL"
    assert run.status == "SUSPENDED"
    assert run.state["hitl_status"] == "PENDING"


def test_critic_max_rounds_cannot_exceed_hard_cap() -> None:
    plugins = _plugins()
    with pytest.raises(ValueError):
        CriticAgent(_context(plugins), max_rounds=4)
    with pytest.raises(GraphDefinitionError):
        GraphBudget(max_self_correction_rounds=5)


def test_hitl_approval_executes_with_rollback_armed() -> None:
    orch = _orchestrator()
    run = orch.run(tenant_id="t", assets=[_trade_asset()], workflow_run_id="wf-exec")
    assert run.status == "SUSPENDED"

    finished = _approve_all(orch, run)
    assert finished.status == "COMPLETED"
    observations = [item for item in finished.state["execution_observations"] if item.get("executed")]
    assert observations
    for item in observations:
        assert item["rollback_armed"] is True
        assert item["timeout_sec"] == 3

    statuses = {item["ticket_id"]: item["status"] for item in finished.state["triage_tickets"]}
    assert "EXECUTED" in statuses.values()
    assert finished.state["hitl_status"] == "NOT_RAISED"


def test_executor_requires_matching_approval_digest() -> None:
    plugins = _plugins()
    plugin = plugins.require("database")
    plugin.register_assets([_trade_asset()])
    report = plugin.probe().collect(_trade_asset())
    finding = report.findings[0]
    proposal = plugin.build_proposal(finding, _trade_asset())

    blocked = plugin.executor().execute(_trade_asset(), proposal, approval_token="")
    assert blocked.executed is False and blocked.blocked is True

    allowed = plugin.executor().execute(_trade_asset(), proposal, approval_token=proposal.approval_digest())
    assert allowed.executed is True
    assert allowed.rollback_armed is True

    rebuilt = proposal_from_dict(proposal.to_dict())
    assert rebuilt.approval_digest() == proposal.approval_digest()
    assert rebuilt.rollback.is_armed() is True


def test_hitl_rejection_does_not_execute() -> None:
    orch = _orchestrator()
    run = orch.run(tenant_id="t", assets=[_trade_asset()], workflow_run_id="wf-reject")
    rejected = orch.resume("wf-reject", decision="REJECTED")

    # 人工驳回后：该变更单标记为人工驳回，且从未产生执行回执
    assert any(item["status"] == "REJECTED_BY_HUMAN" for item in rejected.state["hitl_tickets"])
    assert all(not item.get("executed") for item in rejected.state["execution_observations"])
    assert any(item["status"] == "REJECTED_BY_HUMAN" for item in rejected.state["triage_tickets"])


# ---------------------------------------------------------------------------
# MCP 工具客户端
# ---------------------------------------------------------------------------
def test_mcp_tool_client_rejects_unknown_and_forbidden_params() -> None:
    registry = McpToolRegistry()
    registry.register(
        ToolSpec(name="database_probe_readonly", domain="database", description="probe", required_params=("asset_id",)),
        lambda params: {"asset_id": params["asset_id"]},
    )
    registry.register(
        ToolSpec(name="database_write_probe", domain="database", description="not read only", read_only=False),
        lambda params: {},
    )
    client = McpToolClient(registry)

    unknown = client.call("no_such_tool", {}, workflow_run_id="wf")
    assert unknown.ok is False and unknown.error_code == "METHOD_NOT_FOUND"
    assert unknown.rpc["error"]["code"] == -32601

    forbidden = client.call("database_probe_readonly", {"asset_id": "a", "dsn": "mysql://x"}, workflow_run_id="wf")
    assert forbidden.ok is False and forbidden.error_code == "INVALID_PARAMS"

    missing = client.call("database_probe_readonly", {}, workflow_run_id="wf")
    assert missing.ok is False and "asset_id" in missing.error_message

    denied = client.call("database_write_probe", {}, workflow_run_id="wf")
    assert denied.ok is False and denied.error_code == "TOOL_PERMISSION_DENIED"

    ok = client.call("database_probe_readonly", {"asset_id": "a"}, workflow_run_id="wf")
    assert ok.ok is True and ok.data["asset_id"] == "a"


def test_tool_audit_copy_masks_secrets() -> None:
    masked = sanitize_for_audit({"asset_id": "a1", "password": "S3cr3t", "dsn_token": "abc", "nested": {"token": "t"}})
    assert masked["password"] == "***"
    assert masked["dsn_token"] == "***"
    assert masked["nested"]["token"] == "***"
    assert masked["asset_id"] == "a1"


# ---------------------------------------------------------------------------
# 桌面端可视化约束
# ---------------------------------------------------------------------------
def test_desktop_shell_visualizes_agents_and_self_correction_rounds() -> None:
    source = (REPO_ROOT / "apps" / "desktop" / "NativeShell.cs").read_text(encoding="utf-8")

    for token in (
        "多 Agent 协作",
        "Critic 自愈轮次",
        "_criticMaxRounds = 3",
        "AGENT_SUPERVISOR",
        "AGENT_TRIAGE",
        "AGENT_DB_SPECIALIST",
        "AGENT_CRITIC",
        "AGENT_EVOLUTION",
        "总控调度",
        "研判定级",
        "DB 专家",
        "Critic 红队",
        "进化蒸馏",
        "超限挂起转人工 HITL",
    ):
        assert token in source, f"桌面端缺少多 Agent 可视化元素: {token}"

    # 只允许纯角色标识，禁止编造虚拟人名
    for fake_name in ("张三", "李四", "王五", "赵六", "John Doe", "Alice", "Bob"):
        assert fake_name not in source
    assert "高级 DBA" in source
