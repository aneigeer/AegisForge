"""P3-T2：Hybrid 检索（结构化硬过滤 + 词法打分），只吐出"可被诊断引用"的知识。

为什么不是向量检索：本环境**没有** embedding 传输（`model_role_models` 的
`embedding` 角色在注册期即被拒绝），凭空造一个"语义相似度"就是伪能力（INV-06）。
因此这里实现的是**可解释的混合检索**：

1. **结构化先筛**（交给 `MemoryConflictResolver`）：租户 / 引擎 / 版本 / Adapter /
   能力 / 环境指纹 / 风险 / 可索引 / 有效期，任一不符即出局；冲突条目同样出局；
2. **词法再排**：把查询 `text` 与条目 `content` 展开成 token 集合，按重叠率打分
   （ASCII 词 + 中文 2-gram），同分按 `validation_level` → `revision` → `knowledge_id`
   确定排序，**不用随机、不用时钟**；
3. **防守复核**：即使上游过滤有漏，也再次校验租户/引擎/状态/两级确认，
   不满足直接以 `RETRIEVER_GUARD_*` 出局（纵深防御，不依赖单一层）。

检索结果**只是知识引用**：调用方必须把它渲染为"参考知识（不可执行）"，
不得当作工具输出或授权依据（INV-07）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.memory.models import (
    VALIDATION_LEVEL_RANK,
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryQuery,
    ValidationLevel,
)
from core.memory.resolver import MemoryConflictResolver

_ASCII_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
#: 停用词极少：只去掉明显无信息量的英文词，避免把查询打成碎片。
_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "that", "this", "is", "of"})


def tokenize(text: str) -> frozenset[str]:
    """确定性分词：ASCII 词（≥2 字符）+ 中文 2-gram。"""
    lowered = str(text or "").lower()
    tokens = {token for token in _ASCII_RE.findall(lowered) if token not in _STOPWORDS}
    cjk = _CJK_RE.findall(lowered)
    tokens.update(
        "".join(cjk[index : index + 2]) for index in range(len(cjk) - 1)
    )
    return frozenset(tokens)


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(item) for item in value)
    return str(value)


@dataclass(frozen=True)
class RetrievalHit:
    """一条可引用的知识（附带打分明细，便于解释"为什么是它"）。"""

    knowledge_id: str
    revision: int
    kind: str
    validation_level: str
    db_engine: str | None
    content: dict[str, Any]
    score: float
    matched_terms: tuple[str, ...] = ()

    def as_citation(self) -> dict[str, Any]:
        """渲染给上下文/报告的最小引用形状（**不含** registry/gateway/DSN）。"""
        return {
            "knowledge_id": self.knowledge_id,
            "revision": self.revision,
            "kind": self.kind,
            "validation_level": self.validation_level,
            "db_engine": self.db_engine,
            "score": round(self.score, 4),
            "content": self.content,
        }


@dataclass(frozen=True)
class RetrievalOutcome:
    hits: tuple[RetrievalHit, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()
    considered: int = 0


@dataclass(frozen=True)
class HybridRetriever:
    """结构化过滤 + 词法打分；无随机、无系统时钟（`now` 由调用方注入）。"""

    max_hits: int = 5
    min_score: float = 0.0
    resolver: MemoryConflictResolver = field(default_factory=MemoryConflictResolver)

    def __post_init__(self) -> None:
        if self.max_hits < 1:
            raise ValueError("max_hits 必须 >= 1")

    def search(
        self,
        *,
        query: MemoryQuery,
        entries: Sequence[KnowledgeEntry],
        now: datetime | None = None,
    ) -> RetrievalOutcome:
        resolution = self.resolver.resolve(query=query, entries=list(entries), now=now)
        excluded: list[tuple[str, str]] = list(resolution.excluded)

        query_terms = tokenize(query.text)
        scored: list[RetrievalHit] = []
        for entry in resolution.available:
            guard = self._guard(entry, query)
            if guard is not None:
                excluded.append((entry.knowledge_id, guard))
                continue
            doc_terms = tokenize(_flatten(entry.content))
            matched = tuple(sorted(query_terms & doc_terms))
            score = (len(matched) / len(query_terms)) if query_terms else 0.0
            if query_terms and score < self.min_score:
                excluded.append((entry.knowledge_id, "BELOW_MIN_SCORE"))
                continue
            scored.append(
                RetrievalHit(
                    knowledge_id=entry.knowledge_id,
                    revision=entry.revision,
                    kind=entry.kind.value,
                    validation_level=entry.validation_level.value,
                    db_engine=entry.db_engine,
                    content=dict(entry.content),
                    score=score,
                    matched_terms=matched,
                )
            )

        scored.sort(key=self._sort_key)
        return RetrievalOutcome(
            hits=tuple(scored[: self.max_hits]),
            excluded=tuple(excluded),
            considered=len(entries),
        )

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _sort_key(hit: RetrievalHit) -> tuple[Any, ...]:
        """分数优先；同分按可信度、revision、id 确定性排序（升序 id）。"""
        return (
            -round(hit.score, 6),
            -VALIDATION_LEVEL_RANK[ValidationLevel(hit.validation_level)],
            -hit.revision,
            hit.knowledge_id,
        )

    @staticmethod
    def _guard(entry: KnowledgeEntry, query: MemoryQuery) -> str | None:
        """防守复核：上游若漏放，这里以 `RETRIEVER_GUARD_*` 出局。"""
        if entry.tenant_id != query.tenant_id:
            return "RETRIEVER_GUARD_TENANT"
        if entry.db_engine != query.db_engine:
            return "RETRIEVER_GUARD_ENGINE"
        if entry.status is not KnowledgeStatus.INDEXED:
            return "RETRIEVER_GUARD_STATUS"
        if not entry.is_multi_confirmed:
            return "RETRIEVER_GUARD_UNCONFIRMED"
        return None


__all__ = ["HybridRetriever", "RetrievalHit", "RetrievalOutcome", "tokenize"]
