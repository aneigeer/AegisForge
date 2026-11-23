"""Prepared/Observed 与核对（P0-16/P0-18/P0-19，T03 验收）。

核心断言：写操作前证据落盘 → 崩溃后**只收敛状态、绝不重放工具**。
`tool_calls` 行数与工具执行次数在核对前后必须完全相等（= 0）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from core.audit.service import AuditService
from core.errors import DatabasePermissionError, StateConflictError
from core.execution.reconciler import (
    CONCLUSION_MANUAL_INTERVENTION,
    CONCLUSION_NEEDS_RECONCILIATION,
    REASON_ALREADY_CONVERGED,
    REASON_NOT_OBSERVED,
    Reconciler,
)
from core.execution.step_records import StepObserved, StepPrepared, StepRecordRepository
from core.tasks.models import Task, TaskStatus, new_id, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlClient
from database.mysql.transaction import execute_in, transaction

pytestmark = pytest.mark.integration

# 历史清理清单：W10 L1 起**不再执行**（`DG_ADMIN_DSN` root 1045 不可用，
# 且这些表是只追加证据表，W09 全局纪律禁止 DELETE 历史残留）。保留常量仅为
# 让审计能看到"曾经清过哪些表"，不要恢复使用。
_CLEANUP_TABLES = (
    "step_observed",
    "step_prepared",
    "approval_consumptions",
    "approvals",
    "task_events",
    "tool_calls",
    "task_steps",
    "checkpoints",
    "execution_envelopes",
    "worker_leases",
    "tasks",
)

TOOL_CALL_ID_PREFIX = "call"


@pytest.fixture()
def created_tasks(control_client, monkeypatch):
    """记录本用例创建的任务，用例结束后做**只读**核对。

    为何不再清理（`docs/w09/10-lead-rulings.md` §4 对 M01 的同法裁定）：

    - `DG_ADMIN_DSN` root 1045 在本机从未可用（W08 E-2），
      `admin_client.execute(DELETE ...)` 建连即抛 → teardown 升级为 ERROR；
    - 即便凭据可用也不得执行 DELETE：`step_prepared` / `step_observed` /
      `task_events` 是只追加证据表，W09 全局纪律与 `dg_ctl` 授权都禁止
      删除历史残留；
    - 因此改为「只断言本用例范围内事实」：任务行必须仍可读（证明用例确实
      落库），残留行继续留存、不再尝试清理。
    """
    tracked: list[Task] = []
    original = TaskService.create_task

    def tracked_create(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        tracked.append(task)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked_create)
    yield tracked
    repository = TaskRepository(control_client)
    for task in tracked:
        assert (
            repository.get_task(task.task_id, tenant_id=task.tenant_id).task_id
            == task.task_id
        )


@pytest.fixture()
def task_service(control_client) -> TaskService:
    return TaskService(TaskRepository(control_client), ToolRegistry())


@pytest.fixture()
def audit(control_client) -> AuditService:
    return AuditService(TaskRepository(control_client))


@pytest.fixture()
def step_records(control_client, audit) -> StepRecordRepository:
    return StepRecordRepository(control_client, audit=audit)


@pytest.fixture()
def reconciler(step_records, control_client, audit) -> Reconciler:
    return Reconciler(step_records, TaskRepository(control_client), audit)


def _executing_task(task_service: TaskService, goal: str):
    task = task_service.create_task(goal)
    task_service.transition(task.task_id, TaskStatus.PLANNING)
    task = task_service.transition(task.task_id, TaskStatus.EXECUTING)
    assert task.status is TaskStatus.EXECUTING
    return task


def _prepared(task, *, tool_call_id: str | None = None, approval_id: str | None = None) -> StepPrepared:
    return StepPrepared(
        prepared_id=new_id("spr"),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=tool_call_id or new_id(TOOL_CALL_ID_PREFIX),
        tool_name="mysql_update_rows",
        tool_version="1.0",
        policy_version="w06-v1",
        environment_id=task.environment_id,
        request={"table": "orders", "id": 1001, "status": "PAID"},
        approval_id=approval_id or new_id("apr"),
        action_digest="a" * 64,
        idempotency_key=new_id("idem"),
        prepared_at=utc_now() - timedelta(minutes=5),
    )


def _observed(task, prepared: StepPrepared) -> StepObserved:
    return StepObserved(
        observed_id=new_id("sob"),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=prepared.tool_call_id,
        tool_name=prepared.tool_name,
        outcome="UNKNOWN_OUTCOME",
        started_at=prepared.prepared_at,
        finished_at=prepared.prepared_at + timedelta(milliseconds=20),
        duration_ms=20,
        result={"rows_affected": None},
        observed_at=utc_now(),
    )


def _count_tool_calls(control_client, task_id: str) -> int:
    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS total FROM dg_system.tool_calls WHERE task_id = %s",
        (task_id,),
    )
    return int(rows[0]["total"]) if rows else 0


def test_prepared_is_found_unobserved_and_converges(
    task_service, step_records, reconciler, audit, control_client, created_tasks
) -> None:
    task = _executing_task(task_service, "崩溃恢复用例")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)

    unobserved = step_records.find_unobserved(
        tenant_id=task.tenant_id, older_than_s=0, task_id=task.task_id
    )
    assert [item.tool_call_id for item in unobserved] == [prepared.tool_call_id]

    results = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    assert len(results) == 1
    result = results[0]
    assert result.conclusion == CONCLUSION_NEEDS_RECONCILIATION
    assert result.reason == REASON_NOT_OBSERVED
    assert result.transitioned is True
    assert result.approval_id == prepared.approval_id

    task_after = task_service.get_task(task.task_id)
    assert task_after.status is TaskStatus.NEEDS_RECONCILIATION

    # 绝不重放：核对过程没有产生任何工具调用记录
    assert _count_tool_calls(control_client, task.task_id) == 0

    # 结论写入只追加账本，且明确记录"未发生重放"
    events, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["RECONCILIATION_REQUIRED"],
        limit=50,
    )
    assert len(events) == 1
    assert events[0].payload["replay_attempted"] is False
    assert events[0].payload["prepared_id"] == prepared.prepared_id

    # 再扫一次：已收敛，不重复迁移状态
    again = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    assert again[0].conclusion == CONCLUSION_NEEDS_RECONCILIATION
    assert again[0].transitioned is False
    assert again[0].reason == REASON_ALREADY_CONVERGED


def test_observed_evidence_removes_call_from_unobserved_set(
    task_service, step_records, created_tasks
) -> None:
    task = _executing_task(task_service, "已观测不再核对")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    step_records.insert_observed(_observed(task, prepared))

    unobserved = step_records.find_unobserved(
        tenant_id=task.tenant_id, older_than_s=0, task_id=task.task_id
    )
    assert unobserved == []
    observed = step_records.get_observed(prepared.tool_call_id)
    assert observed is not None
    assert observed.outcome == "UNKNOWN_OUTCOME"


def test_reconciler_skips_calls_that_gained_observation(
    task_service, step_records, reconciler, audit, created_tasks
) -> None:
    task = _executing_task(task_service, "竞态：核对前已观测")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    step_records.insert_observed(_observed(task, prepared))

    results = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    assert results == []
    assert task_service.get_task(task.task_id).status is TaskStatus.EXECUTING
    events, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["RECONCILIATION_REQUIRED"],
        limit=10,
    )
    assert events == []


def test_older_than_threshold_filters_recent_prepared(
    task_service, step_records, created_tasks
) -> None:
    task = _executing_task(task_service, "新鲜证据不参与核对")
    prepared = StepPrepared(
        prepared_id=new_id("spr"),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=new_id(TOOL_CALL_ID_PREFIX),
        tool_name="mysql_update_rows",
        tool_version="1.0",
        policy_version="w06-v1",
        environment_id=task.environment_id,
        request={},
        prepared_at=utc_now(),
    )
    step_records.insert_prepared(prepared)

    assert step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=60, task_id=task.task_id) == []
    assert len(step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=0, task_id=task.task_id)) == 1


def test_duplicate_prepared_is_rejected(task_service, step_records, created_tasks) -> None:
    """同一 tool_call_id 不允许准备两次（否则可能二次执行）。"""
    task = _executing_task(task_service, "重复准备")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    duplicate = StepPrepared(
        prepared_id=new_id("spr"),
        tenant_id=prepared.tenant_id,
        task_id=prepared.task_id,
        run_id=prepared.run_id,
        tool_call_id=prepared.tool_call_id,
        tool_name=prepared.tool_name,
        tool_version=prepared.tool_version,
        policy_version=prepared.policy_version,
        environment_id=prepared.environment_id,
        request=prepared.request,
    )
    with pytest.raises(StateConflictError):
        step_records.insert_prepared(duplicate)


def test_duplicate_observed_is_rejected(task_service, step_records, created_tasks) -> None:
    task = _executing_task(task_service, "重复观测")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    step_records.insert_observed(_observed(task, prepared))
    with pytest.raises(StateConflictError):
        step_records.insert_observed(_observed(task, prepared))


def test_transaction_rolls_back_on_exception(task_service, control_client, created_tasks) -> None:
    """`transaction()` 异常后行必须不存在（T01 验收：回滚用例）。"""
    task = _executing_task(task_service, "事务回滚")
    prepared = _prepared(task)

    with pytest.raises(RuntimeError):
        with transaction(control_client) as conn:
            execute_in(
                control_client,
                conn,
                "INSERT INTO dg_system.step_prepared (prepared_id, tenant_id, task_id, "
                "run_id, step_id, tool_call_id, envelope_id, approval_id, tool_name, "
                "tool_version, action_digest, idempotency_key, policy_version, "
                "environment_id, request_json, prepared_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    prepared.prepared_id,
                    prepared.tenant_id,
                    prepared.task_id,
                    prepared.run_id,
                    prepared.step_id,
                    prepared.tool_call_id,
                    prepared.envelope_id,
                    prepared.approval_id,
                    prepared.tool_name,
                    prepared.tool_version,
                    prepared.action_digest,
                    prepared.idempotency_key,
                    prepared.policy_version,
                    prepared.environment_id,
                    "{}",
                    prepared.prepared_at.replace(tzinfo=None),
                ),
            )
            raise RuntimeError("模拟执行失败")

    rows = control_client.fetch_all(
        "SELECT prepared_id FROM dg_system.step_prepared WHERE prepared_id = %s",
        (prepared.prepared_id,),
    )
    assert rows == []


def test_transaction_commits_on_success(task_service, control_client, created_tasks) -> None:
    task = _executing_task(task_service, "事务提交")
    prepared = _prepared(task)

    with transaction(control_client) as conn:
        execute_in(
            control_client,
            conn,
            "INSERT INTO dg_system.step_prepared (prepared_id, tenant_id, task_id, run_id, "
            "step_id, tool_call_id, envelope_id, approval_id, tool_name, tool_version, "
            "action_digest, idempotency_key, policy_version, environment_id, request_json, "
            "prepared_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                prepared.prepared_id,
                prepared.tenant_id,
                prepared.task_id,
                prepared.run_id,
                prepared.step_id,
                prepared.tool_call_id,
                prepared.envelope_id,
                prepared.approval_id,
                prepared.tool_name,
                prepared.tool_version,
                prepared.action_digest,
                prepared.idempotency_key,
                prepared.policy_version,
                prepared.environment_id,
                "{}",
                prepared.prepared_at.replace(tzinfo=None),
            ),
        )

    rows = control_client.fetch_all(
        "SELECT prepared_id FROM dg_system.step_prepared WHERE prepared_id = %s",
        (prepared.prepared_id,),
    )
    assert len(rows) == 1


def test_prepared_and_observed_tables_reject_update_and_delete(
    task_service, step_records, control_client, created_tasks
) -> None:
    """有真实行时仍然拒绝 UPDATE/DELETE（证据不可改写，不只是空表巧合）。"""
    task = _executing_task(task_service, "证据只追加")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    step_records.insert_observed(_observed(task, prepared))

    for table in ("step_prepared", "step_observed"):
        with pytest.raises(DatabasePermissionError):
            control_client.execute(f"UPDATE dg_system.{table} SET task_id = 'x' WHERE 1 = 1")
        with pytest.raises(DatabasePermissionError):
            control_client.execute(f"DELETE FROM dg_system.{table} WHERE 1 = 1")


def test_sweep_converges_terminal_task_to_manual_intervention(
    task_service, step_records, reconciler, created_tasks
) -> None:
    """任务已在终态时无法迁移，必须如实转人工介入而不是猜测执行结果。"""
    task = _executing_task(task_service, "终态任务")
    prepared = _prepared(task)
    step_records.insert_prepared(prepared)
    task_service.transition(task.task_id, TaskStatus.VERIFYING)
    task = task_service.transition(task.task_id, TaskStatus.COMPLETED)
    assert task.status is TaskStatus.COMPLETED

    results = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    assert len(results) == 1
    assert results[0].conclusion == CONCLUSION_MANUAL_INTERVENTION
    assert results[0].transitioned is False
    # 终态不被悄悄改写
    assert task_service.get_task(task.task_id).status is TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# T9 崩溃恢复：子进程在 StepPrepared 落盘后被强制终止（真实 Popen + kill）
# ---------------------------------------------------------------------------

# 子进程脚本：落盘 step_prepared 后阻塞，等待被父进程强制终止。
# 关键点：**不**靠文件/端口通知，父进程直接轮询控制库确认行已提交。
_CRASH_CHILD_SCRIPT = r"""
import sys
import time
from datetime import datetime, timezone

