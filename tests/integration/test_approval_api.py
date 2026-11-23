"""审批/审计 API 集成测试（PRD §4/§6，T05 验收）。

权限矩阵在服务端强制执行：
- Operator 创建、Approver 裁决、Administrator **不能**裁决（职责分离）、Auditor 只读；
- 跨租户访问统一 404（不泄漏存在性）；
- `GET /v1/approvals/{id}` 返回 PRD §4 的全部披露字段，且与 `action_digest` 同源。
"""

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
from core.errors import ErrorCode
from core.security.passwords import hash_password
from core.security.roles import Role
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlClient
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

TOOL_NAME = "stub_api_r2_write"
PARAMS = {"database": "dg_lab", "table": "orders", "keys": ["id=1001"], "status": "PAID"}

_CLEANUP_TABLES = (
    "step_observed",
    "step_prepared",
    "approval_consumptions",
    "approvals",
    "task_events",
    "tool_calls",
    "task_steps",
    "checkpoints",
    "execution_envelopes",
    "worker_leases",
    "tasks",
)


class _R2WriteTool:
    """API 测试桩：R2 写工具（仅存在于测试代码）。"""

    def __init__(self) -> None:
        self.definition = ToolDefinition(
            name=TOOL_NAME,
            description="API 测试用 R2 写工具",
            version="1.0",
            risk_level="R2",
            side_effect="write",
            idempotent=False,
            permissions=["mysql:write"],
            required_role="dg_rw",
            operation_class="repair",
            db_engines=["mysql"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            target_objects=[
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="table",
                    keys_field="keys",
                )
            ],
            # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
            **write_declarations(),
        )

    def run(self, context: Any, params: Any = None) -> Any:  # pragma: no cover
        raise AssertionError("API 用例不执行工具")


