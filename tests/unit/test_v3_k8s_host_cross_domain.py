"""V3.0 Phase 3 单元测试：云原生 K8s / 主机插件、统一沙箱调度与跨领域记忆库。

覆盖验收要求（``phase 3 acceptance_tests``）：

- DB + K8s 跨资产联动工单协同（同一 StateGraph 内依次处置并各自走 HITL）；
- YAML 双向脱敏与还原（Secret / Namespace / 内网域名 / IP）；
- 沙箱调度路由器（瞬时容器 / 温备池 / vCluster / MicroVM / 流量回放）；
- 跨领域共享记忆库的租户隔离与拒密。
"""

from __future__ import annotations

import pytest

from core.memory.secops_memory import MemoryPolicyError, SecOpsMemory
from core.orchestrator.secops_graph import SecOpsOrchestrator
from core.platform.ten_modules import MappingNotFoundError
from core.plugins.contracts import (
    DomainPlugin,
    ProbeBudget,
    TargetAsset,
    assert_plugin_complete,
)
from core.plugins.database.plugin import DatabasePlugin
from core.plugins.host.plugin import HostPlugin
from core.plugins.k8s.plugin import K8sPlugin
from core.plugins.registry import DomainPluginRegistry
from core.plugins.sandbox_manager import (
    ALL_MODES,
    MODE_EPHEMERAL,
    MODE_MICROVM,
    MODE_TRAFFIC_REPLAY,
    MODE_VCLUSTER,
    MODE_WARM_POOL,
    SandboxManager,
    SandboxQuotaExceeded,
)
from core.privacy_gateway.multimodal import ModalityStructureError, MultimodalPrivacyGateway
from core.privacy_gateway.yaml_masker import YamlMasker

DEPLOYMENT_MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payment-api
  namespace: prod-payment
spec:
  template:
    spec:
      hostPID: true
      containers:
        - name: app
          image: registry.prod.internal.corp/payment:1.4.2
          securityContext:
            privileged: true
            runAsUser: 0
"""

SECRET_MANIFEST = """apiVersion: v1
kind: Secret
metadata:
  name: db-creds
  namespace: prod-payment
stringData:
  password: P@ssw0rd-Strong
  endpoint: db.prod.internal.corp
  port: "5432"
