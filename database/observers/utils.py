"""观察工具公共函数：脱敏、SQL 安全校验、数值转换与结果规范化。"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

_LITERAL_PATTERN = re.compile(r"'(?:\\.|''|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.S)
_NUMBER_LITERAL = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_FIRST_TOKEN = re.compile(r"^([A-Za-z]+)")
_DANGEROUS_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"CALL|LOAD|HANDLER|LOCK|UNLOCK|SET|USE|INTO|OUTFILE|DUMPFILE)\b",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_sql_literals(sql: str) -> str:
    """将 SQL 中的字符串与数字字面量替换为 ?，避免泄漏业务数据。"""
    masked = _LITERAL_PATTERN.sub("?", sql)
    masked = _NUMBER_LITERAL.sub("?", masked)
    return _WHITESPACE.sub(" ", masked).strip()


def truncate_text(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def safe_sql_preview(sql: str | None, limit: int = 200) -> tuple[str | None, bool]:
    if sql is None:
        return None, False
    masked = mask_sql_literals(sql)
    return truncate_text(masked, limit)


def strip_leading_comments(sql: str) -> str:
    text = sql.lstrip()
    while True:
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return ""
            text = text[end + 2 :].lstrip()
            continue
        if text.startswith("--") or text.startswith("#"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline + 1 :].lstrip()
            continue
        return text


def validate_explain_sql(sql: str, *, max_length: int = 20_000) -> tuple[bool, str]:
    """保守的只读校验：只允许单条 SELECT/WITH 查询。"""
    if not sql or not sql.strip():
        return False, "SQL 不能为空"
    if len(sql) > max_length:
        return False, f"SQL 长度超过 {max_length} 字符"
    stripped = strip_leading_comments(sql)
    if not stripped:
        return False, "SQL 去掉注释后为空"
    token = _FIRST_TOKEN.match(stripped)
    if not token or token.group(1).upper() not in {"SELECT", "WITH"}:
        return False, "仅允许 SELECT/WITH 只读查询"

    literal_masked = _LITERAL_PATTERN.sub("?", stripped)
    statements = [part.strip() for part in literal_masked.split(";") if part.strip()]
    if len(statements) > 1:
        return False, "仅允许单条语句"
    if _DANGEROUS_KEYWORDS.search(statements[0]):
        return False, "查询包含不允许的写/管理关键字"
    return True, ""


def timer_ps_to_ms(value: int | None) -> float | None:
    if value is None:
        return None
    return round(int(value) / 1_000_000_000, 3)


def human_bytes(value: int | float | None) -> str | None:
    if value is None:
        return None
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return None


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return "<binary>"
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [jsonable(row) for row in rows]  # type: ignore[misc]
