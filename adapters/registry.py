"""把 DatabaseAdapter 的固定工具接入 ToolRegistry / Executor。

同一引擎内工具名唯一；双引擎并存的名称路由由 W05 Tool Gateway 负责。

W07 装配纪律：

- `build_adapter_registry(adapter)` **签名与行为不变**，永远只注册适配器声明的只读工具；
  两个既有调用点（`core/diagnosis/runner.py`、`apps/api/routes/adapters.py`）不得
  因此获得写能力。
- 写工具只在 `adapters/lab_assembly.py::build_lab_registry()` 注册（LAB + mysql 才放行）。
  本模块只是**转出**该函数，方便装配方从同一处导入，不在此实现 LAB 判定逻辑。
"""

from __future__ import annotations

from typing import Any

from adapters.base import AdapterToolSpec, DatabaseAdapter
from adapters.lab_assembly import build_lab_registry
from core.tools.contracts import ToolContext, ToolResult
from core.tools.registry import ToolRegistry


class AdapterToolBinding:
    def __init__(self, adapter: DatabaseAdapter, spec: AdapterToolSpec) -> None:
        self._adapter = adapter
        self._name = spec.definition.name
        self.definition = spec.definition
        self.input_model = None

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        return self._adapter.call_tool(self._name, context, params)


def build_adapter_registry(adapter: DatabaseAdapter) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in adapter.tool_specs():
        registry.register(AdapterToolBinding(adapter, spec))
    return registry


__all__ = ["AdapterToolBinding", "build_adapter_registry", "build_lab_registry"]
