"""认证数据仓储（MySQL 8，dg_system 库）。

时间约定与 ``TaskRepository`` 一致：写库前转为无时区 UTC，读库后补 ``timezone.utc``。
Token 只以 SHA-256 摘要（64 字符）落库，仓储层不接触明文。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from core.security.models import AuthMethod
from core.security.roles import Role
from database.mysql.connection import MysqlClient

USERS = "dg_system.users"
MEMBERSHIPS = "dg_system.memberships"
SESSIONS = "dg_system.auth_sessions"

_ACTIVE = "ACTIVE"
_REVOKED = "REVOKED"


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc)


def _role_value(role: Role | str) -> str:
    return role.value if isinstance(role, Role) else str(role)


def _auth_method_value(method: AuthMethod | str) -> str:
    return method.value if isinstance(method, AuthMethod) else str(method)


def new_session_id() -> str:
    """会话 ID 与 token 均不可预测；长度满足 VARCHAR(64)。"""
    return f"sess_{secrets.token_hex(16)}"


class AuthRepository:
    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    # ---- users ---------------------------------------------------------
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {USERS} WHERE user_id = %s LIMIT 1", (user_id,)
        )
        return self._to_user(rows[0]) if rows else None

    def get_user_by_oidc(self, issuer: str, subject: str) -> dict[str, Any] | None:
        """按 (issuer, subject) 查本地用户；未映射返回 None（调用方 fail closed）。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {USERS} WHERE oidc_issuer = %s AND oidc_subject = %s LIMIT 1",
            (issuer, subject),
        )
        return self._to_user(rows[0]) if rows else None

    def memberships_by_tenant(self, user_id: str) -> dict[str, tuple[Any, ...]]:
        """租户 → 角色元组；未知角色跳过（脏数据不提升权限）。"""
        from core.security.roles import Role

        grouped: dict[str, list[Role]] = {}
        for membership in self.get_memberships(user_id):
            try:
                role = Role(str(membership["role"]))
            except ValueError:
                continue
            grouped.setdefault(str(membership["tenant_id"]), []).append(role)
        return {
            tenant: tuple(sorted(roles, key=lambda item: item.value))
            for tenant, roles in grouped.items()
            if roles
        }

    def create_or_update_user(
        self,
        user_id: str,
        display_name: str,
        password_hash: str | None,
        status: str = _ACTIVE,
    ) -> dict[str, Any]:
        """创建或更新用户；提供密码哈希时同步更新时间戳并递增 auth_version。"""
        now = _to_db(datetime.now(timezone.utc))
        if password_hash is None:
            self._client.execute(
                f"INSERT INTO {USERS} (user_id, display_name, status, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), "
                "status = VALUES(status), updated_at = VALUES(updated_at)",
                (user_id, display_name, status, now, now),
            )
        else:
            self._client.execute(
                f"INSERT INTO {USERS} (user_id, display_name, status, created_at, "
                "password_hash, password_updated_at, auth_version, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,1,%s) "
                "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), "
                "status = VALUES(status), password_hash = VALUES(password_hash), "
                "password_updated_at = VALUES(password_updated_at), "
                "auth_version = auth_version + 1, updated_at = VALUES(updated_at)",
                (user_id, display_name, status, now, password_hash, now, now),
            )
        user = self.get_user(user_id)
        assert user is not None
        return user

    # ---- memberships ---------------------------------------------------
    def get_memberships(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._client.fetch_all(
            f"SELECT * FROM {MEMBERSHIPS} WHERE user_id = %s "
            "ORDER BY tenant_id ASC, role ASC",
            (user_id,),
        )
        return [self._to_membership(row) for row in rows]

    def upsert_membership(self, tenant_id: str, user_id: str, role: Role | str) -> None:
        self._client.execute(
            f"INSERT INTO {MEMBERSHIPS} (tenant_id, user_id, role, created_at) "
            "VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE role = VALUES(role)",
            (tenant_id, user_id, _role_value(role), _to_db(datetime.now(timezone.utc))),
        )

    # ---- sessions ------------------------------------------------------
    def insert_session(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        token_hash: str,
        auth_method: AuthMethod | str,
        created_at: datetime,
        expires_at: datetime,
        rotated_from: str | None = None,
    ) -> None:
        self._client.execute(
            f"INSERT INTO {SESSIONS} (session_id, user_id, tenant_id, token_hash, "
            "auth_method, status, created_at, expires_at, rotated_from) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                session_id,
                user_id,
                tenant_id,
                token_hash,
                _auth_method_value(auth_method),
                _ACTIVE,
                _to_db(created_at),
                _to_db(expires_at),
                rotated_from,
            ),
        )

    def get_session_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {SESSIONS} WHERE token_hash = %s LIMIT 1", (token_hash,)
        )
        return self._to_session(rows[0]) if rows else None

    def revoke_session(self, session_id: str) -> int:
        """仅允许从 ACTIVE 撤销；返回受影响行数。"""
        return self._client.execute(
            f"UPDATE {SESSIONS} SET status = %s, revoked_at = %s "
            "WHERE session_id = %s AND status = %s",
            (
                _REVOKED,
                _to_db(datetime.now(timezone.utc)),
                session_id,
                _ACTIVE,
            ),
        )

    def rotate_session(
        self,
        old_session_id: str,
        new_session_id: str,
        new_token_hash: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> bool:
        """条件撤销旧会话后插入新会话；旧会话已非 ACTIVE 时返回 False。"""
        if self.revoke_session(old_session_id) == 0:
            return False
        rows = self._client.fetch_all(
            f"SELECT user_id, tenant_id, auth_method FROM {SESSIONS} "
            "WHERE session_id = %s LIMIT 1",
            (old_session_id,),
        )
        if not rows:
            return False
        row = rows[0]
        self.insert_session(
            new_session_id,
            str(row["user_id"]),
            str(row["tenant_id"]),
            new_token_hash,
            str(row["auth_method"]),
            created_at,
            expires_at,
            rotated_from=old_session_id,
        )
        return True

    def touch_last_used(self, session_id: str) -> None:
        self._client.execute(
            f"UPDATE {SESSIONS} SET last_used_at = %s WHERE session_id = %s",
            (_to_db(datetime.now(timezone.utc)), session_id),
        )

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._client.fetch_all(
            f"SELECT * FROM {SESSIONS} WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        return [self._to_session(row) for row in rows]

    # ---- row mapping ---------------------------------------------------
    @staticmethod
    def _to_user(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": str(row["user_id"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "created_at": _from_db(row["created_at"]),
            "password_hash": row.get("password_hash"),
            "password_updated_at": _from_db(row.get("password_updated_at")),
            "auth_version": int(row.get("auth_version") or 1),
            "updated_at": _from_db(row.get("updated_at")),
        }

    @staticmethod
    def _to_membership(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": str(row["tenant_id"]),
            "user_id": str(row["user_id"]),
            "role": str(row["role"]),
            "created_at": _from_db(row["created_at"]),
        }

    @staticmethod
    def _to_session(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": str(row["session_id"]),
            "user_id": str(row["user_id"]),
            "tenant_id": str(row["tenant_id"]),
            "token_hash": str(row["token_hash"]),
            "auth_method": str(row["auth_method"]),
            "status": str(row["status"]),
            "created_at": _from_db(row["created_at"]),
            "expires_at": _from_db(row["expires_at"]),
            "last_used_at": _from_db(row.get("last_used_at")),
            "revoked_at": _from_db(row.get("revoked_at")),
            "rotated_from": row.get("rotated_from"),
        }


__all__ = ["AuthRepository", "new_session_id"]
