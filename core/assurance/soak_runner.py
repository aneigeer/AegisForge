"""P6-T2：多 Worker 并发长稳压测与故障转移驱动（真实统计，绝不伪造 3600s）。

设计原则（INV-06）：
- 驱动多个独立 Worker 线程/进程并发争抢任务队列，并在运行中途主动注入一次
  Worker 停止/故障转移，校验零重复执行（``duplicate_executions == 0``）；
- 产出的 :class:`SoakReport` 一律使用真实计时与真实样本量（``measured_sample_size`` /
  ``measured_duration_s`` / ``p50_ms`` / ``p95_ms``）；
- ``meets_frozen_plan`` 仅在实测达到 ``SOAK_SAMPLE_SIZE`` (200) 且
  ``SOAK_DURATION_S`` (3600s) 时才为 ``True``，短时验证绝不冒充 1 小时压测。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.assurance.soak import (
    SOAK_DURATION_S,
    SOAK_SAMPLE_SIZE,
    SoakReport,
    build_soak_report,
)


@dataclass(frozen=True)
class SoakRunOutcome:
    report: SoakReport
    worker_count: int
    completed_tasks: int
    failed_tasks: int
    duplicate_executions: int
    failover_triggered: bool
    meets_frozen_plan: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "worker_count": self.worker_count,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "duplicate_executions": self.duplicate_executions,
            "failover_triggered": self.failover_triggered,
            "meets_frozen_plan": self.meets_frozen_plan,
            "soak_report": self.report.model_dump(mode="json"),
        }


class MultiWorkerSoakRunner:
    """并发多 Worker 长稳与故障接管驱动器。"""

    def __init__(
        self,
        *,
        work_fn: Callable[[str, int], bool],
        worker_count: int = 2,
        target_tasks: int = 40,
        max_duration_s: float = 60.0,
        inject_failover_at: int | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count 必须 >= 1")
        if target_tasks < 1:
            raise ValueError("target_tasks 必须 >= 1")
        if max_duration_s <= 0:
            raise ValueError("max_duration_s 必须 > 0")
        self._work_fn = work_fn
        self._worker_count = worker_count
        self._target_tasks = target_tasks
        self._max_duration_s = max_duration_s
        self._inject_failover_at = (
            inject_failover_at
            if inject_failover_at is not None
            else (max(1, target_tasks // 3) if worker_count >= 2 else None)
        )

    def run(self) -> SoakRunOutcome:
        lock = threading.Lock()
        next_task_idx = 0
        claimed_ids: set[int] = set()
        executed_counts: dict[int, int] = {}
        latencies_ms: list[float] = []
        completed = 0
        failed = 0
        failover_triggered = False

        stop_flags = [threading.Event() for _ in range(self._worker_count)]
        started = time.monotonic()
        deadline = started + self._max_duration_s

        def _worker_loop(worker_idx: int) -> None:
            nonlocal next_task_idx, completed, failed, failover_triggered
            worker_id = f"soak_worker_{worker_idx + 1:02d}"
            while not stop_flags[worker_idx].is_set() and time.monotonic() < deadline:
                with lock:
                    if next_task_idx >= self._target_tasks:
                        return
                    task_idx = next_task_idx
                    next_task_idx += 1
                    # 单调租约模拟：每个 task_idx 仅允许被领取一次
                    assert task_idx not in claimed_ids
                    claimed_ids.add(task_idx)
                    if (
                        self._inject_failover_at is not None
                        and not failover_triggered
                        and worker_idx == 0
                        and task_idx >= self._inject_failover_at
                    ):
                        # 模拟 worker_01 在处理该任务前突然下线，把任务交还给其余活跃 Worker
                        claimed_ids.remove(task_idx)
                        next_task_idx -= 1
                        failover_triggered = True
                        stop_flags[0].set()
                        return

                t0 = time.perf_counter()
                ok = False
                try:
                    ok = bool(self._work_fn(worker_id, task_idx))
                except Exception:  # noqa: BLE001
                    ok = False
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                with lock:
                    executed_counts[task_idx] = executed_counts.get(task_idx, 0) + 1
                    latencies_ms.append(elapsed_ms)
                    if ok:
                        completed += 1
                    else:
                        failed += 1

        threads = [
            threading.Thread(target=_worker_loop, args=(i,), daemon=True)
            for i in range(self._worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self._max_duration_s + 5.0)

        measured_duration = max(0.001, time.monotonic() - started)
        duplicates = sum(max(0, count - 1) for count in executed_counts.values())
        sample_size = len(latencies_ms)
        report = build_soak_report(
            measured={
                "sample_size": max(1, sample_size),
                "duration_s": round(measured_duration, 4),
                "latencies_ms": latencies_ms,
            }
        )
        meets_plan = (
            sample_size >= SOAK_SAMPLE_SIZE
            and measured_duration >= SOAK_DURATION_S
            and duplicates == 0
            and failed == 0
        )
        return SoakRunOutcome(
            report=report,
            worker_count=self._worker_count,
            completed_tasks=completed,
            failed_tasks=failed,
            duplicate_executions=duplicates,
            failover_triggered=failover_triggered,
            meets_frozen_plan=meets_plan,
        )


__all__ = ["MultiWorkerSoakRunner", "SoakRunOutcome"]
