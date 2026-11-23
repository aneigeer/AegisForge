"""W09 T04 进程内工作态（``WORKING``）：生命周期 = 一次 ``AgentLoop.run``。

设计 §5.1：``WORKING`` 存于进程内、**不落库**、不可索引。对象不持
``tool_registry`` / ``gateway`` / DSN，也不导入任何数据库驱动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkingSnapshot:
    """工作态快照（只读、可序列化），供上下文构建或排障使用。"""

    hypotheses: tuple[dict[str, Any], ...] = ()
    todos: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class WorkingSet:
    """一次运行内的工作记忆；进程内、不持久化、不授权任何执行。"""

    def __init__(self) -> None:
        self._hypotheses: list[dict[str, Any]] = []
        self._todos: list[str] = []
        self._evidence_ids: list[str] = []

    def add_hypothesis(self, *, statement: str, confidence: str = "UNKNOWN") -> None:
        if not statement or not statement.strip():
            raise ValueError("假设陈述不能为空")
        self._hypotheses.append(
            {"statement": statement.strip(), "confidence": confidence}
        )

    def add_todo(self, text: str) -> None:
        if not text or not text.strip():
            raise ValueError("待办不能为空")
        self._todos.append(text.strip())

    def add_evidence(self, evidence_id: str) -> None:
        if not evidence_id or not evidence_id.strip():
            raise ValueError("evidence_id 不能为空")
        if evidence_id not in self._evidence_ids:
            self._evidence_ids.append(evidence_id)

    def snapshot(self) -> WorkingSnapshot:
        return WorkingSnapshot(
            hypotheses=tuple(dict(item) for item in self._hypotheses),
            todos=tuple(self._todos),
            evidence_ids=tuple(self._evidence_ids),
        )

    def clear(self) -> None:
        self._hypotheses.clear()
        self._todos.clear()
        self._evidence_ids.clear()


__all__ = ["WorkingSet", "WorkingSnapshot"]
