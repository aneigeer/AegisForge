"""MySQL Adapter 专属只读工具（事务元数据）。

复用 ObserverTool 的入参校验、错误映射与来源信息，避免在 Adapter 内复制 SQL。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import jsonable_rows, safe_sql_preview


class GetTransactionMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=500)


class GetTransactionMetadataTool(ObserverTool):
    name = "get_transaction_metadata"
    description = (
        "读取 information_schema.innodb_trx 事务元数据（状态、年龄、线程与当前 SQL，"
        "SQL 字面量已脱敏）。"
    )
    input_model = GetTransactionMetadataInput
    timeout_ms = 15_000
    output_schema = {
        "type": "object",
        "properties": {
            "transactions": {"type": "array", "items": {"type": "object"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(
        self, context: ToolContext, params: BaseModel
    ) -> tuple[dict[str, Any], list[str]]:
        request: GetTransactionMetadataInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT trx_id, trx_state, trx_started, "
            "TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS trx_age_seconds, "
            "trx_mysql_thread_id, trx_query "
            "FROM information_schema.innodb_trx "
            "ORDER BY trx_started LIMIT %s",
            (request.limit + 1,),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit

        transactions: list[dict[str, Any]] = []
        for row in jsonable_rows(rows[: request.limit]):
            query_preview, query_truncated = safe_sql_preview(
                str(row["trx_query"]) if row.get("trx_query") is not None else None
            )
            transactions.append(
                {
                    "trx_id": row.get("trx_id"),
                    "trx_state": row.get("trx_state"),
                    "trx_started": row.get("trx_started"),
                    "trx_age_seconds": row.get("trx_age_seconds"),
                    "trx_mysql_thread_id": row.get("trx_mysql_thread_id"),
                    "trx_query": query_preview,
                    "trx_query_truncated": query_truncated,
                }
            )

        if not transactions:
            warnings.append("当前无活跃 InnoDB 事务")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {
            "transactions": transactions,
            "row_count": len(transactions),
            "truncated": truncated,
        }, warnings


__all__ = ["GetTransactionMetadataInput", "GetTransactionMetadataTool"]
