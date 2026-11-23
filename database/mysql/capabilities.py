"""MySQL 能力探测：确保工具在能力缺失时明确降级，而不是返回假数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.errors import DBGuardianError, DatabasePermissionError
from database.mysql.connection import MysqlClient

_REQUIRED_TABLES = (
    "threads",
    "events_statements_current",
    "events_statements_summary_by_digest",
    "data_locks",
    "data_lock_waits",
    "table_io_waits_summary_by_index_usage",
    "global_variables",
    "setup_consumers",
)


@dataclass(frozen=True)
class CapabilityReport:
    server_version: str
    performance_schema_enabled: bool
    process_privilege: bool
    tables: dict[str, bool]
    consumers: dict[str, str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_version": self.server_version,
            "performance_schema_enabled": self.performance_schema_enabled,
            "process_privilege": self.process_privilege,
            "tables": self.tables,
            "consumers": self.consumers,
            "warnings": self.warnings,
        }


def detect_capabilities(client: MysqlClient) -> CapabilityReport:
    warnings: list[str] = []
    rows = client.fetch_all(
        "SELECT VERSION() AS version, @@performance_schema AS ps_enabled"
    )
    server_version = str(rows[0]["version"])
    ps_enabled = bool(int(rows[0]["ps_enabled"]))
    if not ps_enabled:
        warnings.append("performance_schema 未开启，多数观察工具将不可用")

    placeholders = ",".join(["%s"] * len(_REQUIRED_TABLES))
    table_rows = client.fetch_all(
        "SELECT TABLE_NAME AS table_name FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = 'performance_schema' AND TABLE_NAME IN ({placeholders})",
        _REQUIRED_TABLES,
    )
    available = {name: False for name in _REQUIRED_TABLES}
    for row in table_rows:
        available[str(row["table_name"])] = True
    for name, exists in available.items():
        if not exists:
            warnings.append(f"缺少 performance_schema.{name}")

    consumers: dict[str, str] = {}
    try:
        consumer_rows = client.fetch_all(
            "SELECT NAME AS name, ENABLED AS enabled FROM performance_schema.setup_consumers "
            "WHERE NAME IN ('events_statements_current','events_statements_history',"
            "'events_statements_history_long','events_transactions_current')"
        )
        consumers = {str(row["name"]): str(row["enabled"]) for row in consumer_rows}
    except DBGuardianError:
        warnings.append("无法读取 performance_schema.setup_consumers")

    process_privilege = True
    try:
        client.fetch_all("SELECT COUNT(*) AS n FROM information_schema.innodb_trx")
    except DatabasePermissionError:
        process_privilege = False
        warnings.append("账号缺少 PROCESS 权限：活动会话/事务年龄信息不完整")

    return CapabilityReport(
        server_version=server_version,
        performance_schema_enabled=ps_enabled,
        process_privilege=process_privilege,
        tables=available,
        consumers=consumers,
        warnings=warnings,
    )
