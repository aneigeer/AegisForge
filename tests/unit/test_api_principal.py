"""W05 API Principal 依赖单元测试（不访问数据库）。"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from apps.api.security import extract_bearer_token, get_principal, require_permission
from core.auth.service import AuthService
from core.config import Settings
from core.errors import AuthInvalidError, AuthRequiredError, ErrorCode, ForbiddenError
from core.security.models import AuthMethod, Principal
from core.security.roles import Permission, Role


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ro_dsn": "mysql://dg_ro:secret_ro@127.0.0.1:3306/dg_lab",
        "rw_dsn": "mysql://dg_rw:secret_rw@127.0.0.1:3306/dg_lab",
        "auth_mode": "token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw_headers,
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
        }
    )


def _principal(user_id: str, tenant_id: str, role: Role) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(role,))


def test_dev_local_returns_server_side_principal(monkeypatch) -> None:
    expected = Principal(
        user_id="user_local",
        tenant_id="tenant_local",
        roles=(Role.ADMINISTRATOR,),
        auth_method=AuthMethod.DEV_LOCAL,
    )
    authenticate_calls: list[str] = []

    monkeypatch.setattr(AuthService, "dev_principal", lambda self: expected)
    monkeypatch.setattr(
        AuthService, "authenticate", lambda self, token: authenticate_calls.append(token)
    )

    result = get_principal(
        _request({"Authorization": "Bearer ignore-me"}),
        object(),  # type: ignore[arg-type]  # dev_local 不访问仓储
        _settings(auth_mode="dev_local"),
    )

    assert result is expected
    assert result.user_id == "user_local"
    assert result.tenant_id == "tenant_local"
    assert result.has_role(Role.ADMINISTRATOR)
    # dev_local 必须忽略客户端 Authorization 头。
    assert authenticate_calls == []


def test_token_mode_missing_authorization_is_auth_required() -> None:
    with pytest.raises(AuthRequiredError) as excinfo:
        get_principal(_request(), object(), _settings())  # type: ignore[arg-type]
    assert excinfo.value.code is ErrorCode.AUTH_REQUIRED


def test_token_mode_non_bearer_is_auth_invalid() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        get_principal(
            _request({"Authorization": "Basic dXNlcjpwYXNz"}),
            object(),  # type: ignore[arg-type]
            _settings(),
        )
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    assert "dXNlcjpwYXNz" not in str(excinfo.value.details)


def test_token_mode_invalid_token_is_auth_invalid(monkeypatch) -> None:
    def _raise(self, token: str) -> Principal:
        raise AuthInvalidError("Token 无效")

    monkeypatch.setattr(AuthService, "authenticate", _raise)

    with pytest.raises(AuthInvalidError) as excinfo:
        get_principal(
            _request({"Authorization": "Bearer dg_invalid_token_for_unit"}),
            object(),  # type: ignore[arg-type]
            _settings(),
        )
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    assert "dg_invalid_token_for_unit" not in repr(excinfo.value.details)


def test_token_mode_valid_token_returns_principal(monkeypatch) -> None:
    expected = _principal("user_token", "tenant_token", Role.OPERATOR)
    seen: list[str] = []

    def _authenticate(self, token: str) -> Principal:
        seen.append(token)
        return expected

    monkeypatch.setattr(AuthService, "authenticate", _authenticate)

    result = get_principal(
        _request({"Authorization": "Bearer dg_valid_token_for_unit"}),
        object(),  # type: ignore[arg-type]
        _settings(),
    )

    assert result is expected
    assert seen == ["dg_valid_token_for_unit"]


def test_require_permission_checks_rbac() -> None:
    checker = require_permission(Permission.TASK_CREATE)

    viewer = _principal("user_viewer", "tenant_local", Role.VIEWER)
    with pytest.raises(ForbiddenError) as excinfo:
        checker(viewer)
    assert excinfo.value.code is ErrorCode.FORBIDDEN

    administrator = _principal("user_admin", "tenant_local", Role.ADMINISTRATOR)
    assert checker(administrator) is administrator


def test_extract_bearer_token_trims_value() -> None:
    token = extract_bearer_token(_request({"Authorization": "Bearer  dg-token  "}))
    assert token == "dg-token"
