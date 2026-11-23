"""W09 T04 / AC-14：真实控制库下的硬过滤、冲突组与撤销版本保留。

纪律（``03-task-cards.md:6-12``）：

- 只依赖 ``control_client``（dg_ctl），**不依赖** ``DG_ADMIN_DSN``、不依赖 PG 5432；
- 用例只做 INSERT（record / review / revoke 都追加新 revision），不清理、不 DELETE；
- 控制库不可用时 **skipped 带原因**（任务卡 ``:512`` 明确允许），不写「通过」；
- 不加 ``xfail``、不放宽断言、不缩小参数化。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from core.errors import DBGuardianError
from core.memory.models import (  # noqa: F401
    Confirmation,
    content_digest,
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    MemoryQuery,
    ValidationLevel,
)
from core.memory.repository import KnowledgeRepository
from core.memory.resolver import FilterReason, MemoryConflictResolver
from core.memory.service import MemoryService
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tasks.models import new_id

pytestmark = pytest.mark.integration

_TENANT = "tenant_local"
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_WINDOW_FROM = "2026-01-01T00:00:00+00:00"
_WINDOW_UNTIL = "2026-12-31T00:00:00+00:00"


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event


@pytest.fixture
def control_db(control_client):
    """控制库可用性探针：不可用一律 skip（带原因），绝不 failed。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


def _entry(
    knowledge_id: str,
    *,
    db_engine: str = "mysql",
    status: KnowledgeStatus = KnowledgeStatus.INDEXED,
    content: dict[str, Any] | None = None,
    validation_level: ValidationLevel = ValidationLevel.SINGLE_CASE,
) -> KnowledgeEntry:
    payload = content or {"symptom": "lock wait timeout"}
    return KnowledgeEntry(
        knowledge_id=knowledge_id,
        revision=1,
        tenant_id=_TENANT,
        kind=MemoryKind.SEMANTIC,
        status=status,
        db_engine=db_engine,
        version_min="8.0",
        version_max="8.4",
        adapter_version=None,
        capability_scope=(),
        environment_fingerprint_digest=None,
        risk_scope=(),
        evidence_ids=("ev_integration_w09_t04",),
        test_case_ids=(),
        validation_level=validation_level,
        reviewed_by="reviewer_w09_t04",
        reviewed_at="2026-05-01T00:00:00+00:00",
        effective_from=_WINDOW_FROM,
        effective_until=_WINDOW_UNTIL,
        source_license="internal",
        content=payload,
        redaction_version="redaction-v2",
        # P3-T1：两级确认（两名不同的人），否则条目不可索引。
        confirmations=_confirmations(payload),
        content_digest=content_digest(payload),
    )


def _confirmations(content: dict[str, Any]):
    digest = content_digest(content)
    return (
        Confirmation(
            stage=1,
            confirmed_by="reviewer_a",
            confirmed_at="2026-04-30T00:00:00+00:00",
            content_digest=digest,
        ),
        Confirmation(
            stage=2,
            confirmed_by="reviewer_b",
            confirmed_at="2026-04-30T01:00:00+00:00",
            content_digest=digest,
        ),
    )


def _query() -> MemoryQuery:
    return MemoryQuery(
        tenant_id=_TENANT,
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version=None,
        capabilities=(),
        environment_fingerprint_digest=None,
        risk_level="R0",
    )


def _service(control_client: Any) -> tuple[MemoryService, KnowledgeRepository, _FakeAudit]:
    repository = KnowledgeRepository(control_client)
    audit = _FakeAudit()
    service = MemoryService(
        repository,
        gate=RedactionGate(DataRedactionEngine()),
        audit=audit,
    )
    return service, repository, audit


def _publish(service: Any, entry: KnowledgeEntry) -> KnowledgeEntry:
    """P3-T1 的真实晋升路径：record(PROPOSED) → 两级确认 → review(INDEXED)。"""
    stored = service.record(
        entry.model_copy(update={"status": KnowledgeStatus.PROPOSED}), principal=None
    )
    stored = service.confirm(
        stored.knowledge_id, stored.revision, stage=1, confirmed_by="reviewer_a"
    )
    stored = service.confirm(
        stored.knowledge_id, stored.revision, stage=2, confirmed_by="reviewer_b"
    )
    return service.review(
        stored.knowledge_id,
        stored.revision,
        reviewed_by="reviewer_b",
        validation_level=stored.validation_level,
    )


def _candidates(
    repository: KnowledgeRepository, knowledge_ids: set[str]
) -> list[KnowledgeEntry]:
    """只取本用例新建的 knowledge_id（历史残留行不参与断言）。"""
    return [
        entry
        for entry in repository.list_by_tenant(_TENANT)
        if entry.knowledge_id in knowledge_ids
    ]


def test_postgresql_knowledge_is_filtered_out_for_mysql_task(control_db: Any) -> None:
    service, repository, _ = _service(control_db)
    mysql_id = new_id("knw")
    pg_id = new_id("knw")
    _publish(service, _entry(mysql_id))
    _publish(service, _entry(pg_id, db_engine="postgresql"))

    resolution = MemoryConflictResolver().resolve(
        query=_query(),
        entries=_candidates(repository, {mysql_id, pg_id}),
        now=_NOW,
    )
    available_ids = {item.knowledge_id for item in resolution.available}
    assert pg_id not in available_ids
    assert mysql_id in available_ids
    assert (pg_id, FilterReason.ENGINE_MISMATCH.value) in resolution.excluded


def test_conflict_group_is_excluded_from_available(control_db: Any) -> None:
    service, repository, _ = _service(control_db)
    first_id = new_id("knw")
    second_id = new_id("knw")
    _publish(service, _entry(first_id, content={"symptom": "lock wait timeout"}))
    _publish(service, _entry(second_id, content={"symptom": "deadlock detected"}))

    resolution = MemoryConflictResolver().resolve(
        query=_query(),
        entries=_candidates(repository, {first_id, second_id}),
        now=_NOW,
    )
    assert resolution.available == ()
    assert len(resolution.conflicted) == 2
    assert {item.knowledge_id for item in resolution.conflicted} == {
        first_id,
        second_id,
    }
    assert all(
        item.status is KnowledgeStatus.CONFLICTED
        for item in resolution.conflicted
    )


def test_revoke_preserves_versions_and_revoked_reason(control_db: Any) -> None:
    service, repository, audit = _service(control_db)
    knowledge_id = new_id("knw")
    recorded = service.record(_entry(knowledge_id), principal=None)
    principal = type("P", (), {"user_id": "user_w09_t04"})()

    revoked = service.revoke(
        knowledge_id,
        recorded.revision,
        reason="环境已变更，需重新验证",
        principal=principal,
    )

    versions = service.list_versions(knowledge_id, tenant_id=_TENANT)
    assert len(versions) >= 2
    assert versions[0].status is KnowledgeStatus.INDEXED
    latest = versions[-1]
    assert latest.revision == revoked.revision
    assert latest.status is KnowledgeStatus.REVOKED
    assert latest.revoked_reason == "环境已变更，需重新验证"
    assert latest.revoked_at is not None

    assert len(audit.events) == 1
    assert audit.events[0].event_type == "KNOWLEDGE_REVOKED"

    resolution = MemoryConflictResolver().resolve(
        query=_query(),
        entries=[latest],
        now=_NOW,
    )
    assert resolution.available == ()
    assert (knowledge_id, FilterReason.REVOKED.value) in resolution.excluded
