"""MySQL 连接适配层。

设计要点：
- 只读客户端在会话层启用 `TRANSACTION READ ONLY`，同时依赖数据库账号的最小权限；
- 所有连接错误归一化为 DBGuardian 结构化错误，不泄漏 DSN/密码；
- 每次采集都记录 database_id 与采集时间，供后续工具结果使用。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import pymysql
from pymysql.cursors import DictCursor
from pydantic import SecretStr

from core.errors import (
    DatabaseAuthError,
    DatabasePermissionError,
    DatabaseQueryError,
    DatabaseTimeoutError,
    DatabaseUnavailableError,
)
from database.mysql.dsn import MysqlDsn, parse_mysql_dsn

_AUTH_ERROR_CODES = {1045}
_PERMISSION_ERROR_CODES = {1044, 1142, 1143, 1227, 1290, 1792}
_TIMEOUT_ERROR_CODES = {3024}
_UNAVAILABLE_ERROR_CODES = {1049, 2002, 2003, 2005, 2006, 2013}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_dsn(dsn: str | SecretStr | MysqlDsn) -> MysqlDsn:
    if isinstance(dsn, MysqlDsn):
        return dsn
    if isinstance(dsn, SecretStr):
        return parse_mysql_dsn(dsn.get_secret_value())
    return parse_mysql_dsn(dsn)


def _error_message(exc: pymysql.MySQLError, database_id: str) -> tuple[int | None, str]:
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    text = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
    return code, text


def _raise_connect_error(exc: pymysql.MySQLError, database_id: str) -> None:
    code, text = _error_message(exc, database_id)
    lower = text.lower()
    details = {"database_id": database_id, "mysql_error_code": code}

    if code in _AUTH_ERROR_CODES or "access denied for user" in lower:
        raise DatabaseAuthError("数据库认证失败，请检查账号配置", details=details) from None
    if code in _PERMISSION_ERROR_CODES or "command denied" in lower:
        raise DatabasePermissionError("数据库账号无连接/操作权限", details=details) from None
    if "timed out" in lower or "timeout" in lower:
        raise DatabaseTimeoutError("连接数据库超时", details=details) from None
    raise DatabaseUnavailableError("无法连接数据库", details=details) from None


def _raise_query_error(exc: pymysql.MySQLError, database_id: str) -> None:
    code, text = _error_message(exc, database_id)
    lower = text.lower()
    details = {"database_id": database_id, "mysql_error_code": code}

    if code in _PERMISSION_ERROR_CODES or "command denied" in lower or "read only" in lower:
        raise DatabasePermissionError("数据库权限不足，操作被拒绝", details=details) from None
    if code in _TIMEOUT_ERROR_CODES or "maximum statement execution time exceeded" in lower:
        raise DatabaseTimeoutError("SQL 执行超过语句时限", details=details) from None
    if code in _UNAVAILABLE_ERROR_CODES:
        raise DatabaseUnavailableError("数据库连接不可用", details=details) from None
    raise DatabaseQueryError("数据库查询执行失败", details=details) from None


@dataclass(frozen=True)
class MysqlIdentity:
    database_id: str
    user: str
    database: str
    server_version: str
    version_comment: str
    transaction_read_only: int
    collected_at: str


class MysqlClient:
    """单一 DSN 的连接客户端；读/写路径由不同账号与不同实例区分。"""

    def __init__(
        self,
        dsn: str | SecretStr | MysqlDsn,
        *,
        database_id: str,
        read_only: bool,
        connect_timeout_s: float = 5.0,
        statement_timeout_ms: int = 30_000,
        max_result_rows: int = 500,
    ) -> None:
        self._dsn = _coerce_dsn(dsn)
        self._database_id = database_id
        self._read_only = read_only
        self._connect_timeout_s = connect_timeout_s
        self._statement_timeout_ms = statement_timeout_ms
        self._max_result_rows = max_result_rows

    @property
    def database_id(self) -> str:
        return self._database_id

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def database(self) -> str:
        return self._dsn.database

    @property
    def redacted_dsn(self) -> str:
        return self._dsn.redacted

    def _connect_params(self) -> dict[str, object]:
        return {
            "host": self._dsn.host,
            "port": self._dsn.port,
            "user": self._dsn.user,
            "password": self._dsn.password,
            "database": self._dsn.database,
            "charset": self._dsn.charset,
            "cursorclass": DictCursor,
            "autocommit": True,
            "connect_timeout": int(max(1, self._connect_timeout_s)),
            "read_timeout": int(max(1, self._statement_timeout_ms / 1000)),
            "write_timeout": int(max(1, self._statement_timeout_ms / 1000)),
        }

    def _apply_session(self, conn: pymysql.Connection) -> None:
        with conn.cursor() as cur:
            if self._read_only:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION max_execution_time = %s", (self._statement_timeout_ms,))

    def connect(self) -> pymysql.Connection:
        try:
            conn = pymysql.connect(**self._connect_params())
            self._apply_session(conn)
            return conn
        except pymysql.MySQLError as exc:
            _raise_connect_error(exc, self._database_id)
        except OSError:
            raise DatabaseUnavailableError(
                "数据库网络不可达", details={"database_id": self._database_id}
            ) from None
        raise AssertionError("unreachable")

    @contextmanager
    def connection(self) -> Iterator[pymysql.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def ping(self) -> float:
        started = time.perf_counter()
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            except pymysql.MySQLError as exc:
                _raise_query_error(exc, self._database_id)
        return round((time.perf_counter() - started) * 1000, 2)

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> int:
        """执行单条语句并归一化错误；返回受影响行数。"""
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    return int(cur.execute(sql, params or ()))
            except pymysql.MySQLError as exc:
                _raise_query_error(exc, self._database_id)
        raise AssertionError("unreachable")

    def fetch_all(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
        *,
        max_rows: int | None = None,
    ) -> list[dict[str, object]]:
        """执行只读查询并限制返回行数。"""
        limit = max_rows or self._max_result_rows
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    rows = cur.fetchmany(limit)
                    return [dict(row) for row in rows]
            except pymysql.MySQLError as exc:
                _raise_query_error(exc, self._database_id)
        raise AssertionError("unreachable")

    def identity(self) -> MysqlIdentity:
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT VERSION() AS server_version,
                               CURRENT_USER() AS current_user_name,
                               DATABASE() AS current_database,
                               @@version_comment AS version_comment,
                               @@transaction_read_only AS transaction_read_only
                        """
                    )
                    row = cur.fetchone()
            except pymysql.MySQLError as exc:
                _raise_query_error(exc, self._database_id)

        if not row:
            raise DatabaseQueryError(
                "未获取到数据库身份信息", details={"database_id": self._database_id}
            )
        return MysqlIdentity(
            database_id=self._database_id,
            user=str(row["current_user_name"]),
            database=str(row["current_database"] or ""),
            server_version=str(row["server_version"]),
            version_comment=str(row["version_comment"] or ""),
            transaction_read_only=int(row["transaction_read_only"] or 0),
            collected_at=_now_iso(),
        )
