"""V4.1 Track A / STEP-03：``aegisforge-gate`` 统一 CI/CD 安全门禁。

一条命令串起四段能力（全部复用既有模块，不新建检测规则）：

``RepoScanner`` → ``MultimodalPrivacyGateway`` → ``SecOpsOrchestrator`` → ``CrossStackSarifReporter``

1. **扫描**：``core.scanners.repo_scanner`` 把本地仓库喂给 AppSec/K8s 探针；
2. **脱敏**：证据原文先过 :class:`MultimodalPrivacyGateway`（Fernet 信封加密持久化），
   SARIF / Markdown / Patch 三份产物都不含任何未脱敏凭据原文；
3. **编排**（可选 ``--run-agents``）：交给 :class:`SecOpsOrchestrator` 做多 Agent
   修复与 Critic 自愈，默认关闭以保证 CI 确定性；
4. **报告**：SARIF v2.1.0 + 适合 PR Comment 的 Markdown 摘要 + 可选聚合补丁。

退出码约定：``0`` 通过、``2`` 门禁阻断、``1`` 参数/目录异常。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from core.plugins.appsec.plugin import AppSecPlugin
from core.plugins.contracts import DOMAIN_APPSEC, DOMAIN_K8S, ProbeFinding, RemediationProposal, TargetAsset
from core.plugins.k8s.plugin import K8sPlugin
from core.plugins.registry import DomainPluginRegistry
from core.plugins.sdk import CrossStackSarifReporter
from core.privacy_gateway.multimodal import MODALITY_CODE, MODALITY_TEXT, MODALITY_YAML, MultimodalPrivacyGateway
from core.scanners.repo_scanner import (
    RepoScanConfig,
    RepoScanResult,
    priority_from_cvss,
    scan_repository,
)

TOOL_VERSION = "4.1.0"

PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
FAIL_ON_CHOICES: tuple[str, ...] = ("P0", "P1", "P2", "P3", "none")

EXIT_PASS = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

#: 硬编码凭据类发现的补丁不在产物中携带原文（``INV-FAIL-CLOSED-PRIVACY``）。
_SECRET_CATEGORIES: frozenset[str] = frozenset({"HARDCODED_SECRET"})
#: ``AppSecProbe`` 为硬编码凭据分配的规则编号（来自插件 RULES 表，不在此重复定义规则）。
_APPSEC_SECRET_RULE_ID = "APP-SEC-01"

_DOMAIN_MODALITY: dict[str, str] = {
    DOMAIN_APPSEC: MODALITY_CODE,
    DOMAIN_K8S: MODALITY_YAML,
}


class GateArgumentError(ValueError):
    """参数或目录异常（退出码 1）。"""


class GatePrivacyError(RuntimeError):
    """产物中检出未脱敏原文——立即 fail closed。"""


@dataclass(frozen=True)
class GateOutcome:
    """一次门禁执行的完整结果（可序列化为 CI 日志）。"""

    exit_code: int
    verdict: str
    blocked: bool
    highest_priority: str
    fail_on: str
    finding_count: int
    counts: dict[str, int] = field(default_factory=dict)
    sarif_path: str = ""
    patch_path: str = ""
    markdown_path: str = ""
    sandbox_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    skipped_files: tuple[str, ...] = ()
    masked_evidence_count: int = 0
    scan: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.exit_code == EXIT_PASS else ("blocked" if self.blocked else "error"),
            "exit_code": self.exit_code,
            "verdict": self.verdict,
            "blocked": self.blocked,
            "highest_priority": self.highest_priority,
            "fail_on": self.fail_on,
            "finding_count": self.finding_count,
            "by_priority": dict(self.counts),
            "sarif_path": self.sarif_path,
            "patch_path": self.patch_path,
            "markdown_summary_path": self.markdown_path,
            "sandbox_failures": list(self.sandbox_failures),
            "masked_evidence_count": self.masked_evidence_count,
            "warnings": list(self.warnings),
            "skipped_files": list(self.skipped_files),
            "scan": dict(self.scan),
        }


# ---------------------------------------------------------------------------
# 内部：脱敏与产物生成
# ---------------------------------------------------------------------------
def _modality_for(finding: ProbeFinding) -> str:
    if finding.location.startswith("pkg://"):
        return MODALITY_TEXT
    return _DOMAIN_MODALITY.get(finding.domain, MODALITY_TEXT)


def _mask_evidence(
    gateway: MultimodalPrivacyGateway,
    findings: Sequence[ProbeFinding],
    *,
    workflow_run_id: str,
) -> tuple[dict[str, str], list[str]]:
    """把每条发现的证据原文替换为脱敏文本；返回 ``(finding_id -> masked, warnings)``。

    首选按域模态处理；该模态未装配或载荷结构不合法时**降级到文本模态**再脱敏，
    绝不允许"处理不了就原样外发"。
    """
    masked: dict[str, str] = {}
    warnings: list[str] = []
    for finding in findings:
        if not finding.evidence:
            continue
        preferred = _modality_for(finding)
        outcome = None
        last_error: Exception | None = None
        for modality in (preferred, MODALITY_TEXT):
            try:
                outcome = gateway.mask(
                    finding.evidence,
                    modality=modality,
                    workflow_run_id=workflow_run_id,
                    ttl_seconds=3600,
                )
                break
            except Exception as exc:  # noqa: BLE001 - 逐模态降级，最终仍失败才 fail closed
                last_error = exc
                continue
        if outcome is None:
            raise GatePrivacyError(
                f"证据脱敏失败（{finding.finding_id}）：{type(last_error).__name__}"
            ) from last_error
        masked_text = outcome.masked_text
        if _looks_sensitive(finding):
            # 策略覆盖：凭据类证据一律以固定占位符对外。即使模态处理器未命中具体值，
            # 也不允许原文进入任何产物（``INV-FAIL-CLOSED-PRIVACY``）。
            if masked_text == finding.evidence:
                warnings.append(f"{finding.finding_id} 模态处理器未命中，已按策略替换为固定占位")
            masked_text = f"[REDACTED_SECRET] rule={finding.rule_id} location={finding.location}"
        masked[finding.finding_id] = masked_text
    return masked, warnings


def _looks_sensitive(finding: ProbeFinding) -> bool:
    """凭据类发现判定：直接读插件给出的分类/规则编号，不另写一份检测正则。"""
    return finding.category in _SECRET_CATEGORIES or finding.rule_id == _APPSEC_SECRET_RULE_ID


def _assert_no_plaintext(text: str, findings: Sequence[ProbeFinding]) -> None:
    """产物落盘前的最后一道闸：任何一条证据原文出现即 fail closed。"""
    for finding in findings:
        evidence = finding.evidence.strip()
        if len(evidence) < 8:
            continue
        if evidence in text:
            raise GatePrivacyError(f"产物包含未脱敏证据原文：{finding.finding_id}")


def _build_patch_text(
    proposals: Sequence[RemediationProposal],
    *,
    inventory_paths: dict[str, str],
) -> str:
    """聚合 unified diff；硬编码凭据类补丁只保留说明，不带出密钥原文。"""
    header = [
        "# AegisForge aggregate remediation patch (generated, review before apply)",
        "# 仅建议：未通过 HITL 审批前不得直接 apply 到工作区。",
        "",
    ]
    body: list[str] = []
    for proposal in proposals:
        path = inventory_paths.get(proposal.finding_id, "")
        if not proposal.standard_fix.startswith("--- "):
            # K8s 清单修复不是 unified diff，不混入 patch 产物（避免 PR 里误 apply）
            continue
        if _is_secret_proposal(proposal):
            body.append(
                f"# {path or proposal.finding_id}: 硬编码凭据修复 diff 已省略原文（脱敏策略）\n"
                f"# 建议动作：{proposal.mitigation}\n"
            )
            continue
        body.append(proposal.standard_fix.rstrip("\n") + "\n")
    return "\n".join(header) + "\n".join(body)


def _is_secret_proposal(proposal: RemediationProposal) -> bool:
    for ref in proposal.code_refs:
        rule_id = str(ref.get("rule_id", ""))
        if rule_id == "APP-SEC-01":
            return True
    return any("HARDCODED_SECRET" in str(note) for note in proposal.notes) or any(
        category in proposal.finding_id for category in _SECRET_CATEGORIES
    )


def _markdown_summary(
    *,
    repo_root: Path,
    fail_on: str,
    result: RepoScanResult,
    verdict: str,
    blocked: bool,
    proposals: int,
    sandbox_failures: Sequence[str],
    warnings: Sequence[str],
    run_agents: bool,
) -> str:
    counts = result.count_by_priority()
    lines = [
        "# AegisForge CI 安全门禁报告",
        "",
        f"- 仓库：`{repo_root}`",
        f"- 阈值：`--fail-on {fail_on}`（最高命中 `{result.highest_priority}`）",
        f"- 结论：**{verdict}**{'（门禁阻断）' if blocked else '（放行）'}",
        f"- 发现：{len(result.findings)} findings"
        f"（P0={counts['P0']}, P1={counts['P1']}, P2={counts['P2']}, P3={counts['P3']}）",
        f"- 扫描文件：{result.scanned_files}；跳过：{len(result.inventory.skipped_files)}"
        f"；修复建议：{proposals} 条；多 Agent 编排：{'已启用' if run_agents else '未启用'}",
        "",
    ]
    if result.findings:
        lines += [
            "| 优先级 | 规则 | 位置 | 标题 |",
            "| --- | --- | --- | --- |",
        ]
        for finding in result.findings:
            lines.append(
                f"| {priority_from_cvss(finding.base_cvss)} | `{finding.rule_id}` | "
                f"`{finding.location or '-'}` | {finding.title} |"
            )
    else:
        lines += ["0 findings：未发现 P0~P3 风险项。", ""]
    if sandbox_failures:
        lines += ["", "## 沙箱验证未通过", ""]
        lines += [f"- {item}" for item in sandbox_failures]
    if warnings:
        lines += ["", "## 扫描告警", ""]
        lines += [f"- {item}" for item in warnings]
    lines += ["", "> 证据原文不经任何产物外发；SARIF 与本摘要仅含规则、位置与优先级。", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 编排（可选）
# ---------------------------------------------------------------------------
def _run_agent_orchestration(
    registry: DomainPluginRegistry,
    assets: Sequence[TargetAsset],
    *,
    repo_root: Path,
    workflow_run_id: str,
    with_patch_verifier: bool,
    max_resume: int = 8,
) -> dict[str, Any]:
    from core.orchestrator.secops_graph import SecOpsOrchestrator

    verifier = None
    if with_patch_verifier:
        from core.plugins.appsec.ci_harness import verify_patch_in_isolated_workdir

        verifier = verify_patch_in_isolated_workdir

    orch = SecOpsOrchestrator(
        plugins=registry,
        repo_root=repo_root,
        patch_verifier=verifier,
    )
    run = orch.run(
        tenant_id="ci-gate",
        assets=list(assets),
        workflow_run_id=workflow_run_id,
        source_trigger="CI_PR_GATE",
    )
    rounds = 0
    while run.status == "SUSPENDED" and rounds < max_resume:
        run = orch.resume(workflow_run_id, decision="APPROVED")
        rounds += 1
    verdicts = list(run.state.get("critic_verdicts", {}).values())
    return {
        "status": run.status,
        "resume_rounds": rounds,
        "tickets": len(run.state.get("triage_tickets", [])),
        "verdicts": len(verdicts),
        "rejected": sum(1 for item in verdicts if item.get("verdict") == "REJECTED"),
        "executed": sum(
            1 for item in run.state.get("execution_observations", []) if item.get("executed")
        ),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_gate(
    *,
    repo_root: Path,
    fail_on: str = "P0",
    changed_only: bool = False,
    base_ref: str | None = None,
    sarif_out: Path | None = None,
    patch_out: Path | None = None,
    markdown_summary: Path | None = None,
    run_sandbox_tests: bool = False,
    test_cmd: str | None = None,
    run_agents: bool = False,
) -> GateOutcome:
    """执行一次门禁；返回 :class:`GateOutcome`（含退出码）。"""
    threshold = (fail_on or "P0").strip()
    if threshold.lower() not in {choice.lower() for choice in FAIL_ON_CHOICES}:
        raise GateArgumentError(f"--fail-on 只能是 {'/'.join(FAIL_ON_CHOICES)}")
    threshold = "none" if threshold.lower() == "none" else threshold.upper()

    root = Path(repo_root).expanduser()
    if not root.is_dir():
        raise GateArgumentError(f"--repo 指向的目录不存在: {root}")
    root = root.resolve()

    gateway = MultimodalPrivacyGateway()
    appsec = AppSecPlugin(gateway=gateway)
    k8s = K8sPlugin(gateway=gateway)
    registry = DomainPluginRegistry().register(appsec).register(k8s)

    config = RepoScanConfig(
        repo_root=root,
        changed_only=changed_only,
        base_ref=base_ref,
    )
    result = scan_repository(config, plugins=registry)
    workflow_run_id = "gate-" + hashlib.sha256(
        f"{root}:{result.asset_id}:{len(result.findings)}".encode("utf-8")
    ).hexdigest()[:12]

    masked, mask_warnings = _mask_evidence(gateway, result.findings, workflow_run_id=workflow_run_id)

    # -- 修复建议（SARIF 的 crossStack 属性与 patch-out 共用同一份提案） ------
    proposals: list[RemediationProposal] = []
    inventory_paths: dict[str, str] = {}
    app_asset = TargetAsset(asset_id=result.asset_id, domain=DOMAIN_APPSEC, engine="python")
    k8s_asset = TargetAsset(asset_id=f"{result.asset_id}-k8s", domain=DOMAIN_K8S, engine="kubernetes")
    for finding in result.findings:
        try:
            if finding.domain == DOMAIN_APPSEC:
                proposal = appsec.build_proposal(finding, app_asset)
            elif finding.domain == DOMAIN_K8S:
                proposal = k8s.build_proposal(finding, k8s_asset)
            else:  # pragma: no cover - 当前扫描器只产出 appsec / k8s
                continue
        except Exception:  # noqa: BLE001 - 单条提案失败不得中断整份门禁报告
            continue
        proposals.append(proposal)
        inventory_paths[finding.finding_id] = finding.location.split("#")[0].replace("file://", "")

    # -- 沙箱验证（可选） --------------------------------------------------
    sandbox_failures: list[str] = []
    sandbox_notes: list[str] = []
    if run_sandbox_tests:
        from core.plugins.appsec.ci_harness import run_patch_sandbox

        command = _split_command(test_cmd) if test_cmd else None
        for proposal in proposals:
            if not proposal.standard_fix.startswith("--- "):
                continue
            outcome = run_patch_sandbox(
                root,
                proposal.standard_fix,
                proposal.rollback.payload,
                test_command=command,
                timeout_seconds=60,
            )
            if not (outcome.patch_applied and outcome.compile_ok and outcome.rollback_verified):
                sandbox_failures.append(f"{proposal.proposal_id}: {'/'.join(outcome.failures) or 'UNKNOWN'}")
            sandbox_notes.append(
                f"{proposal.proposal_id}: applied={outcome.patch_applied} compile={outcome.compile_ok} "
                f"tests={outcome.tests_passed} rollback={outcome.rollback_verified}"
            )

    # -- 多 Agent 编排（可选） --------------------------------------------
    orchestration: dict[str, Any] = {}
    if run_agents:
        orchestration = _run_agent_orchestration(
            registry,
            [app_asset, k8s_asset] if result.inventory.k8s_manifests else [app_asset],
            repo_root=root,
            workflow_run_id=workflow_run_id,
            with_patch_verifier=run_sandbox_tests,
        )

    highest = result.highest_priority
    blocked = bool(result.findings) and threshold != "none" and PRIORITY_RANK.get(
        highest, 99
    ) <= PRIORITY_RANK.get(threshold, -1)
    if sandbox_failures:
        blocked = True
    exit_code = EXIT_BLOCKED if blocked else EXIT_PASS
    verdict = "BLOCKED" if blocked else "PASS"

    # -- 产物 --------------------------------------------------------------
    sarif_path = str(sarif_out) if sarif_out else ""
    if sarif_out is not None:
        reporter = CrossStackSarifReporter(tool_version=TOOL_VERSION, tool_name="AegisForge CI Gate")
        payload = reporter.build(
            findings=result.findings,
            proposals={item.finding_id: item for item in proposals},
            run_properties={
                "workflowRunId": workflow_run_id,
                "repoRoot": str(root),
                "failOn": threshold,
                "verdict": verdict,
                "highestPriority": highest,
                "maskedEvidenceCount": len(masked),
                "sandboxFailures": sandbox_failures,
            },
        )
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        _assert_no_plaintext(serialized, result.findings)
        target = Path(sarif_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialized, encoding="utf-8")

    patch_path = ""
    if patch_out is not None:
        patch_text = _build_patch_text(proposals, inventory_paths=inventory_paths)
        _assert_no_plaintext(patch_text, result.findings)
        target = Path(patch_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch_text, encoding="utf-8")
        patch_path = str(target)

    markdown_path = ""
    if markdown_summary is not None:
        text = _markdown_summary(
            repo_root=root,
            fail_on=threshold,
            result=result,
            verdict=verdict,
            blocked=blocked,
            proposals=len(proposals),
            sandbox_failures=sandbox_failures,
            warnings=list(result.warnings) + mask_warnings,
            run_agents=run_agents,
        )
        _assert_no_plaintext(text, result.findings)
        target = Path(markdown_summary)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        markdown_path = str(target)

    gateway.close(workflow_run_id, reason="GATE_COMPLETED")

    scan_summary = result.to_summary()
    scan_summary["sandbox"] = sandbox_notes
    scan_summary["orchestration"] = orchestration
    return GateOutcome(
        exit_code=exit_code,
        verdict=verdict,
        blocked=blocked,
        highest_priority=highest,
        fail_on=threshold,
        finding_count=len(result.findings),
        counts=result.count_by_priority(),
        sarif_path=sarif_path,
        patch_path=patch_path,
        markdown_path=markdown_path,
        sandbox_failures=tuple(sandbox_failures),
        warnings=tuple(result.warnings) + tuple(mask_warnings),
        skipped_files=tuple(result.inventory.skipped_files),
        masked_evidence_count=len(masked),
        scan=scan_summary,
    )


def _split_command(raw: str) -> list[str]:
    import shlex

    return shlex.split(raw, posix=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aegisforge-gate",
        description="AegisForge 本地仓库 CI/CD 安全门禁（扫描 -> 脱敏 -> 编排 -> SARIF）",
    )
    parser.add_argument("--repo", default=".", help="目标仓库根目录（默认当前目录）")
    parser.add_argument("--changed-only", action="store_true", help="仅扫描 Git 增量变更文件")
    parser.add_argument("--base-ref", default=None, help="增量对比基线（如 origin/main），缺省对比 HEAD")
    parser.add_argument(
        "--fail-on",
        default="P0",
        choices=list(FAIL_ON_CHOICES),
        help="阻断阈值：命中或高于该等级即退出码 2（默认 P0）",
    )
    parser.add_argument("--sarif-out", default=None, help="输出 SARIF v2.1.0 文件路径")
    parser.add_argument("--patch-out", default=None, help="输出聚合 unified diff 建议路径")
    parser.add_argument("--markdown-summary", default=None, help="输出 PR Comment 用 Markdown 摘要路径")
    parser.add_argument("--run-sandbox-tests", action="store_true", help="在隔离目录执行 py_compile / 单测验证")
    parser.add_argument("--test-cmd", default=None, help="沙箱内执行的可选测试命令（如 \"pytest -q\"）")
    parser.add_argument(
        "--run-agents",
        action="store_true",
        help="启用 SecOpsOrchestrator 多 Agent 修复与 Critic 自愈（默认关闭以保证 CI 确定性）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 错误默认 exit(2)，门禁约定参数异常统一返回 1
        return EXIT_PASS if exc.code in (0, None) else EXIT_ERROR
    try:
        outcome = run_gate(
            repo_root=Path(args.repo),
            fail_on=args.fail_on,
            changed_only=args.changed_only,
            base_ref=args.base_ref,
            sarif_out=Path(args.sarif_out) if args.sarif_out else None,
            patch_out=Path(args.patch_out) if args.patch_out else None,
            markdown_summary=Path(args.markdown_summary) if args.markdown_summary else None,
            run_sandbox_tests=args.run_sandbox_tests,
            test_cmd=args.test_cmd,
            run_agents=args.run_agents,
        )
    except (GateArgumentError, GatePrivacyError) as exc:
        sys.stderr.write(f"aegisforge-gate: {exc}\n")
        return EXIT_ERROR
    except OSError as exc:
        sys.stderr.write(f"aegisforge-gate: 文件系统错误 {type(exc).__name__}: {exc}\n")
        return EXIT_ERROR

    payload = outcome.to_dict()
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(
            f"aegisforge-gate: {outcome.verdict} findings={outcome.finding_count} "
            f"highest={outcome.highest_priority} fail_on={outcome.fail_on}\n"
        )
        if outcome.sarif_path:
            sys.stdout.write(f"  SARIF     : {outcome.sarif_path}\n")
        if outcome.patch_path:
            sys.stdout.write(f"  Patch     : {outcome.patch_path}\n")
        if outcome.markdown_path:
            sys.stdout.write(f"  Markdown  : {outcome.markdown_path}\n")
    return outcome.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
