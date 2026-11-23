"""Worker 执行单元：领取租约、续租、构造并签名执行信封，经 Gateway 提交工具。

安全约束：
- 只持有签名密钥与 `GatewayPort`；不 import 数据库适配层、不持有 DSN；
- 租约失效/续租失败后不得提交新步骤。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core.errors import WorkerLeaseInvalidError
from core.gateway.envelope import build_envelope, sign_envelope
from core.security.models import ExecutionEnvelope, LeaseStatus, RiskLevel, WorkerLease
from core.tasks.models import new_id, utc_now
from core.worker.client import GatewayPort, LeasePort

if TYPE_CHECKING:
    from core.tools.contracts import ToolResult


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Worker:
    def __init__(
        self,
        worker_id: str,
        tenant_id: str,
        actor_user_id: str,
        lease_port: LeasePort,
        gateway_port: GatewayPort,
        *,
        signing_key: bytes,
        tool_version_default: str = "1.0",
    ) -> None:
        self._worker_id = worker_id
        self._tenant_id = tenant_id
        self._actor_user_id = actor_user_id
        self._lease_port = lease_port
        self._gateway_port = gateway_port
        self._signing_key = signing_key
        self._tool_version_default = tool_version_default
        self._lease: WorkerLease | None = None

    # ---- identity ------------------------------------------------------
    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def lease(self) -> WorkerLease | None:
        return self._lease

    # ---- lease lifecycle ----------------------------------------------
    def acquire_lease(
        self,
        task_id: str,
        environment_id: str,
        ttl_s: float,
    ) -> WorkerLease:
        lease = self._lease_port.acquire(
            worker_id=self._worker_id,
            task_id=task_id,
            tenant_id=self._tenant_id,
            environment_id=environment_id,
            ttl_s=ttl_s,
        )
        self._lease = lease
        return lease

    def heartbeat(self, ttl_s: float) -> WorkerLease:
        lease = self._require_lease()
        try:
            updated = self._lease_port.heartbeat(
                lease.lease_id,
                self._worker_id,
                lease.fencing_token,
                ttl_s,
            )
        except Exception:
            # 续租失败后本 Worker 不得再提交新步骤。
            self._lease = None
            raise
        self._lease = updated
        return updated

    def release(self) -> WorkerLease:
        lease = self._require_lease()
        try:
            released = self._lease_port.release(
                lease.lease_id,
                self._worker_id,
                lease.fencing_token,
            )
        except Exception:
            self._lease = None
            raise
        self._lease = None
        return released

    # ---- execution -----------------------------------------------------
    def submit(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        step_id: str | None,
        state_version: int,
        tool_version: str | None = None,
        ttl_s: float = 60,
        model_risk_hint: RiskLevel | None = None,
        approval_id: str | None = None,
    ) -> "ToolResult":
        lease = self._require_lease()
        if lease.status != LeaseStatus.ACTIVE:
            raise WorkerLeaseInvalidError(
                "Worker 没有活动租约",
                details={"lease_id": lease.lease_id, "status": lease.status.value},
            )
        if _normalize_utc(lease.expires_at) <= utc_now():
            raise WorkerLeaseInvalidError(
                "Worker 租约已过期", details={"lease_id": lease.lease_id}
            )

        envelope: ExecutionEnvelope = build_envelope(
            task_id=lease.task_id,
            step_id=step_id,
            worker_id=self._worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=state_version,
            correlation_id=new_id("corr"),
            actor_user_id=self._actor_user_id,
            tool_name=tool_name,
            tool_version=tool_version or self._tool_version_default,
            environment_id=lease.environment_id,
            params=params,
            ttl_s=ttl_s,
            model_risk_hint=model_risk_hint,
            approval_id=approval_id,
        )
        signed = sign_envelope(envelope, self._signing_key)
        return self._gateway_port.execute(signed, params)

    # ---- internals -----------------------------------------------------
    def _require_lease(self) -> WorkerLease:
        if self._lease is None:
            raise WorkerLeaseInvalidError(
                "Worker 没有活动租约", details={"worker_id": self._worker_id}
            )
        return self._lease


__all__ = ["Worker"]
