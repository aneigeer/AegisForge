"""W09 T03 结构化报告领域模型（全部 ``extra="forbid"``）。

设计依据：`docs/w09/02-design.md` §4.1/§4.2/§4.4、`docs/w09/03-task-cards.md:305-395`。

要点：

- ``FactStatus`` 6 值 / ``ActionState`` 6 值 / ``VerificationState`` 5 值，封闭枚举、
  两两不等价；不新增第 7 态。
- ``ActionState`` 只能由证据链阶段唯一确定（见 ``core/report/validate.py`` 的
  ``derive_chain_stage`` / ``derive_action_state``），模型层不做布尔组合推导。
- ``fact_status=CONFIRMED`` 时，模型校验器用模型自身携带的事实输入**主动复核**一次，
  防止手工构造 ``StructuredReport(...)`` 绕过 ``derive_fact_status`` 的推导。
- ``VERIFIED`` ⇒ ``verification_state == PASS`` 且 ``verification_result_id`` 非空；
  ``PROPOSED`` ⇒ 建议非空且全为 ``PROPOSED``；``STALE`` ⇒ ``missing_evidence`` 非空。
- 措辞禁令（AC-11）在模型层强制：建议文本与未执行态结论不得含完成措辞。

相较于任务卡「关键签名」新增两个字段，均为实现必须语义所必需：

- ``weak_evidence_ids``：让 ``CONFIRMED`` 的主动复核有可核对的事实输入；
- ``published``：承载 ``for_document`` 的 ``REVIEW_REQUIRED`` 口径（``published=False``，
  设计 §3 第 5 行 / ``03-task-cards.md:203``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.diagnosis.models import ConfidenceBand
from core.redaction.models import RedactionSummary
from core.tasks.models import utc_now


class FactStatus(str, Enum):
    """事实等级（AC-09/AC-10/AC-12），按设计 §4.2 优先级表首个命中。"""

    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"
    CONFLICTED = "CONFLICTED"


class ActionState(str, Enum):
    """动作执行状态（AC-11 + P0-03.1），按证据链阶段唯一确定，不得跳级。"""

    PROPOSED = "PROPOSED"
    NOT_EXECUTED = "NOT_EXECUTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTED = "EXECUTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    VERIFIED = "VERIFIED"


class VerificationState(str, Enum):
    """验证状态，来自 ``VerificationResult.verdict``（W08 封闭三态 + 两个无结果态）。"""

    NONE = "NONE"
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


# 大小写不敏感的完成措辞黑名单（AC-11 / 设计 §4.4）。
FORBIDDEN_COMPLETION_TERMS: tuple[str, ...] = (
    "已修复",
    "已执行",
    "已完成",
    "已恢复",
    "已解决",
    "fixed",
    "resolved",
    "already applied",
)

_UNEXECUTED_ACTION_STATES: frozenset[ActionState] = frozenset(
    {
        ActionState.PROPOSED,
        ActionState.NOT_EXECUTED,
        ActionState.AWAITING_APPROVAL,
    }
)


def _has_forbidden_wording(value: str) -> bool:
    """大小写不敏感地检测完成措辞；命中即视为违规。"""
    lowered = value.casefold()
    return any(term.casefold() in lowered for term in FORBIDDEN_COMPLETION_TERMS)


@dataclass(frozen=True)
class FactInputs:
    """``derive_fact_status_from_inputs`` 的纯事实输入（不含原文、不含 DSN）。"""

    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    weak_ids: tuple[str, ...] = ()
    denied_ids: tuple[str, ...] = ()
    env_mismatch_ids: tuple[str, ...] = ()
    all_supporting_denied: bool = False
    verification_state: VerificationState = VerificationState.NONE


def derive_fact_status_from_inputs(inputs: FactInputs) -> FactStatus:
    """按设计 §4.2 的 6 条优先级表**首个命中**推导事实等级。

    顺序不可调整：失效引用优先于一切，弱证据不得升格为 ``CONFIRMED``。
    """
    if inputs.missing_evidence or inputs.env_mismatch_ids:
        return FactStatus.STALE
    if not inputs.supporting_evidence_ids:
        return FactStatus.UNKNOWN
    if inputs.all_supporting_denied:
        return FactStatus.DENIED
    if (
        inputs.contradicting_evidence_ids
        and inputs.verification_state is not VerificationState.PASS
    ):
        return FactStatus.CONFLICTED
    if inputs.weak_ids or inputs.denied_ids:
        return FactStatus.PARTIAL
    return FactStatus.CONFIRMED


class Recommendation(BaseModel):
    """一条建议；默认状态为 ``PROPOSED``，文本禁止写成已完成的动作。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    state: ActionState = ActionState.PROPOSED
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"
    requires_approval: bool = False

    @field_validator("text")
    @classmethod
    def _no_completion_wording(cls, value: str) -> str:
        if _has_forbidden_wording(value):
            raise ValueError("建议文本不得含完成措辞（AC-11）")
        return value


