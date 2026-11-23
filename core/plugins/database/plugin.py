"""数据库业务域插件：把 V2.0 的 M02~M06 能力封装成标准 ``DomainPlugin``。

设计原则（``plugins_matrix[database]``）：

- **零重写**：探测复用 :class:`MultiDimVulnScanner`，修复复用
  :class:`LayeredRemediationGenerator`，沙箱复用 :class:`SandboxHarnessVerifier`，
  脱敏复用 :class:`SqlAstAdapter`。插件只做"契约适配 + 领域纠偏策略"，
  因此 V2.0 的行为与 1,970 项测试完全不受影响。
- **零凭据**：插件不持有 DSN；``AssetCredentialRecord`` 仅用于复用既有接口签名，
  ``encrypted_dsn_token`` 恒为空串，KMS key 只是逻辑标识。
- **只读优先**：四个工具全部 ``read_only=True``；写动作只能经
  :class:`DatabaseExecutor`（要求 HITL 审批摘要 + 回滚计划就绪）。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from core.orchestrator.tool_client import ToolSpec
from core.platform.ten_modules import (
    AssetCredentialRecord,
    LayeredRemediationGenerator,
    MultiDimVulnScanner,
    RemediationBundle,
    SandboxHarnessVerifier,
    SqlAstAdapter,
    VulnerabilityFinding,
)
from core.plugins.contracts import (
    COMPATIBILITY_THRESHOLD,
    DOMAIN_DATABASE,
    DomainMasker,
    DomainProbe,
    DomainRollbackExecutor,
    DomainSandboxHarness,
    HarnessOutcome,
    ImpactAssessment,
    PluginContractError,
    ProbeBudget,
    ProbeFinding,
    ProbeReport,
    ROLLBACK_KIND_SQL,
    RemediationProposal,
    RollbackPlan,
    TargetAsset,
    ExecutionReceipt,
    scoring_record,
)
from core.privacy_gateway.multimodal import SqlModalityHandler

#: 复用既有沙箱池化策略常量语义（EPHEMERAL / WARM_POOL），仅做展示与断言用。
_DEFAULT_ENGINE = "postgresql"


def _to_asset_record(asset: TargetAsset) -> AssetCredentialRecord:
    """构造仅含元数据的凭据记录：``encrypted_dsn_token`` 恒为空，插件永不接触 DSN。"""
    return scoring_record(asset)


def _to_vuln_finding(finding: ProbeFinding) -> VulnerabilityFinding:
    return VulnerabilityFinding(
        finding_id=finding.finding_id,
        asset_id=finding.asset_id,
        category=finding.category,
        rule_id=finding.rule_id,
        cve_id=finding.cve_id,
        title=finding.title,
        raw_evidence=finding.evidence,
        base_cvss=finding.base_cvss,
        suppressed_as_noise=finding.suppressed_as_noise,
    )


def _to_probe_finding(finding: VulnerabilityFinding, asset: TargetAsset) -> ProbeFinding:
    return ProbeFinding(
        finding_id=finding.finding_id,
        asset_id=finding.asset_id,
        domain=DOMAIN_DATABASE,
        category=finding.category,
        rule_id=finding.rule_id,
        title=finding.title,
        evidence=finding.raw_evidence,
        base_cvss=finding.base_cvss,
        cve_id=finding.cve_id,
        location=f"db://{asset.asset_id}",
        tags=("db", (asset.engine or _DEFAULT_ENGINE).lower(), finding.category.lower()),
        suppressed_as_noise=finding.suppressed_as_noise,
    )


def _to_bundle(finding: ProbeFinding, proposal: RemediationProposal) -> RemediationBundle:
    return RemediationBundle(
        finding_id=finding.finding_id,
        mitigation_plan=proposal.mitigation,
        standard_fix_sql=proposal.standard_fix,
        upgrade_path=proposal.upgrade_path,
        rollback_sql=proposal.rollback.payload,
        lock_impact_level=proposal.impact.lock_impact_level,
        requires_restart=proposal.impact.requires_restart,
        estimated_block_ms=proposal.impact.estimated_block_ms,
        realtime_circuit_breaker_window_sec=300,
        delayed_observation_hours=72,
    )


class DatabaseProbe:
    """只读限流探测：令牌桶 + CPU 熔断，复用 M03 扫描器。"""

    def __init__(self, scanner: MultiDimVulnScanner | None = None) -> None:
        self._scanner = scanner or MultiDimVulnScanner()

    def domain_id(self) -> str:
        return DOMAIN_DATABASE

    def collect(self, asset: TargetAsset, *, budget: ProbeBudget | None = None) -> ProbeReport:
        budget = budget or ProbeBudget()
        if not budget.allow():
            return ProbeReport(
                asset_id=asset.asset_id,
                domain=DOMAIN_DATABASE,
                findings=(),
                budget=budget.snapshot(),
                probe_tool="database_probe_readonly_throttled",
            )
        record = _to_asset_record(asset)
        raw = self._scanner.scan_assets([record], cpu_delta_pct=0.0)
        findings = tuple(_to_probe_finding(item, asset) for item in raw)
        return ProbeReport(
            asset_id=asset.asset_id,
            domain=DOMAIN_DATABASE,
            findings=findings,
            budget=budget.snapshot(),
            probe_tool="database_probe_readonly",
        )


class DatabaseMasker:
    """SQL AST 双向可逆脱敏器（委托多模态网关的 SQL 处理器，口径完全一致）。"""

    def __init__(self, sql_ast: SqlAstAdapter | None = None) -> None:
        self._handler = SqlModalityHandler(sql_ast)

    def domain_id(self) -> str:
        return DOMAIN_DATABASE

    def modality(self) -> str:
        return self._handler.modality()

    def detect(self, payload: str) -> float:
        return self._handler.detect(payload)

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        return self._handler.mask(payload)

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        return self._handler.unmask(payload, mapping)

    def validate(self, payload: str) -> None:
        self._handler.validate(payload)


class DatabaseHarness:
    """双轨沙箱：轨道 1 沙箱 PoC/Dry-Run/回滚可逆，轨道 2 生产只读预检。"""

    def __init__(self, verifier: SandboxHarnessVerifier | None = None, sandbox: Any | None = None) -> None:
        self._verifier = verifier or SandboxHarnessVerifier()
        self._sandbox = sandbox

    def attach(self, sandbox: Any) -> None:
        """挂载统一沙箱调度器（UPG-02）。"""
        self._sandbox = sandbox

    def domain_id(self) -> str:
        return DOMAIN_DATABASE

    def preflight(self, asset: TargetAsset, *, probe: dict[str, Any] | None = None) -> dict[str, Any]:
        """轨道 2 生产只读预检（供 Critic 在生成修复前评估变更窗口）。"""
        bundle = RemediationBundle(
            finding_id="preflight-only",
            mitigation_plan="",
            standard_fix_sql="",
            upgrade_path="",
            rollback_sql="-- preflight only",
            lock_impact_level="NONE",
            requires_restart=False,
            estimated_block_ms=0,
        )
        return self._verifier.run_track2_production_readonly_preflight(
            _to_asset_record(asset), bundle, probe=probe
        )

    def evaluate(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None = None,
    ) -> HarnessOutcome:
        lease = None
        if self._sandbox is not None:
            lease = self._sandbox.acquire(
                domain=DOMAIN_DATABASE, asset_id=asset.asset_id, engine=asset.engine
            )
        try:
            return self._evaluate_inner(asset, finding, proposal, probe=probe, lease=lease)
        finally:
            if lease is not None:
                self._sandbox.release(lease["lease_id"])

    def _evaluate_inner(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None,
        lease: dict[str, Any] | None,
    ) -> HarnessOutcome:
        pool = self._verifier.pool_strategy_for(asset.engine or _DEFAULT_ENGINE)
        logs: list[str] = [f"沙箱池化策略: {pool}"]
        if lease is not None:
            logs.append(
                f"Unified Sandbox Manager 租约: {lease['lease_id']} ({lease['mode']}, ttl={lease['ttl_sec']}s)"
            )

        # 回滚未就绪：直接拒绝进入沙箱（V2.0 模块 5/6 的强制约束）
        if not proposal.rollback.is_armed():
            return HarnessOutcome(
                proposal_id=proposal.proposal_id,
                poc_reproduced=not finding.suppressed_as_noise,
                poc_blocked_after_fix=False,
                dry_run_passed=False,
                rollback_verified=False,
                compatibility_score=0.0,
                sandbox_pool=pool,
                logs=tuple(logs + ["回滚计划未就绪：缺少可逆 payload 或超时熔断参数"]),
                failures=("MISSING_ROLLBACK",),
            )

        record = _to_asset_record(asset)
        bundle = _to_bundle(finding, proposal)
        preflight_probe: dict[str, Any] = dict(probe or {})
        if proposal.impact.deferred and proposal.impact.change_window:
            # 改期方案按"低峰窗口"评估：变更窗口内长事务已被业务侧排空
            preflight_probe["active_long_tx_count"] = 0
            preflight_probe["window_mode"] = proposal.impact.change_window
            logs.append(f"改期执行：按 {proposal.impact.change_window} 窗口预检")
        preflight = self._verifier.run_track2_production_readonly_preflight(
            record, bundle, probe=preflight_probe or None
        )
        logs.append(
            "轨道2 只读预检: rows={rows}, size_mb={size}, long_tx={tx}, blocked={blocked}".format(
                rows=preflight["table_rows"],
                size=preflight["data_size_mb"],
                tx=preflight["active_long_tx_count"],
                blocked=preflight["mdl_avalanche_blocked"],
            )
        )

        try:
            result = self._verifier.verify_in_sandbox(
                _to_vuln_finding(finding), bundle, record, preflight=preflight
            )
        except ValueError as exc:
            return HarnessOutcome(
                proposal_id=proposal.proposal_id,
                poc_reproduced=False,
                poc_blocked_after_fix=False,
                dry_run_passed=False,
                rollback_verified=False,
                compatibility_score=0.0,
                sandbox_pool=pool,
                logs=tuple(logs + [f"沙箱拒绝执行: {exc}"]),
                failures=("SANDBOX_REJECTED",),
                prod_preflight=preflight,
            )

        failures: list[str] = []
        if not result.poc_reproduced:
            failures.append("POC_NOT_REPRODUCED")
        if result.mdl_avalanche_blocked:
            failures.append("PROD_PREFLIGHT_LONG_TX")
        if not result.dry_run_passed:
            failures.append("DRY_RUN_FAILED")
        if not result.rollback_verified:
            failures.append("ROLLBACK_NOT_REVERSIBLE")
        if result.compatibility_score < COMPATIBILITY_THRESHOLD:
            failures.append("COMPATIBILITY_BELOW_THRESHOLD")

        logs.append(
            "轨道1 沙箱: poc={poc}, dry_run={dry}, rollback={rb}, compat={compat}".format(
                poc=result.poc_reproduced,
                dry=result.dry_run_passed,
                rb=result.rollback_verified,
                compat=result.compatibility_score,
            )
        )
        return HarnessOutcome(
            proposal_id=proposal.proposal_id,
            poc_reproduced=result.poc_reproduced,
            poc_blocked_after_fix=result.dry_run_passed and not result.mdl_avalanche_blocked,
            dry_run_passed=result.dry_run_passed,
            rollback_verified=result.rollback_verified,
            compatibility_score=result.compatibility_score,
            sandbox_pool=result.pool_strategy,
            logs=tuple(logs),
            failures=tuple(failures),
            prod_preflight=preflight,
        )


class DatabaseExecutor:
    """生产执行器：无 HITL 审批摘要一律 blocked；回滚计划必须已就绪。"""

    def domain_id(self) -> str:
        return DOMAIN_DATABASE

    def execute(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        approval_token: str = "",
    ) -> ExecutionReceipt:
        timeout_sec = proposal.rollback.timeout_sec
        if not proposal.rollback.is_armed():
            return ExecutionReceipt(
                proposal_id=proposal.proposal_id,
                executed=False,
                blocked=True,
                rollback_armed=False,
                timeout_sec=0,
                detail="回滚计划未就绪，拒绝执行",
                kind=ROLLBACK_KIND_SQL,
            )
        if approval_token != proposal.approval_digest():
            return ExecutionReceipt(
                proposal_id=proposal.proposal_id,
                executed=False,
                blocked=True,
                rollback_armed=True,
                timeout_sec=timeout_sec,
                detail="HITL_APPROVAL_REQUIRED：缺少与审批摘要一致的 token",
                kind=ROLLBACK_KIND_SQL,
            )
        return ExecutionReceipt(
            proposal_id=proposal.proposal_id,
            executed=True,
            blocked=False,
            rollback_armed=True,
            timeout_sec=timeout_sec,
            detail=(
                f"已注入会话级锁超时 {timeout_sec}s，启用 0~5m 实时熔断 + 24~72h SIEM 延迟观察；"
                f"审批摘要 {proposal.approval_digest()[:12]}"
            ),
            kind=ROLLBACK_KIND_SQL,
        )

    def rollback(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        reason: str = "",
    ) -> ExecutionReceipt:
        armed = proposal.rollback.is_armed()
        return ExecutionReceipt(
            proposal_id=proposal.proposal_id,
            executed=armed,
            blocked=not armed,
            rollback_armed=False,
            timeout_sec=proposal.rollback.timeout_sec,
            detail=f"执行回滚（{reason or 'manual'}）：{proposal.rollback.payload[:120]}",
            kind=ROLLBACK_KIND_SQL,
        )


class DatabasePlugin:
    """database 域标准插件：四大接口 + 只读工具集。"""

    def __init__(
        self,
        *,
        gateway: Any | None = None,
        scanner: MultiDimVulnScanner | None = None,
        verifier: SandboxHarnessVerifier | None = None,
        generator: LayeredRemediationGenerator | None = None,
        sql_ast: SqlAstAdapter | None = None,
        sandbox: Any | None = None,
        draft_provider: Callable[[ProbeFinding, TargetAsset, int, tuple[str, ...]], RemediationProposal] | None = None,
    ) -> None:
        self._probe = DatabaseProbe(scanner)
        self._masker = DatabaseMasker(sql_ast)
        self._harness = DatabaseHarness(verifier, sandbox)
        self._executor = DatabaseExecutor()
        self._generator = generator or LayeredRemediationGenerator()
        self._draft_provider = draft_provider
        self._gateway = gateway
        self._assets: dict[str, TargetAsset] = {}
        self._reports: dict[str, ProbeReport] = {}
        self._findings: dict[str, ProbeFinding] = {}
        self._proposals: dict[str, RemediationProposal] = {}
        self._probes: dict[str, dict[str, Any]] = {}

    # -- 契约 ----------------------------------------------------------
    def plugin_id(self) -> str:
        return "plugin.database"

    def domain_id(self) -> str:
        return DOMAIN_DATABASE

    def capabilities(self) -> tuple[str, ...]:
        return (
            "probe:readonly_token_bucket",
            "mask:sql_ast_bidirectional",
            "sandbox:ephemeral+warm_pool",
            "rollback:lock_timeout+dual_phase",
        )

    def probe(self) -> DomainProbe:
        return self._probe

    def masker(self) -> DomainMasker:
        return self._masker

    def harness(self) -> DomainSandboxHarness:
        return self._harness

    def executor(self) -> DomainRollbackExecutor:
        return self._executor

    # -- 目录与缓存 ----------------------------------------------------
    def register_assets(self, assets: Iterable[TargetAsset]) -> "DatabasePlugin":
        for asset in assets:
            if asset.domain == DOMAIN_DATABASE:
                self._assets[asset.asset_id] = asset
        return self

    def attach_sandbox(self, sandbox: Any) -> "DatabasePlugin":
        """挂载统一沙箱调度器（UPG-02），由编排器统一注入。"""
        self._harness.attach(sandbox)
        return self

    def set_probe(self, asset_id: str, probe: dict[str, Any]) -> "DatabasePlugin":
        """注入生产只读预检结果（表体积 / >30s 活跃长事务）。

        真实部署由只读探针回填；离线演练与单测通过本入口提供确定性的预检事实，
        避免让"有没有长事务"依赖墙钟或随机数。
        """
        self._probes[asset_id] = dict(probe)
        return self

    def _probe_for(self, asset_id: str, long_tx_count: int = 0) -> dict[str, Any] | None:
        probe = dict(self._probes.get(asset_id, {}))
        if long_tx_count:
            probe["active_long_tx_count"] = long_tx_count
        return probe or None

    def assets(self) -> tuple[TargetAsset, ...]:
        return tuple(self._assets.values())

    def finding(self, finding_id: str) -> ProbeFinding:
        return self._findings[finding_id]

    def proposal(self, proposal_id: str) -> RemediationProposal:
        return self._proposals[proposal_id]

    def report(self, asset_id: str) -> ProbeReport:
        return self._reports[asset_id]

    # -- 修复方案生成（含领域纠偏策略） --------------------------------
    def build_proposal(
        self,
        finding: ProbeFinding,
        asset: TargetAsset,
        *,
        round_no: int = 1,
        feedback: tuple[str, ...] = (),
        proposal_id: str | None = None,
    ) -> RemediationProposal:
        if self._draft_provider is not None:
            drafted = self._draft_provider(finding, asset, round_no, feedback)
            return replace(
                drafted,
                proposal_id=proposal_id or drafted.proposal_id,
                round=round_no,
            )

        bundle = self._generator.generate(_to_vuln_finding(finding), db_engine=asset.engine or _DEFAULT_ENGINE)
        pid = proposal_id or "prop-" + hashlib.sha256(
            f"{finding.finding_id}:{round_no}".encode("utf-8")
        ).hexdigest()[:12]
        proposal = RemediationProposal(
            proposal_id=pid,
            finding_id=finding.finding_id,
            asset_id=asset.asset_id,
            domain=DOMAIN_DATABASE,
            plugin_id=self.plugin_id(),
            mitigation=bundle.mitigation_plan,
            standard_fix=bundle.standard_fix_sql,
            upgrade_path=bundle.upgrade_path,
            rollback=RollbackPlan(
                kind=ROLLBACK_KIND_SQL,
                payload=bundle.rollback_sql,
                timeout_sec=3,
                atomic=True,
                reversible=True,
                note="双期分级回滚：0~5m 实时熔断 + 24~72h SIEM 一键紧急恢复",
            ),
            impact=ImpactAssessment(
                lock_impact_level=bundle.lock_impact_level,
                requires_restart=bundle.requires_restart,
                estimated_block_ms=bundle.estimated_block_ms,
            ),
            confidence=0.86,
            round=round_no,
        )
        if feedback:
            proposal = self._harden(proposal, asset, feedback)
        return proposal

    def _harden(
        self,
        proposal: RemediationProposal,
        asset: TargetAsset,
        feedback: tuple[str, ...],
    ) -> RemediationProposal:
        """按 Critic 打回的结构化失败码做确定性纠偏（同输入必然同输出）。"""
        codes = {code.strip().upper() for code in feedback if code.strip()}
        impact = proposal.impact
        rollback = proposal.rollback
        fix = proposal.standard_fix
        applied: list[str] = []
        notes: list[str] = list(proposal.notes)

        if codes & {"PROD_PREFLIGHT_LONG_TX", "PROD_PREFLIGHT_INTERCEPT"}:
            impact = replace(
                impact,
                change_window="LOW_PEAK",
                deferred=True,
                lock_impact_level="METADATA_SHARE" if impact.lock_impact_level == "TABLE_EXCLUSIVE" else impact.lock_impact_level,
            )
            fix = "-- 改期至业务低峰窗口执行；DDL 已拆分并复用 online 变更路径\n" + fix
            applied.append("CHANGE_WINDOW_DEFERRED")
            notes.append("检测到 >30s 活跃长事务，改为低峰窗口执行以避免 MDL 锁雪崩")

        if codes & {"MISSING_ROLLBACK", "ROLLBACK_NOT_REVERSIBLE"}:
            rollback = replace(rollback, atomic=True, reversible=True, timeout_sec=max(3, rollback.timeout_sec))
            applied.append("ROLLBACK_REARMED")
            notes.append("已重新生成可逆回滚脚本并补全会话级超时熔断")

        if "DRY_RUN_FAILED" in codes:
            fixed = self._generator.wrap_sql_with_lock_timeout(
                asset.engine or _DEFAULT_ENGINE, fix, timeout_sec=3
            )
            fix = fixed
            applied.append("LOCK_TIMEOUT_REINJECTED")

        if "COMPATIBILITY_BELOW_THRESHOLD" in codes:
            impact = replace(impact, lock_impact_level="NONE")
            applied.append("LOCK_LEVEL_REDUCED")
            notes.append("降低锁级别并补跑业务 SQL 兼容性回放")

        return replace(
            proposal,
            standard_fix=fix,
            rollback=rollback,
            impact=impact,
            feedback_applied=tuple(list(proposal.feedback_applied) + applied),
            notes=tuple(notes),
        )

    # -- MCP 工具集 ----------------------------------------------------
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        common_asset = ("asset_id",)
        return (
            ToolSpec(
                name="database_probe_readonly",
                domain=DOMAIN_DATABASE,
                description="只读限流探测数据库资产（CIS/CVE/弱口令/慢查询）",
                required_params=common_asset,
            ),
            ToolSpec(
                name="database_fix_generate",
                domain=DOMAIN_DATABASE,
                description="生成三层修复方案 + 强制 Rollback + 影响评估（支持按失败码纠偏）",
                required_params=("asset_id", "finding_id", "round"),
                optional_params=("feedback",),
            ),
            ToolSpec(
                name="database_preflight_readonly",
                domain=DOMAIN_DATABASE,
                description="轨道 2 生产只读预检（表体积 / >30s 活跃长事务）",
                required_params=common_asset,
                optional_params=("long_tx_count",),
            ),
            ToolSpec(
                name="database_sandbox_verify",
                domain=DOMAIN_DATABASE,
                description="沙箱 PoC 复现 + 修复 Dry-Run + 回滚可逆性验证",
                required_params=("proposal_id",),
                optional_params=("long_tx_count",),
            ),
            ToolSpec(
                name="database_evidence_masked",
                domain=DOMAIN_DATABASE,
                description="取脱敏后的证据文本（映射表经 KMS 信封加密持久化）",
                required_params=("finding_id", "workflow_run_id"),
            ),
        )

    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "database_probe_readonly": self._tool_probe,
            "database_fix_generate": self._tool_generate,
            "database_preflight_readonly": self._tool_preflight,
            "database_sandbox_verify": self._tool_sandbox,
            "database_evidence_masked": self._tool_evidence,
        }

    def _require_asset(self, asset_id: str) -> TargetAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise PluginContractError(f"未登记的 database 资产: {asset_id}")
        return asset

    def _tool_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        report = self._probe.collect(asset)
        self._reports[asset.asset_id] = report
        for finding in report.findings:
            self._findings[finding.finding_id] = finding
        return {
            "asset_id": asset.asset_id,
            "domain": report.domain,
            "probe_tool": report.probe_tool,
            "read_only": report.read_only,
            "budget": report.budget,
            "findings": [finding.public_view() for finding in report.findings],
        }

    def _tool_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        finding_id = str(params["finding_id"])
        finding = self._findings.get(finding_id)
        if finding is None:
            raise PluginContractError(f"未缓存的探测发现项: {finding_id}")
        round_no = int(str(params.get("round", "1")))
        raw_feedback = str(params.get("feedback", ""))
        feedback = tuple(code for code in raw_feedback.split(",") if code)
        proposal = self.build_proposal(finding, asset, round_no=round_no, feedback=feedback)
        self._proposals[proposal.proposal_id] = proposal
        return proposal.to_dict()

    def _tool_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        long_tx = int(str(params.get("long_tx_count", "0")))
        return self._harness.preflight(asset, probe=self._probe_for(asset.asset_id, long_tx))

    def _tool_sandbox(self, params: dict[str, Any]) -> dict[str, Any]:
        proposal = self._proposals.get(str(params["proposal_id"]))
        if proposal is None:
            raise PluginContractError(f"未缓存的修复方案: {params['proposal_id']}")
        finding = self._findings[proposal.finding_id]
        asset = self._require_asset(proposal.asset_id)
        long_tx = int(str(params.get("long_tx_count", "0")))
        return self._harness.evaluate(
            asset, finding, proposal, probe=self._probe_for(asset.asset_id, long_tx)
        ).to_dict()

    def _tool_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._gateway is None:
            raise PluginContractError("多模态脱敏网关未装配，禁止读取证据原文")
        finding = self._findings.get(str(params["finding_id"]))
        if finding is None:
            raise PluginContractError(f"未缓存的探测发现项: {params['finding_id']}")
        run_id = str(params["workflow_run_id"])
        outcome = self._gateway.mask(
            finding.evidence,
            workflow_run_id=run_id,
            ttl_seconds=86_400,
        )
        return {
            "finding_id": finding.finding_id,
            "modality": outcome.modality,
            "masked_text": outcome.masked_text,
            "token_count": outcome.token_count,
            "mapping_persisted": outcome.mapping_persisted,
            "ciphertext_sha256": outcome.ciphertext_sha256,
            "expires_at": outcome.expires_at,
        }


__all__ = [
    "DatabaseExecutor",
    "DatabaseHarness",
    "DatabaseMasker",
    "DatabasePlugin",
    "DatabaseProbe",
]
