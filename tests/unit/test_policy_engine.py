"""W05 Policy Engine 单元测试：风险解析、RBAC 授权与 fail closed。"""

from __future__ import annotations

from typing import Any

import pytest

from core.security.models import EnvironmentContext, Principal
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from tests.helpers.definitions import compensation_declaration, verification_declarations

DEFAULT_TOOL: dict[str, Any] = {
    "name": "get_db_version",
    "description": "读取数据库版本",
    "version": "1.0",
    "risk_level": "R0",
    "side_effect": "none",
    "permissions": [],
    "timeout_ms": 30_000,
    "idempotent": True,
    "audit_policy": "metadata",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "db_engines": ["mysql"],
    "allowed_environments": ["LAB"],
    "required_role": "dg_ro",
    "required_capabilities": [],
    "operation_class": "observe",
    "risk_floor": None,
    "row_limit": 500,
    # P0-03b：写工具必须声明目标对象抽取规则（read/none 工具留空即可）。
    "target_objects": [
        {
            "object_type": "row",
            "database_field": "database",
            "name_field": "table",
            "keys_field": "keys",
        }
    ],
    # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
    "compensation": compensation_declaration(),
    "verification_plan": verification_declarations(),
}


def make_principal(
    role: Role,
    *,
    user_id: str = "user_a",
    tenant_id: str = "tenant_a",
) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(role,))


def make_environment(
    *, tier: str = "LAB", tenant_id: str = "tenant_a"
) -> EnvironmentContext:
    return EnvironmentContext(
        environment_id=f"env_{tier.lower()}",
        tenant_id=tenant_id,
        db_engine="mysql",
        tier=tier,  # type: ignore[arg-type]
    )


def make_tool(**overrides: Any) -> ToolDefinition:
    payload = dict(DEFAULT_TOOL)
    payload.update(overrides)
    return ToolDefinition(**payload)


def make_engine() -> PolicyEngine:
    return PolicyEngine()


# ---------------------------------------------------------------------------
# 基础风险
# ---------------------------------------------------------------------------


def test_r0_is_allowed_for_operator_in_lab() -> None:
    engine = make_engine()
    decision = engine.decide(
        make_principal(Role.OPERATOR),
        make_tool(),
        make_environment(),
    )

    assert decision.allowed is True
    assert decision.effective_risk == "R0"
    assert decision.reason == "ALLOWED"
    assert decision.required_approval is False
    assert decision.deny_code is None
    assert decision.policy_version == PolicyEngine().policy_version
    assert decision.risk is not None
    assert decision.risk.effective_risk == "R0"
    assert decision.details == {
        "tool_name": "get_db_version",
        "tool_version": "1.0",
        "environment_id": "env_lab",
        "tenant_id": "tenant_a",
    }


@pytest.mark.parametrize(
    "role",
    [Role.OPERATOR, Role.ADMINISTRATOR],
    ids=lambda role: role.value,
)
def test_r1_is_allowed_for_operator_and_admin_in_lab(role: Role) -> None:
    decision = make_engine().decide(
        make_principal(role),
        make_tool(risk_level="R1"),
        make_environment(tier="LAB"),
    )

    assert decision.allowed is True
    assert decision.effective_risk == "R1"
    assert decision.required_approval is False
    assert decision.deny_code is None


def test_prod_target_is_denied_with_risk_at_least_r2() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0"),
        make_environment(tier="PROD"),
    )

    assert decision.allowed is False
    assert decision.reason == "PROD_TARGET_NOT_ALLOWED"
    assert decision.deny_code == "POLICY_DENIED"
    assert decision.effective_risk == "R2"
    assert decision.required_approval is False


# ---------------------------------------------------------------------------
# 高风险闭锁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [Role.OPERATOR, Role.ADMINISTRATOR],
    ids=lambda role: role.value,
)
def test_r2_always_requires_approval_and_is_not_allowed(role: Role) -> None:
    decision = make_engine().decide(
        make_principal(role),
        make_tool(risk_level="R2"),
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.effective_risk == "R2"
    assert decision.required_approval is True
    # W06 契约：R2 必须显式声明 APPROVAL_REQUIRED，且策略层永不放行。
    assert decision.reason == "R2_REQUIRES_APPROVAL"
    assert decision.deny_code == "APPROVAL_REQUIRED"


def test_r3_is_forbidden_for_agent_execution() -> None:
    decision = make_engine().decide(
        make_principal(Role.ADMINISTRATOR),
        make_tool(risk_level="R3"),
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.effective_risk == "R3"
    assert decision.reason == "R3_AGENT_EXECUTION_FORBIDDEN"
    assert decision.required_approval is False
    assert decision.deny_code == "POLICY_DENIED"


def test_r4_is_never_executable() -> None:
    decision = make_engine().decide(
        make_principal(Role.ADMINISTRATOR),
        make_tool(risk_level="R4"),
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.effective_risk == "R4"
    assert decision.reason == "R4_NOT_EXECUTABLE"
    assert decision.required_approval is False
    assert decision.deny_code == "POLICY_DENIED"


# ---------------------------------------------------------------------------
# 模型风险提示：只升不降
# ---------------------------------------------------------------------------


def test_model_hint_cannot_lower_r2_to_r0() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R2"),
        make_environment(),
        model_risk_hint="R0",
    )

    assert decision.effective_risk == "R2"
    assert decision.allowed is False
    assert decision.required_approval is True
    assert decision.risk is not None
    assert decision.risk.model_hint == "R0"
    assert decision.risk.model_hint_accepted is False
    assert any("只升不降" in note for note in decision.risk.notes)


def test_model_hint_can_raise_r0_to_r4_and_deny() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0"),
        make_environment(),
        model_risk_hint="R4",
    )

    assert decision.effective_risk == "R4"
    assert decision.allowed is False
    assert decision.reason == "R4_NOT_EXECUTABLE"
    assert decision.risk is not None
    assert decision.risk.model_hint == "R4"
    assert decision.risk.model_hint_accepted is True


def test_invalid_model_hint_is_ignored_and_noted() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0"),
        make_environment(),
        model_risk_hint="R9",  # type: ignore[arg-type]
    )

    assert decision.allowed is True
    assert decision.effective_risk == "R0"
    assert decision.risk is not None
    assert decision.risk.model_hint is None
    assert any("非法" in note for note in decision.risk.notes)


