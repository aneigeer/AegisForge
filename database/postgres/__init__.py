"""PostgreSQL 适配层（W01）：DSN、连接与能力探测。"""

from database.postgres.capabilities import (
    AVAILABLE,
    DENIED,
    NOT_SUPPORTED,
    PARTIAL,
    UNKNOWN,
    PgCapabilityReport,
    detect_pg_capabilities,
)
from database.postgres.connection import PgClient, PgIdentity
from database.postgres.dsn import PgDsn, PgDsnParseError, parse_pg_dsn

__all__ = [
    "AVAILABLE",
    "DENIED",
    "NOT_SUPPORTED",
    "PARTIAL",
    "UNKNOWN",
    "PgCapabilityReport",
    "PgClient",
    "PgDsn",
    "PgDsnParseError",
    "PgIdentity",
    "detect_pg_capabilities",
    "parse_pg_dsn",
]
