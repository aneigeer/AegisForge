"""P1-T1 单元测试：角色路由、分角色用量与成本口径。

覆盖：

1. **路由**：`ModelRoleRouter` 按 `request.role` 分派；未映射角色回落默认适配器；
   `embedding` / 未知角色在**装配期**报 `ConfigError`（不拿 chat 适配器冒充向量模型）。
2. **用量**：`ModelGateway.usage_by_role()` 按角色累计，总量仍与逐次相加一致。
3. **成本（INV-06）**：未配置单价 → `cost_for()` / `total_cost()` 恒 `None`（**不估算**）；
   配置单价后按 prompt/completion 分别计价。
4. **装配**：`fake` 配角色映射 → `ConfigError`（单桩适配器无法按角色区分模型）；
   `openai_compat` 配角色映射 → 网关内部为 `ModelRoleRouter`，`describe()` 只暴露模型名。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.errors import ConfigError
from core.models.contracts import ModelRequest, ModelResponse, ModelRole, ModelUsage
from core.models.factory import build_model_gateway
from core.models.gateway import ModelGateway
from core.models.router import ModelRoleRouter


class _StubAdapter:
    """最小适配器桩：记录被调用次数与收到的模型名。"""

    provider = "stub"
    adapter_version = "0.0.1"

    def __init__(
        self,
        model_name: str,
        *,
        text: str = '{"ok": true}',
        finish_reason: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.model_version = f"stub/{model_name}"
        self._text = text
        self._finish_reason = finish_reason
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            text=self._text,
            model_version=self.model_version,
            provider=self.provider,
            adapter_version=self.adapter_version,
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=1.0,
            finish_reason=self._finish_reason,
        )


def _request(role: ModelRole) -> ModelRequest:
    from core.models.contracts import ModelMessage

    return ModelRequest(messages=[ModelMessage(role="user", content="ping")], role=role)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_router_dispatches_by_role() -> None:
    default = _StubAdapter("flagship-7b")
    small = _StubAdapter("small-3b")
    router = ModelRoleRouter(default, role_adapters={ModelRole.CLASSIFIER: small})

    router.complete(_request(ModelRole.PLANNER))
    assert len(default.calls) == 1 and not small.calls

    router.complete(_request(ModelRole.CLASSIFIER))
    assert len(small.calls) == 1
    assert router.model_for(ModelRole.CLASSIFIER) == "small-3b"
    assert router.model_for(ModelRole.PLANNER) == "flagship-7b"
    assert router.mapped_roles() == (ModelRole.CLASSIFIER,)
    assert router.describe() == {"classifier": "small-3b", "planner": "flagship-7b"}


def test_router_accepts_string_role_keys() -> None:
    default = _StubAdapter("flagship-7b")
    small = _StubAdapter("small-3b")
    router = ModelRoleRouter(default, role_adapters={"classifier": small})
    assert router.model_for("classifier") == "small-3b"


def test_router_rejects_embedding_mapping() -> None:
    with pytest.raises(ConfigError) as excinfo:
        ModelRoleRouter(_StubAdapter("a"), role_adapters={ModelRole.EMBEDDING: _StubAdapter("b")})
    assert excinfo.value.code.value == "CONFIG_INVALID"


def test_router_rejects_unknown_role() -> None:
    with pytest.raises(ConfigError):
        ModelRoleRouter(_StubAdapter("a"), role_adapters={"wizard": _StubAdapter("b")})


# ---------------------------------------------------------------------------
# 用量与成本
# ---------------------------------------------------------------------------


def test_gateway_records_usage_per_role() -> None:
    default = _StubAdapter("flagship-7b")
    small = _StubAdapter("small-3b")
    gateway = ModelGateway(
        ModelRoleRouter(default, role_adapters={ModelRole.CLASSIFIER: small}), timeout_s=5
    )

    gateway.generate([{"role": "user", "content": "a"}], role=ModelRole.PLANNER)
    gateway.generate([{"role": "user", "content": "b"}], role=ModelRole.PLANNER)
    gateway.generate([{"role": "user", "content": "c"}], role=ModelRole.CLASSIFIER)

    assert gateway.usage_for(ModelRole.PLANNER).total_tokens == 30
    assert gateway.usage_for(ModelRole.CLASSIFIER).total_tokens == 15
    assert gateway.usage.total_tokens == 45
    assert gateway.model_for(ModelRole.PLANNER) == "flagship-7b"
    assert gateway.model_for(ModelRole.CLASSIFIER) == "small-3b"
    # 路由确实换了适配器，且请求里带的是被路由的模型名。
    assert [call.model for call in default.calls] == ["flagship-7b", "flagship-7b"]
    assert [call.model for call in small.calls] == ["small-3b"]


def test_gateway_cost_is_none_without_pricing() -> None:
    gateway = ModelGateway(_StubAdapter("m"), timeout_s=5)
    gateway.generate([{"role": "user", "content": "a"}])
    assert gateway.cost_for(ModelRole.PLANNER) is None
    assert gateway.total_cost() is None


def test_gateway_cost_uses_configured_prices() -> None:
    gateway = ModelGateway(
        _StubAdapter("m"), timeout_s=5, input_price_per_1k=2.0, output_price_per_1k=8.0
    )
    gateway.generate([{"role": "user", "content": "a"}])  # 10 prompt / 5 completion
    cost = gateway.cost_for(ModelRole.PLANNER)
    assert cost == pytest.approx(10 / 1000 * 2.0 + 5 / 1000 * 8.0)
    assert gateway.total_cost() == pytest.approx(cost)


def test_gateway_reset_usage_clears_roles() -> None:
    gateway = ModelGateway(_StubAdapter("m"), timeout_s=5)
    gateway.generate([{"role": "user", "content": "a"}], role=ModelRole.CLASSIFIER)
    gateway.reset_usage()
    assert gateway.usage.total_tokens == 0
    assert gateway.usage_by_role() == {}


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "model_provider": "openai_compat",
        "model_name": "qwen2.5-7b-instruct-baseline:latest",
        "model_base_url": "http://127.0.0.1:11434/v1",
        "model_api_key": SimpleNamespace(get_secret_value=lambda: "local-noauth"),
        "model_timeout_s": 5.0,
        "model_max_tokens": 256,
        "model_role_models": {},
        "model_input_price_per_1k": None,
        "model_output_price_per_1k": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_factory_rejects_role_routing_for_fake_provider() -> None:
    settings = _settings(model_provider="fake", model_role_models={"classifier": "small"})
    with pytest.raises(ConfigError):
        build_model_gateway(settings)


def test_factory_builds_router_for_openai_compat() -> None:
    settings = _settings(
        model_role_models={"classifier": "qwen2.5-3b-instruct-q8_0-baseline:latest"}
    )
    gateway = build_model_gateway(settings)
    assert isinstance(gateway, ModelGateway)
    assert isinstance(gateway.adapter, ModelRoleRouter)
    assert gateway.adapter.describe() == {
        "classifier": "qwen2.5-3b-instruct-q8_0-baseline:latest",
        "planner": "qwen2.5-7b-instruct-baseline:latest",
    }
    # 路由视图只含模型名，不含任何密钥字段。
    assert "key" not in str(gateway.adapter.describe()).lower()


def test_factory_keeps_single_adapter_without_role_map() -> None:
    gateway = build_model_gateway(_settings())
    assert not isinstance(gateway.adapter, ModelRoleRouter)
    assert gateway.adapter.model_name == "qwen2.5-7b-instruct-baseline:latest"


# ---------------------------------------------------------------------------
# 结构化输出失败归因（P1-T1 / P1-T2 四类归因的输入）
# ---------------------------------------------------------------------------


class _OkModel(__import__("pydantic").BaseModel):
    ok: bool


def test_truncated_output_is_attributed_separately() -> None:
    """达到 max_tokens 上限导致 JSON 截断 → reason=TRUNCATED_OUTPUT（不是笼统 JSON 错）。"""
    from core.errors import ModelOutputInvalidError

    gateway = ModelGateway(
        _StubAdapter("m", text='{"ok": tr', finish_reason="length"), timeout_s=5
    )
    with pytest.raises(ModelOutputInvalidError) as excinfo:
        gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    details = excinfo.value.details
    assert details["reason"] == "TRUNCATED_OUTPUT"
    assert details["finish_reason"] == "length"


def test_malformed_json_is_attributed_as_json_invalid() -> None:
    from core.errors import ModelOutputInvalidError

    gateway = ModelGateway(_StubAdapter("m", text="not-json"), timeout_s=5)
    with pytest.raises(ModelOutputInvalidError) as excinfo:
        gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    assert excinfo.value.details["reason"] == "JSON_INVALID"


def test_structured_call_carries_json_schema() -> None:
    """严格模式：结构化请求必须把目标模型的 JSON Schema 一并下发。"""
    adapter = _StubAdapter("m")
    gateway = ModelGateway(adapter, timeout_s=5)
    gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    assert adapter.calls[0].response_schema is not None
    assert adapter.calls[0].response_schema["title"] == "_OkModel"


def test_schema_can_be_disabled_for_incompatible_endpoints() -> None:
    adapter = _StubAdapter("m")
    gateway = ModelGateway(adapter, timeout_s=5, use_json_schema=False)
    gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    assert adapter.calls[0].response_schema is None
    assert adapter.calls[0].response_format == "json"
