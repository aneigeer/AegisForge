"""MySQL DSN 解析与脱敏表示。

支持 `mysql://user:password@host:port/database?charset=utf8mb4`
以及 SQLAlchemy 风格的 `mysql+pymysql://...`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

_ALLOWED_SCHEMES = {"mysql", "mysql+pymysql"}


class DsnParseError(ValueError):
    """DSN 语法错误；消息不包含密码。"""


@dataclass(frozen=True)
class MysqlDsn:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    charset: str = "utf8mb4"

    @property
    def redacted(self) -> str:
        return f"mysql://{self.user}:***@{self.host}:{self.port}/{self.database}"


def parse_mysql_dsn(dsn: str) -> MysqlDsn:
    if not dsn or not dsn.strip():
        raise DsnParseError("DSN 不能为空")

    parsed = urlparse(dsn)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise DsnParseError("DSN 必须以 mysql:// 或 mysql+pymysql:// 开头")
    if not parsed.hostname:
        raise DsnParseError("DSN 缺少主机名")
    if not parsed.username:
        raise DsnParseError("DSN 缺少用户名")

    database = parsed.path.lstrip("/")
    if not database:
        raise DsnParseError("DSN 缺少数据库名")

    try:
        port = parsed.port or 3306
    except ValueError as exc:
        raise DsnParseError("DSN 端口非法") from exc
    if not 1 <= port <= 65535:
        raise DsnParseError("DSN 端口超出范围")

    query = parse_qs(parsed.query)
    charset = query.get("charset", ["utf8mb4"])[0] or "utf8mb4"

    return MysqlDsn(
        host=parsed.hostname,
        port=port,
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
        database=unquote(database),
        charset=charset,
    )
