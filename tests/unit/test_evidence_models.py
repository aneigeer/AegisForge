"""Evidence 模型与规范化哈希单元测试（W03）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceCreate,
    EvidenceQuality,
)
from core.evidence.service import canonical_payload_hash


def _create_payload(**overrides) -> dict:
    base = {
        "tenant_id": "tenant_local",
        "task_id": "task_demo",
        "environment_id": "env_mysql_local",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w03-1",
        "source_tool": "get_db_version",
        "observed_at": "2026-02-01T00:00:00+00:00",
        "collection_status": CollectionStatus.SUCCESS,
        "payload": {"rows": [{"id": 1}]},
    }
    base.update(overrides)
    return base


def test_evidence_create_defaults() -> None:
    request = EvidenceCreate(**_create_payload())

    assert request.quality == EvidenceQuality.UNKNOWN
    assert request.completeness == EvidenceCompleteness.UNKNOWN
    assert request.reproducible is False
    assert request.redaction_version == "none"
    assert request.redacted_payload is None


def test_evidence_create_rejects_unknown_field_and_invalid_enums() -> None:
    with pytest.raises(ValidationError):
        EvidenceCreate(**_create_payload(unexpected="x"))

    with pytest.raises(ValidationError):
        EvidenceCreate(**_create_payload(collection_status="BROKEN"))

    with pytest.raises(ValidationError):
        EvidenceCreate(**_create_payload(quality="HIGHEST"))


def test_degraded_status_accepts_unknown_quality_model_default() -> None:
    for status in (
        CollectionStatus.DENIED,
        CollectionStatus.TIMEOUT,
        CollectionStatus.NOT_SUPPORTED,
    ):
        request = EvidenceCreate(**_create_payload(collection_status=status))
        assert request.quality == EvidenceQuality.UNKNOWN


def test_payload_hash_is_order_independent_and_content_sensitive() -> None:
    left = {"b": 2, "a": 1, "nested": {"z": [1, 2], "a": "中文"}}
    right = {"nested": {"a": "中文", "z": [1, 2]}, "a": 1, "b": 2}

    digest = canonical_payload_hash(left)
    assert digest == canonical_payload_hash(right)
    assert len(digest) == 64
    assert digest != canonical_payload_hash({**left, "b": 3})


def test_evidence_model_keeps_payload_hash_and_redacted_payload() -> None:
    payload = {"rows": [{"id": 1}]}
    redacted = {"rows": [{"id": "***"}]}
    evidence = Evidence(
        evidence_id="ev_demo",
        tenant_id="tenant_local",
        task_id="task_demo",
        environment_id="env_mysql_local",
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w03-1",
        source_tool="get_db_version",
        observed_at="2026-02-01T00:00:00+00:00",
        collection_status=CollectionStatus.SUCCESS,
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
        redacted_payload=redacted,
    )

    assert evidence.payload_hash == canonical_payload_hash(payload)
    assert evidence.redacted_payload == redacted
