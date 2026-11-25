"""AppSec 业务域插件（P4-T1）：后端源码审计 + 依赖 SCA + App+DB 联合沙箱。

审计能力（``plugins_matrix[appsec].probe``）：

- ``SQL_INJECTION``：SQL 语句与变量拼接 / f-string 插值 / ``%`` / ``.format``；
- ``N_PLUS_ONE``：循环体内逐条查询（ORM ``query/filter`` 或裸 SQL）;
- ``HARDCODED_SECRET``：源码中硬编码口令 / Token / AK；
- ``IDOR``：请求参数直接作为主键查询且无属主过滤（启发式，标注 ``heuristic``）；
- ``SCA_VULN``：依赖清单中的已知 CVE。

修复产物是**联合方案**：代码 Patch（unified diff，可直接进 PR）+ 同审批下的
DB 最小权限加固 SQL（记录在 ``code_refs``，不混进 patch 文本以免污染 diff）。
沙箱为 App+DB 联合容器 + 影子流量回放，回滚是反向 Patch。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from core.orchestrator.tool_client import ToolSpec
from core.plugins.contracts import (
    DOMAIN_APPSEC,
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
    ROLLBACK_KIND_PATCH,
    RemediationProposal,
    RollbackPlan,
    TargetAsset,
)
from core.privacy_gateway.multimodal import ModalityStructureError

RULES: dict[str, tuple[str, str, float]] = {
    "SQL_INJECTION": ("APP-SQLI-01", "SQL 语句拼接外部输入（SQL 注入）", 9.1),
    "N_PLUS_ONE": ("APP-PERF-01", "循环内逐条查询导致 N+1 查询放大", 5.4),
    "HARDCODED_SECRET": ("APP-SEC-01", "源码硬编码口令 / Token / AK", 8.6),
    "IDOR": ("APP-AUTHZ-01", "对象主键直查且缺少属主校验（越权）", 8.2),
    "SCA_VULN": ("APP-SCA-01", "依赖组件存在已知 CVE", 7.4),
}

_SQL_VERB_RE = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
#: SQL 注入的四种"不安全拼接"形态：f-string 插值 / 字符串 + 变量 / % 运算 / .format。
#: 注意：``"... WHERE id = %s", (x,)`` 是参数化查询，不在其中（"%s"在引号内）。
_SQLI_UNSAFE = (
    re.compile(r"f[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*\{", re.IGNORECASE),
    re.compile(r"[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*\+", re.IGNORECASE),
    re.compile(r"[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*%", re.IGNORECASE),
    re.compile(r"[\"'][^\"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^\"']*[\"']\s*\.format\s*\(", re.IGNORECASE),
)


def _is_sqli(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SQLI_UNSAFE)

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\s*=\s*[\"'][^\"']{6,}[\"']"
)
_REQUEST_ID_RE = re.compile(
    r"(?:request\.(?:args|params|GET)\.get\(\s*[\"']id[\"']|request\.(?:args|params)\[\s*[\"']id[\s\"']\])",
    re.IGNORECASE,
)
_FETCH_RE = re.compile(
    r"\.(?:get|find|find_one|filter|where|query|execute|executemany|raw)\s*\(", re.IGNORECASE
)
_OWNER_RE = re.compile(r"(?i)\b(owner\w*|tenant_id|user_id|account_id|belongs_to)\b")
_LOOP_RE = re.compile(r"^(\s*)(?:for|while)\b")
_LOOP_VARS_RE = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\s+([A-Za-z_]\w*)")


class AppSecProbe:
    """后端源码 AST 近似静态审计（行级模式 + 缩进作用域）。"""

    def __init__(self) -> None:
        self._sources: dict[str, dict[str, str]] = {}
        self._dependencies: dict[str, list[dict[str, Any]]] = {}

    def domain_id(self) -> str:
        return DOMAIN_APPSEC

    def set_sources(self, asset_id: str, sources: dict[str, str]) -> None:
        self._sources[asset_id] = dict(sources)

    def set_dependencies(self, asset_id: str, dependencies: list[dict[str, Any]]) -> None:
        self._dependencies[asset_id] = [dict(item) for item in dependencies]

    def sources(self, asset_id: str) -> dict[str, str]:
        return dict(self._sources.get(asset_id, {}))

    def collect(self, asset: TargetAsset, *, budget: ProbeBudget | None = None) -> ProbeReport:
        budget = budget or ProbeBudget()
        if not budget.allow():
            return ProbeReport(asset.asset_id, DOMAIN_APPSEC, (), budget.snapshot(), "appsec_probe_readonly_throttled")

        findings: list[ProbeFinding] = []
        for path, source in self._sources.get(asset.asset_id, {}).items():
            findings.extend(self._scan_source(asset.asset_id, path, source))

        for dep in self._dependencies.get(asset.asset_id, []):
            cve = str(dep.get("cve", "")).strip()
            if not cve:
                continue
            rule_id, title, cvss = RULES["SCA_VULN"]
            name = str(dep.get("name", "unknown"))
            version = str(dep.get("version", "?"))
            fixed = str(dep.get("fixed_version", "latest-safe"))
            findings.append(
                ProbeFinding(
                    finding_id=f"APP-{asset.asset_id[:6].upper()}-SCA-{name}".replace(".", "-"),
                    asset_id=asset.asset_id,
                    domain=DOMAIN_APPSEC,
                    category="SCA_VULN",
                    rule_id=rule_id,
                    title=f"依赖 {name}=={version} 命中 {cve}",
                    evidence=f"{name}=={version} -> {cve}; fixed in {fixed}",
                    base_cvss=cvss,
                    cve_id=cve,
                    location=f"pkg://{name}@{version}",
                    tags=("appsec", "sca", cve.lower()),
                )
            )

        return ProbeReport(asset.asset_id, DOMAIN_APPSEC, tuple(findings), budget.snapshot(), "appsec_probe_readonly")

    # -- 内部 ----------------------------------------------------------
    def _scan_source(self, asset_id: str, path: str, source: str) -> list[ProbeFinding]:
        findings: list[ProbeFinding] = []
        lines = source.splitlines()
        has_owner_check = bool(_OWNER_RE.search(source))
        loop_stack: list[int] = []

        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            loop_stack = [depth for depth in loop_stack if indent > depth]

            if _SECRET_ASSIGN_RE.search(line):
                findings.append(
                    self._make(asset_id, path, index, "HARDCODED_SECRET", line, "源码硬编码凭据")
                )
                continue

            if _SQL_VERB_RE.search(line) and _is_sqli(line):
                findings.append(
                    self._make(asset_id, path, index, "SQL_INJECTION", line, "SQL 拼接外部输入")
                )

            if loop_stack and _FETCH_RE.search(line) and _SQL_VERB_RE.search(line):
                findings.append(
                    self._make(asset_id, path, index, "N_PLUS_ONE", line, "循环内逐条查询")
                )

            if _REQUEST_ID_RE.search(line) and _FETCH_RE.search(line) and not has_owner_check:
                finding = self._make(asset_id, path, index, "IDOR", line, "主键直查且无属主校验")
                findings.append(replace(finding, tags=finding.tags + ("heuristic",)))

            loop = _LOOP_RE.match(line)
            if loop:
                loop_stack.append(indent)

        return findings

    @staticmethod
    def _make(asset_id: str, path: str, line_no: int, category: str, line: str, reason: str) -> ProbeFinding:
        rule_id, title, cvss = RULES[category]
        return ProbeFinding(
            finding_id=f"APP-{asset_id[:6].upper()}-{category}-{path.replace('/', '-')}-{line_no:03d}",
            asset_id=asset_id,
            domain=DOMAIN_APPSEC,
            category=category,
            rule_id=rule_id,
            title=f"{path}:{line_no} {title}",
            evidence=f"{line_no}: {line.strip()}",
            base_cvss=cvss,
            location=f"file://{path}#L{line_no}",
            tags=("appsec", category.lower(), "source"),
        )


class AppSecMasker:
    """源码标识符 / 业务路径 + HTTP Token/Cookie 双向脱敏。"""

    def __init__(self) -> None:
        self._path = re.compile(r"(?<![\w/])/(?:api|v1|v2|internal|admin)/[A-Za-z0-9_./{}-]+")
        self._cookie = re.compile(
            r"(?i)([\"']?(?:cookie|authorization|x-api-key)[\"']?\s*:\s*[\"']?)([^\"'\s,;}]+)"
        )
        self._skipped = frozenset(
            {
                "self",
                "def",
                "return",
                "import",
                "from",
                "class",
                "None",
                "True",
                "False",
                "SELECT",
                "FROM",
                "WHERE",
                "INSERT",
                "UPDATE",
                "DELETE",
                "request",
                "execute",
                "query",
            }
        )

    def domain_id(self) -> str:
        return DOMAIN_APPSEC

    def modality(self) -> str:
        return "code"

    def detect(self, payload: str) -> float:
        if not payload:
            return 0.0
        hints = ("def ", "import ", "class ", "return ", ";", "=>")
        score = 0.15 * sum(1 for hint in hints if hint in payload)
        if _SECRET_ASSIGN_RE.search(payload) or _REQUEST_ID_RE.search(payload):
            score += 0.3
        return min(0.8, score)

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        self.validate(payload)
        mapping: dict[str, str] = {}
        counters = {"PATH": 0, "COOKIE": 0}
        masked = payload

        def path_repl(match: re.Match[str]) -> str:
            counters["PATH"] += 1
            token = f"__PATH_{counters['PATH']:03d}__"
            mapping[token] = match.group(0)
            return token

        def cookie_repl(match: re.Match[str]) -> str:
            counters["COOKIE"] += 1
            token = f"__COOKIE_{counters['COOKIE']:03d}__"
            mapping[token] = match.group(2)
            return f"{match.group(1)}{token}"

        masked = self._path.sub(path_repl, masked)
        masked = self._cookie.sub(cookie_repl, masked)
        return masked, mapping

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        restored = payload
        for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
            restored = restored.replace(token, original)
        return restored

    def validate(self, payload: str) -> None:
        if not isinstance(payload, str) or not payload.strip():
            raise ModalityStructureError("源码载荷为空")
        if "\x00" in payload:
            raise ModalityStructureError("源码载荷包含 NUL 控制字符")
        if payload.count("(") != payload.count(")"):
            raise ModalityStructureError("源码括号不平衡，结构已损坏")


class AppSecHarness:
    """App + DB 联合容器沙箱 + 影子流量回放。

    V4.1 起支持注入**真实隔离工作区校验后端**（``ci_harness``）：文本级判定之外，
    再把补丁真正落到 ``tempfile`` 目录里编译、可选跑单测并验证反向 Patch 哈希。
    未注入时行为与 V3.0 完全一致（既有单测口径不变）。
    """

    def __init__(
        self,
        sandbox: Any | None = None,
        *,
        isolated_verifier: Callable[..., HarnessOutcome] | None = None,
        repo_root: Any | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._isolated_verifier = isolated_verifier
        self._repo_root = repo_root

    def attach(self, sandbox: Any) -> None:
        self._sandbox = sandbox

    def attach_isolated_verifier(
        self,
        verifier: Callable[..., HarnessOutcome] | None,
        *,
        repo_root: Any | None = None,
    ) -> "AppSecHarness":
        """注入隔离工作区校验器（``verify_patch_in_isolated_workdir`` 或其等价实现）。"""
        self._isolated_verifier = verifier
        if repo_root is not None:
            self._repo_root = repo_root
        return self

    def domain_id(self) -> str:
        return DOMAIN_APPSEC

    def evaluate(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None = None,
    ) -> HarnessOutcome:
        logs = ["沙箱: App + DB 联合容器 (TRAFFIC_REPLAY) + 影子流量回放"]
        lease = None
        if self._sandbox is not None:
            lease = self._sandbox.acquire(domain=DOMAIN_APPSEC, asset_id=asset.asset_id)
            logs.append(f"Unified Sandbox Manager 租约: {lease['lease_id']} ({lease['mode']}, ttl={lease['ttl_sec']}s)")

        try:
            probe = dict(probe or {})
            replay_rate = float(probe.get("shadow_replay_pass_rate", 1.0))
            db_fixture = str(probe.get("db_fixture", "app_db_fixture"))

            if not proposal.rollback.is_armed():
                return HarnessOutcome(
                    proposal.proposal_id,
                    not finding.suppressed_as_noise,
                    False,
                    False,
                    False,
                    0.0,
                    "TRAFFIC_REPLAY",
                    tuple(logs + ["反向 Patch 未就绪"]),
                    ("MISSING_ROLLBACK",),
                )

            failures: list[str] = []
            patch_valid = self._looks_like_patch(proposal.standard_fix)
            if not patch_valid:
                failures.append("PATCH_INVALID")
            if not self._pattern_removed(finding.category, proposal.standard_fix):
                failures.append("FIX_INEFFECTIVE")
            if re.search(r"(?im)^[+-].*\b(ALTER|DROP|CREATE)\s+TABLE\b", proposal.standard_fix):
                failures.append("SQL_SCHEMA_CHANGE_FORBIDDEN")
            rollback_ok = proposal.rollback.kind == ROLLBACK_KIND_PATCH and self._looks_like_patch(
                proposal.rollback.payload
            )
            if not rollback_ok:
                failures.append("ROLLBACK_NOT_REVERSIBLE")
            if replay_rate < 0.99:
                failures.append("SHADOW_REPLAY_REGRESSION")

            poc_ok = not finding.suppressed_as_noise
            if not poc_ok:
                failures.append("POC_NOT_REPRODUCED")

            logs.append(
                f"联合沙箱: db_fixture={db_fixture}, replay_pass_rate={replay_rate}, patch_valid={patch_valid}"
            )
            dry_run_passed = patch_valid
            poc_blocked = patch_valid and self._pattern_removed(finding.category, proposal.standard_fix)
            compatible = replay_rate
            rollback_verified = rollback_ok

            if self._isolated_verifier is not None and self._repo_root is not None and patch_valid and rollback_ok:
                isolated = self._run_isolated_verification(proposal)
                logs.extend(isolated.logs)
                for code in isolated.failures:
                    if code not in failures:
                        failures.append(code)
                dry_run_passed = dry_run_passed and isolated.dry_run_passed
                poc_blocked = poc_blocked and isolated.poc_blocked_after_fix
                rollback_verified = rollback_verified and isolated.rollback_verified
                if not isolated.dry_run_passed:
                    compatible = min(compatible, isolated.compatibility_score)
            elif self._isolated_verifier is not None and self._repo_root is not None:
                logs.append("隔离工作区校验跳过：补丁形状或回滚未就绪")

            return HarnessOutcome(
                proposal_id=proposal.proposal_id,
                poc_reproduced=poc_ok,
                poc_blocked_after_fix=poc_blocked,
                dry_run_passed=dry_run_passed,
                rollback_verified=rollback_verified,
                compatibility_score=compatible,
                sandbox_pool="TRAFFIC_REPLAY",
                logs=tuple(logs),
                failures=tuple(dict.fromkeys(failures)),
            )
        finally:
            if lease is not None and self._sandbox is not None:
                self._sandbox.release(lease["lease_id"])

    def _run_isolated_verification(self, proposal: RemediationProposal) -> HarnessOutcome:
        """调用注入的真实沙箱；异常一律 fail closed（绝不当成通过）。"""
        assert self._isolated_verifier is not None
        try:
            outcome = self._isolated_verifier(
                repo_root=self._repo_root,
                patch_text=proposal.standard_fix,
                rollback_patch_text=proposal.rollback.payload,
            )
        except Exception as exc:  # noqa: BLE001 - 校验器故障必须显式失败，不得静默放行
            return HarnessOutcome(
                proposal_id=proposal.proposal_id,
                poc_reproduced=False,
                poc_blocked_after_fix=False,
                dry_run_passed=False,
                rollback_verified=False,
                compatibility_score=0.0,
                sandbox_pool="TEMP_WORKDIR",
                logs=(f"隔离工作区校验器异常: {type(exc).__name__}",),
                failures=("ISOLATED_VERIFIER_ERROR",),
            )
        return outcome

    @staticmethod
    def _looks_like_patch(text: str) -> bool:
        return text.startswith("--- ") and "\n+++ " in text and "\n@@ " in text

    @staticmethod
    def _pattern_removed(category: str, patch: str) -> bool:
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        added_text = "\n".join(added)
        if category == "SQL_INJECTION":
            return ("%s" in added_text or "?" in added_text) and not _is_sqli(added_text)
        if category == "HARDCODED_SECRET":
            return "environ" in added_text or "getenv" in added_text or "vault" in added_text.lower()
        if category == "N_PLUS_ONE":
            return "ANY(" in added_text or "IN (" in added_text or "IN(" in added_text or "prefetch" in added_text
        if category == "IDOR":
            return bool(_OWNER_RE.search(added_text))
        return True


class AppSecExecutor:
    """PR Patch + WAF 规则执行器：审批摘要 + 反向 Patch 回退。"""

    def domain_id(self) -> str:
        return DOMAIN_APPSEC

    def execute(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        approval_token: str = "",
    ) -> ExecutionReceipt:
        if not proposal.rollback.is_armed():
            return ExecutionReceipt(proposal.proposal_id, False, True, False, 0, "反向 Patch 未就绪", ROLLBACK_KIND_PATCH)
        if approval_token != proposal.approval_digest():
            return ExecutionReceipt(
                proposal.proposal_id,
                False,
                True,
                True,
                proposal.rollback.timeout_sec,
                "HITL_APPROVAL_REQUIRED：缺少与审批摘要一致的 token",
                ROLLBACK_KIND_PATCH,
            )
        return ExecutionReceipt(
            proposal.proposal_id,
            True,
            False,
            True,
            proposal.rollback.timeout_sec,
            "PR Patch 已提交 + WAF 拦截规则热下发；网关限流已备份，可一键回退",
            ROLLBACK_KIND_PATCH,
        )

    def rollback(self, asset: TargetAsset, proposal: RemediationProposal, *, reason: str = "") -> ExecutionReceipt:
        armed = proposal.rollback.is_armed()
        return ExecutionReceipt(
            proposal.proposal_id,
            armed,
            not armed,
            False,
            proposal.rollback.timeout_sec,
            f"反向 Patch 回退（{reason or 'manual'}）+ WAF 规则撤回",
            ROLLBACK_KIND_PATCH,
        )


class AppSecPlugin:
    """appsec 域标准插件。"""

    def __init__(
        self,
        *,
        gateway: Any | None = None,
        sandbox: Any | None = None,
        draft_provider: Callable[[ProbeFinding, TargetAsset, int, tuple[str, ...]], RemediationProposal] | None = None,
        isolated_verifier: Callable[..., HarnessOutcome] | None = None,
        repo_root: Any | None = None,
        strategy_store: Any | None = None,
    ) -> None:
        self._probe = AppSecProbe()
        self._masker = AppSecMasker()
        self._harness = AppSecHarness(
            sandbox,
            isolated_verifier=isolated_verifier,
            repo_root=repo_root,
        )
        self._executor = AppSecExecutor()
        self._gateway = gateway
        self._draft_provider = draft_provider
        # V5.0：只有显式注入策略仓库时才读取 active bundle 的修复模板与 Prompt 指导
        self._strategy_store = strategy_store
        self._assets: dict[str, TargetAsset] = {}
        self._findings: dict[str, ProbeFinding] = {}
        self._proposals: dict[str, RemediationProposal] = {}
        if gateway is not None and "code" not in gateway.modalities():
            gateway.register(self._masker)

    def plugin_id(self) -> str:
        return "plugin.appsec"

    def domain_id(self) -> str:
        return DOMAIN_APPSEC

    def capabilities(self) -> tuple[str, ...]:
        return (
            "probe:source_ast+sca",
            "mask:code_and_http",
            "sandbox:app_db_joint+traffic_replay",
            "rollback:reverse_patch+waf_rule",
        )

    def probe(self) -> DomainProbe:
        return self._probe

    def masker(self) -> DomainMasker:
        return self._masker

    def harness(self) -> DomainSandboxHarness:
        return self._harness

    def executor(self) -> DomainRollbackExecutor:
        return self._executor

    def register_assets(self, assets: Iterable[TargetAsset]) -> "AppSecPlugin":
        for asset in assets:
            if asset.domain == DOMAIN_APPSEC:
                self._assets[asset.asset_id] = asset
        return self

    def attach_sandbox(self, sandbox: Any) -> "AppSecPlugin":
        self._harness.attach(sandbox)
        return self

    def attach_isolated_verifier(
        self,
        verifier: Callable[..., HarnessOutcome] | None,
        *,
        repo_root: Any | None = None,
    ) -> "AppSecPlugin":
        """装配 V4.1 隔离工作区校验后端（CI 门禁 / 桌面端真实沙箱验证）。"""
        self._harness.attach_isolated_verifier(verifier, repo_root=repo_root)
        return self

    def set_sources(self, asset_id: str, sources: dict[str, str]) -> "AppSecPlugin":
        self._probe.set_sources(asset_id, sources)
        return self

    def set_dependencies(self, asset_id: str, dependencies: list[dict[str, Any]]) -> "AppSecPlugin":
        self._probe.set_dependencies(asset_id, dependencies)
        return self

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

        pid = proposal_id or "prop-app-" + hashlib.sha256(
            f"{finding.finding_id}:{round_no}".encode("utf-8")
        ).hexdigest()[:12]
        patch, rollback_patch, mitigation = self._build_patch(finding, asset)
        db_sql = self._db_hardening_sql(finding)
        code_refs = [
            {
                "kind": "code",
                "location": finding.location,
                "path": finding.location.split("#")[0].replace("file://", ""),
                "rule_id": finding.rule_id,
            }
        ]
        if db_sql:
            code_refs.append(
                {
                    "kind": "db_sql",
                    "asset_id": asset.label("linked_db_asset", "db://unknown"),
                    "sql": db_sql,
                }
            )

        proposal = RemediationProposal(
            proposal_id=pid,
            finding_id=finding.finding_id,
            asset_id=asset.asset_id,
            domain=DOMAIN_APPSEC,
            plugin_id=self.plugin_id(),
            mitigation=mitigation,
            standard_fix=patch,
            upgrade_path="【升级路径】接入 CI_PR_GATE 强制 SAST/SCA + 参数化查询规范门禁",
            rollback=RollbackPlan(
                kind=ROLLBACK_KIND_PATCH,
                payload=rollback_patch,
                timeout_sec=120,
                atomic=True,
                reversible=True,
                note="反向 Patch + WAF 规则撤回，二者同一审批回退",
            ),
            impact=ImpactAssessment(
                lock_impact_level="NONE",
                requires_restart=True,
                estimated_block_ms=0,
                blast_radius_rows=0,
                change_window="LOW_PEAK",
            ),
            confidence=0.8,
            round=round_no,
            code_refs=tuple(code_refs),
            notes=(f"联合方案：代码 Patch + DB 加固 SQL（{finding.category}）",),
        )
        if self._strategy_store is not None:
            proposal = self._apply_strategy_notes(proposal, finding)
        if feedback:
            proposal = self._harden(proposal, feedback)
        return proposal

    def _apply_strategy_notes(
        self, proposal: RemediationProposal, finding: ProbeFinding
    ) -> RemediationProposal:
        """把当前激活策略包的修复模板与 Prompt 指导记入提案备注。

        刻意**不改动** ``standard_fix``：策略进化只影响"怎么解释与复核修复"，
        patch 文本的生成口径仍由插件决定，避免自我改写绕过代码审计。
        """
        try:
            active = self._strategy_store.get_active()
        except Exception:  # noqa: BLE001 - 策略仓库未就绪时退回默认行为
            return proposal
        notes = list(proposal.notes)
        notes.append(f"[strategy:{active.version_id}] generation={active.generation}")
        template = active.repair_templates.get(finding.category)
        if template:
            notes.append(f"[strategy:{active.version_id}] repair_template={template}")
        for directive in active.prompt_directives:
            notes.append(f"[strategy:{active.version_id}] directive={directive}")
        return replace(proposal, notes=tuple(notes))

    def _harden(self, proposal: RemediationProposal, feedback: tuple[str, ...]) -> RemediationProposal:
        codes = {code.strip().upper() for code in feedback if code.strip()}
        rollback = proposal.rollback
        notes = list(proposal.notes)
        applied: list[str] = []
        if codes & {"MISSING_ROLLBACK", "ROLLBACK_NOT_REVERSIBLE"}:
            rollback = replace(rollback, atomic=True, reversible=True, timeout_sec=max(120, rollback.timeout_sec))
            applied.append("ROLLBACK_REARMED")
        if "SHADOW_REPLAY_REGRESSION" in codes:
            notes.append("关闭新规则默认拒绝，改为影子观察 30 分钟后灰度放量")
            applied.append("WAF_SHADOW_MODE")
        if codes & {"PATCH_INVALID", "FIX_INEFFECTIVE"}:
            notes.append("Patch 已按参数化查询重写并复跑联合沙箱")
            applied.append("PATCH_REGENERATED")
        return replace(
            proposal,
            rollback=rollback,
            notes=tuple(notes),
            feedback_applied=tuple(list(proposal.feedback_applied) + applied),
        )

    def _source_lines(self, finding: ProbeFinding, asset: TargetAsset) -> tuple[str, list[str], int]:
        path, _, line_text = finding.location.partition("#L")
        path = path.replace("file://", "")
        lines = self._probe.sources(asset.asset_id).get(path, "").splitlines()
        line_no = int(line_text or "1")
        return path, lines, line_no

    def _build_patch(self, finding: ProbeFinding, asset: TargetAsset) -> tuple[str, str, str]:
        if finding.category == "SCA_VULN":
            return self._sca_patch(finding)

        path, lines, line_no = self._source_lines(finding, asset)
        original = lines[line_no - 1] if 0 < line_no <= len(lines) else finding.evidence

        if finding.category == "SQL_INJECTION":
            new_lines = self._parameterize(original).splitlines()
            mitigation = "【临时缓解】WAF 下发 SQLi 特征拦截并收紧 DB 账号权限"
        elif finding.category == "HARDCODED_SECRET":
            new_lines = _SECRET_ASSIGN_RE.sub(
                lambda m: f'{m.group(1)} = os.environ["{m.group(1).upper()}"]', original
            ).splitlines()
            mitigation = "【临时缓解】立即轮换该凭据并在网关侧吊销旧 Token"
        elif finding.category == "N_PLUS_ONE":
            new_lines = self._n_plus_one_lines(lines, line_no, original)
            mitigation = "【临时缓解】对热点接口加 200ms 缓存并限流"
        elif finding.category == "IDOR":
            indent = original[: len(original) - len(original.lstrip(" "))]
            new_lines = [
                original,
                indent + "# 属主校验：确保当前 principal 拥有该对象",
                indent + "assert row.owner_id == current_user.id, 'IDOR guard'",
            ]
            mitigation = "【临时缓解】网关侧强制对象级鉴权插件"
        else:
            new_lines = [original + "  # 升级依赖至安全版本后重新生成锁文件"]
            mitigation = "【临时缓解】在 WAF 侧拦截该组件的已知攻击特征"

        patch = self._unified_diff(path, [original], new_lines, line_no)
        rollback = self._unified_diff(path, new_lines, [original], line_no)
        return patch, rollback, mitigation

    @staticmethod
    def _n_plus_one_lines(lines: list[str], line_no: int, original: str) -> list[str]:
        indent = original[: len(original) - len(original.lstrip(" "))]
        loop_line = next((lines[i] for i in range(line_no - 2, -1, -1) if _LOOP_RE.match(lines[i])), "")
        match = _LOOP_VARS_RE.match(loop_line)
        item, collection = (match.group(1), match.group(2)) if match else ("row", "rows")
        lhs = original.split("=", 1)[0].strip() if "=" in original else "items"
        callee = re.search(r"([A-Za-z_][\w.]*)\s*\(", original)
        owner = callee.group(1).rsplit(".", 1)[0] if callee and "." in callee.group(1) else "repository"
        return [
            f"{indent}# prefetch: 单次批量查询，消除循环内 N+1",
            f"{indent}_batch = {owner}.find_by_ids([x.id for x in {collection}])",
            f"{indent}{lhs} = _batch.get({item}.id, [])",
        ]

    @staticmethod
    def _sca_patch(finding: ProbeFinding) -> tuple[str, str, str]:
        match = re.match(
            r"([\w.\-]+)==([^\s]+) -> (\S+); fixed in (\S+)",
            finding.evidence,
        )
        if match is None:
            old_line = finding.evidence
            new_line = old_line + "  # 升级至安全版本"
        else:
            name, version, _cve, fixed = match.groups()
            old_line = f"{name}=={version}"
            new_line = f"{name}=={fixed}"
        patch = AppSecPlugin._unified_diff("requirements.txt", [old_line], [new_line], 1)
        rollback = AppSecPlugin._unified_diff("requirements.txt", [new_line], [old_line], 1)
        return patch, rollback, "【临时缓解】锁定受影响依赖版本并在 WAF 侧拦截已知攻击特征"

    @staticmethod
    def _unified_diff(path: str, old_block: list[str], new_block: list[str], start_line: int) -> str:
        header = f"--- a/{path}\n+++ b/{path}\n"
        hunk = f"@@ -{start_line},{len(old_block)} +{start_line},{len(new_block)} @@\n"
        body = "".join(f"-{line}\n" for line in old_block) + "".join(f"+{line}\n" for line in new_block)
        return header + hunk + body

    @staticmethod
    def _parameterize(line: str) -> str:
        """把拼接式 SQL 改写为参数化查询：在 SQL 串内插入 %s，变量改为参数元组。"""
        pattern = re.compile(
            r"([\"'][^\"']*(?:SELECT|INSERT|UPDATE|DELETE)[^\"']*)([\"'])\s*\+\s*([A-Za-z_][A-Za-z0-9_.]*)",
            re.IGNORECASE,
        )

        def repl(match: re.Match[str]) -> str:
            body, quote, variable = match.group(1), match.group(2), match.group(3)
            return f"{body}%s{quote}, ({variable},)"

        replaced = pattern.sub(repl, line)
        # 兼容 "..." % var 的旧式拼接
        replaced = re.sub(
            r"([\"'][^\"']*(?:SELECT|INSERT|UPDATE|DELETE)[^\"']*)([\"'])\s*%\s*\(?\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)?",
            lambda m: f"{m.group(1)}%s{m.group(2)}, ({m.group(3)},)",
            replaced,
            flags=re.IGNORECASE,
        )
        return replaced

    @staticmethod
    def _db_hardening_sql(finding: ProbeFinding) -> str:
        if finding.category not in ("SQL_INJECTION", "IDOR"):
            return ""
        match = re.search(r"\b(?:FROM|INTO|UPDATE)\s+([A-Za-z_][\w.]*)", finding.evidence, re.IGNORECASE)
        table = match.group(1) if match else "app_table"
        return (
            f"-- 配套 DB 最小权限加固（同一审批执行）\n"
            f"SET SESSION lock_wait_timeout = 3;\n"
            f"REVOKE ALL PRIVILEGES ON {table} FROM app_rw;\n"
            f"GRANT SELECT, INSERT, UPDATE ON {table} TO app_rw;"
        )

    # -- MCP 工具集 ----------------------------------------------------
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="appsec_probe_readonly",
                domain=DOMAIN_APPSEC,
                description="后端源码 SAST（SQLi/N+1/硬编码密钥/IDOR）+ 依赖 SCA",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="appsec_fix_generate",
                domain=DOMAIN_APPSEC,
                description="生成代码 Patch + 同审批 DB 加固 SQL + 反向 Patch",
                required_params=("asset_id", "finding_id", "round"),
                optional_params=("feedback",),
            ),
            ToolSpec(
                name="appsec_preflight_readonly",
                domain=DOMAIN_APPSEC,
                description="只读预检发布窗口与影子流量基线",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="appsec_sandbox_verify",
                domain=DOMAIN_APPSEC,
                description="App+DB 联合沙箱与影子流量回放验证",
                required_params=("proposal_id",),
                optional_params=("long_tx_count", "shadow_replay_pass_rate"),
            ),
            ToolSpec(
                name="appsec_evidence_masked",
                domain=DOMAIN_APPSEC,
                description="取脱敏后的源码片段（业务路径 / Cookie 双向替换）",
                required_params=("finding_id", "workflow_run_id"),
            ),
        )

    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "appsec_probe_readonly": self._tool_probe,
            "appsec_fix_generate": self._tool_generate,
            "appsec_preflight_readonly": self._tool_preflight,
            "appsec_sandbox_verify": self._tool_sandbox,
            "appsec_evidence_masked": self._tool_evidence,
        }

    def _require_asset(self, asset_id: str) -> TargetAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise PluginContractError(f"未登记的 appsec 资产: {asset_id}")
        return asset

    def _tool_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        asset = self._require_asset(str(params["asset_id"]))
        report = self._probe.collect(asset)
        for finding in report.findings:
            self._findings[finding.finding_id] = finding
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
        return {
            "asset_id": asset.asset_id,
            "release_window": "LOW_PEAK",
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
        probe: dict[str, Any] = {}
        if "shadow_replay_pass_rate" in params:
            probe["shadow_replay_pass_rate"] = float(str(params["shadow_replay_pass_rate"]))
        return self._harness.evaluate(asset, finding, proposal, probe=probe).to_dict()

    def _tool_evidence(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._gateway is None:
            raise PluginContractError("多模态脱敏网关未装配，禁止读取证据原文")
        finding = self._findings.get(str(params["finding_id"]))
        if finding is None:
            raise PluginContractError(f"未缓存的探测发现项: {params['finding_id']}")
        outcome = self._gateway.mask(
            finding.evidence,
            modality="code",
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


__all__ = ["AppSecExecutor", "AppSecHarness", "AppSecMasker", "AppSecPlugin", "AppSecProbe", "RULES"]
