"""W07 收口集成测试：以**真实子进程**拉起 `python -m workers`。

三层验证：

1. `--help`：入口与五个 flag 存在（不依赖数据库，任何环境都应通过）；
2. `--self-check`：装配成功且控制库/LAB 可达时退出码为 0；
3. `--once`：真实进程消费一条已批准审批，留下 `step_prepared` / `step_observed` /
   `approval_consumptions` 各 1 行、`tool_calls=1`，且任务被 Worker 自己从
   `WAITING_APPROVAL` 迁到 `EXECUTING`（rev.2：审批通过不改任务状态）。

环境说明：本文件需要**真实 MySQL LAB**（控制库 + 目标库）。本机若只有 TCP 可达
而认证/种子数据缺失，用例会 `skip` 并写明原因——**不伪造通过**。

残留说明（与 `test_w07_repair_e2e.py` 同口径）：控制库里 `task_events` /
`approvals` / `approval_consumptions` / `step_prepared` / `step_observed` 是
**只追加表**，`dg_ctl` 无权 DELETE（W06 设计如此）。因此清理需要可用的
`DG_ADMIN_DSN`；该账号不可用时本文件只恢复 LAB 目标行，并在测试输出里发 warning
列出未清理的表与 task_id，**绝不静默吞掉**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from adapters.lab_assembly import build_lab_stack
from adapters.mysql.lab_repair import TOOL_NAME
from core.approval.models import ApprovalStatus
from core.errors import DBGuardianError
from core.security.models import Principal
from core.security.roles import Role
from core.tasks.models import TaskStatus, TaskStep
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TENANT = "tenant_local"
ORDER_ID = 2001
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"

#: 清理顺序：先子表后父表（外键约束）。
_CLEANUP_TABLES = (
    "approval_consumptions",
    "step_observed",
    "step_prepared",
    "tool_calls",
    "execution_envelopes",
    "approvals",
    "task_events",
    "task_steps",
    "worker_leases",
    "checkpoints",
    "evidence",
    "tasks",
)


def _run_worker(*args: str, timeout_s: float = 180.0) -> subprocess.CompletedProcess[str]:
    """用**当前解释器**（venv 内 Python 3.14）拉起真实子进程。"""
    return subprocess.run(
        [sys.executable, "-m", "workers", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=dict(os.environ),
    )


def _json_line(stdout: str) -> dict[str, Any]:
    """取最后一行 JSON（日志可能写在同一 stdout）。"""
    for line in reversed([item for item in stdout.splitlines() if item.strip()]):
        text = line.strip()
        if text.startswith("{"):
            return dict(json.loads(text))
    raise AssertionError(f"未找到 JSON 输出：{stdout[-2000:]}")


def _count(client: Any, table: str, task_id: str) -> int:
    rows = client.fetch_all(
        f"SELECT COUNT(*) AS total FROM dg_system.{table} WHERE task_id = %s",
        (task_id,),
    )
    return int(rows[0]["total"]) if rows else 0


def _current_note(client: Any) -> str | None:
    rows = client.fetch_all(NOTE_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 scripts/bootstrap_mysql.py")
    return rows[0].get("note")


@pytest.fixture(scope="module")
def lab_reachable(settings, control_client, ro_client, rw_client) -> None:
    """真实 LAB 可达性探测：不可达就 skip（理由明确写"需真实 MySQL LAB"）。"""
    for name, client in (
        ("控制库", control_client),
        ("LAB 只读", ro_client),
        ("LAB 可写", rw_client),
    ):
        try:
            client.ping()
        except DBGuardianError as exc:
            pytest.skip(f"需真实 MySQL LAB：{name}不可用（{exc.code.value}）")
    # `_current_note` 在种子行缺失时自行 skip；note 为 NULL 是合法基线，不跳过。
    _current_note(rw_client)


@pytest.fixture
def approved_repair(
    settings, control_client, ro_client, rw_client, admin_client, lab_reachable
) -> Any:
    """准备一条"已批准 + 步骤参数可还原"的候选，并保证用例后彻底清理。"""
    from core.tasks.repository import TaskRepository

    control_repository = TaskRepository(control_client)
    environment_row = control_repository.get_environment(
        settings.default_environment_id, tenant_id=TENANT
    )
    if environment_row is None:
        pytest.skip("需真实 MySQL LAB：控制库缺少默认环境事实（请先 bootstrap_mysql.py）")

    stack = build_lab_stack(
        settings=settings,
        control_client=control_client,
        environment_row=environment_row,
        ro_client=ro_client,
        rw_client=rw_client,
    )
    registry: ToolRegistry = stack["registry"]
    service = TaskService(stack["repository"], registry)
    before = _current_note(rw_client)
    target = "w07-worker-carrier"
    params = {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": ORDER_ID,
        "keys": f"order_id={ORDER_ID}",
        "note": target,
        "expected_note": before,
    }

    task = service.create_task("W07 收口：独立 Worker 载体执行闭环")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    # rev.2：审批通过**不会**改任务状态，生产形态就是 WAITING_APPROVAL；
    # `WAITING_APPROVAL → EXECUTING` 由 Worker 自己在建信封之前完成。
    task = service.transition(task.task_id, TaskStatus.WAITING_APPROVAL)
    waiting_state_version = task.state_version
    step = TaskStep(
        step_id=f"step_{task.task_id[-12:]}",
        task_id=task.task_id,
        run_id=task.run_id,
        step_index=0,
        title="单行 note 修复",
        tool_name=TOOL_NAME,
        params=params,
        status="PENDING",
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    stack["repository"].insert_step(step)

    operator = Principal(
        user_id="user_w07_worker_operator",
        tenant_id=TENANT,
        roles=(Role.OPERATOR,),
        session_id="sess_w07_worker_operator",
    )
    approver = Principal(
        user_id="user_w07_worker_approver",
        tenant_id=TENANT,
        roles=(Role.APPROVER,),
        session_id="sess_w07_worker_approver",
    )
    created = stack["approvals"].create(
        principal=operator,
        task_id=task.task_id,
        step_id=step.step_id,
        tool_name=TOOL_NAME,
        params=params,
        target_objects=[],
    )
    stack["approvals"].approve(
        created.approval_id, principal=approver, reason="W07 收口验收"
    )

    payload = {
        "task_id": task.task_id,
        "step_id": step.step_id,
        "approval_id": created.approval_id,
        "params": params,
        "target": target,
        "before": before,
        "stack": stack,
        "service": service,
        "waiting_state_version": waiting_state_version,
    }
    try:
        yield payload
    finally:
        # 先撤掉仍未消费的审批：否则该候选会残留到同批次后续用例的 `--once` 里，
        # 造成"另一个测试替我们执行"的假绿/假红（跨用例耦合）。
        try:
            current = stack["approvals"].get(created.approval_id, tenant_id=TENANT)
            if current.status is ApprovalStatus.APPROVED:
                stack["approvals"].revoke(
                    created.approval_id,
                    principal=approver,
                    reason="W07 收口测试清理：避免残留候选被后续 --once 执行",
                )
        except DBGuardianError:
            pass
        try:
            rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
        except DBGuardianError:
            pass
        dirty: list[str] = []
        for table in _CLEANUP_TABLES:
            try:
                admin_client.execute(
                    f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task.task_id,)
                )
            except DBGuardianError:
                dirty.append(table)
        if dirty:
            warnings.warn(
                "控制库清理不完整（需可用的 DG_ADMIN_DSN）："
                f"task_id={task.task_id} 未清理表={','.join(dirty)}",
                stacklevel=1,
            )


def test_help_lists_worker_flags() -> None:
    """`--help` 不依赖数据库：任何环境都必须能看到五个 flag。"""
    result = _run_worker("--help", timeout_s=60)

    assert result.returncode == 0, result.stderr
    for flag in ("--once", "--self-check", "--dry-run", "--worker-id", "--tenant"):
        assert flag in result.stdout, f"--help 缺少 {flag}"


def test_self_check_returns_zero_when_stack_is_available(lab_reachable) -> None:
    result = _run_worker("--self-check", timeout_s=120)

    payload = _json_line(result.stdout)
    assert payload["event"] == "worker_self_check"
    assert payload["checks"]["control"]["ok"] is True
    assert payload["checks"]["discovery"]["ok"] is True
    assert result.returncode == 0, result.stderr


def test_dry_run_lists_candidate_without_executing(approved_repair, control_client) -> None:
    """`--dry-run --once` 只列候选：不得产生 tool_call / 租约 / 审批消费。"""
    result = _run_worker("--dry-run", "--once", timeout_s=120)

    payload = _json_line(result.stdout)
    assert payload["event"] == "worker_dry_run"
    ids = [item["task_id"] for item in payload["candidates"]]
    assert approved_repair["task_id"] in ids
    candidate = next(
        item for item in payload["candidates"] if item["task_id"] == approved_repair["task_id"]
    )
    assert candidate["approval_id"] == approved_repair["approval_id"]
    assert candidate["tool_name"] == TOOL_NAME
    assert candidate["step_id"] == approved_repair["step_id"]
    # 只暴露键名，不暴露参数值。
    assert "note" in candidate["param_keys"]
    assert approved_repair["target"] not in json.dumps(payload, ensure_ascii=False)

    assert _count(control_client, "tool_calls", approved_repair["task_id"]) == 0
    assert _count(control_client, "approval_consumptions", approved_repair["task_id"]) == 0
    assert _count(control_client, "worker_leases", approved_repair["task_id"]) == 0


def test_worker_process_executes_approved_repair_once(
    approved_repair, control_client, rw_client
) -> None:
    """真实进程执行：三表各 1 行 + tool_calls=1 + 目标行真的被改 + 状态迁移到位。"""
    result = _run_worker("--once", timeout_s=180)

    payload = _json_line(result.stdout)
    assert payload["event"] == "worker_report"
    # 允许环境里存在其它候选导致退出码 1，但本进程必须真的执行过至少一条。
    assert payload["executed"] >= 1, result.stdout[-2000:]

    task_id = approved_repair["task_id"]
    assert _count(control_client, "step_prepared", task_id) == 1
    assert _count(control_client, "step_observed", task_id) == 1
    assert _count(control_client, "approval_consumptions", task_id) == 1
    assert _count(control_client, "tool_calls", task_id) == 1

    # 不是"被拒绝也算 1 行"：本任务必须真的成功执行。
    observed = control_client.fetch_all(
        "SELECT outcome FROM dg_system.step_observed WHERE task_id = %s LIMIT 1",
        (task_id,),
    )
    assert observed and str(observed[0]["outcome"]) == "success"
    call_rows = control_client.fetch_all(
        "SELECT status FROM dg_system.tool_calls WHERE task_id = %s LIMIT 1",
        (task_id,),
    )
    assert call_rows and str(call_rows[0]["status"]) == "success"

    assert _current_note(rw_client) == approved_repair["target"], "目标库必须真的发生单行更新"

    rows = control_client.fetch_all(
        "SELECT status FROM dg_system.approvals WHERE approval_id = %s LIMIT 1",
        (approved_repair["approval_id"],),
    )
    assert rows and str(rows[0]["status"]) == "CONSUMED"

    # rev.2：Worker 必须自己把任务从 WAITING_APPROVAL 迁到 EXECUTING（且只涨一次版本），
    # 否则"结果未知"分支会因状态机无此边而静默不落账。
    task_rows = control_client.fetch_all(
        "SELECT status, state_version FROM dg_system.tasks WHERE task_id = %s LIMIT 1",
        (task_id,),
    )
    assert task_rows
    assert str(task_rows[0]["status"]) == "EXECUTING"
    assert int(task_rows[0]["state_version"]) == approved_repair["waiting_state_version"] + 1
