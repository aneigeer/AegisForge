"""P6-T2 单元测试：多 Worker 并发长稳驱动与故障转移零重复验证。"""

from __future__ import annotations

import threading
import time

from core.assurance.soak_runner import MultiWorkerSoakRunner


def test_multi_worker_soak_failover_zero_duplicates() -> None:
    seen_workers: set[str] = set()
    lock = threading.Lock()

    def _work(worker_id: str, task_idx: int) -> bool:
        with lock:
            seen_workers.add(worker_id)
        time.sleep(0.002)
        return True

    outcome = MultiWorkerSoakRunner(
        work_fn=_work,
        worker_count=3,
        target_tasks=30,
        max_duration_s=10.0,
        inject_failover_at=8,
    ).run()

    assert outcome.completed_tasks == 30
    assert outcome.failed_tasks == 0
    assert outcome.duplicate_executions == 0
    assert outcome.failover_triggered is True
    assert len(seen_workers) >= 2
    assert outcome.report.status == "COMPLETED"
    assert outcome.report.measured_sample_size == 30
    assert outcome.report.measured_duration_s is not None and outcome.report.measured_duration_s > 0
    assert outcome.report.p50_ms is not None and outcome.report.p95_ms is not None
    # 短时运行绝不冒充 3600s 完整长稳计划
    assert outcome.meets_frozen_plan is False