"""


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _db_asset() -> TargetAsset:
    return TargetAsset(
        asset_id="pg-prod-trade-01",
        domain="database",
        engine="postgresql",
        version="16.2",
        business_tier="CORE_TRADE",
        network_exposure="PUBLIC_DMZ",
        pii_density="HIGH",
        endpoint="10.x.x.19:5432",
    )


def _k8s_asset() -> TargetAsset:
    return TargetAsset(
        asset_id="k8s-prod-01",
        domain="k8s",
        engine="kubernetes",
        version="1.29",
        business_tier="CORE_TRADE",
        network_exposure="PUBLIC_DMZ",
        pii_density="HIGH",
        endpoint="ns://prod-payment",
    )


def _host_asset() -> TargetAsset:
    return TargetAsset(
        asset_id="host-prod-01",
        domain="host",
        engine="linux",
        version="5.4.0-120",
        business_tier="HIGH_FINANCE",
        network_exposure="INTERNAL_VPC",
        pii_density="HIGH",
        endpoint="10.x.x.31",
    )


def _build_plugins(*, gateway=None, sandbox=None):
    gateway = gateway or MultimodalPrivacyGateway()
    database = DatabasePlugin(gateway=gateway, sandbox=sandbox)
    k8s = K8sPlugin(gateway=gateway, sandbox=sandbox)
    host = HostPlugin(gateway=gateway, sandbox=sandbox)
    registry = DomainPluginRegistry().register(database).register(k8s).register(host)
    return registry, database, k8s, host


# ---------------------------------------------------------------------------
# YAML 双向脱敏
# ---------------------------------------------------------------------------
def test_yaml_masker_masks_secret_namespace_domain_and_ip() -> None:
    masker = YamlMasker()
    masked, mapping = masker.mask(SECRET_MANIFEST)

    assert "P@ssw0rd-Strong" not in masked
    assert "prod-payment" not in masked
    assert "db.prod.internal.corp" not in masked
    assert "__SECRET_001__" in masked
    assert "__NS_001__" in masked
    assert mapping["__SECRET_001__"] == "P@ssw0rd-Strong"

    restored = masker.unmask(masked, mapping)
    assert restored == SECRET_MANIFEST


def test_yaml_masker_detects_and_validates_structure() -> None:
    masker = YamlMasker()
    assert masker.detect(SECRET_MANIFEST) >= 0.9
    assert masker.detect("SELECT * FROM orders") < 0.5

    with pytest.raises(ModalityStructureError):
        masker.validate("apiVersion: v1\nkind Secret\n")
    with pytest.raises(ModalityStructureError):
        masker.validate("apiVersion: v1\n\tkind: Secret\n")
    with pytest.raises(ModalityStructureError):
        masker.validate("   ")


def test_gateway_registers_yaml_modality_and_persists_ciphertext() -> None:
    gateway = MultimodalPrivacyGateway()
    assert "yaml" in gateway.modalities()

    outcome = gateway.mask(SECRET_MANIFEST, workflow_run_id="run-yaml-1", ttl_seconds=600)
    assert outcome.modality == "yaml"
    assert outcome.mapping_persisted is True
    assert "P@ssw0rd-Strong" not in outcome.masked_text

    ciphertext = gateway.redactor().persisted_ciphertext("run-yaml-1")
    assert "P@ssw0rd-Strong" not in ciphertext

    restored = gateway.restore(outcome.masked_text, workflow_run_id="run-yaml-1")
    assert "P@ssw0rd-Strong" in restored.restored_text
    assert restored.destroyed is True
    with pytest.raises(MappingNotFoundError):
        gateway.restore(outcome.masked_text, workflow_run_id="run-yaml-1")


def test_yaml_restore_rejects_broken_structure() -> None:
    gateway = MultimodalPrivacyGateway()
    outcome = gateway.mask(SECRET_MANIFEST, workflow_run_id="run-yaml-2")
    with pytest.raises(ModalityStructureError):
        gateway.restore("kind Secret\n  bad", workflow_run_id="run-yaml-2")
    gateway.close("run-yaml-2")


# ---------------------------------------------------------------------------
# K8s 插件
# ---------------------------------------------------------------------------
def test_k8s_plugin_contract_probe_and_executor() -> None:
    registry, _, k8s, _ = _build_plugins()
    assert isinstance(k8s, DomainPlugin)
    assert_plugin_complete(k8s)
    assert "mask:yaml_bidirectional" in k8s.capabilities()

    asset = _k8s_asset()
    k8s.register_assets([asset])
    k8s.set_manifests(asset.asset_id, [{"text": DEPLOYMENT_MANIFEST}], has_network_policy=False)
    report = k8s.probe().collect(asset, budget=ProbeBudget())

    categories = {finding.category for finding in report.findings}
    assert {"POD_PRIVILEGE_ESCAPE", "HOST_NAMESPACE_SHARE", "ROOT_CONTAINER", "NETPOL_MISSING"}.issubset(categories)
    assert all(finding.location.startswith("k8s://") for finding in report.findings)

    finding = next(item for item in report.findings if item.category == "POD_PRIVILEGE_ESCAPE")
    proposal = k8s.build_proposal(finding, asset)
    assert "privileged: false" in proposal.standard_fix
    assert "runAsNonRoot: true" in proposal.standard_fix
    assert proposal.rollback.kind == "MANIFEST"
    assert proposal.rollback.is_armed() is True
    assert proposal.impact.requires_restart is True

    outcome = k8s.harness().evaluate(asset, finding, proposal)
    assert outcome.passed is True, outcome.failures
    assert outcome.sandbox_pool == "VCLUSTER"

    blocked = k8s.executor().execute(asset, proposal, approval_token="")
    assert blocked.blocked is True
    allowed = k8s.executor().execute(asset, proposal, approval_token=proposal.approval_digest())
    assert allowed.executed is True and allowed.kind == "MANIFEST"


def test_k8s_harness_rejects_ineffective_patch() -> None:
    _, _, k8s, _ = _build_plugins()
    asset = _k8s_asset()
    k8s.register_assets([asset])
    k8s.set_manifests(asset.asset_id, [{"text": DEPLOYMENT_MANIFEST}], has_network_policy=True)
    finding = next(
        item for item in k8s.probe().collect(asset).findings if item.category == "POD_PRIVILEGE_ESCAPE"
    )
    proposal = k8s.build_proposal(finding, asset)
    bad = proposal.__class__(
        proposal_id="prop-bad",
        finding_id=proposal.finding_id,
        asset_id=proposal.asset_id,
        domain=proposal.domain,
        plugin_id=proposal.plugin_id,
        mitigation=proposal.mitigation,
        standard_fix=DEPLOYMENT_MANIFEST,   # 原清单：补丁未生效
        upgrade_path=proposal.upgrade_path,
        rollback=proposal.rollback,
        impact=proposal.impact,
    )
    outcome = k8s.harness().evaluate(asset, finding, bad)
    assert outcome.passed is False
    assert "FIX_INEFFECTIVE" in outcome.failures
    assert "FIX_NOT_APPLIED" in outcome.failures


# ---------------------------------------------------------------------------
# Host 插件
# ---------------------------------------------------------------------------
def test_host_plugin_probe_masker_and_playbook_rollback() -> None:
    registry, _, _, host = _build_plugins()
    assert_plugin_complete(host)
    asset = _host_asset()
    host.register_assets([asset])
    host.set_facts(
        asset.asset_id,
        {
            "ssh": {"permit_root_login": "yes", "password_authentication": "yes"},
            "kernel": {"running": "5.4.0-120", "baseline": "5.15.0"},
            "world_writable": ["/var/tmp/app-cache"],
            "users": [{"name": "deploy", "sudo": "ALL=(ALL) NOPASSWD:ALL"}],
        },
    )
    report = host.probe().collect(asset, budget=ProbeBudget())
    categories = {finding.category for finding in report.findings}
    assert {"SSH_ROOT_LOGIN", "SSH_PASSWORD_AUTH", "KERNEL_CVE_PENDING", "WORLD_WRITABLE_PATH", "SUDO_NOPASSWD"} == categories

    finding = next(item for item in report.findings if item.category == "SSH_ROOT_LOGIN")
    proposal = host.build_proposal(finding, asset)
    assert "PermitRootLogin no" in proposal.standard_fix
    assert "PermitRootLogin yes" in proposal.rollback.payload
    assert proposal.rollback.kind == "PLAYBOOK"

    outcome = host.harness().evaluate(asset, finding, proposal)
    assert outcome.passed is True, outcome.failures
    assert outcome.sandbox_pool == "MICROVM"

    log_line = "sshd_config on prod-db-01.internal (10.24.8.19)\nuser: deploy\npath: /var/lib/pgsql/data"
    masked, mapping = host.masker().mask(log_line)
    assert "10.24.8.19" not in masked
    assert "prod-db-01.internal" not in masked
    assert "/var/lib/pgsql/data" not in masked
    assert "__USER_001__" in masked
    assert host.masker().unmask(masked, mapping) == log_line


# ---------------------------------------------------------------------------
# 统一沙箱调度
# ---------------------------------------------------------------------------
def test_sandbox_manager_routes_by_domain_and_engine() -> None:
    assert SandboxManager.route("database", "postgresql") == MODE_EPHEMERAL
    assert SandboxManager.route("database", "mysql") == MODE_EPHEMERAL
    assert SandboxManager.route("database", "tidb") == MODE_WARM_POOL
    assert SandboxManager.route("database", "oracle") == MODE_WARM_POOL
    assert SandboxManager.route("k8s") == MODE_VCLUSTER
    assert SandboxManager.route("host") == MODE_MICROVM
    assert SandboxManager.route("appsec") == MODE_TRAFFIC_REPLAY
    assert set(ALL_MODES) == {MODE_EPHEMERAL, MODE_WARM_POOL, MODE_VCLUSTER, MODE_MICROVM, MODE_TRAFFIC_REPLAY}


def test_sandbox_manager_quota_and_ttl_reclaim() -> None:
    now = [100.0]
    manager = SandboxManager(max_concurrent=1, clock=lambda: now[0])
    lease = manager.acquire(domain="database", asset_id="pg-1", engine="postgresql")
    assert lease["mode"] == MODE_EPHEMERAL
    assert lease["quota_cpu"] > 0 and lease["quota_mem_mb"] > 0

    with pytest.raises(SandboxQuotaExceeded):
        manager.acquire(domain="k8s", asset_id="k8s-1")

    now[0] += lease["ttl_sec"] + 1
    reclaimed = manager.reap()
    assert [item["lease_id"] for item in reclaimed] == [lease["lease_id"]]
    assert manager.snapshot()["active"] == 0
    assert manager.snapshot()["reclaimed_total"] == 1

    released = manager.release(lease["lease_id"])
    assert released["released"] is False  # 已回收


# ---------------------------------------------------------------------------
# 跨领域共享记忆库
# ---------------------------------------------------------------------------
def test_secops_memory_tenant_isolation_and_cross_domain_recall() -> None:
    memory = SecOpsMemory()
    assert memory.remember(tenant_id="t1", domain="database", kind="REMEDIATION_TEMPLATE", text="PG 权限收敛模板", workflow_run_id="w1")
    assert memory.remember(tenant_id="t1", domain="k8s", kind="FALSE_POSITIVE_PATTERN", text="内网只读命名空间特权告警多为误报")
    assert memory.remember(tenant_id="t2", domain="host", kind="CHANGE_WINDOW", text="host-01 低峰窗口 02:00-04:00")

    assert len(memory.recall(tenant_id="t1")) == 2
    assert len(memory.recall(tenant_id="t2")) == 1
    assert [item["domain"] for item in memory.recall(tenant_id="t1", domain="k8s")] == ["k8s"]

    hits = memory.recall(tenant_id="t1", query="命名空间")
    assert hits and hits[0]["domain"] == "k8s"
    assert hits[0]["score"] > 0

    assert memory.topology("t1") == {}
    assert memory.register_asset(tenant_id="t1", asset_id="api-1", domain="appsec", relations=[{"to": "pg-1", "type": "WRITES"}])
    assert memory.topology("t1")["api-1"]["relations"][0]["to"] == "pg-1"
    assert memory.record_change_window(tenant_id="t1", asset_id="pg-1", window="LOW_PEAK", tolerance_pct=15)
    assert memory.change_windows("t1")["pg-1"]["tolerance_pct"] == 15.0


def test_secops_memory_rejects_secrets_and_auditor_writes() -> None:
    memory = SecOpsMemory()
    assert memory.remember(tenant_id="t1", domain="database", kind="REMEDIATION_TEMPLATE", text="password: P@ssw0rd") is False
    assert memory.remember(
        tenant_id="t1",
        domain="database",
        kind="REMEDIATION_TEMPLATE",
        text="授权收敛模板",
        role="AUDITOR",
    ) is False
    assert memory.snapshot()["rejected"] == 2
    assert memory.recall(tenant_id="t1", role="AUDITOR") == []

    with pytest.raises(MemoryPolicyError):
        memory.recall(tenant_id="t1", role="ROOT")


# ---------------------------------------------------------------------------
# 跨域联动：DB + K8s 同一 StateGraph 内协同处置
# ---------------------------------------------------------------------------
def test_db_and_k8s_cross_domain_remediation_flow() -> None:
    sandbox = SandboxManager(max_concurrent=6)
    memory = SecOpsMemory()
    registry, _, k8s, _ = _build_plugins(sandbox=sandbox)
    k8s.set_manifests("k8s-prod-01", [{"text": DEPLOYMENT_MANIFEST}], has_network_policy=True)

    orch = SecOpsOrchestrator(plugins=registry, sandbox=sandbox, memory=memory)
    run = orch.run(
        tenant_id="tenant-cross",
        assets=[_db_asset(), _k8s_asset()],
        workflow_run_id="wf-cross-domain",
    )

    assert run.status == "SUSPENDED"
    domains = {ticket["domain"] for ticket in run.state["triage_tickets"]}
    assert domains == {"database", "k8s"}
    assert run.state["supervisor_plan"]["domains"] == ["database", "k8s"]

    guard = 0
    while run.status == "SUSPENDED" and guard < 8:
        run = orch.resume("wf-cross-domain", decision="APPROVED")
        guard += 1

    assert run.status == "COMPLETED"
    assert all(ticket["status"] == "EXECUTED" for ticket in run.state["triage_tickets"])
    executed_domains = {item["domain"] for item in run.state["execution_observations"] if item.get("executed")}
    assert executed_domains == {"database", "k8s"}
    kinds = {item["kind"] for item in run.state["execution_observations"]}
    assert {"SQL", "MANIFEST"} == kinds
    assert run.state["metrics"]["final"]["worm_integrity_ok"] is True

    # 进化 Agent 把跨域经验写入共享记忆库
    assert memory.recall(tenant_id="tenant-cross", domain="k8s")
    assert memory.recall(tenant_id="tenant-cross", domain="database")
    assert sandbox.snapshot()["active"] == 0, "所有沙箱租约必须在流程闭环后释放"
