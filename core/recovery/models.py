"""W08 恢复控制的契约模型（T05）。

模型一律 `extra="forbid"`，且**只承载结构与事实**：

- `RecoveryAction`：一次恢复判定的结论（`CONTINUE` / `STOP_LOOP` /
  `STOP_NO_PROGRESS` / `RETRY` / `STOP`）+ 稳定 `reason` + **恒非空**
  `explanation`（冻结口径：停止必须可解释，见设计 §5 注）；
- `Hypothesis`：可被反证关闭的假设；关闭用状态位而非删除，历史按版本追加；
- `RecoveryOutcome`：`UNKNOWN` 结果的收敛结论（`NEEDS_RECONCILIATION` /
  `MANUAL_INTERVENTION`），并显式携带 `tool_calls_delta` / `replayed` 作为
  「不重放写」的可断言证据。

模型层刻意不含 `tool_name` / `sql` / `registry` 等可触发执行的字段：
恢复器即便被误用，也无法从这些结构里拼出一次写操作。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.tasks.models import TaskStatus

# `RecoveryAction.action` 的封闭取值（AC-13/AC-14 的机器判定口径）。
RecoveryActionKind = Literal[
    "CONTINUE", "STOP_LOOP", "STOP_NO_PROGRESS", "RETRY", "STOP"
]
# 假设只有「开放」与「已被反证」两态；反证不删除、不改写历史。
HypothesisStatus = Literal["OPEN", "REFUTED"]

# `RecoveryOutcome.status` 只允许这两种收敛态：UNKNOWN 收敛不得落到 COMPLETED/FAILED。
CONVERGED_STATUSES = frozenset(
    {TaskStatus.NEEDS_RECONCILIATION, TaskStatus.MANUAL_INTERVENTION}
)


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{label} 不得为空")
    return value


def _require_explanation(value: str) -> str:
    return _require_text(value, "explanation")


class RecoveryAction(BaseModel):
    """一次恢复判定。

    `explanation` 是 AC-13 的「停止得可解释」证据：控制器每条返回都必须带
    非空字符串，测试逐条断言，不接受 `None` 或空串。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: RecoveryActionKind
    reason: str
    explanation: str
    call_key: str | None = None
    evidence_digest: str | None = None
    attempt: int | None = None
    repeats: int = 0
    retry_count: int = 0
    plan_version: int = 0

    @field_validator("reason")
    @classmethod
    def _reason_non_empty(cls, value: str) -> str:
        return _require_text(value, "reason")

    @field_validator("explanation")
    @classmethod
    def _explanation_non_empty(cls, value: str) -> str:
        return _require_explanation(value)


class Hypothesis(BaseModel):
    """可被反证关闭的假设（不可变；反证产生新版本，旧版本保留在历史里）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    statement: str
    status: HypothesisStatus = "OPEN"
    plan_version: int = 0
    reason: str | None = None
    created_at: datetime
    updated_at: datetime
    refuted_at: datetime | None = None

    @field_validator("hypothesis_id", "statement")
    @classmethod
    def _identity_non_empty(cls, value: str) -> str:
        return _require_text(value, "identity")

    @field_validator("reason")
    @classmethod
    def _optional_reason_non_empty(cls, value: str | None) -> str | None:
        return None if value is None else _require_text(value, "reason")

    @model_validator(mode="after")
    def _refuted_is_consistent(self) -> "Hypothesis":
        if self.status == "REFUTED" and not self.reason:
            raise ValueError("REFUTED 必须带反证 reason")
        return self


class RecoveryOutcome(BaseModel):
    """`UNKNOWN` 结果的收敛结论。

    - `status` 只能是 `NEEDS_RECONCILIATION` 或 `MANUAL_INTERVENTION`；
    - `tool_calls_delta` 恒为 0、`replayed` 恒为 False（本模块无执行面，
      结构上无法重放写；AC-16 用真实库的 `COUNT(tool_calls)` 再证一次）；
    - `applied` 记录任务状态是否真的被推进（非法迁移不伪造状态变更）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskStatus
    reason: str
    explanation: str
    action_digest: str
    idempotency_key: str
    target_objects: tuple[str, ...] = ()
    evidence_sufficient: bool
    applied: bool
    tool_calls_delta: int = 0
    replayed: bool = False
    plan_version: int = 0
    occurred_at: datetime

    @field_validator("status")
    @classmethod
    def _status_converged(cls, value: TaskStatus) -> TaskStatus:
        if value not in CONVERGED_STATUSES:
            raise ValueError(
                "UNKNOWN 收敛只能落到 NEEDS_RECONCILIATION / MANUAL_INTERVENTION"
            )
        return value

    @field_validator("reason")
    @classmethod
    def _reason_non_empty(cls, value: str) -> str:
        return _require_text(value, "reason")

    @field_validator("explanation")
    @classmethod
    def _explanation_non_empty(cls, value: str) -> str:
        return _require_explanation(value)

    @field_validator("action_digest", "idempotency_key")
    @classmethod
    def _key_non_empty(cls, value: str) -> str:
        return _require_text(value, "key")

    @field_validator("target_objects", mode="before")
    @classmethod
    def _no_free_text_targets(cls, value: object) -> object:
        """拒绝把裸字符串当目标列表传入（防止误把 SQL 文本塞进恢复器）。"""
        if isinstance(value, (str, bytes, bytearray)):
            raise ValueError("target_objects 必须是序列，不得为字符串")
        return value

    @field_validator("tool_calls_delta")
    @classmethod
    def _zero_delta(cls, value: int) -> int:
        if value != 0:
            raise ValueError("恢复器不得产生工具调用增量（tool_calls_delta 必须为 0）")
        return value

    @field_validator("replayed")
    @classmethod
    def _never_replayed(cls, value: bool) -> bool:
        if value:
            raise ValueError("恢复器不得重放写操作")
        return value


def normalize_targets(target_objects: Sequence[object]) -> tuple[str, ...]:
    """把目标对象归一化成稳定文本元组（不做解析，更不构造 SQL）。"""
    normalized: list[str] = []
    for item in target_objects:
        normalized.append(item if isinstance(item, str) else repr(item))
    return tuple(normalized)


__all__ = [
    "CONVERGED_STATUSES",
    "Hypothesis",
    "HypothesisStatus",
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryOutcome",
    "normalize_targets",
]
