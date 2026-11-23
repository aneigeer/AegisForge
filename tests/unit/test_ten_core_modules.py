"""企业级数据库漏洞检测与自进化修复平台 — 十大核心模块单元测试。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from core.platform.ten_modules import (
    BRANCH_WINDOW_SUSPENDED_LONG_TX,
    POOL_STRATEGY_EPHEMERAL,
    POOL_STRATEGY_WARM_POOL,
    AssetVaultManager,
    AuditEcosystemExporter,
    ContextualRiskScorer,
    EvolutionMode,
    LayeredRemediationGenerator,
    MappingExpiredError,
    MappingNotFoundError,
    ModelGatewayReversibleRedactor,
    MultiDimVulnScanner,
    PlatformPipelineOrchestrator,
    ProductionExecutionMonitor,
    RbacApprovalWorkflow,
    RbacRole,
    SandboxHarnessVerifier,
    SqlSyntaxValidationError,
    ToggleableEvolutionEngine,
    WorkflowDagEngine,
)


def test_m01_and_m02_dag_and_asset_vault_zero_plaintext() -> None:
    vault = AssetVaultManager()
    assets = vault.list_assets()
    assert len(assets) >= 3
    for asset in assets:
        # M02: 绝不明文存储密码
        assert "S3cr3t_P@ss!" not in asset.encrypted_dsn_token
        assert "Weak123456" not in asset.encrypted_dsn_token
        lease = vault.issue_ephemeral_lease(asset.asset_id)
        assert lease["plaintext_persisted"] is False
        assert lease["lease_id"].startswith("lease-")

    dag = WorkflowDagEngine()
    assert len(dag.nodes) == 10


def test_m03_and_m04_multidim_scan_and_contextual_risk_scoring() -> None:
    vault = AssetVaultManager()
    assets = vault.list_assets()
    asset_map = {a.asset_id: a for a in assets}

    scanner = MultiDimVulnScanner()
    raw_findings = scanner.scan_assets(assets)
    assert len(raw_findings) >= 4
    categories = {f.category for f in raw_findings}
    assert {"CVE_MATCH", "CIS_BASELINE", "WEAK_AUTH"}.issubset(categories)

    scorer = ContextualRiskScorer()
    scored = scorer.score_and_prioritize(raw_findings, asset_map)
    # 核心交易库 + 公网 DMZ + 高敏数据的 CVE 必须被定级为 P0
    assert scored[0].contextual_priority == "P0"
    # 内网非敏感报表库的轻微告警必须被自动识别为低价值噪音过滤
    assert any(f.suppressed_as_noise for f in scored)


def test_m05_and_m06_remediation_rollback_and_sandbox_harness() -> None:
    vault = AssetVaultManager()
    assets = vault.list_assets()
    asset_map = {a.asset_id: a for a in assets}
    scored = ContextualRiskScorer().score_and_prioritize(MultiDimVulnScanner().scan_assets(assets), asset_map)

    gen = LayeredRemediationGenerator()
    harness = SandboxHarnessVerifier()

    for finding in scored:
        bundle = gen.generate(finding)
        # M05: 必须包含临时缓解、标准修复 SQL、升级路径、强制 Rollback 回滚脚本及锁表评估
        assert bundle.mitigation_plan
        assert bundle.standard_fix_sql
        assert bundle.upgrade_path
        assert "Rollback" in bundle.rollback_sql
        assert bundle.lock_impact_level in {"NONE", "METADATA_SHARE", "TABLE_EXCLUSIVE"}

        res = harness.verify_in_sandbox(finding, bundle, asset_map[finding.asset_id])
        if finding.suppressed_as_noise:
            assert res.false_positive_eliminated is True
        else:
            assert res.poc_reproduced is True
            assert res.dry_run_passed is True
            assert res.rollback_verified is True


def test_m07_reversible_redaction_and_tiered_routing() -> None:
    gateway = ModelGatewayReversibleRedactor()
    r_high = gateway.route_model("HIGH_REASONING")
    r_fast = gateway.route_model("FAST")
    assert r_high["tier"] == "L2_DEEP_REASONING"
    assert r_fast["tier"] == "L1_FAST_LOCAL"

    raw_prompt = "请修复 10.24.8.19 上表 orders_pay_vip 存储过程 sp_settle_order 中手机号 13812345678 的注入漏洞"
    redacted, mapping = gateway.redact_outbound(raw_prompt)
    assert "orders_pay_vip" not in redacted
    assert "sp_settle_order" not in redacted
    assert "10.24.8.19" not in redacted
    assert "13812345678" not in redacted

    # 模型返回后自动还原真实表名与存储过程名，而 PII 保持掩码
    mock_llm_reply = f"建议执行: ALTER FUNCTION public.{next(k for k, v in mapping.items() if v == 'sp_settle_order')} ON {next(k for k, v in mapping.items() if v == 'orders_pay_vip')}"
    restored = gateway.restore_inbound(mock_llm_reply, mapping)
    assert "sp_settle_order" in restored
    assert "orders_pay_vip" in restored


def test_m08_toggleable_evolution_shadow_and_version_rollback() -> None:
    evo = ToggleableEvolutionEngine(EvolutionMode.EVOLVING)
    v_init = evo.active_version

    # 1. 进化模式下可从人工驳回/修改中蒸馏新规则
    d1 = evo.distill_from_human_feedback("VULN-01", "FALSE_POSITIVE_DISMISS", "内网报表只读副本忽略该告警")
    assert d1["distilled"] is True
    assert evo.active_version != v_init

    # 2. 一键回滚至上一版本
    rb = evo.rollback_version()
    assert rb["rolled_back"] is True
    assert evo.active_version == v_init

    # 3. 切换为 STATIC 静态锁定模式后不再自动蒸馏
    evo.set_mode(EvolutionMode.STATIC)
    d2 = evo.distill_from_human_feedback("VULN-02", "HUMAN_EDIT", "不应被蒸馏")
    assert d2["distilled"] is False


def test_m09_and_m10_rbac_worm_audit_and_sarif_export(tmp_path: Path) -> None:
    rbac = RbacApprovalWorkflow()
    assert rbac.check_permission(RbacRole.SEC, "SCAN") is True
    assert rbac.check_permission(RbacRole.SEC, "EXECUTE_FIX") is False
    assert rbac.check_permission(RbacRole.DBA, "APPROVE_HITL") is True
    assert rbac.check_permission(RbacRole.AUDITOR, "EXPORT_SARIF") is True
    assert rbac.check_permission(RbacRole.AUDITOR, "EXECUTE_FIX") is False

    orch = PlatformPipelineOrchestrator()
    summary = orch.run_full_inspection()
    assert summary["status"] == "ok"
    assert summary["worm_integrity_ok"] is True
    assert len(summary["hitl_tickets"]) >= 2

    sarif_file = tmp_path / "report.sarif.json"
    assets = orch.vault.list_assets()
    asset_map = {a.asset_id: a for a in assets}
    scored = orch.scorer.score_and_prioritize(orch.scanner.scan_assets(assets), asset_map)
    bundles = {f.finding_id: orch.fix_gen.generate(f) for f in scored}
    orch.audit.export_sarif_report(scored, bundles, output_path=sarif_file)

    assert sarif_file.is_file()
    sarif_data = json.loads(sarif_file.read_text(encoding="utf-8"))
    assert sarif_data["version"] == "2.1.0"
    assert len(sarif_data["runs"][0]["results"]) >= 3


# =====================================================================
# 生产增强专项：AST 级脱敏 / 双轨验证 / 双期回滚 / 瘦客户端 DTO
# =====================================================================
def test_ast_redaction_encrypts_mapping_and_destroys_on_closure() -> None:
    gateway = ModelGatewayReversibleRedactor(master_key=Fernet.generate_key())
    sql = (
        "ALTER FUNCTION public.sp_settle_order(text) SET search_path = pg_catalog, public;\n"
        "SELECT phone FROM orders_pay_vip WHERE phone = '13812345678' AND host = '10.24.8.19';"
    )
    redacted, mapping = gateway.redact_sql_ast(sql)

    # AST 节点级替换：标识符/字面量清零，但 SQL 骨架与注释结构完整保留
    assert "sp_settle_order" not in redacted
    assert "orders_pay_vip" not in redacted
    assert "13812345678" not in redacted
    assert "10.24.8.19" not in redacted
    assert "ALTER FUNCTION public." in redacted
    assert "SET search_path = pg_catalog, public;" in redacted

    persist = gateway.persist_encrypted_mapping("wf-run-v2", mapping, ttl_seconds=600)
    assert persist["persisted"] is True
    assert persist["plaintext_persisted"] is False
    ciphertext = gateway.persisted_ciphertext("wf-run-v2")
    assert ciphertext and "sp_settle_order" not in ciphertext
    assert "orders_pay_vip" not in ciphertext

    token = next(k for k, v in mapping.items() if v == "orders_pay_vip")
    restored = gateway.load_and_restore_with_syntax_check("wf-run-v2", f"ALTER TABLE {token} SET (fillfactor = 90);")
    assert "orders_pay_vip" in restored["restored_sql"]
    assert restored["syntax_valid"] is True
    assert restored["ast_nodes"]["identifier_nodes"] >= 2

    # 语法非法的模型返回不得进入还原流程
    with pytest.raises(SqlSyntaxValidationError):
        gateway.load_and_restore_with_syntax_check("wf-run-v2", "this is not sql")
    with pytest.raises(SqlSyntaxValidationError):
        gateway.load_and_restore_with_syntax_check("wf-run-v2", "ALTER TABLE t SET (a = 1;")

    destroyed = gateway.destroy_mapping("wf-run-v2")
    assert destroyed["destroyed"] is True
    assert gateway.mapping_lifecycle_summary()["active_mapping_count"] == 0
    with pytest.raises(MappingNotFoundError):
        gateway.load_and_restore_with_syntax_check("wf-run-v2", "ALTER TABLE t SET (a = 1);")

    events = [e["event_type"] for e in gateway.mapping_lifecycle_events]
    assert events == ["M07_MAPPING_PERSISTED", "M07_MAPPING_LOADED_AND_RESTORED", "M07_MAPPING_DESTROYED"]


def test_mapping_ttl_expiry_auto_destroys_ciphertext() -> None:
    now = [1_000.0]
    gateway = ModelGatewayReversibleRedactor(master_key=Fernet.generate_key(), clock=lambda: now[0])
    _, mapping = gateway.redact_sql_ast("SELECT * FROM orders_pay_vip WHERE id = 1;")
    gateway.persist_encrypted_mapping("wf-ttl", mapping, ttl_seconds=10)

    now[0] = 1_011.0
    with pytest.raises(MappingExpiredError):
        gateway.load_and_restore_with_syntax_check("wf-ttl", "ALTER TABLE __IDENT_001__ SET (a = 1);")
    assert gateway.persisted_ciphertext("wf-ttl") == ""
    assert gateway.mapping_lifecycle_summary()["event_counts"]["M07_MAPPING_DESTROYED"] == 1


def test_heterogeneous_sandbox_pool_strategy_routing() -> None:
    vault = AssetVaultManager()
    for asset_id, engine in (("redis-session-04", "redis"), ("tidb-htap-05", "tidb"), ("oracle-erp-06", "oracle")):
        vault.register_asset(
            asset_id=asset_id,
            db_engine=engine,
            engine_version="7.5.1",
            business_tier="IMPORTANT_P1",
            network_exposure="INTERNAL_VPC",
            pii_density="MEDIUM",
            kms_key_id=f"kms://vault/dbguardian/{asset_id}",
            raw_dsn=f"{engine}://reader:ReadOnly2026@10.24.50.10:4000/app",
        )

    harness = SandboxHarnessVerifier()
    strategies = {a.asset_id: harness.pool_strategy_for(a.db_engine) for a in vault.list_assets()}
    for asset_id in ("pg-prod-trade-01", "mysql-fin-ledger-02", "redis-session-04"):
        assert strategies[asset_id] == POOL_STRATEGY_EPHEMERAL
    for asset_id in ("tidb-htap-05", "oracle-erp-06"):
        assert strategies[asset_id] == POOL_STRATEGY_WARM_POOL

    scored = ContextualRiskScorer().score_and_prioritize(
        MultiDimVulnScanner().scan_assets(vault.list_assets()),
        {a.asset_id: a for a in vault.list_assets()},
    )
    finding = scored[0]
    bundle = LayeredRemediationGenerator().generate(finding)
    tidb_result = harness.verify_in_sandbox(finding, bundle, vault.get_asset("tidb-htap-05"))
    assert tidb_result.pool_strategy == POOL_STRATEGY_WARM_POOL
    assert tidb_result.sandbox_image.endswith("-warm-pool")


def test_track2_long_transaction_blocks_execution_and_suspends_window() -> None:
    orch = PlatformPipelineOrchestrator()
    assets = orch.vault.list_assets()
    asset_map = {a.asset_id: a for a in assets}
    scored = orch.scorer.score_and_prioritize(orch.scanner.scan_assets(assets), asset_map)
    target = next(f for f in scored if f.contextual_priority == "P0")

    summary = orch.run_full_inspection(
        preflight_probes={
            target.finding_id: {
                "table_rows": 4_800_000,
                "data_size_mb": 7680.0,
                "active_long_tx_count": 2,
                "source": "READONLY_PROBE",
            }
        }
    )

    harness_result = summary["sandbox_harness"][target.finding_id]
    assert harness_result["mdl_avalanche_blocked"] is True
    assert harness_result["active_long_tx_count"] == 2
    assert harness_result["dry_run_passed"] is False
    assert summary["production_preflight"][target.finding_id]["table_rows"] == 4_800_000
    assert any(w["finding_id"] == target.finding_id for w in summary["suspended_windows"])
    assert any(w["branch"] == BRANCH_WINDOW_SUSPENDED_LONG_TX for w in summary["suspended_windows"])
    assert len(summary["hitl_tickets"]) == 1
    assert summary["worm_integrity_ok"] is True


def test_lock_timeout_wrapper_and_dual_phase_rollback_monitor() -> None:
    vault = AssetVaultManager()
    asset_map = {a.asset_id: a for a in vault.list_assets()}
    scored = ContextualRiskScorer().score_and_prioritize(
        MultiDimVulnScanner().scan_assets(vault.list_assets()), asset_map
    )
    pg_finding = next(f for f in scored if f.category == "CVE_MATCH")
    mysql_finding = next(f for f in scored if f.category == "WEAK_AUTH")

    gen = LayeredRemediationGenerator()
    pg_bundle = gen.generate(pg_finding, db_engine="postgresql")
    mysql_bundle = gen.generate(mysql_finding, db_engine="mysql")

    assert pg_bundle.standard_fix_sql.startswith("SET LOCAL lock_timeout = '3s';")
    assert mysql_bundle.standard_fix_sql.startswith("SET SESSION lock_wait_timeout = 3;")
    assert pg_bundle.rollback_sql.strip()
    assert pg_bundle.shadow_deny_checklist
    assert pg_bundle.realtime_circuit_breaker_window_sec == 300
    assert pg_bundle.delayed_observation_hours == 72
    assert gen.wrap_sql_with_lock_timeout("oracle", "ALTER TABLE t ADD c NUMBER").startswith(
        "ALTER SESSION SET DDL_LOCK_TIMEOUT = 3;"
    )

    audit = AuditEcosystemExporter()
    monitor = ProductionExecutionMonitor(audit)

    monitor.register_execution("F-REALTIME", pg_finding.asset_id, pg_bundle.rollback_sql)
    tripped = monitor.record_realtime_metrics("F-REALTIME", qps_delta_pct=-38.0, error_count=0, elapsed_sec=120.0)
    assert tripped["state"] == "ROLLED_BACK_REALTIME"
    assert "qps_delta_pct" in tripped["rollback_reason"]

    monitor.register_execution("F-DELAYED", pg_finding.asset_id, pg_bundle.rollback_sql)
    delayed = monitor.record_realtime_metrics("F-DELAYED", qps_delta_pct=-1.0, error_count=0, elapsed_sec=420.0)
    assert delayed["state"] == "DELAYED_OBSERVATION"

    alerted = monitor.ingest_siem_alert("F-DELAYED", account="app_order_rw", error_code="ERROR 1045 Access denied")
    assert alerted["state"] == "DELAYED_OBSERVATION_ALERTED"
    assert alerted["siem_alert_count"] == 1

    restore = monitor.trigger_one_click_restore("F-DELAYED")
    assert restore["triggered"] is True
    assert restore["rollback_sql"] == pg_bundle.rollback_sql
    assert restore["worm_seq"] is not None
    assert audit.verify_integrity() is True
    assert any(e["event_type"] == "M05_ONE_CLICK_RESTORE" for e in audit.entries)

    # 状态演进必须落为追加写事件流（append-only）
    states = [e["state"] for e in monitor.transition_events]
    assert states.count("ROLLED_BACK_REALTIME") == 1
    assert states.count("RESTORED_BY_ONE_CLICK") == 1
    assert states.index("DELAYED_OBSERVATION") < states.index("DELAYED_OBSERVATION_ALERTED")


def test_siem_permission_alert_audit_and_thin_client_dto() -> None:
    audit = AuditEcosystemExporter()
    res = audit.ingest_siem_permission_alert("pg-prod-trade-01", "app_order_rw", "ERROR 1045 (28000): Access denied")
    assert res["ingested"] is True
    assert res["permission_denied"] is True
    assert res["restore_recommended"] is True
    assert res["worm_hash"]
    assert audit.verify_integrity() is True

    vault = AssetVaultManager()
    views = vault.list_public_views()
    payload = [asdict(v) for v in views]
    assert all("encrypted_dsn_token" not in item for item in payload)
    assert all(item["sandbox_pool_strategy"] for item in payload)
    assert all("S3cr3t" not in json.dumps(item) for item in payload)

    dag = WorkflowDagEngine()
    snapshot = dag.snapshot_for_thin_client()
    assert len(snapshot["nodes"]) == 10
    assert all("encrypted_dsn_token" not in json.dumps(node) for node in snapshot["nodes"])


def test_scanner_cpu_breaker_degrades_scan_rate() -> None:
    vault = AssetVaultManager()
    assets = vault.list_assets()
    scanner = MultiDimVulnScanner(max_qps=20)
    base = scanner.scan_assets(assets, cpu_delta_pct=0.4)
    assert scanner.throttle_state == "NORMAL"
    assert scanner.last_scan_meta["effective_qps"] == 20
    assert len(base) >= 4

    throttled = scanner.scan_assets(assets, cpu_delta_pct=2.4)
    assert scanner.breaker_tripped is True
    assert scanner.throttle_state == "DEGRADED"
    assert scanner.last_scan_meta["effective_qps"] == 10
    assert scanner.last_scan_meta["rate_limit_waits"] >= 0
    assert len(throttled) == len(base)
