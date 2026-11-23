"""P3-T2 单元测试：Hybrid 检索的"该出现的出现、不该出现的绝不出现"。

验收锚点（roadmap P3-T2）：

- 只有**多级确认 + INDEXED** 的 Wiki/知识可被诊断引用；
- `PROPOSED` / `REVOKED` / `CONFLICTED` / 跨租户 / 跨引擎一律不出现；
- 打分可解释（命中词可枚举）、排序确定（无随机、无时钟依赖）。
"""

from __future__ import annotations

from typing import Any

from core.memory.models import (
    Confirmation,
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    MemoryQuery,
    ValidationLevel,
    content_digest,
)
from core.memory.retrieval import HybridRetriever, tokenize

TENANT = "tenant_local"
NOW = "2026-11-21T00:00:00+00:00"


def _confirmations(content: dict[str, Any]):
    digest = content_digest(content)
    return (
        Confirmation(
            stage=1,
            confirmed_by="alice",
            confirmed_at="2026-11-20T00:00:00+00:00",
            content_digest=digest,
        ),
        Confirmation(
            stage=2,
            confirmed_by="bob",
            confirmed_at="2026-11-20T01:00:00+00:00",
            content_digest=digest,
        ),
    )


def _entry(
    knowledge_id: str,
    *,
    content: dict[str, Any] | None = None,
    status: KnowledgeStatus = KnowledgeStatus.INDEXED,
    tenant_id: str = TENANT,
    db_engine: str | None = "mysql",
    validation_level: ValidationLevel = ValidationLevel.SINGLE_CASE,
    confirmed: bool = True,
    revision: int = 1,
) -> KnowledgeEntry:
    payload = content or {"symptom": "lock wait timeout"}
    return KnowledgeEntry(
        knowledge_id=knowledge_id,
        revision=revision,
        tenant_id=tenant_id,
        kind=MemoryKind.SEMANTIC,
        status=status,
        db_engine=db_engine,
        version_min="8.0",
        version_max="8.4",
        adapter_version=None,
        capability_scope=(),
        environment_fingerprint_digest=None,
        risk_scope=(),
        evidence_ids=("ev_p3t2",),
        test_case_ids=(),
        validation_level=validation_level,
        reviewed_by="bob" if status is KnowledgeStatus.INDEXED else None,
        reviewed_at="2026-11-20T02:00:00+00:00" if status is KnowledgeStatus.INDEXED else None,
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2035-01-01T00:00:00+00:00",
        source_license="internal",
        content=payload,
        redaction_version="redaction-v2",
        confirmations=_confirmations(payload) if confirmed else (),
        content_digest=content_digest(payload) if confirmed else None,
        revoked_at=(
            "2026-11-20T03:00:00+00:00" if status is KnowledgeStatus.REVOKED else None
        ),
        revoked_reason=(
            "P3-T2 测试：已撤销" if status is KnowledgeStatus.REVOKED else None
        ),
    )


def _query(text: str = "", **overrides: Any) -> MemoryQuery:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": None,
        "capabilities": (),
        "environment_fingerprint_digest": None,
        "risk_level": "R0",
        "text": text,
    }
    values.update(overrides)
    return MemoryQuery(**values)


def test_only_multi_confirmed_indexed_entries_are_returned() -> None:
    entries = [
        _entry("knw_ok", validation_level=ValidationLevel.REGRESSION),
        _entry("knw_proposed", status=KnowledgeStatus.PROPOSED, confirmed=False),
        _entry("knw_revoked", status=KnowledgeStatus.REVOKED, confirmed=False),
        _entry("knw_unconfirmed", confirmed=False),
        _entry("knw_other_tenant", tenant_id="tenant_other"),
        _entry("knw_pg", db_engine="postgresql"),
    ]
    outcome = HybridRetriever(max_hits=10).search(
        query=_query("lock wait timeout"), entries=entries
    )
    assert [hit.knowledge_id for hit in outcome.hits] == ["knw_ok"]
    reasons = dict(outcome.excluded)
    assert "knw_other_tenant" in reasons and "knw_pg" in reasons
    assert any(reason.startswith("RETRIEVER_GUARD") or "TENANT" in reason or "ENGINE" in reason
               for reason in reasons.values())


