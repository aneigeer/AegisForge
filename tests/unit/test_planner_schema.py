"""W04 单元测试：Planner 结构化计划、非法输出与工具/参数校验。"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from core.agent.contracts import Plan
from core.agent.planner import Planner
from core.context.builder import ContextBuilder
from core.errors import ErrorCode, ModelOutputInvalidError, PlanInvalidError
from core.models.adapters.fake import FakeModelAdapter
from core.models.gateway import ModelGateway
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlIdentity
from database.observers import build_observer_registry
from tests.helpers.definitions import write_declarations


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StubTool:
    def __init__(self, name: str, *, risk_level: str = "R0", side_effect: str = "read") -> None:
        self.input_model = _NoParams
        self.definition = ToolDefinition(
            name=name,
            description="测试桩工具",
            risk_level=risk_level,
            side_effect=side_effect,
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            # P0-03b：写副作用工具必须声明目标对象抽取规则。
            target_objects=(
                [
                    TargetObjectSpec(
                        object_type="row",
                        database_field="database",
                        name_field="table",
                    )
                ]
                if side_effect == "write"
                else []
            ),
            # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
            **(write_declarations() if side_effect == "write" else {}),
        )


def _identity() -> MysqlIdentity:
    return MysqlIdentity(
        database_id="lab",
        user="dg_ro",
        database="dg_lab",
        server_version="8.0.46",
        version_comment="MySQL",
        transaction_read_only=1,
        collected_at="2026-11-06T00:00:00+00:00",
    )


def _bundle():
    registry = build_observer_registry()
    return ContextBuilder(registry).build(
        goal="订单表出现锁等待", identity=_identity(), read_only=True, budget={"max_steps": 3}
    )


def _plan_payload(steps: list[dict] | None = None) -> dict:
    return {
        "goal": "订单表出现锁等待",
        "assumptions": [],
        "steps": steps
        or [
            {
                "title": "查看当前锁",
                "tool_name": "get_locks",
                "params": {},
                "depends_on": [],
                "expected_evidence": "锁持有与等待关系",
                "risk_level": "R0",
            },
            {
                "title": "查看阻塞树",
                "tool_name": "get_blocking_tree",
                "params": {},
                "depends_on": [0],
                "expected_evidence": "阻塞链与根节点",
                "risk_level": "R0",
            },
        ],
        "stop_conditions": ["证据足够即停止"],
        "budget": {"max_steps": 2, "max_tool_calls": 2},
    }


def _planner(payload: str, registry: ToolRegistry | None = None, *, max_steps: int = 3, max_tool_calls: int = 3) -> tuple[Planner, FakeModelAdapter]:
    adapter = FakeModelAdapter([payload])
    gateway = ModelGateway(adapter)
    return Planner(gateway, registry or build_observer_registry(), max_steps=max_steps, max_tool_calls=max_tool_calls), adapter


def test_valid_plan_is_returned() -> None:
    planner, _ = _planner(json.dumps(_plan_payload()))
    plan, response = planner.create_plan(_bundle())
    assert [step.tool_name for step in plan.steps] == ["get_locks", "get_blocking_tree"]
    assert plan.steps[1].depends_on == [0]
    assert response.model_version.startswith("fake/")


def test_invalid_json_is_rejected_before_execution() -> None:
    planner, _ = _planner("```json not allowed```")
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        planner.create_plan(_bundle())
    assert exc_info.value.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_unregistered_tool_is_rejected() -> None:
    payload = _plan_payload(
        [
            {
                "title": "自由执行",
                "tool_name": "run_shell",
                "params": {},
                "expected_evidence": "x",
                "risk_level": "R0",
            }
        ]
    )
    planner, _ = _planner(json.dumps(payload))
    with pytest.raises(PlanInvalidError) as exc_info:
        planner.create_plan(_bundle())
    assert any("未注册" in item["message"] for item in exc_info.value.details["errors"])


@pytest.mark.parametrize(
    ("risk_level", "side_effect", "name"),
    [("R2", "read", "stub_r2"), ("R3", "read", "stub_r3"), ("R4", "read", "stub_r4"), ("R1", "write", "stub_write")],
)
def test_non_readonly_tools_are_rejected(risk_level: str, side_effect: str, name: str) -> None:
    registry = ToolRegistry()
    registry.register(_StubTool(name, risk_level=risk_level, side_effect=side_effect))
    payload = _plan_payload(
        [
            {
                "title": "执行",
                "tool_name": name,
                "params": {},
                "expected_evidence": "x",
                "risk_level": "R0",
            }
        ]
    )
    planner, _ = _planner(json.dumps(payload), registry)
    with pytest.raises(PlanInvalidError):
        planner.create_plan(_bundle())


def test_step_cannot_downgrade_tool_risk() -> None:
    registry = ToolRegistry()
    registry.register(_StubTool("stub_r1", risk_level="R1"))
    payload = _plan_payload(
        [
            {
                "title": "执行",
                "tool_name": "stub_r1",
                "params": {},
                "expected_evidence": "x",
                "risk_level": "R0",
            }
        ]
    )
    planner, _ = _planner(json.dumps(payload), registry)
    with pytest.raises(PlanInvalidError) as exc_info:
        planner.create_plan(_bundle())
    assert any("风险降级" in item["message"] for item in exc_info.value.details["errors"])


def test_invalid_params_are_rejected() -> None:
    payload = _plan_payload(
        [
            {
                "title": "活跃会话",
                "tool_name": "get_activity",
                "params": {"unknown_field": 1},
                "expected_evidence": "x",
                "risk_level": "R0",
            }
        ]
    )
    planner, _ = _planner(json.dumps(payload))
    with pytest.raises(PlanInvalidError) as exc_info:
        planner.create_plan(_bundle())
    assert any(item["field"].startswith("params") for item in exc_info.value.details["errors"])


def test_forward_and_self_dependencies_are_rejected() -> None:
    payload = _plan_payload(
        [
            {
                "title": "a",
                "tool_name": "get_locks",
                "params": {},
                "depends_on": [0],
                "expected_evidence": "x",
                "risk_level": "R0",
            },
            {
                "title": "b",
                "tool_name": "get_blocking_tree",
                "params": {},
                "depends_on": [1],
                "expected_evidence": "x",
                "risk_level": "R0",
            },
        ]
    )
    planner, _ = _planner(json.dumps(payload))
    with pytest.raises(PlanInvalidError):
        planner.create_plan(_bundle())


def test_step_and_budget_limits_are_enforced() -> None:
    steps = [
        {
            "title": f"step {i}",
            "tool_name": "get_locks",
            "params": {},
            "expected_evidence": "x",
            "risk_level": "R0",
        }
        for i in range(4)
    ]
    payload = _plan_payload(steps)
    payload["budget"] = {"max_steps": 4, "max_tool_calls": 4}
    planner, _ = _planner(json.dumps(payload), max_steps=3)
    with pytest.raises(PlanInvalidError) as exc_info:
        planner.create_plan(_bundle())
    messages = [item["message"] for item in exc_info.value.details["errors"]]
    assert any("超过" in message for message in messages)


def test_assess_stop_and_invalid_next_step() -> None:
    planner, _ = _planner(
        json.dumps({"action": "stop", "reason": "证据已足够", "conclusion": "完成观察"})
    )
    plan = Plan.model_validate(_plan_payload())
    decision, _ = planner.assess(_bundle(), plan, [])
    assert decision.action == "stop"
    assert decision.conclusion == "完成观察"

    planner, _ = _planner(
        json.dumps(
            {
                "action": "continue",
                "reason": "继续",
                "next_step": {
                    "title": "非法补充",
                    "tool_name": "run_shell",
                    "params": {},
                    "expected_evidence": "x",
                    "risk_level": "R0",
                },
            }
        )
    )
    with pytest.raises(PlanInvalidError):
        planner.assess(_bundle(), plan, [])


def test_propose_tool_call_returns_proposal() -> None:
    planner, _ = _planner(
        json.dumps({"tool_name": "get_locks", "arguments": {}, "reason": "补充锁证据"})
    )
    plan = Plan.model_validate(_plan_payload())
    proposal, response = planner.propose_tool_call(_bundle(), plan, [])
    assert proposal is not None
    assert proposal.tool_name == "get_locks"
    assert proposal.expected_evidence == "补充锁证据"
    assert response.model_version


def test_prompt_contains_security_notice_and_request_type() -> None:
    planner, adapter = _planner(json.dumps(_plan_payload()))
    planner.create_plan(_bundle())
    request = adapter.requests[0]
    assert request.messages[0].role == "system"
    assert "不可信数据" in request.messages[0].content
    assert "自由 SQL" in request.messages[0].content
    payload = json.loads(request.messages[1].content)
    assert payload["request_type"] == "plan"
    assert payload["context"]["goal"] == "订单表出现锁等待"
