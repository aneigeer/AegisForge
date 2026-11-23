"""Model Gateway：统一模型调用、墙钟超时、结构化输出与工具选择校验。

约束：
- 模型输出只作为建议；工具名、风险等级、写权限与参数都必须由本层校验；
- 超时与调用失败的 details 只放安全字段，绝不携带消息正文或密钥；
- 每次调用只记录 purpose/model_version/latency_ms/token 数。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.errors import (
    DBGuardianError,
    ModelCallFailedError,
    ModelOutputInvalidError,
    ModelTimeoutError,
)
from core.logging import get_logger, redact_text
from core.models.contracts import (
    ModelAdapterProtocol,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
    StructuredOutput,
    ToolCallSelection,
)
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tools.registry import ToolRegistry

logger = get_logger(__name__)

_PREVIEW_LIMIT = 200
_DEFAULT_ALLOWED_RISKS = {"R0", "R1"}

_TOOL_CALL_SYSTEM_PROMPT = (
    "你是只读 MySQL 诊断 Agent 的工具选择器。"
    "只能从 available_tools 中选择一个工具，并给出符合该工具 input_schema 的 arguments。"
    "只返回 JSON 对象，字段为 tool_name、arguments、reason；不要输出任何解释或 Markdown。"
)


class _ToolCallOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class ModelGateway:
    def __init__(
        self,
        adapter: ModelAdapterProtocol,
        *,
        timeout_s: float = 30.0,
        default_max_tokens: int | None = None,
        redaction_gate: RedactionGate | None = None,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
        use_json_schema: bool = True,
        quota: object | None = None,
    ) -> None:
        self._adapter = adapter
        # P1-T1：结构化输出优先用 `json_schema` 严格模式；端点不支持时可用
        # DG_MODEL_JSON_SCHEMA=false 退回 `json_object`（Pydantic 校验仍然生效）。
        self._use_json_schema = bool(use_json_schema)
        # P1-BYOK-T2：租户配额守卫（None = 不限）。before_call 在发请求前把关，
        # record_tokens 只累加端点回报的真实 usage（不估算）。
        self._quota = quota
        self._timeout_s = timeout_s
        self._default_max_tokens = default_max_tokens
        self._usage = ModelUsage()
        # P1-T1：分角色用量与成本（成本未配置单价时恒 None，不估算）。
        self._usage_by_role: dict[ModelRole, ModelUsage] = {}
        self._models_by_role: dict[ModelRole, str] = {}
        self._pricing: tuple[float, float] | None = (
            (float(input_price_per_1k), float(output_price_per_1k))
            if input_price_per_1k is not None and output_price_per_1k is not None
            else None
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-gateway")
        # W09 闸门④（`docs/w09/02-design.md:256`）：**恒开**，未注入时默认构造
        # （引擎无状态、不持 DSN）。没有 enabled/配置项，不提供"跳过脱敏"路径。
        self._redaction = redaction_gate or RedactionGate(DataRedactionEngine())

    # ---- properties ------------------------------------------------------
    @property
    def adapter(self) -> ModelAdapterProtocol:
        return self._adapter

    @property
    def usage(self) -> ModelUsage:
        return self._usage

    def usage_for(self, role: ModelRole) -> ModelUsage:
        """该角色的累计用量（未调用过 → 全 0，不返回 None）。"""
        return self._usage_by_role.get(role, ModelUsage())

    def usage_by_role(self) -> dict[ModelRole, ModelUsage]:
        """分角色用量快照（只读副本）。"""
        return {role: usage.model_copy() for role, usage in self._usage_by_role.items()}

    def model_for(self, role: ModelRole) -> str:
        """该角色实际调用的模型名（未调用过 → 路由视图，仍不猜）。"""
        if role in self._models_by_role:
            return self._models_by_role[role]
        resolver = getattr(self._adapter, "model_for", None)
        if callable(resolver):
            return str(resolver(role))
        return self._model_name_of(self._adapter)

    def cost_for(self, role: ModelRole) -> float | None:
        """该角色的累计成本（单价未配置 → ``None``，**不估算**）。"""
        if self._pricing is None:
            return None
        usage = self.usage_for(role)
        input_price, output_price = self._pricing
        return (
            usage.prompt_tokens / 1000.0 * input_price
            + usage.completion_tokens / 1000.0 * output_price
        )

    def total_cost(self) -> float | None:
        """全部角色成本合计（单价未配置 → ``None``）。"""
        if self._pricing is None:
            return None
        return sum(self.cost_for(role) or 0.0 for role in self._usage_by_role)

    def reset_usage(self) -> None:
        self._usage = ModelUsage()
        self._usage_by_role.clear()

    # ---- public ----------------------------------------------------------
    def generate(
        self,
        messages: list[ModelMessage] | list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        role: ModelRole = ModelRole.PLANNER,
        purpose: str = "generate",
        metadata: dict | None = None,
    ) -> ModelResponse:
        return self._generate(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
            purpose=purpose,
            metadata=metadata,
            response_format="text",
        )

    def generate_structured(
        self,
        messages: list[ModelMessage] | list[dict],
        output_model: type[BaseModel],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        role: ModelRole = ModelRole.PLANNER,
        purpose: str = "generate_structured",
        metadata: dict | None = None,
    ) -> StructuredOutput:
        response = self._generate(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            role=role,
            purpose=purpose,
            metadata=metadata,
            response_format="json",
            response_schema=output_model.model_json_schema()
            if self._use_json_schema
            else None,
        )
        try:
            data = json.loads(_strip_json_fence(response.text))
        except json.JSONDecodeError as exc:
            # P1-T1：截断与语法错误必须可区分——P1-T2 的四类归因需要它。
            truncated = response.finish_reason == "length"
            raise ModelOutputInvalidError(
                "模型输出不完整（达到 max_tokens 上限，JSON 被截断）"
                if truncated
                else "模型输出不是合法 JSON",
                details={
                    "reason": "TRUNCATED_OUTPUT" if truncated else "JSON_INVALID",
                    "finish_reason": response.finish_reason,
                    "errors": [
                        {
                            "loc": [],
                            "msg": f"JSON 解析失败：{exc.msg}",
                            "type": "value_error.json",
                        }
                    ],
                    "text_preview": self._text_preview(response.text),
                },
            ) from None

        if not isinstance(data, dict):
            raise ModelOutputInvalidError(
                "模型输出必须是 JSON 对象",
                details={
                    "errors": [
                        {
                            "loc": [],
                            "msg": "结构化输出必须是 JSON 对象",
                            "type": "type_error.dict",
                        }
                    ],
                    "text_preview": self._text_preview(response.text),
                },
            )

        try:
            parsed = output_model.model_validate(data)
        except ValidationError as exc:
            raise ModelOutputInvalidError(
                "模型输出不符合目标结构",
                details={
                    "errors": [
                        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                        for item in exc.errors()
                    ],
                    "text_preview": self._text_preview(response.text),
                },
            ) from None

        return StructuredOutput(data=data, parsed=parsed, response=response)

    def tool_call(
        self,
        messages: list[ModelMessage] | list[dict],
        *,
        registry: ToolRegistry,
        allowed_risk_levels: set[str] | None = None,
        allow_write: bool = False,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        purpose: str = "tool_call",
    ) -> ToolCallSelection:
        allowed_risks = (
            set(allowed_risk_levels)
            if allowed_risk_levels is not None
            else set(_DEFAULT_ALLOWED_RISKS)
        )
        available_tools = [
            {
                "name": definition.name,
                "description": definition.description,
                "risk_level": definition.risk_level,
                "side_effect": definition.side_effect,
                "input_schema": definition.input_schema,
            }
            for definition in registry.definitions()
        ]
        request_payload = {
            "request_type": "tool_call",
            "available_tools": available_tools,
            "allowed_risk_levels": sorted(allowed_risks),
            "allow_write": allow_write,
        }
        tool_messages: list[ModelMessage] = [
            ModelMessage(role="system", content=_TOOL_CALL_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=json.dumps(request_payload, ensure_ascii=False, default=str),
            ),
        ]
        # 保留调用方提供的对话上下文，便于模型理解目标。
        tool_messages = self._normalize_messages(messages) + tool_messages

        structured = self.generate_structured(
            tool_messages,
            _ToolCallOutput,
            temperature=temperature,
            max_tokens=max_tokens,
            role=ModelRole.PLANNER,
            purpose=purpose,
        )
        selection: _ToolCallOutput = structured.parsed
        tool_name = selection.tool_name

        if not tool_name:
            raise self._invalid_tool("", "模型未给出 tool_name", structured.response)
        if tool_name not in registry.names():
            raise self._invalid_tool(tool_name, "工具未注册", structured.response)

        tool = registry.get(tool_name)
        definition = getattr(tool, "definition")

        if definition.risk_level not in allowed_risks:
            raise self._invalid_tool(
                tool_name,
                f"风险等级 {definition.risk_level} 不在允许范围内",
                structured.response,
            )
        if definition.side_effect == "write" and not allow_write:
            raise self._invalid_tool(tool_name, "写工具未获授权", structured.response)

        input_model = getattr(tool, "input_model", None)
        if input_model is not None:
            try:
                input_model.model_validate(selection.arguments)
            except ValidationError as exc:
                raise ModelOutputInvalidError(
                    "工具参数不符合 input_schema",
                    details={
                        "tool_name": tool_name,
                        "reason": "参数校验失败",
                        "errors": [
                            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                            for item in exc.errors()
                        ],
                    },
                ) from None

        return ToolCallSelection(
            tool_name=tool_name,
            arguments=selection.arguments,
            reason=selection.reason,
            response=structured.response,
        )

    # ---- internals -------------------------------------------------------
    def _generate(
        self,
        messages: list[ModelMessage] | list[dict],
        *,
        temperature: float,
        max_tokens: int | None,
        role: ModelRole,
        purpose: str,
        metadata: dict | None,
        response_format: str,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        request_metadata = dict(metadata or {})
        request_metadata["purpose"] = purpose
        # W09 闸门④：**适配器调用之前**复查消息正文（纵深防御：调用方漏检也拦得住）。
        # 不通过 → 抛领域错误，`self._adapter.complete` 根本不会被调用（不发 HTTP）。
        # `task_id`/`tenant_id` 从 `metadata` 取（调用方若带上，事件/隔离才有归属）。
        normalized = self._normalize_messages(messages)
        self._redaction.for_model_messages(
            normalized,
            task_id=str(request_metadata.get("task_id") or ""),
            tenant_id=str(request_metadata.get("tenant_id") or "") or None,
        )
        adapter = self._adapter_for(role)
        request = ModelRequest(
            messages=normalized,
            model=self._model_name_of(adapter),
            temperature=temperature,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens,
            response_format=response_format,
            response_schema=response_schema,
            role=role,
            metadata=request_metadata,
        )
        # P1-BYOK-T2：发请求前先过配额（fail closed），拿到真实 usage 后再累加。
        if self._quota is not None:
            self._quota.before_call()
        # P4-T3：只记录角色/结果/耗时/token，不记录 prompt、模型输出或密钥。
        from core.telemetry.metrics import METRICS

        try:
            response = self._invoke(request, purpose, adapter)
        except Exception:
            METRICS.inc(
                "dg_model_calls_total", {"role": role.value, "outcome": "error"}
            )
            raise
        METRICS.inc("dg_model_calls_total", {"role": role.value, "outcome": "success"})
        METRICS.observe(
            "dg_model_latency_seconds", response.latency_ms / 1000.0, {"role": role.value}
        )
        METRICS.inc(
            "dg_model_tokens_total", {"role": role.value}, response.usage.total_tokens
        )
        if self._quota is not None:
            self._quota.record_tokens(response.usage.total_tokens)

        self._record_usage(role, adapter, response)
        logger.info(
            "模型调用完成",
            extra={
                "purpose": purpose,
                "role": role.value,
                "model_name": self._model_name_of(adapter),
                "model_version": response.model_version,
                "latency_ms": response.latency_ms,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )

        if not response.text or not response.text.strip():
            raise ModelCallFailedError(
                "模型返回空内容",
                details={
                    "provider": response.provider,
                    "model_version": response.model_version,
                    "purpose": purpose,
                },
            )
        return response

    def _invoke(
        self, request: ModelRequest, purpose: str, adapter: ModelAdapterProtocol
    ) -> ModelResponse:
        model_version = self._model_version(adapter)
        try:
            future = self._executor.submit(adapter.complete, request)
            try:
                return future.result(timeout=self._timeout_s)
            except FuturesTimeout:
                future.cancel()
                raise ModelTimeoutError(
                    "模型调用超时",
                    details={
                        "provider": getattr(adapter, "provider", "unknown"),
                        "model_version": model_version,
                        "purpose": purpose,
                        "timeout_s": self._timeout_s,
                    },
                ) from None
        except DBGuardianError:
            raise
        except Exception as exc:
            raise ModelCallFailedError(
                "模型调用失败",
                details={
                    "provider": getattr(adapter, "provider", "unknown"),
                    "model_version": model_version,
                    "purpose": purpose,
                    "exception_type": type(exc).__name__,
                },
            ) from None

    def _model_version(self, adapter: ModelAdapterProtocol | None = None) -> str:
        target = adapter if adapter is not None else self._adapter
        version = getattr(target, "model_version", None)
        if isinstance(version, str) and version:
            return version
        provider = getattr(target, "provider", "unknown")
        model_name = getattr(target, "model_name", "unknown")
        adapter_version = getattr(target, "adapter_version", "unknown")
        return f"{provider}/{model_name}/{adapter_version}"

    def _adapter_for(self, role: ModelRole) -> ModelAdapterProtocol:
        """P1-T1：角色路由（非路由适配器 → 返回自身，行为与改造前逐字等价）。"""
        resolver = getattr(self._adapter, "adapter_for", None)
        if callable(resolver):
            return resolver(role)
        return self._adapter

    @staticmethod
    def _model_name_of(adapter: ModelAdapterProtocol) -> str:
        return str(getattr(adapter, "model_name", "unknown"))

    def _record_usage(
        self,
        role: ModelRole,
        adapter: ModelAdapterProtocol,
        response: ModelResponse,
    ) -> None:
        """累计总量 + 分角色用量 + 实际模型名（成本只在配置单价时可算）。"""
        self._usage = self._usage.merge(response.usage)
        self._usage_by_role[role] = self.usage_for(role).merge(response.usage)
        self._models_by_role[role] = self._model_name_of(adapter)

    @staticmethod
    def _normalize_messages(messages: list[ModelMessage] | list[dict]) -> list[ModelMessage]:
        normalized: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelMessage):
                normalized.append(message)
            else:
                normalized.append(ModelMessage.model_validate(message))
        return normalized

    @staticmethod
    def _text_preview(text: str) -> str:
        return redact_text(text[:_PREVIEW_LIMIT])

    def _invalid_tool(
        self, tool_name: str, reason: str, response: ModelResponse
    ) -> ModelOutputInvalidError:
        return ModelOutputInvalidError(
            "模型选择的工具调用不合法",
            details={
                "tool_name": tool_name,
                "reason": reason,
                "model_version": response.model_version,
            },
        )


def _strip_json_fence(text: str) -> str:
    """P1-BYOK-T3：剥离模型常见的 ```json 围栏，**只做确定性去壳**。

    仅当首尾是围栏标记时剥离；不做"猜 JSON 边界"的启发式修复——
    真正的语法错误仍由 `json.loads` 抛出并归因 `JSON_INVALID`（fail closed）。
    """
    body = str(text or "").strip()
    if body.startswith("```"):
        first_newline = body.find("\n")
        if first_newline != -1:
            body = body[first_newline + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -3]
    return body.strip()


__all__ = ["ModelGateway"]
