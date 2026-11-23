from __future__ import annotations

from dataclasses import replace

import pytest

from core.errors import (
    DatabaseAuthError,
    DatabaseTimeoutError,
    DatabaseUnavailableError,
)
from database.mysql.connection import MysqlClient
from database.mysql.dsn import parse_mysql_dsn

pytestmark = pytest.mark.integration


def test_ping_reports_latency(ro_client: MysqlClient) -> None:
    latency = ro_client.ping()
    assert latency >= 0
    assert latency < 5000


def test_identity_contains_version_and_identity(ro_client: MysqlClient) -> None:
    identity = ro_client.identity()
    assert identity.database_id == ro_client.database_id
    assert identity.server_version.startswith("8.")
    assert identity.user.startswith("dg_ro@")
    assert identity.database == "dg_lab"
    assert identity.transaction_read_only == 1
    assert identity.collected_at.endswith("+00:00")


def test_wrong_password_returns_structured_auth_error(settings) -> None:
    dsn = replace(settings.ro_mysql_dsn(), password="definitely_wrong_password")
    client = MysqlClient(dsn, database_id="lab_mysql_01", read_only=True, connect_timeout_s=2)
    with pytest.raises(DatabaseAuthError) as excinfo:
        client.ping()
    assert "definitely_wrong_password" not in str(excinfo.value)
    assert "definitely_wrong_password" not in repr(excinfo.value.details)


def test_unavailable_port_returns_structured_error(settings) -> None:
    dsn = parse_mysql_dsn("mysql://dg_ro:x@127.0.0.1:3399/dg_lab")
    client = MysqlClient(dsn, database_id="lab_mysql_01", read_only=True, connect_timeout_s=1)
    with pytest.raises((DatabaseUnavailableError, DatabaseTimeoutError)):
        client.ping()


def test_redacted_dsn_never_contains_password(settings) -> None:
    dsn = parse_mysql_dsn(settings.ro_dsn.get_secret_value())
    assert dsn.password
    assert dsn.password not in MysqlClient(settings.ro_dsn, database_id="x", read_only=True).redacted_dsn
