"""API 认证依赖：只从服务端配置与凭据构造 Principal。

约束：
- dev_local 模式固定使用控制库中的本地主体，绝不读取 X-Role/X-Tenant 等请求头；
- token 模式只接受 ``Authorization: Bearer <token>``；请求体/查询参数不参与身份构造；
- 日志只记录 user_id/session_id，绝不记录完整 token 或密码。
"""

from __future__ import annotations

from fastapi import Depends, Request

from core.auth.service import AuthService
from core.config import Settings, get_settings
from core.errors import AuthInvalidError, AuthRequiredError
from core.logging import get_logger
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission
from database.mysql.connection import MysqlClient

logger = get_logger(__name__)


def _control_client() -> MysqlClient:
    """按需解析控制库连接，避免与 deps 模块形成导入环。"""
    from apps.api.deps import get_control_client

    return get_control_client()


def extract_bearer_token(http_request: Request) -> str:
    """解析 Authorization 头。

    缺失认证信息返回 AUTH_REQUIRED；头存在但不是合法 Bearer 返回 AUTH_INVALID。
    错误 details 不含 Authorization 原文或 token 片段。
    """
    header = http_request.headers.get("Authorization")
    if not header or not header.strip():
        raise AuthRequiredError("缺少认证信息")
    scheme, _, token = header.partition(" ")
    if scheme.strip().lower() != "bearer" or not token.strip():
        raise AuthInvalidError(
            "Authorization 头格式无效",
            details={"expected": "Bearer <token>"},
        )
    return token.strip()


def get_principal(
    http_request: Request,
    control_client: MysqlClient = Depends(_control_client),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """构造当前请求的服务端 Principal。"""
    service = AuthService(control_client, settings)
    if settings.auth_mode == "dev_local":
        # dev_local 由服务端固定主体决定，完全忽略客户端 Authorization 头。
        principal = service.dev_principal()
        logger.debug(
            "dev_local 主体已加载",
            extra={"user_id": principal.user_id, "tenant_id": principal.tenant_id},
        )
        return principal

    token = extract_bearer_token(http_request)
    if settings.auth_mode == "oidc":
        # P4-T1：OIDC 分支——令牌只用于"证明身份"，角色仍从 DB 读。
        from core.auth.oidc import JwksCache, OIDCConfigError, OIDCService

        if not (settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_uri):
            raise OIDCConfigError(
                "OIDC 配置缺失",
                details={"fields": ["oidc_issuer", "oidc_audience", "oidc_jwks_uri"]},
            )
        import httpx

        def _fetch_jwks(uri: str) -> dict:
            response = httpx.get(uri, timeout=5.0)
            response.raise_for_status()
            return response.json()

        oidc = OIDCService(
            repository=service.repository,
            jwks=JwksCache(fetcher=_fetch_jwks, ttl_s=settings.oidc_jwks_ttl_s),
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_uri=settings.oidc_jwks_uri,
            leeway_s=settings.oidc_leeway_s,
        )
        return oidc.authenticate(token)
    principal = service.authenticate(token)
    logger.debug(
        "Token 认证通过",
        extra={"user_id": principal.user_id, "session_id": principal.session_id},
    )
    return principal


def require_permission(permission: Permission):
    """返回校验指定权限的 FastAPI 依赖；通过后返回 Principal。"""

    def _require(
        principal: Principal = Depends(get_principal),
    ) -> Principal:
        ensure_permission(principal, permission)
        return principal

    return _require


__all__ = [
    "extract_bearer_token",
    "get_principal",
    "require_permission",
]
