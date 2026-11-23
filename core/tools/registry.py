"""最小工具注册表：唯一名称、定义可枚举、按名查找。

W03 的 Executor 将在此基础上增加动态注册、版本与权限校验。
"""

from __future__ import annotations

from core.errors import ToolNotFoundError
from core.tools.contracts import ToolDefinition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def register(self, tool: object) -> None:
        definition = getattr(tool, "definition", None)
        if not isinstance(definition, ToolDefinition):
            raise TypeError("工具必须提供 ToolDefinition")
        if definition.name in self._tools:
            raise ValueError(f"工具名称重复：{definition.name}")
        self._tools[definition.name] = tool

    def get(self, name: str) -> object:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(
                "工具不存在", details={"tool_name": name}
            ) from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        return [getattr(tool, "definition") for _, tool in sorted(self._tools.items())]

    def __len__(self) -> int:
        return len(self._tools)
