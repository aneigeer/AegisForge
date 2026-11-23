"""本机 MySQL 8 初始化（幂等）。

用法：
    python scripts/bootstrap_mysql.py

所需环境变量（只用于初始化，应用运行时不使用）：
    DG_ADMIN_DSN       管理员 DSN，例如 mysql://root:******@127.0.0.1:3306/mysql
    DG_RO_PASSWORD     只读账号密码
    DG_RW_PASSWORD     沙箱写账号密码
    DG_CONTROL_PASSWORD 控制库账号密码

脚本只创建/更新 dg_lab、dg_system、dg_ro、dg_rw、dg_ctl，不修改其他数据库。
dg_rw 仅限 dg_lab；dg_ctl 仅限 dg_system（任务/事件/证据）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "database" / "mysql" / "init" / "01_schema.sql"

RO_GRANTS = [
    "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_ro'@'%'",
    "GRANT SELECT ON dg_lab.* TO 'dg_ro'@'%'",
    "GRANT SELECT ON performance_schema.* TO 'dg_ro'@'%'",
    # PROCESS 为只读实例级权限：用于 processlist / innodb_trx 观察，不允许写入。
    "GRANT PROCESS ON *.* TO 'dg_ro'@'%'",
]
RW_GRANTS = [
    "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_rw'@'%'",
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON dg_lab.* TO 'dg_rw'@'%'"
    ),
]
CONTROL_BASE_GRANTS = [
    "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_ctl'@'%'",
    # 基础读 + 追加写；不授予事件/证据的 UPDATE/DELETE。
    "GRANT SELECT, INSERT ON dg_system.* TO 'dg_ctl'@'%'",
    # 正向迁移只需 CREATE/ALTER/INDEX；REFERENCES 用于复合外键。
    # W06：**不含 DROP**——需要 DROP/RENAME 的迁移与全部回滚脚本只能由
    # DG_ADMIN_DSN（人工运维路径）执行，见 docs/migrations.md。
    "GRANT CREATE, ALTER, INDEX, REFERENCES ON dg_system.* TO 'dg_ctl'@'%'",
]
CONTROL_WRITE_GRANTS = [
    # 仅可变表授予 UPDATE/DELETE；task_events/evidence 保持只追加。
    "GRANT UPDATE, DELETE ON dg_system.tasks TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.task_steps TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.tool_calls TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.checkpoints TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.tenants TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.users TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.memberships TO 'dg_ctl'@'%'",
    "GRANT UPDATE, DELETE ON dg_system.environments TO 'dg_ctl'@'%'",
    # W05：会话撤销/轮换与租约心跳需要 UPDATE；执行信封保持只追加。
    "GRANT UPDATE ON dg_system.auth_sessions TO 'dg_ctl'@'%'",
    "GRANT UPDATE ON dg_system.worker_leases TO 'dg_ctl'@'%'",
    # W06：审批是状态表，需要条件 UPDATE（无 DELETE，历史由 task_events 承担）。
    # approval_consumptions/step_prepared/step_observed 是只追加证据，
    # **不得**出现在本列表；它们只靠既有的 SELECT, INSERT ON dg_system.*。
    "GRANT UPDATE ON dg_system.approvals TO 'dg_ctl'@'%'",
    # P1-BYOK：供应商配置是**可变配置表**（非只追加证据），改/删/切换 active 需要
    # UPDATE/DELETE；密钥仍以密文存储，删除仅移除配置，不触碰历史事件。
    "GRANT UPDATE, DELETE ON dg_system.tenant_model_providers TO 'dg_ctl'@'%'",
]


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"缺少环境变量 {name}（可写入 .env 后通过 $env: 注入）")
    return value


def _split_statements(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def bootstrap() -> None:
    load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT))
    from database.mysql.dsn import parse_mysql_dsn

    admin = parse_mysql_dsn(_require_env("DG_ADMIN_DSN"))
    ro_password = _require_env("DG_RO_PASSWORD")
    rw_password = _require_env("DG_RW_PASSWORD")
    control_password = _require_env("DG_CONTROL_PASSWORD")

    conn = pymysql.connect(
        host=admin.host,
        port=admin.port,
        user=admin.user,
        password=admin.password,
        database=admin.database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS dg_lab "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS dg_system "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(
                "CREATE USER IF NOT EXISTS 'dg_ro'@'%%' IDENTIFIED BY %s", (ro_password,)
            )
            cur.execute("ALTER USER 'dg_ro'@'%%' IDENTIFIED BY %s", (ro_password,))
            cur.execute(
                "CREATE USER IF NOT EXISTS 'dg_rw'@'%%' IDENTIFIED BY %s", (rw_password,)
            )
            cur.execute("ALTER USER 'dg_rw'@'%%' IDENTIFIED BY %s", (rw_password,))
            cur.execute(
                "CREATE USER IF NOT EXISTS 'dg_ctl'@'%%' IDENTIFIED BY %s",
                (control_password,),
            )
            cur.execute(
                "ALTER USER 'dg_ctl'@'%%' IDENTIFIED BY %s", (control_password,)
            )

            for statement in RO_GRANTS + RW_GRANTS + CONTROL_BASE_GRANTS:
                cur.execute(statement)

            schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
            for statement in _split_statements(schema_sql):
                cur.execute(statement)
    finally:
        conn.close()

    from database.migrations import ensure_migrations
    from database.mysql.connection import MysqlClient
    from database.mysql.dsn import MysqlDsn

    rw_client = MysqlClient(
        MysqlDsn(
            host=admin.host,
            port=admin.port,
            user="dg_ctl",
            password=control_password,
            database="dg_system",
        ),
        database_id="dg_system",
        read_only=False,
    )
    versions = ensure_migrations(rw_client)
    # 迁移完成后再授予可变表的 UPDATE/DELETE（事件/证据表不授予）。
    admin_conn = pymysql.connect(
        host=admin.host,
        port=admin.port,
        user=admin.user,
        password=admin.password,
        database=admin.database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with admin_conn.cursor() as cur:
            for statement in CONTROL_WRITE_GRANTS:
                cur.execute(statement)
    finally:
        admin_conn.close()
    if versions:
        print(f"bootstrap ok: migrations applied={','.join(versions)}")
    else:
        print("bootstrap ok: migrations already up to date")

    print("bootstrap ok: database=dg_lab,dg_system")
    print("bootstrap ok: users=dg_ro(select-only), dg_rw(dg_lab sandbox), dg_ctl(dg_system)")


if __name__ == "__main__":
    bootstrap()