@pytest.fixture
def token_client(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DG_AUTH_MODE", "token")
    get_settings.cache_clear()
    application = create_app()
    try:
        with TestClient(application) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def approval_env(token_client, monkeypatch, admin_client) -> Iterator[SimpleNamespace]:
    """两个租户 + Operator/Approver/Administrator/Auditor 账户 + R2 写工具桩。"""
    registry = ToolRegistry()
    registry.register(_R2WriteTool())
    monkeypatch.setattr("apps.api.deps.build_observer_registry", lambda: registry)

    suffix = uuid4().hex[:10]
    auth = AuthRepository(admin_client)
    tenants: dict[str, dict[str, Any]] = {}
    task_ids: list[str] = []

    def _provision(label: str) -> dict[str, Any]:
        tenant_id = f"apr_tenant_{label}_{suffix}"
        environment_id = f"env_apr_{label}_{suffix}"
        admin_client.execute(
            "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
            "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
            (tenant_id, f"Approval API {label} {suffix}"),
        )
        admin_client.execute(
            "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
            "db_version, network_id, image_digest, config_version, status, created_at) "
            "VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'apr-api-test', 'LAB', "
            "UTC_TIMESTAMP(6))",
            (environment_id, tenant_id),
        )
        users: dict[str, tuple[str, str]] = {}
        for role in (Role.OPERATOR, Role.APPROVER, Role.ADMINISTRATOR, Role.AUDITOR):
            user_id = f"apr_{label}_{role.value.lower()}_{suffix}"
            password = f"Aa1!{secrets.token_hex(10)}"
            auth.create_or_update_user(user_id, f"APR {role.value}", hash_password(password))
            auth.upsert_membership(tenant_id, user_id, role)
            users[role.value] = (user_id, password)
        return {
            "tenant_id": tenant_id,
            "environment_id": environment_id,
            "users": users,
        }

    tenants["main"] = _provision("main")
    tenants["other"] = _provision("other")

    try:
        yield SimpleNamespace(
            client=token_client,
            admin_client=admin_client,
            tenants=tenants,
            task_ids=task_ids,
            suffix=suffix,
        )
    finally:
        for task_id in task_ids:
            for table in _CLEANUP_TABLES:
                admin_client.execute(
                    f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
                )
        for tenant in tenants.values():
            admin_client.execute(
                "DELETE FROM dg_system.auth_sessions WHERE tenant_id = %s",
                (tenant["tenant_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.memberships WHERE tenant_id = %s",
                (tenant["tenant_id"],),
            )
            for user_id, _ in tenant["users"].values():
                admin_client.execute(
                    "DELETE FROM dg_system.users WHERE user_id = %s", (user_id,)
                )
            admin_client.execute(
                "DELETE FROM dg_system.environments WHERE environment_id = %s",
                (tenant["environment_id"],),
            )
            admin_client.execute(
                "DELETE FROM dg_system.tenants WHERE tenant_id = %s",
                (tenant["tenant_id"],),
            )


def _login(env: SimpleNamespace, role: Role, label: str = "main") -> dict[str, str]:
    tenant = env.tenants[label]
    user_id, password = tenant["users"][role.value]
    response = env.client.post(
        "/v1/auth/login",
        json={"username": user_id, "password": password, "tenant_id": tenant["tenant_id"]},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _make_task(env: SimpleNamespace, control_client, label: str = "main") -> str:
    tenant = env.tenants[label]
    operator_id = tenant["users"][Role.OPERATOR.value][0]
    service = TaskService(TaskRepository(control_client), ToolRegistry())
    task = service.create_task(
        "审批 API 用例",
        tenant_id=tenant["tenant_id"],
        environment_id=tenant["environment_id"],
        created_by=operator_id,
    )
    env.task_ids.append(task.task_id)
    return task.task_id


def _create_approval(env: SimpleNamespace, task_id: str, label: str = "main"):
    headers = _login(env, Role.OPERATOR, label)
    return env.client.post(
        "/v1/approvals",
        json={
            "task_id": task_id,
            "tool_name": TOOL_NAME,
            "params": dict(PARAMS),
            "operator_note": "API 用例",
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 创建与披露完整性
# ---------------------------------------------------------------------------


def test_operator_creates_approval_with_full_disclosure(
    approval_env, control_client
) -> None:
    task_id = _make_task(approval_env, control_client)
    response = _create_approval(approval_env, task_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    assert len(body["action_digest"]) == 64
    assert body["environment_fingerprint"]["db_engine"] == "mysql"
    assert body["policy_version"] == get_settings().policy_version
    assert body["missing_required_fields"] == []
    assert body["covered_by_action_digest"] is True

    disclosure = body["disclosure"]
    for section in (
        "操作",
        "目标对象",
        "影响面",
        "风险等级",
        "前置条件",
        "补偿方案",
        "验证计划",
        "有效期",
        "绑定信息",
    ):
        assert section in disclosure, section
    # 展示内容与摘要同源
    assert disclosure["绑定信息"]["action_digest"] == body["action_digest"]
    assert disclosure["操作"]["action_descriptor"]["parameters"] == PARAMS
    # W07：三节来自创建时刻落库的快照，必须 provided=true，且不再出现
    # `NOT_PERSISTED_IN_W06`（AC-01）。
    assert disclosure["风险等级"]["provided"] is True
    assert disclosure["补偿方案"]["provided"] is True
    assert disclosure["验证计划"]["provided"] is True
    assert "NOT_PERSISTED_IN_W06" not in response.text
    assert disclosure["风险等级"]["effective_risk"] == "R2"
    assert disclosure["风险等级"]["factors"], "风险因子不得为空（必须可解释）"
    assert disclosure["风险等级"]["source"] == "risk_resolver"
    assert disclosure["补偿方案"]["is_rollback"] is False
    assert disclosure["补偿方案"]["requires_own_approval"] is True
    assert disclosure["补偿方案"]["action"], "补偿动作必须有事实来源"
    assert len(disclosure["验证计划"]["assertions"]) >= 2
    assert disclosure["验证计划"]["assertion_count"] == len(
        disclosure["验证计划"]["assertions"]
    )
    assert disclosure["影响面"]["reversible"] is True
    assert disclosure["影响面"]["affects_other_tasks"] is False
    assert disclosure["影响面"]["missing"] == []
    # 顶层三件套：审批人可核对"页面显示的就是落库的那份"
    assert len(disclosure["绑定信息"]["disclosure_digest"]) == 64
    assert disclosure["绑定信息"]["disclosure_version"] == "w07-v1"
    assert disclosure["绑定信息"]["disclosed_at"]


def test_operator_cannot_create_for_foreign_task(approval_env, control_client) -> None:
    other_task = _make_task(approval_env, control_client, label="other")
    response = _create_approval(approval_env, other_task, label="main")
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == ErrorCode.TASK_NOT_FOUND.value


# ---------------------------------------------------------------------------
# 裁决权限矩阵
# ---------------------------------------------------------------------------


def test_approver_can_approve(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.APPROVER)

    response = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/approve",
        json={"reason": "影响面明确"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "APPROVED"
    assert body["approver_id"] == approval_env.tenants["main"]["users"][Role.APPROVER.value][0]
    assert body["decision_reason"] == "影响面明确"


def test_administrator_cannot_decide(approval_env, control_client) -> None:
    """职责分离：Administrator 没有 approval:decide（W06 不得为此新增权限）。"""
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.ADMINISTRATOR)

    response = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/approve",
        json={},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value


def test_operator_cannot_decide(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.OPERATOR)

    response = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/approve", json={}, headers=headers
    )
    assert response.status_code == 403


def test_auditor_reads_audit_chain_but_not_approval_detail(
    approval_env, control_client
) -> None:
    """PRD §6 权限矩阵：Auditor 只有 `audit:read`，没有 `approval:read`。

    审计员通过审计链（事件）证明"发生了什么"，不通过审批单详情；
    这样职责与最小权限都成立（W06 不得为此新增权限）。
    """
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.AUDITOR)

    events = approval_env.client.get(
        f"/v1/audit/events?task_id={task_id}", headers=headers
    )
    assert events.status_code == 200, events.text
    assert any(
        item["payload"].get("approval_id") == created["approval_id"]
        for item in events.json()["items"]
    )

    denied_detail = approval_env.client.get(
        f"/v1/approvals/{created['approval_id']}", headers=headers
    )
    assert denied_detail.status_code == 403

    denied_decide = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/approve", json={}, headers=headers
    )
    assert denied_decide.status_code == 403


def test_cross_tenant_detail_is_not_found(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.APPROVER, label="other")

    response = approval_env.client.get(
        f"/v1/approvals/{created['approval_id']}", headers=headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["error_code"] == ErrorCode.APPROVAL_NOT_FOUND.value


# ---------------------------------------------------------------------------
# 拒绝、撤销与列表
# ---------------------------------------------------------------------------


def test_reject_requires_reason(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.APPROVER)

    empty = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/reject", json={"reason": ""}, headers=headers
    )
    assert empty.status_code == 422

    rejected = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/reject",
        json={"reason": "影响面不明确"},
        headers=headers,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["decision_reason"] == "影响面不明确"


def test_requester_can_revoke(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.OPERATOR)

    revoked = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/revoke",
        json={"reason": "参数有误"},
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "REVOKED"

    approver_headers = _login(approval_env, Role.APPROVER)
    denied = approval_env.client.post(
        f"/v1/approvals/{created['approval_id']}/approve", json={}, headers=approver_headers
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["error_code"] == ErrorCode.APPROVAL_REVOKED.value


def test_list_returns_created_approval(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    created = _create_approval(approval_env, task_id).json()
    headers = _login(approval_env, Role.APPROVER)

    response = approval_env.client.get(
        f"/v1/approvals?task_id={task_id}&limit=10", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["approval_id"] for item in body["items"]] == [created["approval_id"]]
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# 审计端点
# ---------------------------------------------------------------------------


def test_audit_events_and_integrity(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    _create_approval(approval_env, task_id)
    headers = _login(approval_env, Role.AUDITOR)

    events = approval_env.client.get(
        f"/v1/audit/events?task_id={task_id}&limit=50", headers=headers
    )
    assert events.status_code == 200, events.text
    payload = events.json()
    types = [item["event_type"] for item in payload["items"]]
    assert "APPROVAL_REQUESTED" in types
    assert payload["chain_scope"] == "per-task"
    # 事件必须带链上哈希，便于独立复核
    assert all(len(item["event_hash"]) == 64 for item in payload["items"])

    verified = approval_env.client.post(
        "/v1/audit/integrity/verify", json={"task_id": task_id}, headers=headers
    )
    assert verified.status_code == 200, verified.text
    result = verified.json()
    assert result["ok"] is True
    assert result["checked"] >= 1
    assert result["broken_at_seq"] is None


def test_audit_requires_audit_read_permission(approval_env, control_client) -> None:
    task_id = _make_task(approval_env, control_client)
    headers = _login(approval_env, Role.OPERATOR)  # Operator 没有 audit:read
    response = approval_env.client.get(
        f"/v1/audit/events?task_id={task_id}", headers=headers
    )
    assert response.status_code == 403
    assert response.json()["error"]["error_code"] == ErrorCode.FORBIDDEN.value
