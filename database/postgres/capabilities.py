"""PostgreSQL 能力探测（W01/W02 共用）。

返回 AVAILABLE / PARTIAL / DENIED / NOT_SUPPORTED / UNKNOWN，不把权限不足
解释为“没有故障”，也不伪造扩展或视图存在。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.errors import DBGuardianError
from database.postgres.connection import PgClient

AVAILABLE = "AVAILABLE"
PARTIAL = "PARTIAL"
DENIED = "DENIED"
NOT_SUPPORTED = "NOT_SUPPORTED"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PgCapabilityReport:
    database_id: str
    server_version: str
    pg_stat_statements: str
    pg_stat_activity: str
    pg_locks: str
    pg_blocking_pids: str
    warnings: list[str] = field(default_factory=list)


def detect_pg_capabilities(client: PgClient) -> PgCapabilityReport:
    identity = client.identity()
    warnings: list[str] = []

    try:
        rows = client.fetch_all(
            "SELECT count(*) AS installed FROM pg_extension WHERE extname = 'pg_stat_statements'"
        )
        installed = int(rows[0]["installed"]) if rows else 0
    except DBGuardianError as exc:
        installed = 0
        warnings.append(f"pg_extension 探测失败：{exc.code.value}")

    if installed:
        try:
            client.fetch_all("SELECT 1 FROM pg_stat_statements LIMIT 1")
            stat_statements = AVAILABLE
        except DBGuardianError as exc:
            stat_statements = PARTIAL
            warnings.append(f"pg_stat_statements 不可查询：{exc.code.value}")
    else:
        stat_statements = NOT_SUPPORTED
        warnings.append("pg_stat_statements 未安装；查询统计能力不可用")

    def _privilege(relation: str, label: str) -> str:
        try:
            rows = client.fetch_all(
                "SELECT has_table_privilege(current_user, %s, 'SELECT') AS allowed",
                (relation,),
            )
            allowed = bool(rows[0]["allowed"]) if rows else False
            if not allowed:
                warnings.append(f"{label} 缺少 SELECT 权限")
                return DENIED
            return AVAILABLE
        except DBGuardianError as exc:
            warnings.append(f"{label} 能力探测失败：{exc.code.value}")
            return UNKNOWN

    activity = _privilege("pg_catalog.pg_stat_activity", "pg_stat_activity")
    locks = _privilege("pg_catalog.pg_locks", "pg_locks")
    try:
        rows = client.fetch_all(
            "SELECT has_function_privilege(current_user, 'pg_blocking_pids(integer)', 'EXECUTE') AS allowed"
        )
        allowed = bool(rows[0]["allowed"]) if rows else False
        if allowed:
            blocking = AVAILABLE
        else:
            blocking = DENIED
            warnings.append("pg_blocking_pids 缺少 EXECUTE 权限")
    except DBGuardianError as exc:
        blocking = UNKNOWN
        warnings.append(f"pg_blocking_pids 能力探测失败：{exc.code.value}")

    return PgCapabilityReport(
        database_id=identity.database_id,
        server_version=identity.server_version,
        pg_stat_statements=stat_statements,
        pg_stat_activity=activity,
        pg_locks=locks,
        pg_blocking_pids=blocking,
        warnings=warnings,
    )


__all__ = [
    "AVAILABLE",
    "DENIED",
    "NOT_SUPPORTED",
    "PARTIAL",
    "UNKNOWN",
    "PgCapabilityReport",
    "detect_pg_capabilities",
]
