from __future__ import annotations

import pytest

from database.mysql.dsn import DsnParseError, parse_mysql_dsn


def test_parse_default_port_and_charset() -> None:
    dsn = parse_mysql_dsn("mysql://dg_ro:pass@localhost/dg_lab")
    assert dsn.port == 3306
    assert dsn.charset == "utf8mb4"
    assert dsn.database == "dg_lab"


def test_parse_percent_encoded_password() -> None:
    dsn = parse_mysql_dsn("mysql://dg_ro:p%40ss%3Aword@127.0.0.1:3307/dg_lab?charset=utf8mb4")
    assert dsn.password == "p@ss:word"
    assert dsn.port == 3307


def test_redacted_hides_password() -> None:
    dsn = parse_mysql_dsn("mysql+pymysql://dg_ro:super_secret@127.0.0.1:3306/dg_lab")
    assert "super_secret" not in dsn.redacted
    assert dsn.redacted == "mysql://dg_ro:***@127.0.0.1:3306/dg_lab"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "postgresql://u:p@localhost/db",
        "mysql://localhost/dg_lab",
        "mysql://dg_ro:p@localhost",
        "mysql://dg_ro:p@localhost:99999/dg_lab",
    ],
)
def test_invalid_dsn(value: str) -> None:
    with pytest.raises(DsnParseError):
        parse_mysql_dsn(value)
