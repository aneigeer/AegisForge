"""W10 L2-T01：报告发布器（全假端口，不连库、不连 PG）。

覆盖设计 §5.4 七条：

1. 装配 `is` 一致性（`publisher.redaction` 与组合根注入的闸门是**同一实例**）；
2. 正常发布：`generated=True`、`reports.insert` 恰 1 次、`REPORT_GENERATED` 已发、
   除快照外**没有任何写**；
3. 幂等：已有快照 → `ALREADY_GENERATED`、`insert` 0 次；
4. 失败隔离：builder 抛错 → `failed=True` 且**不冒泡**，日志 `alert=REPORT_FAILED`；
5. 事实投影：3 条证据 + 1 条审批 + 1 条已观测 → `supporting_evidence_ids` 顺序稳定、
   `StepChain` 为 `True/True`、验证端口命中**最后一条**；
6. 两条建议常量都能通过 `Recommendation` 校验（不触 AC-11 黑名单）；
7. 无证据任务：仍 `generated=True`，报告 `fact_status == UNKNOWN`（风险 R1 的表达）。

附加（同一设计的可断言要求）：证据分页达上限只记 `EVIDENCE_TRUNCATED`、不伪造 id。

所有端口都是本地替身：不连库、不依赖 `DG_ADMIN_DSN` / PG、不写任何表。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.errors import RequestInvalidError
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceQuality,
)
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.report.models import (
    ActionState,
    FactStatus,
    Recommendation,
    StructuredReport,
    VerificationState,
)
from core.tasks.models import Task, TaskStatus
from workers.report_assembly import (
    ALERT_REPORT_FAILED,
    MAX_EVIDENCE_PAGES,
    REASON_ALREADY_GENERATED,
    REASON_EVIDENCE_TRUNCATED,
    RECOMMENDATION_EXECUTED,
    RECOMMENDATION_UNKNOWN,
    TaskReportPublisher,
    build_report_publisher,
)

TENANT = "tenant_local"
ENVIRONMENT = "env_mysql_local"
TOOL_CALL_ID = "call_l2_t01"
APPROVAL_ID = "apr_l2_t01"
WORKERS_STACK = Path(__file__).resolve().parents[2] / "workers" / "stack.py"


# ---------------------------------------------------------------------------
# 替身端口
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(task_id: str = "task_l2_t01") -> Task:
    now = _now()
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        goal="W10 L2-T01 报告发布用例",
        status=TaskStatus.EXECUTING,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id=ENVIRONMENT,
    )


def _evidence(
    evidence_id: str,
    *,
    task_id: str = "task_l2_t01",
    tenant_id: str = TENANT,
    environment_id: str = ENVIRONMENT,
    status: CollectionStatus = CollectionStatus.SUCCESS,
    quality: EvidenceQuality = EvidenceQuality.HIGH,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        task_id=task_id,
        environment_id=environment_id,
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-evidence-v1",
        source_tool="mysql_lab_update_order_note",
        observed_at=_now().isoformat(),
        collection_status=status,
        quality=quality,
        completeness=EvidenceCompleteness.COMPLETE,
        payload_hash="a" * 64,
        redaction_version="w09-redaction-v1",
    )


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event

    def event_types(self) -> list[str]:
        result: list[str] = []
        for event in self.events:
            value = getattr(event, "event_type", None)
            result.append(str(getattr(value, "value", value)))
        return result


class _FakeReports:
    """报告快照替身：`insert` 记录报告，`list_by_task` 返回预置行（可模拟幂等）。"""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self._existing = list(existing or [])
        self.inserted: list[StructuredReport] = []
        self.calls: list[str] = []

    def insert(self, report: StructuredReport) -> StructuredReport:
        self.calls.append("insert")
        self.inserted.append(report)
        return report

    def list_by_task(
        self, task_id: str, tenant_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.calls.append("list_by_task")
        return list(self._existing)[:limit]


class _FakeEvidence:
    """`ReportEvidencePort` + 分页读取替身；只提供读方法（无任何写方法）。"""

    def __init__(self, rows: list[Evidence], *, page_size: int = 50) -> None:
        self._rows = list(rows)
        self._page_size = page_size
        self.calls: list[str] = []

    def list_by_task(
        self,
        task_id: str,
        tenant_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Evidence], str | None]:
        self.calls.append("list_by_task")
        start = 0 if cursor is None else int(cursor)
        end = start + min(self._page_size, limit)
        chunk = self._rows[start:end]
        next_cursor = str(end) if end < len(self._rows) else None
        return chunk, next_cursor

    def fetch_by_ids(self, tenant_id: str, evidence_ids: Any) -> list[Evidence]:
        self.calls.append("fetch_by_ids")
        wanted = [str(item) for item in evidence_ids]
        return [
            row
            for row in self._rows
            if row.evidence_id in wanted and row.tenant_id == tenant_id
        ]


class _ApprovalStub:
    def __init__(self, approval_id: str, status: str) -> None:
        self.approval_id = approval_id
        self.status = status


class _FakeApprovals:
    def __init__(self, rows: list[_ApprovalStub] | None = None) -> None:
        self._rows = list(rows or [])
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> tuple[list[_ApprovalStub], str | None]:
        self.calls.append(dict(kwargs))
        return list(self._rows), None


class _FakeStepRecords:
    def __init__(
        self, *, prepared: bool = True, observed: bool = True
    ) -> None:
        self._prepared = prepared
        self._observed = observed
        self.calls: list[list[str]] = []

    def evidence_for_approvals(
        self, approval_ids: Any
    ) -> dict[str, dict[str, Any]]:
        ids = [str(item) for item in approval_ids]
        self.calls.append(ids)
        return {
            approval_id: {
                "step_prepared": self._prepared,
                "step_observed_outcome": (
                    "success" if self._observed else None
                ),
            }
            for approval_id in ids
        }


class _VerificationStub:
    def __init__(self, result_id: str, verdict: str) -> None:
        self.result_id = result_id
        self.verdict = verdict


class _FakeVerifications:
    def __init__(self, rows: list[_VerificationStub] | None = None) -> None:
        self._rows = list(rows or [])
        self.calls: list[tuple[str, str]] = []

    def list_by_tool_call(
        self, tenant_id: str, tool_call_id: str
    ) -> list[_VerificationStub]:
        self.calls.append((tenant_id, tool_call_id))
        return list(self._rows)


class _FailingGate:
    """只在 `for_document` 上抛错：模拟报告通道被闸门拒绝/引擎异常。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def for_document(self, payload: Any, **kwargs: Any) -> Any:
        raise self._error


