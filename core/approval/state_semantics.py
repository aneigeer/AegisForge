"""W07 D4：审批/执行的**状态语义**（纯函数 + 证据输入）。

PRD 强制四态：`AWAITING_APPROVAL`（等待审批）/ `SUGGESTED`（建议、已批准待执行）/
`EXECUTED`（已执行）/ `UNKNOWN_OUTCOME`（结果未知）。本模块在此基础上把
"执行失败"与"拒绝/过期/撤销"拆出来（`FAILED` / `CLOSED`），因为把它们塞进
"结果未知"等于谎报——PRD 明令"不得合并表述"。

判定是完全确定性的：只依赖 `approvals.status` 与执行证据
（`step_prepared` 是否存在、`step_observed.outcome`）。证据端口未装配时
**一律把 `CONSUMED` 判为 `UNKNOWN_OUTCOME` 且 `degraded=true`**：
宁可让 SRE 去核对，也不谎称已执行。

为什么 `SUGGESTED` 落在"已批准未执行"而不是 PENDING：接口按 `approval_id` 取详情，
`PENDING` 必然是"等待审批"；"建议"只能落在已批准、尚未执行的窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# 四态（PRD §2-D4 强制项）
STATE_AWAITING_APPROVAL = "AWAITING_APPROVAL"
STATE_SUGGESTED = "SUGGESTED"
STATE_EXECUTED = "EXECUTED"
STATE_UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
# 精确化拆出的两态（不合并进"结果未知"）
STATE_FAILED = "FAILED"
STATE_CLOSED = "CLOSED"

STATE_LABELS: dict[str, str] = {
    STATE_AWAITING_APPROVAL: "等待审批",
    STATE_SUGGESTED: "建议（已批准、待执行）",
    STATE_EXECUTED: "已执行",
    STATE_UNKNOWN_OUTCOME: "结果未知",
    STATE_FAILED: "执行失败",
    STATE_CLOSED: "已关闭（未执行）",
}

STATE_ACTIONS: dict[str, str] = {
    STATE_AWAITING_APPROVAL: "approve_or_reject",
    STATE_SUGGESTED: "execute_or_revoke",
    STATE_EXECUTED: "verify",
    STATE_UNKNOWN_OUTCOME: "reconcile_manually",
    STATE_FAILED: "verify",
    STATE_CLOSED: "none",
}

# 终态：不再有"下一步动作"的状态（`UNKNOWN_OUTCOME` 刻意不算终态，必须人工核对）。
TERMINAL_STATES: frozenset[str] = frozenset(
    {STATE_EXECUTED, STATE_FAILED, STATE_CLOSED}
)

# `step_observed.outcome` 的分组（取值来自 `ToolStatus`）。
_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"success"})
_UNKNOWN_OUTCOMES: frozenset[str] = frozenset({"timeout", "unknown_outcome"})
_FAILED_OUTCOMES: frozenset[str] = frozenset(
    {"denied", "error", "partial", "not_supported"}
)

_CLOSED_STATUSES: frozenset[str] = frozenset({"REJECTED", "EXPIRED", "REVOKED"})

DERIVED_FROM: tuple[str, ...] = (
    "approvals.status",
    "step_prepared.approval_id",
    "step_observed.outcome",
)


@dataclass(frozen=True)
class ExecutionEvidence:
    """一次审批的执行证据（由 `StepRecordRepository.evidence_for_approvals()` 汇总）。"""

    step_prepared: bool = False
    step_observed_outcome: str | None = None


def _result(
    state: str,
    *,
    approval_status: str,
    evidence: ExecutionEvidence | None,
    degraded: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": state,
        "label": STATE_LABELS[state],
        "approval_status": approval_status,
        "execution_evidence": {
            "step_prepared": bool(evidence.step_prepared) if evidence else False,
            "step_observed_outcome": (
                evidence.step_observed_outcome if evidence else None
            ),
        },
        "derived_from": list(DERIVED_FROM),
        "terminal": state in TERMINAL_STATES,
        "actionable": STATE_ACTIONS[state],
        "degraded": degraded,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def derive_state(
    *, approval_status: str, evidence: ExecutionEvidence | None
) -> dict[str, Any]:
    """按 §6.2 推导表求解状态（互斥且完备）。"""
    status = str(approval_status or "").strip().upper()

    if status == "PENDING":
        # 等待审批与执行证据无关：尚未批准，不可能有合法的 prepared 行。
        return _result(
            STATE_AWAITING_APPROVAL,
            approval_status=status,
            evidence=evidence,
            degraded=False,
        )

    if status == "APPROVED":
        if evidence is not None and evidence.step_prepared:
            # 按设计不可能出现（prepare 只发生在 consume 之后）：不得谎称"建议"。
            return _result(
                STATE_UNKNOWN_OUTCOME,
                approval_status=status,
                evidence=evidence,
                degraded=True,
                reason="PREPARED_BEFORE_CONSUMPTION",
            )
        return _result(
            STATE_SUGGESTED,
            approval_status=status,
            evidence=evidence,
            degraded=False,
        )

    if status in _CLOSED_STATUSES:
        return _result(
            STATE_CLOSED, approval_status=status, evidence=evidence, degraded=False
        )

    if status == "CONSUMED":
        if evidence is None:
            # 证据端口未装配：宁可让 SRE 去核对，也不谎称已执行。
            return _result(
                STATE_UNKNOWN_OUTCOME,
                approval_status=status,
                evidence=None,
                degraded=True,
                reason="EVIDENCE_PORT_NOT_CONFIGURED",
            )
        outcome = evidence.step_observed_outcome
        if outcome in _SUCCESS_OUTCOMES:
            return _result(
                STATE_EXECUTED, approval_status=status, evidence=evidence, degraded=False
            )
        if outcome in _FAILED_OUTCOMES:
            return _result(
                STATE_FAILED, approval_status=status, evidence=evidence, degraded=False
            )
        if outcome in _UNKNOWN_OUTCOMES:
            return _result(
                STATE_UNKNOWN_OUTCOME,
                approval_status=status,
                evidence=evidence,
                degraded=False,
            )
        # 无回执（结果未知）或未归类的 outcome：一律"结果未知"，禁止猜成功/失败。
        return _result(
            STATE_UNKNOWN_OUTCOME,
            approval_status=status,
            evidence=evidence,
            degraded=False,
            reason="OBSERVED_MISSING" if outcome is None else "OUTCOME_UNCLASSIFIED",
        )

    # 状态出现未知取值（数据损坏/未来状态）：不得静默映射到四态之一。
    return _result(
        STATE_UNKNOWN_OUTCOME,
        approval_status=status or "UNKNOWN",
        evidence=evidence,
        degraded=True,
        reason="APPROVAL_STATUS_UNRECOGNIZED",
    )


def evidence_from_mapping(value: Mapping[str, Any] | None) -> ExecutionEvidence | None:
    """把仓储返回的字典转成 `ExecutionEvidence`；`None` 表示"证据不可得"。"""
    if value is None:
        return None
    outcome = value.get("step_observed_outcome")
    return ExecutionEvidence(
        step_prepared=bool(value.get("step_prepared")),
        step_observed_outcome=str(outcome) if outcome is not None else None,
    )


__all__ = [
    "DERIVED_FROM",
    "ExecutionEvidence",
    "STATE_ACTIONS",
    "STATE_AWAITING_APPROVAL",
    "STATE_CLOSED",
    "STATE_EXECUTED",
    "STATE_FAILED",
    "STATE_LABELS",
    "STATE_SUGGESTED",
    "STATE_UNKNOWN_OUTCOME",
    "TERMINAL_STATES",
    "derive_state",
    "evidence_from_mapping",
]
