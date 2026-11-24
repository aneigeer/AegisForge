"""V4.1 Track A 单元测试：本地仓库扫描、隔离沙箱、CI 门禁 CLI。

分三段，对应规范 STEP-01 / STEP-02 / STEP-03 的 ``-k`` 选择器：

- ``-k scanner``：文件分类路由、Git 增量、二进制/超限/排除目录跳过；
- ``-k harness``：临时目录 Patch 沙箱编译校验、回滚哈希验证、Critic 自愈；
- ``-k cli``：``apps.cli.secops_gate`` 的退出码、SARIF 合法性与脱敏。

所有夹具均在 ``tmp_path`` 内构造，不对本仓库做任何写操作。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.scanners.repo_scanner import (
    RepoScanConfig,
    collect_repo_inventory,
    priority_from_cvss,
    scan_repository,
)

# 故意构造漏洞的夹具源码（不含任何真实凭据，机密值仅为无意义占位串）。
VULN_SOURCE = '''import os


def get_order(cur, order_id):
    cur.execute("SELECT * FROM orders WHERE id = " + order_id)
    return cur.fetchone()


DB_PASSWORD = "fixture-pass-9931"
'''

SAFE_SOURCE = '''import os


def get_order(cur, order_id):
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    return cur.fetchone()
'''

PRIVILEGED_MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  template:
    spec:
      containers:
        - name: api
          image: api:1.0
          securityContext:
            privileged: true
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build_repo(root: Path) -> Path:
    """构造一个含四类风险 + 若干"应当被跳过"文件的夹具仓库。"""
    _write(root / "app" / "orders.py", VULN_SOURCE)
    _write(root / "app" / "safe.py", SAFE_SOURCE)
    _write(root / "requirements.txt", "requests==2.19.0\npython-dateutil>=2.8\n")
    _write(root / "deploy" / "api.yaml", PRIVILEGED_MANIFEST)
    # 应被排除的目录里也放一份漏洞文件，验证排除规则真实生效
    _write(root / ".venv" / "lib" / "vendored.py", VULN_SOURCE)
    _write(root / ".git" / "hooks" / "evil.py", VULN_SOURCE)
    _write(root / "node_modules" / "pkg" / "index.js", "const x = 1;\n")
    return root


# ---------------------------------------------------------------------------
# STEP-01：扫描器
# ---------------------------------------------------------------------------
def test_scanner_routes_files_into_inventory(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    inventory = collect_repo_inventory(RepoScanConfig(repo_root=root))

    assert set(inventory.sources) == {"app/orders.py", "app/safe.py"}
    assert "deploy/api.yaml" in inventory.k8s_manifests
    names = {dep["name"] for dep in inventory.dependencies}
    assert names == {"requests", "python-dateutil"}
    # 被排除目录内的文件不得出现在任何清单
    joined = json.dumps(inventory.summary(), ensure_ascii=False)
    assert ".venv" not in joined and "node_modules" not in joined
    assert inventory.asset_id.startswith("repo-")


def test_scanner_detects_four_risk_families(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    result = scan_repository(RepoScanConfig(repo_root=root))

    categories = {finding.category for finding in result.findings}
    assert {"SQL_INJECTION", "HARDCODED_SECRET", "SCA_VULN", "POD_PRIVILEGE_ESCAPE"} <= categories

    sql = next(item for item in result.findings if item.category == "SQL_INJECTION")
    assert sql.location == "file://app/orders.py#L5"
    sca = next(item for item in result.findings if item.category == "SCA_VULN")
    assert sca.cve_id == "CVE-2023-32681"
    assert sca.location == "pkg://requests@2.19.0"
    k8s = next(item for item in result.findings if item.category == "POD_PRIVILEGE_ESCAPE")
    assert k8s.location.startswith("k8s://")

    # 安全写法不得误报
    assert not any("safe.py" in item.location for item in result.findings)
    assert result.highest_priority == "P0"
    assert result.count_by_priority()["P0"] >= 1
    assert set(result.domains) == {"appsec", "k8s"}
    assert priority_from_cvss(7.4) == "P1" and priority_from_cvss(3.1) == "P3"


def test_scanner_changed_only_limits_to_git_diff(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline", env_author=True)
    base = _git(root, "rev-parse", "HEAD").strip()

    _write(root / "app" / "orders.py", VULN_SOURCE + '\ndef extra(cur, v):\n    cur.execute("DELETE FROM audit WHERE id = " + v)\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "touch orders", env_author=True)

    incremental = collect_repo_inventory(
        RepoScanConfig(repo_root=root, changed_only=True, base_ref=base)
    )
    assert set(incremental.sources) == {"app/orders.py"}
    assert "app/safe.py" not in incremental.sources

    result = scan_repository(RepoScanConfig(repo_root=root, changed_only=True, base_ref=base))
    assert all("orders.py" in item.location for item in result.findings if item.location.startswith("file://"))


def test_scanner_falls_back_loudly_outside_git(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    inventory = collect_repo_inventory(RepoScanConfig(repo_root=root, changed_only=True))

    # 非 git 目录：必须回退全量并在 warnings 里如实记录，不得静默吞错
    assert set(inventory.sources) == {"app/orders.py", "app/safe.py"}
    assert any("回退" in warning for warning in inventory.warnings)


def test_scanner_skips_binary_oversize_and_excess_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bin.py").write_bytes(b"\x00\x01\x02binary")
    _write(root / "big.py", "# pad\n" + "x = 1\n" * 400)
    _write(root / "ok.py", SAFE_SOURCE)
    _write(root / "extra.py", SAFE_SOURCE)

    inventory = collect_repo_inventory(
        RepoScanConfig(repo_root=root, max_file_bytes=2048, max_files=1)
    )
    assert "bin.py" in inventory.skipped_files
    assert "big.py" in inventory.skipped_files
    assert len(inventory.sources) == 1
    assert len(inventory.skipped_files) >= 2


def test_scanner_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        collect_repo_inventory(RepoScanConfig(repo_root=tmp_path / "nope"))


def _git(root: Path, *args: str, env_author: bool = False) -> str:
    cmd = ["git", "-C", str(root)]
    if env_author:
        cmd += ["-c", "user.name=AegisForge CI", "-c", "user.email=ci@aegisforge.local"]
    cmd += list(args)
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if completed.returncode != 0:
        pytest.skip(f"git 不可用或初始化失败: {completed.stderr.strip()[:120]}")
    return completed.stdout


# ---------------------------------------------------------------------------
# STEP-02：隔离 Patch 沙箱与 Critic 自愈
# ---------------------------------------------------------------------------
GOOD_PATCH = """--- a/app/orders.py
+++ b/app/orders.py
@@ -5,1 +5,1 @@
-    cur.execute("SELECT * FROM orders WHERE id = " + order_id)
+    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
"""

BROKEN_PATCH = """--- a/app/orders.py
+++ b/app/orders.py
@@ -5,1 +5,1 @@
-    cur.execute("SELECT * FROM orders WHERE id = " + order_id)
+    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,)
"""

REVERSE_GOOD = """--- a/app/orders.py
+++ b/app/orders.py
@@ -5,1 +5,1 @@
-    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
+    cur.execute("SELECT * FROM orders WHERE id = " + order_id)
"""


def _app_asset() -> "TargetAsset":
    from core.plugins.contracts import TargetAsset

    return TargetAsset(
        asset_id="app-ci-01",
        domain="appsec",
        engine="python",
        version="3.12",
        business_tier="CORE_TRADE",
        network_exposure="PUBLIC_DMZ",
        pii_density="HIGH",
        endpoint="/api/orders",
        labels=(("linked_db_asset", "pg-ci-trade-01"),),
    )


def _appsec_plugin(repo_root: Path, **kwargs):
    from core.plugins.appsec.ci_harness import verify_patch_in_isolated_workdir
    from core.plugins.appsec.plugin import AppSecPlugin

    plugin = AppSecPlugin(
        isolated_verifier=kwargs.pop("isolated_verifier", verify_patch_in_isolated_workdir),
        repo_root=repo_root,
        **kwargs,
    )
    plugin.register_assets([_app_asset()])
    plugin.set_sources("app-ci-01", {"app/orders.py": (repo_root / "app" / "orders.py").read_text(encoding="utf-8")})
    return plugin


def test_harness_applies_patch_in_tempdir_without_touching_repo(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    before = (root / "app" / "orders.py").read_bytes()
    plugin = _appsec_plugin(root)

    finding = next(
        item
        for item in plugin.probe().collect(_app_asset()).findings
        if item.category == "SQL_INJECTION"
    )
    proposal = plugin.build_proposal(finding, _app_asset())
    outcome = plugin.harness().evaluate(_app_asset(), finding, proposal)

    assert outcome.passed is True, outcome.failures
    assert outcome.sandbox_pool == "TRAFFIC_REPLAY"
    assert any("py_compile 通过" in line for line in outcome.logs)
    # 源仓库绝不被沙箱写坏
    assert (root / "app" / "orders.py").read_bytes() == before


def test_harness_blocks_syntax_broken_patch(tmp_path: Path) -> None:
    root = _build_repo(tmp_path)
    plugin = _appsec_plugin(root)
    finding = next(
        item
        for item in plugin.probe().collect(_app_asset()).findings
        if item.category == "SQL_INJECTION"
    )
    good = plugin.build_proposal(finding, _app_asset())
    broken = _replace_fix(good, BROKEN_PATCH, REVERSE_GOOD)

    outcome = plugin.harness().evaluate(_app_asset(), finding, broken)
    assert outcome.passed is False
    assert "COMPILE_FAILED" in outcome.failures
    assert outcome.dry_run_passed is False


def test_harness_rollback_hash_verification(tmp_path: Path) -> None:
    from core.plugins.appsec.ci_harness import run_patch_sandbox

    root = _build_repo(tmp_path)
    ok = run_patch_sandbox(root, GOOD_PATCH, REVERSE_GOOD)
    assert ok.patch_applied and ok.compile_ok and ok.rollback_verified
    assert ok.tests_passed is None, "未传 test_command 时必须是 None，不得伪造通过"
    assert ok.failures == ()

    wrong_rollback = run_patch_sandbox(root, GOOD_PATCH, REVERSE_GOOD.replace("%s", "?"))
    assert wrong_rollback.rollback_verified is False
    assert "ROLLBACK_HASH_MISMATCH" in wrong_rollback.failures or "PATCH_CONTEXT_MISMATCH" in wrong_rollback.failures

    mismatch = run_patch_sandbox(
        root,
        GOOD_PATCH.replace('WHERE id = " + order_id', 'WHERE id = 0 AND " + order_id'),
        REVERSE_GOOD,
    )
    assert mismatch.patch_applied is False
    assert "PATCH_CONTEXT_MISMATCH" in mismatch.failures


def test_harness_runs_test_command_in_sandbox(tmp_path: Path) -> None:
    from core.plugins.appsec.ci_harness import run_patch_sandbox

    root = _build_repo(tmp_path)
    _write(
        root / "tests" / "test_orders.py",
        "from app.orders import get_order\n\n\ndef test_ok():\n    assert get_order is not None\n",
    )
    result = run_patch_sandbox(
        root,
        GOOD_PATCH,
        REVERSE_GOOD,
        test_command=[sys.executable, "-m", "pytest", "tests/test_orders.py", "-q", "-p", "no:cacheprovider"],
        timeout_seconds=60,
    )
    assert result.patch_applied and result.compile_ok
    assert result.tests_passed is True, result.logs
    assert result.rollback_verified is True


def test_harness_critic_self_correction_on_broken_patch(tmp_path: Path) -> None:
    from dataclasses import replace

    from core.orchestrator.secops_graph import SecOpsOrchestrator
    from core.plugins.contracts import RemediationProposal, TargetAsset
    from core.plugins.registry import DomainPluginRegistry

    root = _build_repo(tmp_path)

    def _broken_draft(finding, asset: TargetAsset, round_no: int, feedback) -> RemediationProposal:
        plugin = _broken_draft.plugin  # type: ignore[attr-defined]
        saved = plugin._draft_provider  # noqa: SLF001
        plugin._draft_provider = None  # noqa: SLF001 - 避免自定义生成器回调自身
        try:
            base = plugin.build_proposal(finding, asset, round_no=round_no)
        finally:
            plugin._draft_provider = saved  # noqa: SLF001
        return replace(base, standard_fix=BROKEN_PATCH, round=round_no)

    plugin = _appsec_plugin(root)
    _broken_draft.plugin = plugin  # type: ignore[attr-defined]
    plugin._draft_provider = _broken_draft  # noqa: SLF001 - 测试注入坏补丁生成器

    registry = DomainPluginRegistry().register(plugin)
    orch = SecOpsOrchestrator(plugins=registry, repo_root=root)
    run = orch.run(
        tenant_id="tenant-ci",
        assets=[_app_asset()],
        workflow_run_id="wf-v41-critic",
        source_trigger="CI_PR_GATE",
    )

    verdicts = list(run.state["critic_verdicts"].values())
    assert verdicts, "Critic 必须至少裁决一次"
    assert all(item["max_rounds"] == 3 for item in verdicts)
    assert any("COMPILE_FAILED" in item["failures"] for item in verdicts), verdicts
    rejected = [item for item in verdicts if item["verdict"] == "REJECTED"]
    assert rejected, "坏补丁必须被打回"
    assert max(int(item["round"]) for item in verdicts) <= 3
    assert any(item.get("exhausted") for item in verdicts), "3 轮用尽必须转挂起而非静默放行"
    assert run.status in {"SUSPENDED", "COMPLETED"}


def _replace_fix(proposal, patch_text: str, rollback_text: str):
    from dataclasses import replace

    return replace(
        proposal,
        standard_fix=patch_text,
        rollback=replace(proposal.rollback, payload=rollback_text),
    )


# ---------------------------------------------------------------------------
# STEP-03：CI 门禁 CLI
# ---------------------------------------------------------------------------
def test_cli_blocks_on_p0_and_emits_valid_sarif(tmp_path: Path) -> None:
    from apps.cli.secops_gate import main

    root = _build_repo(tmp_path / "vuln")
    out = tmp_path / "out"
    sarif = out / "gate.sarif"
    md = out / "gate.md"

    code = main(
        [
            "--repo",
            str(root),
            "--fail-on",
            "P0",
            "--sarif-out",
            str(sarif),
            "--markdown-summary",
            str(md),
            "--patch-out",
            str(out / "gate.patch"),
        ]
    )
    assert code == 2
    payload = json.loads(sarif.read_text(encoding="utf-8"))
    assert payload["version"] == "2.1.0"
    assert payload["$schema"].endswith("sarif-2.1.0.json")
    run = payload["runs"][0]
    assert run["tool"]["driver"]["version"] == "4.1.0"
    assert len(run["results"]) == 5
    assert all("evidence" not in result for result in run["results"])

    # 任何产物都不得包含夹具里的凭据明文
    secret_value = 'fixture-pass-9931'
    for path in (sarif, md, out / "gate.patch"):
        assert secret_value not in path.read_text(encoding="utf-8")
    assert "APP-SQLI-01" in md.read_text(encoding="utf-8")


def test_cli_passes_clean_directory_with_zero_findings(tmp_path: Path) -> None:
    from apps.cli.secops_gate import main

    root = tmp_path / "clean"
    _write(root / "app" / "safe.py", SAFE_SOURCE)
    md = tmp_path / "clean.md"

    code = main(["--repo", str(root), "--fail-on", "P0", "--markdown-summary", str(md)])
    assert code == 0
    text = md.read_text(encoding="utf-8")
    assert "0 findings" in text
    assert "**PASS**" in text


def test_cli_fail_on_none_never_blocks(tmp_path: Path) -> None:
    from apps.cli.secops_gate import main

    root = _build_repo(tmp_path / "vuln")
    assert main(["--repo", str(root), "--fail-on", "none"]) == 0


def test_cli_returns_one_on_bad_arguments(tmp_path: Path) -> None:
    from apps.cli.secops_gate import main

    assert main(["--repo", str(tmp_path / "missing")]) == 1
    assert main(["--repo", str(tmp_path), "--fail-on", "P9"]) == 1


def test_cli_changed_only_and_sandbox_flags(tmp_path: Path) -> None:
    from apps.cli.secops_gate import main

    root = _build_repo(tmp_path / "vuln")
    _git(root, "init")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "baseline", env_author=True)
    base = _git(root, "rev-parse", "HEAD").strip()
    _write(root / "app" / "orders.py", VULN_SOURCE + "\n")

    assert main(["--repo", str(root), "--changed-only", "--base-ref", base, "--fail-on", "P0"]) == 0
    assert main(["--repo", str(root), "--fail-on", "P0", "--run-sandbox-tests"]) == 2
