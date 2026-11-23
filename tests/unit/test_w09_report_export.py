"""W09 T03 / AC-07 + P1-W09-01：导出列名冻结与拼接后 EXPORT 通道复扫。

离线单测：不连库、不依赖 DG_ADMIN_DSN / PG 5432。``BLOCKED`` 不产生文件，
``REVIEW_REQUIRED`` 返回已抹除原串的文档并置 ``published=False``。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.diagnosis.models import ConfidenceBand
from core.errors import RedactionBlockedError
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionChannel
from core.report.export import EXPORT_COLUMNS, to_csv_rows, to_json_document
from core.report.models import ActionState, FactStatus, Recommendation, StructuredReport, VerificationState


def _report(**overrides: Any) -> StructuredReport:
    values: dict[str, Any] = {
        "report_id": "rpt_w09_export",
        "tenant_id": "tenant_local",
        "task_id": "task_w09_export",
        "database_id": "lab_mysql_01",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-report-v1",
        "environment_id": "env_mysql_local",
        "policy_version": "w06-v1",
        "conclusion": "事实等级与动作状态如实记录",
        "fact_status": FactStatus.CONFIRMED,
        "confidence_band": ConfidenceBand.HIGH,
        "supporting_evidence_ids": ["ev_ok"],
        "recommendations": [Recommendation(text="建议复核索引使用情况")],
        "action_state": ActionState.EXECUTED,
        "verification_state": VerificationState.NONE,
    }
    values.update(overrides)
    return StructuredReport(**values)


class _SpyGate:
    """记录复扫入参，再委托真实闸门，验证「拼接完成后」才扫描。"""

    def __init__(self) -> None:
        self.inner = RedactionGate(DataRedactionEngine())
        self.calls: list[tuple[dict[str, Any], str, str, str | None]] = []

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        self.calls.append((dict(payload), channel.value, task_id, tenant_id))
        return self.inner.for_document(
            payload,
            channel=channel,
            task_id=task_id,
            tenant_id=tenant_id,
        )


def test_export_columns_match_frozen_list() -> None:
    assert EXPORT_COLUMNS == (
        "report_id",
        "report_version",
        "tenant_id",
        "task_id",
        "database_id",
        "db_engine",
        "db_version",
        "adapter_version",
        "environment_id",
        "policy_version",
        "conclusion",
        "fact_status",
        "action_state",
        "verification_state",
        "degraded",
        "generated_at",
    )
    rows = to_csv_rows(_report(), gate=RedactionGate(DataRedactionEngine()))
    assert rows[0] == list(EXPORT_COLUMNS)
    assert len(rows) == 2
    assert len(rows[1]) == len(EXPORT_COLUMNS)


def test_export_rescans_assembled_document_on_export_channel() -> None:
    gate = _SpyGate()
    report = _report()
    to_csv_rows(report, gate=gate)  # type: ignore[arg-type]
    to_json_document(report, gate=gate)  # type: ignore[arg-type]
    assert len(gate.calls) == 2
    for payload, channel, task_id, tenant_id in gate.calls:
        assert channel == RedactionChannel.EXPORT.value
        assert task_id == report.task_id
        assert tenant_id == report.tenant_id
        assert set(payload) == set(EXPORT_COLUMNS)


def test_blocked_export_raises_and_produces_no_output() -> None:
    raw = "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab"
    report = _report(conclusion=f"连接串 {raw} 需复核")
    gate = RedactionGate(DataRedactionEngine())
    with pytest.raises(RedactionBlockedError):
        to_csv_rows(report, gate=gate)
    with pytest.raises(RedactionBlockedError):
        to_json_document(report, gate=gate)


def test_review_required_export_returns_redacted_document_and_unpublished() -> None:
    raw = "AB12345678"
    report = _report(conclusion=raw)
    report.published = True
    gate = RedactionGate(DataRedactionEngine())

    rows = to_csv_rows(report, gate=gate)
    assert raw not in "\n".join(str(cell) for row in rows for cell in row)
    assert report.published is False

    report.published = True
    document = to_json_document(report, gate=gate)
    assert raw not in document
    assert report.published is False
    assert "report_id" in document


def test_redacted_export_does_not_leak_raw_phone_number() -> None:
    raw = "13812345678"
    report = _report(conclusion=f"联系电话 {raw}")
    gate = RedactionGate(DataRedactionEngine())
    rows = to_csv_rows(report, gate=gate)
    document = to_json_document(report, gate=gate)
    assert raw not in "\n".join(str(cell) for row in rows for cell in row)
    assert raw not in document
    assert report.published is False  # 默认不对外发布，除非显式置位
