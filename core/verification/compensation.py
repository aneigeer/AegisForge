"""W08 补偿服务（T04）：补偿是**独立审批的 R2 动作**，不叫回滚。

结构约束（可逐条核对）：

1. 复用既有写工具 `mysql_lab_update_order_note`，**不新增写工具**、不升
   `tool_version`、不做自动补偿：`request()` 只创建一份**新的**待批审批；
2. 补偿参数 = 原动作参数中 `note` / `expected_note` **互换**（`note=before`、
   `expected_note=after`），其余字段（`database` / `table` / `order_id` / `keys`）
   逐字沿用 → 与原动作的 `action_digest` 必然不同，且同 `before` 两次构造必然相同；
3. `compensation_of` 只写进 `approvals` 的独立列（唯一键），**不进
   `action_digest`**：不碰 `DESCRIPTOR_KEYS` / `ACTION_DIGEST_VERSION`；
4. 独立性断言失败一律抛领域错误（`details.reason` 区分），并留
   `COMPENSATION_DENIED` 审计；不静默降级。

事件（设计 §5 第 4–7 行）：`COMPENSATION_REQUESTED` / `COMPENSATION_APPROVED` /
`COMPENSATION_EXECUTED` 由本服务发出；`COMPENSATION_EXECUTED` 由调用方在执行
侧 `step_observed` 写入后触发 `record_executed()`。
"""

from __future__ import annotations

from typing import Any, Mapping

from core.approval.models import Approval, ApprovalDetail, ApprovalStatus
from core.approval.service import ApprovalService
from core.audit.events import AuditEventType, make_event
from core.errors import (
    ApprovalNotFoundError,
    DBGuardianError,
    RequestInvalidError,
    StateConflictError,
)
from core.security.models import Principal
from core.tasks.repository import TaskRepository

# `COMPENSATION_DENIED.reason` 的稳定取值（设计 §5 第 7 行）。
COMPENSATION_REUSES_ORIGINAL_APPROVAL = "COMPENSATION_REUSES_ORIGINAL_APPROVAL"
COMPENSATION_DIGEST_NOT_INDEPENDENT = "COMPENSATION_DIGEST_NOT_INDEPENDENT"
COMPENSATION_ORIGINAL_NOT_CONSUMED = "COMPENSATION_ORIGINAL_NOT_CONSUMED"
COMPENSATION_PARAMETERS_MISSING = "COMPENSATION_PARAMETERS_MISSING"
COMPENSATION_NOT_A_COMPENSATION = "COMPENSATION_NOT_A_COMPENSATION"
COMPENSATION_NOT_APPROVED = "COMPENSATION_NOT_APPROVED"

# 补偿与被补偿动作必须共用的参数键（补偿只允许改 note / expected_note）。
_REQUIRED_KEYS = ("keys", "note", "expected_note")


