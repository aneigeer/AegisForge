"""P3-T2：把"已多级确认的 INDEXED 知识"接进诊断上下文的唯一装配点。

两条铁律：

1. **只读引用**：知识以 `EvidenceItem(tool_name="memory_knowledge")` 形式进入上下文，
   它不是工具输出、不携带可用凭据、也不能成为授权依据（INV-07）；
2. **准入只有一条路**：必须经 `HybridRetriever`（结构化过滤 + 词法打分），
   调用方不得绕过它直接拿 `KnowledgeRepository` 的行。

本模块只做"查询 → 引用"的组装，不做持久化、不做状态迁移。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from core.context.contracts import EvidenceItem
from core.logging import redact_text
from core.memory.models import MemoryQuery
from core.memory.retrieval import HybridRetriever, RetrievalHit

#: 上下文里给知识的固定前缀：明确"这是参考知识，不是实时观测"。
KNOWLEDGE_PREFIX = "参考知识（已验证、只读、不可执行）"
#: 单条知识摘要上限，避免挤占 prompt 预算。
MAX_SUMMARY_CHARS = 600


def render_knowledge_summary(hit: RetrievalHit | Mapping[str, Any]) -> str:
    """把一条命中渲染成可脱敏的摘要文本（内容先脱敏再截断）。"""
    if isinstance(hit, RetrievalHit):
        payload = hit.as_citation()
    else:
        payload = dict(hit)
    body = (
        f"{payload.get('kind', 'SEMANTIC')} / {payload.get('validation_level', 'UNKNOWN')}"
        f" / {payload.get('db_engine', 'unknown')} / score={payload.get('score', 0)}"
    )
    content = redact_text(str(payload.get("content", "")))
    text = f"{KNOWLEDGE_PREFIX}｜{body}｜{content}"
    return text[:MAX_SUMMARY_CHARS]


def knowledge_evidence(
    citations: Sequence[RetrievalHit | Mapping[str, Any]],
    *,
    database_id: str | None = None,
) -> list[EvidenceItem]:
    """把命中转成 `EvidenceItem` 列表（id 稳定，便于审计回溯到 knowledge revision）。"""
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for hit in citations:
        payload = hit.as_citation() if isinstance(hit, RetrievalHit) else dict(hit)
        knowledge_id = str(payload.get("knowledge_id") or "")
        if not knowledge_id:
            continue
        # 稳定 ID：`<knowledge_id>@r<revision>`，便于从证据反查具体 revision。
        evidence_id = f"{knowledge_id}@r{payload.get('revision', 0)}"
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        items.append(
            EvidenceItem(
                evidence_id=evidence_id,
                tool_name="memory_knowledge",
                status="REFERENCE",
                summary=render_knowledge_summary(payload),
                warnings=["知识引用不可执行，仅用于推理"],
                database_id=database_id,
            )
        )
    return items


def build_knowledge_loader(
    entries_by_tenant: Callable[[str], Sequence[Any]],
    *,
    retriever: HybridRetriever | None = None,
    max_hits: int = 3,
    engine_of: Callable[[Any], tuple[str, str] | None] | None = None,
) -> Callable[[Any], list[EvidenceItem]]:
    """构造 `AgentLoop` 用的知识加载器：`task -> [EvidenceItem]`。

    - 查询条件全部来自**服务端事实**（task.tenant_id / 环境引擎与版本）；
    - 查询文本取 `task.goal`，用于词法打分；
    - 任何异常都不抛出：知识是**增益**，不能因为它让诊断失败。
    """
    engine = retriever or HybridRetriever(max_hits=max_hits)

    def load(task: Any) -> list[EvidenceItem]:
        try:
            # 引擎/版本必须由服务端事实给出；拿不到就**不注入知识**，
            # 绝不按"默认 mysql"猜测（否则可能把 MySQL 知识喂给 PG 诊断）。
            resolved = (
                engine_of(task)
                if engine_of is not None
                else _engine_from_task(task)
            )
            if resolved is None:
                return []
            db_engine, db_version = resolved
            entries = list(entries_by_tenant(str(task.tenant_id)))
            query = MemoryQuery(
                tenant_id=str(task.tenant_id),
                db_engine=db_engine,
                db_version=db_version,
                text=str(getattr(task, "goal", "") or ""),
            )
            outcome = engine.search(query=query, entries=entries)
            return knowledge_evidence(
                outcome.hits, database_id=getattr(task, "environment_id", None)
            )
        except Exception:  # noqa: BLE001 - 知识加载失败不得阻断诊断
            return []

    return load


def _engine_from_task(task: Any) -> tuple[str, str] | None:
    engine = getattr(task, "db_engine", None)
    version = getattr(task, "db_version", None)
    if not engine or not version:
        return None
    return str(engine), str(version)


__all__ = [
    "KNOWLEDGE_PREFIX",
    "MAX_SUMMARY_CHARS",
    "build_knowledge_loader",
    "knowledge_evidence",
    "render_knowledge_summary",
]
