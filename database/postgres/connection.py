"""PostgreSQL 连接适配层（W01）。

- 只读会话通过 `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` 与数据库账号
  最小权限共同保证；账号本身只有 SELECT。
- 所有错误归一化为 DBGuardian 结构化错误，不泄漏 DSN/密码。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from pydantic import SecretStr

from core.errors import (
    DatabaseAuthError,
    DatabasePermissionError,
    DatabaseQueryError,
    DatabaseTimeoutError,
    DatabaseUnavailableError,
)
from database.postgres.dsn import PgDsn, parse_pg_dsn

_AUTH_SQLSTATES = {"28P01", "28000"}
_PERMISSION_SQLSTATES = {"42501", "2BP01", "25006"}
_TIMEOUT_SQLSTATES = {"57014"}
_UNAVAILABLE_SQLSTATES = {"08001", "08006", "08003", "3D000", "57P01", "57P02", "57P03"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VERSION_PATTERN = __import__("re").compile(r"\d+(?:\.\d+)+")


def _normalize_version(raw: str) -> str:
    match = _VERSION_PATTERN.search(raw)
    return match.group(0) if match else raw


def _coerce_dsn(dsn: str | SecretStr | PgDsn) -> PgDsn:
    if isinstance(dsn, PgDsn):
        return dsn
    if isinstance(dsn, SecretStr):
        return parse_pg_dsn(dsn.get_secret_value())
    return parse_pg_dsn(dsn)


def _sqlstate(exc: psycopg.Error) -> str | None:
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return str(code) if code else None


def _raise_connect_error(exc: psycopg.Error, database_id: str) -> None:
    state = _sqlstate(exc)
    details = {"database_id": database_id, "sqlstate": state}
    text = str(exc).lower()
    if state in _AUTH_SQLSTATES or "authentication failed" in text or "password" in text and "fail" in text:
        raise DatabaseAuthError("数据库认证失败，请检查账号配置", details=details) from None
    raise DatabaseUnavailableError("无法连接数据库", details=details) from None


def _raise_pg_error(exc: psycopg.Error, database_id: str) -> None:
    state = _sqlstate(exc)
    details = {"database_id": database_id, "sqlstate": state}
    if state in _AUTH_SQLSTATES:
        raise DatabaseAuthError("数据库认证失败，请检查账号配置", details=details) from None
    if state in _PERMISSION_SQLSTATES:
        raise DatabasePermissionError("数据库权限不足，操作被拒绝", details=details) from None
    if state in _TIMEOUT_SQLSTATES:
        raise DatabaseTimeoutError("SQL 执行超过语句时限", details=details) from None
    if state in _UNAVAILABLE_SQLSTATES:
        raise DatabaseUnavailableError("数据库连接不可用", details=details) from None
    raise DatabaseQueryError("数据库查询执行失败", details=details) from None


@dataclass(frozen=True)
class PgIdentity:
    database_id: str
    user: str
    database: str
    server_version: str
    transaction_read_only: int
    collected_at: str


class PgClient:
    """单一 DSN 的连接客户端；只读/写入由不同账号与不同实例区分。"""

    def __init__(
        self,
        dsn: str | SecretStr | PgDsn,
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

    def _connect(self) -> psycopg.Connection:
        try:
            conn = psycopg.connect(
                host=self._dsn.host,
                port=self._dsn.port,
                user=self._dsn.user,
                password=self._dsn.password,
                dbname=self._dsn.database,
                sslmode=self._dsn.sslmode,
                connect_timeout=int(max(1, self._connect_timeout_s)),
                autocommit=True,
                row_factory=dict_row,
                options=f"-c statement_timeout={self._statement_timeout_ms}",
            )
            self._apply_session(conn)
            return conn
        except psycopg.OperationalError as exc:
            _raise_connect_error(exc, self._database_id)
        except OSError:
            raise DatabaseUnavailableError(
                "数据库网络不可达", details={"database_id": self._database_id}
            ) from None
        raise AssertionError("unreachable")

    def _apply_session(self, conn: psycopg.Connection) -> None:
        if self._read_only:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def ping(self) -> float:
        started = time.perf_counter()
        with self.connection() as conn:
            try:
                conn.execute("SELECT 1").fetchone()
            except psycopg.Error as exc:
                _raise_pg_error(exc, self._database_id)
        return round((time.perf_counter() - started) * 1000, 2)

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> int:
        with self.connection() as conn:
            try:
                result = conn.execute(sql, params or ())
                return int(result.rowcount or 0)
            except psycopg.Error as exc:
                _raise_pg_error(exc, self._database_id)
        raise AssertionError("unreachable")

    def fetch_all(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
        *,
        max_rows: int | None = None,
    ) -> list[dict[str, object]]:
        limit = max_rows or self._max_result_rows
        with self.connection() as conn:
            try:
                cursor = conn.execute(sql, params or ())
                rows = cursor.fetchmany(limit)
                return [dict(row) for row in rows]
            except psycopg.Error as exc:
                _raise_pg_error(exc, self._database_id)
        raise AssertionError("unreachable")

    def identity(self) -> PgIdentity:
        with self.connection() as conn:
            try:
                row = conn.execute(
                    """
                    SELECT version() AS server_version,
                           current_user AS current_user_name,
                           current_database() AS current_database,
                           current_setting('transaction_read_only') AS transaction_read_only
                    """
                ).fetchone()
            except psycopg.Error as exc:
                _raise_pg_error(exc, self._database_id)

        if not row:
            raise DatabaseQueryError(
                "未获取到数据库身份信息", details={"database_id": self._database_id}
            )
        return PgIdentity(
            database_id=self._database_id,
            user=str(row["current_user_name"]),
            database=str(row["current_database"] or ""),
            server_version=_normalize_version(str(row["server_version"])),
            transaction_read_only=int(str(row["transaction_read_only"]) == "on"),
            collected_at=_now_iso(),
        )


__all__ = ["PgClient", "PgIdentity"]
