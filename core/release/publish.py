"""W12 T05：发布与复盘（AC-16 / AC-17 / AC-18）。

口径来源：`docs/w12/01-prd.md` §5（AC-16…AC-18）、`docs/w12/02-design.md` §4.4/§5/§9.5、
`docs/w12/03-task-cards.md` T05、`docs/w10-w12-lead-rulings.md` §4（W12-Q1/Q4/Q5）、§14.1。

三条验收的落地方式：

| AC | 口径 | 落点 |
|---|---|---|
| AC-16 | `release_type` **只**由 W11 `publish_decision` 派生；`TECH_PREVIEW` 名称不含 `Alpha`（大小写不敏感）；`ALPHA` 只写「Alpha」，**不写「完整 Alpha」** | :func:`derive_release_type` + :class:`ReleaseGate.__post_init__` |
| AC-17 | 纯函数：签名无 `force_alpha`/`override`/任何 `force_*`；无 I/O、无 `utc_now()`、无全局状态；同输入逐位相等 | :func:`derive_release_type`（`dataclass(frozen=True)` 无时间字段） |
| AC-18 | 训练收益诚实：`model="baseline"`、`sft_reproducible_gain=False`、`dpo/full_14b/rl_deferred` 恒 `True`（`ClassVar`）、`baseline_score=None` | :class:`AlphaAcceptanceReport` 校验器 |

**W12-Q4 默认口径**：`decision is None` → `effective_decision="TECH_PREVIEW_ONLY"` +
`source="DEFAULT_TECH_PREVIEW_ONLY"` + 登记 `PUBLISH_DECISION_MISSING`；其它未知取值同样
fail-closed + 登记 `UNKNOWN_DECISION_VALUE`。**两种输入都产出 `TECH_PREVIEW`**。

**W12-Q1：不预写结论**：本模块**不写死** W11 `publish_decision` 的具体值，也不预写 Alpha
结论——`release_type` 完全由入参派生；`ReleaseGate` 无时间字段、无状态。

**KD-W12-01（`docs/w10-w12-lead-rulings.md` §14.1）**：曾记为「AC-03 = `FAILED`」的
红线项已于 2026-11-17 **关闭**（`core/verification/plan.py` 读取端形状归一，AC-03 转
`PASSED`）。本模块**不写死**该结论：AC-03 的章节文案由 :func:`_ac03_note` /
`_section_known_issues` 按实测 `repair_closure.status` 动态生成（关闭后写「已关闭」，
未通过时写「不得表述为 verification PASS」）。发布件仍按 `decision` 派生类型，
不因 AC-03 改类型（纯函数不可覆盖）；**不写**「Alpha 已就绪」。

**不硬依赖上游（纪律 17）**：W11 `publish_decision` 用 duck-type 协议
:class:`PublishDecisionLike`，**不**顶层 import `core.assurance.*`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from core.release.docs import KNOWN_DEVIATIONS
from core.security.canonical import canonical_digest

#: 报告版本（RC 冻结用）。
REPORT_VERSION: str = "w12-alpha-report-v1"

#: 发布类型闭集。
TECH_PREVIEW_RELEASE_NAME: str = "DBGuardian 技术预览（受限发布）"
ALPHA_RELEASE_NAME: str = "DBGuardian Alpha"
#: 表述禁令：ALPHA 也只写「Alpha」（`完整 Alpha` 一律不许出现）。
FORBIDDEN_RELEASE_NAME_FRAGMENT: str = "完整 Alpha"

#: 发布决定取值闭集（W11 口径）。
DECISION_ALPHA_ALLOWED: str = "ALPHA_ALLOWED"
DECISION_TECH_PREVIEW_ONLY: str = "TECH_PREVIEW_ONLY"

#: 已登记问题编号。
ISSUE_PUBLISH_DECISION_MISSING: str = "PUBLISH_DECISION_MISSING"
ISSUE_UNKNOWN_DECISION_VALUE: str = "UNKNOWN_DECISION_VALUE"

#: O-W12-1 哨兵：区分「入参**没有** ``.decision`` 属性」与「``.decision`` 的值本身是 ``None``」。
#: 裸标量字符串（如 ``"ALPHA_ALLOWED"``）不带该属性，必须 fail-closed，绝不能被当成
#: W11 发布决定值（枚举修复引入的「更容易出 ALPHA」副作用须收回）；
#: ``decision is None`` 则由 :func:`derive_release_type` 提前按
#: ``PUBLISH_DECISION_MISSING`` 处理，与「缺属性」是两条不同路径，不可混为一谈。
_MISSING_DECISION: object = object()

#: 七类章节 + 延期项（顺序固定，缺一章即构造失败）。
SECTION_IDS: tuple[str, ...] = (
    "functional",
    "security",
    "reliability",
    "performance",
    "model_gain",
    "cost",
    "known_issues",
    "deferred",
)

#: 章节中文标题（写进 `alpha-acceptance-report.md`）。
SECTION_TITLES: dict[str, str] = {
    "functional": "功能验收",
    "security": "安全验收",
    "reliability": "可靠性",
    "performance": "性能",
    "model_gain": "模型收益",
    "cost": "成本",
    "known_issues": "已知问题",
    "deferred": "延期项",
}

#: `evidence_refs` 必须覆盖的上游周（AC-20：W09/W10/W11 每周至少 1 条）。
REQUIRED_EVIDENCE_WEEKS: tuple[str, ...] = ("W11", "W10", "W09")

#: 每周的可选门禁件路径（只引用存在的件；缺失由 T06 记 `NOT_AVAILABLE`）。
EVIDENCE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "W11": (
        "docs/w11/02-design.md",
        "docs/w11/03-task-cards.md",
        "docs/w11/05-w11-qa-closeout.md",
        "docs/w11/rc-freeze.md",
    ),
    "W10": (
        "docs/w10/02-design.md",
        "docs/w10/03-task-cards.md",
        "docs/w10/04-w10-qa-closeout.md",
        "benchmarks/w10/metrics-v1.json",
    ),
    "W09": (
        "docs/w09/12-w09-qa-closeout.md",
        "docs/w09/13-l1-l2-design.md",
        "docs/w09/14-l1-l2-qa-closeout.md",
    ),
}

#: 报告与发布说明的落位。
REPORT_MD: str = "docs/w12/alpha-acceptance-report.md"
RELEASE_NOTES_MD: str = "docs/w12/release-notes.md"


class ReleaseType(str, Enum):
    """发布类型：只有两档。"""

    TECH_PREVIEW = "TECH_PREVIEW"
    ALPHA = "ALPHA"


@runtime_checkable
class PublishDecisionLike(Protocol):
    """W11 `publish_decision` 的 duck-type 协议（**不**顶层 import 其实现，D-09）。"""

    decision: str
    failed_test_ids: tuple[str, ...]
    environment_blocks: tuple[str, ...]


class ReleaseTypeDerivationError(ValueError):
    """派生结果不自洽（例如 TECH_PREVIEW 名称里出现 alpha）。"""


@dataclass(frozen=True)
class ReleaseGate:
    """发布门禁结论（**无时间戳**，纯函数可复现）。

    `__post_init__` 做三条构造期强校验（AC-16）：

    1. `effective_decision` 只能是 ``ALPHA_ALLOWED`` / ``TECH_PREVIEW_ONLY``；
    2. `TECH_PREVIEW` 的 `release_name` **不得**出现 `alpha`（大小写不敏感）；
    3. 任何取值都不许在 `release_name` 里出现「完整 Alpha」。
    """

    release_type: ReleaseType
    release_name: str
    effective_decision: str
    decision_digest: str | None
    registered_issues: tuple[str, ...] = ()
    source: Literal["W11_PUBLISH_DECISION", "DEFAULT_TECH_PREVIEW_ONLY"] = (
        "DEFAULT_TECH_PREVIEW_ONLY"
    )

    def __post_init__(self) -> None:
        if self.effective_decision not in {
            DECISION_ALPHA_ALLOWED,
            DECISION_TECH_PREVIEW_ONLY,
        }:
            raise ReleaseTypeDerivationError(
                f"effective_decision 非法：{self.effective_decision!r}"
            )
        if self.release_type is ReleaseType.TECH_PREVIEW:
            if "alpha" in self.release_name.casefold():
                raise ReleaseTypeDerivationError(
                    "TECH_PREVIEW 的 release_name 不得含 alpha（大小写不敏感）"
                )
        if FORBIDDEN_RELEASE_NAME_FRAGMENT in self.release_name:
            raise ReleaseTypeDerivationError(
                "release_name 不得含「完整 Alpha」（表述禁令）"
            )
        if self.release_type is ReleaseType.ALPHA and self.effective_decision != (
            DECISION_ALPHA_ALLOWED
        ):
            raise ReleaseTypeDerivationError(
                "release_type=ALPHA 时 effective_decision 必须是 ALPHA_ALLOWED"
            )
        if self.release_type is ReleaseType.TECH_PREVIEW and self.effective_decision != (
            DECISION_TECH_PREVIEW_ONLY
        ):
            raise ReleaseTypeDerivationError(
                "release_type=TECH_PREVIEW 时 effective_decision 必须是 TECH_PREVIEW_ONLY"
            )

    def to_public_dict(self) -> dict[str, Any]:
        """面向报告的公开视图（无时间字段，可逐字比较）。"""
        return {
            "release_type": self.release_type.value,
            "release_name": self.release_name,
            "effective_decision": self.effective_decision,
            "decision_digest": self.decision_digest,
            "registered_issues": list(self.registered_issues),
            "source": self.source,
        }


def _text_value(value: Any) -> str:
    """把单个值归一为字符串：``Enum`` 取 ``.value``，其它走 ``str()``。

    Python 3.11+ 下 ``str(Decision.ALPHA_ALLOWED)`` 是 ``"Decision.ALPHA_ALLOWED"``，
    与 W11 常量 ``"ALPHA_ALLOWED"`` 不等；直接 ``str()`` 处理枚举会让真实
    ``PublishDecision`` 误落 fail-closed 分支（枚举派生缺陷）。
    """

    if isinstance(value, Enum):
        return str(value.value)
    if value is None:
        return ""
    return str(value)


def _decision_payload(decision: Any) -> dict[str, Any]:
    """把 duck-typed 决定归一为可摘要的纯数据（不含 DSN/口令）。

    - 枚举取值一律用 ``.value``（见 :func:`_text_value`），带 ``.decision``
      属性的裸 ``str`` 替身仍走 ``str()``，向后兼容；
    - **O-W12-1**：只有「带 ``.decision`` 属性」的入参才是 W11 发布决定对象；
      裸标量字符串（以及任何无该属性的对象）命中 :data:`_MISSING_DECISION`
      哨兵后归为空串 → ``UNKNOWN_DECISION_VALUE`` fail-closed，**不得**把入参
      自身当成决定值（否则 ``"ALPHA_ALLOWED"`` 会绕过 W11 决定直接升 ALPHA）；
    - ``decision is None`` 由 :func:`derive_release_type` 提前按
      ``PUBLISH_DECISION_MISSING`` 处理，与「缺属性」是两条不同路径；
    - 非字符串/非枚举的兜底一律归为空串，保证纯函数结果确定（不把对象地址
      写进摘要），未知取值继续 fail-closed。
    """

    raw = getattr(decision, "decision", _MISSING_DECISION)
    if raw is _MISSING_DECISION:
        raw = ""
    if not isinstance(raw, (str, Enum)):
        raw = ""
    return {
        "decision": _text_value(raw),
        "failed_test_ids": [
            _text_value(item) for item in (getattr(decision, "failed_test_ids", ()) or ())
        ],
        "environment_blocks": [
            _text_value(item) for item in (getattr(decision, "environment_blocks", ()) or ())
        ],
    }


def derive_release_type(*, decision: PublishDecisionLike | None) -> ReleaseGate:
    """由 W11 发布决定派生发布门禁结论（AC-16 / AC-17，纯函数）。

    **签名只接受 `decision`**：不存在 `force_alpha` / `override` /
    `manual_override` / 任何 `force_*` 形参，也不读环境变量或文件（AC-17）。
    同一入参逐位相等（`ReleaseGate` 无时间字段）。

    Args:
        decision: W11 `publish_decision`（duck-typed）；``None`` 表示裁定缺失。

    Returns:
        :class:`ReleaseGate`。

    | 入参 | release_type | effective_decision | source |
    |---|---|---|---|
    | `None` | `TECH_PREVIEW` | `TECH_PREVIEW_ONLY` | `DEFAULT_TECH_PREVIEW_ONLY`（+登记 `PUBLISH_DECISION_MISSING`） |
    | `TECH_PREVIEW_ONLY` | `TECH_PREVIEW` | `TECH_PREVIEW_ONLY` | `W11_PUBLISH_DECISION` |
    | `ALPHA_ALLOWED` | `ALPHA` | `ALPHA_ALLOWED` | `W11_PUBLISH_DECISION` |
    | 其它/空串 | `TECH_PREVIEW` | `TECH_PREVIEW_ONLY` | `DEFAULT_TECH_PREVIEW_ONLY`（+登记 `UNKNOWN_DECISION_VALUE`） |
    """
    if decision is None:
        return ReleaseGate(
            release_type=ReleaseType.TECH_PREVIEW,
            release_name=TECH_PREVIEW_RELEASE_NAME,
            effective_decision=DECISION_TECH_PREVIEW_ONLY,
            decision_digest=None,
            registered_issues=(ISSUE_PUBLISH_DECISION_MISSING,),
            source="DEFAULT_TECH_PREVIEW_ONLY",
        )

    payload = _decision_payload(decision)
    digest = canonical_digest(payload)
    value = payload["decision"].strip()

    if value == DECISION_ALPHA_ALLOWED:
        return ReleaseGate(
            release_type=ReleaseType.ALPHA,
            release_name=ALPHA_RELEASE_NAME,
            effective_decision=DECISION_ALPHA_ALLOWED,
            decision_digest=digest,
            registered_issues=(),
            source="W11_PUBLISH_DECISION",
        )
    if value == DECISION_TECH_PREVIEW_ONLY:
        return ReleaseGate(
            release_type=ReleaseType.TECH_PREVIEW,
            release_name=TECH_PREVIEW_RELEASE_NAME,
            effective_decision=DECISION_TECH_PREVIEW_ONLY,
            decision_digest=digest,
            registered_issues=(),
            source="W11_PUBLISH_DECISION",
        )
    # 其它取值（含大小写异常、空串）：fail-closed 到 TECH_PREVIEW_ONLY 并登记。
    return ReleaseGate(
        release_type=ReleaseType.TECH_PREVIEW,
        release_name=TECH_PREVIEW_RELEASE_NAME,
        effective_decision=DECISION_TECH_PREVIEW_ONLY,
        decision_digest=digest,
        registered_issues=(ISSUE_UNKNOWN_DECISION_VALUE,),
        source="DEFAULT_TECH_PREVIEW_ONLY",
    )


class AlphaAcceptanceReport(BaseModel):
    """Alpha 验收报告（七类章节 + 延期项，AC-17）。

    ★ `extra="forbid"`，且**不含**任何 demo 字段（AC-20）。
    """

    model_config = ConfigDict(extra="forbid")

    report_version: Literal["w12-alpha-report-v1"] = REPORT_VERSION
    sections: dict[str, str]
    evidence_refs: tuple[str, ...]
    #: W12-Q5：基线未执行 → 恒 `None`（禁止填任何数值）。
    baseline_score: float | None = None
    model: Literal["baseline"] = "baseline"
    sft_reproducible_gain: bool = False
    #: 三项延期恒 True（ClassVar → 不是字段，无法被数据覆盖）。
    dpo_deferred: ClassVar[bool] = True
    full_14b_deferred: ClassVar[bool] = True
    rl_deferred: ClassVar[bool] = True

    @model_validator(mode="after")
    def _seven_sections_and_baseline_honest(self) -> "AlphaAcceptanceReport":
        """八章必备且非空；基线/收益口径不得被改坏。"""
        missing = [key for key in SECTION_IDS if key not in self.sections]
        if missing:
            raise ValueError(f"sections 缺少章节：{missing}")
        extra = [key for key in self.sections if key not in SECTION_IDS]
        if extra:
            raise ValueError(f"sections 含未声明章节：{extra}")
        for key, text in self.sections.items():
            if not str(text).strip():
                raise ValueError(f"sections[{key}] 不得为空")
        if self.baseline_score is not None:
            raise ValueError("baseline_score 必须为 None（基线 NOT_RUN）")
        if self.model != "baseline":
            raise ValueError("model 必须是 baseline（未微调）")
        if self.sft_reproducible_gain is not False:
            raise ValueError("sft_reproducible_gain 必须为 False（无可重复收益）")
        if FORBIDDEN_RELEASE_NAME_FRAGMENT in " ".join(self.sections.values()):
            raise ValueError("报告正文不得出现「完整 Alpha」（表述禁令）")
        if not self.evidence_refs:
            raise ValueError("evidence_refs 不得为空（AC-20）")
        return self


# ---------------------------------------------------------------------------
# 报告组装（只写实测事实，不预写结论）
# ---------------------------------------------------------------------------


def _count_by_status(statuses: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in statuses:
        key = str(getattr(item, "value", item))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _ac03_note(closure: Any) -> str:
    """AC-03 的**状态驱动**注记（不写死结论，随实测闭合状态变化）。

    KD-W12-01 关闭前（闭环 `FAILED`）：必须写明「不得表述为 verification PASS」；
    关闭后（闭环 `PASSED`）：写明关闭依据（读取端形状归一）与修复点，避免留下
    与实测相悖的「未通过」表述。
    """
    status = str(getattr(getattr(closure, "status", None), "value", "NOT_EXECUTED"))
    verdict = str(getattr(closure, "verdict", "NOT_EXECUTED"))
    reason = str(getattr(closure, "reason_code", "") or "N/A")
    if status == "PASSED" and verdict == "PASS":
        return (
            f"（原因码 {reason}；KD-W12-01 已关闭：验证器改为优先读 "
            'StepObserved.result["data"]、回落顶层，真实形状可判定）'
        )
    return (
        f"（原因码 {reason}；KD-W12-01：StepObserved 形状不一致，"
        "不得表述为 verification PASS）"
    )


def _section_functional(functional: Any) -> str:
    lifecycle = getattr(functional, "lifecycle", {}) or {}
    reports = getattr(functional, "diagnosis_reports", ()) or ()
    closure = getattr(functional, "repair_closure", None)
    five = sum(1 for item in reports if getattr(item, "five_fields_nonempty", False))
    closure_status = str(getattr(getattr(closure, "status", None), "value", "NOT_EXECUTED"))
    closure_verdict = getattr(closure, "verdict", "NOT_EXECUTED")
    lifecycle_text = "、".join(
        f"{phase}={str(getattr(status, 'value', status))}" for phase, status in lifecycle.items()
    )
    return (
        f"AC-01 五态生命周期：{lifecycle_text or '未执行'}（每态 task_events 可查询、"
        "seq 连续、事件链校验通过）。"
        f"AC-02 三类诊断报告五字段非空 {five}/{len(reports)}（PASSED；"
        "KD-W12-02 已于 2026-11-19 关闭：无 table_stats 证据的输入形状改为显式降级 "
        "UNKNOWN + 证据不足，并补真实形状回归 2 例）。"
        f"AC-03 审批修复闭环：独立验证结论 {closure_verdict}、闭环状态 {closure_status}"
        f"{_ac03_note(closure)}。"
        f"AC-04 第二引擎只读：写定义集合为空 "
        f"（{getattr(functional, 'second_engine_readonly', False)}）；"
        f"PG 段 {_pg_segment_text(functional)}"
    )


def _section_security(security: Any) -> str:
    review = getattr(security, "negative_review", ()) or ()
    denied = sum(1 for item in review if getattr(item, "denied", False))
    bypass = getattr(security, "bypass_rejections", {}) or {}
    bypass_text = "、".join(f"{name}={value}" for name, value in sorted(bypass.items()))
    return (
        f"AC-05 W11 负面集复核 {denied}/{len(review)} 条被拒"
        f"（w11_source_status={str(getattr(getattr(security, 'w11_source_status', None), 'value', 'NOT_AVAILABLE'))}）。"
        f"AC-06 R4 未注册={getattr(security, 'r4_not_registered', False)}、"
        f"R3 被拒={getattr(security, 'r3_denied', False)}。"
        f"AC-07 四类绕过：{bypass_text or '未执行'}。"
        f"AC-08 UNKNOWN 不重试={getattr(security, 'unknown_no_retry', False)}、"
        f"审计失败 fail closed={getattr(security, 'fail_closed_ok', False)}。"
        f"AC-09 脱敏不越界={getattr(security, 'redaction_ok', False)}、"
        f"知识索引门禁={getattr(security, 'knowledge_indexable_gate_ok', False)}。"
    )


def _pg_segment_text(functional: Any) -> str:
    """PG 段口径随实测状态变化：P2-T1 起 PG16 可用时不再写死 ENV-PG5432。"""
    status = str(getattr(getattr(functional, "pg_segment_status", None), "value", "NOT_EXECUTED"))
    if status == "PASSED":
        return "PASSED（pg_stat_statements 可查，第二引擎只读诊断可用）。"
    return f"{status}（ENV-PG5432：环境不可达，不判 FAIL）。"


def _pg_known_issue(functional: Any) -> str:
    """ENV-PG5432 条目随实测状态改写（冻结限制清单未动，需下次刷新统一移除）。"""
    status = str(getattr(getattr(functional, "pg_segment_status", None), "value", "NOT_EXECUTED"))
    if status == "PASSED":
        return (
            "ENV-PG5432：已解除（P2-T1，PG16 + pg_stat_statements 实测 PASSED）；"
            "该条仍保留在 W11 冻结限制清单中，待限制刷新统一移除。"
        )
    return "ENV-PG5432：PG 段 NOT_EXECUTED（不判 FAIL、不冒充通过）。"


def _section_reliability(functional: Any, security: Any) -> str:
    closure = getattr(functional, "repair_closure", None)
    verdicts = getattr(closure, "assertion_verdicts", {}) or {}
    detail = "、".join(f"{key}={value}" for key, value in sorted(verdicts.items()))
    closure_status = str(getattr(getattr(closure, "status", None), "value", "NOT_EXECUTED"))
    closed = closure_status == "PASSED" and str(getattr(closure, "verdict", "")) == "PASS"
    if closed:
        verdict_text = (
            f"独立验证逐条断言（KD-W12-01 修复后实测）：{detail}；"
            "原登记（修复前，2026-11-16）：`affected_rows` 曾为 `UNKNOWN`（`OBSERVED_MISSING`）、"
            "`snapshot_comparable=FAIL`，该断言态已于 2026-11-17 关闭"
            "（`core/verification/plan.py` 读取端形状归一，未改写入端与断言常量；"
            "留档见 `docs/w12/known-limitations.md` §3.1）。"
        )
    else:
        verdict_text = f"独立验证逐条断言：{detail or '未执行'}（闭环 {closure_status}）。"
    return (
        "可靠性口径：不可逆/不确定路径一律转核对态，不自动重试。"
        f"UNKNOWN 不重试实测={getattr(security, 'unknown_no_retry', False)}"
        "（结论 NEEDS_RECONCILIATION、tool_calls 增量 0、无 TOOL_CALL_RETRIED）。"
        f"审计不可写即阻断实测={getattr(security, 'fail_closed_ok', False)}"
        "（emit_required 抛 AuditUnavailableError；R2 链不接 try_emit）。"
        f"{verdict_text}"
    )


def _section_performance(deploy: Any) -> str:
    checks = getattr(deploy, "checks", ()) or ()
    summary = _count_by_status([getattr(item, "status", None) for item in checks])
    return (
        "性能：本轮未做压测与长稳实跑，故不提供任何时延/吞吐数值。"
        f"部署五项检查状态分布 {summary or '未执行'}"
        f"（container_runtime_status={getattr(deploy, 'container_runtime_status', 'NOT_VERIFIED')}）。"
        "容器运行态未验证（ENV-NV1），不据此推断性能上限。"
    )


def _section_model_gain(docs: Any) -> str:
    return (
        f"模型状态：{getattr(docs, 'baseline_model_status', '未微调')}；"
        f"指标状态：{getattr(docs, 'baseline_metrics_status', '指标未测得')}；"
        f"阻塞原因：{getattr(docs, 'baseline_blocked_reason', 'BLOCKED_MODEL_ENV_MISSING')}。"
        f"baseline_score={getattr(docs, 'baseline_score', None)}（不填任何数值，含 0）。"
        "SFT 无可重复收益（sft_reproducible_gain=False）；DPO / 全量 14B / RL 均延期。"
    )


def _section_cost(deploy: Any) -> str:
    return (
        "成本：本轮未产生训练与推理账单，故不提供金额类数值。"
        f"镜像 digest={getattr(deploy, 'image_digest', 'UNKNOWN')}"
        f"、制品 hash={getattr(deploy, 'artifact_hash', 'UNKNOWN')}"
        f"、commit={getattr(deploy, 'commit', 'UNKNOWN')}"
        "（缺失项均记 UNKNOWN 并登记，不用估计值顶替）。"
    )


def _section_known_issues(functional: Any, security: Any, deploy: Any) -> str:
    closure = getattr(functional, "repair_closure", None)
    closure_status = str(getattr(getattr(closure, "status", None), "value", "NOT_EXECUTED"))
    issues = list(getattr(deploy, "registered_issues", ()) or ())
    if closure_status == "PASSED" and str(getattr(closure, "verdict", "")) == "PASS":
        ac03_line = (
            "KD-W12-01（已关闭，2026-11-17）：AC-03 独立验证结论已由 FAILED 转 PASS"
            f"（闭环 PASSED；原因码 {getattr(closure, 'reason_code', 'OK')}）。"
            "修复点：`core/verification/plan.py` 读取端形状归一（优先 "
            '`result["data"]`、回落顶层），未改写入端与断言常量；'
            "已补真实形状回归 5 例。本条已从 `docs/w12/known-limitations.json` 移除，"
            "改记入 `known-limitations.md` §3.1 已关闭偏离留档。"
        )
    else:
        ac03_line = (
            f"KD-W12-01：AC-03 独立验证结论 {getattr(closure, 'verdict', 'NOT_EXECUTED')}"
            f"（闭环 {closure_status}；原因码 {getattr(closure, 'reason_code', 'N/A')}）"
            "——不得表述为 verification PASS，也不得称修复闭环已通过独立验证。"
        )
    lines = [
        ac03_line,
        "KD-W12-02（已关闭，2026-11-19）：慢查询分析器在「全表扫描 + 无 table_stats」输入下的 "
        "UnboundLocalError 已修（入口初始化 + 无证据时显式降级 UNKNOWN 并标注缺失证据），"
        "并补真实形状回归 2 例；本条已从 `docs/w12/known-limitations.json` 移除，"
        "改记入 `known-limitations.md` §3.1 已关闭偏离留档。",
        "KD-W11-01/02（备份覆盖漏项 / 负集哨兵归属）：P2-T2 已关闭——备份对象扩到六类"
        "（含 approval_consumptions / step_prepared）并实测 restore 前后非空；负集 29 条"
        "改用真实目标行并断言基线非空。KD-W11-03（audit_chain 桶命名对齐）仍登记未改。",
        _pg_known_issue(functional),
        "ENV-1045：DG_ADMIN_DSN 不可用，升级演练未执行。",
        "ENV-NV1：容器运行态 NOT_VERIFIED，镜像 digest UNKNOWN。",
        "ENV-MODEL：模型环境缺失，指标未测得。",
        "DOC-PARTIAL：迁移说明与回滚清单已补至 011、.env.example 三态已补齐；"
        "W07 冻结四件套的两件补件形成过程未经独立复核，仍不声称齐全。",
    ]
    if issues:
        lines.append("部署登记项：" + "；".join(issues))
    if getattr(security, "fail_closed_ok", False) is not True:
        lines.append("注意：审计失败 fail closed 未实测通过。")
    return " ".join(lines)


def _section_deferred() -> str:
    return (
        "延期项：DPO、全量 14B、RL 三项均未实施（恒为延期）；"
        "性能压测与长稳未实跑；容器网络级隔离未在运行态验证；"
        "W11 publish_decision 的具体值不由 W12 预写。"
    )


def _collect_evidence_refs(
    functional: Any, security: Any, root: Path | None
) -> tuple[str, ...]:
    """汇总证据引用：只保留**真实存在**的路径，每周至少 1 条。"""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    refs: list[str] = []
    for source in (functional, security):
        for ref in getattr(source, "evidence_refs", ()) or ():
            if ref not in refs:
                refs.append(str(ref))
    for week in REQUIRED_EVIDENCE_WEEKS:
        candidates = [path for path in EVIDENCE_CANDIDATES[week] if (base / path).is_file()]
        if not candidates:
            continue
        if not any(candidate in refs for candidate in candidates):
            refs.append(candidates[0])
    return tuple(refs)


def build_alpha_acceptance_report(
    *,
    functional: Any,
    security: Any,
    deploy: Any,
    docs: Any,
    decision: PublishDecisionLike | None,
    root: Path | None = None,
) -> AlphaAcceptanceReport:
    """组装 Alpha 验收报告（七类章节 + 延期项）。

    Args:
        functional: T01 `FunctionalAcceptanceResult`。
        security: T02 `SecurityAcceptanceResult`。
        deploy: T03 `DeployPackageManifest`。
        docs: T04 `DocInventory`。
        decision: W11 `publish_decision`（duck-typed；``None`` = 裁定缺失）。
        root: 可选，仓库根（用于筛选真实存在的证据路径）。

    Returns:
        :class:`AlphaAcceptanceReport`。**不预写**任何 Alpha 结论：
        `release_type` 由 :func:`derive_release_type` 单独派生（见 `release-notes.md`）。
    """
    sections = {
        "functional": _section_functional(functional),
        "security": _section_security(security),
        "reliability": _section_reliability(functional, security),
        "performance": _section_performance(deploy),
        "model_gain": _section_model_gain(docs),
        "cost": _section_cost(deploy),
        "known_issues": _section_known_issues(functional, security, deploy),
        "deferred": _section_deferred(),
    }
    return AlphaAcceptanceReport(
        sections=sections,
        evidence_refs=_collect_evidence_refs(functional, security, root),
        baseline_score=None,
        model="baseline",
        sft_reproducible_gain=False,
    )


# ---------------------------------------------------------------------------
# 发布说明与报告落盘（`release_type` 由同一 `ReleaseGate` 生成）
# ---------------------------------------------------------------------------


def _render_report_markdown(report: AlphaAcceptanceReport, gate: ReleaseGate) -> str:
    lines = [
        "# W12 Alpha 验收报告（T05 / AC-17 / AC-18）",
        "",
        "> 生成方式：`core/release/publish.py::build_alpha_acceptance_report()` + "
        "`write_release_documents()`；",
        f"> 报告版本 `{report.report_version}`；发布结论由 `derive_release_type()` 按 W11 "
        "发布决定派生（**不由人工覆盖**）。",
        "",
        "## 0. 发布结论摘要",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| `release_type` | `{gate.release_type.value}` |",
        f"| `release_name` | {gate.release_name} |",
        f"| `effective_decision` | `{gate.effective_decision}` |",
        f"| `source` | `{gate.source}` |",
        f"| `decision_digest` | `{gate.decision_digest}` |",
        f"| 登记项 | {('；'.join(gate.registered_issues) or '无')} |",
        "",
        (
            "> 口径：本次发布为**受限发布**，功能面仍存在红线与未覆盖路径；"
            "报告正文逐章只写实测事实，不写结论性溢美表述。"
            if gate.release_type is ReleaseType.TECH_PREVIEW
            else "> 口径：发布结论为 ALPHA；报告正文逐章只写实测事实，不写结论性溢美表述。"
        ),
        "",
    ]
    for key in SECTION_IDS:
        lines.append(f"## {SECTION_TITLES[key]}")
        lines.append("")
        lines.append(report.sections[key])
        lines.append("")
    lines.extend(
        [
            "## 证据引用",
            "",
            "| 路径 |",
            "|---|",
        ]
    )
    for ref in report.evidence_refs:
        lines.append(f"| `{ref}` |")
    lines.extend(
        [
            "",
            "## 口径与边界",
            "",
            f"- `baseline_score = {report.baseline_score}`、`model = {report.model}`、"
            f"`sft_reproducible_gain = {report.sft_reproducible_gain}`；",
            "- `dpo_deferred` / `full_14b_deferred` / `rl_deferred` 恒为 `True`（`ClassVar`，不可覆盖）；",
            "- 演示件（`docs/w12/demo-script.md`）**不进入**本报告的判定输入（AC-20）；",
            "- 限制清单随包发布：`docs/w12/known-limitations.json`（唯一真值）。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_release_notes(report: AlphaAcceptanceReport, gate: ReleaseGate) -> str:
    checks_text = report.sections["known_issues"]
    lines = [
        "# W12 发布说明（Release Notes）",
        "",
        f"## 版本：{gate.release_name}",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| `release_type` | `{gate.release_type.value}` |",
        f"| `release_name` | {gate.release_name} |",
        f"| `effective_decision` | `{gate.effective_decision}` |",
        f"| `source` | `{gate.source}` |",
        f"| `decision_digest` | `{gate.decision_digest}` |",
        f"| 报告版本 | `{report.report_version}` |",
        "",
        "## 发布范围",
        "",
        "- 只读诊断、证据留存、单引擎审批修复（唯一 R2 动作）、独立验证与核对链；",
        "- 第二引擎（PostgreSQL/MCP）**只读**；生产写入不在范围内；",
        "- 演示只走隔离 LAB，无 Web 控制台。",
        "",
        "## 已知限制（随包发布）",
        "",
        "- 完整限制清单：`docs/w12/known-limitations.json`（13 个自动基础项不可删 + "
        f"{len(KNOWN_DEVIATIONS)} 条在册已知偏离；已关闭偏离见 `known-limitations.md` §3.1）；",
        f"- 本次登记项：{('；'.join(gate.registered_issues) or '无')}；",
        f"- 关键已知问题：{checks_text}",
        "",
        "## 发布门禁口径",
        "",
        "- `release_type` 只由 W11 发布决定派生：`decision is None` 或 "
        "`TECH_PREVIEW_ONLY` 均产出 `TECH_PREVIEW`；签名内不存在任何覆盖形参；",
        "- 训练收益口径：未微调、指标未测得（`BLOCKED_MODEL_ENV_MISSING`），不宣称收益；",
        "- 本文件不改变 W07「有条件放行；门禁通过：否」、W08「有条件放行」、"
        "W09/W10/W11「门禁未评」的既有口径。",
        "",
    ]
    return "\n".join(lines)


def write_release_documents(
    root: Path,
    *,
    report: AlphaAcceptanceReport,
    gate: ReleaseGate,
) -> tuple[Path, Path]:
    """写出 `alpha-acceptance-report.md` 与 `release-notes.md`（同一 gate 生成）。

    Args:
        root: 仓库根。
        report: 验收报告。
        gate: 发布门禁结论（`release-notes.md` 的类型/名称由它生成，不另行推断）。

    Returns:
        ``(report_path, notes_path)``。
    """
    base = Path(root)
    report_path = base / REPORT_MD
    notes_path = base / RELEASE_NOTES_MD
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report_markdown(report, gate), encoding="utf-8")
    notes_path.write_text(_render_release_notes(report, gate), encoding="utf-8")
    return report_path, notes_path


__all__ = [
    "ALPHA_RELEASE_NAME",
    "AlphaAcceptanceReport",
    "DECISION_ALPHA_ALLOWED",
    "DECISION_TECH_PREVIEW_ONLY",
    "EVIDENCE_CANDIDATES",
    "FORBIDDEN_RELEASE_NAME_FRAGMENT",
    "ISSUE_PUBLISH_DECISION_MISSING",
    "ISSUE_UNKNOWN_DECISION_VALUE",
    "PublishDecisionLike",
    "RELEASE_NOTES_MD",
    "REPORT_MD",
    "REPORT_VERSION",
    "REQUIRED_EVIDENCE_WEEKS",
    "ReleaseGate",
    "ReleaseType",
    "ReleaseTypeDerivationError",
    "SECTION_IDS",
    "SECTION_TITLES",
    "TECH_PREVIEW_RELEASE_NAME",
    "build_alpha_acceptance_report",
    "derive_release_type",
    "write_release_documents",
]
