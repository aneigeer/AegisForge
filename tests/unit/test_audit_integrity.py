"""事件链完整性：通过、篡改定位、删除定位（P0-13/T6，ADR-0006 §3）。

链条由真实 `event_hash()` 构造，因此"校验实现"与"写入实现"必须完全一致——
任何一侧单独改动都会让这些用例失败。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.audit.integrity import (
    REASON_EVENT_HASH_MISMATCH,
    REASON_EVENT_HASH_MISSING,
    REASON_PREVIOUS_HASH_MISMATCH,
    REASON_SEQ_GAP,
    REASON_TASK_ID_MISMATCH,
    ChainVerification,
    verify_event_chain,
)
from core.tasks.models import TaskEvent
from core.tasks.repository import event_hash

BASE_TIME = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def build_chain(count: int, *, task_id: str = "task_chain") -> list[TaskEvent]:
    """按 `insert_event` 的同一套规则构造一条自洽的链（seq 0..count-1）。"""
    events: list[TaskEvent] = []
    previous: str | None = None
    for seq in range(count):
        created_at = BASE_TIME + timedelta(seconds=seq)
        payload = {"seq": seq, "note": f"事件 {seq}"}
        digest = event_hash(
            task_id=task_id,
            run_id="run_chain",
            tool_call_id=None,
            event_type="TOOL_CALL_STARTED",
            payload=payload,
            created_at=created_at,
            seq=seq,
            previous_event_hash=previous,
        )
        events.append(
            TaskEvent(
                event_id=f"evt_{seq:04d}",
                task_id=task_id,
                run_id="run_chain",
                event_type="TOOL_CALL_STARTED",
                payload=payload,
                created_at=created_at,
                seq=seq,
                previous_event_hash=previous,
                event_hash=digest,
            )
        )
        previous = digest
    return events


def test_intact_chain_verifies() -> None:
    result = verify_event_chain(build_chain(5))
    assert result.ok is True
    assert result.checked == 5
    assert result.broken_at_seq is None
    assert result.affected_range is None
    assert result.reason is None


def test_empty_chain_is_not_a_failure() -> None:
    result = verify_event_chain([])
    assert result.ok is True
    assert result.checked == 0


def test_single_event_chain_verifies() -> None:
    result = verify_event_chain(build_chain(1))
    assert result.ok is True
    assert result.checked == 1


def test_tampered_payload_reports_precise_seq() -> None:
    events = build_chain(5)
    tampered = events[2].model_copy(update={"payload": {"seq": 2, "note": "被改写的内容"}})
    events[2] = tampered

    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 2
    assert result.checked == 2
    assert result.affected_range == (1, 4)
    assert result.reason == REASON_EVENT_HASH_MISMATCH


def test_tampered_event_type_reports_precise_seq() -> None:
    events = build_chain(4)
    events[1] = events[1].model_copy(update={"event_type": "TOOL_CALL_SUCCEEDED"})
    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 1
    assert result.reason == REASON_EVENT_HASH_MISMATCH


def test_tampered_created_at_reports_precise_seq() -> None:
    events = build_chain(4)
    events[3] = events[3].model_copy(update={"created_at": BASE_TIME})
    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 3
    assert result.reason == REASON_EVENT_HASH_MISMATCH


def test_deleted_middle_event_reports_gap_and_affected_range() -> None:
    events = build_chain(5)
    del events[2]

    result = verify_event_chain(events)
    assert result.ok is False
    assert result.reason == REASON_SEQ_GAP
    # 空洞位置 = 缺失的 seq（2）；受影响区间 = (断裂前最后有效 seq, 链尾 seq)
    assert result.broken_at_seq == 2
    assert result.affected_range == (1, 4)


def test_deleted_tail_is_detected_by_missing_seq() -> None:
    """只删链尾无法自证——这正是"事后可发现而非事前防篡改"的已知限制。"""
    events = build_chain(4)
    truncated = events[:-1]
    # 删掉链尾后剩余部分是自洽的：校验通过（ADR-0006 已知限制 6），
    # 因此必须靠外部锚定（ArchiveRef）发现"链变短了"。
    result = verify_event_chain(truncated)
    assert result.ok is True
    assert result.checked == 3


def test_rewritten_previous_hash_is_detected() -> None:
    events = build_chain(4)
    events[2] = events[2].model_copy(update={"previous_event_hash": "0" * 64})
    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 2
    assert result.reason == REASON_PREVIOUS_HASH_MISMATCH


def test_missing_event_hash_is_detected() -> None:
    events = build_chain(3)
    events[1] = events[1].model_copy(update={"event_hash": None})
    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 1
    assert result.checked == 1
    assert result.reason == REASON_EVENT_HASH_MISSING


def test_mixed_task_ids_are_rejected() -> None:
    first = build_chain(2)
    second = build_chain(2, task_id="task_other")
    result = verify_event_chain(first + second)
    assert result.ok is False
    assert result.reason == REASON_TASK_ID_MISMATCH


def test_unsorted_input_is_normalized_before_verification() -> None:
    events = build_chain(4)
    shuffled = [events[2], events[0], events[3], events[1]]
    result = verify_event_chain(shuffled)
    assert result.ok is True
    assert result.checked == 4


def test_first_event_with_non_null_previous_hash_is_detected() -> None:
    events = build_chain(3)
    events[0] = events[0].model_copy(update={"previous_event_hash": "1" * 64})
    result = verify_event_chain(events)
    assert result.ok is False
    assert result.broken_at_seq == 0
    assert result.reason == REASON_PREVIOUS_HASH_MISMATCH


def test_verification_is_json_friendly() -> None:
    """API 与 CLI 必须能直接输出同一结构（不得只返回"失败"两个字）。"""
    result = verify_event_chain(build_chain(2))
    payload = result.to_public_dict()
    assert payload == {
        "ok": True,
        "checked": 2,
        "broken_at_seq": None,
        "affected_range": None,
        "reason": None,
    }
    broken = ChainVerification(
        ok=False, checked=1, broken_at_seq=1, affected_range=(0, 3), reason=REASON_SEQ_GAP
    ).to_public_dict()
    assert broken["affected_range"] == [0, 3]


@pytest.mark.parametrize("count", [1, 2, 3, 7])
def test_checked_count_matches_event_count(count: int) -> None:
    result = verify_event_chain(build_chain(count))
    assert result.checked == count
