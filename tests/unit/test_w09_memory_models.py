"""W09 T04 / AC-13、AC-15：五类记忆模型、`is_indexable`、解析器与结构断言。

全部离线：不连库、不依赖 DG_ADMIN_DSN / PG 5432、不新增第三方依赖。
AC-14 的真实库断言见 ``tests/integration/test_w09_conflict_resolver.py``。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import datetime, timezone
from typing import Any, Sequence

import pytest
from pydantic import ValidationError

from core.audit.events import AuditEventType
from core.memory.models import (  # noqa: F401
    content_digest,
    KnowledgeEntry,
    KnowledgeInvalidError,
    KnowledgeStatus,
    MemoryKind,
    MemoryQuery,
    ValidationLevel,
)
from core.memory.resolver import (
    FilterReason,
    MemoryConflictResolver,
    MemoryResolution,
)
from core.memory.service import MemoryService
from core.memory.working import WorkingSet
from core.redaction.models import RedactionResult, RedactionStatus

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_TENANT = "tenant_local"

FORBIDDEN_PARAMS = frozenset(
    {"tool_registry", "gateway", "dsn", "ro_dsn", "rw_dsn", "control_dsn"}
)
FORBIDDEN_IMPORT_PREFIXES = (
    "core.tools.registry",
    "core.gateway",
    "core.tools.executor",
    "database.mysql",
    "database.postgres",
)


def _confirmations(content: dict[str, Any]) -> tuple[Any, ...]:
    """P3-T1：两级确认（两名不同的人）——INDEXED 条目的必备前提。"""
    from core.memory.models import Confirmation, content_digest

    digest = content_digest(content)
    return (
        Confirmation(
            stage=1,
            confirmed_by="reviewer_stage1",
            confirmed_at="2026-04-30T00:00:00+00:00",
            content_digest=digest,
        ),
        Confirmation(
            stage=2,
            confirmed_by="reviewer_stage2",
            confirmed_at="2026-04-30T01:00:00+00:00",
            content_digest=digest,
        ),
    )


def _entry(
    knowledge_id: str = "knw_mysql_lock",
    revision: int = 1,
    **overrides: Any,
) -> KnowledgeEntry:
    values: dict[str, Any] = {
        "knowledge_id": knowledge_id,
        "revision": revision,
        "tenant_id": _TENANT,
        "kind": MemoryKind.SEMANTIC,
        "status": KnowledgeStatus.INDEXED,
        "db_engine": "mysql",
        "version_min": "8.0",
        "version_max": "8.4",
        "adapter_version": "w09-adapter",
        "capability_scope": (),
        "environment_fingerprint_digest": None,
        "risk_scope": (),
        "evidence_ids": ("ev_one",),
        "test_case_ids": (),
        "validation_level": ValidationLevel.SINGLE_CASE,
        "reviewed_by": "reviewer_w09",
        "reviewed_at": "2026-05-01T00:00:00+00:00",
        "effective_from": "2020-01-01T00:00:00+00:00",
        "effective_until": "2035-01-01T00:00:00+00:00",
        "source_license": "internal",
        "content": {"symptom": "lock wait timeout"},
        "redaction_version": "redaction-v2",
    }
    values.update(overrides)
    content = dict(values.get("content") or {})
    values.setdefault("confirmations", _confirmations(content))
    values.setdefault("content_digest", content_digest(content))
    return KnowledgeEntry(**values)


def _query(**overrides: Any) -> MemoryQuery:
    values: dict[str, Any] = {
        "tenant_id": _TENANT,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-adapter",
        "capabilities": (),
        "environment_fingerprint_digest": None,
        "risk_level": "R0",
    }
    values.update(overrides)
    return MemoryQuery(**values)


def _resolved_reason(entry: KnowledgeEntry, query: MemoryQuery | None = None) -> str:
    resolution = MemoryConflictResolver().resolve(
        query=query or _query(), entries=[entry], now=_NOW
    )
    assert resolution.available == ()
    assert len(resolution.excluded) == 1
    return resolution.excluded[0][1]


# --------------------------------------------------------------------------- #
# AC-13：五类封闭枚举与 extra=forbid
# --------------------------------------------------------------------------- #


def test_memory_kind_has_exactly_five_values() -> None:
    assert len(MemoryKind) == 5
    assert {item.value for item in MemoryKind} == {
        "WORKING",
        "EPISODIC",
        "SEMANTIC",
        "PROCEDURAL",
        "ENVIRONMENT",
    }
    assert len(KnowledgeStatus) == 5
    assert len(ValidationLevel) == 4


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _entry(unexpected_field="x")
    with pytest.raises(ValidationError):
        MemoryQuery(
            tenant_id=_TENANT,
            db_engine="mysql",
            db_version="8.0.46",
            unexpected_field="x",
        )


def test_is_indexable_requires_review_window_status_and_kind() -> None:
    assert _entry().is_indexable is True
    # AC-13 反例一：缺审核人（含空白审核人）。
    assert _entry(reviewed_by=None).is_indexable is False
    assert _entry(reviewed_by="   ").is_indexable is False
    # AC-13 反例二：已过期。
    assert (
        _entry(
            effective_from="2020-01-01T00:00:00+00:00",
            effective_until="2025-12-31T00:00:00+00:00",
        ).is_indexable
        is False
    )
    # 其余子条件。
    assert _entry(status=KnowledgeStatus.PROPOSED).is_indexable is False
    assert _entry(kind=MemoryKind.PROCEDURAL).is_indexable is False
    assert (
        _entry(validation_level=ValidationLevel.UNVERIFIED).is_indexable is False
    )
    assert (
        _entry(
            status=KnowledgeStatus.REVOKED,
            revoked_at="2026-05-01T00:00:00+00:00",
            revoked_reason="stale",
        ).is_indexable
        is False
    )


def test_indexable_at_uses_injected_clock() -> None:
    entry = _entry(
        effective_from="2026-01-01T00:00:00+00:00",
        effective_until="2026-12-31T00:00:00+00:00",
    )
    assert entry.indexable_at(_NOW) is True
    assert entry.indexable_at(datetime(2027, 1, 1, tzinfo=timezone.utc)) is False


# --------------------------------------------------------------------------- #
# AC-14（离线部分）：8 道硬过滤顺序与原因
# --------------------------------------------------------------------------- #


def test_postgresql_knowledge_never_matches_mysql_task() -> None:
    """引擎硬过滤结构上先于任何文本/相似度；内容与 mysql 条目完全相同也不命中。"""
    shared_content = {"symptom": "lock wait timeout"}
    mysql_entry = _entry("knw_mysql", content=shared_content)
    pg_entry = _entry("knw_pg", db_engine="postgresql", content=shared_content)
    resolution = MemoryConflictResolver().resolve(
        query=_query(), entries=[mysql_entry, pg_entry], now=_NOW
    )
    assert resolution.available == (mysql_entry,)
    assert ("knw_pg", FilterReason.ENGINE_MISMATCH.value) in resolution.excluded


@pytest.mark.parametrize(
    "entry,expected",
    [
        (_entry(db_engine="postgresql"), FilterReason.ENGINE_MISMATCH.value),
        (_entry(version_min="eight"), FilterReason.VERSION_PARSE_ERROR.value),
        (_entry(version_min="9.0"), FilterReason.VERSION_OUT_OF_RANGE.value),
        (_entry(adapter_version="w99-adapter"), FilterReason.ADAPTER_MISMATCH.value),
        (
            _entry(capability_scope=("locks",)),
            FilterReason.CAPABILITY_MISSING.value,
        ),
        (
            _entry(environment_fingerprint_digest="f" * 64),
            FilterReason.ENVIRONMENT_MISMATCH.value,
        ),
        (_entry(risk_scope=("R3",)), FilterReason.RISK_NOT_IN_SCOPE.value),
        (_entry(status=KnowledgeStatus.PROPOSED), FilterReason.NOT_INDEXABLE.value),
        (
            _entry(
                effective_from="2020-01-01T00:00:00+00:00",
                effective_until="2025-12-31T00:00:00+00:00",
            ),
            FilterReason.EXPIRED.value,
        ),
        (
            _entry(
                status=KnowledgeStatus.REVOKED,
                revoked_at="2026-05-01T00:00:00+00:00",
                revoked_reason="stale",
            ),
            FilterReason.REVOKED.value,
        ),
    ],
)
def test_hard_filter_reasons(entry: KnowledgeEntry, expected: str) -> None:
    assert _resolved_reason(entry) == expected


def test_empty_risk_scope_allows_only_r0_r1() -> None:
    resolver = MemoryConflictResolver()
    assert (
        resolver.resolve(
            query=_query(risk_level="R1"), entries=[_entry()], now=_NOW
        ).available
        != ()
    )
    resolution = resolver.resolve(
        query=_query(risk_level="R2"), entries=[_entry()], now=_NOW
    )
    assert resolution.available == ()
    assert resolution.excluded[0][1] == FilterReason.RISK_NOT_IN_SCOPE.value


@pytest.mark.parametrize(
    "entry",
    [
        _entry(kind=MemoryKind.PROCEDURAL),
        _entry(kind=MemoryKind.ENVIRONMENT),
        _entry(kind=MemoryKind.ENVIRONMENT, db_engine=None),
    ],
)
def test_procedural_and_environment_never_enter_available(
    entry: KnowledgeEntry,
) -> None:
    resolution = MemoryConflictResolver().resolve(
        query=_query(), entries=[entry], now=_NOW
    )
    assert resolution.available == ()
    assert resolution.excluded[0][1] == FilterReason.NOT_INDEXABLE.value


# --------------------------------------------------------------------------- #
# 比较与冲突组（同组候选 content_digest 差异）
# --------------------------------------------------------------------------- #


def test_conflict_when_top_two_content_digests_differ() -> None:
    first = _entry("knw_a", content={"symptom": "lock wait timeout"})
    second = _entry("knw_b", content={"symptom": "deadlock detected"})
    resolution = MemoryConflictResolver().resolve(
        query=_query(), entries=[second, first], now=_NOW
    )
    assert resolution.available == ()
    assert len(resolution.conflicted) == 2
    assert {item.knowledge_id for item in resolution.conflicted} == {
        "knw_a",
        "knw_b",
    }
    assert all(
        item.status is KnowledgeStatus.CONFLICTED
        for item in resolution.conflicted
    )


def test_same_content_digest_is_not_conflict() -> None:
    first = _entry("knw_a", content={"symptom": "lock wait timeout"})
    second = _entry("knw_b", content={"symptom": "lock wait timeout"})
    resolution = MemoryConflictResolver().resolve(
        query=_query(), entries=[first, second], now=_NOW
    )
    assert resolution.conflicted == ()
    assert len(resolution.available) == 2


def test_comparison_order_is_deterministic() -> None:
    resolver = MemoryConflictResolver()
    entries = [
        _entry("knw_c", validation_level=ValidationLevel.REGRESSION),
        _entry("knw_a", validation_level=ValidationLevel.FIELD),
        _entry("knw_b", validation_level=ValidationLevel.FIELD),
    ]
    first = resolver.resolve(query=_query(), entries=entries, now=_NOW)
    second = resolver.resolve(query=_query(), entries=list(reversed(entries)), now=_NOW)
    # FIELD > REGRESSION，同等级按 knowledge_id 字典序；输入顺序不影响结果。
    assert [item.knowledge_id for item in first.available] == [
        "knw_a",
        "knw_b",
        "knw_c",
    ]
    assert first == second


def test_latest_revision_wins_over_older_revision() -> None:
    revoked = _entry(
        "knw_a",
        revision=2,
        status=KnowledgeStatus.REVOKED,
        revoked_at="2026-05-01T00:00:00+00:00",
        revoked_reason="stale",
    )
    older = _entry("knw_a", revision=1)
    resolution = MemoryConflictResolver().resolve(
        query=_query(), entries=[older, revoked], now=_NOW
    )
    assert resolution.available == ()
    assert resolution.excluded == (("knw_a", FilterReason.REVOKED.value),)


def test_resolution_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryResolution(available=(), conflicted=(), excluded=(), extra="x")


# --------------------------------------------------------------------------- #
# AC-15：记忆不授权执行（签名 + 字段 + AST 导入三层结构断言）
# --------------------------------------------------------------------------- #


def test_signatures_have_no_execution_capability_params() -> None:
    targets = [
        MemoryConflictResolver.__init__,
        MemoryConflictResolver.resolve,
        MemoryService.__init__,
        MemoryService.record,
        MemoryService.review,
        MemoryService.revoke,
        MemoryService.expire_due,
        MemoryService.list_versions,
        WorkingSet.__init__,
        WorkingSet.add_hypothesis,
        WorkingSet.add_todo,
        WorkingSet.add_evidence,
        WorkingSet.snapshot,
        WorkingSet.clear,
    ]
    for target in targets:
        params = set(inspect.signature(target).parameters)
        assert params.isdisjoint(FORBIDDEN_PARAMS), (target, params)


def test_knowledge_entry_fields_have_no_execution_keys() -> None:
    assert set(KnowledgeEntry.model_fields).isdisjoint(FORBIDDEN_PARAMS)
    assert set(MemoryQuery.model_fields).isdisjoint(FORBIDDEN_PARAMS)
    assert not hasattr(MemoryConflictResolver, "insert")
    assert not hasattr(MemoryConflictResolver, "save")


def test_memory_modules_do_not_import_tooling_or_drivers() -> None:
    memory_dir = pathlib.Path(__file__).resolve().parents[2] / "core" / "memory"
    files = sorted(memory_dir.glob("*.py"))
    assert files, "core/memory 下应有模块"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        for module in modules:
            assert not any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in FORBIDDEN_IMPORT_PREFIXES
            ), (path.name, module)


# --------------------------------------------------------------------------- #
# MemoryService：落库前脱敏、WORKING/EPISODIC 拒绝、审核/撤销版本保留
# --------------------------------------------------------------------------- #


def _result(
    status: RedactionStatus,
    payload: dict[str, Any],
    *,
    version: str = "redaction-v2",
) -> RedactionResult:
    return RedactionResult(
        payload_redacted=payload,
        redaction_version=version,
        detected_categories=(),
        redaction_count=0,
        unresolved_sensitive_spans=(),
        payload_hash="a" * 64,
        status=status,
    )


class _FakeGate:
    def __init__(self, result: RedactionResult) -> None:
        self.result = result
        self.calls: list[tuple[dict[str, Any], str, str | None]] = []

    def scan_event(
        self, payload: dict[str, Any], *, task_id: str, tenant_id: str | None = None
    ) -> RedactionResult:
        self.calls.append((dict(payload), task_id, tenant_id))
        return self.result


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event


class _FakeRepository:
    def __init__(self, entries: Sequence[KnowledgeEntry] = ()) -> None:
        self.entries: list[KnowledgeEntry] = list(entries)
        self.inserted: list[KnowledgeEntry] = []
        self.expired: tuple[str, ...] = ()

    def insert(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        self.inserted.append(entry)
        self.entries.append(entry)
        return entry

    def get(self, knowledge_id: str, revision: int) -> KnowledgeEntry | None:
        for entry in self.entries:
            if entry.knowledge_id == knowledge_id and entry.revision == revision:
                return entry
        return None

    def latest(self, knowledge_id: str, *, tenant_id: str) -> KnowledgeEntry | None:
        matches = [
            entry
            for entry in self.entries
            if entry.knowledge_id == knowledge_id and entry.tenant_id == tenant_id
        ]
        return max(matches, key=lambda item: item.revision) if matches else None

    def list_versions(
        self, knowledge_id: str, *, tenant_id: str
    ) -> tuple[KnowledgeEntry, ...]:
        return tuple(
            sorted(
                (
                    entry
                    for entry in self.entries
                    if entry.knowledge_id == knowledge_id
                    and entry.tenant_id == tenant_id
                ),
                key=lambda item: item.revision,
            )
        )

    def list_by_tenant(
        self, tenant_id: str, limit: int = 500
    ) -> tuple[KnowledgeEntry, ...]:
        return tuple(
            entry for entry in self.entries if entry.tenant_id == tenant_id
        )[:limit]

    def list_expired(self, now: datetime) -> tuple[str, ...]:
        return self.expired


def _service(repository: _FakeRepository | None = None, result: RedactionResult | None = None):
    repo = repository or _FakeRepository()
    gate = _FakeGate(
        result
        or _result(
            RedactionStatus.CLEAN,
            {"symptom": "lock wait timeout"},
        )
    )
    audit = _FakeAudit()
    return MemoryService(repo, gate=gate, audit=audit), repo, gate, audit  # type: ignore[arg-type]


def test_record_rejects_working_and_episodic_kinds() -> None:
    service, repository, gate, _ = _service()
    for kind in (MemoryKind.WORKING, MemoryKind.EPISODIC):
        with pytest.raises(KnowledgeInvalidError) as raised:
            service.record(
                _entry(kind=kind, status=KnowledgeStatus.PROPOSED),
                principal=None,
            )
        assert raised.value.code.value == "KNOWLEDGE_INVALID"
    assert repository.inserted == []
    assert gate.calls == []


def test_record_scans_content_on_evidence_channel_and_stores_redaction_version() -> None:
    redacted = _result(
        RedactionStatus.REDACTED,
        {"note": "<PHONE:deadbeef>"},
        version="redaction-v2",
    )
    service, repository, gate, _ = _service(result=redacted)
    entry = _entry(content={"note": "13812345678"})
    stored = service.record(entry, principal=None)
    assert gate.calls == [
        ({"note": "13812345678"}, entry.knowledge_id, _TENANT)
    ]
    assert stored.content == {"note": "<PHONE:deadbeef>"}
    assert stored.redaction_version == "redaction-v2"
    assert repository.inserted == [stored]


def test_record_rejects_non_passable_redaction() -> None:
    blocked = _result(RedactionStatus.BLOCKED, {"note": "<BLOCKED:TOKEN>"})
    service, repository, _, _ = _service(result=blocked)
    with pytest.raises(KnowledgeInvalidError):
        service.record(_entry(content={"note": "AKIA-secret"}), principal=None)
    assert repository.inserted == []


def test_review_appends_indexed_revision() -> None:
    base = _entry(
        status=KnowledgeStatus.PROPOSED,
        validation_level=ValidationLevel.UNVERIFIED,
        reviewed_by=None,
        reviewed_at=None,
    )
    service, repository, _, _ = _service(_FakeRepository([base]))
    reviewed = service.review(
        base.knowledge_id,
        base.revision,
        reviewed_by="reviewer_w09",
        validation_level=ValidationLevel.SINGLE_CASE,
    )
    assert reviewed.revision == 2
    assert reviewed.status is KnowledgeStatus.INDEXED
    assert reviewed.reviewed_by == "reviewer_w09"
    assert reviewed.reviewed_at is not None
    assert reviewed.is_indexable is True
    assert len(repository.inserted) == 1


def test_revoke_appends_revision_and_emits_knowledge_revoked() -> None:
    base = _entry()
    service, repository, _, audit = _service(_FakeRepository([base]))
    revoked = service.revoke(
        base.knowledge_id, base.revision, reason="环境已变更", principal=type(
            "P", (), {"user_id": "user_w09"}
        )()
    )
    assert revoked.revision == 2
    assert revoked.status is KnowledgeStatus.REVOKED
    assert revoked.revoked_reason == "环境已变更"
    assert revoked.revoked_at is not None
    assert len(repository.inserted) == 1
    assert len(audit.events) == 1
    assert audit.events[0].event_type == AuditEventType.KNOWLEDGE_REVOKED.value
    assert audit.events[0].payload["revoked_reason"] == "环境已变更"
    versions = service.list_versions(base.knowledge_id, tenant_id=_TENANT)
    assert len(versions) == 2
    assert versions[0].status is KnowledgeStatus.INDEXED
    assert versions[-1].revoked_reason == "环境已变更"


def test_revoke_requires_reason() -> None:
    service, _, _, _ = _service(_FakeRepository([_entry()]))
    with pytest.raises(KnowledgeInvalidError):
        service.revoke("knw_mysql_lock", 1, reason="   ", principal=None)


def test_expire_due_is_read_only() -> None:
    repository = _FakeRepository([_entry()])
    repository.expired = ("knw_mysql_lock",)
    service, _, _, _ = _service(repository)
    assert service.expire_due(_NOW) == ("knw_mysql_lock",)
    assert repository.inserted == []


def test_working_set_and_episodic_view_are_in_memory() -> None:
    working = WorkingSet()
    working.add_hypothesis(statement="锁等待来自长事务", confidence="MEDIUM")
    working.add_todo("检查 innodb_trx")
    working.add_evidence("ev_one")
    snapshot = working.snapshot()
    assert snapshot.hypotheses[0]["statement"] == "锁等待来自长事务"
    assert snapshot.evidence_ids == ("ev_one",)
    working.clear()
    assert working.snapshot().todos == ()

    from core.memory.episodic import EpisodicView

    class _Events:
        def list_events(self, task_id: str, limit: int = 200, *, after_seq: int = 0):
            return [
                type(
                    "E",
                    (),
                    {
                        "event_id": "evt_1",
                        "task_id": task_id,
                        "event_type": "TASK_CREATED",
                        "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                    },
                )()
            ]

    class _Evidence:
        def list_by_task(
            self, task_id: str, tenant_id: str, limit: int = 50, cursor: str | None = None
        ):
            return (
                [
                    type(
                        "V",
                        (),
                        {
                            "evidence_id": "ev_1",
                            "task_id": task_id,
                            "observed_at": "2026-06-01T00:00:01+00:00",
                            "source_tool": "get_locks",
                            "collection_status": type(
                                "S", (), {"value": "SUCCESS"}
                            )(),
                        },
                    )()
                ],
                None,
            )

    view = EpisodicView(events=_Events(), evidence=_Evidence())
    timeline = view.timeline(task_id="task_1", tenant_id=_TENANT)
    assert [item.source for item in timeline] == ["event", "evidence"]