class _FakeSettings:
    policy_version = "w06-v1"
    database_id = "lab_mysql_01"


def _gate(audit: _FakeAudit) -> RedactionGate:
    return RedactionGate(DataRedactionEngine(), audit=audit)


def _publisher(
    *,
    gate: Any,
    audit: Any,
    evidence: Any = None,
    reports: Any = None,
    approvals: Any = None,
    step_records: Any = None,
    verifications: Any = None,
) -> TaskReportPublisher:
    return TaskReportPublisher(
        evidence=evidence if evidence is not None else _FakeEvidence([]),
        reports=reports if reports is not None else _FakeReports(),
        approvals=approvals if approvals is not None else _FakeApprovals(),
        step_records=(
            step_records if step_records is not None else _FakeStepRecords()
        ),
        verifications=(
            verifications if verifications is not None else _FakeVerifications()
        ),
        audit=audit,
        gate=gate,
        tenant_id=TENANT,
        policy_version="w06-v1",
        database_id="lab_mysql_01",
    )


# ---------------------------------------------------------------------------
# 1) 装配 is 一致性
# ---------------------------------------------------------------------------


def test_publisher_redaction_is_the_injected_gate() -> None:
    audit = _FakeAudit()
    gate = _gate(audit)

    publisher = build_report_publisher(
        settings=_FakeSettings(),
        tenant_id=TENANT,
        control_client=object(),
        audit=audit,
        gate=gate,
        approvals=_FakeApprovals(),
        step_records=_FakeStepRecords(),
    )

    assert isinstance(publisher, TaskReportPublisher)
    # 同一实例：报告通道与 stack.redaction 槽位共用一个闸门对象。
    assert publisher.redaction is gate


def test_stack_assembly_uses_one_gate_instance_for_both_slots() -> None:
    """组合根源码级证据：同一个局部名同时喂给两个槽位（`is` 由此成立）。

    本用例不启动 `build_worker_stack`（那需要真实控制库），而是锁定"单实例"
    这一结构性事实；真实装配的 `is` 断言由集成用例覆盖。
    """
    source = WORKERS_STACK.read_text(encoding="utf-8")

    assert "redaction_gate = RedactionGate(DataRedactionEngine(), audit=audit)" in source
    assert "redaction=redaction_gate" in source
    assert "gate=redaction_gate" in source
    # 只创建一个闸门实例（否则 `is` 断言会失效）。
    assert source.count("RedactionGate(") == 1


