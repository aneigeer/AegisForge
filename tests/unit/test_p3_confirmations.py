"""P3-T1 单元测试：3-Gate 两级确认（N-of-M）与索引准入。

三条验收（roadmap）逐条对应：

1. 同一人尝试完成阶段 1 与阶段 2 → fail closed（`SAME_USER_TWO_STAGES`）；
2. 阶段 1 之后内容被改 → 阶段 1 失效（阶段 2 被拒，需从阶段 1 重来）；
3. 未确认/仅单级确认的知识**永不**进入可检索面（`is_indexable=False`、`review` 被拒）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.memory.models import (
    KnowledgeEntry,
    KnowledgeInvalidError,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
    content_digest,
)
from core.memory.service import MemoryService
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate


class _MemoryRepo:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], KnowledgeEntry] = {}

    def insert(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        self.rows[(entry.knowledge_id, entry.revision)] = entry
        return entry

    def get(self, knowledge_id: str, revision: int) -> KnowledgeEntry | None:
        return self.rows.get((knowledge_id, revision))

    def latest(self, knowledge_id: str) -> KnowledgeEntry | None:
        revisions = [rev for (kid, rev) in self.rows if kid == knowledge_id]
        return self.rows.get((knowledge_id, max(revisions))) if revisions else None


class _Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> None:
        self.events.append(event)


def _service() -> tuple[MemoryService, _MemoryRepo, _Audit]:
    repo = _MemoryRepo()
    audit = _Audit()
    return MemoryService(repo, gate=RedactionGate(DataRedactionEngine()), audit=audit), repo, audit


def _entry(content: dict[str, Any] | None = None) -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id="knw_p3_gate",
        revision=1,
        tenant_id="tenant_local",
        kind=MemoryKind.SEMANTIC,
        status=KnowledgeStatus.PROPOSED,
        db_engine="mysql",
        version_min="8.0",
        version_max="8.4",
        adapter_version=None,
        capability_scope=(),
        environment_fingerprint_digest=None,
        risk_scope=(),
        evidence_ids=("ev_p3",),
        test_case_ids=(),
        validation_level=ValidationLevel.SINGLE_CASE,
        reviewed_by=None,
        reviewed_at=None,
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2035-01-01T00:00:00+00:00",
        source_license="internal",
        content=content or {"symptom": "lock wait timeout"},
        redaction_version="redaction-v2",
    )


def _recorded(service: MemoryService) -> KnowledgeEntry:
    return service.record(_entry(), principal=None)


def test_same_user_cannot_do_both_stages() -> None:
    service, _, _ = _service()
    stored = _recorded(service)
    stage1 = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="alice"
    )
    with pytest.raises(KnowledgeInvalidError) as excinfo:
        service.confirm(
            stage1.knowledge_id, stage1.revision, stage=2, confirmed_by="alice"
        )
    assert excinfo.value.details["reason"] == "SAME_USER_TWO_STAGES"


def test_two_distinct_users_complete_the_gate_then_index() -> None:
    service, _, audit = _service()
    stored = _recorded(service)
    stage1 = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="alice"
    )
    stage2 = service.confirm(
        stage1.knowledge_id, stage1.revision, stage=2, confirmed_by="bob"
    )
    assert stage2.is_multi_confirmed is True
    assert stage2.confirmer_ids == {"alice", "bob"}

    indexed = service.review(
        stage2.knowledge_id,
        stage2.revision,
        reviewed_by="bob",
        validation_level=ValidationLevel.SINGLE_CASE,
    )
    assert indexed.status is KnowledgeStatus.INDEXED
    assert indexed.is_indexable is True
    types = [getattr(item.event_type, "value", item.event_type) for item in audit.events]
    assert "KNOWLEDGE_CONFIRMED" in types and "KNOWLEDGE_INDEXED" in types


def test_content_change_after_stage1_invalidates_stage1() -> None:
    service, repo, _ = _service()
    stored = _recorded(service)
    stage1 = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="alice"
    )
    # 内容被改：写入新 revision（service.record 会重置确认）
    revised = service.record(
        _entry({"symptom": "deadlock detected"}).model_copy(
            update={"knowledge_id": stored.knowledge_id, "revision": stage1.revision + 1}
        ),
        principal=None,
    )
    assert revised.confirmations == (), "内容修订后旧签核必须清空"
    with pytest.raises(KnowledgeInvalidError) as excinfo:
        service.confirm(
            revised.knowledge_id, revised.revision, stage=2, confirmed_by="bob"
        )
    assert excinfo.value.details["reason"] == "STAGE_OUT_OF_ORDER"
    assert repo.latest(stored.knowledge_id) is not None


def test_stage2_before_stage1_is_rejected() -> None:
    service, _, _ = _service()
    stored = _recorded(service)
    with pytest.raises(KnowledgeInvalidError) as excinfo:
        service.confirm(
            stored.knowledge_id, stored.revision, stage=2, confirmed_by="bob"
        )
    assert excinfo.value.details["reason"] == "STAGE_OUT_OF_ORDER"


def test_unconfirmed_and_single_confirmed_are_not_indexable() -> None:
    service, _, _ = _service()
    stored = _recorded(service)
    assert stored.is_indexable is False

    stage1 = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="alice"
    )
    assert stage1.is_indexable is False, "仅单级确认不得进入检索面"
    with pytest.raises(KnowledgeInvalidError) as excinfo:
        service.review(
            stage1.knowledge_id,
            stage1.revision,
            reviewed_by="alice",
            validation_level=ValidationLevel.SINGLE_CASE,
        )
    assert excinfo.value.details["reason"] == "CONFIRMATION_INCOMPLETE"


def test_confirmation_digest_must_match_current_content() -> None:
    service, _, _ = _service()
    stored = _recorded(service)
    stage1 = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="alice"
    )
    # 伪造：把确认记录的摘要换掉（模拟"内容变了但记录没变"）
    tampered = stage1.model_copy(
        update={
            "confirmations": tuple(
                item.model_copy(update={"content_digest": "0" * 64})
                for item in stage1.confirmations
            )
        }
    )
    assert tampered.is_multi_confirmed is False
    assert tampered.is_indexable is False
    assert content_digest(tampered.content) != "0" * 64
