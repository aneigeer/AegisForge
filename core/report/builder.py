"""W09 T03 报告组装器：取证 → 推导 → 复扫 → 落快照 → 发 ``REPORT_GENERATED``。

设计 §4 / §3 第 5 行 / 任务卡 ``:366-371``。

- 引用校验走 ``inspect_evidence_refs``（只记录不抛），失效 id 一并进入
  ``missing_evidence`` 并降为 ``STALE``，绝不静默剔除后仍报 ``CONFIRMED``。
- ``ActionState`` 由证据链阶段唯一确定；``verdict=UNKNOWN`` 不映射成已执行/已验证。
- 报告本体（含 Markdown / HTML 渲染产物）在返回前走 ``REPORT`` 通道独立复扫；
  导出侧另有 ``EXPORT`` 通道复扫（AC-07 / ``PM 裁定 §4.4 第 2 条``）。
  KD-W09-03 的登记与口径见 ``core/report/export.py`` 模块文档；本卡的两次复扫
  都只吃报告自身字段，不把内部轨迹/工具入参原文当对外输入。
- ``REVIEW_REQUIRED`` → ``published=False``；``BLOCKED`` 由闸门抛错，不产出报告。
- 不改 ``core/evidence/**`` / ``core/gateway/**`` / ``apps/api/**``；本模块只通过
  注入端口读取证据链事实，不新增 API 端点。

端口说明（本卡不改组合根，故以 Protocol + 注入方式定义，测试用内存实现）：

- ``verifications.latest_for_task`` → ``VerificationResult | None``；
- ``steps.chain_for_task`` → ``StepChain``（审批状态 + step_prepared / step_observed 是否存在）；
- ``audit.emit_required`` → 追加 ``REPORT_GENERATED``；
- ``reports.insert`` → 只追加快照。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from core.audit.events import AuditEventType, make_event
from core.diagnosis.models import ConfidenceBand
from core.evidence.models import Evidence, ReportFacts
from core.redaction.models import (
    RedactionChannel,
    RedactionStatus,
    RedactionSummary,
)
from core.redaction.payload_text import canonical_payload_hash
from core.report.models import (
    ActionState,
    FactStatus,
    Recommendation,
    StructuredReport,
)
from core.report.render import render_markdown, to_html
from core.report.validate import (
    ActionChainObservation,
    ReportEvidencePort,
    derive_action_state,
    derive_verification_state,
    inspect_evidence_refs,
)
from core.tasks.models import Task, new_id

DEFAULT_POLICY_VERSION = "w06-v1"
DEFAULT_DATABASE_ID = "lab_mysql_01"


class VerificationPort(Protocol):
    def latest_for_task(
        self, *, task_id: str, tenant_id: str
    ) -> Any | None: ...


@dataclass(frozen=True)
class StepChain:
    """审批 + 步骤记录观测（由组合根从只读仓储投影得到）。"""

    approval_status: str | None = None
    has_step_prepared: bool = False
    has_step_observed: bool = False


class StepPort(Protocol):
    def chain_for_task(
        self, *, task_id: str, tenant_id: str
    ) -> StepChain: ...


class AuditPort(Protocol):
    def emit_required(
        self, event: Any, *, actor_user_id: str | None = None
    ) -> Any: ...


class ReportSink(Protocol):
    def insert(self, report: StructuredReport) -> None: ...


def _confidence_for(status: FactStatus) -> ConfidenceBand:
    if status is FactStatus.CONFIRMED:
        return ConfidenceBand.HIGH
    if status is FactStatus.PARTIAL:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.UNKNOWN


def _conclusion(
    *,
    fact_status: FactStatus,
    action_state: ActionState,
    verification_state: str,
    supporting: int,
    contradicting: int,
    missing: int,
) -> str:
    """确定性事实陈述；不含任何动作完成措辞（AC-11）。"""
    return (
        f"事实等级 {fact_status.value}，动作状态 {action_state.value}，"
        f"验证状态 {verification_state}；支持证据 {supporting} 条，"
        f"反证 {contradicting} 条，缺失项 {missing} 条。"
    )


def _first(rows: Sequence[Evidence], attribute: str, default: str) -> str:
    for row in rows:
        value = getattr(row, attribute, None)
        if value:
            return str(value)
    return default


def _summary(result: Any) -> RedactionSummary:
    """从闸门结果生成不含 payload 本体的摘要（复用 T01 口径）。"""
    return RedactionSummary(
        redaction_version=result.redaction_version,
        status=result.status,
        detected_categories=result.detected_categories,
        redaction_count=result.redaction_count,
        source_payload_hash=result.payload_hash,
        redacted_payload_hash=canonical_payload_hash(result.payload_redacted),
    )


class ReportBuilder:
    """组装一份结构化报告；所有状态由事实推导，调用方不得直填。"""

    def __init__(
        self,
        *,
        evidence: ReportEvidencePort,
        verifications: VerificationPort | None,
        steps: StepPort | None,
        audit: AuditPort,
        gate: Any,
        reports: ReportSink,
        policy_version: str = DEFAULT_POLICY_VERSION,
        database_id: str = DEFAULT_DATABASE_ID,
    ) -> None:
        self._evidence = evidence
        self._verifications = verifications
        self._steps = steps
        self._audit = audit
        self._gate = gate
        self._reports = reports
        self._policy_version = policy_version
        self._database_id = database_id

    def build(
        self,
        *,
        task: Task,
        facts: ReportFacts,
        recommendations: Sequence[Recommendation],
    ) -> StructuredReport:
        """取证 → 推导 → 复扫 → 落快照 → 发事件，返回最终报告。"""
        verification = (
            self._verifications.latest_for_task(
                task_id=facts.task_id, tenant_id=facts.tenant_id
            )
            if self._verifications is not None
            else None
        )
        chain = (
            self._steps.chain_for_task(
                task_id=facts.task_id, tenant_id=facts.tenant_id
            )
            if self._steps is not None
            else StepChain()
        )
        observation = ActionChainObservation(
            approval_status=chain.approval_status,
            has_step_prepared=chain.has_step_prepared,
            has_step_observed=chain.has_step_observed,
            verdict=getattr(verification, "verdict", None),
            verification_result_id=getattr(verification, "result_id", None),
        )
        action_state = derive_action_state(observation)
        verification_state = derive_verification_state(observation)

        values: dict[str, Any] = {
            "report_id": new_id("rpt"),
            "report_version": "report-v1",
            "tenant_id": facts.tenant_id,
            "task_id": facts.task_id,
            "database_id": self._database_id,
            "db_engine": "mysql",
            "db_version": "8.0.46",
            "adapter_version": "w09-report-v1",
            "environment_id": task.environment_id,
            "environment_fingerprint_digest": None,
            "policy_version": self._policy_version,
            "conclusion": "取证中",
            "fact_status": FactStatus.UNKNOWN,
            "confidence_band": ConfidenceBand.UNKNOWN,
            "supporting_evidence_ids": list(facts.supporting_evidence_ids),
            "contradicting_evidence_ids": list(facts.contradicting_evidence_ids),
            "missing_evidence": list(facts.missing_evidence),
            "weak_evidence_ids": [],
            "recommendations": list(recommendations),
            "action_state": action_state,
            "verification_state": verification_state,
            "verification_result_id": observation.verification_result_id,
            "scope": "LAB",
            "degraded": False,
            "degrade_reason": None,
            "redaction": None,
            "published": False,
        }

        draft = StructuredReport(**values)
        check = inspect_evidence_refs(draft, self._evidence)
        rows = check.found_evidence
        values["database_id"] = self._database_id
        values["db_engine"] = _first(rows, "db_engine", "mysql")
        values["db_version"] = _first(rows, "db_version", "8.0.46")
        values["adapter_version"] = _first(rows, "adapter_version", "w09-report-v1")
        values["fact_status"] = check.fact_status
        values["confidence_band"] = _confidence_for(check.fact_status)
        values["missing_evidence"] = list(check.missing_evidence)
        values["weak_evidence_ids"] = list(check.weak_ids)
        values["conclusion"] = _conclusion(
            fact_status=check.fact_status,
            action_state=action_state,
            verification_state=verification_state.value,
            supporting=len(facts.supporting_evidence_ids),
            contradicting=len(facts.contradicting_evidence_ids),
            missing=len(check.missing_evidence),
        )

        provisional = StructuredReport(**values)
        payload = {
            **provisional.model_dump(mode="json"),
            "markdown": render_markdown(provisional),
            "html": to_html(provisional),
        }
        # 报告通道独立复扫（含渲染产物）；BLOCKED 由闸门抛错，不产出报告。
        _, result = self._gate.for_document(
            payload,
            channel=RedactionChannel.REPORT,
            task_id=provisional.task_id,
            tenant_id=provisional.tenant_id,
        )
        values["redaction"] = _summary(result)
        values["published"] = (
            result.status in (RedactionStatus.CLEAN, RedactionStatus.REDACTED)
            and not check.invalid_ids
            and check.fact_status is not FactStatus.STALE
        )

        report = StructuredReport(**values)
        self._reports.insert(report)
        self._emit(report, run_id=task.run_id)
        return report

    def _emit(self, report: StructuredReport, *, run_id: str) -> None:
        """``REPORT_GENERATED`` 必带四个状态字段（完成判据）。"""
        event = make_event(
            AuditEventType.REPORT_GENERATED,
            task_id=report.task_id,
            run_id=run_id,
            payload={
                "report_id": report.report_id,
                "tenant_id": report.tenant_id,
                "task_id": report.task_id,
                "fact_status": report.fact_status.value,
                "action_state": report.action_state.value,
                "verification_state": report.verification_state.value,
                "degraded": report.degraded,
                "published": report.published,
                "redaction_status": (
                    report.redaction.status.value if report.redaction else None
                ),
            },
        )
        self._audit.emit_required(event, actor_user_id=None)


__all__ = [
    "DEFAULT_DATABASE_ID",
    "DEFAULT_POLICY_VERSION",
    "ReportBuilder",
    "StepChain",
]
