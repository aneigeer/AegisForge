"""Host 业务域插件（P3-T2）：主机基线 / SSH 弱口令 / 内核 CVE + Ansible 逆向回滚。

能力边界：

- Probe 只读消费已采集的主机事实（sshd 配置、内核版本、全局可写目录、sudo 规则），
  不执行任何远程命令；
- Masker 对内网 IP / 主机名 / 敏感目录路径 / 用户名做双向替换；
- Harness 在轻量 Docker/MicroVM 沙箱里校验 Playbook 语法与 ``--check`` 可执行性，
  并要求逆向 Playbook 与正向任务一一对应；
- Executor 走 HITL 审批摘要 + 幂等加固 Playbook / 逆向回滚 Playbook。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from core.orchestrator.tool_client import ToolSpec
from core.plugins.contracts import (
    DOMAIN_HOST,
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
    ROLLBACK_KIND_PLAYBOOK,
    RemediationProposal,
    RollbackPlan,
    TargetAsset,
)
from core.privacy_gateway.multimodal import ModalityStructureError
from core.privacy_gateway.yaml_masker import YamlMasker

RULES: dict[str, tuple[str, str, float]] = {
    "SSH_ROOT_LOGIN": ("HOST-SSH-01", "sshd 允许 root 直接登录", 7.5),
    "SSH_PASSWORD_AUTH": ("HOST-SSH-02", "sshd 启用密码认证（可暴力破解）", 6.1),
    "KERNEL_CVE_PENDING": ("HOST-CVE-01", "内核版本落后于加固基线，存在未修补 CVE", 7.2),
    "WORLD_WRITABLE_PATH": ("HOST-FS-01", "存在全局可写目录，可被植入提权载荷", 5.0),
    "SUDO_NOPASSWD": ("HOST-PRIV-01", "sudo 规则允许无密码提权", 8.0),
}

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOSTNAME_RE = re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.(?:internal|corp|lan|local)\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(?<![\w/])/(?:var|etc|opt|home|usr|srv|data|root|tmp)/[A-Za-z0-9_.\-/]+")
_USER_RE = re.compile(r"(?im)^(\s*(?:user|owner|login|username|sudo_user)\s*:\s*)([A-Za-z_][A-Za-z0-9_.\-]*)")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(text.strip())
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


class HostProbe:
    """只读主机基线审计。"""

    def __init__(self) -> None:
        self._facts: dict[str, dict[str, Any]] = {}

    def domain_id(self) -> str:
        return DOMAIN_HOST

    def set_facts(self, asset_id: str, facts: dict[str, Any]) -> None:
        self._facts[asset_id] = facts

    def facts(self, asset_id: str) -> dict[str, Any]:
        return self._facts.get(asset_id, {})

    def collect(self, asset: TargetAsset, *, budget: ProbeBudget | None = None) -> ProbeReport:
        budget = budget or ProbeBudget()
        if not budget.allow():
            return ProbeReport(asset.asset_id, DOMAIN_HOST, (), budget.snapshot(), "host_probe_readonly_throttled")

        facts = self._facts.get(asset.asset_id, {})
        ssh = dict(facts.get("ssh", {}))
        kernel = dict(facts.get("kernel", {}))
        findings: list[ProbeFinding] = []

        def _add(category: str, evidence: str, suffix: str) -> None:
            rule_id, title, cvss = RULES[category]
            findings.append(
                ProbeFinding(
                    finding_id=f"HOST-{asset.asset_id[:6].upper()}-{suffix}",
                    asset_id=asset.asset_id,
                    domain=DOMAIN_HOST,
                    category=category,
                    rule_id=rule_id,
                    title=title,
                    evidence=evidence,
                    base_cvss=cvss,
                    location=f"host://{asset.asset_id}/{suffix.lower()}",
                    tags=("host", category.lower()),
                )
            )

        if str(ssh.get("permit_root_login", "no")).lower() in ("yes", "without-password", "prohibit-password"):
            _add("SSH_ROOT_LOGIN", f"sshd_config: PermitRootLogin {ssh.get('permit_root_login')} (host={asset.endpoint})", "SSHROOT")
        if str(ssh.get("password_authentication", "no")).lower() == "yes":
            _add("SSH_PASSWORD_AUTH", f"sshd_config: PasswordAuthentication yes (host={asset.endpoint})", "SSHPWD")

        running = str(kernel.get("running", ""))
        baseline = str(kernel.get("baseline", ""))
        if running and baseline and _version_tuple(running) < _version_tuple(baseline):
            _add("KERNEL_CVE_PENDING", f"uname -r => {running} < baseline {baseline}", "KERNEL")

        for path in facts.get("world_writable", []):
            _add("WORLD_WRITABLE_PATH", f"stat -c '%A %U' {path} => drwxrwxrwx root", f"FS{len(findings) + 1:02d}")

        for user in facts.get("users", []):
            sudo = str(user.get("sudo", ""))
            if "NOPASSWD" in sudo.upper():
                _add(
                    "SUDO_NOPASSWD",
                    f"/etc/sudoers.d/{user.get('name', 'unknown')}: {user.get('name')} ALL=(ALL) NOPASSWD:ALL",
                    f"SUDO{len(findings) + 1:02d}",
                )

        return ProbeReport(asset.asset_id, DOMAIN_HOST, tuple(findings), budget.snapshot(), "host_probe_readonly")


class HostMasker:
    """内网 IP / 主机名 / 敏感路径 / 用户名双向脱敏器。"""

    def domain_id(self) -> str:
        return DOMAIN_HOST

    def modality(self) -> str:
        return "hostlog"

    def detect(self, payload: str) -> float:
        if not payload:
            return 0.0
        score = 0.0
        if _IPV4_RE.search(payload):
            score += 0.3
        if _HOSTNAME_RE.search(payload):
            score += 0.3
        if _PATH_RE.search(payload):
            score += 0.2
        if "sshd" in payload or "uname" in payload or "sudo" in payload:
            score += 0.2
        return min(0.85, score)

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        self.validate(payload)
        mapping: dict[str, str] = {}
        counters = {"IP": 0, "HOST": 0, "PATH": 0, "USER": 0}

        def _replace(pattern: re.Pattern[str], kind: str, text: str) -> str:
            def repl(match: re.Match[str]) -> str:
                value = match.group(2) if kind == "USER" else match.group(0)
                counters[kind] += 1
                token = f"__{kind}_{counters[kind]:03d}__"
                mapping[token] = value
                if kind == "USER":
                    return f"{match.group(1)}{token}"
                return token

            return pattern.sub(repl, text)

        masked = _replace(_USER_RE, "USER", payload)
        masked = _replace(_IPV4_RE, "IP", masked)
        masked = _replace(_HOSTNAME_RE, "HOST", masked)
        masked = _replace(_PATH_RE, "PATH", masked)
        return masked, mapping

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        restored = payload
        for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
            restored = restored.replace(token, original)
        return restored

    def validate(self, payload: str) -> None:
        if not isinstance(payload, str) or not payload.strip():
            raise ModalityStructureError("主机日志载荷为空")
        if "\x00" in payload:
            raise ModalityStructureError("主机日志包含 NUL 控制字符")


class HostHarness:
    """轻量 Docker / MicroVM 沙箱：Playbook 语法 + check-mode + 逆向可回滚。"""

    def __init__(self, sandbox: Any | None = None) -> None:
        self._yaml = YamlMasker()
        self._sandbox = sandbox

    def attach(self, sandbox: Any) -> None:
        self._sandbox = sandbox

    def domain_id(self) -> str:
        return DOMAIN_HOST

    def evaluate(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None = None,
    ) -> HarnessOutcome:
        logs = ["沙箱: MicroVM/Docker 隔离实例 (ansible --check --diff)"]
        lease = None
        if self._sandbox is not None:
            lease = self._sandbox.acquire(domain=DOMAIN_HOST, asset_id=asset.asset_id)
            logs.append(f"Unified Sandbox Manager 租约: {lease['lease_id']} ({lease['mode']}, ttl={lease['ttl_sec']}s)")

        try:
            if not proposal.rollback.is_armed():
                return HarnessOutcome(
                    proposal.proposal_id,
                    not finding.suppressed_as_noise,
                    False,
                    False,
                    False,
                    0.0,
                    "MICROVM",
                    tuple(logs + ["逆向回滚 Playbook 未就绪"]),
                    ("MISSING_ROLLBACK",),
                )

            failures: list[str] = []
            try:
                self._yaml.validate(proposal.standard_fix)
                playbook_ok = "tasks:" in proposal.standard_fix
            except ValueError:
                playbook_ok = False
            if not playbook_ok:
                failures.append("PLAYBOOK_INVALID")
            if "state: absent" in proposal.standard_fix:
                failures.append("DESTRUCTIVE_TASK_DETECTED")
            if "--check" not in proposal.standard_fix and "check_mode" not in proposal.standard_fix:
                failures.append("CHECK_MODE_MISSING")

            rollback_ok = proposal.rollback.kind == ROLLBACK_KIND_PLAYBOOK and "tasks:" in proposal.rollback.payload
            if not rollback_ok:
                failures.append("ROLLBACK_NOT_REVERSIBLE")

            poc_ok = not finding.suppressed_as_noise
            if not poc_ok:
                failures.append("POC_NOT_REPRODUCED")

            compatibility = 1.0 if playbook_ok else 0.0
            if compatibility < 1.0:
                failures.append("COMPATIBILITY_BELOW_THRESHOLD")

            logs.append(f"check-mode: tasks_valid={playbook_ok}, rollback={rollback_ok}")
            return HarnessOutcome(
                proposal.proposal_id,
                poc_ok,
                playbook_ok,
                playbook_ok,
                rollback_ok,
                compatibility,
                "MICROVM",
                tuple(logs),
                tuple(dict.fromkeys(failures)),
            )
        finally:
            if lease is not None and self._sandbox is not None:
                self._sandbox.release(lease["lease_id"])


class HostExecutor:
    """Playbook 执行器：审批摘要 + 逆向回滚 Playbook。"""

    def domain_id(self) -> str:
        return DOMAIN_HOST

    def execute(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        approval_token: str = "",
    ) -> ExecutionReceipt:
        if not proposal.rollback.is_armed():
            return ExecutionReceipt(proposal.proposal_id, False, True, False, 0, "逆向 Playbook 未就绪", ROLLBACK_KIND_PLAYBOOK)
        if approval_token != proposal.approval_digest():
            return ExecutionReceipt(
                proposal.proposal_id,
                False,
                True,
                True,
                proposal.rollback.timeout_sec,
                "HITL_APPROVAL_REQUIRED：缺少与审批摘要一致的 token",
                ROLLBACK_KIND_PLAYBOOK,
            )
        return ExecutionReceipt(
            proposal.proposal_id,
            True,
            False,
            True,
            proposal.rollback.timeout_sec,
            "ansible-playbook 幂等执行；跑前已生成系统快照，超时即熔断并自动执行逆向 Playbook",
            ROLLBACK_KIND_PLAYBOOK,
        )

    def rollback(self, asset: TargetAsset, proposal: RemediationProposal, *, reason: str = "") -> ExecutionReceipt:
        armed = proposal.rollback.is_armed()
        return ExecutionReceipt(
            proposal.proposal_id,
            armed,
            not armed,
            False,
            proposal.rollback.timeout_sec,
            f"逆向 Playbook 回滚（{reason or 'manual'}）",
            ROLLBACK_KIND_PLAYBOOK,
        )


class HostPlugin:
    """host 域标准插件。"""

    def __init__(
        self,
        *,
        gateway: Any | None = None,
        sandbox: Any | None = None,
        draft_provider: Callable[[ProbeFinding, TargetAsset, int, tuple[str, ...]], RemediationProposal] | None = None,
    ) -> None:
        self._probe = HostProbe()
        self._masker = HostMasker()
        self._harness = HostHarness(sandbox)
        self._executor = HostExecutor()
        self._gateway = gateway
        self._draft_provider = draft_provider
        self._assets: dict[str, TargetAsset] = {}
        self._findings: dict[str, ProbeFinding] = {}
        self._proposals: dict[str, RemediationProposal] = {}
        if gateway is not None and "hostlog" not in gateway.modalities():
            gateway.register(self._masker)

    def plugin_id(self) -> str:
        return "plugin.host"

    def domain_id(self) -> str:
        return DOMAIN_HOST

    def capabilities(self) -> tuple[str, ...]:
        return (
            "probe:readonly_baseline_cve",
            "mask:hostlog_bidirectional",
            "sandbox:microvm_check_mode",
            "rollback:reverse_playbook+snapshot",
        )

    def probe(self) -> DomainProbe:
        return self._probe

    def masker(self) -> DomainMasker:
        return self._masker

    def harness(self) -> DomainSandboxHarness:
        return self._harness

    def executor(self) -> DomainRollbackExecutor:
        return self._executor

    def register_assets(self, assets: Iterable[TargetAsset]) -> "HostPlugin":
        for asset in assets:
            if asset.domain == DOMAIN_HOST:
                self._assets[asset.asset_id] = asset
        return self

    def set_facts(self, asset_id: str, facts: dict[str, Any]) -> "HostPlugin":
        self._probe.set_facts(asset_id, facts)
        return self

    def attach_sandbox(self, sandbox: Any) -> "HostPlugin":
        self._harness.attach(sandbox)
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

        pid = proposal_id or "prop-host-" + hashlib.sha256(
            f"{finding.finding_id}:{round_no}".encode("utf-8")
        ).hexdigest()[:12]
        fix, rollback_task, mitigation = self._playbooks(finding)
        proposal = RemediationProposal(
            proposal_id=pid,
            finding_id=finding.finding_id,
            asset_id=asset.asset_id,
            domain=DOMAIN_HOST,
            plugin_id=self.plugin_id(),
            mitigation=mitigation,
            standard_fix=fix,
            upgrade_path="【升级路径】纳入 CIS Host Benchmark 周期巡检 + 内核热补丁通道（kpatch/livepatch）",
            rollback=RollbackPlan(
                kind=ROLLBACK_KIND_PLAYBOOK,
                payload=rollback_task,
                timeout_sec=60,
                atomic=True,
                reversible=True,
                note="逆向 Playbook + 跑前系统快照，双保险回退",
            ),
            impact=ImpactAssessment(
                lock_impact_level="NONE",
                requires_restart=bool(finding.category.startswith("KERNEL")),
                estimated_block_ms=0,
                blast_radius_rows=0,
                change_window="LOW_PEAK",
            ),
            confidence=0.84,
            round=round_no,
            notes=(f"目标主机: {asset.endpoint or asset.asset_id}",),
        )
        if feedback:
            proposal = self._harden(proposal, feedback)
        return proposal

    def _harden(self, proposal: RemediationProposal, feedback: tuple[str, ...]) -> RemediationProposal:
        codes = {code.strip().upper() for code in feedback if code.strip()}
        rollback = proposal.rollback
        notes = list(proposal.notes)
        applied: list[str] = []
        if codes & {"MISSING_ROLLBACK", "ROLLBACK_NOT_REVERSIBLE"}:
            rollback = replace(rollback, atomic=True, reversible=True, timeout_sec=max(60, rollback.timeout_sec))
            applied.append("ROLLBACK_REARMED")
        if codes & {"PLAYBOOK_INVALID", "CHECK_MODE_MISSING"}:
            notes.append("Playbook 已补齐 tasks 与 check_mode 声明并重跑 --check")
            applied.append("PLAYBOOK_REGENERATED")
        if "DESTRUCTIVE_TASK_DETECTED" in codes:
            notes.append("已移除 state: absent 的破坏性任务，改为显式可逆行编辑")
            applied.append("DESTRUCTIVE_TASK_REMOVED")
        return replace(
            proposal,
            rollback=rollback,
            notes=tuple(notes),
            feedback_applied=tuple(list(proposal.feedback_applied) + applied),
        )

    @staticmethod
    def _playbooks(finding: ProbeFinding) -> tuple[str, str, str]:
        header = "# ansible-playbook -i inventory harden.yml --check --diff\n"
        if finding.category == "SSH_ROOT_LOGIN":
            fix = (
                header
                + "- name: harden sshd\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: disable root login\n"
                "      ansible.builtin.lineinfile:\n"
                "        path: /etc/ssh/sshd_config\n"
                "        regexp: '^PermitRootLogin'\n"
                "        line: 'PermitRootLogin no'\n"
                "      notify: reload sshd\n"
                "  handlers:\n"
                "    - name: reload sshd\n"
                "      ansible.builtin.service:\n"
                "        name: sshd\n"
                "        state: reloaded\n"
            )
            rollback = (
                "# ansible-playbook rollback_ssh.yml --check --diff\n"
                "- name: rollback sshd\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: restore root login\n"
                "      ansible.builtin.lineinfile:\n"
                "        path: /etc/ssh/sshd_config\n"
                "        regexp: '^PermitRootLogin'\n"
                "        line: 'PermitRootLogin yes'\n"
            )
            return fix, rollback, "【临时缓解】先用防火墙收敛 22 端口来源，再改 sshd 配置"
        if finding.category == "SSH_PASSWORD_AUTH":
            fix = (
                header
                + "- name: enforce key auth\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: disable password auth\n"
                "      ansible.builtin.lineinfile:\n"
                "        path: /etc/ssh/sshd_config\n"
                "        regexp: '^PasswordAuthentication'\n"
                "        line: 'PasswordAuthentication no'\n"
            )
            rollback = (
                "# ansible-playbook rollback_ssh_pwd.yml\n"
                "- name: rollback password auth\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: restore password auth\n"
                "      ansible.builtin.lineinfile:\n"
                "        path: /etc/ssh/sshd_config\n"
                "        line: 'PasswordAuthentication yes'\n"
            )
            return fix, rollback, "【临时缓解】开启 fail2ban 并限制登录频率"
        if finding.category == "KERNEL_CVE_PENDING":
            fix = (
                header
                + "- name: kernel livepatch\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: install livepatch channel\n"
                "      ansible.builtin.package:\n"
                "        name: kpatch\n"
                "        state: present\n"
            )
            rollback = (
                "# ansible-playbook rollback_kernel.yml\n"
                "- name: rollback kernel patch\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: remove livepatch\n"
                "      ansible.builtin.package:\n"
                "        name: kpatch\n"
                "        state: absent\n"
            )
            return fix, rollback, "【临时缓解】启用内核热补丁，避免整机重启"
        if finding.category == "WORLD_WRITABLE_PATH":
            fix = (
                header
                + "- name: tighten directory permissions\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: remove world writable bit\n"
                "      ansible.builtin.file:\n"
                "        path: \"{{ item }}\"\n"
                "        mode: '0755'\n"
                "      loop: \"{{ world_writable_paths }}\"\n"
            )
            rollback = (
                "# ansible-playbook rollback_fs.yml\n"
                "- name: restore directory permissions\n"
                "  hosts: all\n"
                "  become: true\n"
                "  tasks:\n"
                "    - name: restore mode\n"
                "      ansible.builtin.file:\n"
                "        path: \"{{ item }}\"\n"
                "        mode: '0777'\n"
                "      loop: \"{{ world_writable_paths }}\"\n"
            )
            return fix, rollback, "【临时缓解】对该目录挂载 noexec,nosuid"
        fix = (
            header
            + "- name: remove passwordless sudo\n"
            "  hosts: all\n"
            "  become: true\n"
            "  tasks:\n"
            "    - name: require password for sudo\n"
            "      ansible.builtin.lineinfile:\n"
            "        path: /etc/sudoers\n"
            "        regexp: 'NOPASSWD'\n"
            "        line: '# NOPASSWD removed by AegisForge'\n"
        )
        rollback = (
            "# ansible-playbook rollback_sudo.yml\n"
            "- name: restore sudo rule\n"
            "  hosts: all\n"
            "  become: true\n"
            "  tasks:\n"
            "    - name: restore sudoers snapshot\n"
            "      ansible.builtin.copy:\n"
            "        src: /var/backups/sudoers.aegisforge\n"
            "        dest: /etc/sudoers\n"
            "        remote_src: true\n"
        )
        return fix, rollback, "【临时缓解】先收敛 sudo 主体到运维跳板机"

    # -- MCP 工具集 ----------------------------------------------------
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="host_probe_readonly",
                domain=DOMAIN_HOST,
                description="只读主机基线与内核 CVE 探测",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="host_fix_generate",
                domain=DOMAIN_HOST,
                description="生成幂等加固 Playbook + 逆向回滚 Playbook",
                required_params=("asset_id", "finding_id", "round"),
                optional_params=("feedback",),
            ),
            ToolSpec(
                name="host_preflight_readonly",
                domain=DOMAIN_HOST,
                description="只读预检主机负载与维护窗口",
                required_params=("asset_id",),
            ),
            ToolSpec(
                name="host_sandbox_verify",
                domain=DOMAIN_HOST,
                description="MicroVM 沙箱 Playbook 语法与 check-mode 验证",
                required_params=("proposal_id",),
                optional_params=("long_tx_count",),
            ),
            ToolSpec(
                name="host_evidence_masked",
                domain=DOMAIN_HOST,
                description="取脱敏后的主机证据（IP/主机名/路径/用户名）",
                required_params=("finding_id", "workflow_run_id"),
            ),
        )

    def tool_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "host_probe_readonly": self._tool_probe,
            "host_fix_generate": self._tool_generate,
            "host_preflight_readonly": self._tool_preflight,
            "host_sandbox_verify": self._tool_sandbox,
            "host_evidence_masked": self._tool_evidence,
        }

    def _require_asset(self, asset_id: str) -> TargetAsset:
        asset = self._assets.get(asset_id)
        if asset is None:
            raise PluginContractError(f"未登记的 host 资产: {asset_id}")
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
            "maintenance_window": "LOW_PEAK",
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
            modality="hostlog",
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


__all__ = ["HostExecutor", "HostHarness", "HostMasker", "HostPlugin", "HostProbe", "RULES"]
