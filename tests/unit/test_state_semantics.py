"""W07 D4：状态语义推导表逐行断言（纯函数，不连数据库）。

§6.2 的推导必须**互斥且完备**：每个 (approvals.status, step_prepared,
step_observed.outcome) 组合都落到唯一状态，且四态（PRD 强制）之外的
`FAILED` / `CLOSED` 是"精确化拆分"而非"合并表述"。
"""

from __future__ import annotations

import pytest

from core.approval.state_semantics import (
    STATE_ACTIONS,
    STATE_AWAITING_APPROVAL,
    STATE_CLOSED,
    STATE_EXECUTED,
    STATE_FAILED,
    STATE_LABELS,
    STATE_SUGGESTED,
    STATE_UNKNOWN_OUTCOME,
    TERMINAL_STATES,
    ExecutionEvidence,
    derive_state,
    evidence_from_mapping,
)

_FOUR_STATES = {
    STATE_AWAITING_APPROVAL,
    STATE_SUGGESTED,
    STATE_EXECUTED,
    STATE_UNKNOWN_OUTCOME,
}


@pytest.mark.parametrize(
    (
        "status",
        "evidence",
        "expected_state",
        "expected_action",
        "expected_degraded",
    ),
    [
        # 表 1：PENDING 任意证据 → 等待审批
        ("PENDING", None, STATE_AWAITING_APPROVAL, "approve_or_reject", False),
        (
            "PENDING",
            ExecutionEvidence(step_prepared=False, step_observed_outcome=None),
            STATE_AWAITING_APPROVAL,
            "approve_or_reject",
            False,
        ),
        # 表 2：APPROVED 无证据 → 建议（已批准、待执行）
        (
            "APPROVED",
            ExecutionEvidence(step_prepared=False, step_observed_outcome=None),
            STATE_SUGGESTED,
            "execute_or_revoke",
            False,
        ),
        # 表 3：CONSUMED + success → 已执行
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome="success"),
            STATE_EXECUTED,
            "verify",
            False,
        ),
        # 表 4：CONSUMED + timeout / unknown_outcome → 结果未知
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome="timeout"),
            STATE_UNKNOWN_OUTCOME,
            "reconcile_manually",
            False,
        ),
        (
            "CONSUMED",
            ExecutionEvidence(
                step_prepared=True, step_observed_outcome="unknown_outcome"
            ),
            STATE_UNKNOWN_OUTCOME,
            "reconcile_manually",
            False,
        ),
        # 表 5：CONSUMED 无回执 → 结果未知（已执行未回执）
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome=None),
            STATE_UNKNOWN_OUTCOME,
            "reconcile_manually",
            False,
        ),
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=False, step_observed_outcome=None),
            STATE_UNKNOWN_OUTCOME,
            "reconcile_manually",
            False,
        ),
        # 表 6：CONSUMED + 否定类结果 → 执行失败（不得塞进"结果未知"）
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome="denied"),
            STATE_FAILED,
            "verify",
            False,
        ),
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome="error"),
            STATE_FAILED,
            "verify",
            False,
        ),
        (
            "CONSUMED",
            ExecutionEvidence(step_prepared=True, step_observed_outcome="partial"),
            STATE_FAILED,
            "verify",
            False,
        ),
        (
            "CONSUMED",
            ExecutionEvidence(
                step_prepared=True, step_observed_outcome="not_supported"
            ),
            STATE_FAILED,
            "verify",
            False,
        ),
        # 表 7：终态 → 已关闭（未执行）
        (
            "REJECTED",
            ExecutionEvidence(step_prepared=False, step_observed_outcome=None),
            STATE_CLOSED,
            "none",
            False,
        ),
        ("EXPIRED", None, STATE_CLOSED, "none", False),
        ("REVOKED", None, STATE_CLOSED, "none", False),
    ],
)
def test_derivation_table_rows(
    status: str,
    evidence: ExecutionEvidence | None,
    expected_state: str,
    expected_action: str,
    expected_degraded: bool,
) -> None:
    state = derive_state(approval_status=status, evidence=evidence)
    assert state["state"] == expected_state
    assert state["label"] == STATE_LABELS[expected_state]
    assert state["actionable"] == expected_action
    assert state["actionable"] == STATE_ACTIONS[expected_state]
    assert state["degraded"] is expected_degraded
    assert state["approval_status"] == status
    assert state["derived_from"] == [
        "approvals.status",
        "step_prepared.approval_id",
        "step_observed.outcome",
    ]


def test_consumed_without_evidence_port_is_degraded_unknown_outcome() -> None:
    """证据端口未装配：`CONSUMED` 一律判"结果未知"并标 degraded（绝不谎称已执行）。"""
    state = derive_state(approval_status="CONSUMED", evidence=None)
    assert state["state"] == STATE_UNKNOWN_OUTCOME
    assert state["degraded"] is True
    assert state["reason"] == "EVIDENCE_PORT_NOT_CONFIGURED"
    assert state["execution_evidence"] == {
        "step_prepared": False,
        "step_observed_outcome": None,
    }


def test_prepared_before_consumption_is_degraded_unknown_outcome() -> None:
    """APPROVED 却有 prepared 行（按设计不可能）：不谎称"建议"，交人工核对。"""
    state = derive_state(
        approval_status="APPROVED",
        evidence=ExecutionEvidence(step_prepared=True),
    )
    assert state["state"] == STATE_UNKNOWN_OUTCOME
    assert state["degraded"] is True
    assert state["reason"] == "PREPARED_BEFORE_CONSUMPTION"


def test_unknown_status_never_silently_maps_to_a_four_state() -> None:
    state = derive_state(approval_status="WEIRD_STATUS", evidence=None)
    assert state["state"] == STATE_UNKNOWN_OUTCOME
    assert state["degraded"] is True
    assert state["reason"] == "APPROVAL_STATUS_UNRECOGNIZED"


def test_four_states_are_present_and_labels_are_distinct() -> None:
    """PRD 四态必须在实现里真实存在，且每个状态都有唯一中文标签。"""
    assert _FOUR_STATES <= set(STATE_LABELS)
    assert len(set(STATE_LABELS.values())) == len(STATE_LABELS)
    # 失败与关闭不得与"结果未知"共用标签（PRD：不得合并表述）
    assert STATE_LABELS[STATE_FAILED] != STATE_LABELS[STATE_UNKNOWN_OUTCOME]
    assert STATE_LABELS[STATE_CLOSED] != STATE_LABELS[STATE_UNKNOWN_OUTCOME]


def test_terminal_states_have_no_next_action() -> None:
    assert STATE_CLOSED in TERMINAL_STATES
    assert STATE_EXECUTED in TERMINAL_STATES
    assert STATE_FAILED in TERMINAL_STATES
    assert STATE_UNKNOWN_OUTCOME not in TERMINAL_STATES, "结果未知必须保持可核对"
    assert STATE_AWAITING_APPROVAL not in TERMINAL_STATES
    assert STATE_SUGGESTED not in TERMINAL_STATES


def test_evidence_from_mapping_round_trip() -> None:
    assert evidence_from_mapping(None) is None
    evidence = evidence_from_mapping(
        {"step_prepared": True, "step_observed_outcome": "success"}
    )
    assert evidence == ExecutionEvidence(
        step_prepared=True, step_observed_outcome="success"
    )
    empty = evidence_from_mapping({})
    assert empty == ExecutionEvidence(
        step_prepared=False, step_observed_outcome=None
    )
