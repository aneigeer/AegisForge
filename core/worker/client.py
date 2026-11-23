"""Worker 与 Gateway/租约服务的端口契约（仅类型协议，不含数据库实现）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from core.security.models import ExecutionEnvelope, WorkerLease

if TYPE_CHECKING:
    from core.tools.contracts import ToolResult


class GatewayPort(Protocol):
    def execute(
        self,
        envelope: ExecutionEnvelope,
        params: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> "ToolResult": ...


class LeasePort(Protocol):
    def acquire(
        self,
        *,
        worker_id: str,
        task_id: str,
        tenant_id: str,
        environment_id: str,
        ttl_s: float,
    ) -> WorkerLease: ...

    def heartbeat(
        self,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
        ttl_s: float,
    ) -> WorkerLease: ...

    def release(
        self,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> WorkerLease: ...


__all__ = ["GatewayPort", "LeasePort"]
