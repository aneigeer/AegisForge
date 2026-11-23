"""W12 T03：部署包清单与可复现演示（AC-10 / AC-11 / AC-12）。

口径来源：`docs/w12/01-prd.md` §5（AC-10…AC-12）、`docs/w12/02-design.md` §4.2/§9.1/§9.2、
`docs/w12/03-task-cards.md` T03、`docs/w10-w12-lead-rulings.md` §4（W12-Q2/Q3/Q7）。

三条验收的落地方式：

| AC | 口径 | 落点 |
|---|---|---|
| AC-10 | 部署清单 11 项必填文件逐项 `exists is True`；`.env.example` 口令全 `CHANGE_ME_*` | :func:`collect_deploy_package` → `required_files` / `env_example_has_real_secret` |
| AC-11 | 五项检查（install/start/stop/upgrade/rollback）各 1；未实跑 → `NOT_EXECUTED` + `reason`，**不判 FAIL** | :class:`DeployCheck` + `_default_checks` |
| AC-12 | 四段演示各 1；`lab_only=True`、`production_dsn_referenced=False`、无 Web 控制台 | :func:`build_demo_result` |

**容器不作硬门槛（W12-Q3）**：`DeployPackageManifest.nv1_is_hard_gate` 是
``ClassVar[bool] = False``（不是模型字段，不可被数据覆盖），`container_runtime_status`
恒 ``"NOT_VERIFIED"``。本模块**不实跑** `docker build` / `compose up`，**不执行**
rollback（含 DROP，须人工 admin）。

**禁止编造（W12-Q2/Q5）**：镜像 digest 缺失记 ``"UNKNOWN"`` + `registered_issues`；
`artifact_hash` 由**实际交付文件内容**计算（同一输入必得同一输出），任一件缺失即
``"UNKNOWN"`` + 登记。**绝不**写估计值、绝不把 `NOT_EXECUTED` 写成 `PASS`。

**只读边界**：本模块只读文件与 `git rev-parse`；不改 `deploy/**`、`docker-compose.yml`、
`.env.example`、`scripts/**`；不连任何数据库、不引用生产 DSN。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from core.release import AcceptanceStatus

#: 部署包清单版本（后续卡只读引用）。
DEPLOY_MANIFEST_VERSION: str = "w12-deploy-v1"

#: AC-10 必填文件（设计 §9.1 第 475 行逐字，共 11 项）。
REQUIRED_DEPLOY_FILES: tuple[str, ...] = (
    "deploy/Dockerfile.api",
    "deploy/Dockerfile.worker",
    "deploy/environment-manifest.example.json",
    "deploy/init/pg_lab_bootstrap.sh",
    "deploy/network/README.md",
    "docker-compose.yml",
    ".env.example",
    "scripts/migrate.py",
    "scripts/bootstrap_mysql.py",
    "scripts/bootstrap_pg_lab.py",
    "scripts/environment_manifest.py",
)

#: 五项部署检查（AC-11）；顺序固定。
DEPLOY_CHECK_IDS: tuple[str, ...] = ("install", "start", "stop", "upgrade", "rollback")

#: 四段演示（AC-12，设计 §9.2）；顺序固定。
DEMO_SEGMENT_IDS: tuple[str, ...] = ("S1", "S2", "S3", "S4")

#: 演示稿缺省路径（`DemoScriptResult` 措辞自检的输入）。
DEFAULT_DEMO_DOC: str = "docs/w12/demo-script.md"

#: 「建议项被写成已完成」的判定词面（AC-12：不得用「已修复」描述「建议」）。
COMPLETION_WORDING: str = "已修复"
ADVICE_WORDING: str = "建议"
#: 规范引用/禁令语境豁免（否则文档里"禁止写成已修复"这句自身会被判违规）。
COMPLETION_WORDING_NEGATIONS: tuple[str, ...] = (
    "不得",
    "禁止",
    "不使用",
    "不允许",
    "违规",
)

#: `.env.example` 里必须为占位符的敏感键（口令/密钥/DSN 的密码段）。
PLACEHOLDER_TOKEN: str = "CHANGE_ME"
_SENSITIVE_ASSIGNMENT = re.compile(
    r"^\s*(?P<key>[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|KEY|DSN))\s*=\s*(?P<value>.*)$"
)

#: 已登记问题编号（只登记，不驱动判定）。
ISSUE_IMAGE_DIGEST_UNKNOWN = "W12-Q2/ENV-NV1：容器运行态未验证，镜像 digest=UNKNOWN"
ISSUE_ARTIFACT_HASH_UNKNOWN = "ARTIFACT_HASH_UNKNOWN：交付文件缺失，无法计算制品哈希"
ISSUE_COMMIT_UNKNOWN = "COMMIT_UNKNOWN：`git rev-parse HEAD` 不可用"
ISSUE_CONTAINER_NOT_VERIFIED = "ENV-NV1：container_runtime_status=NOT_VERIFIED（不作硬门槛）"


class CheckStatus(str, Enum):
    """部署检查状态：只有"实测通过"与"未执行"两档（未执行不判 FAIL）。"""

    PASS = "PASS"
    NOT_EXECUTED = "NOT_EXECUTED"


class DeployCheck(BaseModel):
    """一条部署检查结论；`NOT_EXECUTED` 必须给出原因。"""

    model_config = ConfigDict(extra="forbid")

    check_id: Literal["install", "start", "stop", "upgrade", "rollback"]
    status: CheckStatus
    reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_not_executed(self) -> "DeployCheck":
        """`NOT_EXECUTED` 必须带非空原因（禁止"未执行"却不说为什么）。"""
        if self.status is CheckStatus.NOT_EXECUTED:
            text = (self.reason or "").strip()
            if not text:
                raise ValueError(
                    f"{self.check_id}：status=NOT_EXECUTED 必须给出 reason（如 ENV-NV1）"
                )
        return self


class DeployPackageManifest(BaseModel):
    """部署包清单（AC-10 / AC-11）；`extra="forbid"`。"""

    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["w12-deploy-v1"] = DEPLOY_MANIFEST_VERSION
    #: `git rev-parse HEAD`（不写死；不可用时 "UNKNOWN" + 登记）。
    commit: str
    #: W12-Q2 / ENV-NV1：未实跑容器，digest 只能是 UNKNOWN。
    image_digest: str = "UNKNOWN"
    artifact_hash: str = "UNKNOWN"
    registered_issues: tuple[str, ...]
    checks: tuple[DeployCheck, ...]
    required_files: dict[str, bool]
    env_example_has_real_secret: bool = False
    container_runtime_status: Literal["NOT_VERIFIED"] = "NOT_VERIFIED"
    #: W12-Q3：容器**不是**硬门槛（ClassVar → 不是字段，无法被数据覆盖）。
    nv1_is_hard_gate: ClassVar[bool] = False

    @model_validator(mode="after")
    def _checks_complete_and_unique(self) -> "DeployPackageManifest":
        """五项检查必须恰各 1 条（缺项/多项一律构造失败）。"""
        ids = [item.check_id for item in self.checks]
        if len(ids) != len(DEPLOY_CHECK_IDS):
            raise ValueError(
                f"checks 必须恰 {len(DEPLOY_CHECK_IDS)} 条，实际 {len(ids)}"
            )
        if sorted(ids) != sorted(DEPLOY_CHECK_IDS):
            raise ValueError(f"checks 的 check_id 集合必须恰为 {DEPLOY_CHECK_IDS}，实际 {ids}")
        return self

    @model_validator(mode="after")
    def _required_files_complete(self) -> "DeployPackageManifest":
        """必填文件清单必须覆盖全部 11 项（不允许"少列一项"混过去）。"""
        missing = [path for path in REQUIRED_DEPLOY_FILES if path not in self.required_files]
        if missing:
            raise ValueError(f"required_files 缺少必填项：{missing}")
        return self

    @model_validator(mode="after")
    def _unknown_values_are_registered(self) -> "DeployPackageManifest":
        """`UNKNOWN` 必须伴随登记项（禁止"未知但不声明"）。"""
        if self.image_digest == "UNKNOWN" and not any(
            "digest" in issue for issue in self.registered_issues
        ):
            raise ValueError("image_digest=UNKNOWN 必须在 registered_issues 中登记")
        if self.artifact_hash == "UNKNOWN" and not any(
            "ARTIFACT_HASH" in issue for issue in self.registered_issues
        ):
            raise ValueError("artifact_hash=UNKNOWN 必须在 registered_issues 中登记")
        if self.commit == "UNKNOWN" and not any(
            "COMMIT_UNKNOWN" in issue for issue in self.registered_issues
        ):
            raise ValueError("commit=UNKNOWN 必须在 registered_issues 中登记")
        return self


class DemoSegment(BaseModel):
    """一段演示（AC-12）；未执行必须给原因。"""

    model_config = ConfigDict(extra="forbid")

    segment_id: Literal["S1", "S2", "S3", "S4"]
    name: str
    status: AcceptanceStatus
    reason: str | None = None


class DemoScriptResult(BaseModel):
    """演示四段的结论件（AC-12）。

    ★ 不承载任何验收判定输入（AC-20：演示 ≠ 验收）：本模型只写进
    `docs/w12/demo-script.md`，**不进入** `AlphaAcceptanceReport` 的 `ac_result`。
    """

    model_config = ConfigDict(extra="forbid")

    segments: tuple[DemoSegment, ...]
    #: 演示只走隔离 LAB（AC-12）：恒 True，构造期强制。
    lab_only: bool = True
    #: 演示不引用生产 DSN（AC-12）：恒 False，构造期强制。
    production_dsn_referenced: bool = False
    pg_segment_status: AcceptanceStatus
    #: 「已修复」描述「建议」→ True 即违规；构造期强制为 False。
    report_has_completion_wording_for_advice: bool = False
    #: W12-Q7：无 Web 控制台（ClassVar → 不是字段，无法被数据覆盖）。
    web_console_used: ClassVar[bool] = False

    @model_validator(mode="after")
    def _four_segments_unique(self) -> "DemoScriptResult":
        """四段必须恰各 1 例（顺序无关但集合相等）。"""
        ids = [item.segment_id for item in self.segments]
        if len(ids) != len(DEMO_SEGMENT_IDS):
            raise ValueError(f"segments 必须恰 {len(DEMO_SEGMENT_IDS)} 段，实际 {len(ids)}")
        if sorted(ids) != sorted(DEMO_SEGMENT_IDS):
            raise ValueError(f"segments 的 segment_id 集合必须恰为 {DEMO_SEGMENT_IDS}，实际 {ids}")
        return self

    @model_validator(mode="after")
    def _isolation_invariants(self) -> "DemoScriptResult":
        """隔离不变量：两个布尔位不得被改坏。

        `report_has_completion_wording_for_advice` **不在此强制**：True 表示"检测到违规"，
        是**合法取值**（由调用方/断言负责拒绝），强制为 False 会让违规被 Validator 掩盖，
        失去可观测性。
        """
        if self.lab_only is not True:
            raise ValueError("演示必须 lab_only=True（只走隔离 LAB）")
        if self.production_dsn_referenced is not False:
            raise ValueError("演示不得引用生产 DSN")
        return self


# ---------------------------------------------------------------------------
# 读取工具
# ---------------------------------------------------------------------------


def _run_git_rev_parse(root: Path) -> str:
    """只读取 `HEAD`（不写死；不可用返回 "UNKNOWN"，绝不猜）。"""
    try:
        completed = subprocess.run(  # noqa: S603 - 固定只读命令，无 shell
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    if completed.returncode != 0:
        return "UNKNOWN"
    commit = completed.stdout.strip()
    return commit or "UNKNOWN"


def artifact_hash(root: Path) -> str:
    """按**实际文件内容**计算交付制品哈希（同一输入必得同一输出）。

    缺失任一件返回 ``"UNKNOWN"``——**不**用占位值或估计值顶替。
    """
    digest = hashlib.sha256()
    for relative in REQUIRED_DEPLOY_FILES:
        path = root / relative
        if not path.is_file():
            return "UNKNOWN"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def env_example_has_real_secret(root: Path) -> bool:
    """`.env.example` 是否含**非占位**口令/密钥（True 即违规）。

    判定口径：所有敏感键（`*PASSWORD` / `*SECRET` / `*TOKEN` / `*KEY` / `*DSN`）的取值
    必须包含 ``CHANGE_ME`` 占位符；DSN 只看其中口令段是否含占位符。空值不算真实口令
    （无口令可泄露），但也**不**算"已配置"。
    """
    path = root / ".env.example"
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0]
        match = _SENSITIVE_ASSIGNMENT.match(line)
        if match is None:
            continue
        key = match.group("key")
        value = match.group("value").strip()
        if not value:
            continue
        if key.endswith("DSN"):
            # DSN 形如 scheme://user:<password>@host/db；只看口令段。
            if "://" not in value:
                continue
            head, _, tail = value.partition("://")
            _scheme = head
            credentials, _, _rest = tail.partition("@")
            password = credentials.partition(":")[2] if ":" in credentials else ""
            if password and PLACEHOLDER_TOKEN not in password:
                return True
            continue
        if PLACEHOLDER_TOKEN not in value:
            return True
    return False


def completion_wording_for_advice(text: str) -> bool:
    """文本是否把"建议"写成"已修复"（AC-12 的措辞红线）。

    上下文感知（沿用设计 §8.2 的否定语境思路）：同一行同时出现
    :data:`ADVICE_WORDING` 与 :data:`COMPLETION_WORDING`，且**不含**
    :data:`COMPLETION_WORDING_NEGATIONS` 中的任一词 → 判为违规。
    """
    for raw_line in text.splitlines():
        if ADVICE_WORDING not in raw_line or COMPLETION_WORDING not in raw_line:
            continue
        if any(token in raw_line for token in COMPLETION_WORDING_NEGATIONS):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# AC-10 / AC-11：部署包清单
# ---------------------------------------------------------------------------


def _default_checks() -> tuple[DeployCheck, ...]:
    """五项检查的**如实**结论：本轮全部未实跑，逐条给出原因（不判 FAIL）。"""
    return (
        DeployCheck(
            check_id="install",
            status=CheckStatus.NOT_EXECUTED,
            reason="ENV-NV1：本轮不实跑 docker build / 镜像安装（容器不作硬门槛，W12-Q3）",
        ),
        DeployCheck(
            check_id="start",
            status=CheckStatus.NOT_EXECUTED,
            reason="ENV-NV1：本轮不实跑 compose up（运行态 NOT_VERIFIED）",
        ),
        DeployCheck(
            check_id="stop",
            status=CheckStatus.NOT_EXECUTED,
            reason="ENV-NV1：本轮不实跑 compose down（无运行态可停）",
        ),
        DeployCheck(
            check_id="upgrade",
            status=CheckStatus.NOT_EXECUTED,
            reason="DG_ADMIN_DSN 不可用（ENV-1045）：本轮不实跑升级演练",
        ),
        DeployCheck(
            check_id="rollback",
            status=CheckStatus.NOT_EXECUTED,
            reason="本轮不执行 rollback（含 DROP，须人工 admin；W12 不执行回滚）",
        ),
    )


def collect_deploy_package(root: Path) -> DeployPackageManifest:
    """采集部署包清单（AC-10/AC-11）。

    Args:
        root: 仓库根目录。

    Returns:
        :class:`DeployPackageManifest`；`commit` 由 `git rev-parse HEAD` 取得，
        `artifact_hash` 由必填文件内容哈希得出，两者缺失均记 ``UNKNOWN`` + 登记。
    """
    base = Path(root)
    commit = _run_git_rev_parse(base)
    required = {relative: (base / relative).is_file() for relative in REQUIRED_DEPLOY_FILES}
    computed_hash = artifact_hash(base)

    registered: list[str] = [ISSUE_IMAGE_DIGEST_UNKNOWN, ISSUE_CONTAINER_NOT_VERIFIED]
    if computed_hash == "UNKNOWN":
        missing = [path for path, exists in required.items() if not exists]
        registered.append(f"{ISSUE_ARTIFACT_HASH_UNKNOWN}（缺失：{', '.join(missing) or '未知'}）")
    if commit == "UNKNOWN":
        registered.append(ISSUE_COMMIT_UNKNOWN)

    return DeployPackageManifest(
        commit=commit,
        image_digest="UNKNOWN",
        artifact_hash=computed_hash,
        registered_issues=tuple(registered),
        checks=_default_checks(),
        required_files=required,
        env_example_has_real_secret=env_example_has_real_secret(base),
    )


# ---------------------------------------------------------------------------
# AC-12：可复现演示（隔离 + 措辞诚实）
# ---------------------------------------------------------------------------


def build_demo_result(root: Path | None = None) -> DemoScriptResult:
    """构造演示结论件（AC-12）。

    Args:
        root: 可选，仓库根（用于对 `docs/w12/demo-script.md` 做措辞自检）；
            缺省按 `core/release/deploy.py` 上溯三级定位仓库根。

    Returns:
        :class:`DemoScriptResult`。**本轮四段演示均未实跑**（无 Web 控制台、不实跑
        容器、不写生产库），因此四段一律 ``NOT_EXECUTED`` + 原因，PG 段亦
        ``NOT_EXECUTED``（`ENV-PG5432`）——**不冒充通过**。

    措辞自检：若演示稿里出现「建议」被描述成「已修复」（无否定语境），
    `report_has_completion_wording_for_advice` 会置 True 并触发构造失败。
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    doc = base / DEFAULT_DEMO_DOC
    violated = False
    if doc.is_file():
        violated = completion_wording_for_advice(doc.read_text(encoding="utf-8"))

    segments = (
        DemoSegment(
            segment_id="S1",
            name="双引擎只读诊断（PostgreSQL 段 NOT_EXECUTED）",
            status=AcceptanceStatus.NOT_EXECUTED,
            reason="本轮未实跑演示脚本；PG 段受 ENV-PG5432 阻塞（不判 FAIL、不冒充通过）",
        ),
        DemoSegment(
            segment_id="S2",
            name="单引擎审批修复（唯一 R2 动作）",
            status=AcceptanceStatus.NOT_EXECUTED,
            reason="本轮未实跑演示脚本；该链路的验收结论见 T01 AC-03（结论 PASSED，KD-W12-01 已关闭 2026-11-17）",
        ),
        DemoSegment(
            segment_id="S3",
            name="独立 Verifier 回读（异路径只读）",
            status=AcceptanceStatus.NOT_EXECUTED,
            reason="本轮未实跑演示脚本；独立回读端口已由 T01/T02 用例覆盖，演示本身未执行",
        ),
        DemoSegment(
            segment_id="S4",
            name="UNKNOWN 恢复演练（不重试）",
            status=AcceptanceStatus.NOT_EXECUTED,
            reason="本轮未实跑演示脚本；UNKNOWN 不重试已由 T02 AC-08 实测覆盖",
        ),
    )
    return DemoScriptResult(
        segments=segments,
        lab_only=True,
        production_dsn_referenced=False,
        pg_segment_status=AcceptanceStatus.NOT_EXECUTED,
        report_has_completion_wording_for_advice=violated,
    )


