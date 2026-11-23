"""审批 API（P0-10，PRD §6）：创建/查询/批准/拒绝/撤销。

权限矩阵（服务端 Principal 决定，客户端提交的角色不参与授权）：
- `POST /v1/approvals`                  → `approval:create`
- `GET  /v1/approvals`、`/{id}`         → `approval:read`
- `POST /{id}/approve`、`/{id}/reject`  → `approval:decide`
- `POST /{id}/revoke`                   → 申请人本人或 `approval:decide`（服务层复核）

响应必须包含 PRD §4 的全部披露字段：无法由 W06 数据模型提供的字段显式标记
`provided=false`（"未提供"），绝不折叠隐藏、也不只返回摘要哈希。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_approval_service, get_principal
from core.approval.models import ApprovalDetail, ApprovalStatus
from core.approval.service import ApprovalService
from core.errors import ForbiddenError
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

MAX_REASON = 512
MAX_OPERATOR_NOTE = 2000


class CreateApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    step_id: str | None = Field(default=None, max_length=64)
    tool_name: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    # 目标对象可由工具契约从 params 抽取；显式给出时必须符合五键封闭结构。
    target_objects: list[dict[str, Any]] = Field(default_factory=list)
    operator_note: str | None = Field(default=None, max_length=MAX_OPERATOR_NOTE)
    ttl_s: float | None = Field(default=None, gt=0)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_REASON)


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_REASON)


def _disclosure(detail: ApprovalDetail) -> dict[str, Any]:
    """按 PRD §4 组装披露块：每个字段都出现，缺失的显式标注"未提供"。

    W07 起三节读取的是**创建时刻落库的快照**（`disclosure_json`），不再现算；
    也不再出现 W06 的"未持久化"占位文案。顶层同时给出 `disclosure_digest` /
    `disclosure_version` / `disclosed_at`，供审批人核对"页面显示的就是落库的那份"。
    """
    operation = detail.normalized_operation
    targets = detail.target_objects
    snapshot = detail.disclosure or {}
    risk = snapshot.get("risk") if isinstance(snapshot.get("risk"), dict) else None
    compensation = (
        snapshot.get("compensation")
        if isinstance(snapshot.get("compensation"), dict)
        else None
    )
    verification = (
        snapshot.get("verification_plan")
        if isinstance(snapshot.get("verification_plan"), dict)
        else None
    )
    impact = snapshot.get("impact") if isinstance(snapshot.get("impact"), dict) else None
    legacy_reason = "LEGACY_ROW_WITHOUT_DISCLOSURE"
    captured_at = snapshot.get("captured_at")

    impact_missing: list[str] = []
    for key in ("reversible", "affects_other_tasks"):
        if impact is None or impact.get(key) is None:
            impact_missing.append(key)

    return {
        "操作": {
            "provided": bool(detail.tool_name and operation),
            "tool_name": detail.tool_name,
            "tool_version": detail.tool_version,
            "normalized_operation": operation,
            "action_descriptor": detail.action_descriptor,
        },
        "目标对象": {
            "provided": bool(targets),
            "items": targets,
            "object_count": len(targets),
        },
        "影响面": {
            "provided": bool(impact),
            "side_effect": operation.get("side_effect"),
            "operation_class": operation.get("operation_class"),
            "idempotent": operation.get("idempotent"),
            "object_count": len(targets),
            # W07：可逆性与跨任务影响来自服务端快照（P1-W07-01 的事实来源）。
            "reversible": None if impact is None else impact.get("reversible"),
            "affects_other_tasks": (
                None if impact is None else impact.get("affects_other_tasks")
            ),
            "reversible_reason": (
                None if impact is None else impact.get("reversible_reason")
            ),
            "affects_other_tasks_reason": (
                None if impact is None else impact.get("affects_other_tasks_reason")
            ),
            "missing": impact_missing,
        },
        "风险等级": {
            "provided": risk is not None,
            "effective_risk": None if risk is None else risk.get("effective_risk"),
            "factors": [] if risk is None else list(risk.get("factors") or []),
            "notes": [] if risk is None else list(risk.get("notes") or []),
            "model_hint": None if risk is None else risk.get("model_hint"),
            "model_hint_accepted": (
                None if risk is None else risk.get("model_hint_accepted")
            ),
            "source": None if risk is None else risk.get("source"),
            "captured_at": captured_at,
            **({"reason": legacy_reason} if risk is None else {}),
        },
        "前置条件": {
            "provided": bool(detail.prerequisites),
            "digest": detail.prerequisites_digest,
            "checks": [check.model_dump(mode="json") for check in detail.prerequisites],
        },
        "补偿方案": {
            "provided": compensation is not None,
            "action": None if compensation is None else compensation.get("action"),
            "description": (
                None if compensation is None else compensation.get("description")
            ),
            "tool_name": (
                None if compensation is None else compensation.get("tool_name")
            ),
            "tool_version": (
                None if compensation is None else compensation.get("tool_version")
            ),
            # 补偿不是回滚，且补偿本身是独立操作、需独立策略与必要时独立审批。
            "is_rollback": (
                False if compensation is None else bool(compensation.get("is_rollback"))
            ),
            "requires_own_approval": (
                None
                if compensation is None
                else compensation.get("requires_own_approval")
            ),
            **({"reason": legacy_reason} if compensation is None else {}),
        },
        "验证计划": {
            "provided": verification is not None,
            "assertions": (
                [] if verification is None else list(verification.get("assertions") or [])
            ),
            "assertion_count": (
                0
                if verification is None
                else len(verification.get("assertions") or [])
            ),
            "min_assertions": (
                None if verification is None else verification.get("min_assertions")
            ),
            **({"reason": legacy_reason} if verification is None else {}),
        },
        "有效期": {
            "provided": True,
            "expires_at": detail.expires_at.isoformat(),
            "remaining_seconds": detail.remaining_seconds,
        },
        "绑定信息": {
            "provided": True,
            "tenant_id": detail.tenant_id,
            "environment_id": detail.environment_id,
            "environment_fingerprint": detail.environment_fingerprint,
            "environment_fingerprint_digest": detail.environment_fingerprint_digest,
            "policy_version": detail.policy_version,
            "action_digest": detail.action_digest,
            "parameters_digest": detail.parameters_digest,
            "target_objects_digest": detail.target_objects_digest,
            "prerequisites_digest": detail.prerequisites_digest,
            "db_engine": detail.db_engine,
            "disclosure_digest": detail.disclosure_digest,
            "disclosure_version": detail.disclosure_version,
            "disclosed_at": (
                None if detail.disclosed_at is None else detail.disclosed_at.isoformat()
            ),
        },
    }


def _render(
    detail: ApprovalDetail, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """详情响应：审批记录 + 披露块 + 状态语义 + 阻止批准所需的缺失字段清单。"""
    payload = detail.model_dump(mode="json")
    payload["disclosure"] = _disclosure(detail)
    payload["missing_required_fields"] = detail.missing_required_fields()
    payload["covered_by_action_digest"] = True
    if state is not None:
        payload["state_semantics"] = state
    return payload


@router.post("", status_code=200)
def create_approval(
    request: CreateApprovalRequest,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.APPROVAL_CREATE)
    detail = service.create(
        principal=principal,
        task_id=request.task_id,
        step_id=request.step_id,
        tool_name=request.tool_name,
        params=request.params,
        target_objects=request.target_objects,
        operator_note=request.operator_note,
        ttl_s=request.ttl_s,
    )
    return _render(detail, service.state_of(detail))


@router.get("")
def list_approvals(
    status: ApprovalStatus | None = None,
    task_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.APPROVAL_READ)
    details, next_cursor = service.list(
        tenant_id=principal.tenant_id,
        status=status,
        task_id=task_id,
        limit=limit,
        cursor=cursor,
    )
    states = service.states_of(details)
    return {
        "items": [_render(detail, states.get(detail.approval_id)) for detail in details],
        "next_cursor": next_cursor,
    }


@router.get("/{approval_id}")
def get_approval(
    approval_id: str,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.APPROVAL_READ)
    detail = service.get(approval_id, tenant_id=principal.tenant_id)
    return _render(detail, service.state_of(detail))


@router.post("/{approval_id}/approve")
def approve_approval(
    approval_id: str,
    request: ApproveRequest,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.APPROVAL_DECIDE)
    detail = service.approve(approval_id, principal=principal, reason=request.reason)
    return _render(detail, service.state_of(detail))


@router.post("/{approval_id}/reject")
def reject_approval(
    approval_id: str,
    request: RejectRequest,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    ensure_permission(principal, Permission.APPROVAL_DECIDE)
    detail = service.reject(approval_id, principal=principal, reason=request.reason)
    return _render(detail, service.state_of(detail))


@router.post("/{approval_id}/revoke")
def revoke_approval(
    approval_id: str,
    request: RevokeRequest,
    principal: Principal = Depends(get_principal),
    service: ApprovalService = Depends(get_approval_service),
) -> dict[str, Any]:
    # 撤销权归属在服务层复核（申请人本人或具备 approval:decide 的角色）；
    # 路由层只要求"与审批相关"的任一权限，避免把申请人（Operator）挡在门外。
    allowed = (
        principal.has_permission(Permission.APPROVAL_CREATE)
        or principal.has_permission(Permission.APPROVAL_READ)
        or principal.has_permission(Permission.APPROVAL_DECIDE)
    )
    if not allowed:
        raise ForbiddenError(
            "当前身份缺少撤销审批所需权限",
            details={
                "user_id": principal.user_id,
                "permissions": [
                    Permission.APPROVAL_CREATE.value,
                    Permission.APPROVAL_READ.value,
                    Permission.APPROVAL_DECIDE.value,
                ],
            },
        )
    detail = service.revoke(approval_id, principal=principal, reason=request.reason)
    return _render(detail, service.state_of(detail))


__all__ = ["router"]