# ---------------------------------------------------------------------------
# 2) 正常发布
# ---------------------------------------------------------------------------


def test_publish_inserts_exactly_one_snapshot_and_emits_event() -> None:
    audit = _FakeAudit()
    evidence = _FakeEvidence([_evidence("ev_1")])
    reports = _FakeReports()
    publisher = _publisher(gate=_gate(audit), audit=audit, evidence=evidence, reports=reports)

    outcome = publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=True)

    assert outcome.generated is True
    assert outcome.failed is False
    assert outcome.skipped_reason is None
    assert str(outcome.report_id).startswith("rpt_")
    assert outcome.report_id == reports.inserted[0].report_id
    assert reports.calls.count("insert") == 1
    assert len(reports.inserted) == 1
    assert "REPORT_GENERATED" in audit.event_types()
    # 只读事实 + 只追加快照：除 `insert` 外没有任何写路径。
    assert set(evidence.calls) <= {"list_by_task", "fetch_by_ids"}
    assert set(reports.calls) <= {"list_by_task", "insert"}


# ---------------------------------------------------------------------------
# 3) 幂等
# ---------------------------------------------------------------------------


def test_publish_is_idempotent_when_snapshot_exists() -> None:
    audit = _FakeAudit()
    reports = _FakeReports(existing=[{"report_id": "rpt_existing"}])
    publisher = _publisher(gate=_gate(audit), audit=audit, reports=reports)

    outcome = publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=True)

    assert outcome.generated is False
    assert outcome.failed is False
    assert outcome.skipped_reason == REASON_ALREADY_GENERATED
    assert reports.calls.count("insert") == 0
    assert audit.events == []


# ---------------------------------------------------------------------------
# 4) 失败隔离
# ---------------------------------------------------------------------------


def test_publish_failure_is_isolated_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = _FakeAudit()
    publisher = _publisher(
        gate=_FailingGate(RequestInvalidError("模拟报告通道失败")), audit=audit
    )

    with caplog.at_level(logging.ERROR, logger="workers.report_assembly"):
        outcome = publisher.publish(
            _task(), tool_call_id=TOOL_CALL_ID, succeeded=True
        )

    assert outcome.generated is False
    assert outcome.failed is True
    assert outcome.skipped_reason is None
    alerts = [
        getattr(record, "alert", None)
        for record in caplog.records
        if record.name == "workers.report_assembly"
    ]
    assert ALERT_REPORT_FAILED in alerts
    # 失败不写快照、不发业务事件（REPORT_GENERATED 只在 builder 成功时发）。
    assert "REPORT_GENERATED" not in audit.event_types()


# ---------------------------------------------------------------------------
# 5) 事实投影
# ---------------------------------------------------------------------------


def test_publish_projects_facts_and_chain_and_last_verification() -> None:
    audit = _FakeAudit()
    evidence_rows = [_evidence("ev_a"), _evidence("ev_b"), _evidence("ev_c")]
    evidence = _FakeEvidence(evidence_rows)
    reports = _FakeReports()
    approvals = _FakeApprovals([_ApprovalStub(APPROVAL_ID, "CONSUMED")])
    step_records = _FakeStepRecords(prepared=True, observed=True)
    verifications = _FakeVerifications(
        [_VerificationStub("vr_first", "UNKNOWN"), _VerificationStub("vr_last", "PASS")]
    )
    publisher = _publisher(
        gate=_gate(audit),
        audit=audit,
        evidence=evidence,
        reports=reports,
        approvals=approvals,
        step_records=step_records,
        verifications=verifications,
    )

    publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=True)

    report = reports.inserted[0]
    # 顺序稳定：与仓储返回顺序一致（`created_at, evidence_id`）。
    assert report.supporting_evidence_ids == ["ev_a", "ev_b", "ev_c"]
    assert report.contradicting_evidence_ids == []
    assert report.missing_evidence == []
    assert report.fact_status is FactStatus.CONFIRMED
    # 证据链：审批 CONSUMED + 已 prepared + 已 observed + verdict=PASS（有 result_id）
    # → 阶段唯一确定为 VERIFIED（`derive_chain_stage` 首个命中，不由布尔组合拼装）。
    assert report.action_state is ActionState.VERIFIED
    # 验证端口按本轮 tool_call_id 查，且取**最后一条**。
    assert verifications.calls == [(TENANT, TOOL_CALL_ID)]
    assert report.verification_result_id == "vr_last"
    assert report.verification_state is VerificationState.PASS
    # 两次单行只读查询，不带写路径。
    assert approvals.calls == [{"tenant_id": TENANT, "task_id": "task_l2_t01", "limit": 1}]
    assert step_records.calls == [[APPROVAL_ID]]


