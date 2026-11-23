"""只读观察工具集合与默认注册表。"""

from __future__ import annotations

from core.tools.registry import ToolRegistry
from database.observers.activity import GetActivityTool
from database.observers.explain import ExplainQueryTool
from database.observers.locks import GetBlockingTreeTool, GetLocksTool
from database.observers.query_stats import GetQueryStatsTool
from database.observers.server_info import GetDbSettingsTool, GetDbVersionTool
from database.observers.table_stats import (
    GetDatabaseSizeTool,
    GetIndexStatsTool,
    GetTableStatsTool,
)

OBSERVER_TOOL_CLASSES = (
    GetDbVersionTool,
    GetDbSettingsTool,
    GetActivityTool,
    GetLocksTool,
    GetBlockingTreeTool,
    GetQueryStatsTool,
    GetTableStatsTool,
    GetIndexStatsTool,
    ExplainQueryTool,
    GetDatabaseSizeTool,
)


def build_observer_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool_class in OBSERVER_TOOL_CLASSES:
        registry.register(tool_class())
    return registry


__all__ = [
    "OBSERVER_TOOL_CLASSES",
    "build_observer_registry",
    "GetActivityTool",
    "GetBlockingTreeTool",
    "GetDatabaseSizeTool",
    "GetDbSettingsTool",
    "GetDbVersionTool",
    "GetIndexStatsTool",
    "GetLocksTool",
    "GetQueryStatsTool",
    "GetTableStatsTool",
    "ExplainQueryTool",
]
