"""V3.0 确定性 StateGraph 编排层。

本包只做编排，不含业务语义：

- ``state_graph``：DAG 状态机骨架（节点注册 / 条件边 / 自愈回退边 / HITL 挂起快照）
- ``tool_client``：标准 MCP ToolRegistry 与 JSON-RPC 工具客户端
"""

from __future__ import annotations

__all__ = ["state_graph", "tool_client"]
