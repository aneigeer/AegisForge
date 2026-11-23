"""W08 T06 / AC-17 第三项：`--once` **真实子进程**端到端（核对进生产调度）。

与 W07 冻结的 `tests/integration/test_worker_process.py` **互不影响**（本卡新建，
不改冻结文件）。本文件只验证 W08 新增的调度事实：

1. 子进程 `python -m workers --once` 跑完一轮，先 `sweep()` 再发现候选；
2. 一条"已 `Prepared`、无 `Observed`"的历史调用被收敛为 `NEEDS_RECONCILIATION`；
3. 该任务的 `tool_calls` 增量 **0**（核对绝不重放写）；
4. `RECONCILIATION_SWEPT` 事件真的落库（设计 §5 第 8 行，调度伪任务承载）；
5. 任务链上不出现 `TOOL_CALL_RETRIED`。

环境说明：需要真实 MySQL（控制库 + LAB）。不可达即 skip 并写明原因。
控制库只追加表无法 DELETE（`DG_ADMIN_DSN` 不可用），清理只做可做的两件事：
恢复 LAB 目标行 + warning 列出未清理事实，绝不静默吞掉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from core.errors import DBGuardianError
from core.execution.step_records import StepPrepared, StepRecordRepository
from core.tasks.models import TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry
from workers.runner import SCHEDULER_TASK_ID

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TENANT = "tenant_local"
ORDER_ID = 2001
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"
PREPARED_AGE_S = 600.0


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


def _run_worker(*args: str, timeout_s: float = 180.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # 明确关闭启动自愈：否则进程启动阶段可能抢先改动历史任务，掩盖本次注入。
    env["DG_RECOVER_ON_STARTUP"] = "false"
    return subprocess.run(
        [sys.executable, "-m", "workers", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )


def _json_line(stdout: str) -> dict[str, Any]:
    for line in reversed([item for item in stdout.splitlines() if item.strip()]):
        text = line.strip()
        if text.startswith("{"):
            return dict(json.loads(text))
    raise AssertionError(f"未找到 JSON 输出：{stdout[-2000:]}")


# ---------------------------------------------------------------------------
# 库内事实
# ---------------------------------------------------------------------------


def _count(client: Any, table: str, task_id: str) -> int:
    rows = client.fetch_all(
        f"SELECT COUNT(*) AS total FROM dg_system.{table} WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["total"]) if rows else 0


def _tool_calls_total(client: Any) -> int:
    rows = client.fetch_all("SELECT COUNT(*) AS total FROM dg_system.tool_calls", ())
    return int(rows[0]["total"]) if rows else 0


def _scheduler_max_seq(client: Any) -> int:
    rows = client.fetch_all(
        "SELECT MAX(seq) AS max_seq FROM dg_system.task_events WHERE task_id = %s",
        (SCHEDULER_TASK_ID,),
    )
    value = rows[0]["max_seq"] if rows else None
    return 0 if value is None else int(value)


def _note(client: Any) -> str | None:
    rows = client.fetch_all(NOTE_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 scripts/bootstrap_mysql.py")
    return rows[0].get("note")


@pytest.fixture(scope="module", autouse=True)
def lab_reachable(settings, control_client, ro_client, rw_client) -> None:
    for name, client in (
        ("控制库", control_client),
        ("LAB 只读", ro_client),
        ("LAB 可写", rw_client),
    ):
        try:
            client.ping()
        except DBGuardianError as exc:
            pytest.skip(f"需真实 MySQL LAB：{name}不可用（{exc.code.value}）")
    _note(rw_client)


@pytest.fixture
def crashed_task(settings, control_client, rw_client):
    """造一条"执行前崩溃"的历史调用：任务 EXECUTING + Prepared 证据，无 Observed。

    不创建审批：任务不在 `WAITING_APPROVAL`，不可能成为候选，因此子进程的
    `--once` 只会核对它，不会执行它——这正是本用例要证明的隔离。
    """
    repository = TaskRepository(control_client)
    service = TaskService(repository, ToolRegistry())
    before_note = _note(rw_client)

    task = service.create_task("W08 T06 --once 端到端（执行前崩溃）")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)

    tool_call_id = f"tc_w08_once_{uuid4().hex[:12]}"
    StepRecordRepository(control_client).insert_prepared(
        StepPrepared(
            prepared_id=f"prep_{uuid4().hex}",
            tenant_id=TENANT,
            task_id=task.task_id,
            run_id=task.run_id,
            tool_call_id=tool_call_id,
            tool_name="mysql_lab_update_order_note",
            tool_version="1.0",
            policy_version=str(settings.policy_version),
            environment_id=task.environment_id,
            request={"param_keys": ["note", "order_id"]},
            prepared_at=utc_now() - timedelta(seconds=PREPARED_AGE_S),
        )
    )

    payload = {
        "task_id": task.task_id,
        "tool_call_id": tool_call_id,
        "before_note": before_note,
    }
    try:
        yield payload
    finally:
        try:
            rw_client.execute(RESTORE_SQL, (before_note, ORDER_ID))
        except DBGuardianError:
            pass
        warnings.warn(
            "控制库只追加事实未清理（需可用的 DG_ADMIN_DSN）："
            f"task_id={task.task_id}",
            stacklevel=1,
        )


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def test_once_process_reconciles_prepared_without_observed(
    crashed_task, control_client
) -> None:
    task_id = crashed_task["task_id"]
    before_calls_task = _count(control_client, "tool_calls", task_id)
    before_calls_total = _tool_calls_total(control_client)
    before_seq = _scheduler_max_seq(control_client)

    result = _run_worker("--once", timeout_s=180)

    # 退出码只允许 0/1：1 = 库内还有其它未收敛候选（本用例不依赖该数字）。
    assert result.returncode in (0, 1), result.stderr
    payload = _json_line(result.stdout)
    assert payload["event"] == "worker_report"
    assert payload["reconciled"] >= 1, payload
    assert "reconciled" in payload

    # ① 任务被收敛为"需核对"，且没有停留在 EXECUTING。
    rows = control_client.fetch_all(
        "SELECT status FROM dg_system.tasks WHERE task_id = %s LIMIT 1", (task_id,)
    )
    assert rows and str(rows[0]["status"]) == TaskStatus.NEEDS_RECONCILIATION.value

    # ② 本任务零工具调用：核对不重放写。
    assert _count(control_client, "tool_calls", task_id) == before_calls_task == 0
    # 全局增量只能来自"其它被处理的候选"，不得超过报告里的 executed。
    assert _tool_calls_total(control_client) - before_calls_total <= int(payload["executed"])

    # ③ 目标行没被改动（从未执行）：目标行断言放在下一个用例里单独验证。
    assert _count(control_client, "step_observed", task_id) == 0
    events = control_client.fetch_all(
        "SELECT event_type FROM dg_system.task_events WHERE task_id = %s ORDER BY seq",
        (task_id,),
    )
    types = [str(row["event_type"]) for row in events]
    assert "TOOL_CALL_RETRIED" not in types

    # ④ RECONCILIATION_SWEPT 真的落库（本次运行新增，按 seq 增量定位）。
    swept = control_client.fetch_all(
        "SELECT seq, payload_json FROM dg_system.task_events "
        "WHERE task_id = %s AND event_type = 'RECONCILIATION_SWEPT' AND seq > %s "
        "ORDER BY seq ASC",
        (SCHEDULER_TASK_ID, before_seq),
    )
    assert swept, "子进程必须留下 RECONCILIATION_SWEPT 事件"
    swept_payload = swept[0]["payload_json"]
    text = (
        swept_payload
        if isinstance(swept_payload, str)
        else json.dumps(swept_payload, ensure_ascii=False, default=str)
    )
    for key in ("scanned", "converged", "conclusions", "duration_ms"):
        assert key in text, f"RECONCILIATION_SWEPT 缺少冻结字段 {key}: {text}"


def test_once_process_keeps_lab_target_row_untouched(crashed_task, rw_client) -> None:
    """独立断言"目标行未被写入"：执行前崩溃不可能改变 LAB 数据。"""
    before_note = crashed_task["before_note"]

    _run_worker("--once", timeout_s=180)

    assert _note(rw_client) == before_note
