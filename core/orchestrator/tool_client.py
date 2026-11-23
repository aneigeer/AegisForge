"""标准 MCP 工具注册表与 JSON-RPC 工具客户端（V3 跨 Agent 唯一工具入口）。

红线（``INV-MCP-STANDARD-TOOLING``）：

- 所有跨 Agent 工具调用必须经本模块的 :class:`McpToolRegistry` 暴露，禁止 Agent 直接
  持有插件对象调用私有方法；
- 请求/响应信封复用 ``adapters/postgres_mcp/protocol.py`` 的 JSON-RPC 2.0 模型，
  与既有 MCP 工具契约保持同一套字段命名（``tools/call`` / ``isError`` / ``content``）；
- 参数在进入执行器前做**严格校验**：未知参数、缺失必填、携带原始证据/凭据的键一律拒绝；
- 审计副本按密钥名脱敏后写入 WORM，永不落明文凭据。

工具默认只读：写动作必须走 :class:`core.plugins.contracts.DomainRollbackExecutor`
（带审批摘要与超时熔断），因此 Agent 不具备"直接改生产"的能力。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from adapters.postgres_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    SERVER_NAME,
    JsonRpcRequest,
    error_response,
)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

#: 禁止出现在工具参数里的键：原始证据、连接串与任何凭据都必须留在控制面内部。
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "raw_evidence",
        "evidence",
        "dsn",
        "dsn_token",
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "private_key",
        "access_key",
    }
)

#: 审计副本需要打码的键（按名匹配，避免把 PII 写进 WORM）。
_SECRET_KEY_RE = re.compile(r"(pass|secret|token|key|credential|dsn)", re.IGNORECASE)

_MAX_AUDIT_VALUE_CHARS = 160


class ToolCallError(RuntimeError):
    """工具调用失败（含 JSON-RPC 错误码），供 Agent 转成结构化失败原因。"""

    def __init__(self, code: str, message: str, *, tool_name: str = "") -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.tool_name = tool_name


@dataclass(frozen=True)
class ToolSpec:
    """工具契约：名称、所属域、是否只读、参数白名单。"""

    name: str
    domain: str
    description: str
    read_only: bool = True
    required_params: tuple[str, ...] = ()
    optional_params: tuple[str, ...] = ()
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.match(self.name):
            raise ValueError(f"工具名不合规（需匹配 {_TOOL_NAME_RE.pattern}）: {self.name}")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms 必须为正")

    @property
    def allowed_params(self) -> frozenset[str]:
        return frozenset(self.required_params) | frozenset(self.optional_params)

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": f"[{self.domain}] {self.description}",
            "readOnly": self.read_only,
            "inputSchema": {
                "type": "object",
                "properties": {key: {"type": "string"} for key in sorted(self.allowed_params)},
                "required": list(self.required_params),
                "additionalProperties": False,
            },
        }


@dataclass(frozen=True)
class ToolCallResult:
    tool_name: str
    ok: bool
    data: dict[str, Any]
    error_code: str = ""
    error_message: str = ""
    duration_ms: float = 0.0
    rpc: dict[str, Any] = field(default_factory=dict)
    masked_params: dict[str, Any] = field(default_factory=dict)

    def require(self) -> dict[str, Any]:
        if not self.ok:
            raise ToolCallError(self.error_code or "TOOL_ERROR", self.error_message, tool_name=self.tool_name)
        return self.data


def sanitize_for_audit(params: dict[str, Any]) -> dict[str, Any]:
    """生成审计副本：密钥类键打码、长值截断，确保 WORM 里没有明文凭据。"""

    def _scrub(key: str, value: Any) -> Any:
        if _SECRET_KEY_RE.search(key):
            return "***"
        if isinstance(value, str):
            return value if len(value) <= _MAX_AUDIT_VALUE_CHARS else value[:_MAX_AUDIT_VALUE_CHARS] + "…"
        if isinstance(value, dict):
            return {str(k): _scrub(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_scrub(key, item) for item in value]
        return value

    return {str(k): _scrub(str(k), v) for k, v in params.items()}


class McpToolRegistry:
    """工具注册表：只装载通过契约校验的只读/受控工具。"""

    def __init__(self, *, server_name: str = SERVER_NAME) -> None:
        self.server_name = server_name
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> "McpToolRegistry":
        if spec.name in self._specs:
            raise ValueError(f"工具重复注册: {spec.name}")
        if not callable(handler):
            raise ValueError(f"工具 {spec.name} 的 handler 不可调用")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler
        return self

    def merge(self, other: "McpToolRegistry") -> "McpToolRegistry":
        for name, spec in other._specs.items():
            self.register(spec, other._handlers[name])
        return self

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def list_tools(self) -> list[dict[str, Any]]:
        return [spec.to_payload() for spec in self._specs.values()]

    def handler(self, name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        return self._handlers[name]


class McpToolClient:
    """面向 Agent 的 MCP 工具客户端：校验 -> 执行 -> 审计 -> 返回结构化结果。"""

    def __init__(
        self,
        registry: McpToolRegistry,
        *,
        audit: Any | None = None,
        actor_role: str = "SEC",
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._actor_role = actor_role
        self._rpc_seq = 0
        self._calls: list[dict[str, Any]] = []

    @property
    def registry(self) -> McpToolRegistry:
        return self._registry

    @property
    def calls(self) -> list[dict[str, Any]]:
        return list(self._calls)

    def list_tools(self) -> list[dict[str, Any]]:
        return self._registry.list_tools()

    def call(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        *,
        workflow_run_id: str = "",
    ) -> ToolCallResult:
        started = time.perf_counter()
        raw_params = dict(params or {})
        masked = sanitize_for_audit(raw_params)
        self._rpc_seq += 1
        request = JsonRpcRequest(
            id=self._rpc_seq,
            method="tools/call",
            params={"name": tool_name, "arguments": masked},
        )

        spec = self._registry.get(tool_name)
        if spec is None:
            return self._fail(
                tool_name,
                METHOD_NOT_FOUND,
                "METHOD_NOT_FOUND",
                f"未注册的工具: {tool_name}",
                request=request,
                masked=masked,
                started=started,
                workflow_run_id=workflow_run_id,
            )

        problem = self._validate_params(spec, raw_params)
        if problem:
            return self._fail(
                tool_name,
                INVALID_PARAMS,
                "INVALID_PARAMS",
                problem,
                request=request,
                masked=masked,
                started=started,
                workflow_run_id=workflow_run_id,
            )

        if not spec.read_only:
            return self._fail(
                tool_name,
                INVALID_PARAMS,
                "TOOL_PERMISSION_DENIED",
                "Agent 只允许调用只读/受控工具；写动作必须走 DomainRollbackExecutor",
                request=request,
                masked=masked,
                started=started,
                workflow_run_id=workflow_run_id,
            )

        try:
            data = self._registry.handler(tool_name)(raw_params)
        except Exception as exc:  # noqa: BLE001 - 需转成结构化错误回传给 Agent
            return self._fail(
                tool_name,
                INTERNAL_ERROR,
                "TOOL_EXECUTION_ERROR",
                f"{exc.__class__.__name__}: {exc}",
                request=request,
                masked=masked,
                started=started,
                workflow_run_id=workflow_run_id,
            )

        if not isinstance(data, dict):
            data = {"value": data}

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response = {
            "jsonrpc": "2.0",
            "id": request.id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, sort_keys=True)}],
                "isError": False,
            },
        }
        self._record(
            tool_name=tool_name,
            spec=spec,
            masked=masked,
            ok=True,
            error_code="",
            duration_ms=duration_ms,
            workflow_run_id=workflow_run_id,
            request=request,
        )
        return ToolCallResult(
            tool_name=tool_name,
            ok=True,
            data=data,
            duration_ms=duration_ms,
            rpc=response,
            masked_params=masked,
        )

    # -- 内部 ----------------------------------------------------------
    def _validate_params(self, spec: ToolSpec, params: dict[str, Any]) -> str:
        forbidden = sorted(set(params) & FORBIDDEN_PARAM_KEYS)
        if forbidden:
            return f"参数 {forbidden} 禁止携带原始证据/凭据，请改传标识符"
        unknown = sorted(set(params) - spec.allowed_params)
        if unknown:
            return f"存在未声明参数: {unknown}"
        missing = [key for key in spec.required_params if key not in params]
        if missing:
            return f"缺少必填参数: {missing}"
        return ""

    def _fail(
        self,
        tool_name: str,
        rpc_code: int,
        error_code: str,
        message: str,
        *,
        request: JsonRpcRequest,
        masked: dict[str, Any],
        started: float,
        workflow_run_id: str,
    ) -> ToolCallResult:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        response = error_response(request.id, rpc_code, message, data={"tool": tool_name}).model_dump(
            exclude_none=True
        )
        spec = self._registry.get(tool_name)
        self._record(
            tool_name=tool_name,
            spec=spec,
            masked=masked,
            ok=False,
            error_code=error_code,
            duration_ms=duration_ms,
            workflow_run_id=workflow_run_id,
            request=request,
        )
        return ToolCallResult(
            tool_name=tool_name,
            ok=False,
            data={},
            error_code=error_code,
            error_message=message,
            duration_ms=duration_ms,
            rpc=response,
            masked_params=masked,
        )

    def _record(
        self,
        *,
        tool_name: str,
        spec: ToolSpec | None,
        masked: dict[str, Any],
        ok: bool,
        error_code: str,
        duration_ms: float,
        workflow_run_id: str,
        request: JsonRpcRequest,
    ) -> None:
        entry = {
            "seq": len(self._calls) + 1,
            "workflow_run_id": workflow_run_id,
            "tool": tool_name,
            "domain": spec.domain if spec else "",
            "read_only": bool(spec.read_only) if spec else False,
            "ok": ok,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "params": masked,
            "protocol_version": PROTOCOL_VERSION,
        }
        self._calls.append(entry)
        if self._audit is not None:
            self._audit.append_worm_event(
                "MCP_TOOL_INVOKED",
                self._actor_role,
                {
                    "workflow_run_id": workflow_run_id,
                    "tool": tool_name,
                    "domain": spec.domain if spec else "",
                    "ok": ok,
                    "error_code": error_code,
                    "params": masked,
                    "rpc_id": request.id,
                },
            )


__all__ = [
    "FORBIDDEN_PARAM_KEYS",
    "McpToolClient",
    "McpToolRegistry",
    "ToolCallError",
    "ToolCallResult",
    "ToolSpec",
    "sanitize_for_audit",
]
