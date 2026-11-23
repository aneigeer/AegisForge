"""W05 RBAC 与对象级授权 API 集成测试（token 模式真实账户）。"""

from __future__ import annotations

import secrets
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.auth.repository import AuthRepository
from core.config import get_settings
from core.errors import DatabaseQueryError, ErrorCode
from core.security.passwords import hash_password
from core.security.roles import Role
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolDefinition
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry

pytestmark = pytest.mark.integration


@pytest.fixture
def token_client(monkeypatch) -> Iterator[TestClient]:
    """token 认证模式的新 app；测试结束恢复到默认 dev_local。"""
    monkeypatch.setenv("DG_AUTH_MODE", "token")
    get_settings.cache_clear()
    application = create_app()
    try:
        with TestClient(application) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def dev_local_client(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DG_AUTH_MODE", "dev_local")
    get_settings.cache_clear()
    application = create_app()
    try:
        with TestClient(application) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def rbac(control_client: MysqlClient, admin_client: MysqlClient, token_client: TestClient):
    """准备唯一租户/环境与 Viewer/Operator/Approver/Auditor 账户。"""
    suffix = uuid4().hex[:10]
    tenant_id = f"rbac_api_{suffix}"
    environment_id = f"env_rbac_api_{suffix}"
    users: dict[str, tuple[str, str]] = {}
    task_ids: list[str] = []

    admin_client.execute(
        "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
        "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
        (tenant_id, f"RBAC API {suffix}"),
    )
    admin_client.execute(
        "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
        "db_version, network_id, image_digest, config_version, status, created_at) "
        "VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'rbac-api-test', 'LAB', "
        "UTC_TIMESTAMP(6))",
        (environment_id, tenant_id),
    )

    repository = AuthRepository(admin_client)
    for role in (Role.VIEWER, Role.OPERATOR, Role.APPROVER, Role.AUDITOR):
        user_id = f"rbac_{role.value.lower()}_{suffix}"
        password = f"Aa1!{secrets.token_hex(10)}"
        repository.create_or_update_user(
            user_id, f"RBAC {role.value}", hash_password(password)
        )
        repository.upsert_membership(tenant_id, user_id, role)
        users[role.value] = (user_id, password)

    try:
        yield SimpleNamespace(
            client=token_client,
            tenant_id=tenant_id,
            environment_id=environment_id,
            users=users,
            task_ids=task_ids,
        )
    finally:
        for task_id in task_ids:
            _delete_task_rows(admin_client, task_id)
        admin_client.execute(
            "DELETE FROM dg_system.auth_sessions WHERE tenant_id = %s", (tenant_id,)
        )
        admin_client.execute(
            "DELETE FROM dg_system.memberships WHERE tenant_id = %s", (tenant_id,)
        )
        for user_id, _ in users.values():
            admin_client.execute(
                "DELETE FROM dg_system.users WHERE user_id = %s", (user_id,)
            )
        admin_client.execute(
            "DELETE FROM dg_system.environments WHERE environment_id = %s",
            (environment_id,),
        )
        admin_client.execute(
            "DELETE FROM dg_system.tenants WHERE tenant_id = %s", (tenant_id,)
        )


def _delete_task_rows(admin_client: MysqlClient, task_id: str) -> None:
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


def _direct_task(
    control_client: MysqlClient,
    *,
    tenant_id: str = "tenant_local",
    environment_id: str = "env_mysql_local",
    created_by: str = "user_local",
    goal: str = "RBAC 测试任务",
) -> str:
    service = TaskService(TaskRepository(control_client), build_observer_registry())
    task = service.create_task(
        goal,
        tenant_id=tenant_id,
        environment_id=environment_id,
        created_by=created_by,
    )
    return task.task_id


def _login(rbac: SimpleNamespace, role: Role) -> dict[str, str]:
    user_id, password = rbac.users[role.value]
    response = rbac.client.post(
        "/v1/auth/login",
        json={
            "username": user_id,
            "password": password,
            "tenant_id": rbac.tenant_id,
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_viewer_can_read_but_cannot_create(rbac) -> None:
    headers = _login(rbac, Role.VIEWER)

    listed = rbac.client.get("/v1/tasks?limit=10", headers=headers)
    assert listed.status_code == 200, listed.text
    assert "items" in listed.json()

    created = rbac.client.post(
        "/v1/tasks", json={"goal": "Viewer 创建"}, headers=headers
    )
    assert created.status_code == 403, created.text
    assert created.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value


def test_auditor_can_read_but_cannot_create(rbac) -> None:
    headers = _login(rbac, Role.AUDITOR)

    listed = rbac.client.get("/v1/tasks?limit=10", headers=headers)
    assert listed.status_code == 200, listed.text

    created = rbac.client.post(
        "/v1/tasks", json={"goal": "Auditor 创建"}, headers=headers
    )
    assert created.status_code == 403, created.text
    assert created.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value


def test_approver_cannot_create_task(rbac) -> None:
    headers = _login(rbac, Role.APPROVER)
    response = rbac.client.post(
        "/v1/tasks", json={"goal": "Approver 创建"}, headers=headers
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value


def test_operator_create_ignores_client_tenant_and_controls_own_task(
    rbac, control_client: MysqlClient
) -> None:
    headers = _login(rbac, Role.OPERATOR)
    operator_id = rbac.users[Role.OPERATOR.value][0]

    created = rbac.client.post(
        "/v1/tasks",
        json={
            "goal": "Operator 自己的任务",
            "tenant_id": "tenant_local",  # 客户端跨租户声明必须被忽略
            "environment_id": rbac.environment_id,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    own_task_id = body["task_id"]
    rbac.task_ids.append(own_task_id)
    assert body["tenant_id"] == rbac.tenant_id
    assert body["tenant_id"] != "tenant_local"
    assert body["created_by"] == operator_id

    own_pause = rbac.client.post(
        f"/v1/tasks/{own_task_id}/pause", headers=headers
    )
    assert own_pause.status_code == 200, own_pause.text
    assert own_pause.json()["pause_requested"] is True

    other_task_id = _direct_task(
        control_client,
        tenant_id=rbac.tenant_id,
        environment_id=rbac.environment_id,
        created_by="user_rbac_other",
        goal="同租户他人任务",
    )
    rbac.task_ids.append(other_task_id)
    other_pause = rbac.client.post(
        f"/v1/tasks/{other_task_id}/pause", headers=headers
    )
    assert other_pause.status_code == 403, other_pause.text
    assert other_pause.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value


def test_client_supplied_role_and_approver_are_rejected(rbac) -> None:
    headers = _login(rbac, Role.OPERATOR)

    for extra in ({"role": "Administrator"}, {"approver_id": "user_someone"}):
        rejected = rbac.client.post(
            "/v1/tasks",
            json={
                "goal": "非法字段",
                "environment_id": rbac.environment_id,
                **extra,
            },
            headers=headers,
        )
        assert rejected.status_code == 422, rejected.text
        assert (
            rejected.json()["error"]["error_code"] == ErrorCode.REQUEST_INVALID.value
        )


def test_cross_tenant_task_is_404_for_get_and_pause(rbac, control_client: MysqlClient) -> None:
    headers = _login(rbac, Role.OPERATOR)
    foreign_task_id = _direct_task(control_client, goal="跨租户任务")
    rbac.task_ids.append(foreign_task_id)

    for method, path in (
        ("GET", f"/v1/tasks/{foreign_task_id}"),
        ("GET", f"/v1/tasks/{foreign_task_id}/events"),
        ("GET", f"/v1/tasks/{foreign_task_id}/evidence"),
        ("POST", f"/v1/tasks/{foreign_task_id}/pause"),
    ):
        response = rbac.client.request(method, path, headers=headers)
        assert response.status_code == 404, (path, response.text)
        assert (
            response.json()["error"]["error_code"] == ErrorCode.TASK_NOT_FOUND.value
        )


class _R2Tool:
    """策略测试桩：R2 操作必须被 PolicyEngine 拦下。"""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            name="test_r2_tool",
            description="R2 策略测试工具",
            risk_level="R2",
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            db_engines=["mysql"],
            operation_class="observe",
        )

    def run(self, context: Any, params: Any = None) -> Any:
        raise AssertionError("R2 工具不应被执行")


def test_r2_tool_call_is_denied_by_policy(
    dev_local_client: TestClient, admin_client: MysqlClient, monkeypatch
) -> None:
    registry = ToolRegistry()
    registry.register(_R2Tool())
    monkeypatch.setattr(
        "apps.api.routes.tasks.build_observer_registry", lambda: registry
    )

    created_ids: list[str] = []
    try:
        created = dev_local_client.post("/tasks", json={"goal": "R2 策略拒绝测试"})
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        created_ids.append(task_id)

        denied = dev_local_client.post(
            f"/tasks/{task_id}/tools/test_r2_tool", json={"params": {}}
        )
        assert denied.status_code == 403, denied.text
        body = denied.json()
        assert body["error"]["error_code"] == ErrorCode.POLICY_DENIED.value
        details = body["error"]["details"]
        assert details["effective_risk"] == "R2"
        assert details["required_approval"] is True
        # W06 契约：R2 用稳定的 APPROVAL_REQUIRED，不再用 POLICY_DENIED 一把梭。
        assert details["reason"] == "R2_REQUIRES_APPROVAL"
        assert details["deny_code"] == ErrorCode.APPROVAL_REQUIRED.value
        # legacy 路由不提供审批入口：响应里没有 approval_id，也没有产生工具调用记录。
        assert "approval_id" not in body["error"]
        assert "approval_id" not in details
        rows = admin_client.fetch_all(
            "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id = %s",
            (task_id,),
        )
        assert int(rows[0]["total"]) == 0
    finally:
        for task_id in created_ids:
            _delete_task_rows(admin_client, task_id)


def test_dev_local_administrator_regression(
    dev_local_client: TestClient, admin_client: MysqlClient
) -> None:
    created_ids: list[str] = []
    try:
        created = dev_local_client.post("/v1/tasks", json={"goal": "dev_local 回归"})
        assert created.status_code == 200, created.text
        body = created.json()
        created_ids.append(body["task_id"])
        assert body["tenant_id"] == "tenant_local"
        assert body["created_by"] == "user_local"

        paused = dev_local_client.post(f"/v1/tasks/{body['task_id']}/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["pause_requested"] is True
    finally:
        for task_id in created_ids:
            _delete_task_rows(admin_client, task_id)
