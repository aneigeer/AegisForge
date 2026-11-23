"""W09 T04 任务时间线（``EPISODIC``）：``task_events`` + ``evidence`` 的只读投影。

设计 §5.1：``EPISODIC`` 是**投影**，不可索引、不落 ``knowledge_entries``。
本模块只读两个既有端口，不写库、不改 ``core/evidence/**``、不导入数据库驱动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class EventPort(Protocol):
    """``TaskRepository.list_events`` 的只读契约。"""

    def list_events(
        self, task_id: str, limit: int = 200, *, after_seq: int = 0
    ) -> list[Any]: ...


class EvidencePort(Protocol):
    """``EvidenceRepository.list_by_task`` 的只读契约。"""

    def list_by_task(
        self,
        task_id: str,
        tenant_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Any], str | None]: ...


@dataclass(frozen=True)
class EpisodicItem:
    """时间线上的一条只读事实（事件或证据）。"""

    source: str
    ref_id: str
    task_id: str
    occurred_at: str
    summary: str


class EpisodicView:
    """把事件流与证据行合并成一条按时间排序的只读时间线。"""

    def __init__(self, *, events: EventPort, evidence: EvidencePort) -> None:
        self._events = events
        self._evidence = evidence

    def timeline(
        self, *, task_id: str, tenant_id: str, limit: int = 200
    ) -> tuple[EpisodicItem, ...]:
        """只读投影；调用方自带租户，跨租户证据由 ``list_by_task`` 过滤。"""
        safe_limit = max(1, min(int(limit), 500))
        items: list[EpisodicItem] = []
        for event in self._events.list_events(task_id, safe_limit):
            created = getattr(event, "created_at", None)
            items.append(
                EpisodicItem(
                    source="event",
                    ref_id=str(getattr(event, "event_id", "")),
                    task_id=str(getattr(event, "task_id", task_id)),
                    occurred_at=(
                        created.isoformat() if hasattr(created, "isoformat") else str(created)
                    ),
                    summary=str(getattr(event, "event_type", "UNKNOWN")),
                )
            )
        rows, _ = self._evidence.list_by_task(
            task_id, tenant_id, limit=safe_limit
        )
        for row in rows:
            items.append(
                EpisodicItem(
                    source="evidence",
                    ref_id=str(getattr(row, "evidence_id", "")),
                    task_id=str(getattr(row, "task_id", task_id)),
                    occurred_at=str(getattr(row, "observed_at", "")),
                    summary=(
                        f"{getattr(row, 'source_tool', 'UNKNOWN')}"
                        f"/{getattr(getattr(row, 'collection_status', None), 'value', 'UNKNOWN')}"
                    ),
                )
            )
        items.sort(key=lambda item: (item.occurred_at, item.source, item.ref_id))
        return tuple(items)


__all__ = ["EpisodicItem", "EpisodicView"]
