"""W09 T03 / W08 KD-1：复用原审批驱动补偿被拒时补发 COMPENSATION_DENIED。

单测用内存桩，只覆盖 ``ApprovalService.consume()`` 第 2 步（状态检查）之前
补发的一条事件；不连库、不依赖 DG_ADMIN_DSN / PG 5432、不重排任何校验。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.approval.models import ApprovalStatus
from core.approval.service import (
    COMPENSATION_REUSES_ORIGINAL_APPROVAL,
    ApprovalService,
)
from core.audit.events import AuditEventType
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalNotApprovedError,
    ErrorCode,
)
from core.verification.compensation import (
    COMPENSATION_REUSES_ORIGINAL_APPROVAL as VERIFICATION_REASON,
)

_TENANT = "tenant_local"
_TASK = "task_w09_kd1"
_ORIGINAL = "apr_original"
_COMPENSATION = "apr_compensation"


class _FakeApprovalRepository:
    """只实现第 2 步会用到的两个只读方法；多余调用会直接 AttributeError。"""

    def __init__(
        self,
        status: ApprovalStatus,
        *,
        compensation_approval_id: str | None = None,
    ) -> None:
        self.status = status
        self.compensation_approval_id = compensation_approval_id
        self.calls: list[tuple[str, str]] = []

    def get(self, approval_id: str, *, tenant_id: str | None = None) -> Any:
        self.calls.append(("get", approval_id))
        return SimpleNamespace(
            approval_id=approval_id,
            tenant_id=tenant_id,
            task_id=_TASK,
            status=self.status,
        )

    def find_compensation_for(self, original_approval_id: str) -> str | None:
        self.calls.append(("find_compensation_for", original_approval_id))
        return self.compensation_approval_id


class _FakeAudit:
    def __init__(self, *, emit_ok: bool = True) -> None:
        self.events: list[Any] = []
        self.emit_ok = emit_ok
        self.emit_required_calls = 0

    def try_emit(self, event: Any, *, actor_user_id: str | None = None) -> bool:
        self.events.append(event)
        return self.emit_ok

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.emit_required_calls += 1
        raise AssertionError("KD-1 拒绝事件必须走 try_emit")


def _service(repository: _FakeApprovalRepository, audit: _FakeAudit) -> ApprovalService:
    probe = SimpleNamespace(
        policy_version="w06-v1", bind_policy_version=lambda value: None
    )
    settings = SimpleNamespace(policy_version="w06-v1", action_digest_version="v1")
    tasks = SimpleNamespace(
        get_task=lambda task_id, tenant_id=None: SimpleNamespace(run_id="run_w09_kd1")
    )
    return ApprovalService(
        repository=repository,  # type: ignore[arg-type]
        task_repository=tasks,  # type: ignore[arg-type]
        probe=probe,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        risk_resolver=object(),  # type: ignore[arg-type]
    )


def _consume(service: ApprovalService) -> Exception:
    principal = SimpleNamespace(user_id="user_w09", tenant_id=_TENANT)
    task = SimpleNamespace(
        task_id=_TASK,
        run_id="run_w09_kd1",
        tenant_id=_TENANT,
        environment_id="env_mysql_local",
    )
    with pytest.raises(Exception) as raised:
        service.consume(
            approval_id=_ORIGINAL,
            principal=principal,  # type: ignore[arg-type]
            task=task,  # type: ignore[arg-type]
            definition=None,  # type: ignore[arg-type]
            params=None,
            environment_row={},
            policy_version="w06-v1",
            envelope_id=None,
            tool_call_id=None,
        )
    return raised.value


def test_reason_constant_matches_verification_module() -> None:
    """防漂移：approval 侧常量与 verification 侧同值，且不引入反向依赖。"""
    assert COMPENSATION_REUSES_ORIGINAL_APPROVAL == VERIFICATION_REASON
    assert COMPENSATION_REUSES_ORIGINAL_APPROVAL == "COMPENSATION_REUSES_ORIGINAL_APPROVAL"


def test_reused_consumed_approval_emits_one_compensation_denied_event() -> None:
    repository = _FakeApprovalRepository(
        ApprovalStatus.CONSUMED, compensation_approval_id=_COMPENSATION
    )
    audit = _FakeAudit()
    error = _consume(_service(repository, audit))

    assert isinstance(error, ApprovalAlreadyConsumedError)
    assert error.code is ErrorCode.APPROVAL_ALREADY_CONSUMED
    assert repository.calls == [
        ("get", _ORIGINAL),
        ("find_compensation_for", _ORIGINAL),
    ]
    assert len(audit.events) == 1
    assert audit.emit_required_calls == 0
    event = audit.events[0]
    assert event.event_type == AuditEventType.COMPENSATION_DENIED.value
    assert event.task_id == _TASK
    payload = event.payload
    assert payload["reason"] == COMPENSATION_REUSES_ORIGINAL_APPROVAL
    assert payload["deny_code"] == ErrorCode.APPROVAL_ALREADY_CONSUMED.value
    assert payload["original_approval_id"] == _ORIGINAL
    assert payload["compensation_approval_id"] == _COMPENSATION
    assert payload["status"] == ApprovalStatus.CONSUMED.value


def test_no_compensation_approval_emits_no_event() -> None:
    repository = _FakeApprovalRepository(
        ApprovalStatus.CONSUMED, compensation_approval_id=None
    )
    audit = _FakeAudit()
    error = _consume(_service(repository, audit))
    assert isinstance(error, ApprovalAlreadyConsumedError)
    assert audit.events == []


def test_non_consumed_status_does_not_query_compensation() -> None:
    repository = _FakeApprovalRepository(
        ApprovalStatus.PENDING, compensation_approval_id=_COMPENSATION
    )
    audit = _FakeAudit()
    error = _consume(_service(repository, audit))
    assert isinstance(error, ApprovalNotApprovedError)
    assert repository.calls == [("get", _ORIGINAL)]
    assert audit.events == []


def test_audit_write_failure_does_not_change_denial() -> None:
    """拒绝优先：try_emit 返回 False 也不把拒绝改成放行或 500。"""
    repository = _FakeApprovalRepository(
        ApprovalStatus.CONSUMED, compensation_approval_id=_COMPENSATION
    )
    audit = _FakeAudit(emit_ok=False)
    error = _consume(_service(repository, audit))
    assert isinstance(error, ApprovalAlreadyConsumedError)
    assert len(audit.events) == 1
