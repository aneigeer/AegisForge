"""活动会话工具（performance_schema.threads + 当前语句）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import DBGuardianError, DatabasePermissionError
from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import jsonable_rows, safe_sql_preview, truncate_text


class GetActivityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_duration_seconds: int = Field(default=0, ge=0, le=86_400)
    limit: int = Field(default=50, ge=1, le=500)
    include_idle: bool = False


class GetActivityTool(ObserverTool):
    name = "get_activity"
    description = "查看当前 MySQL 活动会话、耗时、状态和当前 SQL（字面量已脱敏）。"
    input_model = GetActivityInput
    timeout_ms = 15_000
    output_schema = {
        "type": "object",
        "properties": {
            "sessions": {"type": "array", "items": {"type": "object"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "filters": {"type": "object"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetActivityInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT t.THREAD_ID AS thread_id, t.PROCESSLIST_ID AS processlist_id, "
            "t.PROCESSLIST_USER AS user_name, t.PROCESSLIST_HOST AS host, "
            "t.PROCESSLIST_DB AS database_name, t.PROCESSLIST_COMMAND AS command, "
            "t.PROCESSLIST_STATE AS state, COALESCE(t.PROCESSLIST_TIME, 0) AS time_seconds, "
            "e.SQL_TEXT AS sql_text, e.DIGEST_TEXT AS digest_text, "
            "e.ROWS_EXAMINED AS rows_examined "
            "FROM performance_schema.threads t "
            "LEFT JOIN performance_schema.events_statements_current e ON e.THREAD_ID = t.THREAD_ID "
            "WHERE t.PROCESSLIST_ID IS NOT NULL "
            "AND (%s = 1 OR t.PROCESSLIST_COMMAND <> 'Sleep') "
            "AND COALESCE(t.PROCESSLIST_TIME, 0) >= %s "
            "ORDER BY time_seconds DESC, processlist_id "
            "LIMIT %s",
            (1 if request.include_idle else 0, request.min_duration_seconds, request.limit + 1),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        rows = rows[: request.limit]

        sessions: list[dict[str, Any]] = []
        for row in jsonable_rows(rows):
            sql_text, sql_truncated = safe_sql_preview(
                str(row["sql_text"]) if row["sql_text"] is not None else None
            )
            digest_text, _ = truncate_text(
                str(row["digest_text"]) if row["digest_text"] is not None else None, 300
            )
            sessions.append(
                {
                    "thread_id": row["thread_id"],
                    "processlist_id": row["processlist_id"],
                    "user": row["user_name"],
                    "host": row["host"],
                    "database": row["database_name"],
                    "command": row["command"],
                    "state": row["state"],
                    "time_seconds": row["time_seconds"],
                    "sql_text": sql_text,
                    "sql_truncated": sql_truncated,
                    "digest_text": digest_text,
                    "rows_examined": row["rows_examined"],
                    "transaction_state": None,
                    "transaction_age_seconds": None,
                    "transaction_wait_started": None,
                }
            )

        try:
            trx_rows = context.client.fetch_all(
                "SELECT trx_mysql_thread_id AS processlist_id, trx_state AS transaction_state, "
                "TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS transaction_age_seconds, "
                "trx_wait_started AS transaction_wait_started "
                "FROM information_schema.innodb_trx"
            )
        except DatabasePermissionError:
            trx_rows = []
            warnings.append("缺少 PROCESS 权限，未合并 InnoDB 事务年龄")
        except DBGuardianError:
            trx_rows = []
            warnings.append("InnoDB 事务信息暂不可用")

        trx_by_pid = {row["processlist_id"]: row for row in jsonable_rows(trx_rows)}
        for session in sessions:
            trx = trx_by_pid.get(session["processlist_id"])
            if trx:
                session["transaction_state"] = trx["transaction_state"]
                session["transaction_age_seconds"] = trx["transaction_age_seconds"]
                session["transaction_wait_started"] = trx["transaction_wait_started"]

        if not sessions:
            warnings.append("无匹配的活动会话")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {
            "sessions": sessions,
            "row_count": len(sessions),
            "truncated": truncated,
            "filters": {
                "min_duration_seconds": request.min_duration_seconds,
                "include_idle": request.include_idle,
                "limit": request.limit,
            },
        }, warnings
