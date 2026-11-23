"""写工具目标对象门禁的**运行时**断言（P0-03b）。

为什么不用静态 grep：`side_effect` 可能由参数注入（如 `_StubTool(side_effect=side_effect)`），
静态搜索只能列出候选，不能证明"不存在未声明目标对象的写工具"。
真正的证明是运行时的构造器门禁：任何 `side_effect="write"` 的 `ToolDefinition`
在**构造时**就必须带非空 `target_objects`，因此测试桩与生产工具都绕不过去。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.approval.digest import extract_target_objects
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from core.tools.registry import ToolRegistry
from database.observers import build_observer_registry
from tests.helpers.definitions import write_declarations


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "stub_write_gate",
        "description": "门禁用例桩",
        "version": "1.0",
        "risk_level": "R2",
        "side_effect": "write",
        "permissions": ["mysql:write"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "required_role": "dg_rw",
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    }
    payload.update(overrides)
    return payload


def test_direct_write_tool_without_target_objects_raises_value_error() -> None:
    """直接构造 = 最容易被"顺手"绕过的路径，必须在构造期就失败。"""
    with pytest.raises(ValidationError) as excinfo:
        ToolDefinition(**_base_payload())  # type: ignore[arg-type]
    assert "target_objects" in str(excinfo.value)


def test_direct_write_tool_with_explicit_empty_target_objects_raises() -> None:
    with pytest.raises(ValidationError):
        ToolDefinition(**_base_payload(target_objects=[]))  # type: ignore[arg-type]


def test_write_tool_with_declared_spec_is_accepted() -> None:
    definition = ToolDefinition(
        **_base_payload(  # type: ignore[arg-type]
            target_objects=[
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="table",
                )
            ]
        )
    )
    assert definition.target_objects
    assert extract_target_objects(
        definition, {"database": "dg_lab", "table": "orders"}
    ) == [
        {
            "object_type": "row",
            "database": "dg_lab",
            "schema": None,
            "name": "orders",
            "keys": [],
        }
    ]


def test_read_tool_without_target_objects_is_accepted() -> None:
    """只读工具不需要声明目标对象（否则会把观察类工具全部卡死）。"""
    definition = ToolDefinition(
        name="stub_read_gate",
        description="只读桩",
        side_effect="read",
        permissions=["mysql:read"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    assert definition.target_objects == []


def test_registry_cannot_hold_write_tool_without_target_objects() -> None:
    """注册表装配路径同样绕不过构造期门禁。"""
    registry = ToolRegistry()

    class _ForgedTool:
        def __init__(self) -> None:
            self.definition = {
                "name": "forged_write",
                "description": "伪造",
                "side_effect": "write",
            }

    with pytest.raises(TypeError):
        registry.register(_ForgedTool())


@pytest.mark.parametrize(
    "registry_builder",
    [build_observer_registry],
    ids=["mysql_observer_registry"],
)
def test_production_registry_write_tools_declare_target_objects(registry_builder) -> None:
    """生产注册表内所有写工具都必须声明目标对象抽取规则。"""
    definitions = registry_builder().definitions()
    assert definitions, "注册表不应为空（否则本断言无意义）"
    writes = [item for item in definitions if item.side_effect == "write"]
    for definition in writes:
        assert definition.target_objects, f"{definition.name} 是写工具但未声明 target_objects"
    # 当前 W06 生产工具全是只读 observer，写工具由测试桩提供证据（W07 才接入真实写工具）
    assert all(item.side_effect != "write" for item in definitions)
