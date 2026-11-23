"""`/v1` API 集成测试：默认租户、游标、trace_id、统一错误与跨租户 404。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.errors import is_retryable
from apps.api.main import app
from core.config import get_settings
from core.errors import DatabaseQueryError, ErrorCode
from core.evidence.models import CollectionStatus, EvidenceCreate
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(admin_client, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        try:
            admin_client.execute(
                "DELETE FROM dg_system.evidence WHERE task_id = %s", (task_id,)
            )
        except DatabaseQueryError:
            pass
        for table in ("task_events", "tool_calls", "task_steps", "checkpoints", "tasks"):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


def _create_task(client: TestClient, **overrides) -> dict:
    body = {"goal": "数据库只读诊断", "risk_level": "R0"}
    body.update(overrides)
    response = client.post("/v1/tasks", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _evidence_request(task) -> EvidenceCreate:
    return EvidenceCreate(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        environment_id=task.environment_id,
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w03-api",
        source_tool="get_db_version",
        observed_at="2026-02-01T00:00:00+00:00",
        collection_status=CollectionStatus.SUCCESS,
        payload={"rows": [{"id": 1}]},
    )


def test_create_get_list_ignores_client_tenant(client: TestClient) -> None:
    settings = get_settings()
    response = client.post(
        "/v1/tasks",
        json={"goal": "诊断", "tenant_id": "tenant_absent", "max_steps": 3},
        headers={"X-Trace-Id": "trace-create"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Trace-Id"] == "trace-create"
    body = response.json()

    assert body["tenant_id"] == settings.default_tenant_id
    assert body["tenant_id"] != "tenant_absent"
    assert body["environment_id"] == settings.default_environment_id
    assert body["max_steps"] == 3

    task_id = body["task_id"]
    fetched = client.get(
        f"/v1/tasks/{task_id}", headers={"X-Trace-Id": "trace-get"}
    )
    assert fetched.status_code == 200
    assert fetched.headers["X-Trace-Id"] == "trace-get"
    assert fetched.json()["task_id"] == task_id

    listed = client.get("/v1/tasks?limit=10")
    assert listed.status_code == 200
    payload = listed.json()
    assert "items" in payload and "next_cursor" in payload
    assert task_id in {item["task_id"] for item in payload["items"]}


def test_events_after_seq_and_evidence_listing(client: TestClient, control_client) -> None:
    created = _create_task(client)
    task_id = created["task_id"]

    settings = get_settings()
    repository = TaskRepository(control_client)
    TaskService(repository, ToolRegistry()).transition(
        task_id, TaskStatus.PLANNING
    )

    first = client.get(f"/v1/tasks/{task_id}/events?after_seq=0&limit=2")
    assert first.status_code == 200, first.text
    body = first.json()
    assert [item["seq"] for item in body["items"]] == [0, 1]
    assert body["next_seq"] == 1

    second = client.get(f"/v1/tasks/{task_id}/events?after_seq=1&limit=2")
    assert second.status_code == 200
    assert [item["seq"] for item in second.json()["items"]] == [2, 3]

    task = repository.get_task(task_id, tenant_id=settings.default_tenant_id)
    service = EvidenceService(EvidenceRepository(control_client), repository)
    evidence = service.create(_evidence_request(task))

    listed = client.get(f"/v1/tasks/{task_id}/evidence?limit=10")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [item["evidence_id"] for item in payload["items"]] == [
        evidence.evidence_id
    ]
    assert payload["next_cursor"] is None


def test_pause_resume_cancel_semantics(client: TestClient, control_client) -> None:
    settings = get_settings()
    repository = TaskRepository(control_client)
    service = TaskService(repository, ToolRegistry())

    created = _create_task(client, goal="暂停恢复取消")
    task_id = created["task_id"]
    service.transition(task_id, TaskStatus.PLANNING)

    paused = client.post(f"/v1/tasks/{task_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == TaskStatus.PAUSED.value
    assert paused.json()["pause_requested"] is True

    resumed = client.post(f"/v1/tasks/{task_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == TaskStatus.PLANNING.value
    assert resumed.json()["pause_requested"] is False

    cancelling = client.post(f"/v1/tasks/{task_id}/cancel")
    assert cancelling.status_code == 200
    assert cancelling.json()["cancel_requested"] is True
    assert cancelling.json()["status"] in {
        TaskStatus.CANCEL_REQUESTED.value,
        TaskStatus.CANCELLED.value,
    }

    pending = _create_task(client, goal="取消待执行")
    cancelled = client.post(f"/v1/tasks/{pending['task_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == TaskStatus.CANCELLED.value

    assert repository.get_task(task_id, tenant_id=settings.default_tenant_id)


def test_cross_tenant_access_returns_404_without_leaking(
    client: TestClient, control_client, admin_client
) -> None:
    other_tenant = "tenant_w03_other"
    other_env = "env_w03_other"
    control_client.execute(
        "INSERT IGNORE INTO dg_system.tenants (tenant_id, name, status, created_at) "
        "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
        (other_tenant, "W03 Other Tenant"),
    )
    control_client.execute(
        "INSERT IGNORE INTO dg_system.environments (environment_id, tenant_id, "
        "db_engine, db_version, network_id, image_digest, config_version, status, "
        "created_at) VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'w03-test', "
        "'LAB', UTC_TIMESTAMP(6))",
        (other_env, other_tenant),
    )
    other_task = TaskService(TaskRepository(control_client), ToolRegistry()).create_task(
        "其他租户任务", tenant_id=other_tenant, environment_id=other_env
    )

    try:
        for method, path in (
            ("GET", f"/v1/tasks/{other_task.task_id}"),
            ("GET", f"/v1/tasks/{other_task.task_id}/events"),
            ("GET", f"/v1/tasks/{other_task.task_id}/evidence"),
            ("POST", f"/v1/tasks/{other_task.task_id}/pause"),
        ):
            response = client.request(
                method, path, headers={"X-Trace-Id": "trace-cross"}
            )
            assert response.status_code == 404, (path, response.text)
            assert response.headers["X-Trace-Id"] == "trace-cross"
            body = response.json()
            assert body["status"] == "error"
            assert body["error"]["error_code"] == ErrorCode.TASK_NOT_FOUND.value
            assert body["error"]["retryable"] is False
            assert body["error"]["trace_id"] == "trace-cross"

        listed = client.get("/v1/tasks?limit=200").json()
        assert other_task.task_id not in {
            item["task_id"] for item in listed["items"]
        }
    finally:
        for table in ("task_events", "tool_calls", "task_steps", "checkpoints", "tasks"):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s",
                (other_task.task_id,),
            )
        admin_client.execute(
            "DELETE FROM dg_system.environments WHERE environment_id = %s",
            (other_env,),
        )
        admin_client.execute(
            "DELETE FROM dg_system.tenants WHERE tenant_id = %s", (other_tenant,)
        )


def test_request_validation_error_has_trace_and_retryable(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        json={"goal": "", "risk_level": "R9"},
        headers={"X-Trace-Id": "trace-invalid"},
    )
    assert response.status_code == 422, response.text
    assert response.headers["X-Trace-Id"] == "trace-invalid"
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["error_code"] == ErrorCode.REQUEST_INVALID.value
    assert body["error"]["retryable"] is False
    assert body["error"]["trace_id"] == "trace-invalid"
    assert body["error"]["details"]["errors"]


def test_trace_id_is_generated_and_openapi_is_31(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    generated = response.headers["X-Trace-Id"]
    assert generated.startswith("trace_")

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    schema = openapi.json()
    assert schema["openapi"].startswith("3.1")
    assert "/v1/tasks" in schema["paths"]


def test_retryable_error_code_mapping() -> None:
    for code in (
        ErrorCode.DB_UNAVAILABLE,
        ErrorCode.DB_TIMEOUT,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.MODEL_TIMEOUT,
    ):
        assert is_retryable(code) is True
    for code in (
        ErrorCode.TASK_NOT_FOUND,
        ErrorCode.REQUEST_INVALID,
        ErrorCode.EVIDENCE_INVALID,
    ):
        assert is_retryable(code) is False