from core.config import Settings
from core.execution.step_records import StepPrepared, StepRecordRepository
from database.mysql.connection import MysqlClient

prepared_id, tenant_id, task_id, run_id, tool_call_id, environment_id = sys.argv[1:7]

settings = Settings()
client = MysqlClient(settings.control_mysql_dsn(), database_id="dg_system", read_only=False)
repository = StepRecordRepository(client)
repository.insert_prepared(
    StepPrepared(
        prepared_id=prepared_id,
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name="mysql_update_rows",
        tool_version="1.0",
        policy_version="w06-v1",
        environment_id=environment_id,
        request={"table": "orders", "id": 1001, "status": "PAID"},
        approval_id="apr_crash_case",
        action_digest="a" * 64,
        idempotency_key="idem_crash_case",
        prepared_at=datetime.now(timezone.utc),
    )
)
print("PREPARED", flush=True)
# 模拟"落盘 Prepared 后、执行前"崩溃窗口：保持进程存活直到被 kill。
while True:
    time.sleep(0.2)
"""

CRASH_ITERATIONS = 10
_ROOT = Path(__file__).resolve().parents[2]


def _wait_for_prepared(control_client, prepared_id: str, *, timeout_s: float = 60.0) -> bool:
    """轮询控制库确认子进程已提交 Prepared 行（不依赖文件或端口）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = control_client.fetch_all(
            "SELECT prepared_id FROM dg_system.step_prepared WHERE prepared_id = %s",
            (prepared_id,),
        )
        if rows:
            return True
        time.sleep(0.05)
    return False


