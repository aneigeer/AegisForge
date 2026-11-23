"""P3-T2：Wiki 编译器（把一次诊断沉淀为**待确认**的知识草稿）。

红线（对应 roadmap P3-T2 验收）：

1. 草稿**永不**自动晋升 `INDEXED`：落库状态恒为 `PROPOSED`，必须走 P3-T1 的两级人工确认；
2. 落库必须经 `MemoryService.record()`——它先过 `RedactionGate`（EVIDENCE 通道）再写，
   本模块**不提供**任何绕过闸门的写路径；
3. 草稿内容只引用**已采集证据的 id 与摘要**，不写入 DSN、连接串、凭据或工具句柄（INV-02/INV-07）。

编译是"确定性的整理"，不是"再调一次模型"：同样的输入产出同样的结构，
避免把模型幻觉固化成知识。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Sequence

from core.context.contracts import EvidenceItem
from core.memory.models import (
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
    content_digest,
)
from core.memory.service import MemoryService
from core.tasks.models import new_id, utc_now

#: 参与编译的记忆类型（诊断结论属于 SEMANTIC）。
DEFAULT_KIND = MemoryKind.SEMANTIC
#: 草稿有效期（默认 90 天；到期后由 expire_due 追加 EXPIRED revision）。
DEFAULT_TTL_DAYS = 90

_SECTIONS = ("symptom", "root_cause", "actions", "verification")


@dataclass(frozen=True)
class WikiDraft:
    """编译结果（尚未落库的纯数据结构，便于先审后写）。"""

    content: dict[str, Any]
    evidence_ids: tuple[str, ...]
    redaction_version: str

    def as_entry(
        self,
        *,
        tenant_id: str,
        db_engine: str,
        db_version: str,
        adapter_version: str | None = None,
        environment_fingerprint_digest: str | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
        knowledge_id: str | None = None,
    ) -> KnowledgeEntry:
        now = utc_now()
        return KnowledgeEntry(
            knowledge_id=knowledge_id or new_id("knw"),
            revision=1,
            tenant_id=tenant_id,
            kind=DEFAULT_KIND,
            # 红线 1：永远是 PROPOSED。
            status=KnowledgeStatus.PROPOSED,
            db_engine=db_engine,
            version_min=db_version,
            version_max=db_version,
            adapter_version=adapter_version,
            capability_scope=(),
            environment_fingerprint_digest=environment_fingerprint_digest,
            risk_scope=(),
            evidence_ids=self.evidence_ids,
            test_case_ids=(),
            validation_level=ValidationLevel.UNVERIFIED,
            reviewed_by=None,
            reviewed_at=None,
            effective_from=now.isoformat(),
            effective_until=(now + timedelta(days=ttl_days)).isoformat(),
            source_license="internal",
            content=dict(self.content),
            redaction_version=self.redaction_version,
            confirmations=(),
            content_digest=content_digest(self.content),
        )


def compile_draft(
    *,
    goal: str,
    root_cause: str | None,
    evidence: Sequence[EvidenceItem],
    redaction_version: str = "redaction-v2",
) -> WikiDraft:
    """把"目标 + 归因 + 证据"整理成结构化草稿（不落库、不脱敏、不判可信度）。"""
    evidence_ids = tuple(item.evidence_id for item in evidence)[:12]
    actions = [
        f"{item.tool_name}: {item.summary[:160]}"
        for item in evidence
        if item.tool_name != "memory_knowledge"
    ][:6]
    content: dict[str, Any] = {
        "symptom": str(goal or "").strip()[:400],
        "root_cause": (str(root_cause).strip()[:400] if root_cause else None),
        "actions": actions,
        "verification": {
            "evidence_count": len(evidence_ids),
            "has_root_cause": bool(root_cause),
        },
        "sections": list(_SECTIONS),
    }
    return WikiDraft(content=content, evidence_ids=evidence_ids, redaction_version=redaction_version)


class WikiCompiler:
    """草稿生成 + 落库（落库只能经 `MemoryService.record`，即必经脱敏闸门）。"""

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    def compile_and_store(
        self,
        *,
        tenant_id: str,
        goal: str,
        root_cause: str | None,
        evidence: Sequence[EvidenceItem],
        db_engine: str,
        db_version: str,
        adapter_version: str | None = None,
        environment_fingerprint_digest: str | None = None,
        principal: Any = None,
    ) -> KnowledgeEntry:
        draft = compile_draft(goal=goal, root_cause=root_cause, evidence=evidence)
        entry = draft.as_entry(
            tenant_id=tenant_id,
            db_engine=db_engine,
            db_version=db_version,
            adapter_version=adapter_version,
            environment_fingerprint_digest=environment_fingerprint_digest,
        )
        stored = self._service.record(entry, principal=principal)
        # 二次断言：任何路径都不允许编译器直接产出可检索条目。
        if stored.status is not KnowledgeStatus.PROPOSED or stored.is_indexable:
            raise RuntimeError("Wiki 编译器不得把草稿晋升为可检索状态")
        return stored


__all__ = ["DEFAULT_TTL_DAYS", "WikiCompiler", "WikiDraft", "compile_draft"]
