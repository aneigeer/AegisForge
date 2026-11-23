r"""初始化本机隔离 PostgreSQL LAB（幂等）。

用法：
    .\.venv\Scripts\python.exe scripts\bootstrap_pg_lab.py

环境变量（.env 或进程环境）：
    DG_PG_ADMIN_DSN      管理员连接串（只用于初始化）
    DG_PG_RO_PASSWORD    只读账号密码
    DG_PG_RW_PASSWORD    沙箱写账号密码

脚本不会启动/停止系统 PostgreSQL 服务，也不会把密码写入仓库或日志。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "database" / "postgres" / "init" / "01_schema.sql"
ROLES_FILE = ROOT / "database" / "postgres" / "init" / "00_roles.sql"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"缺少环境变量 {name}")
    return value


def _connect(dsn: str) -> psycopg.Connection:
    params = conninfo_to_dict(dsn)
    return psycopg.connect(**params, autocommit=True)


def main() -> int:
    load_dotenv(ROOT / ".env")
    admin_dsn = os.environ.get("DG_PG_ADMIN_DSN", "").strip()
    if not admin_dsn:
        raise SystemExit("缺少 DG_PG_ADMIN_DSN（管理员连接串，仅用于初始化）")
    ro_password = _env("DG_PG_RO_PASSWORD")
    rw_password = _env("DG_PG_RW_PASSWORD")

    parameters = conninfo_to_dict(admin_dsn)
    database = parameters.get("dbname", "postgres")

    with _connect(admin_dsn) as conn:
        with conn.cursor() as cur:
            # 本地 LAB 的 pg_hba 使用 scram-sha-256；显式设置会话级加密方式，避免
            # 服务端默认 md5 生成无法用于 scram 认证的密码哈希。
            cur.execute("SET password_encryption = 'scram-sha-256'")
            for role, password in (("dg_ro_pg", ro_password), ("dg_rw_pg", rw_password)):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cur.fetchone():
                    cur.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
                else:
                    cur.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
                print(f"角色就绪：{role}")

            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'dg_lab_pg'")
            if not cur.fetchone():
                cur.execute("CREATE DATABASE dg_lab_pg OWNER dg_rw_pg ENCODING 'UTF8'")
                print("数据库已创建：dg_lab_pg")
            else:
                print("数据库已存在：dg_lab_pg")

    lab_dsn = _replace_database(admin_dsn, "dg_lab_pg")
    with _connect(lab_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        print("schema 已应用（幂等）：customers / orders")

    # 打印脱敏摘要，绝不输出密码。
    safe = {key: ("***" if key in {"password"} else value) for key, value in parameters.items()}
    print(f"完成：host={safe.get('host')} port={safe.get('port')} db={database} schema={SCHEMA_FILE.name}")
    return 0


def _replace_database(dsn: str, database: str) -> str:
    parameters = conninfo_to_dict(dsn)
    parameters["dbname"] = database
    return " ".join(f"{key}={value}" for key, value in parameters.items())


if __name__ == "__main__":
    sys.exit(main())
