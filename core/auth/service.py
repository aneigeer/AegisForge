"""本地身份认证服务（W05）。

安全原则：
- 角色只来自控制库 memberships，绝不读取请求中的 role/tenant 权限声明；
- 登录成功后只返回一次 raw token，数据库仅保存 SHA-256 摘要；
- 所有异常 details 只包含 user_id/session_id 等安全字段，不记录 token/password；
- dev_local 主体只允许非生产环境，且必须由控制库 memberships 支撑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.auth.repository import AuthRepository, new_session_id
from core.config import Settings
from core.errors import AuthExpiredError, AuthInvalidError, ConfigError
from core.security.models import AuthMethod, Principal
from core.security.passwords import verify_password
from core.security.roles import Role
from core.security.tokens import hash_token, new_session_token
from database.mysql.connection import MysqlClient

_ACTIVE = "ACTIVE"
_DEV_USER_ID = "user_local"


@dataclass(frozen=True)
class LoginResult:
    """登录/轮换结果；token 不进 repr，避免被日志意外记录。"""

    principal: Principal
    token: str = field(repr=False)
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status(value: object) -> str:
    return str(value).upper()


class AuthService:
    def __init__(self, control_client: MysqlClient, settings: Settings) -> None:
        self._repository = AuthRepository(control_client)
        self._settings = settings

    @property
    def repository(self) -> AuthRepository:
        return self._repository

    # ---- login ---------------------------------------------------------
    def login(
        self, username: str, password: str, tenant_id: str | None = None
    ) -> LoginResult:
        user = self._repository.get_user(username) if isinstance(username, str) else None
        if user is None:
            # 用户不存在也执行一次 dummy scrypt，减小时序差异。
            verify_password(password if isinstance(password, str) else "", None)
            raise AuthInvalidError("用户名或密码无效")

        if _status(user["status"]) != _ACTIVE or not user.get("password_hash"):
            verify_password(password if isinstance(password, str) else "", None)
            raise AuthInvalidError(
                "用户名或密码无效", details={"user_id": user["user_id"]}
            )

        if not verify_password(
            password if isinstance(password, str) else "", str(user["password_hash"])
        ):
            raise AuthInvalidError(
                "用户名或密码无效", details={"user_id": user["user_id"]}
            )

        memberships = self._memberships_by_tenant(user["user_id"])
        tenant = self._select_tenant(memberships, tenant_id, user["user_id"])
        roles = memberships[tenant]

        now = _now()
        expires_at = now + timedelta(seconds=self._settings.auth_token_ttl_s)
        raw_token, token_hash = new_session_token()
        session_id = self._new_session_id()
        self._repository.insert_session(
            session_id,
            user["user_id"],
            tenant,
            token_hash,
            AuthMethod.PASSWORD,
            now,
            expires_at,
        )
        principal = Principal(
            user_id=user["user_id"],
            tenant_id=tenant,
            roles=roles,
            display_name=user["display_name"],
            session_id=session_id,
            auth_method=AuthMethod.PASSWORD,
        )
        return LoginResult(principal=principal, token=raw_token, expires_at=expires_at)

    # ---- authenticate --------------------------------------------------
    def authenticate(self, token: str) -> Principal:
        token_hash = self._hash_or_invalid(token)
        session = self._repository.get_session_by_token_hash(token_hash)
        if session is None:
            raise AuthInvalidError("Token 无效")
        session_id = session["session_id"]
        if _status(session["status"]) != _ACTIVE:
            raise AuthInvalidError("Token 已失效", details={"session_id": session_id})

        expires_at = session["expires_at"]
        if expires_at is not None and _now() >= expires_at:
            raise AuthExpiredError(
                "Token 已过期", details={"session_id": session_id}
            )

        user = self._repository.get_user(session["user_id"])
        if user is None or _status(user["status"]) != _ACTIVE:
            raise AuthInvalidError(
                "用户不存在或已停用", details={"user_id": session["user_id"]}
            )

        memberships = self._memberships_by_tenant(user["user_id"])
        roles = memberships.get(session["tenant_id"])
        if not roles:
            raise AuthInvalidError(
                "用户已不属于该租户",
                details={"user_id": user["user_id"], "tenant_id": session["tenant_id"]},
            )

        self._repository.touch_last_used(session_id)
        return Principal(
            user_id=user["user_id"],
            tenant_id=session["tenant_id"],
            roles=roles,
            display_name=user["display_name"],
            session_id=session_id,
            auth_method=AuthMethod.TOKEN,
        )

    # ---- logout --------------------------------------------------------
    def logout(self, token: str) -> bool:
        try:
            token_hash = hash_token(token)
        except ValueError:
            return False
        session = self._repository.get_session_by_token_hash(token_hash)
        if session is None:
            return False
        return self._repository.revoke_session(session["session_id"]) > 0

    # ---- rotate --------------------------------------------------------
    def rotate(self, token: str) -> LoginResult:
        principal = self.authenticate(token)
        if principal.session_id is None:
            raise AuthInvalidError("Token 无效")
        old_session_id = principal.session_id

        now = _now()
        expires_at = now + timedelta(seconds=self._settings.auth_token_ttl_s)
        raw_token, token_hash = new_session_token()
        new_session_id = self._new_session_id()
        if not self._repository.rotate_session(
            old_session_id, new_session_id, token_hash, now, expires_at
        ):
            raise AuthInvalidError(
                "Token 已失效", details={"session_id": old_session_id}
            )
        rotated = principal.model_copy(update={"session_id": new_session_id})
        return LoginResult(principal=rotated, token=raw_token, expires_at=expires_at)

    # ---- dev_local -----------------------------------------------------
    def dev_principal(self) -> Principal:
        if self._settings.auth_mode != "dev_local" or self._settings.env == "prod":
            raise ConfigError(
                "dev_local 认证未启用",
                details={"auth_mode": self._settings.auth_mode, "env": self._settings.env},
            )

        user = self._repository.get_user(_DEV_USER_ID)
        if user is None or _status(user["status"]) != _ACTIVE:
            raise ConfigError(
                "本地开发主体不存在或已停用", details={"user_id": _DEV_USER_ID}
            )

        memberships = self._memberships_by_tenant(_DEV_USER_ID)
        if not memberships:
            raise ConfigError(
                "本地开发主体缺少租户成员关系", details={"user_id": _DEV_USER_ID}
            )

        if self._settings.default_tenant_id in memberships:
            tenant = self._settings.default_tenant_id
        elif len(memberships) == 1:
            tenant = next(iter(memberships))
        else:
            raise ConfigError(
                "本地开发主体存在多个租户，需显式配置 default_tenant_id",
                details={"user_id": _DEV_USER_ID},
            )

        return Principal(
            user_id=user["user_id"],
            tenant_id=tenant,
            roles=memberships[tenant],
            display_name=user["display_name"],
            session_id=None,
            auth_method=AuthMethod.DEV_LOCAL,
        )

    # ---- helpers -------------------------------------------------------
    def _memberships_by_tenant(self, user_id: str) -> dict[str, tuple[Role, ...]]:
        grouped: dict[str, list[Role]] = {}
        for membership in self._repository.get_memberships(user_id):
            try:
                role = Role(str(membership["role"]))
            except ValueError:
                # 服务端脏数据不应被静默提升；跳过未知角色。
                continue
            grouped.setdefault(str(membership["tenant_id"]), []).append(role)
        return {
            tenant: tuple(sorted(roles, key=lambda item: item.value))
            for tenant, roles in grouped.items()
            if roles
        }

    @staticmethod
    def _select_tenant(
        memberships: dict[str, tuple[Role, ...]],
        tenant_id: str | None,
        user_id: str,
    ) -> str:
        if tenant_id is not None:
            if tenant_id not in memberships:
                raise AuthInvalidError(
                    "用户不属于该租户", details={"user_id": user_id}
                )
            return tenant_id
        if not memberships:
            raise AuthInvalidError(
                "用户未加入任何租户", details={"user_id": user_id}
            )
        if len(memberships) > 1:
            raise AuthInvalidError(
                "用户属于多个租户，必须显式指定租户",
                details={"user_id": user_id, "tenant_count": len(memberships)},
            )
        return next(iter(memberships))

    @staticmethod
    def _hash_or_invalid(token: str) -> str:
        try:
            return hash_token(token)
        except ValueError:
            raise AuthInvalidError("Token 无效") from None

    @staticmethod
    def _new_session_id() -> str:
        return new_session_id()


__all__ = ["AuthService", "LoginResult"]
