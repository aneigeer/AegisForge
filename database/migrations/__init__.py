"""版本化数据库迁移：按文件名顺序执行，schema_migrations 记录已应用版本。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from core.errors import DatabaseQueryError
from database.mysql.connection import MysqlClient

MIGRATIONS_DIR = Path(__file__).resolve().parent


def split_statements(sql: str) -> list[str]:
    """去掉整行注释并按分号拆分；当前迁移语句不含字面量分号。"""
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def migration_files() -> list[Path]:
    return sorted(
        path
        for path in MIGRATIONS_DIR.glob("*.sql")
        if re.match(r"^\d+_[a-z0-9_]+\.sql$", path.name)
    )


def ensure_migrations(client: MysqlClient) -> list[str]:
    """返回本次新应用的版本列表；重复执行安全。"""
    client.execute(
        "CREATE TABLE IF NOT EXISTS dg_system.schema_migrations ("
        "version VARCHAR(128) PRIMARY KEY, applied_at DATETIME(6) NOT NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    applied_rows = client.fetch_all("SELECT version FROM dg_system.schema_migrations")
    applied = {str(row["version"]) for row in applied_rows}

    newly_applied: list[str] = []
    for path in migration_files():
        version = path.stem
        if version in applied:
            continue
        for statement in split_statements(path.read_text(encoding="utf-8")):
            client.execute(statement)
        try:
            client.execute(
                "INSERT INTO dg_system.schema_migrations (version, applied_at) VALUES (%s, %s)",
                (version, datetime.now(timezone.utc).replace(tzinfo=None)),
            )
        except DatabaseQueryError as exc:
            if exc.details.get("mysql_error_code") != 1062:
                raise
        newly_applied.append(version)
    return newly_applied
