"""P3-T2 单元测试：Wiki 编译器的两条红线。

1. 草稿**永不**自动晋升 `INDEXED`（落库即 `PROPOSED`、不可索引，必须走两级确认）；
2. 落库**必经** `MemoryService.record()` → `RedactionGate`；含手机号/邮箱等 PII 的内容
   会被改写或阻断，编译器不提供绕过路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.context.contracts import EvidenceItem
from core.memory.models import KnowledgeStatus
from core.memory.service import MemoryService
from core.memory.wiki.compiler import WikiCompiler, compile_draft
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate


class _MemoryRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], Any] = {}

    def insert(self, entry: Any) -> Any:
        self.rows[(entry.knowledge_id, entry.revision)] = entry
        return entry

    def get(self, knowledge_id: str, revision: int) -> Any:
        return self.rows.get((knowledge_id, revision))

    def latest(self, knowledge_id: str) -> Any:
        revisions = [rev for (kid, rev) in self.rows if kid == knowledge_id]
        return self.rows.get((knowledge_id, max(revisions))) if revisions else None


class _Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> None:
        self.events.append(event)


def _service() -> tuple[MemoryService, _MemoryRepo]:
    repo = _MemoryRepo()
    return MemoryService(repo, gate=RedactionGate(DataRedactionEngine()), audit=_Audit()), repo


def _evidence(summary: str = "lock wait timeout on seed_orders") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="ev_p3t2_1",
        tool_name="get_lock_wait_edges",
        status="success",
        summary=summary,
    )


def test_draft_is_always_proposed_and_not_indexable() -> None:
    service, repo = _service()
    compiler = WikiCompiler(service)
    stored = compiler.compile_and_store(
        tenant_id="tenant_local",
        goal="订单更新卡住",
        root_cause="lock_wait",
        evidence=[_evidence()],
        db_engine="mysql",
        db_version="8.0.46",
    )
    assert stored.status is KnowledgeStatus.PROPOSED
    assert stored.is_indexable is False
    assert stored.validation_level.value == "UNVERIFIED"
    assert stored.reviewed_by is None
    assert repo.latest(stored.knowledge_id) is stored


def test_compile_never_writes_indexed_even_with_root_cause() -> None:
    draft = compile_draft(
        goal="慢查询", root_cause="missing index", evidence=[_evidence()]
    )
    entry = draft.as_entry(tenant_id="tenant_local", db_engine="mysql", db_version="8.0.46")
    assert entry.status is KnowledgeStatus.PROPOSED
    assert entry.is_indexable is False


def test_pii_in_evidence_never_reaches_knowledge_content() -> None:
    service, _ = _service()
    compiler = WikiCompiler(service)
    stored = compiler.compile_and_store(
        tenant_id="tenant_local",
        goal="用户 13800138000 报障",
        root_cause="lock_wait",
        evidence=[_evidence("contact 13800138000 or user@example.com")],
        db_engine="mysql",
        db_version="8.0.46",
    )
    serialized = str(stored.content)
    assert "13800138000" not in serialized
    assert "user@example.com" not in serialized


def test_root_cause_absent_is_recorded_as_none_not_guessed() -> None:
    draft = compile_draft(goal="未知卡顿", root_cause=None, evidence=[_evidence()])
    assert draft.content["root_cause"] is None
    assert draft.content["verification"]["has_root_cause"] is False


def test_compiler_refuses_to_promote() -> None:
    service, _ = _service()
    compiler = WikiCompiler(service)
    stored = compiler.compile_and_store(
        tenant_id="tenant_local",
        goal="x",
        root_cause=None,
        evidence=[],
        db_engine="mysql",
        db_version="8.0.46",
    )
    # 只有经过 confirm×2 + review 才可能可索引；编译器产物永远不满足。
    with pytest.raises(Exception):
        service.review(
            stored.knowledge_id,
            stored.revision,
            reviewed_by="someone",
            validation_level=stored.validation_level.__class__.SINGLE_CASE,
        )
