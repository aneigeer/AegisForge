"""W09 T03 报告快照仓储（``dg_system.report_snapshots``，**只追加**）。

约束：``insert`` 只发一条 ``INSERT``；``list_by_task`` 只读且强制 ``tenant_id``；
不发 ``UPDATE`` / ``DELETE`` / ``TRUNCATE``（``dg_ctl`` 也没有这些授权）。
DDL 见 ``database/migrations/010_w09_report.sql``（设计 §8.3 冻结口径）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.report.models import StructuredReport
from database.mysql.connection import MysqlClient

REPORT_SNAPSHOTS = "dg_system.report_snapshots"


def _to_db(value: str | datetime | None) -> datetime | None:
    """入库前转 naive UTC（与 ``core/evidence/repository.py`` 同口径）。"""
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _json_dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, default=str, separators=(",", ":")
    )


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


class ReportRepository:
    """报告快照只追加仓储；``client`` 由组合根注入（本类不认识账号）。"""

    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    def insert(self, report: StructuredReport) -> StructuredReport:
        """写一条快照，返回原报告；不修改任何既有行。"""
        evidence_ids = [
            *report.supporting_evidence_ids,
            *report.contradicting_evidence_ids,
        ]
        redaction_version = (
            report.redaction.redaction_version if report.redaction else "none"
        )
        self._client.execute(
            f"INSERT INTO {REPORT_SNAPSHOTS} (report_id, tenant_id, task_id, "
            "report_version, fact_status, action_state, verification_state, "
            "verification_result_id, evidence_ids_json, missing_evidence_json, "
            "body_json, redaction_version, degraded, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                report.report_id,
                report.tenant_id,
                report.task_id,
                report.report_version,
                report.fact_status.value,
                report.action_state.value,
                report.verification_state.value,
                report.verification_result_id,
                _json_dump(evidence_ids),
                _json_dump(list(report.missing_evidence)),
                _json_dump(report.model_dump(mode="json")),
                redaction_version,
                int(report.degraded),
                _to_db(report.generated_at),
            ),
        )
        return report

    def list_by_task(
        self, task_id: str, tenant_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """按任务读快照（强制租户过滤，只读）。"""
        safe_limit = max(1, min(int(limit), 500))
        rows = self._client.fetch_all(
            f"SELECT * FROM {REPORT_SNAPSHOTS} "
            "WHERE tenant_id = %s AND task_id = %s "
            "ORDER BY created_at ASC, report_id ASC LIMIT %s",
            (tenant_id, task_id, safe_limit),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence_ids_json"] = _json_load(
                item.get("evidence_ids_json"), []
            )
            item["missing_evidence_json"] = _json_load(
                item.get("missing_evidence_json"), []
            )
            item["body_json"] = _json_load(item.get("body_json"), {})
            result.append(item)
        return result


__all__ = ["REPORT_SNAPSHOTS", "ReportRepository"]
