"""最小 MCP 协议模型（JSON-RPC 2.0，按行分隔 JSON）。

只实现固定只读工具集所需的 `initialize` / `tools/list` / `tools/call`，
不引入任意 SQL、资源或提示词能力。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "dbguardian-postgres-mcp"
SERVER_VERSION = "1.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    id: int | str | None = None
    method: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] | None = None

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class JsonRpcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = JSONRPC_VERSION
    id: int | str | None = None
    result: Any | None = None
    error: JsonRpcError | None = None

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class ServerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class InitializeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    protocol_version: str = Field(alias="protocolVersion")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    server_info: ServerInfo = Field(alias="serverInfo")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class McpTool(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(alias="inputSchema")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class ToolsListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[McpTool]

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class McpContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str


class McpCallToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    content: list[McpContent]
    is_error: bool = Field(default=False, alias="isError")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


def parse_request_line(line: str) -> JsonRpcRequest:
    """解析一行 JSON；抛出 JSONDecodeError/ValidationError 由服务端映射错误码。"""

    return JsonRpcRequest.model_validate(json.loads(line))


def error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=code, message=message, data=data),
    )


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "InitializeResult",
    "JSONRPC_VERSION",
    "JsonRpcError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "METHOD_NOT_FOUND",
    "McpCallToolResult",
    "McpContent",
    "McpTool",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "ServerInfo",
    "ToolsListResult",
    "error_response",
    "parse_request_line",
]
