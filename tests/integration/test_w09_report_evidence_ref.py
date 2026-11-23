"""W09 T03 / AC-12：真实控制库下的引用校验与安全渲染（需 DB）。

纪律（``03-task-cards.md:6-12``）：

- 只依赖 ``control_client``（dg_ctl），**不依赖** ``DG_ADMIN_DSN``、不依赖 PG 5432；
- 用例只做 INSERT（创建 task / evidence），不清理、不 DELETE 残留行；
- 不加 ``skip`` / ``xfail``、不放宽断言；
- ``report_snapshots`` 表在本机尚未由迁移 010 建立，因此快照落点用内存桩，
  真实 ``ReportRepository.insert`` 的只追加 SQL 由单测用假 client 断言。
"""

from __future__ import annotations

from typing import Any, Sequence

import pytest

from core.diagnosis.models import ConfidenceBand
from core.evidence.models import CollectionStatus, EvidenceCreate, ReportFacts
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.report.builder import ReportBuilder, StepChain
from core.report.models import (
    ActionState,
    FactStatus,
    Recommendation,
    StructuredReport,
    VerificationState,
)
from core.report.render import render_markdown, to_html
from core.report.validate import inspect_evidence_refs
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


def _make_request(task: Any, **overrides: Any) -> EvidenceCreate:
    values: dict[str, Any] = {
        "tenant_id": task.tenant_id,
        "task_id": task.task_id,
        "environment_id": task.environment_id,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-t03-integration",
        "source_tool": "get_db_version",
        "observed_at": "2026-03-01T00:00:00+00:00",
        "collection_status": CollectionStatus.SUCCESS,
        "payload": {"rows": [{"id": 1}]},
        "time_window_start": "2026-02-28T23:59:00+00:00",
        "time_window_end": "2026-03-01T00:00:00+00:00",
    }
    values.update(overrides)
    return EvidenceCreate(**values)


def _draft_report(task: Any, supporting: Sequence[str]) -> StructuredReport:
    return StructuredReport(
        report_id="rpt_w09_draft",
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        database_id="lab_mysql_01",
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-t03-integration",
        environment_id=task.environment_id,
        policy_version="w06-v1",
        conclusion="事实等级与动作状态如实记录",
        fact_status=FactStatus.UNKNOWN,
        confidence_band=ConfidenceBand.UNKNOWN,
        supporting_evidence_ids=list(supporting),
        action_state=ActionState.EXECUTED,
        verification_state=VerificationState.NONE,
    )


class _InMemoryReports:
    """快照落点内存桩：只记录 insert，不碰数据库（表尚未迁移）。"""

    def __init__(self) -> None:
        self.inserted: list[StructuredReport] = []

    def insert(self, report: StructuredReport) -> None:
        self.inserted.append(report)


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event


class _NoVerifications:
    def latest_for_task(self, *, task_id: str, tenant_id: str) -> Any | None:
        return None


class _NoSteps:
    def chain_for_task(self, *, task_id: str, tenant_id: str) -> StepChain:
        return StepChain()


class _EnvMismatchPort:
    """把真实取回的证据投影成「环境已变更」，模拟失效引用（不写库）。"""

    def __init__(self, inner: EvidenceRepository, environment_id: str) -> None:
        self.inner = inner
        self.environment_id = environment_id

    def fetch_by_ids(
        self, tenant_id: str, evidence_ids: Sequence[str]
    ) -> list[Any]:
        rows = self.inner.fetch_by_ids(tenant_id, evidence_ids)
        return [
            row.model_copy(update={"environment_id": self.environment_id})
            for row in rows
        ]


def _builder(evidence_port: Any, reports: _InMemoryReports, audit: _FakeAudit) -> ReportBuilder:
    return ReportBuilder(
        evidence=evidence_port,
        verifications=_NoVerifications(),
        steps=_NoSteps(),
        audit=audit,
        gate=RedactionGate(DataRedactionEngine()),
        reports=reports,
    )


