"""W08 验证结果仓储：**只 INSERT**（无 update/delete，与 `dg_ctl` 授权边界一致）。

`verification_results` 是只追加证据表：`scripts/bootstrap_mysql.py` 的
`CONTROL_BASE_GRANTS` 只给 `dg_ctl` 授予 `SELECT, INSERT ON dg_system.*`，
本类刻意不提供任何 update/delete 方法。
"""

from __future__ import annotations

from typing import Any

from core.execution.step_records import from_db_time, json_dumps, json_loads, to_db_time
from core.tasks.models import utc_now
from core.verification.models import AssertionOutcome, VerificationResult
from database.mysql.connection import MysqlClient

TABLE = "dg_system.verification_results"


class VerificationResultRepository:
    """只追加仓储；重复 insert 由 `result_id` 主键在数据库层挡住。"""

    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    def insert(self, result: VerificationResult) -> str:
        """落一条 VerificationResult（含 outcomes JSON）；返回 result_id。"""
        outcomes = [item.model_dump(mode="json") for item in result.outcomes]
        self._client.execute(
            f"INSERT INTO {TABLE} (result_id, tenant_id, task_id, tool_call_id, "
            "plan_digest, verdict, outcomes_json, disposition, approval_required, "
            "claimed_success, collected_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                result.result_id,
                result.tenant_id,
                result.task_id,
                result.tool_call_id,
                result.plan_digest,
                result.verdict,
                json_dumps(outcomes),
                result.disposition,
                1 if result.approval_required else 0,
                1 if result.claimed_success else 0,
                to_db_time(result.collected_at),
            ),
        )
        return result.result_id

    def list_by_tool_call(
        self, tenant_id: str, tool_call_id: str
    ) -> list[VerificationResult]:
        """按 `(tenant_id, tool_call_id)` 列出全部验证结果（只追加，可多轮）。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {TABLE} WHERE tenant_id = %s AND tool_call_id = %s "
            "ORDER BY collected_at ASC, result_id ASC",
            (tenant_id, tool_call_id),
        )
        return [self._to_result(dict(row)) for row in rows]

    @staticmethod
    def _to_result(row: dict[str, Any]) -> VerificationResult:
        raw = json_loads(row.get("outcomes_json"), [])
        outcomes = tuple(
            AssertionOutcome(**item) for item in raw if isinstance(item, dict)
        )
        return VerificationResult(
            result_id=str(row["result_id"]),
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            tool_call_id=str(row["tool_call_id"]),
            plan_digest=str(row["plan_digest"]),
            verdict=row["verdict"],
            outcomes=outcomes,
            disposition=row.get("disposition"),
            approval_required=bool(row["approval_required"]),
            claimed_success=bool(row["claimed_success"]),
            collected_at=from_db_time(row.get("collected_at")) or utc_now(),
        )


__all__ = ["TABLE", "VerificationResultRepository"]
