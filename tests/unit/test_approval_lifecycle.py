"""审批状态白名单、职责分离与 seal 构造约束（P0-05/P0-09，T02 验收）。

本文件只做纯领域断言（不连数据库），保证状态机与凭证类型无法被"顺手放宽"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.approval.models import (
    ALLOWED_APPROVAL_TRANSITIONS,
    DECIDABLE_STATUSES,
    NON_BLOCKING_CHECK_STATUSES,
    REVOCABLE_STATUSES,
    Approval,
    ApprovalGrant,
    ApprovalStatus,
    PrerequisiteCheck,
    can_transition,
    ensure_approval_transition,
    ensure_segregation_of_duty,
    error_for_status,
    issue_grant,
)
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalExpiredError,
    ApprovalNotApprovedError,
    ApprovalRejectedError,
    ApprovalRevokedError,
    ErrorCode,
    ForbiddenError,
    StateConflictError,
)

ALL_STATUSES = list(ApprovalStatus)

# P0-05 的完整白名单（测试独立抄写一遍，防止实现被悄悄放宽）。
EXPECTED_ALLOWED = {
    (ApprovalStatus.PENDING, ApprovalStatus.APPROVED),
    (ApprovalStatus.PENDING, ApprovalStatus.REJECTED),
    (ApprovalStatus.PENDING, ApprovalStatus.EXPIRED),
    (ApprovalStatus.PENDING, ApprovalStatus.REVOKED),
    (ApprovalStatus.APPROVED, ApprovalStatus.CONSUMED),
    (ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED),
    (ApprovalStatus.APPROVED, ApprovalStatus.REVOKED),
}


def _disclosure_snapshot() -> dict[str, object]:
    """W07：合法的披露三节快照（字段集与 `core/approval/disclosure.py` 对齐）。"""
    return {
        "disclosure_version": "w07-v1",
        "captured_at": "2026-11-11T03:21:45.120Z",
        "risk": {
            "effective_risk": "R2",
            "factors": ["definition.risk_level=R2", "operation_class=repair", "side_effect=write"],
            "notes": [],
            "model_hint": None,
            "model_hint_accepted": False,
            "operation_known": True,
            "side_effect_known": True,
            "source": "risk_resolver",
            "policy_version": "w06-v1",
        },
        "compensation": {
            "action": "mysql_lab_update_order_note",
            "description": "把 note 写回执行前 before.note；补偿是独立 R2 动作",
            "is_rollback": False,
            "requires_own_approval": True,
            "tool_name": "mysql_lab_update_order_note",
            "tool_version": "1.0",
        },
        "verification_plan": {
            "assertions": [
                {
                    "assertion_id": "affected_rows",
                    "metric": "affected_rows",
                    "expected": "1",
                    "tolerance": "exact",
                    "source": "step_observed",
                }
            ],
            "min_assertions": 3,
        },
        "impact": {
            "reversible": True,
            "affects_other_tasks": False,
            "reversible_reason": "把 before.note 写回（独立 R2 动作，需新审批）",
            "affects_other_tasks_reason": "note 不参与外键/索引/约束",
        },
        "sources": {
            "risk": "risk_resolver",
            "compensation": "tool_definition",
            "verification_plan": "tool_definition",
        },
    }


def _approval(**overrides: object) -> Approval:
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "approval_id": "apr_test",
        "tenant_id": "tenant_local",
        "task_id": "task_test",
        "step_id": None,
        "environment_id": "env_mysql_local",
        "db_engine": "mysql",
        "environment_fingerprint": {
            "db_engine": "mysql",
            "db_version": "8.0.46",
            "image_digest": None,
            "network_id": "lab_net",
            "config_version": "w03-local",
        },
        "environment_fingerprint_digest": "f" * 64,
        "action_digest": "a" * 64,
        "parameters_digest": "b" * 64,
        "target_objects_digest": "c" * 64,
        "prerequisites_digest": "d" * 64,
        "prerequisites": [
            PrerequisiteCheck(
                check_id="environment_fingerprint",
                status="PASS",
                observed={"reason": "OK"},
                source="control_db",
            )
        ],
        "action_descriptor": {
            "normalized_operation": {
                "operation_class": "repair",
                "side_effect": "write",
                "idempotent": False,
                "statement_kind": "update",
                "summary": "更新一行",
            },
            "target_objects": [
                {
                    "object_type": "table",
                    "database": "dg_lab",
                    "schema": None,
                    "name": "orders",
                    "keys": ["id=1001"],
                }
            ],
        },
        "policy_version": "w06-v1",
        "tool_name": "mysql_update_rows",
        "tool_version": "1.0",
        "requested_by": "user_operator",
        "authn_context": {"auth_method": "token"},
        "nonce_hash": "e" * 64,
        "status": ApprovalStatus.PENDING,
        "created_at": now,
        "expires_at": now + timedelta(minutes=30),
        # W07：披露三节的创建时刻快照（服务端生成，客户端不可提交）。
        "disclosure": _disclosure_snapshot(),
        "disclosure_digest": "1" * 64,
        "disclosure_version": "w07-v1",
        "disclosed_at": now,
    }
    values.update(overrides)
    return Approval(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 状态白名单
# ---------------------------------------------------------------------------


def test_allowed_transitions_match_p0_whitelist_exactly() -> None:
    actual = {
        (current, target)
        for current in ALL_STATUSES
        for target in ALLOWED_APPROVAL_TRANSITIONS.get(current, frozenset())
    }
    assert actual == EXPECTED_ALLOWED


def test_terminal_statuses_have_no_outgoing_transition() -> None:
    for terminal in (
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.REVOKED,
        ApprovalStatus.CONSUMED,
    ):
        assert ALLOWED_APPROVAL_TRANSITIONS[terminal] == frozenset()


@pytest.mark.parametrize("current", ALL_STATUSES)
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_can_transition_matches_whitelist(current: ApprovalStatus, target: ApprovalStatus) -> None:
    assert can_transition(current, target) == ((current, target) in EXPECTED_ALLOWED)


@pytest.mark.parametrize("current", ALL_STATUSES)
@pytest.mark.parametrize("target", ALL_STATUSES)
def test_illegal_transition_raises_state_conflict(
    current: ApprovalStatus, target: ApprovalStatus
) -> None:
    if (current, target) in EXPECTED_ALLOWED:
        ensure_approval_transition(current, target)
        return
    with pytest.raises(StateConflictError) as excinfo:
        ensure_approval_transition(current, target)
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT


def test_approved_cannot_fall_back_to_pending() -> None:
    with pytest.raises(StateConflictError):
        ensure_approval_transition(ApprovalStatus.APPROVED, ApprovalStatus.PENDING)


def test_decidable_and_revocable_sets_are_narrow() -> None:
    assert DECIDABLE_STATUSES == frozenset({ApprovalStatus.PENDING})
    assert REVOCABLE_STATUSES == frozenset(
        {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
    )
    assert ApprovalStatus.CONSUMED not in REVOCABLE_STATUSES


# ---------------------------------------------------------------------------
# 状态 → 错误码映射（设计 §5 第 2 项）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_type,expected_code",
    [
        (ApprovalStatus.PENDING, ApprovalNotApprovedError, ErrorCode.APPROVAL_NOT_APPROVED),
        (ApprovalStatus.REJECTED, ApprovalRejectedError, ErrorCode.APPROVAL_REJECTED),
        (ApprovalStatus.EXPIRED, ApprovalExpiredError, ErrorCode.APPROVAL_EXPIRED),
        (ApprovalStatus.REVOKED, ApprovalRevokedError, ErrorCode.APPROVAL_REVOKED),
        (
            ApprovalStatus.CONSUMED,
            ApprovalAlreadyConsumedError,
            ErrorCode.APPROVAL_ALREADY_CONSUMED,
        ),
        (ApprovalStatus.APPROVED, StateConflictError, ErrorCode.STATE_CONFLICT),
    ],
)
def test_error_for_status_mapping(status: ApprovalStatus, expected_type: type, expected_code) -> None:
    error = error_for_status(status, approval_id="apr_test")
    assert isinstance(error, expected_type)
    assert error.code is expected_code
    assert error.details["approval_id"] == "apr_test"


def test_error_codes_are_stable_strings() -> None:
    assert ErrorCode.APPROVAL_NOT_FOUND.value == "APPROVAL_NOT_FOUND"
    assert ErrorCode.APPROVAL_DIGEST_MISMATCH.value == "APPROVAL_DIGEST_MISMATCH"
    assert ErrorCode.APPROVAL_PREREQUISITE_FAILED.value == "APPROVAL_PREREQUISITE_FAILED"
    assert ErrorCode.AUDIT_UNAVAILABLE.value == "AUDIT_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 职责分离
# ---------------------------------------------------------------------------


def test_segregation_of_duty_blocks_self_approval() -> None:
    with pytest.raises(ForbiddenError) as excinfo:
        ensure_segregation_of_duty(requested_by="user_operator", approver_id="user_operator")
    assert excinfo.value.code is ErrorCode.FORBIDDEN
    assert excinfo.value.details["reason"] == "SEGREGATION_OF_DUTY"
    assert excinfo.value.status_code == 403


def test_segregation_of_duty_ignores_session_boundary() -> None:
    """同一用户以不同会话操作仍算自批（P0-09 明文要求）。"""
    with pytest.raises(ForbiddenError):
        ensure_segregation_of_duty(requested_by=" user_operator ", approver_id="user_operator")


def test_segregation_of_duty_allows_different_users() -> None:
    ensure_segregation_of_duty(requested_by="user_operator", approver_id="user_approver")


# ---------------------------------------------------------------------------
# ApprovalGrant seal
# ---------------------------------------------------------------------------


def test_grant_cannot_be_constructed_directly() -> None:
    with pytest.raises(ValueError):
        ApprovalGrant(
            approval_id="apr_test",
            tenant_id="tenant_local",
            task_id="task_test",
            tool_name="mysql_update_rows",
            tool_version="1.0",
            environment_id="env_mysql_local",
            action_digest="a" * 64,
            prerequisites_digest="d" * 64,
            policy_version="w06-v1",
            consumed_by="user_worker",
            consumed_at=datetime.now(timezone.utc),
        )


def test_grant_cannot_be_forged_with_none_seal() -> None:
    with pytest.raises(ValueError):
        ApprovalGrant(
            approval_id="apr_test",
            tenant_id="tenant_local",
            task_id="task_test",
            tool_name="mysql_update_rows",
            tool_version="1.0",
            environment_id="env_mysql_local",
            action_digest="a" * 64,
            prerequisites_digest="d" * 64,
            policy_version="w06-v1",
            consumed_by="user_worker",
            consumed_at=datetime.now(timezone.utc),
            grant_seal=None,
        )


def test_grant_cannot_be_forged_with_foreign_seal() -> None:
    with pytest.raises(ValueError):
        ApprovalGrant(
            approval_id="apr_test",
            tenant_id="tenant_local",
            task_id="task_test",
            tool_name="mysql_update_rows",
            tool_version="1.0",
            environment_id="env_mysql_local",
            action_digest="a" * 64,
            prerequisites_digest="d" * 64,
            policy_version="w06-v1",
            consumed_by="user_worker",
            consumed_at=datetime.now(timezone.utc),
            grant_seal=object(),
        )


def test_issue_grant_rejects_blank_required_field() -> None:
    with pytest.raises(ValueError):
        issue_grant(
            approval_id="",
            tenant_id="tenant_local",
            task_id="task_test",
            tool_name="mysql_update_rows",
            tool_version="1.0",
            environment_id="env_mysql_local",
            action_digest="a" * 64,
            prerequisites_digest="d" * 64,
            policy_version="w06-v1",
            consumed_by="user_worker",
            consumed_at=datetime.now(timezone.utc),
        )


def test_issue_grant_returns_frozen_grant() -> None:
    grant = issue_grant(
        approval_id="apr_test",
        tenant_id="tenant_local",
        task_id="task_test",
        tool_name="mysql_update_rows",
        tool_version="1.0",
        environment_id="env_mysql_local",
        action_digest="a" * 64,
        prerequisites_digest="d" * 64,
        policy_version="w06-v1",
        consumed_by="user_worker",
        consumed_at=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc),
    )
    assert grant.approval_id == "apr_test"
    assert grant.step_id is None
    with pytest.raises(Exception):
        grant.approval_id = "apr_other"  # type: ignore[misc]
    public = grant.to_public_dict()
    assert public["action_digest"] == "a" * 64
    assert "grant_seal" not in public


# ---------------------------------------------------------------------------
# 前置条件与披露字段
# ---------------------------------------------------------------------------


def test_non_blocking_check_statuses() -> None:
    assert NON_BLOCKING_CHECK_STATUSES == frozenset({"PASS", "NOT_APPLICABLE"})
    assert PrerequisiteCheck(check_id="x", status="PASS", source="control_db").blocks() is False
    assert (
        PrerequisiteCheck(check_id="x", status="NOT_APPLICABLE", source="control_db").blocks()
        is False
    )
    assert PrerequisiteCheck(check_id="x", status="FAIL", source="control_db").blocks() is True
    assert PrerequisiteCheck(check_id="x", status="UNKNOWN", source="control_db").blocks() is True


def test_prerequisite_check_rejects_extra_field() -> None:
    with pytest.raises(Exception):
        PrerequisiteCheck(
            check_id="x",
            status="PASS",
            observed={},
            source="control_db",
            operator_note="人工填的前置条件",  # 人工输入不得进入摘要结构
        )


def test_prerequisite_check_digest_dict_has_closed_keys() -> None:
    check = PrerequisiteCheck(check_id="x", status="PASS", observed={"a": 1}, source="control_db")
    assert set(check.to_digest_dict()) == {"check_id", "status", "observed", "source"}


def test_detail_missing_fields_blocks_write_without_target_objects() -> None:
    approval = _approval(
        action_descriptor={
            "normalized_operation": {
                "operation_class": "repair",
                "side_effect": "write",
                "idempotent": False,
                "statement_kind": "update",
                "summary": "更新一行",
            },
            "target_objects": [],
        }
    )
    detail = approval.to_detail()
    assert "target_objects" in detail.missing_required_fields()


def test_detail_missing_fields_empty_for_complete_approval() -> None:
    detail = _approval().to_detail()
    assert detail.missing_required_fields() == []


def test_detail_missing_fields_reports_absent_digest() -> None:
    approval = _approval()
    detail = approval.to_detail().model_copy(update={"action_digest": ""})
    assert "action_digest" in detail.missing_required_fields()


def test_missing_required_fields_includes_disclosure_sections() -> None:
    """W07：披露三节缺失必须逐项进入 missing_fields（不止报第一项）。"""
    approval = _approval(disclosure={})
    detail = approval.to_detail()
    missing = detail.missing_required_fields()
    assert "disclosure.risk" in missing
    assert "disclosure.compensation" in missing
    assert "disclosure.verification_plan" in missing
    assert detail.missing_disclosure_sections() == [
        "risk",
        "compensation",
        "verification_plan",
    ]


def test_missing_required_fields_accepts_complete_disclosure() -> None:
    """三节齐全时不得把 `disclosure.*` 报成缺失（否则等于永远批不了）。"""
    detail = _approval().to_detail()
    assert detail.missing_required_fields() == []
    assert detail.missing_disclosure_sections() == []


def test_detail_remaining_seconds_and_expiry() -> None:
    now = datetime.now(timezone.utc)
    approval = _approval(expires_at=now + timedelta(seconds=60))
    detail = approval.to_detail(now=now)
    assert 59.0 <= detail.remaining_seconds <= 61.0
    assert detail.is_expired(now=now) is False
    assert detail.is_expired(now=now + timedelta(seconds=61)) is True
