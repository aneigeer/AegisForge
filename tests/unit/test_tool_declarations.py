"""W07 T1：`ToolDefinition` 三节声明的构造期门禁（不连接数据库）。

为什么必须有这一组用例：`ToolDefinition.model_config = ConfigDict(extra="forbid")`，
新字段一旦没有默认值就变成必填，会让全仓 27 处构造点（PG 7 个只读工具、全部
观察器、MySQL 观察工具与适配器声明）集体失败。因此这里同时断言两个方向：

1. **写**工具缺补偿/缺验证计划必须在构造期失败（fail closed，不存在"先跑起来再说"）；
2. **既有只读**工具必须仍可构造，且新字段取默认值（不破坏 W06 契约）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.base import CapabilityReport, DbEngine
from adapters.mysql.adapter import MySQLAdapter
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOL_DEFINITIONS
from adapters.registry import build_adapter_registry
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from database.observers import build_observer_registry
from tests.helpers.definitions import (
    compensation_declaration,
    verification_declarations,
    write_declarations,
)

_TARGET_SPEC = TargetObjectSpec(
    object_type="row",
    database_field="database",
    name_field="table",
    keys_field="keys",
)


def _write_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "stub_w07_declaration",
        "description": "W07 契约说明用写工具",
        "version": "1.0",
        "risk_level": "R2",
        "side_effect": "write",
        "permissions": ["mysql:write"],
        "required_role": "dg_rw",
        "operation_class": "repair",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "target_objects": [_TARGET_SPEC],
        "prepare_required": True,
    }
    payload.update(overrides)
    return payload


def test_write_tool_without_compensation_fails_construction() -> None:
    payload = _write_payload(
        compensation=None,
        verification_plan=verification_declarations(),
    )
    with pytest.raises(ValidationError) as excinfo:
        ToolDefinition(**payload)
    assert "compensation" in str(excinfo.value)


def test_write_tool_with_two_assertions_fails_construction() -> None:
    """写动作至少要 3 条断言：affected 行数 / 目标终值 / 前后快照可比对。"""
    payload = _write_payload(
        compensation=compensation_declaration(),
        verification_plan=verification_declarations()[:2],
    )
    with pytest.raises(ValidationError) as excinfo:
        ToolDefinition(**payload)
    assert "verification_plan" in str(excinfo.value)


def test_write_tool_missing_target_readback_fails_construction() -> None:
    """3 条断言但全部来自 step_observed：仍不满足"目标行终值必须回读"的要求。"""
    assertions = [
        {
            "assertion_id": f"only_observed_{index}",
            "metric": "affected_rows",
            "expected": "1",
            "source": "step_observed",
        }
        for index in range(3)
    ]
    payload = _write_payload(
        compensation=compensation_declaration(),
        verification_plan=assertions,
    )
    with pytest.raises(ValidationError) as excinfo:
        ToolDefinition(**payload)
    assert "target_readback" in str(excinfo.value)


def test_write_tool_with_three_assertions_constructs() -> None:
    definition = ToolDefinition(**_write_payload(**write_declarations()))
    assert definition.compensation is not None
    assert definition.compensation.is_rollback is False
    assert definition.compensation.requires_own_approval is True
    assert len(definition.verification_plan) == 3
    assert definition.prepare_required is True


def test_prepare_required_without_write_fails_construction() -> None:
    """`prepare_required=True` 只允许用在写操作上（读工具没有可准备的写动作）。"""
    with pytest.raises(ValidationError) as excinfo:
        ToolDefinition(
            name="stub_read_prepare",
            description="非法：只读工具声明 prepare_required",
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            prepare_required=True,
        )
    assert "prepare_required" in str(excinfo.value)


def _assert_contract_defaults(definition: ToolDefinition) -> None:
    assert definition.compensation is None, definition.name
    assert definition.verification_plan == [], definition.name
    assert definition.prepare_required is False, definition.name


class _FakeMysqlClient:
    """单元测试用客户端：只满足 `MySQLAdapter` 装配所需的最小接口。"""

    database_id = "lab_mysql_01"


def _mysql_capability_report() -> CapabilityReport:
    """固定能力报告：避免 `tool_specs()` 触发真实能力探测。"""
    return CapabilityReport(
        engine=DbEngine.MYSQL,
        database_id="lab_mysql_01",
        server_version="8.0.46",
        adapter_version="1.0",
        checked_at="2026-11-11T00:00:00.000Z",
    )


def test_existing_definitions_still_construct() -> None:
    """PG 只读工具、MySQL 观察器与两条装配点都必须不受 W07 新字段影响。"""
    mysql_adapter = MySQLAdapter(  # type: ignore[arg-type]
        _FakeMysqlClient(),
        capability_report=_mysql_capability_report(),
    )
    definitions = [spec.definition for spec in mysql_adapter.tool_specs()]
    assert definitions, "MySQL 适配器不应没有工具声明"
    for definition in definitions:
        assert isinstance(definition, ToolDefinition)
        _assert_contract_defaults(definition)

    for definition in PG_TOOL_DEFINITIONS.values():
        _assert_contract_defaults(definition)

    observed = build_observer_registry().definitions()
    assert observed
    for definition in observed:
        _assert_contract_defaults(definition)

    # 既有装配点：`build_adapter_registry` 只注册只读工具，
    # 因此其写集合恒为空（写工具只能经 `adapters/lab_assembly.py` 注册）。
    registry = build_adapter_registry(mysql_adapter)
    assert registry.definitions()
    assert not [item for item in registry.definitions() if item.side_effect == "write"]


def test_postgres_adapter_has_seven_read_only_tools() -> None:
    assert len(PG_TOOL_DEFINITIONS) == 7
    assert all(item.side_effect != "write" for item in PG_TOOL_DEFINITIONS.values())
