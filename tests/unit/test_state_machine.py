"""任务状态机单元测试：15 状态、白名单、非法转换、终态与安全不变量。"""

from __future__ import annotations

import pytest

from core.errors import ErrorCode, StateInvalidError
from core.tasks.models import TERMINAL_STATUSES, TaskStatus
from core.tasks.state_machine import (
    ALLOWED_TRANSITIONS,
    allowed_targets,
    can_transition,
    ensure_transition,
    is_terminal,
)

EXPECTED_STATUS_NAMES = {
    "PENDING",
    "PLANNING",
    "OBSERVING",
    "DIAGNOSING",
    "WAITING_APPROVAL",
    "EXECUTING",
    "VERIFYING",
    "PAUSED",
    "CANCEL_REQUESTED",
    "NEEDS_RECONCILIATION",
    "MANUAL_INTERVENTION",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
}

ALLOWED_PAIRS = [
    (source, target)
    for source, targets in ALLOWED_TRANSITIONS.items()
    for target in targets
]

ILLEGAL_PAIRS = [
    (TaskStatus.PENDING, TaskStatus.COMPLETED),
    (TaskStatus.PENDING, TaskStatus.EXECUTING),
    (TaskStatus.PENDING, TaskStatus.VERIFYING),
    (TaskStatus.OBSERVING, TaskStatus.EXECUTING),
    (TaskStatus.EXECUTING, TaskStatus.PAUSED),
    (TaskStatus.COMPLETED, TaskStatus.PLANNING),
    (TaskStatus.CANCELLED, TaskStatus.EXECUTING),
    (TaskStatus.FAILED, TaskStatus.PLANNING),
    (TaskStatus.CANCEL_REQUESTED, TaskStatus.PLANNING),
]


def test_all_fifteen_statuses_present() -> None:
    assert {status.name for status in TaskStatus} == EXPECTED_STATUS_NAMES
    assert len(TaskStatus) == 15
    assert set(ALLOWED_TRANSITIONS) == set(TaskStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    ALLOWED_PAIRS,
    ids=[f"{current.name}->{target.name}" for current, target in ALLOWED_PAIRS],
)
def test_allowed_transition_is_accepted(current: TaskStatus, target: TaskStatus) -> None:
    assert can_transition(current, target) is True
    assert ensure_transition(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    ILLEGAL_PAIRS,
    ids=[f"{current.name}->{target.name}" for current, target in ILLEGAL_PAIRS],
)
def test_illegal_transition_is_rejected(current: TaskStatus, target: TaskStatus) -> None:
    assert can_transition(current, target) is False
    with pytest.raises(StateInvalidError) as exc_info:
        ensure_transition(current, target)

    error = exc_info.value
    assert error.code is ErrorCode.STATE_INVALID
    assert error.code.value == "STATE_INVALID"
    assert error.details == {"from": current.value, "to": target.value}
    assert error.status_code == 409


@pytest.mark.parametrize(
    "status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED],
    ids=lambda status: status.name,
)
def test_terminal_status_has_no_outgoing_edges(status: TaskStatus) -> None:
    assert is_terminal(status) is True
    assert ALLOWED_TRANSITIONS[status] == frozenset()
    assert allowed_targets(status) == []
    for target in TaskStatus:
        assert can_transition(status, target) is False


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.PENDING,
        TaskStatus.PAUSED,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.NEEDS_RECONCILIATION,
        TaskStatus.TIMED_OUT,
    ],
    ids=lambda status: status.name,
)
def test_non_terminal_status_is_not_terminal(status: TaskStatus) -> None:
    assert is_terminal(status) is False
    assert ALLOWED_TRANSITIONS[status] != frozenset()


@pytest.mark.parametrize("status", list(TaskStatus), ids=lambda status: status.name)
def test_allowed_targets_is_sorted_string_list_matching_whitelist(
    status: TaskStatus,
) -> None:
    targets = allowed_targets(status)
    assert isinstance(targets, list)
    assert all(isinstance(target, str) for target in targets)
    assert targets == sorted(targets)
    assert set(targets) == {target.value for target in ALLOWED_TRANSITIONS[status]}


@pytest.mark.parametrize("status", list(TaskStatus), ids=lambda status: status.name)
def test_self_transition_is_allowed(status: TaskStatus) -> None:
    assert ensure_transition(status, status) is None


@pytest.mark.parametrize(
    "target",
    [TaskStatus.EXECUTING, TaskStatus.VERIFYING],
    ids=lambda status: status.name,
)
def test_pending_cannot_skip_steps_into_execution(target: TaskStatus) -> None:
    assert can_transition(TaskStatus.PENDING, target) is False
    with pytest.raises(StateInvalidError) as exc_info:
        ensure_transition(TaskStatus.PENDING, target)

    error = exc_info.value
    assert error.code.value == "STATE_INVALID"
    assert error.details == {"from": "PENDING", "to": target.value}


def test_paused_can_resume_or_cancel() -> None:
    assert can_transition(TaskStatus.PAUSED, TaskStatus.PLANNING) is True
    assert can_transition(TaskStatus.PAUSED, TaskStatus.OBSERVING) is True
    assert can_transition(TaskStatus.PAUSED, TaskStatus.DIAGNOSING) is True
    assert can_transition(TaskStatus.PAUSED, TaskStatus.EXECUTING) is True
    assert can_transition(TaskStatus.PAUSED, TaskStatus.CANCEL_REQUESTED) is True
    assert can_transition(TaskStatus.PAUSED, TaskStatus.CANCELLED) is False


def test_executing_must_go_through_cancel_requested() -> None:
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.CANCEL_REQUESTED) is True
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.CANCELLED) is False
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.NEEDS_RECONCILIATION) is True
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.VERIFYING) is True
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.PAUSED) is False


def test_cancel_requested_cannot_return_to_planning() -> None:
    assert can_transition(TaskStatus.CANCEL_REQUESTED, TaskStatus.CANCELLED) is True
    assert can_transition(
        TaskStatus.CANCEL_REQUESTED, TaskStatus.NEEDS_RECONCILIATION
    ) is True
    assert can_transition(TaskStatus.CANCEL_REQUESTED, TaskStatus.FAILED) is True
    for target in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.EXECUTING):
        assert can_transition(TaskStatus.CANCEL_REQUESTED, target) is False


def test_needs_reconciliation_paths() -> None:
    assert can_transition(
        TaskStatus.NEEDS_RECONCILIATION, TaskStatus.VERIFYING
    ) is True
    assert can_transition(TaskStatus.NEEDS_RECONCILIATION, TaskStatus.COMPLETED) is True
    assert can_transition(
        TaskStatus.NEEDS_RECONCILIATION, TaskStatus.MANUAL_INTERVENTION
    ) is True
    assert can_transition(TaskStatus.NEEDS_RECONCILIATION, TaskStatus.CANCELLED) is True
    assert can_transition(TaskStatus.NEEDS_RECONCILIATION, TaskStatus.FAILED) is True
    assert can_transition(TaskStatus.NEEDS_RECONCILIATION, TaskStatus.PLANNING) is False


def test_terminal_status_set_matches_models() -> None:
    assert {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    } == set(TERMINAL_STATUSES)
    assert {status for status in TaskStatus if is_terminal(status)} == set(
        TERMINAL_STATUSES
    )