class CompensationService:
    """补偿请求构造 + 独立性断言 + 补偿审计。"""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        audit: Any,
        tasks: TaskRepository,
    ) -> None:
        self._approvals = approvals
        self._audit = audit
        self._tasks = tasks

    # ---- 请求 ----------------------------------------------------------
    def request(
        self,
        *,
        original_approval: Approval | ApprovalDetail | str,
        before: str,
        after: str,
        principal: Principal,
    ) -> Approval:
        """为已执行的写动作发起**补偿审批**（新的 `approval_id`，不复用原审批）。"""
        original = self._load(original_approval, tenant_id=principal.tenant_id)
        try:
            self._ensure_compensable(original)
            if str(before) == str(after):
                # 无差异即无补偿可做；先拒再建，避免留下"永远不该存在"的待批单。
                raise StateConflictError(
                    "补偿前后取值相同，补偿动作没有意义",
                    details={
                        "reason": COMPENSATION_DIGEST_NOT_INDEPENDENT,
                        "approval_id": original.approval_id,
                    },
                )
            params = _compensation_parameters(original, before=before, after=after)
            detail = self._approvals.create(
                principal=principal,
                task_id=original.task_id,
                step_id=original.step_id,
                tool_name=original.tool_name,
                params=params,
                target_objects=[],
                compensation_of=original.approval_id,
            )
            compensation = self._approvals.repository.get(
                detail.approval_id, tenant_id=original.tenant_id
            )
            self.assert_independent(compensation, original)
        except DBGuardianError as exc:
            self._emit_denied(original, principal=principal, error=exc)
            raise
        self._emit(
            AuditEventType.COMPENSATION_REQUESTED,
            compensation,
            principal=principal,
            payload={
                "original_approval_id": original.approval_id,
                "compensation_approval_id": compensation.approval_id,
                "parameters_digest": compensation.parameters_digest,
                "action_digest": compensation.action_digest,
                "requested_by": principal.user_id,
            },
        )
        return compensation

    # ---- 裁决 ----------------------------------------------------------
    def approve(
        self,
        compensation_approval_id: str,
        *,
        principal: Principal,
        reason: str | None = None,
    ) -> ApprovalDetail:
        """批准补偿审批；批准人职责分离由 `ApprovalService.approve()` 保证。"""
        _, original_id = self._compensation_of_record(
            compensation_approval_id, principal=principal
        )
        detail = self._approvals.approve(
            compensation_approval_id, principal=principal, reason=reason
        )
        compensation = self._load(compensation_approval_id, tenant_id=principal.tenant_id)
        self._emit(
            AuditEventType.COMPENSATION_APPROVED,
            compensation,
            principal=principal,
            payload={
                "compensation_approval_id": compensation.approval_id,
                "original_approval_id": original_id,
                "approver_id": principal.user_id,
                "action_digest": compensation.action_digest,
            },
        )
        return detail

    def record_executed(
        self,
        *,
        compensation_approval_id: str,
        tool_call_id: str,
        observed_digest: str,
        outcome: str,
        principal: Principal,
    ) -> None:
        """补偿执行完成后由调用方触发（`step_observed` 已写入之后）。"""
        compensation, original_id = self._compensation_of_record(
            compensation_approval_id, principal=principal
        )
        if compensation.status not in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.CONSUMED,
        ):
            raise StateConflictError(
                "补偿审批尚未批准，禁止记录补偿执行",
                details={
                    "reason": COMPENSATION_NOT_APPROVED,
                    "approval_id": compensation_approval_id,
                    "status": compensation.status.value,
                },
            )
        self._emit(
            AuditEventType.COMPENSATION_EXECUTED,
            compensation,
            principal=principal,
            payload={
                "compensation_approval_id": compensation.approval_id,
                "original_approval_id": original_id,
                "tool_call_id": tool_call_id,
                "observed_digest": observed_digest,
                "outcome": outcome,
            },
        )

    # ---- 独立性 --------------------------------------------------------
    def assert_independent(self, compensation: Approval, original: Approval) -> None:
        """补偿必须是一份独立审批：不同 `approval_id`、不同 `action_digest`。"""
        if compensation.approval_id == original.approval_id:
            raise StateConflictError(
                "补偿不得复用原审批（必须是新的 R2 动作与新的审批）",
                details={
                    "reason": COMPENSATION_REUSES_ORIGINAL_APPROVAL,
                    "approval_id": original.approval_id,
                },
            )
        if compensation.action_digest == original.action_digest:
            raise StateConflictError(
                "补偿与原动作的动作摘要相同，不构成独立动作",
                details={
                    "reason": COMPENSATION_DIGEST_NOT_INDEPENDENT,
                    "approval_id": original.approval_id,
                    "compensation_approval_id": compensation.approval_id,
                    "action_digest": original.action_digest,
                },
            )

    # ---- 内部 ----------------------------------------------------------
    def _load(
        self, value: Approval | ApprovalDetail | str, *, tenant_id: str
    ) -> Approval:
        """归一化为 `Approval`；跨租户按「不存在」处理（不泄漏存在性）。"""
        if isinstance(value, Approval):
            if value.tenant_id != tenant_id:
                raise ApprovalNotFoundError(
                    "审批不存在", details={"approval_id": value.approval_id}
                )
            return value
        approval_id = (
            value.approval_id if isinstance(value, ApprovalDetail) else str(value)
        )
        return self._approvals.repository.get(approval_id, tenant_id=tenant_id)

    def _compensation_of_record(
        self, compensation_approval_id: str, *, principal: Principal
    ) -> tuple[Approval, str]:
        """取补偿审批本体 + `compensation_of`；缺失关联即拒绝（不猜原始审批）。"""
        approval = self._load(compensation_approval_id, tenant_id=principal.tenant_id)
        original_id = self._approvals.repository.get_compensation_of(
            compensation_approval_id
        )
        if original_id is None:
            raise StateConflictError(
                "该审批不是补偿审批（缺少 compensation_of 关联）",
                details={
                    "reason": COMPENSATION_NOT_A_COMPENSATION,
                    "approval_id": compensation_approval_id,
                },
            )
        return approval, original_id

    def _ensure_compensable(self, original: Approval) -> None:
        """补偿的前置事实：原动作必须已执行（审批已消费）。

        `CONSUMED` 才代表「审批凭证被用掉、动作真的发出过」；`APPROVED` 之前发起
        补偿会补偿一个可能根本没执行的动作。
        """
        if original.status is not ApprovalStatus.CONSUMED:
            raise StateConflictError(
                "原审批未执行（未消费），禁止发起补偿",
                details={
                    "reason": COMPENSATION_ORIGINAL_NOT_CONSUMED,
                    "approval_id": original.approval_id,
                    "status": original.status.value,
                },
            )

    def _run_id(self, task_id: str) -> str:
        try:
            return self._tasks.get_task(task_id).run_id
        except DBGuardianError:
            # 与 ApprovalService 同口径：任务行被清理时事件仍可写入。
            return "run_unknown"

    def _emit(
        self,
        event_type: AuditEventType,
        approval: Approval,
        *,
        principal: Principal | None,
        payload: dict[str, Any],
    ) -> None:
        self._audit.emit_required(
            make_event(
                event_type,
                task_id=approval.task_id,
                run_id=self._run_id(approval.task_id),
                payload={
                    **payload,
                    "task_id": approval.task_id,
                    "tenant_id": approval.tenant_id,
                    "actor_user_id": principal.user_id if principal else None,
                    "tool_name": approval.tool_name,
                    "tool_version": approval.tool_version,
                    "policy_version": approval.policy_version,
                },
            ),
            actor_user_id=principal.user_id if principal else None,
        )

    def _emit_denied(
        self,
        original: Approval,
        *,
        principal: Principal | None,
        error: DBGuardianError,
    ) -> None:
        """拒绝也要可解释：`reason` 取稳定原因，`deny_code` 取错误码。"""
        reason = error.details.get("reason") if isinstance(error.details, Mapping) else None
        self._emit(
            AuditEventType.COMPENSATION_DENIED,
            original,
            principal=principal,
            payload={
                "reason": str(reason) if reason else "COMPENSATION_REQUEST_INVALID",
                "deny_code": error.code.value,
                "original_approval_id": original.approval_id,
                "compensation_approval_id": None,
            },
        )