def test_publish_marks_evidence_truncation_without_faking_ids() -> None:
    """分页达上限：只记 `EVIDENCE_TRUNCATED`，报告只含**真实读到**的 id。"""
    audit = _FakeAudit()
    rows = [_evidence(f"ev_{index:02d}") for index in range(20)]
    evidence = _FakeEvidence(rows, page_size=2)
    reports = _FakeReports()
    publisher = _publisher(
        gate=_gate(audit), audit=audit, evidence=evidence, reports=reports
    )

    outcome = publisher.publish(
        _task(), tool_call_id=TOOL_CALL_ID, succeeded=True
    )

    assert outcome.generated is True
    collected = reports.inserted[0].supporting_evidence_ids
    assert len(collected) == MAX_EVIDENCE_PAGES * 2
    assert collected == [row.evidence_id for row in rows[: len(collected)]]
    assert all(item in {row.evidence_id for row in rows} for item in collected)


def test_publish_logs_evidence_truncated_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = _FakeAudit()
    rows = [_evidence(f"ev_{index:02d}") for index in range(20)]
    publisher = _publisher(
        gate=_gate(audit),
        audit=audit,
        evidence=_FakeEvidence(rows, page_size=2),
        reports=_FakeReports(),
    )

    with caplog.at_level(logging.WARNING, logger="workers.report_assembly"):
        publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=False)

    alerts = [
        getattr(record, "alert", None)
        for record in caplog.records
        if record.name == "workers.report_assembly"
    ]
    assert REASON_EVIDENCE_TRUNCATED in alerts


# ---------------------------------------------------------------------------
# 6) 建议文本合规（AC-11 黑名单）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", [RECOMMENDATION_EXECUTED, RECOMMENDATION_UNKNOWN]
)
def test_recommendation_texts_pass_ac11_validation(text: str) -> None:
    recommendation = Recommendation(text=text)

    assert recommendation.text == text
    assert recommendation.state is ActionState.PROPOSED


def test_succeeded_only_switches_recommendation_text() -> None:
    """`succeeded` 只决定建议文本，不参与状态推导（AC-11 / AC-09 不放宽）。"""
    audit = _FakeAudit()
    reports = _FakeReports()
    publisher = _publisher(
        gate=_gate(audit),
        audit=audit,
        evidence=_FakeEvidence([]),
        reports=reports,
    )

    publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=True)
    publisher.publish(_task(), tool_call_id=TOOL_CALL_ID, succeeded=False)

    first, second = reports.inserted
    assert [item.text for item in first.recommendations] == [RECOMMENDATION_EXECUTED]
    assert [item.text for item in second.recommendations] == [RECOMMENDATION_UNKNOWN]
    # 状态推导不受 `succeeded` 影响：两者都是 UNKNOWN 事实。
    assert first.fact_status is second.fact_status is FactStatus.UNKNOWN
    assert first.action_state is second.action_state


# ---------------------------------------------------------------------------
# 7) 无证据任务：仍然生成，事实如实记为 UNKNOWN
# ---------------------------------------------------------------------------


def test_publish_without_evidence_still_generates_unknown_fact() -> None:
    audit = _FakeAudit()
    reports = _FakeReports()
    publisher = _publisher(
        gate=_gate(audit),
        audit=audit,
        evidence=_FakeEvidence([]),
        reports=reports,
    )

    outcome = publisher.publish(
        _task(), tool_call_id=TOOL_CALL_ID, succeeded=True
    )

    assert outcome.generated is True
    report = reports.inserted[0]
    assert report.supporting_evidence_ids == []
    assert report.fact_status is FactStatus.UNKNOWN
    assert report.conclusion.startswith("事实等级 UNKNOWN")
    # `published` 只排除 STALE / 无效引用 / 不可放行脱敏，UNKNOWN 不阻断发布，
    # 但报告本体如实标注 UNKNOWN（风险 R1 的机器化表达）。
    assert isinstance(report.published, bool)
