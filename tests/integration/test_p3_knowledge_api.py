"""P3-T3 集成测试：知识工作台 API（列表 / 两级确认 / 晋升 / 撤销 / 租户隔离）。

用 dev_local 主体（固定租户、Administrator+Operator）走真实控制库；第二条知识
写入另一租户，用来证明"跨租户一律 404"而不是"恰好没有数据"。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from core.audit.service import AuditService
from core.config import get_settings
from core.memory.models import (
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
)
from core.memory.repository import KnowledgeRepository
from core.memory.service import MemoryService
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tasks.repository import TaskRepository
from database.mysql.connection import MysqlClient

pytestmark = pytest.mark.integration

TENANT = "tenant_local"


def _service() -> MemoryService:
    settings = get_settings()
    client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=500,
    )
    return MemoryService(
        KnowledgeRepository(client),
        gate=RedactionGate(DataRedactionEngine()),
        audit=AuditService(TaskRepository(client)),
    )


def _entry(knowledge_id: str, *, tenant_id: str = TENANT) -> KnowledgeEntry:
    return KnowledgeEntry(
        knowledge_id=knowledge_id,
        revision=1,
        tenant_id=tenant_id,
        kind=MemoryKind.SEMANTIC,
        status=KnowledgeStatus.PROPOSED,
        db_engine="mysql",
        version_min="8.0",
        version_max="8.4",
        adapter_version=None,
        capability_scope=(),
        environment_fingerprint_digest=None,
        risk_scope=(),
        evidence_ids=("ev_p3t3",),
        test_case_ids=(),
        validation_level=ValidationLevel.SINGLE_CASE,
        reviewed_by=None,
        reviewed_at=None,
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2035-01-01T00:00:00+00:00",
        source_license="internal",
        content={"symptom": "lock wait timeout on seed_orders"},
        redaction_version="redaction-v2",
    )


def _seeded(tenant_id: str = TENANT) -> str:
    knowledge_id = f"knw_p3t3_{uuid4().hex[:8]}"
    _service().record(_entry(knowledge_id, tenant_id=tenant_id), principal=None)
    return knowledge_id


def test_confirm_flow_and_promotion() -> None:
    knowledge_id = _seeded()
    with TestClient(app) as client:
        listed = client.get("/v1/knowledge", params={"status": "PROPOSED"})
        assert listed.status_code == 200, listed.text
        assert any(item["knowledge_id"] == knowledge_id for item in listed.json())

        stage1 = client.post(f"/v1/knowledge/{knowledge_id}/confirm", json={"stage": 1})
        assert stage1.status_code == 200, stage1.text
        body = stage1.json()
        assert body["status"] == "PROPOSED"
        assert body["is_multi_confirmed"] is False

        # 同一主体再确认阶段 2 → 服务层职责分离拒绝
        stage2_same = client.post(
            f"/v1/knowledge/{knowledge_id}/confirm", json={"stage": 2}
        )
        assert stage2_same.status_code >= 400, stage2_same.text
        assert "SAME_USER_TWO_STAGES" in stage2_same.text

        # 未完成两级确认时不得晋升
        review_early = client.post(
            f"/v1/knowledge/{knowledge_id}/review", json={"validation_level": "SINGLE_CASE"}
        )
        assert review_early.status_code >= 400
        assert "CONFIRMATION_INCOMPLETE" in review_early.text


def test_second_confirmation_by_another_principal_then_review() -> None:
    """API 只能用当前主体；第二个确认人走服务层不同 principal（与真实双人一致）。"""
    knowledge_id = _seeded()
    service = _service()
    with TestClient(app) as client:
        assert (
            client.post(f"/v1/knowledge/{knowledge_id}/confirm", json={"stage": 1}).status_code
            == 200
        )
    latest = service.repository.latest(knowledge_id, tenant_id=TENANT)
    assert latest is not None
    service.confirm(
        knowledge_id, latest.revision, stage=2, confirmed_by="user_approver"
    )
    with TestClient(app) as client:
        promoted = client.post(
            f"/v1/knowledge/{knowledge_id}/review", json={"validation_level": "SINGLE_CASE"}
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["status"] == "INDEXED"
        assert promoted.json()["is_indexable"] is True

        revoked = client.post(
            f"/v1/knowledge/{knowledge_id}/revoke", json={"reason": "P3-T3 测试撤销"}
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "REVOKED"


def test_cross_tenant_is_404() -> None:
    foreign = _seeded(tenant_id="tenant_p3t3_other")
    with TestClient(app) as client:
        assert client.get(f"/v1/knowledge/{foreign}").status_code == 404
        assert (
            client.post(f"/v1/knowledge/{foreign}/confirm", json={"stage": 1}).status_code
            == 404
        )


def test_unknown_knowledge_is_404() -> None:
    with TestClient(app) as client:
        assert client.get("/v1/knowledge/knw_does_not_exist").status_code == 404


def test_console_is_served_and_wired() -> None:
    """P3-T3：单页控制台可访问，且引用了知识工作台/供应商端点。"""
    with TestClient(app) as client:
        page = client.get("/console/")
        assert page.status_code == 200, page.text
        body = page.text
        assert "DBGuardian" in body
        assert "/v1/knowledge" in body
        assert "/v1/model-providers" in body
        assert "两级确认" in body
