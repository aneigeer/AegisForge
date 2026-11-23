"""本地账户认证 API（W05）：登录、登出、刷新与当前身份。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from apps.api.deps import get_auth_service, get_principal
from apps.api.security import extract_bearer_token
from core.auth.service import AuthService, LoginResult
from core.config import Settings, get_settings
from core.errors import AuthRequiredError
from core.security.models import Principal

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)


def _login_payload(result: LoginResult) -> dict[str, Any]:
    return {
        "token": result.token,
        "token_type": "bearer",
        "expires_at": result.expires_at,
        "principal": result.principal.to_public_dict(),
    }


@router.post("/login")
def login(
    body: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    # 密码只在进程内以明文传给 AuthService；不写日志、不回显。
    result = auth_service.login(
        body.username, body.password.get_secret_value(), body.tenant_id
    )
    return _login_payload(result)


@router.post("/logout")
def logout(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, bool]:
    header = request.headers.get("Authorization")
    # dev_local 没有会话可撤销；无 Bearer 时返回未登出，避免把本地主体误判为失败。
    if settings.auth_mode == "dev_local" and not (header and header.strip()):
        return {"logged_out": False}
    token = extract_bearer_token(request)
    return {"logged_out": auth_service.logout(token)}


@router.post("/refresh")
def refresh(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    if settings.auth_mode != "token":
        raise AuthRequiredError("dev_local 模式不支持刷新 Token")
    token = extract_bearer_token(request)
    result = auth_service.rotate(token)
    return _login_payload(result)


@router.get("/me")
def me(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    return principal.to_public_dict()


__all__ = ["router"]
