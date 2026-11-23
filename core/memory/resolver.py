"""W09 T04 记忆冲突解析器：8 道硬过滤（先过滤）→ 确定比较（后比较）。

设计依据：``docs/w09/02-design.md`` §5.4、``docs/w09/03-task-cards.md:463-501``。

- **先硬过滤、短路**：引擎 → 版本 → Adapter → 能力 → 环境 → 风险 → 可索引 → 有效期，
  过滤原因逐一可枚举；``postgresql`` 知识在 ``mysql`` 任务上**结构上**不可能命中
  （没有相似度打分，不使用 ``difflib``）。
- **后比较**：只在通过 8 道过滤的候选间按 ``validation_level`` → ``len(evidence_ids)``
  → ``effective_from`` → ``knowledge_id`` 字典序确定排序。
- 前两名 ``content_digest``（``canonical_digest(content)``）不同 ⇒ 两者同置
  ``CONFLICTED``、从 ``available`` 剔除、进 ``conflicted``；**不静默择一**。
- ``resolve`` **只判定、不写库**；冲突状态由 ``MemoryService`` 追加新 revision 表达。
- 解析器不读系统时钟：调用方注入 ``now`` 时全程使用注入值（可复现）。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict

from core.memory.models import (
    VALIDATION_LEVEL_RANK,
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    MemoryQuery,
    _iso_utc,
    _parse_iso,
)
from core.security.canonical import canonical_digest
from core.tasks.models import utc_now

_VERSION_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?")


class FilterReason(str, Enum):
    """硬过滤原因（与设计 §5.4 表一一对应）。"""

    ENGINE_MISMATCH = "ENGINE_MISMATCH"
    VERSION_PARSE_ERROR = "VERSION_PARSE_ERROR"
    VERSION_OUT_OF_RANGE = "VERSION_OUT_OF_RANGE"
    ADAPTER_MISMATCH = "ADAPTER_MISMATCH"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    RISK_NOT_IN_SCOPE = "RISK_NOT_IN_SCOPE"
    NOT_INDEXABLE = "NOT_INDEXABLE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class MemoryResolution(BaseModel):
    """解析结果：可用集 / 冲突组 / 排除清单（``(knowledge_id, reason)``）。"""

    model_config = ConfigDict(extra="forbid")

    available: tuple[KnowledgeEntry, ...]
    conflicted: tuple[KnowledgeEntry, ...]
    excluded: tuple[tuple[str, str], ...]


def _parse_version(value: str) -> tuple[int, int] | None:
    """解析 ``8`` / ``8.0`` / ``8.0.46`` / ``15.4``；失败返回 ``None``。"""
    match = _VERSION_RE.match(str(value))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def _version_reason(entry: KnowledgeEntry, query: MemoryQuery) -> str | None:
    query_version = _parse_version(query.db_version)
    if query_version is None:
        return FilterReason.VERSION_PARSE_ERROR.value
    minimum: tuple[int, int] | None = None
    maximum: tuple[int, int] | None = None
    if entry.version_min:
        minimum = _parse_version(entry.version_min)
        if minimum is None:
            return FilterReason.VERSION_PARSE_ERROR.value
    if entry.version_max:
        maximum = _parse_version(entry.version_max)
        if maximum is None:
            return FilterReason.VERSION_PARSE_ERROR.value
    if minimum is not None and query_version < minimum:
        return FilterReason.VERSION_OUT_OF_RANGE.value
    if maximum is not None and query_version > maximum:
        return FilterReason.VERSION_OUT_OF_RANGE.value
    return None


def _within_window(entry: KnowledgeEntry, moment: datetime) -> bool:
    return (
        _iso_utc(entry.effective_from)
        <= _iso_utc(moment)
        <= _iso_utc(entry.effective_until)
    )


class MemoryConflictResolver:
    """确定性冲突解析器；不持有仓储、GateWay、DSN，也不写库。"""

    def __init__(self, *, max_candidates: int = 20) -> None:
        if max_candidates < 2:
            raise ValueError("max_candidates 必须 >= 2（冲突判定至少需要两条候选）")
        self._max_candidates = max_candidates

    def resolve(
        self,
        *,
        query: MemoryQuery,
        entries: Sequence[KnowledgeEntry],
        now: datetime | None = None,
    ) -> MemoryResolution:
        """先硬过滤，再在通过过滤的候选间做确定比较与冲突判定。"""
        # 时钟只在入口取一次；注入 now 时全程使用注入值，逐条过滤不再读系统时钟。
        moment = now if now is not None else utc_now()
        candidates = self._latest_per_knowledge(entries)

        excluded: list[tuple[str, str]] = []
        passed: list[KnowledgeEntry] = []
        for entry in candidates:
            reason = self._filter_reason(entry, query, moment)
            if reason is not None:
                excluded.append((entry.knowledge_id, reason))
                continue
            passed.append(entry)

        ranked = sorted(passed, key=self._sort_key)[: self._max_candidates]
        available = list(ranked)
        conflicted: list[KnowledgeEntry] = []
        if len(ranked) >= 2:
            first, second = ranked[0], ranked[1]
            if canonical_digest(first.content) != canonical_digest(second.content):
                conflicted = [
                    first.model_copy(update={"status": KnowledgeStatus.CONFLICTED}),
                    second.model_copy(update={"status": KnowledgeStatus.CONFLICTED}),
                ]
                available = ranked[2:]
        return MemoryResolution(
            available=tuple(available),
            conflicted=tuple(conflicted),
            excluded=tuple(excluded),
        )

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _latest_per_knowledge(
        entries: Sequence[KnowledgeEntry],
    ) -> list[KnowledgeEntry]:
        """同 ``knowledge_id`` 只保留最新 revision（撤销后旧版不得复活为可用）。"""
        latest: dict[str, KnowledgeEntry] = {}
        for entry in entries:
            current = latest.get(entry.knowledge_id)
            if current is None or entry.revision > current.revision:
                latest[entry.knowledge_id] = entry
        return sorted(latest.values(), key=lambda item: item.knowledge_id)

    @staticmethod
    def _sort_key(entry: KnowledgeEntry) -> Any:
        return (
            -VALIDATION_LEVEL_RANK[entry.validation_level],
            -len(entry.evidence_ids),
            -_parse_iso(entry.effective_from).timestamp(),
            entry.knowledge_id,
        )

    def _filter_reason(
        self, entry: KnowledgeEntry, query: MemoryQuery, moment: datetime
    ) -> str | None:
        # 1) 引擎：None 仅在 ENVIRONMENT 类豁免（其最终仍会因非 SEMANTIC 被排除）。
        if entry.db_engine is None:
            if entry.kind is not MemoryKind.ENVIRONMENT:
                return FilterReason.ENGINE_MISMATCH.value
        elif entry.db_engine != query.db_engine:
            return FilterReason.ENGINE_MISMATCH.value

        # 2) 主/次版本区间。
        version_reason = _version_reason(entry, query)
        if version_reason is not None:
            return version_reason

        # 3) Adapter：空 = 通配；非空必须精确相等。
        if entry.adapter_version:
            if not query.adapter_version or entry.adapter_version != query.adapter_version:
                return FilterReason.ADAPTER_MISMATCH.value

        # 4) 能力：空 = 通配；非空须被任务能力集覆盖。
        if entry.capability_scope:
            if not set(entry.capability_scope).issubset(set(query.capabilities)):
                return FilterReason.CAPABILITY_MISSING.value

        # 5) 环境：空 = 跨环境通用；非空必须相等。
        if entry.environment_fingerprint_digest:
            if (
                entry.environment_fingerprint_digest
                != query.environment_fingerprint_digest
            ):
                return FilterReason.ENVIRONMENT_MISMATCH.value

        # 6) 风险：空 = 仅 R0/R1。
        scope = set(entry.risk_scope) if entry.risk_scope else {"R0", "R1"}
        if query.risk_level not in scope:
            return FilterReason.RISK_NOT_IN_SCOPE.value

        # 7/8) 状态与有效期：以 indexable_at 为唯一判定，再按子条件给出原因。
        if not entry.indexable_at(moment):
            if entry.revoked_at is not None:
                return FilterReason.REVOKED.value
            if not _within_window(entry, moment):
                return FilterReason.EXPIRED.value
            return FilterReason.NOT_INDEXABLE.value
        return None


__all__ = [
    "FilterReason",
    "MemoryConflictResolver",
    "MemoryResolution",
]