class StructuredReport(BaseModel):
    """结构化报告模板（设计 §4.1）；字段全部显式声明，禁止隐式旁路。"""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    report_version: str = "report-v1"
    tenant_id: str
    task_id: str
    database_id: str
    db_engine: str
    db_version: str
    adapter_version: str
    environment_id: str
    environment_fingerprint_digest: str | None = None
    policy_version: str
    conclusion: str = Field(min_length=1, max_length=2000)
    fact_status: FactStatus
    confidence_band: ConfidenceBand
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    weak_evidence_ids: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    action_state: ActionState
    verification_state: VerificationState
    verification_result_id: str | None = None
    scope: str = "LAB"
    degraded: bool = False
    degrade_reason: str | None = None
    redaction: RedactionSummary | None = None
    published: bool = False
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    def _fact_inputs(self) -> FactInputs:
        """用模型自身携带的事实输入复核 ``CONFIRMED``，不读取任何外部端口。"""
        return FactInputs(
            supporting_evidence_ids=tuple(self.supporting_evidence_ids),
            contradicting_evidence_ids=tuple(self.contradicting_evidence_ids),
            missing_evidence=tuple(self.missing_evidence),
            weak_ids=tuple(self.weak_evidence_ids),
            denied_ids=(),
            env_mismatch_ids=(),
            all_supporting_denied=False,
            verification_state=self.verification_state,
        )

    @model_validator(mode="after")
    def _enforce_state_consistency(self) -> "StructuredReport":
        """模型层不可绕过的状态一致性（设计 §4.2「模型层校验器」）。"""
        if self.fact_status is FactStatus.STALE and not self.missing_evidence:
            raise ValueError("STALE 报告必须显式记录 missing_evidence（AC-12）")
        if self.fact_status is FactStatus.CONFIRMED:
            recomputed = derive_fact_status_from_inputs(self._fact_inputs())
            if recomputed is not FactStatus.CONFIRMED:
                raise ValueError(
                    "CONFIRMED 主动复核未通过：存在失效引用、弱证据或未解决反证"
                )
        if self.action_state is ActionState.VERIFIED:
            if self.verification_state is not VerificationState.PASS:
                raise ValueError("VERIFIED 要求 verification_state == PASS")
            if not self.verification_result_id:
                raise ValueError("VERIFIED 要求 verification_result_id 非空")
        if self.action_state is ActionState.PROPOSED:
            if not self.recommendations:
                raise ValueError("PROPOSED 要求 recommendations 非空")
            if any(item.state is not ActionState.PROPOSED for item in self.recommendations):
                raise ValueError("PROPOSED 要求所有建议均为 PROPOSED")
        if (
            self.verification_state is VerificationState.UNKNOWN
            and self.action_state in (ActionState.EXECUTED, ActionState.VERIFIED)
        ):
            raise ValueError("verdict=UNKNOWN 不得映射为 EXECUTED / VERIFIED")
        if self.action_state in _UNEXECUTED_ACTION_STATES and _has_forbidden_wording(
            self.conclusion
        ):
            raise ValueError("未执行态结论不得含完成措辞（AC-11）")
        return self


__all__ = [
    "FORBIDDEN_COMPLETION_TERMS",
    "ActionState",
    "FactInputs",
    "FactStatus",
    "Recommendation",
    "StructuredReport",
    "VerificationState",
    "derive_fact_status_from_inputs",
]
