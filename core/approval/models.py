"""审批领域模型（W06 D2）：状态白名单、一次性授权凭证与披露字段。

关键约束：
- `ApprovalGrant` 是**唯一**的"人工同意"载体，只能由 `ApprovalService.consume()`
  通过模块私有 seal 构造；外部直接 `ApprovalGrant(...)` 必抛 `ValueError`。
  "传 `approved=True`"的旁路在类型层与运行时都不存在。
- 状态迁移只允许 P0-05 白名单，非法迁移 `STATE_CONFLICT`；
- 职责分离：`approver_id` 不得等于 `requested_by`（含同一用户不同会话）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalExpiredError,
    ApprovalNotApprovedError,
    ApprovalRejectedError,
    ApprovalRevokedError,
    DBGuardianError,
    ForbiddenError,
    StateConflictError,
)

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


# P0-05：仅允许 PENDING→(APPROVED|REJECTED|EXPIRED|REVOKED)、
# APPROVED→(CONSUMED|EXPIRED|REVOKED)；其余终态不可迁出。
ALLOWED_APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    ),
    ApprovalStatus.APPROVED: frozenset(
        {
            ApprovalStatus.CONSUMED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    ),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.REVOKED: frozenset(),
    ApprovalStatus.CONSUMED: frozenset(),
}

# 可以"由审批人裁决"的状态（只有 PENDING）。
DECIDABLE_STATUSES = frozenset({ApprovalStatus.PENDING})
# 可以被撤销的状态（CONSUMED 不可撤销：凭证已被使用）。
REVOCABLE_STATUSES = frozenset({ApprovalStatus.PENDING, ApprovalStatus.APPROVED})


def can_transition(current: ApprovalStatus, target: ApprovalStatus) -> bool:
    return target in ALLOWED_APPROVAL_TRANSITIONS.get(current, frozenset())


def ensure_approval_transition(current: ApprovalStatus, target: ApprovalStatus) -> None:
    """非法迁移抛 `STATE_CONFLICT`（含终态迁出与重复裁决）。"""
    if current == target:
        raise StateConflictError(
            "审批状态未发生变化，禁止重复操作",
            details={"from": current.value, "to": target.value},
        )
    if not can_transition(current, target):
        raise StateConflictError(
            "非法审批状态迁移",
            details={"from": current.value, "to": target.value},
        )


_STATUS_ERRORS: dict[ApprovalStatus, type[DBGuardianError]] = {
    ApprovalStatus.PENDING: ApprovalNotApprovedError,
    ApprovalStatus.REJECTED: ApprovalRejectedError,
    ApprovalStatus.EXPIRED: ApprovalExpiredError,
    ApprovalStatus.REVOKED: ApprovalRevokedError,
    ApprovalStatus.CONSUMED: ApprovalAlreadyConsumedError,
}


def error_for_status(status: ApprovalStatus, *, approval_id: str) -> DBGuardianError:
    """把"当前状态不允许消费/裁决"映射为稳定错误码（设计 §5 第 2 项）。"""
    error_type = _STATUS_ERRORS.get(status)
    if error_type is None:  # APPROVED 时无错误；兜底按状态冲突处理。
        return StateConflictError(
            "审批当前状态不允许该操作",
            details={"approval_id": approval_id, "status": status.value},
        )
    return error_type(
        "审批当前状态不允许该操作",
        details={"approval_id": approval_id, "status": status.value},
    )


def ensure_segregation_of_duty(*, requested_by: str, approver_id: str) -> None:
    """职责分离：申请人不得自批（含同一用户不同会话）。"""
    if requested_by.strip() == approver_id.strip():
        raise ForbiddenError(
            "职责分离：审批人不得与申请人为同一用户",
            details={"reason": "SEGREGATION_OF_DUTY"},
        )


# ---------------------------------------------------------------------------
# 一次性授权凭证
# ---------------------------------------------------------------------------

_GRANT_SEAL = object()


@dataclass(frozen=True)
class ApprovalGrant:
    """一次性审批凭证：只能由 `ApprovalService.consume()` 用模块私有 seal 构造。

    执行侧（`ToolExecutor._precheck`）只接受本类型；没有"布尔旁路"。
    """

    approval_id: str
    tenant_id: str
    task_id: str
    tool_name: str
    tool_version: str
    environment_id: str
    action_digest: str
    prerequisites_digest: str
    policy_version: str
    consumed_by: str
    consumed_at: datetime
    step_id: str | None = None
    grant_seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.grant_seal is not _GRANT_SEAL:
            raise ValueError(
                "ApprovalGrant 只能由 ApprovalService.consume() 构造，禁止直接实例化"
            )
        for name in (
            "approval_id",
            "tenant_id",
            "task_id",
            "tool_name",
            "tool_version",
            "environment_id",
            "action_digest",
            "prerequisites_digest",
            "policy_version",
            "consumed_by",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"ApprovalGrant 缺少必填字段：{name}")

    def to_public_dict(self) -> dict[str, Any]:
        """审计用扁平字典；不含任何凭据。"""
        return {
            "approval_id": self.approval_id,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "environment_id": self.environment_id,
            "action_digest": self.action_digest,
            "prerequisites_digest": self.prerequisites_digest,
            "policy_version": self.policy_version,
            "consumed_by": self.consumed_by,
            "consumed_at": self.consumed_at.astimezone(timezone.utc).isoformat(),
        }


def issue_grant(**kwargs: Any) -> ApprovalGrant:
    """模块私有构造入口：仅供 `ApprovalService.consume()` 调用（勿在别处使用）。"""
    return ApprovalGrant(grant_seal=_GRANT_SEAL, **kwargs)


# ---------------------------------------------------------------------------
# 前置条件探测结果
# ---------------------------------------------------------------------------

CheckStatus = Literal["PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
# W07：`target_probe` 是"连目标库执行固定语句"的来源（W06 只有控制库/服务端探测）。
CheckSource = Literal["server_probe", "control_db", "target_probe"]

# 披露三节（PRD §4 硬规则①）：键名稳定，被测试与前端引用。
DISCLOSURE_SECTION_KEYS: tuple[str, ...] = ("risk", "compensation", "verification_plan")

NON_BLOCKING_CHECK_STATUSES: frozenset[str] = frozenset({"PASS", "NOT_APPLICABLE"})


class PrerequisiteCheck(BaseModel):
    """一条前置条件事实；只有 PASS/NOT_APPLICABLE 不阻断审批创建。"""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=64)
    status: CheckStatus
    observed: dict[str, Any] = Field(default_factory=dict)
    source: CheckSource = "server_probe"

    def blocks(self) -> bool:
        return self.status not in NON_BLOCKING_CHECK_STATUSES

    def to_digest_dict(self) -> dict[str, Any]:
        """参与 `prerequisites_digest` 的规范形（字段集封闭）。"""
        return {
            "check_id": self.check_id,
            "status": self.status,
            "observed": self.observed,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# 审批实体与查询视图
# ---------------------------------------------------------------------------


class Approval(BaseModel):
    """审批状态表的完整行映射；历史由 `task_events` 承担，不在此重复。"""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    tenant_id: str
    task_id: str
    step_id: str | None = None
    environment_id: str
    db_engine: Literal["mysql", "postgresql"]
    environment_fingerprint: dict[str, Any]
    environment_fingerprint_digest: str
    action_digest: str
    parameters_digest: str
    target_objects_digest: str
    prerequisites_digest: str
    prerequisites: list[PrerequisiteCheck] = Field(default_factory=list)
    action_descriptor: dict[str, Any] = Field(default_factory=dict)
    policy_version: str
    tool_name: str
    tool_version: str
    requested_by: str
    requested_session_id: str | None = None
    approver_id: str | None = None
    approver_session_id: str | None = None
    authn_context: dict[str, Any] = Field(default_factory=dict)
    nonce_hash: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_reason: str | None = None
    operator_note: str | None = None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    consumed_by: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    # W07：披露三节的创建时刻快照（与 action_digest 解耦，历史行允许为空）。
    disclosure: dict[str, Any] = Field(default_factory=dict)
    disclosure_digest: str | None = None
    disclosure_version: str | None = None
    disclosed_at: datetime | None = None

    @property
    def normalized_operation(self) -> dict[str, Any]:
        value = self.action_descriptor.get("normalized_operation")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def target_objects(self) -> list[dict[str, Any]]:
        value = self.action_descriptor.get("target_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def to_detail(self, *, now: datetime | None = None) -> "ApprovalDetail":
        return ApprovalDetail(
            approval_id=self.approval_id,
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            step_id=self.step_id,
            environment_id=self.environment_id,
            db_engine=self.db_engine,
            environment_fingerprint=self.environment_fingerprint,
            environment_fingerprint_digest=self.environment_fingerprint_digest,
            action_digest=self.action_digest,
            parameters_digest=self.parameters_digest,
            target_objects_digest=self.target_objects_digest,
            prerequisites_digest=self.prerequisites_digest,
            prerequisites=self.prerequisites,
            action_descriptor=self.action_descriptor,
            policy_version=self.policy_version,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            requested_by=self.requested_by,
            approver_id=self.approver_id,
            authn_context=self.authn_context,
            nonce_hash=self.nonce_hash,
            status=self.status,
            decision_reason=self.decision_reason,
            operator_note=self.operator_note,
            created_at=self.created_at,
            expires_at=self.expires_at,
            decided_at=self.decided_at,
            consumed_at=self.consumed_at,
            consumed_by=self.consumed_by,
            revoked_at=self.revoked_at,
            revoked_by=self.revoked_by,
            disclosure=self.disclosure,
            disclosure_digest=self.disclosure_digest,
            disclosure_version=self.disclosure_version,
            disclosed_at=self.disclosed_at,
            remaining_seconds=_remaining_seconds(self.expires_at, now),
        )


class ApprovalDetail(BaseModel):
    """审批查询/披露视图：与 `action_digest` 同源（`action_descriptor` 即摘要输入）。"""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    tenant_id: str
    task_id: str
    step_id: str | None = None
    environment_id: str
    db_engine: Literal["mysql", "postgresql"]
    environment_fingerprint: dict[str, Any]
    environment_fingerprint_digest: str
    action_digest: str
    parameters_digest: str
    target_objects_digest: str
    prerequisites_digest: str
    prerequisites: list[PrerequisiteCheck] = Field(default_factory=list)
    action_descriptor: dict[str, Any] = Field(default_factory=dict)
    policy_version: str
    tool_name: str
    tool_version: str
    requested_by: str
    approver_id: str | None = None
    authn_context: dict[str, Any] = Field(default_factory=dict)
    nonce_hash: str
    status: ApprovalStatus
    decision_reason: str | None = None
    operator_note: str | None = None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    consumed_by: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    # W07：披露三节快照与其完整性摘要（历史行为空）。
    disclosure: dict[str, Any] = Field(default_factory=dict)
    disclosure_digest: str | None = None
    disclosure_version: str | None = None
    disclosed_at: datetime | None = None
    remaining_seconds: float = 0.0

    @property
    def normalized_operation(self) -> dict[str, Any]:
        value = self.action_descriptor.get("normalized_operation")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def target_objects(self) -> list[dict[str, Any]]:
        value = self.action_descriptor.get("target_objects")
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def missing_required_fields(self) -> list[str]:
        """批准前的字段完整性校验（PRD §4 硬规则①：缺任一必需字段即阻止批准）。

        除 W06 的操作概要字段外，W07 起把**披露三节**纳入必需项：审批人看不到
        风险等级/补偿方案/验证计划时不得批准（fail closed）。三节只能由服务端
        在 `create()` 时刻生成，客户端提交会被 `extra="forbid"` 直接 422。
        """
        required: dict[str, Any] = {
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "action_digest": self.action_digest,
            "environment_fingerprint": self.environment_fingerprint or None,
            "environment_fingerprint_digest": self.environment_fingerprint_digest,
            "policy_version": self.policy_version,
            "action_descriptor": self.action_descriptor or None,
            "normalized_operation": self.normalized_operation or None,
            "prerequisites": self.prerequisites or None,
            "prerequisites_digest": self.prerequisites_digest,
            "expires_at": self.expires_at,
            "tenant_id": self.tenant_id,
            "environment_id": self.environment_id,
            "db_engine": self.db_engine,
        }
        missing = [name for name, value in required.items() if not value]
        # 写操作的 target_objects 必须有内容（P0-03b：声明为空即 fail closed）。
        if str(self.normalized_operation.get("side_effect") or "") == "write" and not self.target_objects:
            missing.append("target_objects")
        # W07：披露三节必须是服务端快照的实体内容，键存在但为空同样视为缺失。
        for section in DISCLOSURE_SECTION_KEYS:
            if not self.disclosure.get(section):
                missing.append(f"disclosure.{section}")
        return missing

    def missing_disclosure_sections(self) -> list[str]:
        """只返回缺失的披露小节名（`risk`/`compensation`/`verification_plan`）。"""
        return [
            section
            for section in DISCLOSURE_SECTION_KEYS
            if not self.disclosure.get(section)
        ]

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return _remaining_seconds(self.expires_at, now) <= 0


def _remaining_seconds(expires_at: datetime, now: datetime | None) -> float:
    current = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return round((expires_at - current).total_seconds(), 3)


__all__ = [
    "ALLOWED_APPROVAL_TRANSITIONS",
    "Approval",
    "ApprovalDetail",
    "ApprovalGrant",
    "ApprovalStatus",
    "CheckSource",
    "CheckStatus",
    "DECIDABLE_STATUSES",
    "DISCLOSURE_SECTION_KEYS",
    "NON_BLOCKING_CHECK_STATUSES",
    "PrerequisiteCheck",
    "REVOCABLE_STATUSES",
    "can_transition",
    "ensure_approval_transition",
    "ensure_segregation_of_duty",
    "error_for_status",
    "issue_grant",
]
