"""V3.0 Phase 4 单元测试：AppSec 插件、App+DB 跨栈联动、插件 SDK 与跨栈 SARIF。

覆盖验收要求（``phase 4 acceptance_tests``）：

- SQLi / N+1 / 硬编码密钥 / IDOR / SCA 的源码审计；
- 「源码定位 -> 代码 Patch + 同审批 DB 加固 SQL」联合方案与反向 Patch 回滚；
- App + DB 联合沙箱（影子流量回放）与 schema 变更拦截；
- 第三方插件 SDK manifest 校验与装配期 fail closed；
- SARIF v2.1.0 同时承载 ``db://`` 与 ``file://#L`` 位置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.orchestrator.secops_graph import SecOpsOrchestrator
from core.plugins.appsec.plugin import AppSecPlugin
from core.plugins.contracts import (
    DomainPlugin,
    ProbeBudget,
    ProbeFinding,
    RemediationProposal,
    TargetAsset,
    assert_plugin_complete,
)
from core.plugins.database.plugin import DatabasePlugin
from core.plugins.registry import DomainPluginRegistry
from core.plugins.sandbox_manager import SandboxManager
from core.plugins.sdk import (
    CONTRACTS_VERSION,
    CrossStackSarifReporter,
    PluginManifest,
    PluginManifestError,
    build_third_party_plugin,
    describe_plugin,
    validate_manifest,
)
from core.privacy_gateway.multimodal import MultimodalPrivacyGateway

APP_SOURCE = '''import os
from flask import request

DB_PASSWORD = "P@ssw0rd-123"

def get_order(cur, order_id):
    cur.execute("SELECT * FROM orders WHERE id = " + order_id)
    return cur.fetchone()

def list_orders(db, orders):
    result = []
    for order in orders:
        items = db.query("SELECT * FROM order_items WHERE order_id = %s", order.id)
        result.append(items)
    return result

def show_order(db):
    row = Order.query.get(request.args.get("id"))
    return row
'''


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _app_asset() -> TargetAsset:
    return TargetAsset(
        asset_id="app-api-01",
        domain="appsec",
        engine="python",
        version="3.11",
        business_tier="CORE_TRADE",
        network_exposure="PUBLIC_DMZ",
        pii_density="HIGH",
        endpoint="/api/orders",
        labels=(("linked_db_asset", "pg-prod-trade-01"),),
    )


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


def _appsec_plugin(*, sandbox=None) -> AppSecPlugin:
    plugin = AppSecPlugin(gateway=MultimodalPrivacyGateway(), sandbox=sandbox)
    plugin.register_assets([_app_asset()])
    plugin.set_sources("app-api-01", {"src/api/orders.py": APP_SOURCE})
    plugin.set_dependencies(
        "app-api-01",
        [{"name": "requests", "version": "2.19.0", "cve": "CVE-2023-32681", "fixed_version": "2.31.0"}],
    )
    return plugin


def _finding(plugin: AppSecPlugin, category: str) -> ProbeFinding:
    report = plugin.probe().collect(_app_asset(), budget=ProbeBudget())
    return next(item for item in report.findings if item.category == category)


# ---------------------------------------------------------------------------
# AppSec 源码审计
# ---------------------------------------------------------------------------
def test_appsec_probe_detects_all_rule_families() -> None:
    plugin = _appsec_plugin()
    assert isinstance(plugin, DomainPlugin)
    assert_plugin_complete(plugin)

    report = plugin.probe().collect(_app_asset(), budget=ProbeBudget())
    categories = {item.category for item in report.findings}
    assert {"SQL_INJECTION", "N_PLUS_ONE", "HARDCODED_SECRET", "IDOR", "SCA_VULN"} == categories
    assert report.read_only is True
    assert report.probe_tool == "appsec_probe_readonly"

    sql_finding = next(item for item in report.findings if item.category == "SQL_INJECTION")
    assert sql_finding.location == "file://src/api/orders.py#L7"
    assert sql_finding.base_cvss >= 9.0

    # 参数化查询不得被误报为注入
    assert not any(
        item.category == "SQL_INJECTION" and item.location.endswith("#L13") for item in report.findings
    )
    # IDOR 是启发式，必须显式标注
    idor = next(item for item in report.findings if item.category == "IDOR")
    assert "heuristic" in idor.tags


def test_appsec_fix_is_joint_patch_with_db_hardening_and_reverse() -> None:
    plugin = _appsec_plugin()
    finding = _finding(plugin, "SQL_INJECTION")
    proposal = plugin.build_proposal(finding, _app_asset())

    assert proposal.standard_fix.startswith("--- a/src/api/orders.py")
    assert "%s" in proposal.standard_fix
    added_lines = [line[1:] for line in proposal.standard_fix.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert added_lines and all(" + order_id" not in line for line in added_lines)
    assert "parameter" not in proposal.standard_fix  # 不是注释式"修复"
    assert proposal.rollback.kind == "PATCH"
    assert proposal.rollback.payload.startswith("--- a/src/api/orders.py")

    db_refs = [ref for ref in proposal.code_refs if ref["kind"] == "db_sql"]
    assert db_refs, "必须联合生成 DB 加固 SQL"
    assert "REVOKE ALL PRIVILEGES ON orders" in db_refs[0]["sql"]
    assert "lock_wait_timeout" in db_refs[0]["sql"]
    assert db_refs[0]["asset_id"] == "pg-prod-trade-01"

    outcome = plugin.harness().evaluate(_app_asset(), finding, proposal)
    assert outcome.passed is True, outcome.failures
    assert outcome.sandbox_pool == "TRAFFIC_REPLAY"
    assert outcome.compatibility_score == 1.0

    blocked = plugin.executor().execute(_app_asset(), proposal, approval_token="")
    assert blocked.blocked is True
    allowed = plugin.executor().execute(
        _app_asset(), proposal, approval_token=proposal.approval_digest()
    )
    assert allowed.executed is True and allowed.kind == "PATCH"


def test_appsec_harness_blocks_schema_change_and_replay_regression() -> None:
    plugin = _appsec_plugin()
    finding = _finding(plugin, "SQL_INJECTION")
    proposal = plugin.build_proposal(finding, _app_asset())

    schema_change = RemediationProposal(
        proposal_id="prop-schema",
        finding_id=proposal.finding_id,
        asset_id=proposal.asset_id,
        domain=proposal.domain,
        plugin_id=proposal.plugin_id,
        mitigation=proposal.mitigation,
        standard_fix=(
            "--- a/src/api/orders.py\n+++ b/src/api/orders.py\n@@ -7,1 +7,2 @@\n"
            '-    cur.execute("SELECT 1")\n'
            "+    cur.execute(\"SELECT * FROM orders WHERE id = %s\", (order_id,))\n"
            "+    cur.execute(\"ALTER TABLE orders ADD COLUMN x int\")\n"
        ),
        upgrade_path=proposal.upgrade_path,
        rollback=proposal.rollback,
        impact=proposal.impact,
    )
    blocked = plugin.harness().evaluate(_app_asset(), finding, schema_change)
    assert blocked.passed is False
    assert "SQL_SCHEMA_CHANGE_FORBIDDEN" in blocked.failures

    regressed = plugin.harness().evaluate(
        _app_asset(), finding, proposal, probe={"shadow_replay_pass_rate": 0.62}
    )
    assert regressed.passed is False
    assert "SHADOW_REPLAY_REGRESSION" in regressed.failures
    assert regressed.compatibility_score == 0.62


def test_appsec_masker_roundtrip_for_code_and_cookie() -> None:
    plugin = _appsec_plugin()
    payload = 'def handler(request):\n    url = "/api/v1/orders/detail"\n    headers = {"Authorization": "Bearer abcdef123456"}'
    masked, mapping = plugin.masker().mask(payload)
    assert "/api/v1/orders/detail" not in masked
    assert "Bearer abcdef123456" not in masked
    assert plugin.masker().unmask(masked, mapping) == payload


# ---------------------------------------------------------------------------
# 插件 SDK
# ---------------------------------------------------------------------------
def test_plugin_manifest_validation_fails_closed() -> None:
    good = PluginManifest(
        plugin_id="plugin.acme_db",
        domain_id="database",
        name="Acme DB Pack",
        version="1.2.3",
        vendor="Acme",
        capabilities=("probe:readonly", "mask:sql"),
    )
    validate_manifest(good)

    with pytest.raises(PluginManifestError):
        validate_manifest(
            PluginManifest("acme", "database", "x", "1.0.0", "Acme", ("probe:readonly",))
        )
    with pytest.raises(PluginManifestError):
        validate_manifest(
            PluginManifest("plugin.x", "vmware", "x", "1.0.0", "Acme", ("probe:readonly",))
        )
    with pytest.raises(PluginManifestError):
        validate_manifest(
            PluginManifest("plugin.x", "database", "x", "1.0", "Acme", ("probe:readonly",))
        )
    with pytest.raises(PluginManifestError):
        validate_manifest(
            PluginManifest("plugin.x", "database", "x", "1.0.0", "Acme", ())
        )
    with pytest.raises(PluginManifestError):
        validate_manifest(
            PluginManifest(
                "plugin.x",
                "database",
                "x",
                "1.0.0",
                "Acme",
                ("probe:readonly",),
                contracts_version="2.0.0",
            )
        )


def test_third_party_plugin_assembles_and_registers() -> None:
    base = DatabasePlugin()
    manifest = PluginManifest(
        plugin_id="plugin.acme_db",
        domain_id="database",
        name="Acme DB Pack",
        version="1.0.0",
        vendor="Acme Security",
        capabilities=("probe:readonly", "mask:sql_ast", "sandbox:ephemeral", "rollback:sql"),
        contracts_version=CONTRACTS_VERSION,
        entrypoint="acme_db.plugin:build",
    )
    third = build_third_party_plugin(
        manifest,
        probe=base.probe(),
        masker=base.masker(),
        harness=base.harness(),
        executor=base.executor(),
    )
    assert_plugin_complete(third)
    assert third.plugin_id() == "plugin.acme_db"
    assert DomainPluginRegistry().register(third).require("database") is third

    described = describe_plugin(third)
    assert described["manifest"]["vendor"] == "Acme Security"
    assert "probe:readonly" in described["capabilities"]


# ---------------------------------------------------------------------------
# 跨栈 SARIF
# ---------------------------------------------------------------------------
def test_cross_stack_sarif_contains_db_and_file_locations(tmp_path: Path) -> None:
    plugin = _appsec_plugin()
    app_finding = _finding(plugin, "SQL_INJECTION")
    app_proposal = plugin.build_proposal(app_finding, _app_asset())

    db_finding = ProbeFinding(
        finding_id="DB-F1",
        asset_id="pg-prod-trade-01",
        domain="database",
        category="CVE_MATCH",
        rule_id="PG-CVE-2024-7348",
        title="PostgreSQL 提权漏洞",
        evidence="",
        base_cvss=8.8,
        location="db://pg-prod-trade-01",
    )

    reporter = CrossStackSarifReporter()
    sarif = reporter.build(
        findings=[db_finding, app_finding],
        proposals={app_finding.finding_id: app_proposal},
        run_properties={"workflowRunId": "wf-sarif", "selfCorrectionRound": 2},
    )

    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "AegisForge SecOps"
    assert len(run["results"]) == 2
    assert len(run["tool"]["driver"]["rules"]) == 2

    by_rule = {item["ruleId"]: item for item in run["results"]}
    app_result = by_rule["APP-SQLI-01"]
    location = app_result["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "src/api/orders.py"
    assert location["region"]["startLine"] == 7
    assert app_result["level"] == "error"
    assert app_result["properties"]["rollbackArmed"] is True
    assert app_result["properties"]["selfCorrectionRound"] == 1
    assert app_result["properties"]["crossStack"][0]["kind"] == "code"

    db_result = by_rule["PG-CVE-2024-7348"]
    assert db_result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "db://pg-prod-trade-01"
    assert "region" not in db_result["locations"][0]["physicalLocation"]

    path = tmp_path / "secops.sarif"
    reporter.write(path, findings=[db_finding, app_finding], proposals={})
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "2.1.0"


# ---------------------------------------------------------------------------
# 端到端：AppSec + DB 跨栈联动
# ---------------------------------------------------------------------------
def test_appsec_and_db_cross_stack_flow() -> None:
    sandbox = SandboxManager(max_concurrent=6)
    gateway = MultimodalPrivacyGateway()
    database = DatabasePlugin(gateway=gateway, sandbox=sandbox)
    appsec = AppSecPlugin(gateway=gateway, sandbox=sandbox)
    appsec.set_sources("app-api-01", {"src/api/orders.py": APP_SOURCE})
    appsec.set_dependencies(
        "app-api-01",
        [{"name": "requests", "version": "2.19.0", "cve": "CVE-2023-32681", "fixed_version": "2.31.0"}],
    )
    registry = DomainPluginRegistry().register(database).register(appsec)

    orch = SecOpsOrchestrator(plugins=registry, gateway=gateway, sandbox=sandbox)
    run = orch.run(
        tenant_id="tenant-appsec",
        assets=[_app_asset(), _db_asset()],
        workflow_run_id="wf-appsec-stack",
        source_trigger="CI_PR_GATE",
    )

    assert run.state["source_trigger"] == "CI_PR_GATE"
    domains = {ticket["domain"] for ticket in run.state["triage_tickets"]}
    assert domains == {"appsec", "database"}

    guard = 0
    while run.status == "SUSPENDED" and guard < 12:
        run = orch.resume("wf-appsec-stack", decision="APPROVED")
        guard += 1

    assert run.status == "COMPLETED"
    executed = [item for item in run.state["execution_observations"] if item.get("executed")]
    kinds = {item["kind"] for item in executed}
    assert "PATCH" in kinds and "SQL" in kinds

    appsec_proposals = [
        item for item in run.state["proposal_snapshots"].values() if item.get("domain") == "appsec"
    ]
    assert appsec_proposals
    assert any(
        ref["kind"] == "db_sql"
        for proposal in appsec_proposals
        for ref in proposal.get("code_refs", [])
    ), "AppSec 提案必须联合携带 DB 加固 SQL"
    assert all(proposal["rollback"]["armed"] for proposal in appsec_proposals)
    assert sandbox.snapshot()["active"] == 0
