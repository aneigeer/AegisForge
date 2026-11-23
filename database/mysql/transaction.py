"""显式事务上下文管理器（W06 T01）。

为什么单独成一个模块而不给 `MysqlClient` 加方法：
`database/mysql/connection.py` 的**原始字节**被 W04 故障资产哈希覆盖
（`core/faults/store.py::_HASH_DEPENDENCIES` 包含该文件）。只要改动它，6 个已冻结的
故障 Manifest 就会被判为"fault_script_hash 过期"，36 项 W04 冲击用例直接变红。
因此事务能力放在本模块，语义与设计一致：`BEGIN` → yield → `COMMIT` / 异常 `ROLLBACK`。

核心风险（设计 §4 点名）：现有 `execute()`/`fetch_all()` 每次开新连接且
`autocommit=True`，直接连调三次**等于没有事务**。因此事务内的所有语句必须使用
:func:`execute_in` / :func:`fetch_all_in` 传入本模块 yield 出的同一个 `conn`。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pymysql

from core.errors import DatabasePermissionError
# 复用连接层的错误归一化，保证事务内错误与普通查询的错误码完全一致。
from database.mysql.connection import MysqlClient, _raise_query_error

DEFAULT_MAX_ROWS = 500


@contextmanager
def transaction(client: MysqlClient) -> Iterator[pymysql.Connection]:
    """在**同一个**连接上开启显式事务；只读客户端拒绝开启（fail closed）。"""
    if client.read_only:
        raise DatabasePermissionError(
            "只读连接不允许开启事务",
            details={"database_id": client.database_id},
        )
    conn = client.connect()
    try:
        conn.begin()
        try:
            yield conn
        except BaseException:
            # 任何异常（含业务校验失败）都必须回滚，避免半成品状态被提交。
            try:
                conn.rollback()
            except pymysql.MySQLError as exc:  # pragma: no cover - 连接已断
                _raise_query_error(exc, client.database_id)
            raise
        else:
            conn.commit()
    finally:
        conn.close()


def execute_in(
    client: MysqlClient,
    conn: pymysql.Connection,
    sql: str,
    params: tuple[object, ...] | None = None,
) -> int:
    """在事务连接上执行单条写语句；返回受影响行数。"""
    try:
        with conn.cursor() as cur:
            return int(cur.execute(sql, params or ()))
    except pymysql.MySQLError as exc:
        _raise_query_error(exc, client.database_id)
    raise AssertionError("unreachable")


def fetch_all_in(
    client: MysqlClient,
    conn: pymysql.Connection,
    sql: str,
    params: tuple[object, ...] | None = None,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[dict[str, object]]:
    """在事务连接上执行查询（`SELECT ... FOR UPDATE` 必须走这里才持锁）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchmany(max_rows)
            return [dict(row) for row in rows]
    except pymysql.MySQLError as exc:
        _raise_query_error(exc, client.database_id)
    raise AssertionError("unreachable")


__all__ = ["DEFAULT_MAX_ROWS", "execute_in", "fetch_all_in", "transaction"]
