"""W02 API 集成：双引擎固定工具经生产路由执行。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from core.config import get_settings

pytestmark = pytest.mark.integration


def test_mysql_tools_list_and_call() -> None:
    with TestClient(app) as client:
        listed = client.get("/adapters/mysql/tools")
        assert listed.status_code == 200, listed.text
        names = {item["name"] for item in listed.json()}
        assert len(names) == 7
        assert "get_connection_snapshot" in names

        response = client.post(
            "/adapters/mysql/tools/get_connection_snapshot",
            json={"params": {"limit": 10}},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in {"success", "partial"}
        assert body["source"]["engine"] == "mysql"
        assert body["observed_at"]

        unknown = client.post("/adapters/mysql/tools/run_shell", json={"params": {}})
        assert unknown.status_code == 200
        assert unknown.json()["status"] == "denied"
        assert unknown.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_postgres_tools_list_and_call() -> None:
    if get_settings().pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN")
    with TestClient(app) as client:
        listed = client.get("/adapters/postgresql/tools")
        assert listed.status_code == 200, listed.text
        items = {
            item["name"]: item for item in listed.json()
        }
        assert len(items) == 7
        assert items["get_query_stats"]["available"] is False
        assert "pg_stat_statements" in (items["get_query_stats"]["unavailable_reason"] or "")

        response = client.post(
            "/adapters/postgresql/tools/get_connection_snapshot",
            json={"params": {"limit": 10}},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in {"success", "partial"}
        assert body["source"]["engine"] == "postgresql"
        assert body["capability_ref"]
