"""统一资源与沙箱调度层（UPG-02）。

按 DomainPlugin 类型统一路由并管理隔离沙箱生命周期：

======================  ====================================  ================
模式                    适用域                                典型生命周期
======================  ====================================  ================
EPHEMERAL               轻量数据库（MySQL/PG/Redis）          秒级拉起、即用即毁
WARM_POOL               重量数据库（TiDB/Oracle）             常驻温备、逻辑隔离
VCLUSTER                K8s（Kind / vCluster）                虚拟隔离集群
MICROVM                 主机（Docker / MicroVM）              轻量隔离实例
TRAFFIC_REPLAY          AppSec（App + DB 联合 + 影子流量）     流量回放沙箱
======================  ====================================  ================

调度器只做资源账本：**超配即拒绝（fail closed）**、**TTL 到期强制回收**，
绝不把"沙箱不够用"降级成"直接打生产"。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

MODE_EPHEMERAL = "EPHEMERAL"
MODE_WARM_POOL = "WARM_POOL"
MODE_VCLUSTER = "VCLUSTER"
MODE_MICROVM = "MICROVM"
MODE_TRAFFIC_REPLAY = "TRAFFIC_REPLAY"

ALL_MODES: tuple[str, ...] = (
    MODE_EPHEMERAL,
    MODE_WARM_POOL,
    MODE_VCLUSTER,
    MODE_MICROVM,
    MODE_TRAFFIC_REPLAY,
)

_WARM_POOL_ENGINES = frozenset({"tidb", "oracle"})


class SandboxQuotaExceeded(RuntimeError):
    """并发沙箱配额耗尽：拒绝新租约，不允许降级绕过。"""


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str
    quota_cpu: float
    quota_mem_mb: int
    ttl_sec: int


POLICIES: dict[str, SandboxPolicy] = {
    MODE_EPHEMERAL: SandboxPolicy(MODE_EPHEMERAL, 1.0, 1024, 300),
    MODE_WARM_POOL: SandboxPolicy(MODE_WARM_POOL, 2.0, 4096, 900),
    MODE_VCLUSTER: SandboxPolicy(MODE_VCLUSTER, 2.0, 4096, 900),
    MODE_MICROVM: SandboxPolicy(MODE_MICROVM, 1.0, 2048, 600),
    MODE_TRAFFIC_REPLAY: SandboxPolicy(MODE_TRAFFIC_REPLAY, 2.0, 4096, 600),
}


class SandboxManager:
    """沙箱生命周期与配额账本（纯内存、确定性、可快照）。"""

    def __init__(
        self,
        *,
        max_concurrent: int = 4,
        clock: Callable[[], float] = time.monotonic,
        max_leases_per_asset: int = 3,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 至少为 1")
        self.max_concurrent = max_concurrent
        self.max_leases_per_asset = max_leases_per_asset
        self._clock = clock
        self._leases: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._reclaimed = 0

    # -- 路由 ----------------------------------------------------------
    @staticmethod
    def route(domain: str, engine: str = "") -> str:
        normalized = (domain or "").lower()
        if normalized == "database":
            return MODE_WARM_POOL if (engine or "").lower() in _WARM_POOL_ENGINES else MODE_EPHEMERAL
        if normalized == "k8s":
            return MODE_VCLUSTER
        if normalized == "host":
            return MODE_MICROVM
        if normalized == "appsec":
            return MODE_TRAFFIC_REPLAY
        return MODE_EPHEMERAL

    # -- 生命周期 ------------------------------------------------------
    def acquire(
        self,
        *,
        domain: str,
        asset_id: str,
        engine: str = "",
        mode: str | None = None,
        ttl_sec: int | None = None,
    ) -> dict[str, Any]:
        self.reap()
        if len(self._leases) >= self.max_concurrent:
            raise SandboxQuotaExceeded(
                f"并发沙箱配额已满（{len(self._leases)}/{self.max_concurrent}），拒绝新租约"
            )
        active_for_asset = sum(1 for lease in self._leases.values() if lease["asset_id"] == asset_id)
        if active_for_asset >= self.max_leases_per_asset:
            raise SandboxQuotaExceeded(
                f"资产 {asset_id} 的沙箱租约超过上限 {self.max_leases_per_asset}"
            )

        chosen = mode or self.route(domain, engine)
        if chosen not in POLICIES:
            raise ValueError(f"未知沙箱模式: {chosen}")
        policy = POLICIES[chosen]
        self._seq += 1
        lease = {
            "lease_id": f"sbx-{self._seq:04d}",
            "domain": domain,
            "asset_id": asset_id,
            "engine": engine,
            "mode": policy.mode,
            "state": "ACTIVE",
            "quota_cpu": policy.quota_cpu,
            "quota_mem_mb": policy.quota_mem_mb,
            "ttl_sec": int(ttl_sec if ttl_sec is not None else policy.ttl_sec),
            "created_at_epoch": self._clock(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._leases[lease["lease_id"]] = lease
        return dict(lease)

    def release(self, lease_id: str) -> dict[str, Any]:
        lease = self._leases.pop(lease_id, None)
        if lease is None:
            return {"lease_id": lease_id, "released": False, "reason": "NOT_FOUND"}
        lease["state"] = "RELEASED"
        return {"lease_id": lease_id, "released": True, "mode": lease["mode"], "asset_id": lease["asset_id"]}

    def reap(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """TTL 到期强制回收，返回被回收的租约清单。"""
        moment = self._clock() if now is None else now
        expired = [
            lease for lease in self._leases.values() if moment - lease["created_at_epoch"] > lease["ttl_sec"]
        ]
        for lease in expired:
            self._leases.pop(lease["lease_id"], None)
            self._reclaimed += 1
        return [{"lease_id": item["lease_id"], "mode": item["mode"], "asset_id": item["asset_id"]} for item in expired]

    # -- 可观测性 ------------------------------------------------------
    def active_leases(self) -> list[dict[str, Any]]:
        return [dict(lease) for lease in self._leases.values()]

    def snapshot(self) -> dict[str, Any]:
        by_mode: dict[str, int] = {}
        for lease in self._leases.values():
            by_mode[lease["mode"]] = by_mode.get(lease["mode"], 0) + 1
        return {
            "max_concurrent": self.max_concurrent,
            "active": len(self._leases),
            "by_mode": by_mode,
            "reclaimed_total": self._reclaimed,
            "policies": {mode: asdict(policy) for mode, policy in POLICIES.items()},
        }


__all__ = [
    "ALL_MODES",
    "MODE_EPHEMERAL",
    "MODE_MICROVM",
    "MODE_TRAFFIC_REPLAY",
    "MODE_VCLUSTER",
    "MODE_WARM_POOL",
    "POLICIES",
    "SandboxManager",
    "SandboxPolicy",
    "SandboxQuotaExceeded",
]