def _crash_once(
    task, prepared_id: str, tool_call_id: str
) -> tuple[bool, str]:
    """启动子进程 → 轮询控制库确认 Prepared 已提交 → kill；返回 (是否落盘, 诊断串)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_ROOT)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CRASH_CHILD_SCRIPT,
            prepared_id,
            task.tenant_id,
            task.task_id,
            task.run_id,
            tool_call_id,
            task.environment_id,
        ],
        cwd=str(_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        from core.config import Settings  # noqa: PLC0415 - 复用同一控制库账号

        client = MysqlClient(
            Settings().control_mysql_dsn(), database_id="dg_system", read_only=False
        )
        committed = _wait_for_prepared(client, prepared_id)
    finally:
        # Windows 下为 TerminateProcess：进程在"已落盘、未执行"窗口被强制终止。
        # communicate() 同时回收管道句柄，避免长跑用例句柄泄漏。
        if proc.poll() is None:
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            proc.kill()
            stdout, stderr = proc.communicate(timeout=30)
    return committed, f"stdout={stdout!r} stderr={stderr!r}"


@pytest.mark.parametrize("iteration", range(CRASH_ITERATIONS))
def test_crash_after_prepared_is_recovered_without_replay(
    iteration: int,
    task_service,
    step_records,
    reconciler,
    audit,
    control_client,
    created_tasks,
) -> None:
    """独立子进程落盘 Prepared 后被 kill → 重启对账得出确定结论且不重复执行。"""
    task = _executing_task(task_service, f"崩溃恢复 T9 #{iteration}")
    prepared_id = new_id("spr")
    tool_call_id = new_id(TOOL_CALL_ID_PREFIX)

    committed, diagnostics = _crash_once(task, prepared_id, tool_call_id)
    if not committed:
        # 子进程启动/建连抖动（Windows 下偶发）时换新 ID 重试一次：
        # 只重试"启动"，不重试"执行"，崩溃语义与断言强度不变。
        prepared_id = new_id("spr")
        tool_call_id = new_id(TOOL_CALL_ID_PREFIX)
        committed, retry_diagnostics = _crash_once(task, prepared_id, tool_call_id)
        assert committed, (
            "两次子进程均未能在超时前提交 Prepared 证据："
            f"iteration={iteration} first=[{diagnostics}] retry=[{retry_diagnostics}]"
        )

    # 崩溃后的事实：有 Prepared、无 Observed
    prepared_rows = control_client.fetch_all(
        "SELECT prepared_id FROM dg_system.step_prepared WHERE prepared_id = %s", (prepared_id,)
    )
    assert len(prepared_rows) == 1
    assert step_records.get_observed(tool_call_id) is None
    assert task_service.get_task(task.task_id).status is TaskStatus.EXECUTING

    # 重启对账：只收敛状态，绝不重放工具
    results = reconciler.sweep_task(
        tenant_id=task.tenant_id, task_id=task.task_id, older_than_s=0
    )
    mine = [item for item in results if item.tool_call_id == tool_call_id]
    assert len(mine) == 1, f"本次调用的核对结论缺失：{results}"
    assert mine[0].conclusion in {
        CONCLUSION_NEEDS_RECONCILIATION,
        CONCLUSION_MANUAL_INTERVENTION,
    }
    assert mine[0].transitioned is True
    assert task_service.get_task(task.task_id).status in {
        TaskStatus.NEEDS_RECONCILIATION,
        TaskStatus.MANUAL_INTERVENTION,
    }

    # 该 step 没有第二次/第三次工具调用记录，也没有任何重试事件
    assert _count_tool_calls(control_client, task.task_id) == 0
    retried, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["TOOL_CALL_RETRIED"],
        limit=10,
    )
    assert retried == []
    recon, _ = audit.query(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        event_types=["RECONCILIATION_REQUIRED"],
        limit=10,
    )
    assert len(recon) == 1
    assert recon[0].payload["replay_attempted"] is False
    assert recon[0].payload["tool_call_id"] == tool_call_id
