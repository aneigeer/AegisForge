"""审计链完整性校验：纯函数，返回"通过"或可定位的断裂位置（ADR-0006 §3）。

约束：
- 重算 `event_hash` 必须复用 `core/tasks/repository.py::event_hash`，不得另写一套；
- 不允许只返回"失败"两个字，必须给出 `broken_at_seq` 与 `affected_range`；
- 校验只读，不修改任何数据（审计表对运行账号没有 UPDATE/DELETE 授权）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.tasks.models import TaskEvent
from core.tasks.repository import event_hash as compute_event_hash

# 断裂原因（稳定字符串，供 API/CLI 与测试断言）
REASON_SEQ_GAP = "SEQ_GAP"
REASON_PREVIOUS_HASH_MISMATCH = "PREVIOUS_HASH_MISMATCH"
REASON_EVENT_HASH_MISMATCH = "EVENT_HASH_MISMATCH"
REASON_EVENT_HASH_MISSING = "EVENT_HASH_MISSING"
REASON_TASK_ID_MISMATCH = "TASK_ID_MISMATCH"


@dataclass(frozen=True)
class ChainVerification:
    """完整性校验结果。

    - `ok=True`：整条链自洽；`checked` 为校验过的条目数；
    - `ok=False`：`broken_at_seq` 指向断裂位置（若为空洞，取缺失的 seq），
      `affected_range` 为 `(断裂前最后有效 seq, 链尾 seq)`。
    """

    ok: bool
    checked: int
    broken_at_seq: int | None = None
    affected_range: tuple[int, int] | None = None
    reason: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "broken_at_seq": self.broken_at_seq,
            "affected_range": list(self.affected_range) if self.affected_range else None,
            "reason": self.reason,
        }


def _tail_seq(ordered: Sequence[TaskEvent]) -> int:
    return max(int(event.seq) for event in ordered)


def verify_event_chain(events: Sequence[TaskEvent]) -> ChainVerification:
    """按 `seq` 升序逐条重算哈希链，返回可定位的校验结论。"""
    if not events:
        # 空链不是完整性失败：没有任何记录可被篡改。
        return ChainVerification(ok=True, checked=0)

    ordered = sorted(events, key=lambda event: int(event.seq))
    tail = _tail_seq(ordered)
    task_ids = {event.task_id for event in ordered}
    if len(task_ids) > 1:
        # 链的边界是 task_id；混入其他任务的事件视为断裂。
        return ChainVerification(
            ok=False,
            checked=0,
            broken_at_seq=int(ordered[0].seq),
            affected_range=(int(ordered[0].seq), tail),
            reason=REASON_TASK_ID_MISMATCH,
        )

    checked = 0
    expected_seq = 0
    previous_hash: str | None = None
    for event in ordered:
        seq = int(event.seq)
        if seq != expected_seq:
            # seq 空洞：broken_at_seq 取缺失位置（预期 seq），便于人工定位。
            last_valid = expected_seq - 1
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_at_seq=expected_seq,
                affected_range=(last_valid, tail),
                reason=REASON_SEQ_GAP,
            )
        if event.previous_event_hash != previous_hash:
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_at_seq=seq,
                affected_range=(seq - 1, tail),
                reason=REASON_PREVIOUS_HASH_MISMATCH,
            )
        if not event.event_hash:
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_at_seq=seq,
                affected_range=(seq - 1, tail),
                reason=REASON_EVENT_HASH_MISSING,
            )
        recomputed = compute_event_hash(
            task_id=event.task_id,
            run_id=event.run_id,
            tool_call_id=event.tool_call_id,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
            seq=seq,
            previous_event_hash=event.previous_event_hash,
        )
        if recomputed != event.event_hash:
            # payload/type/时间被改写：本条重算不符，其后所有条目的链也不再被信任。
            return ChainVerification(
                ok=False,
                checked=checked,
                broken_at_seq=seq,
                affected_range=(seq - 1, tail),
                reason=REASON_EVENT_HASH_MISMATCH,
            )
        checked += 1
        expected_seq += 1
        previous_hash = event.event_hash

    return ChainVerification(ok=True, checked=checked)


__all__ = [
    "REASON_EVENT_HASH_MISMATCH",
    "REASON_EVENT_HASH_MISSING",
    "REASON_PREVIOUS_HASH_MISMATCH",
    "REASON_SEQ_GAP",
    "REASON_TASK_ID_MISMATCH",
    "ChainVerification",
    "verify_event_chain",
]
