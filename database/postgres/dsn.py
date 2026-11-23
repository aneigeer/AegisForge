"""PostgreSQL DSN 解析与脱敏表示。

支持 `postgresql://user:password@host:port/database?sslmode=require`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

_ALLOWED_SCHEMES = {"postgresql", "postgres"}


class PgDsnParseError(ValueError):
    """DSN 语法错误；消息不包含密码。"""


@dataclass(frozen=True)
class PgDsn:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    sslmode: str = "prefer"

    @property
    def redacted(self) -> str:
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


def parse_pg_dsn(dsn: str) -> PgDsn:
    if not dsn or not dsn.strip():
        raise PgDsnParseError("DSN 不能为空")

    parsed = urlparse(dsn)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise PgDsnParseError("DSN 必须以 postgresql:// 或 postgres:// 开头")
    if not parsed.hostname:
        raise PgDsnParseError("DSN 缺少主机名")
    if not parsed.username:
        raise PgDsnParseError("DSN 缺少用户名")

    database = parsed.path.lstrip("/")
    if not database:
        raise PgDsnParseError("DSN 缺少数据库名")

    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise PgDsnParseError("DSN 端口非法") from exc
    port = 5432 if parsed_port is None else parsed_port
    if not 1 <= port <= 65535:
        raise PgDsnParseError("DSN 端口超出范围")

    query = parse_qs(parsed.query)
    sslmode = (query.get("sslmode", ["prefer"])[0] or "prefer").lower()
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise PgDsnParseError("sslmode 非法")

    return PgDsn(
        host=parsed.hostname,
        port=port,
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
        database=unquote(database),
        sslmode=sslmode,
    )


__all__ = ["PgDsn", "PgDsnParseError", "parse_pg_dsn"]
