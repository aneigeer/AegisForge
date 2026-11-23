"""W11 T06 单元测试：RC 冻结清单（AC-15 / AC-21）。

覆盖：

- `commit` 由 `git rev-parse HEAD` 取得（**不写死**，与命令输出逐字相等）；
- `migration_versions` 枚举 `database/migrations/*.sql` = **11 项**；
- 版本字段与 `benchmarks/baseline_v0/baseline_manifest.json` 一致；
- 缺项规则：`UNKNOWN` 必须配 `registered_issue`；`db_image_digest="UNKNOWN"`（W11-Q7）、
  `model_version=None`（ENV-MODEL）必须登记；`docs_migrations_lag`（D-06）按
  `docs/migrations.md` **实际覆盖度**派生（未覆盖才登记，已覆盖则不登记）；
  `deploy/environment-manifest.example.json` 的 `"NOT_VERIFIED"` **不当作** digest；
- `rc_manifest.json` 与 `rc-freeze.md` 都是**函数产物**（磁盘件与函数输出逐字相等，可重生成）；
- 冻结件内嵌限制清单与 `docs/w11/limitations.json` **同源**，`canonical_digest` 对账一致；
- 不预写 Alpha 结论、不写「门禁通过」。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from core.assurance.publish import (
    ENVIRONMENT_BLOCK_IDS,
    build_limitations,
    limitations_to_json,
    publish_decision,
)
from core.assurance.rc import (
    DEFAULT_REPORT_INDEX,
    EXPECTED_MIGRATION_COUNT,
    ISSUE_CONTAINER,
    ISSUE_DB_IMAGE,
    ISSUE_MIGRATIONS_DOC,
    ISSUE_MODEL_VERSION,
    METRIC_DEFINITION_VERSION,
    UNKNOWN,
    RCManifest,
    baseline_versions,
    build_rc_freeze,
    collect_rc_manifest,
    git_rev_parse,
    limitations_digest,
    migration_versions,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "benchmarks" / "w11" / "rc_manifest.json"
_FREEZE_PATH = _REPO_ROOT / "docs" / "w11" / "rc-freeze.md"
_LIMITATIONS_PATH = _REPO_ROOT / "docs" / "w11" / "limitations.json"
_BASELINE_PATH = _REPO_ROOT / "benchmarks" / "baseline_v0" / "baseline_manifest.json"
_ENV_MANIFEST = _REPO_ROOT / "deploy" / "environment-manifest.example.json"
_MIGRATIONS_DOC = _REPO_ROOT / "docs" / "migrations.md"

_REPORTS = DEFAULT_REPORT_INDEX


@pytest.fixture(scope="module")
def manifest() -> RCManifest:
    return collect_rc_manifest(repo_root=_REPO_ROOT)


# ---------------------------------------------------------------------------
# AC-15：逐项有值 / 不写死
# ---------------------------------------------------------------------------


def test_commit_comes_from_git_rev_parse(manifest: RCManifest) -> None:
    expected = git_rev_parse(_REPO_ROOT)
    if expected is None:  # 非 git 环境
        assert manifest.commit == UNKNOWN
        assert any(issue.field == "commit" for issue in manifest.registered_issues)
        return
    assert manifest.commit == expected
    assert len(manifest.commit) == 40
    assert all(char in "0123456789abcdef" for char in manifest.commit)


def test_migration_versions_are_counted_from_disk(manifest: RCManifest) -> None:
    """迁移条数以**磁盘实测**为准（P1-BYOK 新增 012/013 后由 11 增至 13）。"""
    on_disk = migration_versions(_REPO_ROOT)
    assert len(on_disk) == EXPECTED_MIGRATION_COUNT
    assert manifest.migration_versions == on_disk
    assert manifest.migration_versions[0] == "001_harness_core"
    # 末位版本随迁移增长（P5-T1 起为 015）；只断言"与磁盘一致"，不写死编号。
    assert manifest.migration_versions[-1] == on_disk[-1]


def test_version_fields_match_baseline_manifest(manifest: RCManifest) -> None:
    baseline = baseline_versions(_REPO_ROOT)
    assert manifest.prompt_version == baseline["prompt_version"]
    assert manifest.tool_schema_version == baseline["tool_schema_version"]
    assert manifest.testset_version == baseline["testset_version"]
    assert manifest.manifest_version == "w11-rc-v1"
    assert manifest.metric_definition_version == METRIC_DEFINITION_VERSION == "metrics-v1"


def test_adapter_and_policy_versions_come_from_code(manifest: RCManifest) -> None:
    from adapters.postgres_mcp.tools import ADAPTER_VERSION as PG_ADAPTER_VERSION

    assert "mysql:" in manifest.adapter_version
    assert PG_ADAPTER_VERSION in manifest.adapter_version
    assert manifest.policy_version  # 由 PolicyEngine 默认值读取，非写死空串


# ---------------------------------------------------------------------------
# 缺项规则：UNKNOWN + registered_issue
# ---------------------------------------------------------------------------


def test_db_image_digest_is_unknown_and_registered(manifest: RCManifest) -> None:
    assert manifest.db_image_digest == UNKNOWN
    assert manifest.has_issue(ISSUE_DB_IMAGE)
    # `"NOT_VERIFIED"` 不是 digest，不得借用
    assert manifest.db_image_digest != "NOT_VERIFIED"
    example = _ENV_MANIFEST.read_text(encoding="utf-8")
    assert "NOT_VERIFIED" in example
    assert manifest.container_runtime_status == "NOT_VERIFIED"
    assert manifest.has_issue(ISSUE_CONTAINER)


def test_model_version_is_none_and_registered(manifest: RCManifest) -> None:
    assert manifest.model_version is None
    assert manifest.has_issue(ISSUE_MODEL_VERSION)
    issue = next(item for item in manifest.registered_issues if item.issue_id == ISSUE_MODEL_VERSION)
    assert issue.value is None
    assert "ENV-MODEL" in issue.note


def test_docs_migrations_lag_is_measured_not_hardcoded(manifest: RCManifest) -> None:
    """D-06：滞后按文档**实测**判定；当前文档已覆盖全部迁移，不再登记滞后。"""
    doc = _MIGRATIONS_DOC.read_text(encoding="utf-8")
    for version in manifest.migration_versions:
        assert version in doc, version
    assert "011" in doc
    assert manifest.docs_migrations_lag is False
    assert not manifest.has_issue(ISSUE_MIGRATIONS_DOC)


def test_docs_migrations_lag_detected_when_doc_incomplete(tmp_path: Path) -> None:
    """守卫不失效：文档未覆盖全部迁移时仍必须判滞后并逐项登记缺失版本。"""
    migrations_dir = tmp_path / "database" / "migrations"
    migrations_dir.mkdir(parents=True)
    for version in ("001_harness_core", "011_w09_memory"):
        (migrations_dir / f"{version}.sql").write_text("SELECT 1;\n", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "migrations.md").write_text("| `001_harness_core` | x |\n", encoding="utf-8")

    lagging = collect_rc_manifest(repo_root=tmp_path)
    assert lagging.docs_migrations_lag is True
    assert lagging.has_issue(ISSUE_MIGRATIONS_DOC)
    issue = next(item for item in lagging.registered_issues if item.issue_id == ISSUE_MIGRATIONS_DOC)
    assert "011_w09_memory" in issue.note

    # 文档缺失同样判滞后（"读不到"不得静默变成"没滞后"）。
    (docs_dir / "migrations.md").unlink()
    assert collect_rc_manifest(repo_root=tmp_path).docs_migrations_lag is True

    # 补齐后滞后消失。
    (docs_dir / "migrations.md").write_text(
        "| `001_harness_core` | x |\n| `011_w09_memory` | y |\n", encoding="utf-8"
    )
    repaired = collect_rc_manifest(repo_root=tmp_path)
    assert repaired.docs_migrations_lag is False
    assert not repaired.has_issue(ISSUE_MIGRATIONS_DOC)


def test_every_unknown_value_has_a_registered_issue(manifest: RCManifest) -> None:
    issue_fields = {issue.field for issue in manifest.registered_issues}
    for field, value in manifest.model_dump(mode="json").items():
        if value == UNKNOWN:
            assert field in issue_fields, f"{field} 为 UNKNOWN 但没有登记问题"


def test_frozen_evidence_available_reflects_real_files(manifest: RCManifest) -> None:
    """AC-21：W10 T03 已落盘 ⇒ 冻结证据可用（不写死 True，按文件存在判定）。"""
    metrics = _REPO_ROOT / "benchmarks" / "w10" / "metrics-v1.json"
    test_file = _REPO_ROOT / "tests" / "unit" / "test_w10_metrics.py"
    assert manifest.frozen_evidence_available is (metrics.is_file() and test_file.is_file())


# ---------------------------------------------------------------------------
# 落盘件与生成函数一致（单一来源、可重生成）
# ---------------------------------------------------------------------------


def test_frozen_evidence_requires_real_content(tmp_path: Path) -> None:
    """L-4：只看文件存在不够——空 `{}` + 空测试文件必须判 False。"""
    metrics = tmp_path / "benchmarks" / "w10" / "metrics-v1.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text("{}", encoding="utf-8")
    test_file = tmp_path / "tests" / "unit" / "test_w10_metrics.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("", encoding="utf-8")

    hollow = collect_rc_manifest(repo_root=tmp_path)
    assert hollow.frozen_evidence_available is False
    assert hollow.has_issue("ISSUE-FROZEN-EVIDENCE")

    # 版本号对但定义条数不对 → 仍 False
    metrics.write_text(
        json.dumps({"version": "metrics-v1", "definitions": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    test_file.write_text(
        "def test_placeholder() -> None:\n    assert True\n", encoding="utf-8"
    )
    assert collect_rc_manifest(repo_root=tmp_path).frozen_evidence_available is False

    # 版本 + 14 条定义 + 含 def test_ → True
    metrics.write_text(
        json.dumps(
            {"version": "metrics-v1", "definitions": [{"name": f"m{i}"} for i in range(14)]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert collect_rc_manifest(repo_root=tmp_path).frozen_evidence_available is True


def test_rc_manifest_json_matches_collect_output() -> None:
    """落盘件与函数产物**逐字相等**（用落盘件记录的 commit 复现）。

    冻结件记录的是「其证据所对应的提交」；提交落盘件本身会让 HEAD 前进一格，
    因此比较时显式传入落盘件里的 commit（`commit_override`），其余 12 个字段仍然逐字比。
    """
    on_disk = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    _assert_commit_is_traceable(on_disk["commit"])
    rebuilt = collect_rc_manifest(repo_root=_REPO_ROOT, commit_override=on_disk["commit"])
    assert on_disk == rebuilt.model_dump(mode="json")


def _frozen_commit() -> str:
    """落盘冻结件记录的提交（其证据所对应的代码版本）。"""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))["commit"]


def _assert_commit_is_traceable(commit: str) -> None:
    """冻结件里的 commit 必须是 40 位 sha **且在当前仓库历史中可达**（不得伪造）。"""
    assert re.fullmatch(r"[0-9a-f]{40}", commit), commit
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, f"冻结 commit 不在仓库历史中：{commit}"


def _freeze_text() -> tuple[str, str]:
    # 用落盘件记录的 commit 复现（提交落盘件本身会让 HEAD 前进一格，属预期）。
    manifest = collect_rc_manifest(repo_root=_REPO_ROOT, commit_override=_frozen_commit())
    decision = publish_decision(
        negative=(),
        concurrency=(),
        faults=(),
        environment_blocks=ENVIRONMENT_BLOCK_IDS,
    )
    limitations = build_limitations(decision)
    return (
        build_rc_freeze(
            manifest=manifest,
            decision=decision,
            limitations=limitations,
            reports=_REPORTS,
        ),
        limitations_digest(limitations),
    )


def test_rc_freeze_md_matches_build_output() -> None:
    generated, digest = _freeze_text()
    on_disk = _FREEZE_PATH.read_text(encoding="utf-8")
    assert on_disk == generated
    assert digest in on_disk


def test_freeze_embeds_limitations_same_source() -> None:
    generated, digest = _freeze_text()
    assert f"limitations_digest = {digest}" in generated
    limitations_on_disk = json.loads(_LIMITATIONS_PATH.read_text(encoding="utf-8"))
    decision = publish_decision(
        negative=(), concurrency=(), faults=(), environment_blocks=ENVIRONMENT_BLOCK_IDS
    )
    assert limitations_on_disk == json.loads(
        limitations_to_json(build_limitations(decision))
    )
    assert digest == limitations_digest(build_limitations(decision))
    # 内嵌块可解析且与磁盘件一致
    block = generated.split("```json\n", 1)[1].split("```", 1)[0]
    assert json.loads(block) == limitations_on_disk


def test_freeze_does_not_prewrite_alpha_conclusion() -> None:
    text = _FREEZE_PATH.read_text(encoding="utf-8")
    # 表述禁令：只允许「否定式」或 W07 权威短语「有条件放行；门禁通过：否」里的用法。
    allowed_prefixes = ("不", "禁", "有条件放行；")
    for banned in ("Alpha 已就绪", "完整 Alpha 已", "门禁通过", "环境已清零", "同一冻结版本已复核"):
        for match in _find(text, banned):
            prefix = text[max(0, match - 12) : match]
            assert any(item in prefix for item in allowed_prefixes), (banned, prefix)
    assert "TECH_PREVIEW_ONLY" in text
    assert "由 `publish_decision` 纯函数派生" in text


def test_freeze_lists_all_reports_and_migrations() -> None:
    text = _FREEZE_PATH.read_text(encoding="utf-8")
    for report in _REPORTS:
        assert report["path"] in text
        assert report["label"] in text
    for index in range(1, 12):
        assert f"| {index} |" in text


def _find(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = text.find(needle)
    while start != -1:
        positions.append(start)
        start = text.find(needle, start + 1)
    return positions


# ---------------------------------------------------------------------------
# 模型校验与边界
# ---------------------------------------------------------------------------


def test_manifest_model_is_closed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RCManifest(
            commit="0" * 40,
            adapter_version="x",
            tool_schema_version="x",
            policy_version="x",
            prompt_version="x",
            model_version=None,
            testset_version="x",
            migration_versions=(),
            unknown_field=1,
        )


def test_git_rev_parse_returns_none_outside_repo(tmp_path: Path) -> None:
    assert git_rev_parse(tmp_path) is None


def test_migration_versions_empty_when_directory_missing(tmp_path: Path) -> None:
    assert migration_versions(tmp_path) == ()


def test_baseline_versions_empty_when_missing(tmp_path: Path) -> None:
    assert baseline_versions(tmp_path) == {}


def test_collect_flags_unreadable_baseline(tmp_path: Path) -> None:
    manifest = collect_rc_manifest(repo_root=tmp_path)
    assert manifest.prompt_version == UNKNOWN
    assert manifest.tool_schema_version == UNKNOWN
    assert manifest.testset_version == UNKNOWN
    for field in ("prompt_version", "tool_schema_version", "testset_version"):
        assert any(issue.field == field for issue in manifest.registered_issues), field
    assert manifest.frozen_evidence_available is False


def test_freeze_rejects_decision_mismatch() -> None:
    """限制清单的 decision 必须与发布结论一致（防止出现两个真值）。"""
    from core.assurance.publish import Decision, LimitationList, _auto_items

    manifest = collect_rc_manifest(repo_root=_REPO_ROOT)
    decision = publish_decision(
        negative=(), concurrency=(), faults=(), environment_blocks=ENVIRONMENT_BLOCK_IDS
    )
    inconsistent = LimitationList(
        decision=Decision.ALPHA_ALLOWED, items=_auto_items(Decision.ALPHA_ALLOWED)
    )
    with pytest.raises(ValueError):
        build_rc_freeze(
            manifest=manifest, decision=decision, limitations=inconsistent, reports=_REPORTS
        )


def test_issue_ids_are_unique(manifest: RCManifest) -> None:
    ids = manifest.issue_ids()
    assert len(ids) == len(set(ids))
