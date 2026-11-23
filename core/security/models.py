"""W05 共享安全模型：Principal、策略决策、租约与执行信封。

约束：
- Principal 只由服务端构造；API/Worker 不得用请求体中的 tenant/role 覆盖；
- PolicyDecision 的 effective_risk 由服务端风险解析器决定，模型提示只能升不能降；
- ExecutionEnvelope 的 signature 由 Gateway 用服务端密钥校验，Worker 本地缓存不可信。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.security.roles import Permission, Role, permissions_for

RiskLevel = Literal["R0", "R1", "R2", "R3", "R4"]

_RISK_ORDER: tuple[RiskLevel, ...] = ("R0", "R1", "R2", "R3", "R4")


def risk_rank(level: RiskLevel) -> int:
    return _RISK_ORDER.index(level)


def max_risk(*levels: RiskLevel | None) -> RiskLevel:
    present = [level for level in levels if level is not None]
    if not present:
        return "R0"
    return max(present, key=risk_rank)


def environment_tier(status: str | None) -> Literal["LAB", "PROD"]:
    """把控制库环境状态映射为策略层级；未知/缺失一律按 PROD fail closed。"""
    return "LAB" if str(status or "").strip().upper() == "LAB" else "PROD"


class AuthMethod(str, Enum):
    PASSWORD = "password"
    TOKEN = "token"
    SERVICE = "service"
    DEV_LOCAL = "dev_local"


class Principal(BaseModel):
    """服务端认证后的身份；不包含任何凭据/Token。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    roles: tuple[Role, ...]
    display_name: str | None = None
    session_id: str | None = None
    auth_method: AuthMethod = AuthMethod.TOKEN
    permissions: frozenset[Permission] = Field(default_factory=frozenset)

    @field_validator("roles")
    @classmethod
    def _reject_empty_roles(cls, value: tuple[Role, ...]) -> tuple[Role, ...]:
        if not value:
            raise ValueError("Principal 至少需要一个角色")
        return value

    def model_post_init(self, __context: Any) -> None:
        if not self.permissions:
            object.__setattr__(self, "permissions", permissions_for(self.roles))

    def has_role(self, role: Role) -> bool:
        return role in self.roles

    def has_permission(self, permission: Permission | str) -> bool:
        wanted = Permission(permission) if isinstance(permission, str) else permission
        return wanted in self.permissions

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": [role.value for role in self.roles],
            "display_name": self.display_name,
            "session_id": self.session_id,
            "auth_method": self.auth_method.value,
            "permissions": sorted(item.value for item in self.permissions),
        }


class EnvironmentContext(BaseModel):
    """策略计算所需的环境事实；由服务端从控制库读取。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_id: str
    tenant_id: str
    db_engine: Literal["mysql", "postgresql"]
    tier: Literal["LAB", "PROD"] = "LAB"
    db_version: str | None = None
    # W06：进入 `environment_fingerprint` 的三个事实（来源仍是 environments 行）。
    config_version: str | None = None
    image_digest: str | None = None
    network_id: str | None = None


class RiskEvaluation(BaseModel):
    """风险解析的可解释结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_risk: RiskLevel
    factors: tuple[str, ...] = ()
    model_hint: RiskLevel | None = None
    model_hint_accepted: bool = False
    notes: tuple[str, ...] = ()


class PolicyDecision(BaseModel):
    """Policy Engine 的唯一输出；缺策略默认拒绝。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    effective_risk: RiskLevel
    reason: str
    policy_version: str
    required_approval: bool = False
    deny_code: str | None = None
    risk: RiskEvaluation | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class LeaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"
    REVOKED = "REVOKED"


class WorkerLease(BaseModel):
    """任务级短租约；fencing_token 每次重新领取必须递增。"""

    model_config = ConfigDict(extra="forbid")

    lease_id: str
    worker_id: str
    task_id: str
    tenant_id: str
    environment_id: str
    fencing_token: int = Field(ge=1)
    status: LeaseStatus = LeaseStatus.ACTIVE
    issued_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None


class ExecutionEnvelope(BaseModel):
    """Worker 提交给 Gateway 的执行信封；签名与 nonce 由 Gateway 校验。"""

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    task_id: str
    step_id: str | None = None
    worker_id: str
    lease_id: str
    fencing_token: int = Field(ge=1)
    state_version: int = Field(ge=1)
    correlation_id: str
    actor_user_id: str
    tool_name: str
    tool_version: str
    environment_id: str
    args_digest: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    model_risk_hint: RiskLevel | None = None
    # W06：R2 审批单 id；进入签名载荷，篡改 approval_id 会破坏签名。
    approval_id: str | None = None
    signature: str = ""

    def unsigned_payload(self) -> dict[str, Any]:
        """签名覆盖除 signature 外的全部字段；保持稳定字段顺序由签名实现负责。"""
        data = self.model_dump(mode="json")
        data.pop("signature", None)
        return data


__all__ = [
    "AuthMethod",
    "EnvironmentContext",
    "ExecutionEnvelope",
    "LeaseStatus",
    "PolicyDecision",
    "Principal",
    "RiskEvaluation",
    "RiskLevel",
    "WorkerLease",
    "environment_tier",
    "max_risk",
    "risk_rank",
]
