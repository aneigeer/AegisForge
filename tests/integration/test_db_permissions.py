from __future__ import annotations

import uuid

import pytest

from core.errors import DatabasePermissionError
from database.mysql.connection import MysqlClient

pytestmark = pytest.mark.integration


def test_readonly_can_select(ro_client: MysqlClient) -> None:
    rows = ro_client.fetch_all("SELECT COUNT(*) AS total FROM dg_lab.seed_orders")
    assert int(rows[0]["total"]) >= 200


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dg_lab.seed_orders (order_id, customer_id, status, amount_cents, created_at) "
        "VALUES (999001, 1001, 'paid', 100, NOW())",
        "UPDATE dg_lab.seed_orders SET status = 'paid' WHERE order_id = 2001",
        "DELETE FROM dg_lab.seed_orders WHERE order_id = 2001",
        "CREATE TABLE dg_lab.perm_probe_ro (id INT)",
        "TRUNCATE TABLE dg_lab.seed_orders",
        "DROP TABLE dg_lab.seed_orders",
    ],
)
def test_readonly_write_operations_are_denied(ro_client: MysqlClient, sql: str) -> None:
    with pytest.raises(DatabasePermissionError) as excinfo:
        ro_client.execute(sql)
    assert excinfo.value.code.value == "DB_PERMISSION_DENIED"


def test_readonly_cannot_create_database(ro_client: MysqlClient) -> None:
    with pytest.raises(DatabasePermissionError):
        ro_client.execute(f"CREATE DATABASE dg_ro_should_not_{uuid.uuid4().hex[:8]}")


def test_sandbox_account_can_write_and_cleanup(rw_client: MysqlClient) -> None:
    table = f"dg_lab.perm_probe_rw_{uuid.uuid4().hex[:8]}"
    try:
        rw_client.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, value VARCHAR(32))")
        rw_client.execute(f"INSERT INTO {table} (id, value) VALUES (1, 'a')")
        rw_client.execute(f"UPDATE {table} SET value = 'b' WHERE id = 1")
        rows = rw_client.fetch_all(f"SELECT value FROM {table} WHERE id = 1")
        assert rows[0]["value"] == "b"
        rw_client.execute(f"DELETE FROM {table} WHERE id = 1")
    finally:
        rw_client.execute(f"DROP TABLE IF EXISTS {table}")


def test_sandbox_account_cannot_create_database(rw_client: MysqlClient) -> None:
    with pytest.raises(DatabasePermissionError):
        rw_client.execute(f"CREATE DATABASE dg_rw_should_not_{uuid.uuid4().hex[:8]}")


def test_committed_row_survives_reconnect(rw_client: MysqlClient, ro_client: MysqlClient) -> None:
    order_id = 900000 + int(uuid.uuid4().hex[:4], 16)
    try:
        rw_client.execute(
            "INSERT INTO dg_lab.seed_orders "
            "(order_id, customer_id, status, amount_cents, created_at) "
            "VALUES (%s, 1001, 'pending', 100, NOW())",
            (order_id,),
        )

        rows = ro_client.fetch_all(
            "SELECT status FROM dg_lab.seed_orders WHERE order_id = %s", (order_id,)
        )
        assert rows and rows[0]["status"] == "pending"
    finally:
        rw_client.execute("DELETE FROM dg_lab.seed_orders WHERE order_id = %s", (order_id,))