def _compensation_parameters(
    original: Approval, *, before: str, after: str
) -> dict[str, Any]:
    """补偿参数 = 原参数中 `note` / `expected_note` 互换，其余逐字沿用。

    只允许这两个键变化，是为了让「补偿」在参数层面就与「原动作」构成确定性差异：
    同 `before` 两次构造必得同一摘要，换 `before` 必得不同摘要。
    """
    source = original.action_descriptor.get("parameters")
    if not isinstance(source, Mapping):
        raise RequestInvalidError(
            "原审批缺少参数快照，无法构造补偿参数",
            details={
                "reason": COMPENSATION_PARAMETERS_MISSING,
                "approval_id": original.approval_id,
            },
        )
    missing = [key for key in _REQUIRED_KEYS if key not in source]
    if missing:
        raise RequestInvalidError(
            "原审批参数缺少补偿必需键",
            details={
                "reason": COMPENSATION_PARAMETERS_MISSING,
                "approval_id": original.approval_id,
                "missing_keys": missing,
            },
        )
    params = dict(source)
    params["note"] = before
    params["expected_note"] = after
    return params


__all__ = [
    "COMPENSATION_DIGEST_NOT_INDEPENDENT",
    "COMPENSATION_NOT_APPROVED",
    "COMPENSATION_NOT_A_COMPENSATION",
    "COMPENSATION_ORIGINAL_NOT_CONSUMED",
    "COMPENSATION_PARAMETERS_MISSING",
    "COMPENSATION_REUSES_ORIGINAL_APPROVAL",
    "CompensationService",
]
