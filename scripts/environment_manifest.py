r"""生成本地环境指纹（W01），不包含任何凭据。

用法：
    .\.venv\Scripts\python.exe scripts\environment_manifest.py
输出：
    deploy/environment-manifest.mysql.json
    deploy/environment-manifest.postgresql.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.config import get_settings
from database.mysql.connection import MysqlClient
from database.postgres.capabilities import detect_pg_capabilities
from database.postgres.connection import PgClient
ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _write(name: str, payload: dict) -> Path:
    DEPLOY.mkdir(parents=True, exist_ok=True)
    path = DEPLOY / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _migration_version(settings) -> str:
    try:
        control = MysqlClient(
            settings.control_mysql_dsn(),
            database_id="dg_system",
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
        )
        rows = control.fetch_all(
            "SELECT version FROM dg_system.schema_migrations ORDER BY version"
        )
        return ",".join(str(row["version"]) for row in rows) if rows else "NONE"
    except Exception:
        return "UNKNOWN"


def main() -> int:
    settings = get_settings()
    now = datetime.now(timezone.utc).isoformat()
    migration_version = _migration_version(settings)
    written: list[Path] = []

    mysql = MysqlClient(
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
    ).identity()
    written.append(
        _write(
            "environment-manifest.mysql.json",
            {
                "environment_id": settings.database_id,
                "tenant_id_placeholder": "tenant_local",
                "db_engine": "mysql",
                "db_version": mysql.server_version,
                "image_digest": None,
                "network_id": "lab_net",
                "config_version": "w01-local",
                "migration_version": migration_version,
                "created_at": now,
                "capabilities": {"transaction_read_only": mysql.transaction_read_only},
                "status": "LOCAL_DEV_NOT_VERIFIED",
            },
        )
    )

    if settings.pg_ro_dsn is None:
        print("未配置 DG_PG_RO_DSN，跳过 PostgreSQL Manifest")
    else:
        pg_client = PgClient(
            settings.pg_ro_dsn,
            database_id=settings.pg_database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
        )
        identity = pg_client.identity()
        capabilities = detect_pg_capabilities(pg_client)
        written.append(
            _write(
                "environment-manifest.postgresql.json",
                {
                    "environment_id": settings.pg_database_id,
                    "tenant_id_placeholder": "tenant_local",
                    "db_engine": "postgresql",
                    "db_version": identity.server_version,
                    "image_digest": None,
                    "network_id": "lab_net",
                    "config_version": "w01-local",
                    "migration_version": migration_version,
                    "created_at": now,
                    "capabilities": {
                        "pg_stat_statements": capabilities.pg_stat_statements,
                        "pg_stat_activity": capabilities.pg_stat_activity,
                        "pg_locks": capabilities.pg_locks,
                        "pg_blocking_pids": capabilities.pg_blocking_pids,
                    },
                    "status": "LOCAL_DEV_NOT_VERIFIED",
                },
            )
        )

    for path in written:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