def test_single_entry_scores_and_explains() -> None:
    entry = _entry("knw_lock", content={"symptom": "lock wait timeout on seed_orders"})
    outcome = HybridRetriever(max_hits=3).search(
        query=_query("lock wait timeout"), entries=[entry]
    )
    assert outcome.hits[0].knowledge_id == "knw_lock"
    assert outcome.hits[0].score > 0
    assert "lock" in outcome.hits[0].matched_terms
    irrelevant = HybridRetriever(max_hits=3).search(
        query=_query("lock wait timeout"),
        entries=[_entry("knw_io", content={"symptom": "disk io saturation"})],
    )
    assert irrelevant.hits[0].score == 0.0


def test_conflicting_contents_are_excluded_not_silently_picked() -> None:
    """两条不同内容 → 解析器判冲突并**两组都不给**（不做"择优"）。

    这条把 W09 的冲突语义与 P3-T2 的检索面绑在一起：
    检索绝不能绕过冲突判定去"挑一个更像的"。
    """
    entries = [
        _entry("knw_a", content={"symptom": "lock wait timeout"}),
        _entry("knw_b", content={"symptom": "deadlock detected"}),
    ]
    outcome = HybridRetriever(max_hits=5).search(
        query=_query("lock wait timeout"), entries=entries
    )
    assert outcome.hits == ()


def test_min_score_excludes_unrelated_entries() -> None:
    entries = [_entry("knw_io", content={"symptom": "disk io saturation"})]
    outcome = HybridRetriever(min_score=0.1).search(
        query=_query("lock wait timeout"), entries=entries
    )
    assert outcome.hits == ()
    assert dict(outcome.excluded)["knw_io"] == "BELOW_MIN_SCORE"


def test_empty_query_text_falls_back_to_trust_ordering() -> None:
    entries = [
        _entry("knw_single", validation_level=ValidationLevel.SINGLE_CASE),
        _entry("knw_field", validation_level=ValidationLevel.FIELD),
        _entry("knw_regression", validation_level=ValidationLevel.REGRESSION),
    ]
    outcome = HybridRetriever(max_hits=3).search(query=_query(), entries=entries)
    assert [hit.knowledge_id for hit in outcome.hits] == [
        "knw_field",
        "knw_regression",
        "knw_single",
    ]


def test_max_hits_and_determinism() -> None:
    entries = [_entry(f"knw_{index:02d}", content={"symptom": "lock wait"}) for index in range(8)]
    first = HybridRetriever(max_hits=3).search(query=_query("lock wait"), entries=entries)
    second = HybridRetriever(max_hits=3).search(
        query=_query("lock wait"), entries=list(reversed(entries))
    )
    assert len(first.hits) == 3
    assert [hit.knowledge_id for hit in first.hits] == [hit.knowledge_id for hit in second.hits]


def test_citation_shape_has_no_execution_handles() -> None:
    entry = _entry("knw_cite")
    hit = HybridRetriever(max_hits=1).search(query=_query("lock wait"), entries=[entry]).hits[0]
    citation = hit.as_citation()
    assert set(citation) == {
        "knowledge_id",
        "revision",
        "kind",
        "validation_level",
        "db_engine",
        "score",
        "content",
    }
    assert "registry" not in citation and "gateway" not in citation


def test_tokenize_is_deterministic_for_cjk_and_ascii() -> None:
    assert tokenize("Lock Wait 超时") == tokenize("lock wait 超时")
    assert "lock" in tokenize("Lock Wait")
    assert "超时" in tokenize("锁等待超时")
