"""适配器包：MySQL Native Adapter 与 PostgreSQL MCP。"""

from adapters.base import (
    AdapterToolSpec,
    CapabilityReport,
    CapabilityStatus,
    DatabaseAdapter,
    DbEngine,
)

__all__ = [
    "AdapterToolSpec",
    "CapabilityReport",
    "CapabilityStatus",
    "DatabaseAdapter",
    "DbEngine",
]