def test_render_escapes_script_and_inline_code() -> None:
    """AC-12：含大小写 / 空格变体的可执行内容一律纯文本化。"""
    hostile = (
        "<script>alert(1)</script> <SCRIPT>alert(2)</SCRIPT> "
        "< script >alert(3)</ script > onerror=alert(4) ONERROR=alert(5) "
        "javascript:alert(6) JaVaScRiPt:alert(7)"
    )
    report = StructuredReport(
        report_id="rpt_w09_render",
        tenant_id="tenant_local",
        task_id="task_w09_render",
        database_id="lab_mysql_01",
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-t03-integration",
        environment_id="env_mysql_local",
        policy_version="w06-v1",
        conclusion=hostile,
        fact_status=FactStatus.UNKNOWN,
        confidence_band=ConfidenceBand.UNKNOWN,
        action_state=ActionState.EXECUTED,
        verification_state=VerificationState.NONE,
    )
    markdown = render_markdown(report)
    html_text = to_html(report)
    for rendered in (markdown, html_text):
        lowered = rendered.lower()
        assert "<script" not in lowered
        assert "< script" not in lowered
        assert "onerror=" not in lowered
        assert "javascript:" not in lowered
    # to_html 只生成 <pre> / </pre> 两个标签，其余全部转义。
    assert html_text.startswith("<pre>")
    assert html_text.endswith("</pre>")
    assert html_text.count("<") == 2
    assert html_text.count(">") == 2


def test_stale_missing_evidence_id_marks_stale_and_emits_event(control_client: Any) -> None:
    """AC-12：失效 evidence_id → STALE + missing_evidence 非空 + REPORT_GENERATED。"""
    task_repository = TaskRepository(control_client)
    task_service = TaskService(task_repository, ToolRegistry())
    task = task_service.create_task("W09 T03 报告失效引用")
    evidence_service = EvidenceService(
        EvidenceRepository(control_client), task_repository
    )
    stored = evidence_service.create(_make_request(task))
    facts = ReportFacts(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        supporting_evidence_ids=[stored.evidence_id, "ev_not_collected_w09"],
        missing_evidence=["ev_not_collected_w09"],
    )
    reports = _InMemoryReports()
    audit = _FakeAudit()
    builder = _builder(EvidenceRepository(control_client), reports, audit)

    report = builder.build(
        task=task,
        facts=facts,
        recommendations=[Recommendation(text="建议补充采集缺失证据")],
    )

    assert report.fact_status is FactStatus.STALE
    assert report.missing_evidence
    assert "ev_not_collected_w09" in report.missing_evidence
    assert report.published is False
    assert len(reports.inserted) == 1
    assert reports.inserted[0].report_id == report.report_id
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == "REPORT_GENERATED"
    for key in ("fact_status", "action_state", "verification_state", "degraded"):
        assert key in event.payload
    assert event.payload["fact_status"] == FactStatus.STALE.value


def test_environment_mismatch_downgrades_to_stale(control_client: Any) -> None:
    """环境不符记入 env_mismatch_ids 并降 STALE，不静默剔除后仍报 CONFIRMED。"""
    task_repository = TaskRepository(control_client)
    task_service = TaskService(task_repository, ToolRegistry())
    task = task_service.create_task("W09 T03 报告环境不符")
    evidence_service = EvidenceService(
        EvidenceRepository(control_client), task_repository
    )
    stored = evidence_service.create(_make_request(task))
    port = _EnvMismatchPort(EvidenceRepository(control_client), "env_pg_local")

    check = inspect_evidence_refs(_draft_report(task, [stored.evidence_id]), port)
    assert check.env_mismatch_ids == (stored.evidence_id,)
    assert check.invalid_ids == ()
    assert check.fact_status is FactStatus.STALE
    assert check.missing_evidence

    reports = _InMemoryReports()
    audit = _FakeAudit()
    builder = _builder(port, reports, audit)
    report = builder.build(
        task=task,
        facts=ReportFacts(
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            supporting_evidence_ids=[stored.evidence_id],
        ),
        recommendations=[Recommendation(text="建议重新采集当前环境证据")],
    )
    assert report.fact_status is FactStatus.STALE
    assert stored.evidence_id in report.missing_evidence
    assert report.published is False
    assert len(reports.inserted) == 1
