"""租约心跳线程：续租失败必须立即停止提交。

为什么不把心跳塞进 `Worker`：`core/worker/` 是领域层，不应关心线程/信号；
心跳属于**载体**职责（进程内并发编排），因此放在组合根这一层的 `workers/`。

不变式：

- `lost=True` 一旦置位就**永不复位**：本租约已经不可信，只能放弃当前任务；
- 续租失败时 `Worker.heartbeat()` 自身已把内部租约置空，后续 `submit()` 会立刻
  抛 `WorkerLeaseInvalidError`（双保险，见 `core/worker/worker.py:80-94`）；
- 线程是 daemon，进程退出不会被它拖住；`stop()` 必须 join，避免遗留线程写库。
"""

from __future__ import annotations

import threading
from typing import Protocol

from core.logging import get_logger

logger = get_logger(__name__)

ALERT_LEASE_HEARTBEAT_LOST = "WORKER_LEASE_HEARTBEAT_LOST"


class HeartbeatWorkerPort(Protocol):
    """心跳所需的最小面：`Worker.heartbeat(ttl_s)`。"""

    @property
    def worker_id(self) -> str: ...

    def heartbeat(self, ttl_s: float) -> object: ...


class LeaseHeartbeat:
    """周期续租；失败置 `lost=True` 并退出线程。"""

    def __init__(
        self,
        worker: HeartbeatWorkerPort,
        *,
        interval_s: float,
        ttl_s: float,
        name: str = "dg-worker-heartbeat",
    ) -> None:
        if interval_s <= 0:
            raise ValueError("heartbeat interval_s 必须为正数")
        if ttl_s <= 0:
            raise ValueError("heartbeat ttl_s 必须为正数")
        if interval_s >= ttl_s:
            raise ValueError("heartbeat interval_s 必须小于租约 ttl_s，否则必然掉租约")
        self._worker = worker
        self._interval_s = float(interval_s)
        self._ttl_s = float(ttl_s)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._name = name

    # ---- state ---------------------------------------------------------
    @property
    def lost(self) -> bool:
        """本租约是否已经丢失（续租失败）。"""
        return self._lost.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("LeaseHeartbeat 只能启动一次")
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """停止并等待线程退出；幂等。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))

    # ---- worker --------------------------------------------------------
    def _run(self) -> None:
        # 首次等待一个间隔再续租：acquire 已经把 expires_at 设为 now + ttl。
        while not self._stop.wait(self._interval_s):
            try:
                self._worker.heartbeat(self._ttl_s)
            except Exception as exc:  # noqa: BLE001 - 任何续租失败都必须停
                self._lost.set()
                self._stop.set()
                logger.error(
                    "Worker 租约续租失败，停止提交新步骤",
                    extra={
                        "alert": ALERT_LEASE_HEARTBEAT_LOST,
                        "worker_id": self._worker.worker_id,
                        "error": type(exc).__name__,
                        "code": getattr(getattr(exc, "code", None), "value", None),
                    },
                )
                return


__all__ = ["ALERT_LEASE_HEARTBEAT_LOST", "HeartbeatWorkerPort", "LeaseHeartbeat"]
