"""W11 T06：RC 冻结清单与冻结件生成（P0-W11-05 / AC-15 / AC-21）。

设计依据：``docs/w11/02-design.md`` §9、§11；裁定 W11-Q7（镜像 digest 填 ``UNKNOWN``
并登记，容器运行态 ``NOT_VERIFIED``）、W11-Q8（``rc-freeze.md`` 内嵌限制清单同源 JSON）、
Q-W11-D（同源 JSON 块 + ``canonical_digest`` 对账）、Q-W11-E（AC-21 条件性执行）、
D-06（``docs/migrations.md`` 覆盖度**按实测判定**：未覆盖全部迁移才登记 ``ISSUE-MIGRATIONS-DOC-LAG``）。

三条硬约束：

1. **不写死 commit**：``commit`` 由 ``git rev-parse HEAD`` 取得；取不到才落 ``UNKNOWN``
   并登记问题（AC-15）。
2. **缺项规则**：任何读不到的版本项一律 ``value == "UNKNOWN"`` **且** ``registered_issue``；
   ``db_image_digest`` / ``model_version`` / ``docs_migrations_lag`` 三项按现状如实登记
   （``"NOT_VERIFIED"`` 不是 digest，不得借用；``docs_migrations_lag`` 由
   :func:`migrations_doc_lag` 按文档实测派生，不写死）。
3. **不预写结论**：``build_rc_freeze`` 的发布结论**只由** ``publish_decision`` 纯函数派生
   （W12-Q1/Q4），本模块不提供任何"人工改结论"的入口。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.assurance.publish import (
    LimitationList,
    PublishDecision,
    limitations_to_json,
)
from core.security.canonical import canonical_digest

RC_MANIFEST_VERSION = "w11-rc-v1"
METRIC_DEFINITION_VERSION = "metrics-v1"
UNKNOWN = "UNKNOWN"
NOT_VERIFIED = "NOT_VERIFIED"

#: 迁移文件应枚举到的条数（001–017 共 17 支正向迁移）。
EXPECTED_MIGRATION_COUNT = 17

#: 版本字段的权威来源（与 ``benchmarks/baseline_v0/baseline_manifest.json`` 一致）。
BASELINE_MANIFEST_PATH = ("benchmarks", "baseline_v0", "baseline_manifest.json")
W10_METRICS_PATH = ("benchmarks", "w10", "metrics-v1.json")
W10_METRICS_TEST_PATH = ("tests", "unit", "test_w10_metrics.py")

ISSUE_DB_IMAGE = "ISSUE-DB-IMAGE-DIGEST"
ISSUE_MODEL_VERSION = "ISSUE-MODEL-VERSION"
ISSUE_MIGRATIONS_DOC = "ISSUE-MIGRATIONS-DOC-LAG"
ISSUE_CONTAINER = "ISSUE-CONTAINER-RUNTIME"
ISSUE_FROZEN_EVIDENCE = "ISSUE-FROZEN-EVIDENCE"
ISSUE_UNREADABLE_PREFIX = "ISSUE-UNREADABLE-"


#: 报告索引（冻结顺序；状态取自各卡的定向验收结论，未实测项写 UNKNOWN/PARTIAL）。
DEFAULT_REPORT_INDEX: tuple[dict[str, str], ...] = (
    {
        "label": "安全｜负面集 29 条（AC-01…AC-04）",
        "path": "benchmarks/w11/negative_cases.json",
        "status": "PASSED",
    },
    {
        "label": "安全｜跨租户 + 十类并发（AC-05…AC-07）",
        "path": "benchmarks/w11/concurrency_cases.json",
        "status": "PASSED",
    },
    {
        "label": "可靠性｜六类故障注入与恢复（AC-08…AC-11）",
        "path": "docs/w11/fault-injection-report.md",
        "status": "PASSED",
    },
    {
        "label": "恢复｜逻辑影子回放 + 长稳计划（AC-12…AC-14）",
        "path": "docs/w11/backup-restore-report.md",
        "status": "PARTIAL",
    },
    {
        "label": "性能｜指标口径仅字面引用（AC-21）",
        "path": "benchmarks/w10/metrics-v1.json",
        "status": "REFERENCE_ONLY",
    },
    {
        "label": "成本｜未做成本实测（无独立 Object Store / 未跑容器）",
        "path": "docs/w11/limitations.json",
        "status": "UNKNOWN",
    },
)


class RegisteredIssue(BaseModel):
    """RC 清单里的一条登记问题（缺项必须登记，不得静默 ``UNKNOWN``）。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: str | None = None
    note: str = Field(min_length=1)


