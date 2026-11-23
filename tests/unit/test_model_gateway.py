"""W04 单元测试：Model Gateway、适配器、超时与结构化输出。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from core.agent.contracts import Plan, PlannerDecision
from core.errors import (
    ConfigError,
    ErrorCode,
    ModelCallFailedError,
    ModelOutputInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from core.models.adapters.fake import FakeModelAdapter, HeuristicDiagnosticAdapter
from core.models.adapters.openai_compat import OpenAICompatibleAdapter
from core.models.contracts import (
    ModelMessage,
    ModelRequest,
    ModelUsage,
)
from core.models.degradation import DisabledModelGateway
from core.models.factory import build_model_gateway
from core.models.gateway import ModelGateway
from core.tools.contracts import ToolDefinition
from core.tools.contracts import TargetObjectSpec
from core.tools.registry import ToolRegistry
from database.observers import build_observer_registry
from database.observers.utils import utc_now_iso
from tests.helpers.definitions import write_declarations


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int
    note: str = ""


class _WriteTool:
    def __init__(self) -> None:
        self.definition = ToolDefinition(
            name="stub_write",
            description="测试写工具",
            risk_level="R1",
            side_effect="write",
            permissions=["mysql:write"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            target_objects=[
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="table",
                )
            ],
            # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
            **write_declarations(),
        )


def _messages(content: str = "hi") -> list[ModelMessage]:
    return [ModelMessage(role="user", content=content)]


def test_generate_returns_text_usage_and_version() -> None:
    adapter = FakeModelAdapter(
        ["hello"],
        usage=ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    gateway = ModelGateway(adapter)
    response = gateway.generate(_messages(), purpose="unit")
    assert response.text == "hello"
    assert response.provider == "fake"
    assert response.adapter_version
    assert "fake-diagnostic-1" in response.model_version
    assert response.usage.total_tokens == 15
    assert response.latency_ms >= 0


def test_script_exhausted_is_model_call_failed() -> None:
    gateway = ModelGateway(FakeModelAdapter([]))
    with pytest.raises(ModelCallFailedError) as exc_info:
        gateway.generate(_messages())
    assert exc_info.value.code is ErrorCode.MODEL_CALL_FAILED


def test_adapter_exception_is_normalized() -> None:
    def boom(_: ModelRequest) -> str:
        raise RuntimeError("secret internal detail")

    gateway = ModelGateway(FakeModelAdapter(boom))
    with pytest.raises(ModelCallFailedError) as exc_info:
        gateway.generate(_messages())
    assert exc_info.value.code is ErrorCode.MODEL_CALL_FAILED
    assert "secret internal detail" not in str(exc_info.value.details)


def test_wall_clock_timeout() -> None:
    gateway = ModelGateway(FakeModelAdapter(["late"], delay_s=0.2), timeout_s=0.05)
    with pytest.raises(ModelTimeoutError) as exc_info:
        gateway.generate(_messages(), purpose="timeout-case")
    assert exc_info.value.code is ErrorCode.MODEL_TIMEOUT
    assert exc_info.value.details["timeout_s"] == 0.05


def test_empty_model_output_is_failed() -> None:
    gateway = ModelGateway(FakeModelAdapter([""]))
    with pytest.raises(ModelCallFailedError):
        gateway.generate(_messages())


def test_structured_output_parses_and_validates() -> None:
    gateway = ModelGateway(FakeModelAdapter(['{"value": 7, "note": "ok"}']))
    output = gateway.generate_structured(_messages(), Answer, purpose="structured")
    assert isinstance(output.parsed, Answer)
    assert output.parsed.value == 7
    assert output.data == {"value": 7, "note": "ok"}
    assert output.response.text


def test_invalid_json_is_model_output_invalid() -> None:
    gateway = ModelGateway(FakeModelAdapter(["not-json"]))
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        gateway.generate_structured(_messages(), Answer)
    assert exc_info.value.code is ErrorCode.MODEL_OUTPUT_INVALID


def test_schema_violation_is_model_output_invalid() -> None:
    gateway = ModelGateway(FakeModelAdapter(['{"value": "x"}']))
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        gateway.generate_structured(_messages(), Answer)
    assert any("value" in str(item["loc"]) for item in exc_info.value.details["errors"])


def test_tool_call_accepts_registered_read_tool() -> None:
    registry = build_observer_registry()
    gateway = ModelGateway(
        FakeModelAdapter(['{"tool_name": "get_locks", "arguments": {}, "reason": "查锁"}'])
    )
    selection = gateway.tool_call(_messages(), registry=registry)
    assert selection.tool_name == "get_locks"
    assert selection.arguments == {}
    assert selection.reason == "查锁"


def test_tool_call_rejects_unregistered_tool() -> None:
    registry = build_observer_registry()
    gateway = ModelGateway(
        FakeModelAdapter(['{"tool_name": "run_shell", "arguments": {}, "reason": "x"}'])
    )
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        gateway.tool_call(_messages(), registry=registry)
    assert "工具未注册" in str(exc_info.value.details["reason"])


def test_tool_call_rejects_write_tool_by_default() -> None:
    registry = ToolRegistry()
    registry.register(_WriteTool())
    gateway = ModelGateway(
        FakeModelAdapter(['{"tool_name": "stub_write", "arguments": {}, "reason": "x"}'])
    )
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        gateway.tool_call(_messages(), registry=registry)
    assert "写工具" in str(exc_info.value.details["reason"])


def test_tool_call_rejects_invalid_arguments() -> None:
    registry = build_observer_registry()
    gateway = ModelGateway(
        FakeModelAdapter(
            ['{"tool_name": "get_activity", "arguments": {"unknown_field": 1}, "reason": "x"}']
        )
    )
    with pytest.raises(ModelOutputInvalidError) as exc_info:
        gateway.tool_call(_messages(), registry=registry)
    assert exc_info.value.details["tool_name"] == "get_activity"


def test_usage_accumulates_and_resets() -> None:
    adapter = FakeModelAdapter(
        ["a", "b"],
        usage=ModelUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    gateway = ModelGateway(adapter)
    gateway.generate(_messages())
    gateway.generate(_messages())
    assert gateway.usage.total_tokens == 10
    gateway.reset_usage()
    assert gateway.usage.total_tokens == 0


def test_openai_compatible_success_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    adapter = OpenAICompatibleAdapter(
        "test-model",
        "https://model.example/v1",
        "test-key",
        transport=httpx.MockTransport(handler),
    )
    gateway = ModelGateway(adapter)
    response = gateway.generate(_messages())
    assert response.text == "ok"
    assert response.usage.total_tokens == 6
    assert response.model_version.startswith("openai_compat/test-model")


def test_openai_compatible_timeout_and_auth_errors() -> None:
    def timeout_handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    adapter = OpenAICompatibleAdapter(
        "m", "https://model.example/v1", "k", http_timeout_s=0.05,
        transport=httpx.MockTransport(timeout_handler),
    )
    with pytest.raises(ModelTimeoutError):
        ModelGateway(adapter).generate(_messages())

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    adapter = OpenAICompatibleAdapter(
        "m", "https://model.example/v1", "k", transport=httpx.MockTransport(unauthorized)
    )
    from core.errors import ModelUnavailableError

    with pytest.raises(ModelUnavailableError) as exc_info:
        ModelGateway(adapter).generate(_messages())
    assert exc_info.value.details["status_code"] == 401


def _tool_catalog() -> list[dict]:
    return [definition.model_dump(mode="json") for definition in build_observer_registry().definitions()]


def test_heuristic_adapter_plan_is_valid() -> None:
    adapter = HeuristicDiagnosticAdapter()
    request = ModelRequest(
        messages=[
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "request_type": "plan",
                        "context": {"goal": "订单表出现锁等待", "tools": _tool_catalog()},
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    )
    response = adapter.complete(request)
    plan = Plan.model_validate_json(response.text)
    assert [step.tool_name for step in plan.steps] == [
        "get_locks",
        "get_blocking_tree",
        "get_activity",
    ]
    assert all(step.risk_level == "R0" for step in plan.steps)
    assert plan.goal == "订单表出现锁等待"


def test_heuristic_adapter_assess_stops_after_evidence() -> None:
    adapter = HeuristicDiagnosticAdapter()
    plan = Plan(
        goal="g",
        steps=[
            {"title": "a", "tool_name": "get_locks", "params": {}, "expected_evidence": "e"},
            {"title": "b", "tool_name": "get_blocking_tree", "params": {}, "expected_evidence": "e"},
        ],
        budget={"max_steps": 2, "max_tool_calls": 2},
    )
    payload = {
        "request_type": "assess",
        "context": {"goal": "g", "tools": _tool_catalog()},
        "plan": plan.model_dump(mode="json"),
        "evidence": [{"evidence_id": "e1"}, {"evidence_id": "e2"}],
    }
    response = adapter.complete(
        ModelRequest(
            messages=[ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False))]
        )
    )
    decision = PlannerDecision.model_validate_json(response.text)
    assert decision.action == "stop"
    assert decision.conclusion


def test_factory_modes() -> None:
    base = {
        "model_name": "fake-diagnostic-1",
        "model_timeout_s": 5,
        "model_max_tokens": 100,
        "model_base_url": None,
        "model_api_key": None,
    }
    gateway = build_model_gateway(SimpleNamespace(model_provider="fake", **base))
    assert gateway.adapter.provider == "fake"

    with pytest.raises(ConfigError):
        build_model_gateway(SimpleNamespace(model_provider="openai_compat", **base))

    # W09 T05 / A9（设计 §6.5、PRD P0-W09-05）：provider=disabled 不再抛 ConfigError，
    # 改为返回占位网关；任何模型调用抛 ModelUnavailableError，由降级路径兜住。
    disabled = build_model_gateway(
        SimpleNamespace(model_provider="disabled", **base)
    )
    assert isinstance(disabled, DisabledModelGateway)
    with pytest.raises(ModelUnavailableError):
        disabled.generate([ModelMessage(role="user", content="ping")])
