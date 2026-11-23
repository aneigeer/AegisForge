"""W12 T03 集成测试：部署包清单（AC-10 / AC-11）。

口径：`docs/w12/03-task-cards.md` T03、`docs/w12/02-design.md` §4.2/§9.1、
`docs/w10-w12-lead-rulings.md` §4（W12-Q2/Q3）。

断言清单：

1. **AC-10**：11 项必填文件逐项 `exists is True`（清单覆盖设计 §9.1 第 475 行的全部路径）；
   `.env.example` 口令全为 `CHANGE_ME_*` 占位 → `env_example_has_real_secret is False`；
2. **AC-11**：五项检查（install/start/stop/upgrade/rollback）各 1 条；未实跑一律
   `NOT_EXECUTED` + 非空原因，**不判 FAIL**；
3. **制品不编造**：`commit` 来自 `git rev-parse HEAD`（与命令输出逐字相等），
   `image_digest="UNKNOWN"` + 登记，`artifact_hash` 由文件内容计算且可复现；
4. **容器不作硬门槛**：`nv1_is_hard_gate` 是 `ClassVar`（不在 `model_fields`）且为 `False`，
   `container_runtime_status="NOT_VERIFIED"`；
5. 负测：`NOT_EXECUTED` 缺原因 / 少一项检查 / 少列必填文件 / `UNKNOWN` 未登记 → 构造失败。

本文件只读文件与 `git rev-parse`：**不实跑** `docker build` / `compose up`、不执行 rollback、
不改 `deploy/**`、`docker-compose.yml`、`.env.example`、`scripts/**`。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.release.deploy import (
    DEPLOY_CHECK_IDS,
    DEPLOY_MANIFEST_VERSION,
    REQUIRED_DEPLOY_FILES,
    CheckStatus,
    DeployCheck,
    DeployPackageManifest,
    artifact_hash,
    collect_deploy_package,
    env_example_has_real_secret,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def manifest() -> DeployPackageManifest:
    return collect_deploy_package(_REPO_ROOT)


def _git_head(root: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - 固定只读命令，无 shell
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# AC-10：部署清单完整 + 口令占位
# ---------------------------------------------------------------------------


def test_ac10_required_files_all_exist(manifest: DeployPackageManifest) -> None:
    """11 项必填文件逐项存在（清单本身必须覆盖设计 §9.1 的全部路径）。"""
    assert set(manifest.required_files) == set(REQUIRED_DEPLOY_FILES)
    assert len(REQUIRED_DEPLOY_FILES) == 11
    for relative in REQUIRED_DEPLOY_FILES:
        assert manifest.required_files[relative] is True, f"必填文件缺失：{relative}"
        assert (_REPO_ROOT / relative).is_file(), f"磁盘上不存在：{relative}"


def test_ac10_env_example_has_no_real_secret(manifest: DeployPackageManifest) -> None:
    """`.env.example` 口令全为占位符（真实口令一律经 Secret 注入）。"""
    assert manifest.env_example_has_real_secret is False
    assert env_example_has_real_secret(_REPO_ROOT) is False

    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CHANGE_ME" in text
    assert "DG_RO_PASSWORD=CHANGE_ME_RO" in text
    assert "CHANGE_ME_CONTROL" in text


def test_ac10_real_secret_is_detected(tmp_path: Path) -> None:
    """可证伪性：真口令必须被判定为 True（不是"永远 False"）。"""
    fake = tmp_path / ".env.example"
    fake.write_text(
        "DG_RO_PASSWORD=CHANGE_ME_RO\n"
        "DG_RW_PASSWORD=super-secret-value\n"
        "DG_CONTROL_DSN=mysql://dg_ctl:CHANGE_ME_CONTROL@127.0.0.1:3306/dg_system\n",
        encoding="utf-8",
    )
    assert env_example_has_real_secret(tmp_path) is True

    dsn_leak = tmp_path / "dsn"
    dsn_leak.mkdir()
    (dsn_leak / ".env.example").write_text(
        "DG_CONTROL_DSN=mysql://dg_ctl:real_pass@127.0.0.1:3306/dg_system\n",
        encoding="utf-8",
    )
    assert env_example_has_real_secret(dsn_leak) is True


# ---------------------------------------------------------------------------
# AC-11：五项检查 + 制品记录
# ---------------------------------------------------------------------------


def test_ac11_five_checks_with_reason(manifest: DeployPackageManifest) -> None:
    """五项检查各 1 条；`NOT_EXECUTED` 必须给原因；不判 FAIL（没有 FAIL 这一档）。"""
    assert manifest.checks and len(manifest.checks) == 5
    assert [item.check_id for item in manifest.checks] == list(DEPLOY_CHECK_IDS)
    for item in manifest.checks:
        assert item.status in {CheckStatus.PASS, CheckStatus.NOT_EXECUTED}
        if item.status is CheckStatus.NOT_EXECUTED:
            assert (item.reason or "").strip(), f"{item.check_id} 缺原因"
    # 本轮实测：五项均未实跑（容器/升级/回滚都不在 W12 范围内执行）。
    assert {item.status for item in manifest.checks} == {CheckStatus.NOT_EXECUTED}
    reasons = " ".join(item.reason or "" for item in manifest.checks)
    assert "ENV-NV1" in reasons
    assert "ENV-1045" in reasons
    assert "rollback" in reasons


def test_ac11_commit_is_read_from_git_not_hardcoded(manifest: DeployPackageManifest) -> None:
    """`commit` 与 `git rev-parse HEAD` 逐字相等（不写死）。"""
    assert manifest.commit == _git_head(_REPO_ROOT)
    assert len(manifest.commit) == 40
    assert manifest.manifest_version == DEPLOY_MANIFEST_VERSION


def test_ac11_artifact_and_image_records_are_honest(manifest: DeployPackageManifest) -> None:
    """digest 缺失记 `UNKNOWN` + 登记；`artifact_hash` 可复现且不是占位值。"""
    assert manifest.image_digest == "UNKNOWN"
    assert any("digest" in issue for issue in manifest.registered_issues)

    assert manifest.artifact_hash != "UNKNOWN"
    assert len(manifest.artifact_hash) == 64
    assert manifest.artifact_hash == artifact_hash(_REPO_ROOT)
    int(manifest.artifact_hash, 16)  # 必须是合法十六进制
    # 可复现：同一输入两次计算结果一致。
    assert artifact_hash(_REPO_ROOT) == artifact_hash(_REPO_ROOT)


def test_ac11_container_is_not_a_hard_gate(manifest: DeployPackageManifest) -> None:
    """`nv1_is_hard_gate` 是 `ClassVar`（非字段）且为 `False`；运行态 `NOT_VERIFIED`。"""
    assert "nv1_is_hard_gate" not in DeployPackageManifest.model_fields
    assert DeployPackageManifest.nv1_is_hard_gate is False
    assert type(manifest).nv1_is_hard_gate is False
    assert manifest.container_runtime_status == "NOT_VERIFIED"
    assert any("ENV-NV1" in issue for issue in manifest.registered_issues)


# ---------------------------------------------------------------------------
# 负测：构造期 fail closed
# ---------------------------------------------------------------------------


def test_not_executed_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DeployCheck(check_id="install", status=CheckStatus.NOT_EXECUTED)
    with pytest.raises(ValidationError):
        DeployCheck(check_id="install", status=CheckStatus.NOT_EXECUTED, reason="   ")
    # 有原因即合法。
    assert DeployCheck(
        check_id="install", status=CheckStatus.NOT_EXECUTED, reason="ENV-NV1"
    ).status is CheckStatus.NOT_EXECUTED


def _checks() -> tuple[DeployCheck, ...]:
    return tuple(
        DeployCheck(check_id=check_id, status=CheckStatus.NOT_EXECUTED, reason="ENV-NV1")
        for check_id in DEPLOY_CHECK_IDS  # type: ignore[arg-type]
    )


def test_manifest_rejects_incomplete_checks() -> None:
    base = dict(
        commit="0" * 40,
        image_digest="UNKNOWN",
        artifact_hash="1" * 64,
        registered_issues=("W12-Q2/ENV-NV1：镜像 digest=UNKNOWN",),
        required_files={path: True for path in REQUIRED_DEPLOY_FILES},
    )
    with pytest.raises(ValidationError):
        DeployPackageManifest(checks=_checks()[:-1], **base)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DeployPackageManifest(checks=(*_checks(), _checks()[0]), **base)  # type: ignore[arg-type]
    assert len(DeployPackageManifest(checks=_checks(), **base).checks) == 5  # type: ignore[arg-type]


def test_manifest_rejects_missing_required_file_entry() -> None:
    files = {path: True for path in REQUIRED_DEPLOY_FILES}
    files.pop("deploy/Dockerfile.api")
    with pytest.raises(ValidationError):
        DeployPackageManifest(
            commit="0" * 40,
            image_digest="UNKNOWN",
            artifact_hash="1" * 64,
            registered_issues=("W12-Q2/ENV-NV1：镜像 digest=UNKNOWN",),
            checks=_checks(),
            required_files=files,
        )


def test_manifest_rejects_unregistered_unknown() -> None:
    base = dict(
        commit="0" * 40,
        checks=_checks(),
        required_files={path: True for path in REQUIRED_DEPLOY_FILES},
    )
    with pytest.raises(ValidationError):
        DeployPackageManifest(image_digest="UNKNOWN", artifact_hash="1" * 64, registered_issues=(), **base)
    with pytest.raises(ValidationError):
        DeployPackageManifest(
            image_digest="sha256:abc",
            artifact_hash="UNKNOWN",
            registered_issues=("digest registered",),
            **base,
        )


# ---------------------------------------------------------------------------
# 文档（AC-10 的说明面）
# ---------------------------------------------------------------------------


def test_deployment_guide_exists_and_keeps_kd2_naming() -> None:
    """部署指南存在，且 KD-2 命名正确（Gateway 与 Worker 同进程装配）。"""
    guide = _REPO_ROOT / "docs" / "w12" / "deployment-guide.md"
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    assert "同进程装配" in text
    assert "遗留占位" in text
    assert "docker-compose.yml:155-162" in text
    # 不得把网关写成独立容器链路。
    assert "api → gateway 容器" not in text
    assert "api -> gateway 容器" not in text
