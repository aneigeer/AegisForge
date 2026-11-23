"""DatabaseAdapter 统一接口（V2.2 W02）。

约束：
- 适配器只暴露固定只读工具，不接受任意 SQL/host/port；
- 能力、权限、超时、行数限制与错误语义必须显式；
- 工具结果统一为 ToolResult，并标注 engine/adapter_version/capability_ref。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.tools.contracts import ToolContext, ToolDefinition, ToolResult


class DbEngine(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


class CapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    DENIED = "DENIED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class CapabilityReport(BaseModel):
    """一次能力探测结果；不得把权限不足解释为“没有故障”。"""

    model_config = ConfigDict(extra="forbid")

    engine: DbEngine
    database_id: str
    server_version: str
    adapter_version: str
    checked_at: str
    capabilities: dict[str, CapabilityStatus] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def capability_ref(self) -> str:
        return f"{self.engine.value}:{self.adapter_version}:{self.checked_at}"


class AdapterToolSpec(BaseModel):
    """供 Registry/Planner 使用的固定工具元数据。"""

    model_config = ConfigDict(extra="forbid")

    definition: ToolDefinition
    engine: DbEngine
    available: bool = True
    unavailable_reason: str | None = None


class DatabaseAdapter(Protocol):
    """所有引擎适配器必须实现的只读接口。"""

    engine: DbEngine
    adapter_version: str
    database_id: str

    def probe_capabilities(self) -> CapabilityReport: ...

    def tool_specs(self) -> list[AdapterToolSpec]: ...

    def call_tool(
        self, name: str, context: ToolContext, params: dict[str, Any] | None = None
    ) -> ToolResult: ...


__all__ = [
    "AdapterToolSpec",
    "CapabilityReport",
    "CapabilityStatus",
    "DatabaseAdapter",
    "DbEngine",
]
