"""表、索引与库容量统计工具（information_schema + performance_schema）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import human_bytes, jsonable_rows


class GetTableStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_name: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=100, ge=1, le=500)


class GetIndexStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_name: str | None = Field(default=None, max_length=64)
    table_name: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=200, ge=1, le=500)


class GetDatabaseSizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_name: str | None = Field(default=None, max_length=64)
    include_system: bool = False
    limit: int = Field(default=100, ge=1, le=200)


class GetTableStatsTool(ObserverTool):
    name = "get_table_stats"
    description = "读取指定库的表规模、估算行数、数据/索引/碎片空间与更新时间。"
    input_model = GetTableStatsInput
    timeout_ms = 20_000
    output_schema = {
        "type": "object",
        "properties": {
            "database_name": {"type": "string"},
            "tables": {"type": "array", "items": {"type": "object"}},
            "row_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetTableStatsInput = params  # type: ignore[assignment]
        database_name = request.database_name or context.client.database
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT TABLE_SCHEMA AS database_name, TABLE_NAME AS table_name, ENGINE AS engine, "
            "TABLE_ROWS AS estimated_rows, DATA_LENGTH AS data_bytes, "
            "INDEX_LENGTH AS index_bytes, DATA_FREE AS free_bytes, "
            "AUTO_INCREMENT AS auto_increment, TABLE_COLLATION AS collation, "
            "CREATE_TIME AS create_time, UPDATE_TIME AS update_time "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY (COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) DESC, TABLE_NAME "
            "LIMIT %s",
            (database_name, request.limit + 1),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        tables: list[dict[str, Any]] = []
        for row in jsonable_rows(rows[: request.limit]):
            data_bytes = int(row.get("data_bytes") or 0)
            index_bytes = int(row.get("index_bytes") or 0)
            tables.append(
                {
                    "database_name": row.get("database_name"),
                    "table_name": row.get("table_name"),
                    "engine": row.get("engine"),
                    "estimated_rows": row.get("estimated_rows"),
                    "data_bytes": data_bytes,
                    "index_bytes": index_bytes,
                    "total_bytes": data_bytes + index_bytes,
                    "data_readable": human_bytes(data_bytes),
                    "index_readable": human_bytes(index_bytes),
                    "free_bytes": row.get("free_bytes"),
                    "auto_increment": row.get("auto_increment"),
                    "collation": row.get("collation"),
                    "create_time": row.get("create_time"),
                    "update_time": row.get("update_time"),
                }
            )

        warnings.append("TABLE_ROWS 为 InnoDB 估算值，统计信息可能滞后")
        if not tables:
            warnings.append(f"库 {database_name} 无可见基础表")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 条")

        return {
            "database_name": database_name,
            "tables": tables,
            "row_count": len(tables),
            "truncated": truncated,
        }, warnings


class GetIndexStatsTool(ObserverTool):
    name = "get_index_stats"
    description = "读取索引结构、基数与读写次数，标记从未被读取的索引（需人工确认）。"
    input_model = GetIndexStatsInput
    timeout_ms = 20_000
    output_schema = {
        "type": "object",
        "properties": {
            "database_name": {"type": "string"},
            "indexes": {"type": "array", "items": {"type": "object"}},
            "index_count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetIndexStatsInput = params  # type: ignore[assignment]
        database_name = request.database_name or context.client.database
        warnings: list[str] = []
        rows = context.client.fetch_all(
            "SELECT s.TABLE_SCHEMA AS database_name, s.TABLE_NAME AS table_name, "
            "s.INDEX_NAME AS index_name, s.NON_UNIQUE AS non_unique, "
            "s.SEQ_IN_INDEX AS seq_in_index, s.COLUMN_NAME AS column_name, "
            "s.CARDINALITY AS cardinality, s.INDEX_TYPE AS index_type, "
            "COALESCE(u.COUNT_STAR, 0) AS count_star, "
            "COALESCE(u.COUNT_READ, 0) AS count_read, "
            "COALESCE(u.COUNT_FETCH, 0) AS count_fetch "
            "FROM information_schema.STATISTICS s "
            "LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage u "
            "ON u.OBJECT_SCHEMA = s.TABLE_SCHEMA AND u.OBJECT_NAME = s.TABLE_NAME "
            "AND u.INDEX_NAME = s.INDEX_NAME "
            "WHERE s.TABLE_SCHEMA = %s AND (%s IS NULL OR s.TABLE_NAME = %s) "
            "ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX "
            "LIMIT %s",
            (database_name, request.table_name, request.table_name, request.limit + 1),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in jsonable_rows(rows[: request.limit]):
            key = (str(row["database_name"]), str(row["table_name"]), str(row["index_name"]))
            index = grouped.get(key)
            if index is None:
                index = {
                    "database_name": row["database_name"],
                    "table_name": row["table_name"],
                    "index_name": row["index_name"],
                    "unique": bool(int(row["non_unique"]) == 0),
                    "index_type": row["index_type"],
                    "columns": [],
                    "count_star": row["count_star"],
                    "count_read": row["count_read"],
                    "count_fetch": row["count_fetch"],
                }
                grouped[key] = index
            index["columns"].append(
                {
                    "seq_in_index": row["seq_in_index"],
                    "column_name": row["column_name"],
                    "cardinality": row["cardinality"],
                }
            )

        indexes = list(grouped.values())
        read_unused = [item for item in indexes if int(item["count_read"] or 0) == 0]
        for item in indexes:
            item["read_unused"] = int(item["count_read"] or 0) == 0

        if not indexes:
            warnings.append(f"库 {database_name} 无可见索引")
        if read_unused:
            warnings.append(
                f"{len(read_unused)} 个索引在统计周期内 count_read=0；"
                "不能仅据此判定可删除，需结合业务与统计重置时间"
            )
        warnings.append("cardinality 为估算值，统计信息可能滞后")
        if truncated:
            warnings.append(f"索引列行数已截断到 {request.limit} 行，可能未包含完整组合索引")

        return {
            "database_name": database_name,
            "indexes": indexes,
            "index_count": len(indexes),
            "truncated": truncated,
        }, warnings


class GetDatabaseSizeTool(ObserverTool):
    name = "get_database_size"
    description = "按库汇总数据/索引/碎片空间与表数量，默认排除系统库。"
    input_model = GetDatabaseSizeInput
    timeout_ms = 20_000
    output_schema = {
        "type": "object",
        "properties": {
            "databases": {"type": "array", "items": {"type": "object"}},
            "total_bytes": {"type": "integer"},
            "total_readable": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetDatabaseSizeInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        conditions = ["TABLE_TYPE = 'BASE TABLE'"]
        params_list: list[Any] = []
        if request.database_name:
            conditions.append("TABLE_SCHEMA = %s")
            params_list.append(request.database_name)
        if not request.include_system:
            conditions.append(
                "TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')"
            )
        rows = context.client.fetch_all(
            "SELECT TABLE_SCHEMA AS database_name, COUNT(*) AS table_count, "
            "SUM(COALESCE(DATA_LENGTH, 0)) AS data_bytes, "
            "SUM(COALESCE(INDEX_LENGTH, 0)) AS index_bytes, "
            "SUM(COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0)) AS total_bytes, "
            "SUM(COALESCE(DATA_FREE, 0)) AS free_bytes "
            f"FROM information_schema.TABLES WHERE {' AND '.join(conditions)} "
            "GROUP BY TABLE_SCHEMA ORDER BY total_bytes DESC LIMIT %s",
            (*params_list, request.limit + 1),
            max_rows=request.limit + 1,
        )
        truncated = len(rows) > request.limit
        databases: list[dict[str, Any]] = []
        for row in jsonable_rows(rows[: request.limit]):
            total_bytes = int(row.get("total_bytes") or 0)
            databases.append(
                {
                    **row,
                    "data_bytes": int(row.get("data_bytes") or 0),
                    "index_bytes": int(row.get("index_bytes") or 0),
                    "total_bytes": total_bytes,
                    "free_bytes": int(row.get("free_bytes") or 0),
                    "total_readable": human_bytes(total_bytes),
                }
            )
        total_bytes = sum(int(item["total_bytes"]) for item in databases)

        warnings.append("容量按 information_schema 统计值汇总，可能与磁盘实际占用存在差异")
        if not databases:
            warnings.append("无可统计的数据库（可能无权限或库为空）")
        if truncated:
            warnings.append(f"结果已截断到 {request.limit} 个库")

        return {
            "databases": databases,
            "total_bytes": total_bytes,
            "total_readable": human_bytes(total_bytes),
            "truncated": truncated,
        }, warnings
