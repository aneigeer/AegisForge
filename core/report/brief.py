"""W09 T05 规则化简报：模型不可用时只基于已有证据给出事实摘要。

设计依据：``docs/w09/02-design.md`` §6.1、``docs/w09/03-task-cards.md:563-584``。

- 简报字段固定：``degraded=True``、``degrade_reason``、``model_used=False``、
  ``planning_suspended=True``、``fact_status ∈ {UNKNOWN, PARTIAL}``（**永不 CONFIRMED**）。
- ``observations`` 是逐条证据摘要的**再脱敏**文本：每条都经 ``gate`` 的 ``EVIDENCE``
  通道复扫，不把内部通道原文当作对外输入（KD-W09-03）。
- 含「模型已诊断 / 已修复」等完成措辞的摘要**不进入**简报；规则简报不得声称结论。
- 事实源全部来自已落库证据（``rules-v0`` 口径），不伪造模型结论。

KD-W09-03 如实登记（唯一对外口径来源：``docs/w09/07-pm-ruling.md`` §4.3 / §4.4）：
简报 ``observations`` 逐条经闸门 ``EVIDENCE`` 通道再脱敏；**不把**
``StepPrepared.request`` / ``TrajectoryRecorder`` 等内部通道原文当作对外通道输入。
本卡不改 ``redact_payload``、不升 ``redaction_version``（保持 ``redaction-v2``）、
不补冻结回归、不新增 AC；引用 ``docs/w09/06-qa-report.md`` §11.5 时必须并列引用
``docs/w09/07-pm-ruling.md`` §4。该发现项定性为**已声明的已知限制，不是本轮
FAIL**，W10 再议；对外不得写「脱敏全链路无死角 / PII 零落盘」。T06 的
「未开工、未验证」状态如实保留。
"""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from core.evidence.models import CollectionStatus, Evidence
from core.models.degradation import DegradeReason
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionResult, RedactionStatus, RedactionSummary
from core.redaction.payload_text import canonical_payload_hash
from core.report.models import FactStatus
from core.tasks.models import Task

# 规则简报不得出现的完成/诊断措辞（AC-16 / 卡 :590）。
FORBIDDEN_BRIEF_WORDING: tuple[str, ...] = (
    "模型已诊断",
    "已修复",
    "已执行",
    "已完成",
    "已恢复",
    "已解决",
)

_MISSING_STATUSES: frozenset[CollectionStatus] = frozenset(
    {
        CollectionStatus.DENIED,
        CollectionStatus.TIMEOUT,
        CollectionStatus.NOT_SUPPORTED,
        CollectionStatus.UNKNOWN,
    }
)

_SEVERITY: dict[RedactionStatus, int] = {
    RedactionStatus.CLEAN: 0,
    RedactionStatus.REDACTED: 1,
    RedactionStatus.REVIEW_REQUIRED: 2,
    RedactionStatus.BLOCKED: 3,
}


def _has_forbidden_wording(value: str) -> bool:
    return any(term in value for term in FORBIDDEN_BRIEF_WORDING)


def _summary(result: RedactionResult) -> RedactionSummary:
    return RedactionSummary(
        redaction_version=result.redaction_version,
        status=result.status,
        detected_categories=result.detected_categories,
        redaction_count=result.redaction_count,
        source_payload_hash=result.payload_hash,
        redacted_payload_hash=canonical_payload_hash(result.payload_redacted),
    )


class DegradedBrief(BaseModel):
    """降级简报；字段封闭，状态恒为「未诊断 / 未执行」。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    degraded: bool = True
    degrade_reason: DegradeReason
    model_used: bool = False
    planning_suspended: bool = True
    fact_status: FactStatus
    evidence_ids: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    redaction: RedactionSummary | None = None

    @model_validator(mode="after")
    def _enforce_degraded_semantics(self) -> "DegradedBrief":
        if self.degraded is not True:
            raise ValueError("规则简报必须 degraded=True")
        if self.model_used is not False:
            raise ValueError("规则简报不得声称使用过模型（model_used 必须为 False）")
        if self.planning_suspended is not True:
            raise ValueError("规则简报必须 planning_suspended=True")
        if self.fact_status not in (FactStatus.UNKNOWN, FactStatus.PARTIAL):
            raise ValueError("规则简报 fact_status 只允许 UNKNOWN / PARTIAL")
        if any(_has_forbidden_wording(item) for item in self.observations):
            raise ValueError("规则简报不得出现完成/诊断措辞")
        return self


def build_rule_brief(
    *,
    task: Task,
    evidence: Sequence[Evidence],
    summaries: Sequence[str],
    reason: DegradeReason,
    gate: RedactionGate,
) -> DegradedBrief:
    """基于已有证据生成规则简报；每条摘要经 ``gate`` 的 ``EVIDENCE`` 通道再脱敏。"""
    evidence_ids = tuple(item.evidence_id for item in evidence)
    missing = tuple(
        item.evidence_id
        for item in evidence
        if item.collection_status in _MISSING_STATUSES
    )
    observations: list[str] = []
    worst: RedactionResult | None = None
    for summary in summaries:
        scan = gate.scan_event(
            {"summary": str(summary)},
            task_id=task.task_id,
            tenant_id=task.tenant_id,
        )
        text = str(scan.payload_redacted.get("summary", ""))
        if _has_forbidden_wording(text):
            # 完成措辞不得进入规则简报（不伪造「已修复 / 模型已诊断」）。
            continue
        observations.append(text)
        if worst is None or _SEVERITY[scan.status] > _SEVERITY[worst.status]:
            worst = scan
    fact_status = FactStatus.PARTIAL if evidence else FactStatus.UNKNOWN
    return DegradedBrief(
        task_id=task.task_id,
        degrade_reason=reason,
        fact_status=fact_status,
        evidence_ids=evidence_ids,
        missing_evidence=missing,
        observations=tuple(observations),
        redaction=_summary(worst) if worst is not None else None,
    )


__all__ = ["DegradedBrief", "FORBIDDEN_BRIEF_WORDING", "build_rule_brief"]
