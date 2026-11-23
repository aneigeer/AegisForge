"""W09 T04 记忆包（纯领域层：不连库、不持 DSN、不导入工具/Gateway/数据库驱动）。

公开面只导出五类记忆模型与冲突解析器；仓储/服务/进程内工作集/时间线投影
按模块路径显式导入，避免包初始化阶段产生 `core.memory` 内部循环导入。
"""

from __future__ import annotations

from core.memory.models import (
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    MemoryQuery,
    ValidationLevel,
)
from core.memory.resolver import (
    FilterReason,
    MemoryConflictResolver,
    MemoryResolution,
)

__version__ = "0.1.0"

__all__ = [
    "FilterReason",
    "KnowledgeEntry",
    "KnowledgeStatus",
    "MemoryConflictResolver",
    "MemoryKind",
    "MemoryQuery",
    "MemoryResolution",
    "ValidationLevel",
]
