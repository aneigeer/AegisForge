"""语句摘要统计工具（performance_schema.events_statements_summary_by_digest）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import jsonable_rows, timer_ps_to_ms, truncate_text

_ORDER_COLUMNS: dict[str, str] = {
    "total_time_ms": "SUM_TIMER_WAIT",
    "avg_time_ms": "AVG_TIMER_WAIT",
    "rows_examined": "SUM_ROWS_EXAMINED",
}


class GetQueryStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_name: str | None = Field(default=None, max_length=64)
    min_exec_count: int = Field(default=1, ge=1, le=1_000_000_000)
    order_by: Literal["total_time_ms", "avg_time_ms", "rows_examined"] = "total_time_ms"
    limit: int = Field(default=20, ge=1, le=200)


class GetQueryStatsTool(ObserverTool):
    name = "get_query_stats"
    description = "按 digest 汇总 MySQL 语句执行次数、耗时、扫描行数与无索引标记。"
    input_model = GetQueryStatsInput
    timeout_ms = 20_000
    output_schema = {
        "type": "object",
        "properties": {
            "statements": {"type": "array", "items": {"type": "object"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "filters": {"type": "object"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetQueryStatsInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        order_column = _ORDER_COLUMNS[request.order_by]
        rows = context.client.fetch_all(
            "SELECT SCHEMA_NAME AS schema_name, DIGEST AS digest, DIGEST_TEXT AS digest_text, "
            "COUNT_STAR AS count_star, SUM_TIMER_WAIT AS sum_timer_wait, "
            "AVG_TIMER_WAIT AS avg_timer_wait, MAX_TIMER_WAIT AS max_timer_wait, "
            "SUM_LOCK_TIME AS sum_lock_time, SUM_ROWS_EXAMINED AS sum_rows_examined, "
            "SUM_ROWS_SENT AS sum_rows_sent, SUM_NO_INDEX_USED AS sum_no_index_used, "
            "SUM_NO_GOOD_INDEX_USED AS sum_no_good_index_used, SUM_ERRORS AS sum_errors, "
            "SUM_WARNINGS AS sum_warnings, FIRST_SEEN AS first_seen, LAST_SEEN AS last_seen "
            "FROM performance_schema.events_statements_summary_by_digest "
            "WHERE COUNT_STAR >= %s AND (%s IS NULL OR SCHEMA_NAME = %s) "
            f"ORDER BY {order_column} DESC, COUNT_STAR DESC "
            "LIMIT %s",
            (
                request.min_exec_count,
                request.database_name,
                request.database_name,
                request.limit + 1,
            ),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        statements: list[dict[str, Any]] = []
        for row in jsonable_rows(rows[: request.limit]):
            digest_text, _ = truncate_text(row.get("digest_text"), 300)
            statements.append(
                {
                    "schema_name": row.get("schema_name"),
                    "digest": row.get("digest"),
                    "digest_text": digest_text,
                    "count_star": row.get("count_star"),
                    "total_time_ms": timer_ps_to_ms(row.get("sum_timer_wait")),
                    "avg_time_ms": timer_ps_to_ms(row.get("avg_timer_wait")),
                    "max_time_ms": timer_ps_to_ms(row.get("max_timer_wait")),
                    "lock_time_ms": timer_ps_to_ms(row.get("sum_lock_time")),
                    "rows_examined": row.get("sum_rows_examined"),
                    "rows_sent": row.get("sum_rows_sent"),
                    "no_index_used": row.get("sum_no_index_used"),
                    "no_good_index_used": row.get("sum_no_good_index_used"),
                    "errors": row.get("sum_errors"),
                    "warnings": row.get("sum_warnings"),
                    "first_seen": row.get("first_seen"),
                    "last_seen": row.get("last_seen"),
                }
            )
        if not statements:
            warnings.append("无匹配的语句摘要（可能统计未采集或执行次数不足）")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {
            "statements": statements,
            "row_count": len(statements),
            "truncated": truncated,
            "filters": {
                "database_name": request.database_name,
                "min_exec_count": request.min_exec_count,
                "order_by": request.order_by,
                "limit": request.limit,
            },
        }, warnings