def describe_isolation() -> dict[str, Any]:
    """演示隔离检查记录（供 `docs/w12/demo-script.md` 与测试引用）。

    只记录**可复核的事实口径**，不写"已验证"之类结论：
    - 只连 `dg_lab` / `dg_lab_pg`（LAB），不引用生产 DSN；
    - 写动作只有 LAB 受限单行更新（`mysql_lab_update_order_note`/1.0）；
    - 无 Web 控制台（W12-Q7），演示只经 CLI 脚本。
    """
    return {
        "lab_only": True,
        "production_dsn_referenced": False,
        "allowed_write_tool": "mysql_lab_update_order_note",
        "allowed_write_tool_version": "1.0",
        "web_console_used": False,
        "reused_script": "scripts/demo_agent_run.py",
        "note": "隔离口径为配置层只读复核；运行态容器未验证（ENV-NV1），不据此下结论",
    }


__all__ = [
    "CheckStatus",
    "DEFAULT_DEMO_DOC",
    "DEMO_SEGMENT_IDS",
    "DEPLOY_CHECK_IDS",
    "DEPLOY_MANIFEST_VERSION",
    "DeployCheck",
    "DeployPackageManifest",
    "DemoScriptResult",
    "DemoSegment",
    "REQUIRED_DEPLOY_FILES",
    "artifact_hash",
    "build_demo_result",
    "collect_deploy_package",
    "completion_wording_for_advice",
    "describe_isolation",
    "env_example_has_real_secret",
]
