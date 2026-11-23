"""W09 T03 报告导出（AC-07）：拼接完成后再走 ``EXPORT`` 通道复扫。

设计 §3 第 6 行 / ``03-task-cards.md:384``：导出是纯函数，不新增 API 端点
（P2-W09-02 明确延期）。``BLOCKED`` 时 ``for_document`` 抛 ``RedactionBlockedError``，
本模块不产生任何半成品文件；``REVIEW_REQUIRED`` 不抛，返回已抹除原串的文档，
并把 ``report.published`` 置为 ``False``（``03-task-cards.md:203``）。

KD-W09-03 如实登记（唯一对外口径来源：``docs/w09/07-pm-ruling.md`` §4.3 / §4.4）：
本卡按 §4.4 第 2 条，报告本体走 ``REPORT`` 通道、导出走 ``EXPORT`` 通道，
各自独立调用 ``for_document`` 复扫；**不把** ``StepPrepared.request`` /
``TrajectoryRecorder`` 等内部通道原文当作对外通道输入。本卡不改
``redact_payload``、不升 ``redaction_version``（保持 ``redaction-v2``）、
不补冻结回归、不新增 AC；引用 ``docs/w09/06-qa-report.md`` §11.5 时必须并列
引用 ``docs/w09/07-pm-ruling.md`` §4。该发现项定性为**已声明的已知限制，
不是本轮 FAIL**，W10 再议；对外不得写「脱敏全链路无死角 / PII 零落盘」。
T04–T06 的「未开工、未验证」状态如实保留。
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from core.redaction.models import RedactionChannel, RedactionResult, RedactionStatus
from core.report.models import StructuredReport
from core.report.render import sanitize_text

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与闸门模块循环导入
    from core.redaction.gate import RedactionGate

# P1-W09-01：导出字段白名单与列头固定化。列头顺序即冻结口径，逐字相等。
EXPORT_COLUMNS: tuple[str, ...] = (
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


def _assemble(report: StructuredReport) -> dict[str, Any]:
    """先拼接完整文档，再交给闸门；不提前过滤、不提前写出。"""
    return {column: str(getattr(report, column)) for column in EXPORT_COLUMNS}


def _scan(
    report: StructuredReport, gate: "RedactionGate"
) -> tuple[dict[str, Any], RedactionResult]:
    """拼接后调用 ``EXPORT`` 通道；``BLOCKED`` 由闸门抛错，不返回半成品。"""
    payload = _assemble(report)
    document, result = gate.for_document(
        payload,
        channel=RedactionChannel.EXPORT,
        task_id=report.task_id,
        tenant_id=report.tenant_id,
    )
    if result.status is RedactionStatus.REVIEW_REQUIRED:
        # 设计 §3 第 5 行：复核态文档不对外交付。
        report.published = False
    return dict(document), result


def to_csv_rows(
    report: StructuredReport, *, gate: "RedactionGate"
) -> list[list[str]]:
    """导出为 CSV 行（表头 + 数据行）；命中即抛错，不产生文件。"""
    document, _ = _scan(report, gate)
    return [
        list(EXPORT_COLUMNS),
        [
            sanitize_text(str(document.get(column, "")))
            for column in EXPORT_COLUMNS
        ],
    ]


def to_json_document(
    report: StructuredReport, *, gate: "RedactionGate"
) -> str:
    """导出为 JSON 文档字符串；命中即抛错，不产生文件。"""
    document, _ = _scan(report, gate)
    safe = {
        column: sanitize_text(str(document.get(column, "")))
        for column in EXPORT_COLUMNS
    }
    return json.dumps(
        safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = ["EXPORT_COLUMNS", "to_csv_rows", "to_json_document"]
