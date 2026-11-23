from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from core.config import get_settings

pytestmark = pytest.mark.integration


def test_healthz_returns_service_metadata() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "dbguardian"


def test_healthz_db_returns_readonly_identity() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz/db")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_id"] == "lab_mysql_01"
    assert body["server_version"].startswith("8.")
    assert body["transaction_read_only"] == 1
    assert body["collected_at"].endswith("+00:00")


def test_healthz_pg_returns_readonly_identity() -> None:
    if get_settings().pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN")
    with TestClient(app) as client:
        response = client.get("/healthz/pg")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_id"] == "lab_pg_01"
    assert body["server_version"].startswith(("12.", "16."))
    assert body["transaction_read_only"] == 1
    assert body["database"] == "dg_lab_pg"
    assert body["collected_at"].endswith("+00:00")
