"""W05 认证 API 集成测试：token 模式登录、logout、refresh 与 /me。"""

from __future__ import annotations

import secrets
from typing import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.auth.repository import AuthRepository
from core.config import get_settings
from core.errors import ErrorCode
from core.security.passwords import hash_password
from core.security.roles import Role

pytestmark = pytest.mark.integration


@pytest.fixture
def token_client(monkeypatch) -> Iterator[TestClient]:
    """使用 token 认证模式新建 app；测试结束恢复默认 dev_local 配置缓存。"""
    monkeypatch.setenv("DG_AUTH_MODE", "token")
    get_settings.cache_clear()
    app = create_app()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def dev_local_client(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DG_AUTH_MODE", "dev_local")
    get_settings.cache_clear()
    app = create_app()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def auth_user_factory(admin_client) -> Iterator[callable]:
    """用 admin_client 准备唯一租户/用户；清理统一走 admin_client。"""
    created: list[dict[str, str]] = []

    def make(roles: tuple[Role, ...]) -> dict[str, str]:
        suffix = uuid4().hex[:10]
        tenant_id = f"api_auth_{suffix}"
        user_id = f"api_user_{suffix}"
        password = f"Aa1!{secrets.token_hex(10)}"

        admin_client.execute(
            "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
            "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
            (tenant_id, f"API Auth {suffix}"),
        )
        repository = AuthRepository(admin_client)
        repository.create_or_update_user(
            user_id, "API Auth User", hash_password(password)
        )
        for role in roles:
            repository.upsert_membership(tenant_id, user_id, role)

        record = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "password": password,
        }
        created.append(record)
        return record

    try:
        yield make
    finally:
        for record in created:
            admin_client.execute(
                "DELETE FROM dg_system.auth_sessions WHERE user_id = %s",
                (record["user_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.auth_sessions WHERE tenant_id = %s",
                (record["tenant_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.memberships WHERE user_id = %s",
                (record["user_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.memberships WHERE tenant_id = %s",
                (record["tenant_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.users WHERE user_id = %s",
                (record["user_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.tenants WHERE tenant_id = %s",
                (record["tenant_id"],),
            )


@pytest.fixture
def auth_user(auth_user_factory) -> dict[str, str]:
    return auth_user_factory((Role.OPERATOR, Role.VIEWER))


def _login(client: TestClient, auth_user: dict[str, str]) -> dict:
    response = client.post(
        "/v1/auth/login",
        json={
            "username": auth_user["user_id"],
            "password": auth_user["password"],
            "tenant_id": auth_user["tenant_id"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_missing_authorization_returns_auth_required(token_client: TestClient) -> None:
    response = token_client.get("/v1/auth/me")
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"]["error_code"] == ErrorCode.AUTH_REQUIRED.value


def test_invalid_token_returns_auth_invalid(token_client: TestClient) -> None:
    token = "dg_invalid_token_for_test"
    response = token_client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"]["error_code"] == ErrorCode.AUTH_INVALID.value
    assert token not in response.text


def test_non_bearer_authorization_is_auth_invalid(token_client: TestClient) -> None:
    response = token_client.get(
        "/v1/auth/me",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_INVALID.value


@pytest.mark.parametrize(
    "role", [Role.VIEWER, Role.OPERATOR, Role.APPROVER]
)
def test_login_principal_roles_come_from_db(
    token_client: TestClient, auth_user_factory, role: Role
) -> None:
    user = auth_user_factory((role,))
    response = token_client.post(
        "/v1/auth/login",
        json={
            "username": user["user_id"],
            "password": user["password"],
            "tenant_id": user["tenant_id"],
        },
    )
    assert response.status_code == 200, response.text

    principal = response.json()["principal"]
    assert principal["user_id"] == user["user_id"]
    assert principal["tenant_id"] == user["tenant_id"]
    assert principal["roles"] == [role.value]
    assert "password" not in {key.lower() for key in principal}
    assert user["password"] not in response.text


def test_login_me_logout_flow(
    token_client: TestClient, auth_user: dict[str, str]
) -> None:
    login = token_client.post(
        "/v1/auth/login",
        json={
            "username": auth_user["user_id"],
            "password": auth_user["password"],
            "tenant_id": auth_user["tenant_id"],
        },
    )
    assert login.status_code == 200, login.text
    assert auth_user["password"] not in login.text

    body = login.json()
    assert body["token_type"] == "bearer"
    assert body["token"]
    assert body["expires_at"]
    principal = body["principal"]
    assert principal["user_id"] == auth_user["user_id"]
    assert principal["tenant_id"] == auth_user["tenant_id"]
    assert set(principal["roles"]) == {Role.OPERATOR.value, Role.VIEWER.value}
    assert "password" not in {key.lower() for key in principal}

    token = body["token"]
    me = token_client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["user_id"] == auth_user["user_id"]
    assert me.json()["tenant_id"] == auth_user["tenant_id"]

    logout = token_client.post(
        "/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logout.status_code == 200, logout.text
    assert logout.json() == {"logged_out": True}

    revoked = token_client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert revoked.status_code == 401, revoked.text
    assert revoked.json()["error"]["error_code"] == ErrorCode.AUTH_INVALID.value
    assert token not in revoked.text


def test_login_wrong_password_is_invalid(
    token_client: TestClient, auth_user: dict[str, str]
) -> None:
    response = token_client.post(
        "/v1/auth/login",
        json={
            "username": auth_user["user_id"],
            "password": "wrong-password",
            "tenant_id": auth_user["tenant_id"],
        },
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_INVALID.value
    assert "wrong-password" not in response.text
    assert auth_user["password"] not in response.text


def test_login_unknown_user_is_invalid(token_client: TestClient) -> None:
    response = token_client.post(
        "/v1/auth/login",
        json={"username": "user_does_not_exist", "password": "secret"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_INVALID.value


def test_login_rejects_client_supplied_role(token_client: TestClient) -> None:
    response = token_client.post(
        "/v1/auth/login",
        json={
            "username": "user_local",
            "password": "secret",
            "role": "Administrator",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.REQUEST_INVALID.value


def test_refresh_invalidates_old_token(
    token_client: TestClient, auth_user: dict[str, str]
) -> None:
    body = _login(token_client, auth_user)
    old_token = body["token"]

    refreshed = token_client.post(
        "/v1/auth/refresh", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert refreshed.status_code == 200, refreshed.text
    new_body = refreshed.json()
    assert new_body["token"] != old_token
    assert new_body["principal"]["user_id"] == auth_user["user_id"]
    assert auth_user["password"] not in refreshed.text

    rejected = token_client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"}
    )
    assert rejected.status_code == 401, rejected.text

    accepted = token_client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {new_body['token']}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["tenant_id"] == auth_user["tenant_id"]


def test_refresh_requires_bearer(token_client: TestClient) -> None:
    response = token_client.post("/v1/auth/refresh")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_REQUIRED.value


def test_logout_without_token_is_auth_required(token_client: TestClient) -> None:
    response = token_client.post("/v1/auth/logout")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_REQUIRED.value


def test_dev_local_me_returns_local_administrator(dev_local_client: TestClient) -> None:
    response = dev_local_client.get("/v1/auth/me")
    assert response.status_code == 200, response.text
    principal = response.json()
    assert principal["user_id"] == "user_local"
    assert principal["tenant_id"] == "tenant_local"
    assert Role.ADMINISTRATOR.value in principal["roles"]
    assert principal["auth_method"] == "dev_local"


def test_dev_local_logout_without_token_returns_false(dev_local_client: TestClient) -> None:
    response = dev_local_client.post("/v1/auth/logout")
    assert response.status_code == 200, response.text
    assert response.json() == {"logged_out": False}


def test_dev_local_refresh_is_auth_required(dev_local_client: TestClient) -> None:
    response = dev_local_client.post("/v1/auth/refresh")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.AUTH_REQUIRED.value
