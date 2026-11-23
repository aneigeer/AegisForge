"""服务信息工具：版本与关键参数（白名单读取）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import jsonable_rows

_ALLOWED_SETTINGS = (
    "autocommit",
    "character_set_server",
    "collation_server",
    "event_scheduler",
    "gtid_mode",
    "innodb_buffer_pool_instances",
    "innodb_buffer_pool_size",
    "innodb_flush_log_at_trx_commit",
    "innodb_flush_method",
    "innodb_io_capacity",
    "innodb_lock_wait_timeout",
    "innodb_log_file_size",
    "innodb_stats_auto_recalc",
    "innodb_stats_persistent",
    "interactive_timeout",
    "join_buffer_size",
    "lock_wait_timeout",
    "log_bin",
    "long_query_time",
    "lower_case_table_names",
    "max_allowed_packet",
    "max_connections",
    "max_heap_table_size",
    "max_user_connections",
    "net_read_timeout",
    "net_write_timeout",
    "performance_schema",
    "read_only",
    "slow_query_log",
    "sort_buffer_size",
    "sql_mode",
    "super_read_only",
    "sync_binlog",
    "system_time_zone",
    "table_definition_cache",
    "table_open_cache",
    "thread_cache_size",
    "time_zone",
    "tmp_table_size",
    "transaction_isolation",
    "version_comment",
    "wait_timeout",
)


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetDbVersionInput(NoParams):
    pass


class GetDbSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] | None = Field(default=None, max_length=100)
    limit: int = Field(default=200, ge=1, le=200)


class GetDbVersionTool(ObserverTool):
    name = "get_db_version"
    description = "采集 MySQL 版本、编译信息、当前账号与全局只读标记。"
    input_model = GetDbVersionInput
    timeout_ms = 10_000
    output_schema = {
        "type": "object",
        "properties": {
            "version": {"type": "string"},
            "version_comment": {"type": "string"},
            "compile_os": {"type": "string"},
            "compile_machine": {"type": "string"},
            "current_user": {"type": "string"},
            "current_database": {"type": ["string", "null"]},
            "server_time": {"type": "string"},
            "performance_schema_enabled": {"type": "integer"},
            "global_read_only": {"type": "integer"},
            "super_read_only": {"type": "integer"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        rows = jsonable_rows(
            context.client.fetch_all(
                "SELECT @@version AS version, @@version_comment AS version_comment, "
                "@@version_compile_os AS compile_os, @@version_compile_machine AS compile_machine, "
                "@@performance_schema AS performance_schema_enabled, @@read_only AS global_read_only, "
                "@@super_read_only AS super_read_only, CURRENT_USER() AS current_user_name, "
                "DATABASE() AS current_database, NOW(3) AS server_time"
            )
        )
        if not rows:
            return {}, ["未读取到版本信息"]
        row = rows[0]
        data = dict(row)
        data["current_user"] = data.pop("current_user_name")
        return data, []


class GetDbSettingsTool(ObserverTool):
    name = "get_db_settings"
    description = "按白名单读取 MySQL 关键参数，拒绝读取未授权/敏感配置项。"
    input_model = GetDbSettingsInput
    timeout_ms = 10_000
    output_schema = {
        "type": "object",
        "properties": {
            "settings": {"type": "object"},
            "requested": {"type": ["array", "null"], "items": {"type": "string"}},
            "excluded": {"type": "array", "items": {"type": "string"}},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: GetDbSettingsInput = params  # type: ignore[assignment]
        warnings: list[str] = []
        excluded: list[str] = []

        if request.names:
            normalized = list(dict.fromkeys(name.strip().lower() for name in request.names if name.strip()))
            wanted = [name for name in normalized if name in _ALLOWED_SETTINGS]
            excluded = [name for name in normalized if name not in _ALLOWED_SETTINGS]
            if excluded:
                warnings.append(f"已忽略 {len(excluded)} 个非白名单配置项")
        else:
            wanted = list(_ALLOWED_SETTINGS)

        if not wanted:
            return {"settings": {}, "requested": request.names, "excluded": excluded}, warnings

        wanted = wanted[: request.limit]
        placeholders = ",".join(["%s"] * len(wanted))
        rows = context.client.fetch_all(
            "SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS value "
            f"FROM performance_schema.global_variables WHERE VARIABLE_NAME IN ({placeholders}) "
            "ORDER BY VARIABLE_NAME",
            tuple(wanted),
        )
        settings = {str(row["name"]): str(row["value"]) for row in rows}
        if not settings:
            warnings.append("未读取到任何配置项，请检查 performance_schema 是否可用")
        return {"settings": settings, "requested": request.names, "excluded": excluded}, warnings
