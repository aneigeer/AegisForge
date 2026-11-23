"""本地身份认证集成测试（真实 MySQL 控制库，W05）。"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from core.auth.repository import AuthRepository, new_session_id
from core.auth.service import AuthService
from core.config import Settings
from core.errors import AuthExpiredError, AuthInvalidError, ConfigError
from core.security.models import AuthMethod, Principal
from core.security.passwords import hash_password, verify_password
from core.security.roles import Permission, Role
from core.security.tokens import hash_token, new_session_token
from database.mysql.connection import MysqlClient

pytestmark = pytest.mark.integration


@pytest.fixture
def auth_repository(control_client: MysqlClient) -> AuthRepository:
    return AuthRepository(control_client)


@pytest.fixture
def auth_service(control_client: MysqlClient, settings: Settings) -> AuthService:
    return AuthService(control_client, settings)


@pytest.fixture
def auth_env(
    control_client: MysqlClient,
    admin_client: MysqlClient,
    auth_repository: AuthRepository,
) -> Any:
    """创建唯一测试租户/用户，测试后按外键顺序清理。"""
    suffix = uuid4().hex[:10]
    tenants: list[str] = []
    users: list[str] = []

    def add_tenant(label: str = "tenant") -> str:
        tenant_id = f"auth_{label}_{suffix}"
        control_client.execute(
            "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
            "VALUES (%s,%s,'ACTIVE',UTC_TIMESTAMP(6))",
            (tenant_id, tenant_id),
        )
        tenants.append(tenant_id)
        return tenant_id

    def add_user(
        grants: list[tuple[str, tuple[Role, ...]]],
        *,
        password: str | None = None,
        status: str = "ACTIVE",
        label: str = "user",
    ) -> tuple[str, str]:
        user_id = f"auth_{label}_{suffix}"
        chosen_password = password or f"Aa1!{secrets.token_hex(8)}"
        auth_repository.create_or_update_user(
            user_id, f"Auth {label}", hash_password(chosen_password), status=status
        )
        for tenant_id, roles in grants:
            for role in roles:
                auth_repository.upsert_membership(tenant_id, user_id, role)
        users.append(user_id)
        return user_id, chosen_password

    tenant_id = add_tenant("main")
    user_id, password = add_user(
        [(tenant_id, (Role.OPERATOR, Role.VIEWER))], label="main"
    )

    try:
        yield {
            "repository": auth_repository,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "password": password,
            "add_tenant": add_tenant,
            "add_user": add_user,
        }
    finally:
        for user in users:
            admin_client.execute(
                "DELETE FROM dg_system.auth_sessions WHERE user_id = %s", (user,)
            )
        for tenant in tenants:
            admin_client.execute(
                "DELETE FROM dg_system.auth_sessions WHERE tenant_id = %s", (tenant,)
            )
        for user in users:
            admin_client.execute(
                "DELETE FROM dg_system.memberships WHERE user_id = %s", (user,)
            )
        for user in users:
            admin_client.execute(
                "DELETE FROM dg_system.users WHERE user_id = %s", (user,)
            )
        for tenant in tenants:
            admin_client.execute(
                "DELETE FROM dg_system.memberships WHERE tenant_id = %s", (tenant,)
            )
            admin_client.execute(
                "DELETE FROM dg_system.tenants WHERE tenant_id = %s", (tenant,)
            )


def _login_entry(env: dict[str, Any], service: AuthService) -> Any:
    return service.login(env["user_id"], env["password"], env["tenant_id"])


def _role_set(principal: Principal) -> set[Role]:
    return set(principal.roles)


def test_login_success_roles_come_from_database(
    auth_env: dict[str, Any], auth_service: AuthService, settings: Settings
) -> None:
    result = _login_entry(auth_env, auth_service)

    assert result.principal.user_id == auth_env["user_id"]
    assert result.principal.tenant_id == auth_env["tenant_id"]
    assert _role_set(result.principal) == {Role.OPERATOR, Role.VIEWER}
    assert result.principal.session_id
    assert result.principal.auth_method == AuthMethod.PASSWORD
    assert Permission.TASK_CREATE in result.principal.permissions
    assert result.token.startswith("dg_")
    assert result.expires_at.tzinfo is not None

    row = auth_env["repository"].get_session_by_token_hash(hash_token(result.token))
    assert row is not None
    assert row["user_id"] == auth_env["user_id"]
    assert row["tenant_id"] == auth_env["tenant_id"]
    assert row["status"] == "ACTIVE"
    assert row["auth_method"] == AuthMethod.PASSWORD.value
    assert row["created_at"].tzinfo is not None
    assert row["expires_at"].tzinfo is not None
    ttl = (row["expires_at"] - row["created_at"]).total_seconds()
    assert abs(ttl - settings.auth_token_ttl_s) < 1.0
    # token 只存摘要，密码只存 scrypt 哈希。
    assert row["token_hash"] == hash_token(result.token)
    user = auth_env["repository"].get_user(auth_env["user_id"])
    assert user is not None
    assert auth_env["password"] not in user["password_hash"]
    assert verify_password(auth_env["password"], user["password_hash"])


def test_login_single_tenant_is_auto_selected(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    result = auth_service.login(auth_env["user_id"], auth_env["password"])
    assert result.principal.tenant_id == auth_env["tenant_id"]
    assert _role_set(result.principal) == {Role.OPERATOR, Role.VIEWER}


def test_login_wrong_password_is_invalid(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    with pytest.raises(AuthInvalidError):
        auth_service.login(auth_env["user_id"], "wrong-password", auth_env["tenant_id"])


def test_login_unknown_user_is_invalid(auth_service: AuthService) -> None:
    with pytest.raises(AuthInvalidError):
        auth_service.login("auth_missing_user", "any-password", "tenant_local")


def test_login_inactive_user_is_invalid(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    tenant_id = auth_env["add_tenant"]("disabled")
    user_id, password = auth_env["add_user"](
        [(tenant_id, (Role.VIEWER,))], status="DISABLED", label="disabled"
    )
    with pytest.raises(AuthInvalidError):
        auth_service.login(user_id, password, tenant_id)


def test_login_multi_tenant_requires_explicit_tenant(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    other_tenant = auth_env["add_tenant"]("other")
    user_id, password = auth_env["add_user"](
        [
            (auth_env["tenant_id"], (Role.VIEWER,)),
            (other_tenant, (Role.APPROVER,)),
        ],
        label="multi",
    )

    with pytest.raises(AuthInvalidError):
        auth_service.login(user_id, password)
    with pytest.raises(AuthInvalidError):
        auth_service.login(user_id, password, "auth_not_a_member")

    result = auth_service.login(user_id, password, other_tenant)
    assert result.principal.tenant_id == other_tenant
    assert _role_set(result.principal) == {Role.APPROVER}


def test_authenticate_success_and_tampered_token(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    result = _login_entry(auth_env, auth_service)
    principal = auth_service.authenticate(result.token)
    assert principal.user_id == auth_env["user_id"]
    assert principal.tenant_id == auth_env["tenant_id"]
    assert principal.session_id == result.principal.session_id
    assert principal.auth_method == AuthMethod.TOKEN
    assert _role_set(principal) == {Role.OPERATOR, Role.VIEWER}

    row = auth_env["repository"].get_session_by_token_hash(hash_token(result.token))
    assert row is not None and row["last_used_at"] is not None

    with pytest.raises(AuthInvalidError):
        auth_service.authenticate("dg_" + secrets.token_urlsafe(32))
    with pytest.raises(AuthInvalidError):
        auth_service.authenticate("")


def test_authenticate_rejects_disabled_user_and_removed_membership(
    auth_env: dict[str, Any], auth_service: AuthService, control_client: MysqlClient
) -> None:
    disabled_result = _login_entry(auth_env, auth_service)
    control_client.execute(
        "UPDATE dg_system.users SET status = 'DISABLED' WHERE user_id = %s",
        (auth_env["user_id"],),
    )
    with pytest.raises(AuthInvalidError):
        auth_service.authenticate(disabled_result.token)

    control_client.execute(
        "UPDATE dg_system.users SET status = 'ACTIVE' WHERE user_id = %s",
        (auth_env["user_id"],),
    )
    membership_result = _login_entry(auth_env, auth_service)
    control_client.execute(
        "DELETE FROM dg_system.memberships WHERE user_id = %s AND tenant_id = %s",
        (auth_env["user_id"], auth_env["tenant_id"]),
    )
    with pytest.raises(AuthInvalidError):
        auth_service.authenticate(membership_result.token)


def test_authenticate_expired_session(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    raw_token, token_hash = new_session_token()
    now = datetime.now(timezone.utc)
    auth_env["repository"].insert_session(
        new_session_id(),
        auth_env["user_id"],
        auth_env["tenant_id"],
        token_hash,
        AuthMethod.PASSWORD,
        now - timedelta(hours=2),
        now - timedelta(hours=1),
    )
    with pytest.raises(AuthExpiredError):
        auth_service.authenticate(raw_token)


def test_logout_is_idempotent_and_revokes_session(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    result = _login_entry(auth_env, auth_service)
    assert auth_service.logout(result.token) is True
    assert auth_service.logout(result.token) is False
    assert auth_service.logout("") is False
    assert auth_service.logout("dg_" + secrets.token_urlsafe(32)) is False
    with pytest.raises(AuthInvalidError):
        auth_service.authenticate(result.token)

    row = auth_env["repository"].get_session_by_token_hash(hash_token(result.token))
    assert row is not None
    assert row["status"] == "REVOKED"
    assert row["revoked_at"] is not None


def test_rotate_invalidates_old_token(
    auth_env: dict[str, Any], auth_service: AuthService
) -> None:
    result = _login_entry(auth_env, auth_service)
    old_session_id = result.principal.session_id

    rotated = auth_service.rotate(result.token)
    assert rotated.token != result.token
    assert rotated.principal.session_id != old_session_id
    assert rotated.principal.tenant_id == auth_env["tenant_id"]
    assert rotated.principal.user_id == auth_env["user_id"]

    with pytest.raises(AuthInvalidError):
        auth_service.authenticate(result.token)

    principal = auth_service.authenticate(rotated.token)
    assert principal.session_id == rotated.principal.session_id
    assert _role_set(principal) == {Role.OPERATOR, Role.VIEWER}

    old_row = auth_env["repository"].get_session_by_token_hash(hash_token(result.token))
    assert old_row is not None
    assert old_row["status"] == "REVOKED"
    assert old_row["revoked_at"] is not None

    new_row = auth_env["repository"].get_session_by_token_hash(hash_token(rotated.token))
    assert new_row is not None
    assert new_row["status"] == "ACTIVE"
    assert new_row["rotated_from"] == old_session_id

    sessions = auth_env["repository"].list_sessions(auth_env["user_id"])
    assert len(sessions) == 2


def test_dev_principal_reads_memberships_without_touching_user_local(
    control_client: MysqlClient, settings: Settings, auth_repository: AuthRepository
) -> None:
    dev_settings = settings.model_copy(update={"auth_mode": "dev_local", "env": "dev"})
    principal = AuthService(control_client, dev_settings).dev_principal()

    assert principal.user_id == "user_local"
    assert principal.tenant_id == "tenant_local"
    assert principal.auth_method == AuthMethod.DEV_LOCAL
    assert principal.session_id is None

    expected = {
        Role(membership["role"])
        for membership in auth_repository.get_memberships("user_local")
        if membership["tenant_id"] == principal.tenant_id
    }
    assert expected
    assert _role_set(principal) == expected


@pytest.mark.parametrize(
    "overrides",
    [{"auth_mode": "token"}, {"env": "prod"}],
)
def test_dev_principal_rejected_outside_dev_local(
    control_client: MysqlClient, settings: Settings, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ConfigError):
        AuthService(control_client, settings.model_copy(update=overrides)).dev_principal()
