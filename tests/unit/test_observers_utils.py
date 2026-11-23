from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from database.observers.utils import (
    human_bytes,
    jsonable,
    mask_sql_literals,
    safe_sql_preview,
    timer_ps_to_ms,
    validate_explain_sql,
)


def test_mask_sql_literals_removes_business_values() -> None:
    sql = "SELECT * FROM orders WHERE name = '张三' AND status = \"paid\" AND id = 2001"
    masked = mask_sql_literals(sql)
    assert "张三" not in masked
    assert "paid" not in masked
    assert masked.startswith("SELECT * FROM orders WHERE name = ? AND status = ?")


def test_safe_sql_preview_truncates_and_masks() -> None:
    sql = "SELECT '" + "秘" * 20 + "', " + "column_name_" * 30
    text, truncated = safe_sql_preview(sql, limit=50)
    assert text is not None
    assert truncated is True
    assert len(text) == 50
    assert "秘" not in text


def test_validate_explain_sql_allows_select_and_cte() -> None:
    assert validate_explain_sql("SELECT 1")[0]
    assert validate_explain_sql("  /* comment */ SELECT * FROM t")[0]
    assert validate_explain_sql("WITH cte AS (SELECT 1) SELECT * FROM cte")[0]


def test_validate_explain_sql_rejects_writes_and_multiple_statements() -> None:
    for sql in (
        "",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DROP TABLE t",
        "SELECT 1; SELECT 2",
        "SELECT * INTO OUTFILE '/tmp/x' FROM t",
        "WITH cte AS (SELECT 1) UPDATE t SET a = 1",
        "/* unterminated",
    ):
        ok, reason = validate_explain_sql(sql)
        assert not ok, sql
        assert reason


def test_timer_and_byte_conversion() -> None:
    assert timer_ps_to_ms(1_000_000_000) == 1.0
    assert timer_ps_to_ms(None) is None
    assert human_bytes(1024) == "1.00 KiB"
    assert human_bytes(0) == "0.00 B"


def test_jsonable_converts_mysql_types() -> None:
    value = jsonable(
        {
            "dt": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "dec": Decimal("1.25"),
            "nested": [Decimal("2.5")],
        }
    )
    assert value["dt"] == "2026-01-01T00:00:00+00:00"
    assert value["dec"] == 1.25
    assert value["nested"] == [2.5]
