"""P1-BYOK 集成测试：`/v1/model-providers` 的租户隔离、掩码与 SSRF 拒绝。

验收口径（P1-BYOK-T1/T2）：

1. 两个租户可各自配置供应商，**互不可见**（跨租户 404）；
2. API 响应、错误详情里**零明文密钥**（只回 `sk-***1234`）；
3. `169.254.169.254` / 控制库主机等 base_url 被结构性拒绝（422/400 `SSRF_BLOCKED`）。

测试用 dev_local 主体（固定租户）走 API；第二个租户直接经仓库写入，
以验证"跨租户不可见"而不是"没有数据"。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from core.config import get_settings
from core.models.provider_registry import ModelProviderRepository

pytestmark = pytest.mark.integration

_MASTER = "integration-master-key-0123456789"


@pytest.fixture()
def byok_env(monkeypatch):
    monkeypatch.setenv("DG_SECRET_MASTER_KEY", _MASTER)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _repo() -> ModelProviderRepository:
    from database.mysql.connection import MysqlClient

    settings = get_settings()
    control = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    return ModelProviderRepository(
        control,
        master_key=_MASTER,
        resolver=lambda host, port: ("93.184.216.34",),
    )


def test_create_list_and_mask(byok_env) -> None:
    with TestClient(app) as client:
        created = client.post(
            "/v1/model-providers",
            json={
                "name": f"it-openai-{uuid.uuid4().hex[:8]}",
                "provider_type": "openai_compat",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-integration-secret-9876",
                "role_models": {"planner": "gpt-4o-mini"},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["api_key_preview"] == "sk-***9876"
        assert "sk-integration-secret-9876" not in created.text

        listed = client.get("/v1/model-providers")
        assert listed.status_code == 200
        assert any(item["provider_id"] == body["provider_id"] for item in listed.json())

        client.delete(f"/v1/model-providers/{body['provider_id']}")


def test_cross_tenant_is_404(byok_env) -> None:
    repo = _repo()
    foreign = repo.create(
        "tenant_other",
        name=f"other-{uuid.uuid4().hex[:8]}",
        provider_type="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="sk-other-00001111",
    )
    try:
        with TestClient(app) as client:
            assert (
                client.get(f"/v1/model-providers/{foreign.provider_id}").status_code == 404
            )
            assert (
                client.delete(f"/v1/model-providers/{foreign.provider_id}").status_code
                == 404
            )
    finally:
        repo.delete("tenant_other", foreign.provider_id)


def test_ssrf_metadata_endpoint_is_rejected(byok_env) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/model-providers",
            json={
                "name": "evil",
                "provider_type": "openai_compat",
                "base_url": "https://169.254.169.254/latest/meta-data",
                "api_key": "sk-evil",
            },
        )
        assert response.status_code in {400, 422}, response.text
        payload = json.loads(response.text)
        error = payload.get("error") or payload.get("detail") or {}
        code = error.get("error_code") or error.get("code")
        assert code == "SSRF_BLOCKED", payload
        assert "sk-evil" not in response.text
