"""本地账号引导脚本（W05）。

用法：
    python scripts/bootstrap_auth.py --user-id <id> --tenant-id <id> \
        --role Operator --role Viewer [--display-name <name>]

密码只从环境变量 ``DG_AUTH_BOOTSTRAP_PASSWORD`` 或交互式 getpass 读取；
脚本不打印、不写文件、不提供默认密码。租户必须已存在。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from core.auth.repository import AuthRepository
from core.config import Settings
from core.security.passwords import hash_password
from core.security.roles import Role, role_from_text
from database.mysql.connection import MysqlClient

_PASSWORD_ENV = "DG_AUTH_BOOTSTRAP_PASSWORD"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建/更新 DBGuardian 本地账号")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument(
        "--role",
        dest="roles",
        action="append",
        required=True,
        help="可重复指定，例如 --role Operator --role Viewer",
    )
    parser.add_argument("--display-name", default=None)
    return parser.parse_args(argv)


def _read_password() -> str:
    value = os.environ.get(_PASSWORD_ENV, "")
    if not value:
        value = getpass.getpass("Password: ")
    if not value.strip():
        raise SystemExit(f"密码不能为空（请设置 {_PASSWORD_ENV} 或交互输入）")
    return value


def _normalize_roles(values: list[str]) -> list[Role]:
    roles: list[Role] = []
    for value in values:
        try:
            roles.append(role_from_text(value))
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    return roles


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT / ".env")
    password = _read_password()
    roles = _normalize_roles(args.roles)
    display_name = args.display_name or args.user_id

    client = MysqlClient(
        Settings().control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
    )
    tenant_rows = client.fetch_all(
        "SELECT tenant_id FROM dg_system.tenants WHERE tenant_id = %s LIMIT 1",
        (args.tenant_id,),
    )
    if not tenant_rows:
        raise SystemExit(f"租户不存在：{args.tenant_id}（请先创建租户）")

    repository = AuthRepository(client)
    repository.create_or_update_user(
        args.user_id, display_name, hash_password(password)
    )
    for role in roles:
        repository.upsert_membership(args.tenant_id, args.user_id, role)

    print(f"user={args.user_id}")
    print(f"tenant={args.tenant_id}")
    print(f"roles={','.join(sorted(role.value for role in roles))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
