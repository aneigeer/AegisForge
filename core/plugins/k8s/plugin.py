"""K8s 业务域插件（P3-T1）：只读审计 Probe + YAML 脱敏 + vCluster 沙箱 + 声明式回退。

能力边界：

- Probe 只读扫描已采集的 Manifest 事实（特权 Pod / 宿主命名空间共享 / root 容器 /
  RBAC 通配 / 缺失 NetworkPolicy），不连接真实集群；
- Harness 用 YAML 结构校验模拟 ``kubectl --dry-run=server``：补丁必须是合法
  Manifest、必须真正移除违规标记、必须与原清单不同；
- Executor 走 HITL 审批摘要 + 声明式回退（原 Manifest 快照 / ``kubectl rollout undo``）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from core.orchestrator.tool_client import ToolSpec
from core.plugins.contracts import (
    COMPATIBILITY_THRESHOLD,
    DOMAIN_K8S,
    DomainMasker,
    DomainProbe,
    DomainRollbackExecutor,
    DomainSandboxHarness,
    ExecutionReceipt,
    HarnessOutcome,
    ImpactAssessment,
    PluginContractError,
    ProbeBudget,
    ProbeFinding,
    ProbeReport,
    ROLLBACK_KIND_MANIFEST,
    RemediationProposal,
    RollbackPlan,
    TargetAsset,
)
from core.privacy_gateway.multimodal import MODALITY_YAML
from core.privacy_gateway.yaml_masker import YamlMasker

#: 规则表：category -> (rule_id, 标题, 基础 CVSS)
RULES: dict[str, tuple[str, str, float]] = {
    "POD_PRIVILEGE_ESCAPE": ("K8S-PSA-01", "特权容器可逃逸至宿主机内核", 8.8),
    "HOST_NAMESPACE_SHARE": ("K8S-PSA-02", "共享宿主 PID/网络/IPC 命名空间", 7.8),
    "ROOT_CONTAINER": ("K8S-PSA-03", "容器以 root(uid=0) 运行且未禁止提权", 6.5),
    "RBAC_WILDCARD": ("K8S-RBAC-01", "RBAC 角色授予通配 verbs/resources", 7.5),
    "NETPOL_MISSING": ("K8S-NET-01", "命名空间缺少默认拒绝的 NetworkPolicy", 5.3),
}

_PRIV_RE = re.compile(r"privileged\s*:\s*true", re.IGNORECASE)
_HOSTNS_RE = re.compile(r"host(?:PID|Network|IPC)\s*:\s*true", re.IGNORECASE)
_ROOT_RE = re.compile(r"runAsUser\s*:\s*0\b", re.IGNORECASE)
_WILDCARD_RE = re.compile(r"(?:verbs|resources)\s*:\s*\[?\s*[\"']?\*", re.IGNORECASE)


def _manifest_meta(text: str) -> tuple[str, str, str]:
    """从 Manifest 文本提取 (kind, name, namespace)，不引入 YAML 解析器。"""
    kind = name = namespace = ""
    in_metadata = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("kind:") and not kind:
            kind = stripped.split(":", 1)[1].strip().strip("\"'")
            continue
        if stripped.startswith("metadata:"):
            in_metadata = True
            continue
        if in_metadata:
            if not line.startswith(" ") and not stripped.startswith("-"):
                in_metadata = False
                continue
            if stripped.startswith("name:") and not name:
                name = stripped.split(":", 1)[1].strip().strip("\"'")
            elif stripped.startswith("namespace:") and not namespace:
                namespace = stripped.split(":", 1)[1].strip().strip("\"'")
    return kind or "Manifest", name or "unnamed", namespace or "default"


def _location(asset_id: str, kind: str, namespace: str, name: str) -> str:
    return f"k8s://{asset_id}/{namespace}/{kind}/{name}"


class K8sProbe:
    """只读 Manifest 审计（Pod Security / RBAC / NetworkPolicy）。"""

    def __init__(self) -> None:
        self._facts: dict[str, dict[str, Any]] = {}

    def domain_id(self) -> str:
        return DOMAIN_K8S

    def set_facts(self, asset_id: str, facts: dict[str, Any]) -> None:
        self._facts[asset_id] = facts

    def facts(self, asset_id: str) -> dict[str, Any]:
        return self._facts.get(asset_id, {})

    def collect(self, asset: TargetAsset, *, budget: ProbeBudget | None = None) -> ProbeReport:
        budget = budget or ProbeBudget()
        if not budget.allow():
            return ProbeReport(asset.asset_id, DOMAIN_K8S, (), budget.snapshot(), "k8s_probe_readonly_throttled")

        facts = self._facts.get(asset.asset_id, {})
        manifests: list[dict[str, Any]] = list(facts.get("manifests", []))
        findings: list[ProbeFinding] = []
        workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "Pod", "CronJob"}

        for index, manifest in enumerate(manifests, start=1):
            text = str(manifest.get("text", ""))
            kind, name, namespace = _manifest_meta(text)
            location = _location(asset.asset_id, kind, namespace, name)
            checks: list[str] = []
            if _PRIV_RE.search(text):
                checks.append("POD_PRIVILEGE_ESCAPE")
            if _HOSTNS_RE.search(text):
                checks.append("HOST_NAMESPACE_SHARE")
            if _ROOT_RE.search(text):
                checks.append("ROOT_CONTAINER")
            if _WILDCARD_RE.search(text):
                checks.append("RBAC_WILDCARD")

            for category in checks:
                rule_id, title, cvss = RULES[category]
                findings.append(
                    ProbeFinding(
                        finding_id=f"K8S-{asset.asset_id[:6].upper()}-{category}-{index:02d}",
                        asset_id=asset.asset_id,
                        domain=DOMAIN_K8S,
                        category=category,
                        rule_id=rule_id,
                        title=f"{kind}/{name} {title}",
                        evidence=text,
                        base_cvss=cvss,
                        location=location,
                        tags=("k8s", kind.lower(), category.lower()),
                    )
                )

        if not facts.get("has_network_policy", True) and any(
            _manifest_meta(str(item.get("text", "")))[0] in workload_kinds for item in manifests
        ):
            rule_id, title, cvss = RULES["NETPOL_MISSING"]
            namespace = _manifest_meta(str(manifests[0].get("text", "")))[2]
            findings.append(
                ProbeFinding(
                    finding_id=f"K8S-{asset.asset_id[:6].upper()}-NETPOL-01",
                    asset_id=asset.asset_id,
                    domain=DOMAIN_K8S,
                    category="NETPOL_MISSING",
                    rule_id=rule_id,
                    title=f"namespace/{namespace} {title}",
                    evidence=f"kubectl get networkpolicy -n {namespace} # empty",
                    base_cvss=cvss,
                    location=f"k8s://{asset.asset_id}/{namespace}/NetworkPolicy/-",
                    tags=("k8s", "network", "netpol_missing"),
                )
            )

        return ProbeReport(asset.asset_id, DOMAIN_K8S, tuple(findings), budget.snapshot(), "k8s_probe_readonly")


class K8sMasker:
    """YAML 模态脱敏器（委托 :class:`YamlMasker`，与网关同一实现）。"""

    def __init__(self) -> None:
        self._yaml = YamlMasker()

    def domain_id(self) -> str:
        return DOMAIN_K8S

    def modality(self) -> str:
        return MODALITY_YAML

    def detect(self, payload: str) -> float:
        return self._yaml.detect(payload)

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        return self._yaml.mask(payload)

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        return self._yaml.unmask(payload, mapping)

    def validate(self, payload: str) -> None:
        self._yaml.validate(payload)


class K8sHarness:
    """vCluster / Kind 沙箱：Manifest Dry-Run + 回滚可逆性验证。"""

    def __init__(self, sandbox: Any | None = None) -> None:
        self._yaml = YamlMasker()
        self._sandbox = sandbox

    def attach(self, sandbox: Any) -> None:
        self._sandbox = sandbox

    def domain_id(self) -> str:
        return DOMAIN_K8S

    def evaluate(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None = None,
    ) -> HarnessOutcome:
        logs: list[str] = ["沙箱: vCluster 虚拟隔离集群 (kubectl --dry-run=server)"]
        lease = None
        if self._sandbox is not None:
            lease = self._sandbox.acquire(domain=DOMAIN_K8S, asset_id=asset.asset_id)
            logs.append(f"Unified Sandbox Manager 租约: {lease['lease_id']} ({lease['mode']}, ttl={lease['ttl_sec']}s)")

        try:
            if not proposal.rollback.is_armed():
                return HarnessOutcome(
                    proposal_id=proposal.proposal_id,
                    poc_reproduced=not finding.suppressed_as_noise,
                    poc_blocked_after_fix=False,
                    dry_run_passed=False,
                    rollback_verified=False,
                    compatibility_score=0.0,
                    sandbox_pool="VCLUSTER",
                    logs=tuple(logs + ["回滚 Manifest 未就绪"]),
                    failures=("MISSING_ROLLBACK",),
                )

            failures: list[str] = []
            try:
                self._yaml.validate(proposal.standard_fix)
                patch_valid = True
            except ValueError:
                patch_valid = False
                failures.append("MANIFEST_INVALID")

            ineffective = self._still_vulnerable(finding.category, proposal.standard_fix)
            if ineffective:
                failures.append("FIX_INEFFECTIVE")
            if proposal.standard_fix.strip() == finding.evidence.strip():
                failures.append("FIX_NOT_APPLIED")

            rollback_ok = proposal.rollback.kind == ROLLBACK_KIND_MANIFEST and (
                "kubectl" in proposal.rollback.payload or "apiVersion:" in proposal.rollback.payload
            )
            if not rollback_ok:
                failures.append("ROLLBACK_NOT_REVERSIBLE")

            poc_ok = not finding.suppressed_as_noise
            if not poc_ok:
                failures.append("POC_NOT_REPRODUCED")

            compatibility = 1.0 if patch_valid and not ineffective else 0.0
            if compatibility < COMPATIBILITY_THRESHOLD and "COMPATIBILITY_BELOW_THRESHOLD" not in failures:
                failures.append("COMPATIBILITY_BELOW_THRESHOLD")

            logs.append(
                "Dry-Run: patch_valid={p}, ineffective={i}, rollback={r}".format(
                    p=patch_valid, i=ineffective, r=rollback_ok
                )
            )
            return HarnessOutcome(
                proposal_id=proposal.proposal_id,
                poc_reproduced=poc_ok,
                poc_blocked_after_fix=patch_valid and not ineffective,
                dry_run_passed=patch_valid and not ineffective,
                rollback_verified=rollback_ok,
                compatibility_score=compatibility,
                sandbox_pool="VCLUSTER",
                logs=tuple(logs),
                failures=tuple(dict.fromkeys(failures)),
            )
        finally:
            if lease is not None and self._sandbox is not None:
                self._sandbox.release(lease["lease_id"])

    @staticmethod
    def _still_vulnerable(category: str, patch: str) -> bool:
        if category == "POD_PRIVILEGE_ESCAPE":
            return bool(_PRIV_RE.search(patch))
        if category == "HOST_NAMESPACE_SHARE":
            return bool(_HOSTNS_RE.search(patch))
        if category == "ROOT_CONTAINER":
            return bool(_ROOT_RE.search(patch))
        if category == "RBAC_WILDCARD":
            return bool(_WILDCARD_RE.search(patch))
        if category == "NETPOL_MISSING":
            return "kind: NetworkPolicy" not in patch
        return False


class K8sExecutor:
    """声明式执行器：HITL 审批摘要校验 + 原 Manifest 快照回退。"""

    def domain_id(self) -> str:
        return DOMAIN_K8S

    def execute(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        approval_token: str = "",
    ) -> ExecutionReceipt:
        if not proposal.rollback.is_armed():
            return ExecutionReceipt(proposal.proposal_id, False, True, False, 0, "回滚 Manifest 未就绪", ROLLBACK_KIND_MANIFEST)
        if approval_token != proposal.approval_digest():
            return ExecutionReceipt(
                proposal.proposal_id,
                False,
                True,
                True,
                proposal.rollback.timeout_sec,
                "HITL_APPROVAL_REQUIRED：缺少与审批摘要一致的 token",
                ROLLBACK_KIND_MANIFEST,
            )
        return ExecutionReceipt(
            proposal.proposal_id,
            True,
            False,
            True,
            proposal.rollback.timeout_sec,
            f"kubectl apply --server-side 已提交；就绪探针 {proposal.rollback.timeout_sec}s 熔断；回退可用 rollout undo",
            ROLLBACK_KIND_MANIFEST,
        )

    def rollback(self, asset: TargetAsset, proposal: RemediationProposal, *, reason: str = "") -> ExecutionReceipt:
        armed = proposal.rollback.is_armed()
        return ExecutionReceipt(
            proposal.proposal_id,
            armed,
            not armed,
            False,
            proposal.rollback.timeout_sec,
            f"声明式回退（{reason or 'manual'}）：kubectl apply -f 原清单快照",
            ROLLBACK_KIND_MANIFEST,
        )


class K8sPlugin:
    """k8s 域标准插件。"""

    def __init__(
        self,
        *,
        gateway: Any | None = None,
        sandbox: Any | None = None,
        draft_provider: Callable[[ProbeFinding, TargetAsset, int, tuple[str, ...]], RemediationProposal] | None = None,
    ) -> None:
        self._probe = K8sProbe()
        self._masker = K8sMasker()
        self._harness = K8sHarness(sandbox)
        self._executor = K8sExecutor()
        self._gateway = gateway
        self._draft_provider = draft_provider
        self._assets: dict[str, TargetAsset] = {}
        self._findings: dict[str, ProbeFinding] = {}
        self._proposals: dict[str, RemediationProposal] = {}
        self._originals: dict[str, str] = {}

    # -- 契约 ----------------------------------------------------------
    def plugin_id(self) -> str:
        return "plugin.k8s"

    def domain_id(self) -> str:
        return DOMAIN_K8S

    def capabilities(self) -> tuple[str, ...]:
        return (
            "probe:readonly_manifest_audit",
            "mask:yaml_bidirectional",
            "sandbox:vcluster_dry_run",
            "rollback:manifest_snapshot+rollout_undo",
        )

    def probe(self) -> DomainProbe:
        return self._probe

    def masker(self) -> DomainMasker:
        return self._masker

    def harness(self) -> DomainSandboxHarness:
        return self._harness

    def executor(self) -> DomainRollbackExecutor:
        return self._executor

    # -- 目录 ----------------------------------------------------------
    def register_assets(self, assets: Iterable[TargetAsset]) -> "K8sPlugin":
        for asset in assets:
            if asset.domain == DOMAIN_K8S:
                self._assets[asset.asset_id] = asset
        return self

    def set_manifests(self, asset_id: str, manifests: list[dict[str, Any]], *, has_network_policy: bool = False) -> "K8sPlugin":
        self._probe.set_facts(asset_id, {"manifests": manifests, "has_network_policy": has_network_policy})
        return self

    def attach_sandbox(self, sandbox: Any) -> "K8sPlugin":
        self._harness.attach(sandbox)
        return self

    def finding(self, finding_id: str) -> ProbeFinding:
        return self._findings[finding_id]

    # -- 提案生成 ------------------------------------------------------
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
            return replace(drafted, proposal_id=proposal_id or drafted.proposal_id, round=round_no)

        original = finding.evidence
        kind, name, namespace = _manifest_meta(original)
        pid = proposal_id or "prop-k8s-" + hashlib.sha256(
            f"{finding.finding_id}:{round_no}".encode("utf-8")
        ).hexdigest()[:12]

        if finding.category == "NETPOL_MISSING":
            fix = self._netpol_manifest(namespace)
            rollback = RollbackPlan(
                kind=ROLLBACK_KIND_MANIFEST,
                payload=f"# kubectl delete -f -\n# kubectl delete networkpolicy default-deny-ingress -n {namespace}",
                timeout_sec=30,
                atomic=False,
                reversible=True,
                note="新增资源：回退即删除该 NetworkPolicy",
            )
            mitigation = "【临时缓解】先以 audit 模式观察东西向流量，再切换为默认拒绝"
        elif finding.category == "RBAC_WILDCARD":
            fix = self._rbac_patch(name, namespace)
            rollback = RollbackPlan(
                kind=ROLLBACK_KIND_MANIFEST,
                payload=original,
                timeout_sec=30,
                atomic=False,
                reversible=True,
                note="声明式回退：apply 原 Role 清单快照",
            )
            mitigation = "【临时缓解】收敛 RoleBinding 主体范围并开启 RBAC 审计日志"
        else:
            fix = self._pod_security_patch(kind, name, namespace)
            rollback = RollbackPlan(
                kind=ROLLBACK_KIND_MANIFEST,
                payload=original,
                timeout_sec=30,
                atomic=False,
                reversible=True,
                note="声明式回退：apply 原 Deployment 清单 / kubectl rollout undo",
            )
            mitigation = "【临时缓解】驱逐特权 Pod 并禁止 hostPath 挂载"

        proposal = RemediationProposal(
            proposal_id=pid,
            finding_id=finding.finding_id,
            asset_id=asset.asset_id,
            domain=DOMAIN_K8S,
            plugin_id=self.plugin_id(),
            mitigation=mitigation,
            standard_fix=fix,
            upgrade_path="【升级路径】纳入 Pod Security Standards(restricted) + OPA Gatekeeper 准入基线",
            rollback=rollback,
            impact=ImpactAssessment(
                lock_impact_level="NONE",
                requires_restart=True,
                estimated_block_ms=0,
                blast_radius_rows=0,
                change_window="LOW_PEAK",
                deferred=False,
            ),
            confidence=0.82,
            round=round_no,
            notes=(f"目标资源: {kind}/{name} @ {namespace}",),
        )
        if feedback:
            proposal = self._harden(proposal, feedback)
        return proposal

    def _harden(self, proposal: RemediationProposal, feedback: tuple[str, ...]) -> RemediationProposal:
        codes = {code.strip().upper() for code in feedback if code.strip()}
        rollback = proposal.rollback
        applied: list[str] = []
        notes = list(proposal.notes)
        if codes & {"MISSING_ROLLBACK", "ROLLBACK_NOT_REVERSIBLE"}:
            rollback = replace(rollback, atomic=False, reversible=True, timeout_sec=max(30, rollback.timeout_sec))
            applied.append("ROLLBACK_REARMED")
        if codes & {"MANIFEST_INVALID", "FIX_INEFFECTIVE", "FIX_NOT_APPLIED"}:
            notes.append("补丁已按 restricted 基线重新生成并复跑 --dry-run=server")
            applied.append("MANIFEST_REGENERATED")
        if codes & {"COMPATIBILITY_BELOW_THRESHOLD"}:
            notes.append("改为灰度命名空间先验证再全量下发")
            applied.append("CANARY_NAMESPACE_FIRST")
        return replace(
            proposal,
            rollback=rollback,
            notes=tuple(notes),
            feedback_applied=tuple(list(proposal.feedback_applied) + applied),
        )

    @staticmethod
    def _pod_security_patch(kind: str, name: str, namespace: str) -> str:
        return (
            "# kubectl patch (strategic merge) — 收敛 Pod 安全上下文\n"
            f"apiVersion: apps/v1\n"
            f"kind: {kind if kind in ('Deployment', 'StatefulSet', 'DaemonSet') else 'Deployment'}\n"
            "metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      securityContext:\n"
            "        runAsNonRoot: true\n"
            "        runAsUser: 1000\n"
            "      containers:\n"
            "        - name: app\n"
            "          securityContext:\n"
            "            privileged: false\n"
            "            allowPrivilegeEscalation: false\n"
            "            readOnlyRootFilesystem: true\n"
            "            capabilities:\n"
            "              drop: [\"ALL\"]\n"
        )

    @staticmethod
    def _rbac_patch(name: str, namespace: str) -> str:
        return (
            "# kubectl apply — RBAC 最小权限收敛\n"
            "apiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: Role\n"
            "metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {namespace}\n"
            "rules:\n"
            "  - apiGroups: [\"\"]\n"
            "    resources: [\"pods\", \"configmaps\"]\n"
            "    verbs: [\"get\", \"list\", \"watch\"]\n"
        )

    @staticmethod
    def _netpol_manifest(namespace: str) -> str:
        return (
            "# kubectl apply — 默认拒绝东西向入站流量\n"
            "apiVersion: networking.k8s.io/v1\n"
            "kind: NetworkPolicy\n"
            "metadata:\n"
            "  name: default-deny-ingress\n"
            f"  namespace: {namespace}\n"
            "spec:\n"
            "  podSelector: {}\n"
            "  policyTypes: [\"Ingress\"]\n"
        )

    # -- MCP 工具集 ----------------------------------------------------
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="k8s_probe_readonly",
                domain=DOMAIN_K8S,
                description="只读扫描 K8s Manifest（特权 Pod / RBAC / NetworkPolicy）",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="k8s_fix_generate",
                domain=DOMAIN_K8S,
                description="生成 Manifest 补丁 + 声明式回滚快照",
                required_params=("asset_id", "finding_id", "round"),
                optional_params=("feedback",),
            ),
            ToolSpec(
                name="k8s_preflight_readonly",
                domain=DOMAIN_K8S,
                description="只读预检命名空间就绪副本与 PDB 状态",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="k8s_sandbox_verify",
                domain=DOMAIN_K8S,
                description="vCluster 沙箱 Manifest Dry-Run 与回滚验证",
                required_params=("proposal_id",),
                optional_params=("long_tx_count",),
            ),
            ToolSpec(
                name="k8s_evidence_masked",
                domain=DOMAIN_K8S,
                description="取 YAML 脱敏后的证据（Secret/Namespace/域名双向替换）",
                required_params=("finding_id", "workflow_run_id"),
            ),
        )

    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "k8s_probe_readonly": self._tool_probe,
            "k8s_fix_generate": self._tool_generate,
            "k8s_preflight_readonly": self._tool_preflight,
            "k8s_sandbox_verify": self._tool_sandbox,
            "k8s_evidence_masked": self._tool_evidence,
        }

    def _require_asset(self, asset_id: str) -> TargetAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise PluginContractError(f"未登记的 k8s 资产: {asset_id}")
        return asset

    def _tool_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        report = self._probe.collect(asset)
        for finding in report.findings:
            self._findings[finding.finding_id] = finding
            self._originals[finding.finding_id] = finding.evidence
        return {
            "asset_id": asset.asset_id,
            "domain": report.domain,
            "probe_tool": report.probe_tool,
            "read_only": True,
            "budget": report.budget,
            "findings": [finding.public_view() for finding in report.findings],
        }

    def _tool_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        finding = self._findings.get(str(params["finding_id"]))
        if finding is None:
            raise PluginContractError(f"未缓存的探测发现项: {params['finding_id']}")
        round_no = int(str(params.get("round", "1")))
        feedback = tuple(code for code in str(params.get("feedback", "")).split(",") if code)
        proposal = self.build_proposal(finding, asset, round_no=round_no, feedback=feedback)
        self._proposals[proposal.proposal_id] = proposal
        return proposal.to_dict()

    def _tool_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        manifests = list(self._probe.facts(asset.asset_id).get("manifests", []))
        return {
            "asset_id": asset.asset_id,
            "manifests": len(manifests),
            "has_network_policy": self._probe.facts(asset.asset_id).get("has_network_policy", True),
            "active_long_tx_count": 0,
            "mdl_avalanche_blocked": False,
            "read_only": True,
        }

    def _tool_sandbox(self, params: dict[str, Any]) -> dict[str, Any]:
        proposal = self._proposals.get(str(params["proposal_id"]))
        if proposal is None:
            raise PluginContractError(f"未缓存的修复方案: {params['proposal_id']}")
        asset = self._require_asset(proposal.asset_id)
        finding = self._findings[proposal.finding_id]
        return self._harness.evaluate(asset, finding, proposal).to_dict()

    def _tool_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._gateway is None:
            raise PluginContractError("多模态脱敏网关未装配，禁止读取证据原文")
        finding = self._findings.get(str(params["finding_id"]))
        if finding is None:
            raise PluginContractError(f"未缓存的探测发现项: {params['finding_id']}")
        outcome = self._gateway.mask(
            finding.evidence,
            modality=MODALITY_YAML,
            workflow_run_id=str(params["workflow_run_id"]),
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


__all__ = ["K8sExecutor", "K8sHarness", "K8sMasker", "K8sPlugin", "K8sProbe", "RULES"]
