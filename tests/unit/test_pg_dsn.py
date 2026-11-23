"""W01 单元测试：PostgreSQL DSN 解析、脱敏与非法输入。"""

from __future__ import annotations

import pytest

from database.postgres.dsn import PgDsnParseError, parse_pg_dsn


def test_parse_basic_dsn() -> None:
    dsn = parse_pg_dsn("postgresql://dg_ro:secret@127.0.0.1:55432/dg_lab_pg")
    assert dsn.host == "127.0.0.1"
    assert dsn.port == 55432
    assert dsn.user == "dg_ro"
    assert dsn.database == "dg_lab_pg"
    assert dsn.sslmode == "prefer"
    assert dsn.password == "secret"


def test_parse_postgres_scheme_and_options() -> None:
    dsn = parse_pg_dsn("postgres://u:p@db.local/db?sslmode=disable")
    assert dsn.port == 5432
    assert dsn.sslmode == "disable"


def test_redacted_never_contains_password() -> None:
    dsn = parse_pg_dsn("postgresql://dg_ro:secret@127.0.0.1:55432/dg_lab_pg")
    assert "secret" not in dsn.redacted
    assert "***" in dsn.redacted
    assert repr(dsn).find("secret") == -1


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "   ",
        "mysql://u:p@h/db",
        "postgresql:///db",
        "postgresql://u@h",
        "postgresql://u:p@h/db?sslmode=bogus",
        "postgresql://u:p@h:0/db",
    ],
)
def test_invalid_dsn_rejected_without_leaking_password(dsn: str) -> None:
    with pytest.raises(PgDsnParseError) as exc_info:
        parse_pg_dsn(dsn)
    assert "p\"@" not in str(exc_info.value)
