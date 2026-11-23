"""应用数据库迁移（幂等）。

用法：
    python scripts/migrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from database.migrations import ensure_migrations  # noqa: E402
from database.mysql.connection import MysqlClient  # noqa: E402


def main() -> None:
    settings = Settings()
    client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
    )
    versions = ensure_migrations(client)
    print("migrations applied:", ",".join(versions) if versions else "(none, up to date)")


if __name__ == "__main__":
    main()