# ---------------------------------------------------------------------------
# RBAC 授权
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [Role.VIEWER, Role.AUDITOR, Role.APPROVER],
    ids=lambda role: role.value,
)
def test_roles_without_adapter_invoke_are_denied(role: Role) -> None:
    decision = make_engine().decide(
        make_principal(role),
        make_tool(risk_level="R0"),
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.reason == "MISSING_ADAPTER_INVOKE"
    assert decision.deny_code == "FORBIDDEN"


# ---------------------------------------------------------------------------
# 风险下限与环境约束
# ---------------------------------------------------------------------------


def test_risk_floor_raises_effective_risk_to_r2() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0", risk_floor="R2"),
        make_environment(),
    )

    assert decision.effective_risk == "R2"
    assert decision.allowed is False
    assert decision.required_approval is True
    # W06 契约：策略只声明"需要审批"，绝不因审批存在而放行。
    assert decision.reason == "R2_REQUIRES_APPROVAL"
    assert decision.deny_code == "APPROVAL_REQUIRED"
    assert decision.policy_version == PolicyEngine().policy_version


def test_write_side_effect_raises_risk_to_r2() -> None:
    decision = make_engine().decide(
        make_principal(Role.ADMINISTRATOR),
        make_tool(risk_level="R0", side_effect="write"),
        make_environment(),
    )

    assert decision.effective_risk == "R2"
    assert decision.allowed is False
    assert decision.required_approval is True
    assert decision.reason == "R2_REQUIRES_APPROVAL"
    assert decision.deny_code == "APPROVAL_REQUIRED"


def test_environment_allow_list_blocks_non_matching_tier() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(allowed_environments=["PROD"]),
        make_environment(tier="LAB"),
    )

    assert decision.allowed is False
    assert decision.reason == "ENVIRONMENT_NOT_ALLOWED"
    assert decision.deny_code == "POLICY_DENIED"


def test_wildcard_environment_allow_list_is_accepted_in_lab() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(allowed_environments=["*"]),
        make_environment(tier="LAB"),
    )

    assert decision.allowed is True


# ---------------------------------------------------------------------------
# fail closed、版本与 target_objects
# ---------------------------------------------------------------------------


def test_unknown_operation_class_fails_closed() -> None:
    weird = ToolDefinition.model_construct(
        name="weird_tool",
        description="x",
        version="1.0",
        risk_level="R0",
        side_effect="none",
        permissions=[],
        timeout_ms=30_000,
        idempotent=True,
        audit_policy="metadata",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        allowed_environments=["LAB"],
        required_role="dg_ro",
        required_capabilities=[],
        operation_class="drop",
        risk_floor=None,
        row_limit=500,
    )

    decision = make_engine().decide(
        make_principal(Role.ADMINISTRATOR),
        weird,
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.reason == "UNKNOWN_OPERATION_CLASS"
    assert decision.deny_code == "POLICY_DENIED"


def test_missing_definition_fails_closed() -> None:
    decision = make_engine().decide(
        make_principal(Role.ADMINISTRATOR),
        None,  # type: ignore[arg-type]
        make_environment(),
    )

    assert decision.allowed is False
    assert decision.effective_risk == "R4"
    assert decision.reason == "POLICY_MISSING"
    assert decision.deny_code == "POLICY_DENIED"
    assert decision.details["tool_name"] is None


def test_policy_version_is_returned_verbatim() -> None:
    engine = PolicyEngine(policy_version="w05-test")
    decision = engine.decide(
        make_principal(Role.OPERATOR),
        make_tool(),
        make_environment(),
    )

    assert decision.policy_version == "w05-test"


def test_target_objects_are_recorded_without_changing_risk() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0"),
        make_environment(),
        target_objects=["db_lab", "table_orders"],
    )

    assert decision.allowed is True
    assert decision.effective_risk == "R0"
    assert decision.details["target_objects"] == ["db_lab", "table_orders"]


def test_empty_target_objects_is_not_rejected() -> None:
    decision = make_engine().decide(
        make_principal(Role.OPERATOR),
        make_tool(risk_level="R0"),
        make_environment(),
        target_objects=[],
    )

    assert decision.allowed is True
    assert decision.effective_risk == "R0"
    assert decision.details["target_objects"] == []
