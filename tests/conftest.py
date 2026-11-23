"""pytest 共享夹具。

集成测试需要本机 MySQL 8 已执行 scripts/bootstrap_mysql.py，且根目录存在 .env。
配置缺失时集成测试跳过，单元测试不受影响。
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from core.config import Settings
from database.mysql.connection import MysqlClient
from database.postgres.connection import PgClient

# 测试进程默认关闭启动恢复：`TestClient(app)` 会触发 lifespan，历史未完成任务
# （本机实测 763 条非终态）会被逐条 transition / emit，而每条 SQL 都新建 TCP 连接，
# 形成「启动恢复风暴」。仅影响本测试进程；tests/ 下没有用例依赖 recover_on_startup=True。
os.environ.setdefault("DG_RECOVER_ON_STARTUP", "false")


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        pytest.skip(f"集成测试需要 .env 配置：{exc.error_count()} 个字段缺失/非法")


@pytest.fixture(scope="session")
def ro_client(settings: Settings) -> MysqlClient:
    return MysqlClient(
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


@pytest.fixture(scope="session")
def rw_client(settings: Settings) -> MysqlClient:
    return MysqlClient(
        settings.rw_dsn,
        database_id=settings.database_id,
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


@pytest.fixture(scope="session")
def control_client(settings: Settings) -> MysqlClient:
    return MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


@pytest.fixture(scope="session")
def admin_client(settings: Settings) -> MysqlClient:
    if settings.admin_dsn is None:
        pytest.skip("未配置 DG_ADMIN_DSN，无法执行测试清理")
    return MysqlClient(
        settings.admin_dsn,
        database_id="dg_admin",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


@pytest.fixture(scope="session")
def pg_ro_client(settings: Settings) -> PgClient:
    if settings.pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN，跳过 PostgreSQL 集成测试")
    return PgClient(
        settings.pg_ro_dsn,
        database_id=settings.pg_database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )


@pytest.fixture(scope="session")
def pg_rw_client(settings: Settings) -> PgClient:
    if settings.pg_rw_dsn is None:
        pytest.skip("未配置 DG_PG_RW_DSN，跳过 PostgreSQL 写账号测试")
    return PgClient(
        settings.pg_rw_dsn,
        database_id=settings.pg_database_id,
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
