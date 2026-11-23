"""P3-T2 单元测试：知识注入上下文的两条铁律。

1. **只注入多级确认 + INDEXED** 的知识（其余一律不进上下文）；
2. **拿不到引擎事实就不注入**（绝不按默认 mysql 猜测，防止跨引擎污染）；
3. 引用形状是"只读参考"：`tool_name="memory_knowledge"`、带不可执行 warning，
   且不含 registry/gateway/DSN 之类可执行句柄。
"""

from __future__ import annotations

from typing import Any

from core.memory.models import (
    Confirmation,
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
    content_digest,
)
from core.memory.wiring import KNOWLEDGE_PREFIX, build_knowledge_loader

TENANT = "tenant_local"


def _entry(knowledge_id: str, *, confirmed: bool = True, tenant_id: str = TENANT) -> KnowledgeEntry:
    content = {"symptom": "lock wait timeout on seed_orders"}
    digest = content_digest(content)
    confirmations = (
        (
            Confirmation(stage=1, confirmed_by="alice", confirmed_at="2026-11-20T00:00:00+00:00", content_digest=digest),
            Confirmation(stage=2, confirmed_by="bob", confirmed_at="2026-11-20T01:00:00+00:00", content_digest=digest),
        )
        if confirmed
        else ()
    )
    return KnowledgeEntry(
        knowledge_id=knowledge_id,
        revision=1,
        tenant_id=tenant_id,
        kind=MemoryKind.SEMANTIC,
        status=KnowledgeStatus.INDEXED,
        db_engine="mysql",
        version_min="8.0",
        version_max="8.4",
        adapter_version=None,
        capability_scope=(),
        environment_fingerprint_digest=None,
        risk_scope=(),
        evidence_ids=("ev_p3t2",),
        test_case_ids=(),
        validation_level=ValidationLevel.SINGLE_CASE,
        reviewed_by="bob",
        reviewed_at="2026-11-20T02:00:00+00:00",
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2035-01-01T00:00:00+00:00",
        source_license="internal",
        content=content,
        redaction_version="redaction-v2",
        confirmations=confirmations,
        content_digest=digest if confirmed else None,
    )


class _Task:
    def __init__(self, goal: str = "lock wait timeout", tenant_id: str = TENANT) -> None:
        self.task_id = "task_p3t2"
        self.tenant_id = tenant_id
        self.goal = goal
        self.environment_id = "env_lab"


def test_loader_returns_only_confirmed_indexed_knowledge() -> None:
    entries = [_entry("knw_ok"), _entry("knw_unconfirmed", confirmed=False)]
    loader = build_knowledge_loader(
        lambda tenant: [item for item in entries if item.tenant_id == tenant],
        engine_of=lambda task: ("mysql", "8.0.46"),
    )
    items = loader(_Task())
    assert [item.evidence_id for item in items] == ["knw_ok@r1"]
    assert items[0].tool_name == "memory_knowledge"
    assert items[0].status == "REFERENCE"
    assert KNOWLEDGE_PREFIX in items[0].summary
    assert items[0].warnings and "不可执行" in items[0].warnings[0]


def test_loader_skips_when_engine_facts_unknown() -> None:
    loader = build_knowledge_loader(
        lambda tenant: [_entry("knw_ok")],
        engine_of=lambda task: None,
    )
    assert loader(_Task()) == []


def test_loader_never_raises_on_repository_failure() -> None:
    def boom(tenant: str) -> Any:
        raise RuntimeError("db down")

    loader = build_knowledge_loader(boom, engine_of=lambda task: ("mysql", "8.0.46"))
    assert loader(_Task()) == []


def test_loader_filters_cross_tenant_entries() -> None:
    loader = build_knowledge_loader(
        lambda tenant: [_entry("knw_other", tenant_id="tenant_other")],
        engine_of=lambda task: ("mysql", "8.0.46"),
    )
    assert loader(_Task()) == []


def test_citation_carries_no_execution_handles() -> None:
    loader = build_knowledge_loader(
        lambda tenant: [_entry("knw_ok")],
        engine_of=lambda task: ("mysql", "8.0.46"),
    )
    item = loader(_Task())[0]
    text = item.summary
    for forbidden in ("registry", "gateway", "dsn", "password"):
        assert forbidden not in text.lower()
