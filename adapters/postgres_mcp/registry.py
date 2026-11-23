"""P2-T1：PostgreSQL 只读工具注册表（供 Agent Loop 在第二引擎上做只读诊断）。

为什么需要单独一层：`adapters/postgres_mcp/tools.py` 提供的是**函数式**
`execute_tool(name, args, client=...)`，而 `AgentLoop` 要的是 `ToolRegistry` 里的
工具对象（`definition` + `run(context, params)`）。本模块只做这一层适配，
**不复制**任何 SQL、不新增工具、不放宽权限：

- 只注册 `TOOL_DEFINITIONS` 中 `side_effect == "read"` 的工具；
- 每个工具把 `ToolContext.client`（必须是 `PgClient`）原样交给 `execute_tool`，
  因此只读守卫、行数上限、超时全部沿用既有实现；
- `database_id` 取自 client，`db_version` 由实际查询结果回填（未知时留空串），
  不臆造版本号。
"""

from __future__ import annotations

from typing import Any

from adapters.postgres_mcp.tools import TOOL_DEFINITIONS, execute_tool
from core.errors import BenchmarkInvalidError
from core.tools.contracts import ToolContext, ToolResult
from core.tools.registry import ToolRegistry

#: PG 只读工具名（顺序固定；与 `TOOL_DEFINITIONS` 的只读子集一致）。
PG_READ_TOOLS: tuple[str, ...] = tuple(
    name
    for name, definition in TOOL_DEFINITIONS.items()
    if getattr(definition, "side_effect", None) == "read"
)


class _PgTool:
    """把静态定义 + `execute_tool` 包成 ToolRegistry 可注册的工具对象。"""

    def __init__(self, name: str) -> None:
        self.definition = TOOL_DEFINITIONS[name]
        # execute_tool 自己按 `input_model` 校验参数，这里保持一致以避免双份 schema。
        self.input_model = getattr(self.definition, "input_model", None)

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        db_version = str(getattr(context.client, "server_version", "") or "")
        result = execute_tool(
            self.definition.name,
            dict(params or {}),
            client=context.client,
            database_id=str(getattr(context.client, "database_id", "") or ""),
            db_version=db_version or "unknown",
        )
        return result


def build_pg_read_registry() -> ToolRegistry:
    """构造 PG 只读工具注册表；出现写工具即拒绝装配（fail closed）。"""

    registry = ToolRegistry()
    for name in PG_READ_TOOLS:
        definition = TOOL_DEFINITIONS[name]
        if definition.side_effect != "read":
            raise BenchmarkInvalidError(
                "PG 诊断注册表不得装配写工具",
                details={"tool_name": name, "side_effect": definition.side_effect},
            )
        registry.register(_PgTool(name))
    return registry


__all__ = ["PG_READ_TOOLS", "build_pg_read_registry"]
