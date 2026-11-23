"""P6-T2 多 Worker 长稳压测 CLI。

用法：
    python scripts/run_soak.py --workers 2 --tasks 200 --duration-s 3600 --out runs/soak.json
    python scripts/run_soak.py --workers 2 --tasks 20 --duration-s 10 --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.assurance.soak_runner import MultiWorkerSoakRunner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBGuardian 多 Worker 并发长稳与故障接管压测")
    parser.add_argument("--workers", type=int, default=2, help="并发 Worker 数（建议 >= 2）")
    parser.add_argument("--tasks", type=int, default=40, help="压测任务总数")
    parser.add_argument("--duration-s", type=float, default=30.0, help="最大运行时长（秒）")
    parser.add_argument("--step-ms", type=float, default=2.0, help="单任务模拟/执行开销（ms）")
    parser.add_argument("--out", default=None, help="可选：JSON 报告输出路径")
    parser.add_argument("--json", action="store_true", help="控制台输出 JSON")
    args = parser.parse_args(argv)

    sleep_s = max(0.0, args.step_ms / 1000.0)

    def _work(worker_id: str, task_idx: int) -> bool:
        if sleep_s > 0:
            time.sleep(sleep_s)
        return bool(worker_id) and task_idx >= 0

    outcome = MultiWorkerSoakRunner(
        work_fn=_work,
        worker_count=args.workers,
        target_tasks=args.tasks,
        max_duration_s=args.duration_s,
    ).run()
    payload = outcome.to_public_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        r = outcome.report
        print(
            f"workers={outcome.worker_count} completed={outcome.completed_tasks} "
            f"failed={outcome.failed_tasks} dup={outcome.duplicate_executions} "
            f"failover={outcome.failover_triggered} duration_s={r.measured_duration_s} "
            f"p50_ms={r.p50_ms} p95_ms={r.p95_ms} meets_3600s_plan={outcome.meets_frozen_plan}"
        )
    return 0 if outcome.failed_tasks == 0 and outcome.duplicate_executions == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
