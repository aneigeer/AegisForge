"""模型层统一契约：消息、请求、响应、用量、结构化输出与工具选择。

约束（来自总纲 4.4）：
- 模型不得持有数据库凭据，不得直接执行任意 Python、Shell 或 SQL；
- 模型输出只作为建议，状态转换与权限判定由系统代码执行；
- 每次调用记录 model_version、Token 用量与延迟。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRole(str, Enum):
    """模型角色：主规划、快速分类、Embedding、评估辅助。"""

    PLANNER = "planner"
    CLASSIFIER = "classifier"
    EMBEDDING = "embedding"
    EVALUATOR = "evaluator"


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=0, max_length=200_000)


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def merge(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ModelMessage] = Field(min_length=1)
    model: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32_768)
    response_format: Literal["text", "json"] = "text"
    #: P1-T1：目标结构的 JSON Schema。给出时适配器走 `json_schema`（严格模式）；
    #: 为 None 时退回 `json_object`。网关按 `output_model` 自动填充。
    response_schema: dict[str, Any] | None = None
    role: ModelRole = ModelRole.PLANNER
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """适配器返回的标准化结果；token 用量缺失时保持 0，不做估算。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    model_version: str
    provider: str
    adapter_version: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_ms: float = Field(ge=0)
    finish_reason: str | None = None
    call_id: str | None = None


class StructuredOutput(BaseModel):
    """结构化输出的封装：data 为 JSON 对象，parsed 为校验后的 Pydantic 实例。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    data: dict[str, Any]
    parsed: Any | None = None
    response: ModelResponse


class ToolCallSelection(BaseModel):
    """模型选择的一次工具调用；tool_name/arguments 必须经系统校验后才可执行。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    response: ModelResponse


class ModelAdapterProtocol:
    """适配器需要具备的最小属性与方法，供 ModelGateway 调用。"""

    provider: str
    adapter_version: str
    model_name: str

    def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover - 协议声明
        raise NotImplementedError


__all__ = [
    "ModelAdapterProtocol",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "ModelUsage",
    "StructuredOutput",
    "ToolCallSelection",
]
