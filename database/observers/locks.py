"""锁与阻塞链工具（MySQL 8 performance_schema.data_locks / data_lock_waits）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import DBGuardianError, DatabasePermissionError
from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import jsonable_rows, safe_sql_preview


def _safe_lock_data(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if "supremum" in text.lower() or "infimum" in text.lower():
        return text
    return "<redacted>"


class GetLocksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_name: str | None = Field(default=None, max_length=64)
    table_name: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=100, ge=1, le=500)


class GetBlockingTreeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=500)


class GetLocksTool(ObserverTool):
    name = "get_locks"
    description = "读取 InnoDB 当前持有的行/表锁，关联会话与事务，锁数据值脱敏。"
    input_model = GetLocksInput
    timeout_ms = 15_000
    output_schema = {
        "type": "object",
        "properties": {
            "locks": {"type": "array", "items": {"type": "object"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetLocksInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT l.ENGINE AS engine, l.ENGINE_LOCK_ID AS engine_lock_id, "
            "l.ENGINE_TRANSACTION_ID AS engine_transaction_id, l.THREAD_ID AS thread_id, "
            "t.PROCESSLIST_ID AS processlist_id, t.PROCESSLIST_USER AS user_name, "
            "t.PROCESSLIST_DB AS session_database, t.PROCESSLIST_TIME AS session_time_seconds, "
            "l.OBJECT_SCHEMA AS object_schema, l.OBJECT_NAME AS object_name, "
            "l.INDEX_NAME AS index_name, l.LOCK_TYPE AS lock_type, l.LOCK_MODE AS lock_mode, "
            "l.LOCK_STATUS AS lock_status, l.LOCK_DATA AS lock_data "
            "FROM performance_schema.data_locks l "
            "LEFT JOIN performance_schema.threads t ON t.THREAD_ID = l.THREAD_ID "
            "WHERE (%s IS NULL OR l.OBJECT_SCHEMA = %s) "
            "AND (%s IS NULL OR l.OBJECT_NAME = %s) "
            "ORDER BY l.ENGINE_TRANSACTION_ID, l.THREAD_ID, l.EVENT_ID "
            "LIMIT %s",
            (
                request.database_name,
                request.database_name,
                request.table_name,
                request.table_name,
                request.limit + 1,
            ),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        rows = jsonable_rows(rows[: request.limit])
        for row in rows:
            row["lock_data"] = _safe_lock_data(row.get("lock_data"))

        if not rows:
            warnings.append("当前无 InnoDB 锁记录（可能无并发或 performance_schema 未采集）")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {"locks": rows, "row_count": len(rows), "truncated": truncated}, warnings


class GetBlockingTreeTool(ObserverTool):
    name = "get_blocking_tree"
    description = "构建 InnoDB 锁等待阻塞树，关联等待/阻塞会话、SQL 与事务年龄。"
    input_model = GetBlockingTreeInput
    timeout_ms = 15_000
    output_schema = {
        "type": "object",
        "properties": {
            "edges": {"type": "array", "items": {"type": "object"}},
            "roots": {"type": "array", "items": {"type": "object"}},
            "wait_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetBlockingTreeInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT w.REQUESTING_THREAD_ID AS waiting_thread_id, "
            "w.BLOCKING_THREAD_ID AS blocking_thread_id, "
            "wt.PROCESSLIST_ID AS waiting_processlist_id, bt.PROCESSLIST_ID AS blocking_processlist_id, "
            "wt.PROCESSLIST_USER AS waiting_user, bt.PROCESSLIST_USER AS blocking_user, "
            "wt.PROCESSLIST_DB AS waiting_database, bt.PROCESSLIST_DB AS blocking_database, "
            "wt.PROCESSLIST_TIME AS waiting_time_seconds, bt.PROCESSLIST_TIME AS blocking_time_seconds, "
            "we.SQL_TEXT AS waiting_sql_text, be.SQL_TEXT AS blocking_sql_text, "
            "wl.OBJECT_SCHEMA AS object_schema, wl.OBJECT_NAME AS object_name, "
            "wl.LOCK_MODE AS waiting_lock_mode, bl.LOCK_MODE AS blocking_lock_mode "
            "FROM performance_schema.data_lock_waits w "
            "LEFT JOIN performance_schema.threads wt ON wt.THREAD_ID = w.REQUESTING_THREAD_ID "
            "LEFT JOIN performance_schema.threads bt ON bt.THREAD_ID = w.BLOCKING_THREAD_ID "
            "LEFT JOIN performance_schema.events_statements_current we "
            "ON we.THREAD_ID = w.REQUESTING_THREAD_ID "
            "LEFT JOIN performance_schema.events_statements_current be "
            "ON be.THREAD_ID = w.BLOCKING_THREAD_ID "
            "LEFT JOIN performance_schema.data_locks wl "
            "ON wl.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
            "LEFT JOIN performance_schema.data_locks bl "
            "ON bl.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID "
            "LIMIT %s",
            (request.limit + 1,),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        rows = jsonable_rows(rows[: request.limit])

        try:
            trx_rows = context.client.fetch_all(
                "SELECT trx_mysql_thread_id AS processlist_id, "
                "TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS transaction_age_seconds, "
                "TIMESTAMPDIFF(SECOND, trx_wait_started, NOW()) AS transaction_wait_seconds "
                "FROM information_schema.innodb_trx"
            )
            trx_by_pid = {row["processlist_id"]: row for row in jsonable_rows(trx_rows)}
        except DatabasePermissionError:
            trx_by_pid = {}
            warnings.append("缺少 PROCESS 权限，事务年龄/等待时长不可用")
        except DBGuardianError:
            trx_by_pid = {}
            warnings.append("InnoDB 事务信息暂不可用")

        edges: list[dict[str, Any]] = []
        roots: dict[int, dict[str, Any]] = {}
        waiting_threads: set[int] = set()

        for row in rows:
            waiting_sql, waiting_truncated = safe_sql_preview(row.get("waiting_sql_text"))
            blocking_sql, blocking_truncated = safe_sql_preview(row.get("blocking_sql_text"))
            waiting_thread_id = int(row["waiting_thread_id"])
            blocking_thread_id = int(row["blocking_thread_id"])
            waiting_threads.add(waiting_thread_id)

            waiting_trx = trx_by_pid.get(row.get("waiting_processlist_id"), {})
            blocking_trx = trx_by_pid.get(row.get("blocking_processlist_id"), {})
            edge = {
                "waiting_thread_id": waiting_thread_id,
                "blocking_thread_id": blocking_thread_id,
                "waiting_processlist_id": row.get("waiting_processlist_id"),
                "blocking_processlist_id": row.get("blocking_processlist_id"),
                "waiting_user": row.get("waiting_user"),
                "blocking_user": row.get("blocking_user"),
                "waiting_database": row.get("waiting_database"),
                "blocking_database": row.get("blocking_database"),
                "waiting_time_seconds": row.get("waiting_time_seconds"),
                "blocking_time_seconds": row.get("blocking_time_seconds"),
                "waiting_sql_text": waiting_sql,
                "waiting_sql_truncated": waiting_truncated,
                "blocking_sql_text": blocking_sql,
                "blocking_sql_truncated": blocking_truncated,
                "object_schema": row.get("object_schema"),
                "object_name": row.get("object_name"),
                "waiting_lock_mode": row.get("waiting_lock_mode"),
                "blocking_lock_mode": row.get("blocking_lock_mode"),
                "waiting_transaction_age_seconds": waiting_trx.get("transaction_age_seconds"),
                "waiting_transaction_wait_seconds": waiting_trx.get("transaction_wait_seconds"),
                "blocking_transaction_age_seconds": blocking_trx.get("transaction_age_seconds"),
            }
            edges.append(edge)

            root = roots.setdefault(
                blocking_thread_id,
                {
                    "blocking_thread_id": blocking_thread_id,
                    "blocking_processlist_id": row.get("blocking_processlist_id"),
                    "blocking_user": row.get("blocking_user"),
                    "blocking_database": row.get("blocking_database"),
                    "blocking_sql_text": blocking_sql,
                    "blocking_time_seconds": row.get("blocking_time_seconds"),
                    "blocking_lock_mode": row.get("blocking_lock_mode"),
                    "blocking_transaction_age_seconds": blocking_trx.get("transaction_age_seconds"),
                    "waiting_count": 0,
                },
            )
            root["waiting_count"] += 1

        root_list = [root for tid, root in roots.items() if tid not in waiting_threads]
        if not edges:
            warnings.append("当前无锁等待阻塞链")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {
            "edges": edges,
            "roots": root_list,
            "wait_count": len(edges),
            "truncated": truncated,
        }, warnings