class RCManifest(BaseModel):
    """RC 冻结清单（``extra="forbid"``；字段集与设计 §9 一致）。"""

    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["w11-rc-v1"] = RC_MANIFEST_VERSION
    commit: str
    db_image_digest: str = UNKNOWN
    adapter_version: str
    tool_schema_version: str
    policy_version: str
    prompt_version: str
    model_version: str | None
    testset_version: str
    testset_version_label: str = "testset-v0-preview"
    migration_versions: tuple[str, ...]
    docs_migrations_lag: bool = True
    container_runtime_status: Literal["NOT_VERIFIED"] = NOT_VERIFIED
    metric_definition_version: str = METRIC_DEFINITION_VERSION
    frozen_evidence_available: bool = False
    registered_issues: tuple[RegisteredIssue, ...] = ()

    def issue_ids(self) -> tuple[str, ...]:
        return tuple(issue.issue_id for issue in self.registered_issues)

    def has_issue(self, issue_id: str) -> bool:
        return any(issue.issue_id == issue_id for issue in self.registered_issues)


@dataclass(frozen=True)
class E2ESegment:
    """E2E 四段之一（状态是闭集：不得用"部分通过"糊过去）。"""

    segment_id: Literal["S1", "S2", "S3", "S4"]
    status: Literal["PASSED", "FAILED", "NOT_EXECUTED", "NOT_VERIFIED"]
    reason: str | None = None


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def git_rev_parse(repo_root: Path) -> str | None:
    """``git rev-parse HEAD``；失败返回 ``None``（调用方落 ``UNKNOWN`` 并登记）。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 固定 argv，只读命令
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def migration_versions(repo_root: Path) -> tuple[str, ...]:
    """枚举 ``database/migrations/*.sql`` 的版本号（迁移文件为版本权威，D-06）。"""
    directory = Path(repo_root) / "database" / "migrations"
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.sql")))


def baseline_versions(repo_root: Path) -> dict[str, Any]:
    """读取基线清单里冻结的版本字段（读不到即空字典，由调用方登记）。"""
    path = Path(repo_root).joinpath(*BASELINE_MANIFEST_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _adapter_version() -> str:
    """两套适配器**声明的**版本（从代码事实读取，不写死）。"""
    import inspect as _inspect

    from adapters.mysql.adapter import MySQLAdapter
    from adapters.postgres_mcp.tools import ADAPTER_VERSION as PG_ADAPTER_VERSION

    mysql_version = _inspect.signature(MySQLAdapter.__init__).parameters["adapter_version"].default
    return f"mysql:{mysql_version};postgres_mcp:{PG_ADAPTER_VERSION}"


def _policy_version() -> str:
    from core.security.policy import PolicyEngine

    return str(PolicyEngine().policy_version)


#: 迁移文档落位（`docs_migrations_lag` 的判定输入）。
MIGRATIONS_DOC_PATH = ("docs", "migrations.md")


def migrations_doc_missing(root: Path, migrations: Sequence[str]) -> tuple[str, ...]:
    """`docs/migrations.md` **未覆盖**的迁移版本（按枚举顺序，去重保序）。

    文档缺失/不可读 → 返回全部版本（"读不到"不得静默变成"没滞后"）。
    没有任何迁移文件 → 返回空元组。
    """
    versions = tuple(dict.fromkeys(migrations))
    if not versions:
        return ()
    try:
        text = root.joinpath(*MIGRATIONS_DOC_PATH).read_text(encoding="utf-8")
    except OSError:
        return versions
    return tuple(version for version in versions if version not in text)


def migrations_doc_lag(root: Path, migrations: Sequence[str]) -> bool:
    """**按实测**判定迁移文档是否滞后（D-06）。

    判据：任一枚举到的迁移版本号未出现在 `docs/migrations.md` 正文即视为滞后；
    文档缺失/不可读同样视为滞后。没有任何迁移文件时返回 ``False``（无可滞后项）。

    Args:
        root: 仓库根目录。
        migrations: `migration_versions(root)` 的实测结果。

    Returns:
        ``True`` 表示文档未覆盖全部迁移；``False`` 表示已覆盖。
    """
    return bool(migrations_doc_missing(root, migrations))




#: W10 冻结件要求：版本号 + 指标定义条数（设计 §5 `metrics-v1` 冻结口径）。
REQUIRED_METRIC_DEFINITIONS = 14


def _frozen_evidence_available(root: Path) -> bool:
    """W10 冻结证据是否可用（**内容校验**，不是只看文件存在）。

    判据（任一不满足即 ``False``，异常也按 ``False`` 处理、不向上抛）：

    - ``benchmarks/w10/metrics-v1.json`` 存在、可解析、是对象；
    - ``version == "metrics-v1"``；
    - ``definitions`` 是长度为 14 的列表（W10 冻结的指标口径条数）；
    - ``tests/unit/test_w10_metrics.py`` 存在且含 ``def test_``。
    """
    metrics_path = root.joinpath(*W10_METRICS_PATH)
    test_path = root.joinpath(*W10_METRICS_TEST_PATH)
    try:
        if not metrics_path.is_file() or not test_path.is_file():
            return False
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if payload.get("version") != METRIC_DEFINITION_VERSION:
            return False
        definitions = payload.get("definitions")
        if not isinstance(definitions, list) or len(definitions) != REQUIRED_METRIC_DEFINITIONS:
            return False
        return "def test_" in test_path.read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return False


def collect_rc_manifest(*, repo_root: Path, commit_override: str | None = None) -> RCManifest:
    """采集 RC 清单：能读的读、读不到的落 ``UNKNOWN`` 并逐条登记（AC-15）。

    Args:
        repo_root: 仓库根目录。
        commit_override: 可选，显式指定 `commit`（用于**复现**已落盘冻结件：
            冻结件记录的是「其证据所对应的提交」，落盘并提交后 HEAD 会前进一格，
            若不放行显式入参，生成物与磁盘件将永远无法逐字对账）。缺省仍取
            ``git rev-parse HEAD``，**不写死**；`commit` 合法性与可追溯性由测试核对。

    Returns:
        :class:`RCManifest`。
    """
    root = Path(repo_root)
    issues: list[RegisteredIssue] = []

    commit = commit_override or git_rev_parse(root)
    if commit is None:
        commit = UNKNOWN
        issues.append(
            RegisteredIssue(
                issue_id=f"{ISSUE_UNREADABLE_PREFIX}COMMIT",
                field="commit",
                value=UNKNOWN,
                note="git rev-parse HEAD 不可用（非 git 环境或命令失败），如实记 UNKNOWN",
            )
        )

    baseline = baseline_versions(root)

    def _from_baseline(key: str) -> str:
        value = baseline.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        issues.append(
            RegisteredIssue(
                issue_id=f"{ISSUE_UNREADABLE_PREFIX}{key.upper()}",
                field=key,
                value=UNKNOWN,
                note=f"基线清单缺少 {key}，如实记 UNKNOWN",
            )
        )
        return UNKNOWN

    prompt_version = _from_baseline("prompt_version")
    tool_schema_version = _from_baseline("tool_schema_version")
    testset_version = _from_baseline("testset_version")

    adapter_version = _adapter_version()
    policy_version = _policy_version()

    migrations = migration_versions(root)
    if len(migrations) != EXPECTED_MIGRATION_COUNT:
        issues.append(
            RegisteredIssue(
                issue_id=f"{ISSUE_UNREADABLE_PREFIX}MIGRATIONS",
                field="migration_versions",
                value=str(len(migrations)),
                note=f"迁移文件枚举到 {len(migrations)} 项，与冻结值 {EXPECTED_MIGRATION_COUNT} 不符",
            )
        )

    # W11-Q7：容器运行态未验证 ⇒ 镜像 digest 只能 UNKNOWN，且必须登记。
    issues.append(
        RegisteredIssue(
            issue_id=ISSUE_DB_IMAGE,
            field="db_image_digest",
            value=UNKNOWN,
            note=(
                "容器运行态 NOT_VERIFIED（ENV-NV1）：未实跑 build/compose up，"
                "镜像 digest 取不到；deploy/environment-manifest.example.json 的 "
                "\"NOT_VERIFIED\" 不是 digest，不借用"
            ),
        )
    )
    issues.append(
        RegisteredIssue(
            issue_id=ISSUE_CONTAINER,
            field="container_runtime_status",
            value=NOT_VERIFIED,
            note="容器网络隔离只在配置层核对过，运行态未验证（不声称已验证）",
        )
    )
    # ENV-MODEL：模型环境缺失 ⇒ model_version 保持 None。
    model_version = baseline.get("model_commit")
    if not isinstance(model_version, str) or not model_version.strip():
        model_version = None
        issues.append(
            RegisteredIssue(
                issue_id=ISSUE_MODEL_VERSION,
                field="model_version",
                value=None,
                note=(
                    "ENV-MODEL：BLOCKED_MODEL_ENV_MISSING，model_commit 为 null；"
                    "模型版本与相关指标保持 null，不填估计值"
                ),
            )
        )
    # D-06：迁移文档滞后按**实测**判定（未全覆盖才登记，已覆盖则不再登记为问题）。
    docs_migrations_lag = migrations_doc_lag(root, migrations)
    if docs_migrations_lag:
        missing = migrations_doc_missing(root, migrations)
        issues.append(
            RegisteredIssue(
                issue_id=ISSUE_MIGRATIONS_DOC,
                field="docs_migrations_lag",
                value="True",
                note=(
                    "docs/migrations.md 未覆盖全部迁移版本（缺 "
                    + "、".join(missing)
                    + "）：以迁移文件枚举为权威，文档滞后如实登记"
                ),
            )
        )

    frozen_evidence_available = _frozen_evidence_available(root)
    if not frozen_evidence_available:
        issues.append(
            RegisteredIssue(
                issue_id=ISSUE_FROZEN_EVIDENCE,
                field="frozen_evidence_available",
                value="False",
                note="W10 metrics-v1 冻结件或对应用例缺失，AC-21 只能按条件性口径登记",
            )
        )

    return RCManifest(
        commit=commit,
        db_image_digest=UNKNOWN,
        adapter_version=adapter_version,
        tool_schema_version=tool_schema_version,
        policy_version=policy_version,
        prompt_version=prompt_version,
        model_version=model_version,
        testset_version=testset_version,
        migration_versions=migrations,
        docs_migrations_lag=docs_migrations_lag,
        metric_definition_version=METRIC_DEFINITION_VERSION,
        frozen_evidence_available=frozen_evidence_available,
        registered_issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# 冻结件生成
# ---------------------------------------------------------------------------


def limitations_digest(limitations: LimitationList) -> str:
    """限制清单的规范化摘要（与 ``rc-freeze.md`` 内嵌 JSON 对账用）。"""
    return canonical_digest(json.loads(limitations_to_json(limitations)))


def _issue_table(manifest: RCManifest) -> str:
    if not manifest.registered_issues:
        return "_无登记问题_"
    lines = ["| 问题编号 | 字段 | 取值 | 说明 |", "|---|---|---|---|"]
    for issue in manifest.registered_issues:
        value = "null" if issue.value is None else str(issue.value)
        lines.append(f"| `{issue.issue_id}` | `{issue.field}` | `{value}` | {issue.note} |")
    return "\n".join(lines)


def build_rc_freeze(
    *,
    manifest: RCManifest,
    decision: PublishDecision,
    limitations: LimitationList,
    reports: Sequence[Any] = (),
) -> str:
    """生成 ``docs/w11/rc-freeze.md`` 正文（纯字符串拼装，**不写文件**）。

    Args:
        manifest: RC 冻结清单。
        decision: 由 ``publish_decision`` 纯函数派生的发布结论（不得人工改写）。
        limitations: 限制清单（与 ``docs/w11/limitations.json`` **同源**）。
        reports: 报告索引条目（每项需提供 ``label`` / ``path`` / ``status``）。

    Returns:
        Markdown 文本；正文内嵌限制清单的**同源 JSON 块**与 ``canonical_digest``。
    """
    limitations_json = limitations_to_json(limitations)
    digest = limitations_digest(limitations)
    if limitations.decision is not decision.decision:
        raise ValueError("限制清单的 decision 必须与 publish_decision 的结论一致")

    report_rows = []
    for report in reports:
        label = getattr(report, "label", None) or (report.get("label") if isinstance(report, dict) else None)
        path = getattr(report, "path", None) or (report.get("path") if isinstance(report, dict) else None)
        status = getattr(report, "status", None) or (
            report.get("status") if isinstance(report, dict) else None
        )
        report_rows.append(f"| {label} | `{path}` | {status} |")
    report_table = "\n".join(
        ["| 类别 | 文件 | 状态 |", "|---|---|---|", *report_rows]
    ) if report_rows else "_未提供报告索引_"

    migration_rows = "\n".join(
        f"| {index} | `{name}` |" for index, name in enumerate(manifest.migration_versions, start=1)
    )

    return f"""# W11 RC 冻结件（`{manifest.manifest_version}`）

- 生成方式：`core.assurance.rc.build_rc_freeze()`（**纯字符串拼装**）+ `collect_rc_manifest()`；
  正文里的限制清单 JSON 与 `docs/w11/limitations.json` **同源**（同一 `build_limitations()` 输出）。
- 时点锚：`commit = {manifest.commit}`（由 `git rev-parse HEAD` 取得，**未写死**）
- 上游门禁口径：W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09「门禁未评」、W10「门禁未评」。
  **本文件不构成门禁结论，也不预写 Alpha 结论**（W12-Q1/Q4）。

## 1. 冻结清单

| 字段 | 取值 |
|---|---|
| `manifest_version` | `{manifest.manifest_version}` |
| `commit` | `{manifest.commit}` |
| `db_image_digest` | `{manifest.db_image_digest}`（W11-Q7：未验证，登记为问题） |
| `adapter_version` | `{manifest.adapter_version}` |
| `tool_schema_version` | `{manifest.tool_schema_version}` |
| `policy_version` | `{manifest.policy_version}` |
| `prompt_version` | `{manifest.prompt_version}` |
| `model_version` | `{"null" if manifest.model_version is None else manifest.model_version}`（ENV-MODEL） |
| `testset_version` | `{manifest.testset_version}` |
| `docs_migrations_lag` | `{manifest.docs_migrations_lag}`（D-06：`docs/migrations.md` {"未覆盖全部迁移" if manifest.docs_migrations_lag else "已覆盖全部迁移"}） |
| `container_runtime_status` | `{manifest.container_runtime_status}` |
| `metric_definition_version` | `{manifest.metric_definition_version}`（仅字面引用） |
| `frozen_evidence_available` | `{manifest.frozen_evidence_available}`（AC-21 按实测） |

### 1.1 迁移版本（枚举 `database/migrations/*.sql`，共 {len(manifest.migration_versions)} 项）

| # | 迁移 |
|---|---|
{migration_rows}

### 1.2 登记问题（缺项一律 `UNKNOWN` + 登记，不得静默）

{_issue_table(manifest)}

## 2. 报告索引

{report_table}

## 3. 未通过项与延期方案

- `TECH_PREVIEW_ONLY` 相关的未清项见第 4 节限制清单（自动项**不可删**）；延期方案：
  `ENV-PG5432` / `ENV-1045` / `ENV-NV1` / `ENV-MODEL` 四项落 W12 实跑，`ENV-RESIDUE` 只做归因不清理。
- AC-21（上游指标口径未回退）：按实测执行（`tests/unit/test_w10_metrics.py` 真跑），
  但 **W10 门禁仍未评**，本文件不写「同一冻结版本已复核」，也不另造 `metrics-v2`。

## 4. 发布结论（由 `publish_decision` 纯函数派生，不可人工覆盖）

| 字段 | 取值 |
|---|---|
| `decision` | `{decision.decision.value}` |
| `reason` | `{decision.reason}` |
| `failed_test_ids` | `{list(decision.failed_test_ids)}` |
| `environment_blocks` | `{list(decision.environment_blocks)}` |
| `inputs_digest` | `{decision.inputs_digest}` |
| `metric_definition_version` | `{decision.metric_definition_version}` |

> `failed_test_ids` 为空表示**没有红线失败**；环境阻塞属于完备性守卫（Q-W11-A），
> 不计入失败用例（PG 段仍是 `NOT_EXECUTED`，不判 FAIL）。
> 三份结果集（29 / 10 / 6）在收口时由 QA 用真实运行数据重算；本文件只固化口径与索引。

## 5. 限制清单（同源 JSON，单一数据源）

- 对账摘要：`limitations_digest = {digest}`（`canonical_digest(限制清单 JSON)`）
- 与 `docs/w11/limitations.json`：**同一函数**（`build_limitations()` + `limitations_to_json()`）序列化，
  不存在两个真值；自动项 6 项不可删（删 → `LIMITATION_REMOVED`）。

```json
{limitations_json}```
"""


__all__ = [
    "BASELINE_MANIFEST_PATH",
    "REQUIRED_METRIC_DEFINITIONS",
    "DEFAULT_REPORT_INDEX",
    "EXPECTED_MIGRATION_COUNT",
    "E2ESegment",
    "ISSUE_CONTAINER",
    "ISSUE_DB_IMAGE",
    "ISSUE_FROZEN_EVIDENCE",
    "ISSUE_MIGRATIONS_DOC",
    "ISSUE_MODEL_VERSION",
    "METRIC_DEFINITION_VERSION",
    "NOT_VERIFIED",
    "RC_MANIFEST_VERSION",
    "RCManifest",
    "RegisteredIssue",
    "UNKNOWN",
    "baseline_versions",
    "build_rc_freeze",
    "collect_rc_manifest",
    "git_rev_parse",
    "limitations_digest",
    "migration_versions",
    "migrations_doc_lag",
    "migrations_doc_missing",
]
