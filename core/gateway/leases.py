"""Worker 租约：任务级短租约、心跳、释放与 fencing token 单调递增。

保证边界：
- 同一任务最多一个 ACTIVE 租约由数据库生成列 `active_task_id` + 唯一键兜底；
- fencing_token 取自该任务历史最大值 + 1，跨过期/释放租约仍严格递增；
- 不声称分布式事务或 Exactly Once，仅提供可验证的租约事实。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.errors import (
    DatabaseQueryError,
    FencingTokenStaleError,
    WorkerConflictError,
    WorkerLeaseInvalidError,
)
from core.security.models import LeaseStatus, WorkerLease
from core.tasks.models import new_id, utc_now
from database.mysql.connection import MysqlClient

LEASES = "dg_system.worker_leases"


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class LeaseRepository:
    """worker_leases 表的最小读写；所有写操作均为条件单语句 UPDATE。"""

    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    # ---- reads ---------------------------------------------------------
    def get(self, lease_id: str) -> WorkerLease | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {LEASES} WHERE lease_id = %s LIMIT 1", (lease_id,)
        )
        return self._to_lease(rows[0]) if rows else None

    def get_active(self, task_id: str) -> WorkerLease | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {LEASES} WHERE task_id = %s AND status = 'ACTIVE' LIMIT 1",
            (task_id,),
        )
        return self._to_lease(rows[0]) if rows else None

    def max_fencing_token(self, task_id: str) -> int:
        rows = self._client.fetch_all(
            f"SELECT COALESCE(MAX(fencing_token), 0) AS max_token FROM {LEASES} "
            "WHERE task_id = %s",
            (task_id,),
        )
        return int(rows[0]["max_token"]) if rows else 0

    # ---- writes --------------------------------------------------------
    def insert(self, lease: WorkerLease) -> None:
        self._client.execute(
            f"INSERT INTO {LEASES} (lease_id, task_id, tenant_id, worker_id, "
            "environment_id, fencing_token, status, issued_at, heartbeat_at, "
            "expires_at, released_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                lease.lease_id,
                lease.task_id,
                lease.tenant_id,
                lease.worker_id,
                lease.environment_id,
                lease.fencing_token,
                lease.status.value,
                _to_db(lease.issued_at),
                _to_db(lease.heartbeat_at),
                _to_db(lease.expires_at),
                _to_db(lease.released_at),
            ),
        )

    def mark_expired(self, lease_id: str, now: datetime) -> int:
        return self._client.execute(
            f"UPDATE {LEASES} SET status = 'EXPIRED', released_at = %s "
            "WHERE lease_id = %s AND status = 'ACTIVE'",
            (_to_db(now), lease_id),
        )

    def expire_stale(self, now: datetime) -> int:
        return self._client.execute(
            f"UPDATE {LEASES} SET status = 'EXPIRED', released_at = %s "
            "WHERE status = 'ACTIVE' AND expires_at <= %s",
            (_to_db(now), _to_db(now)),
        )

    def touch(
        self,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        heartbeat_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> int:
        return self._client.execute(
            f"UPDATE {LEASES} SET heartbeat_at = %s, expires_at = %s "
            "WHERE lease_id = %s AND status = 'ACTIVE' AND worker_id = %s "
            "AND fencing_token = %s AND expires_at > %s",
            (
                _to_db(heartbeat_at),
                _to_db(expires_at),
                lease_id,
                worker_id,
                fencing_token,
                _to_db(now),
            ),
        )

    def mark_released(
        self, lease_id: str, worker_id: str, fencing_token: int, released_at: datetime
    ) -> int:
        return self._client.execute(
            f"UPDATE {LEASES} SET status = 'RELEASED', released_at = %s "
            "WHERE lease_id = %s AND status = 'ACTIVE' AND worker_id = %s "
            "AND fencing_token = %s",
            (_to_db(released_at), lease_id, worker_id, fencing_token),
        )

    # ---- row mapping ---------------------------------------------------
    @staticmethod
    def _to_lease(row: dict[str, Any]) -> WorkerLease:
        return WorkerLease(
            lease_id=str(row["lease_id"]),
            worker_id=str(row["worker_id"]),
            task_id=str(row["task_id"]),
            tenant_id=str(row["tenant_id"]),
            environment_id=str(row["environment_id"]),
            fencing_token=int(row["fencing_token"]),
            status=LeaseStatus(str(row["status"])),
            issued_at=_from_db(row["issued_at"]),  # type: ignore[arg-type]
            heartbeat_at=_from_db(row["heartbeat_at"]),  # type: ignore[arg-type]
            expires_at=_from_db(row["expires_at"]),  # type: ignore[arg-type]
            released_at=_from_db(row["released_at"]),
        )


class LeaseService:
    def __init__(self, repository: LeaseRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> LeaseRepository:
        return self._repository

    # ---- acquire -------------------------------------------------------
    def acquire(
        self,
        *,
        worker_id: str,
        task_id: str,
        tenant_id: str,
        environment_id: str,
        ttl_s: float,
    ) -> WorkerLease:
        now = utc_now()
        active = self._repository.get_active(task_id)
        if active is not None:
            if _normalize_utc(active.expires_at) > now:
                raise WorkerConflictError(
                    "任务已有活动 Worker 租约",
                    details={
                        "task_id": task_id,
                        "lease_id": active.lease_id,
                        "worker_id": active.worker_id,
                    },
                )
            # 过期租约先置 EXPIRED，释放唯一键占位后再插入。
            self._repository.mark_expired(active.lease_id, now)

        token = self._repository.max_fencing_token(task_id) + 1
        lease = WorkerLease(
            lease_id=new_id("lease"),
            worker_id=worker_id,
            task_id=task_id,
            tenant_id=tenant_id,
            environment_id=environment_id,
            fencing_token=token,
            status=LeaseStatus.ACTIVE,
            issued_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
        try:
            self._repository.insert(lease)
        except DatabaseQueryError as exc:
            if exc.details.get("mysql_error_code") == 1062:
                raise WorkerConflictError(
                    "任务已有活动 Worker 租约",
                    details={
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "reason": "active_task_id_unique",
                    },
                ) from None
            raise
        return lease

    # ---- heartbeat -----------------------------------------------------
    def heartbeat(
        self,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
        ttl_s: float,
    ) -> WorkerLease:
        now = utc_now()
        affected = self._repository.touch(
            lease_id,
            worker_id,
            fencing_token,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
            now=now,
        )
        if affected == 0:
            self._raise_invalid_or_stale(lease_id, worker_id, fencing_token, now)
        lease = self._repository.get(lease_id)
        if lease is None:
            raise WorkerLeaseInvalidError(
                "租约不存在", details={"lease_id": lease_id}
            )
        return lease

    # ---- release -------------------------------------------------------
    def release(self, lease_id: str, worker_id: str, fencing_token: int) -> WorkerLease:
        now = utc_now()
        affected = self._repository.mark_released(lease_id, worker_id, fencing_token, now)
        if affected == 0:
            self._raise_invalid_or_stale(lease_id, worker_id, fencing_token, now)
        lease = self._repository.get(lease_id)
        if lease is None:
            raise WorkerLeaseInvalidError(
                "租约不存在", details={"lease_id": lease_id}
            )
        return lease

    # ---- verify --------------------------------------------------------
    def verify(
        self,
        lease_id: str,
        task_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> WorkerLease:
        now = utc_now()
        lease = self._repository.get(lease_id)
        if lease is None:
            raise WorkerLeaseInvalidError(
                "租约不存在", details={"lease_id": lease_id}
            )
        if lease.task_id != task_id:
            raise WorkerLeaseInvalidError(
                "租约与任务不匹配",
                details={"lease_id": lease_id, "lease_task_id": lease.task_id},
            )
        if lease.worker_id != worker_id:
            raise WorkerLeaseInvalidError(
                "租约不属于该 Worker",
                details={"lease_id": lease_id, "worker_id": worker_id},
            )
        if lease.status != LeaseStatus.ACTIVE:
            raise WorkerLeaseInvalidError(
                "租约不是活动状态",
                details={"lease_id": lease_id, "status": lease.status.value},
            )
        if _normalize_utc(lease.expires_at) <= now:
            raise WorkerLeaseInvalidError(
                "租约已过期", details={"lease_id": lease_id}
            )
        max_token = self._repository.max_fencing_token(task_id)
        if fencing_token < max_token:
            raise FencingTokenStaleError(
                "fencing token 已过期",
                details={
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "current_max_token": max_token,
                },
            )
        if fencing_token != lease.fencing_token:
            # 防御伪造的更高 token：必须与当前活动租约完全一致。
            raise FencingTokenStaleError(
                "fencing token 与活动租约不一致",
                details={
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "lease_fencing_token": lease.fencing_token,
                },
            )
        return lease

    # ---- maintenance ---------------------------------------------------
    def expire_stale(self, now: datetime | None = None) -> int:
        return self._repository.expire_stale(_normalize_utc(now or utc_now()))

    # ---- internals -----------------------------------------------------
    def _raise_invalid_or_stale(
        self, lease_id: str, worker_id: str, fencing_token: int, now: datetime
    ) -> None:
        lease = self._repository.get(lease_id)
        if lease is None:
            raise WorkerLeaseInvalidError(
                "租约不存在", details={"lease_id": lease_id}
            )
        if lease.status != LeaseStatus.ACTIVE:
            raise WorkerLeaseInvalidError(
                "租约不是活动状态",
                details={"lease_id": lease_id, "status": lease.status.value},
            )
        if lease.worker_id != worker_id:
            raise WorkerLeaseInvalidError(
                "租约不属于该 Worker",
                details={"lease_id": lease_id, "worker_id": worker_id},
            )
        if _normalize_utc(lease.expires_at) <= now:
            raise WorkerLeaseInvalidError(
                "租约已过期", details={"lease_id": lease_id}
            )
        max_token = self._repository.max_fencing_token(lease.task_id)
        if fencing_token < max_token:
            raise FencingTokenStaleError(
                "fencing token 已过期",
                details={
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "current_max_token": max_token,
                },
            )
        if fencing_token != lease.fencing_token:
            raise WorkerLeaseInvalidError(
                "fencing token 与租约不匹配",
                details={
                    "lease_id": lease_id,
                    "fencing_token": fencing_token,
                    "lease_fencing_token": lease.fencing_token,
                },
            )
        raise WorkerLeaseInvalidError(
            "租约条件更新未生效", details={"lease_id": lease_id}
        )


__all__ = ["LeaseRepository", "LeaseService"]
