"""W11 T06 单元测试：P0 门禁清单（AC-20 / AC-19 静态面）。

九项门禁（`GATE_IDS`）逐项静态判定，全部来自"导入 + 符号存在 + 结构事实"：

- 全在 → `present` 九项全 `True`、`count() == 9`；
- 把任一项置为缺失 → `GateRemovedError`（`code="GATE_REMOVED"`），并且该缺失
  **作为环境阻塞条目喂给 `publish_decision`** → `TECH_PREVIEW_ONLY` 且
  `failed_test_ids` 保持为空（门禁缺失不是红线失败，是"不能发 Alpha"的阻塞）；
- AC-19 静态面：PG 7 工具全 `read`、observer 10 工具全 `read`（两套工具面**不混写**）、
  写工具仍 `LAB+mysql` 单一、`policy.py` 的 R4/R3 拒绝仍生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.assurance.gates import (
    ALLOWED_GATEWAY_BUILDERS,
    GATE_IDS,
    GATE_REMOVED,
    GateInventory,
    GateRemovedError,
    _GATE_CHECKS,
    audit_gate_sources,
    check_gate_inventory,
)
from core.assurance.publish import Decision, ENVIRONMENT_BLOCK_IDS, publish_decision

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def inventory() -> GateInventory:
    return check_gate_inventory(_REPO_ROOT)


# ---------------------------------------------------------------------------
# 九项门禁
# ---------------------------------------------------------------------------


def test_gate_ids_are_exactly_nine_and_frozen() -> None:
    assert len(GATE_IDS) == 9
    assert GATE_IDS == (
        "ENV_ISOLATION",
        "FIXED_READONLY_TOOLS",
        "SERVER_SIDE_RISK",
        "HUMAN_APPROVAL",
        "UNIQUE_TOOL_GATEWAY",
        "RECOVERY_RECONCILIATION",
        "EVIDENCE_OWNERSHIP",
        "INDEPENDENT_VERIFIER",
        "SECURITY_NEGATIVE_SET",
    )


def test_all_nine_gates_are_present(inventory: GateInventory) -> None:
    assert inventory.count() == 9
    for gate in GATE_IDS:
        assert inventory.present[gate] is True, gate
    assert inventory.version == "w11-gates-v1"


def test_each_gate_check_is_callable_and_independently_true() -> None:
    assert set(_GATE_CHECKS) == set(GATE_IDS)
    for gate, check in _GATE_CHECKS.items():
        assert callable(check)
        assert check(_REPO_ROOT) is True, gate


@pytest.mark.parametrize("gate", GATE_IDS)
def test_removing_any_gate_raises_gate_removed(
    gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_GATE_CHECKS, gate, lambda root: False)
    with pytest.raises(GateRemovedError) as excinfo:
        check_gate_inventory(_REPO_ROOT)
    assert excinfo.value.code == GATE_REMOVED
    assert gate in excinfo.value.details["missing"]


def test_inventory_model_rejects_missing_gate() -> None:
    present = {gate: True for gate in GATE_IDS}
    present["HUMAN_APPROVAL"] = False
    with pytest.raises(GateRemovedError) as excinfo:
        GateInventory(present=present)
    assert excinfo.value.code == GATE_REMOVED


def test_inventory_model_rejects_unregistered_gate() -> None:
    present = {gate: True for gate in GATE_IDS}
    present["EXTRA_GATE"] = True
    with pytest.raises(GateRemovedError):
        GateInventory(present=present)


@pytest.mark.parametrize(
    "gate_ids",
    [
        tuple(list(GATE_IDS)[:8]),          # 裁掉 1 项
        tuple(reversed(GATE_IDS)),          # 换序
        (),                                 # 空
        tuple(GATE_IDS) + ("EXTRA_GATE",),  # 多一项
    ],
    ids=["subset8", "reordered", "empty", "extra"],
)
def test_gate_inventory_rejects_trimmed_or_reordered_gate_ids(gate_ids: tuple[str, ...]) -> None:
    """L-7：`gate_ids` 可传参，但必须与冻结的 9 项**顺序与内容全等**。"""
    present = {gate: True for gate in GATE_IDS}
    with pytest.raises(ValidationError) as excinfo:
        GateInventory(gate_ids=gate_ids, present=present)
    assert "GATE_REMOVED" in str(excinfo.value)


def test_check_gate_inventory_always_returns_nine() -> None:
    inventory = check_gate_inventory(_REPO_ROOT)
    assert tuple(inventory.gate_ids) == GATE_IDS
    assert len(inventory.present) == 9
    assert inventory.count() == 9


def test_gate_removal_feeds_publish_decision_as_block() -> None:
    """门禁缺失 → 作为阻塞条目喂给纯函数：TECH_PREVIEW_ONLY 且不产生失败用例。"""
    present = {gate: True for gate in GATE_IDS}
    present["ENV_ISOLATION"] = False
    with pytest.raises(GateRemovedError) as excinfo:
        GateInventory(present=present)
    blocked = (*ENVIRONMENT_BLOCK_IDS, f"{GATE_REMOVED}:ENV_ISOLATION")
    decision = publish_decision(
        negative=(), concurrency=(), faults=(), environment_blocks=blocked
    )
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.failed_test_ids == ()
    assert decision.red_line_failures == ()
    assert f"{GATE_REMOVED}:ENV_ISOLATION" in decision.environment_blocks
    assert excinfo.value.code == GATE_REMOVED


# ---------------------------------------------------------------------------
# 静态事实
# ---------------------------------------------------------------------------


def test_gateway_builder_is_unique() -> None:
    facts = audit_gate_sources(_REPO_ROOT)
    assert tuple(facts["gateway_builders"]) == ALLOWED_GATEWAY_BUILDERS == ("workers/stack.py",)


def test_observer_tool_surface_is_readonly() -> None:
    facts = audit_gate_sources(_REPO_ROOT)
    assert facts["observer_tool_count"] == 10
    assert facts["observer_write_tools"] == []


def test_pg_tool_surface_is_readonly_and_separate() -> None:
    """AC-19（D-03）：PG 7 个工具与 observer 10 个是**两套工具面**，全 read。"""
    from adapters.postgres_mcp.tools import TOOL_DEFINITIONS

    assert len(TOOL_DEFINITIONS) == 7
    assert {item.side_effect for item in TOOL_DEFINITIONS.values()} == {"read"}
    from database.observers import build_observer_registry

    observers = build_observer_registry().definitions()
    assert len(observers) == 10
    assert {item.side_effect for item in observers} == {"read"}
    # 两套工具面"不混写"的证据：同名工具的引擎声明必须各自单一且不同
    observer_by_name = {item.name: item for item in observers}
    shared = set(TOOL_DEFINITIONS) & set(observer_by_name)
    assert shared, "两套工具面应当存在同名工具，否则本断言没有对照意义"
    for name in sorted(shared):
        assert TOOL_DEFINITIONS[name].db_engines == ["postgresql"], name
        assert observer_by_name[name].db_engines == ["mysql"], name
    assert {item.db_engines[0] for item in TOOL_DEFINITIONS.values()} == {"postgresql"}
    assert {item.db_engines[0] for item in observers} == {"mysql"}


def test_write_tool_surface_is_closed_and_lab_mysql_only() -> None:
    """P2-T3：写工具从 1 支扩到 3 支（note / 状态前进 / 状态回退），仍为闭集。

    断言改为"**逐字列出**的三支"，而不是"只有一支"——新增写工具必须同时改这里，
    以免有人悄悄往注册表里塞第四个写能力。
    """
    from adapters.lab_assembly import build_lab_registry, is_lab_mysql_target

    lab_row = {"status": "LAB", "tenant_id": "tenant_local"}
    prod_row = {"status": "PROD", "tenant_id": "tenant_local"}
    lab_registry = build_lab_registry(None, lab_row, "mysql")
    writes = [item.name for item in lab_registry.definitions() if item.side_effect == "write"]
    assert sorted(writes) == [
        "mysql_lab_advance_order_status",
        "mysql_lab_revert_order_status",
        "mysql_lab_update_order_note",
    ], "写工具集合必须严格等于这三支"
    assert is_lab_mysql_target(lab_row, "mysql") is True
    assert is_lab_mysql_target(prod_row, "mysql") is False
    assert is_lab_mysql_target(lab_row, "postgresql") is False
    # 非 LAB / 非 mysql 的注册表里没有写工具（fail closed）
    assert [
        item.name
        for item in build_lab_registry(None, prod_row, "mysql").definitions()
        if item.side_effect == "write"
    ] == []


def test_policy_still_denies_r4_and_r3() -> None:
    """AC-19：`core/security/policy.py` 的 R4/R3 拒绝仍生效（引用既有行为，不重写）。"""
    from core.security.models import EnvironmentContext, Principal
    from core.security.policy import PolicyEngine
    from core.security.roles import Role
    from core.tools.contracts import ToolDefinition

    engine = PolicyEngine()
    environment = EnvironmentContext(
        environment_id="env_w11_gate", tenant_id="tenant_w11_gate", db_engine="mysql", tier="LAB"
    )
    principal = Principal(
        user_id="user_w11_gate", tenant_id="tenant_w11_gate", roles=(Role.OPERATOR,)
    )

    def _definition(risk: str) -> ToolDefinition:
        return ToolDefinition(
            name="w11_gate_policy_probe",
            description="W11 门禁静态探针（不注册）",
            risk_level=risk,  # type: ignore[arg-type]
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

    r4 = engine.decide(principal, _definition("R4"), environment)
    r3 = engine.decide(principal, _definition("R3"), environment)
    assert r4.allowed is False and r4.reason == "R4_NOT_EXECUTABLE"
    assert r3.allowed is False and r3.reason == "R3_AGENT_EXECUTION_FORBIDDEN"


def test_gate_inventory_is_repeatable() -> None:
    first = check_gate_inventory(_REPO_ROOT)
    second = check_gate_inventory(_REPO_ROOT)
    assert first == second


def test_gate_module_has_no_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """门禁不可"临时关闭"：模块内没有 enabled/skip/force 之类开关形参。"""
    import inspect as _inspect

    from core.assurance import gates as gates_module

    for name, function in _inspect.getmembers(gates_module, _inspect.isfunction):
        if function.__module__ != gates_module.__name__:
            continue  # 只看本模块定义的函数（跳过 import 进来的）
        parameters = _inspect.signature(function).parameters
        for parameter in parameters:
            assert parameter not in {"force", "enabled", "skip", "allow", "strict"}, (name, parameter)


def test_audit_snapshot_is_structured() -> None:
    facts: dict[str, Any] = audit_gate_sources(_REPO_ROOT)
    assert set(facts) == {"gate_ids", "gateway_builders", "observer_tool_count", "observer_write_tools"}
    assert facts["gate_ids"] == list(GATE_IDS)
