"""W01 集成测试：PostgreSQL LAB 连接、只读权限与错误归一化。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.errors import (
    DatabaseAuthError,
    DatabasePermissionError,
    DatabaseUnavailableError,
)
from database.postgres.capabilities import (
    AVAILABLE,
    DENIED,
    NOT_SUPPORTED,
    PARTIAL,
    UNKNOWN,
    detect_pg_capabilities,
)
from database.postgres.connection import PgClient

pytestmark = pytest.mark.integration


def test_identity_and_readonly_session(pg_ro_client: PgClient) -> None:
    identity = pg_ro_client.identity()
    assert identity.database_id == "lab_pg_01"
    assert identity.server_version.startswith(("12.", "16."))
    assert identity.transaction_read_only == 1
    assert identity.user
    assert identity.database == "dg_lab_pg"


def test_read_seed_tables_and_row_limit(pg_ro_client: PgClient) -> None:
    rows = pg_ro_client.fetch_all("SELECT count(*) AS c FROM customers")
    assert int(rows[0]["c"]) == 40
    rows = pg_ro_client.fetch_all("SELECT count(*) AS c FROM orders")
    assert int(rows[0]["c"]) == 200

    limited = PgClient(
        pg_ro_client._dsn,  # noqa: SLF001 - 测试内复用已解析 DSN
        database_id="lab_pg_01",
        read_only=True,
        max_result_rows=7,
    )
    rows = limited.fetch_all("SELECT order_id FROM orders ORDER BY order_id")
    assert len(rows) == 7


def test_readonly_account_cannot_write(pg_ro_client: PgClient) -> None:
    with pytest.raises(DatabasePermissionError) as exc_info:
        pg_ro_client.execute(
            "INSERT INTO customers(customer_id, name, email) VALUES (9999, 'x', 'x@x')"
        )
    assert exc_info.value.code.value == "DB_PERMISSION_DENIED"
    assert "password" not in str(exc_info.value.details).lower()


def test_wrong_password_maps_to_auth_error(settings) -> None:
    base = settings.pg_ro_pg_dsn()
    assert base is not None
    client = PgClient(
        replace(base, password="wrong-password"),
        database_id="lab_pg_01",
        read_only=True,
        connect_timeout_s=2,
    )
    with pytest.raises(DatabaseAuthError) as exc_info:
        client.identity()
    assert exc_info.value.code.value == "DB_AUTH_FAILED"


def test_unreachable_port_maps_to_unavailable(settings) -> None:
    base = settings.pg_ro_pg_dsn()
    assert base is not None
    client = PgClient(
        replace(base, port=1),
        database_id="lab_pg_01",
        read_only=True,
        connect_timeout_s=1,
    )
    with pytest.raises(DatabaseUnavailableError) as exc_info:
        client.identity()
    assert exc_info.value.code.value == "DB_UNAVAILABLE"


def test_capability_report_is_explicit(pg_ro_client: PgClient) -> None:
    report = detect_pg_capabilities(pg_ro_client)
    allowed = {AVAILABLE, PARTIAL, DENIED, NOT_SUPPORTED, UNKNOWN}
    assert report.server_version.startswith(("12.", "16."))
    assert report.pg_stat_activity in allowed
    assert report.pg_locks in allowed
    assert report.pg_blocking_pids in allowed
    assert report.pg_stat_statements in allowed
