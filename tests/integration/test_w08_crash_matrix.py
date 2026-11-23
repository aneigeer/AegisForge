"""W08 T06 / AC-18、AC-19：崩溃矩阵（7 类注入，**每例独立可定位**）。

七类注入（与任务书 D5 一致）：

| # | 注入 | 断言要点 |
|---|---|---|
| 1 | Worker 在 `StepPrepared` 后崩溃 | 收敛 `NEEDS_RECONCILIATION`、`tool_calls` 增量 0、note 不变 |
| 2 | 数据库已执行但响应丢失 | 收敛 `NEEDS_RECONCILIATION`、note 只变 1 次、`tool_calls` 增量 0 |
| 3 | 连接中断（核对期） | `run_once()` 不抛错、候选照常执行、`RECONCILIATION_FAILED` 落库 |
| 4 | 租约过期 | 旧 Worker 被拒 `FENCING_TOKEN_STALE`、无副作用 |
| 5 | 双 Worker 并发 | 第二个 Worker `WORKER_CONFLICT`、`tool_calls` 增量 ≤1 |
| 6 | 重复消息 | 第二轮不重复执行、`tool_calls` 与 note 都不再变化 |
| 7 | PAUSE / CANCEL 与在途 SQL 竞态 | PAUSE 不伪造"已停止"、CANCEL 不产生执行 |

每例都跑同一组硬断言（`_assert_no_duplicate_effect`）：
`tool_calls` 增量 ≤1、目标行 `note` 只可能从基线变到目标值一次、
`task_events.seq` 连续、**无 `TOOL_CALL_RETRIED`**。

环境说明：需要真实 MySQL（控制库 + LAB 目标库）。不可达时**skip 并写明原因**，
绝不伪造通过。控制库 `task_events` 等为只追加表，清理只做两件可控的事：
撤销未消费审批（防止残留候选被其它用例执行）与恢复 LAB 目标行。
"""

from __future__ import annotations

import warnings
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from adapters.mysql.lab_repair import TOOL_NAME
from core.approval.models import ApprovalStatus
from core.errors import (
    DBGuardianError,
    DatabaseQueryError,
    FencingTokenStaleError,
    WorkerConflictError,
    WorkerLeaseInvalidError,
)
from core.execution.step_records import StepPrepared
from core.security.models import LeaseStatus, Principal, WorkerLease
from core.security.roles import Role
from core.tasks.models import TaskStatus, TaskStep, utc_now
from core.tools.contracts import ToolStatus
from core.worker.worker import Worker
from workers.discovery import discover_candidates
from workers.runner import SCHEDULER_TASK_ID, WorkerRunner
from workers.stack import WorkerStack, build_worker_stack

pytestmark = pytest.mark.integration

TENANT = "tenant_local"
ORDER_ID = 2001
NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"
RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"
PREPARED_AGE_S = 300.0


# ---------------------------------------------------------------------------
# 装配与基础工具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def worker_stack(settings) -> WorkerStack:
    """生产装配（`build_worker_stack`），含 W08 的 verifier/readback/reconciler。"""
    stack = build_worker_stack(
        settings, worker_id=f"worker_w08_crash_{uuid4().hex[:8]}"
    )
    for name, client in (
        ("控制库", stack.control_client),
        ("LAB 只读", stack.ro_client),
        ("LAB 可写", stack.rw_client),
    ):
        try:
            client.ping()
        except DBGuardianError as exc:
            pytest.skip(f"需真实 MySQL LAB：{name}不可用（{exc.code.value}）")
    if not stack.lab_write_enabled:
        pytest.skip("环境事实不是 LAB+mysql，写工具未注册，崩溃矩阵无法执行真实副作用")
    assert stack.reconciler is not None, "生产装配必须注入核对端口（AC-17）"
    return stack


def _note(client: Any) -> str | None:
    rows = client.fetch_all(NOTE_SQL, (ORDER_ID,))
    if not rows:
        pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 scripts/bootstrap_mysql.py")
    return rows[0].get("note")


def _count(client: Any, table: str, task_id: str) -> int:
    rows = client.fetch_all(
        f"SELECT COUNT(*) AS total FROM dg_system.{table} WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["total"]) if rows else 0


def _event_types(client: Any, task_id: str) -> list[str]:
    rows = client.fetch_all(
        "SELECT event_type FROM dg_system.task_events WHERE task_id = %s ORDER BY seq",
        (task_id,),
    )
    return [str(row["event_type"]) for row in rows]


def _seq(client: Any, task_id: str) -> list[int]:
    rows = client.fetch_all(
        "SELECT seq FROM dg_system.task_events WHERE task_id = %s ORDER BY seq", (task_id,)
    )
    return [int(row["seq"]) for row in rows]


def _task_status(stack: WorkerStack, task_id: str) -> TaskStatus:
    return stack.repository.get_task(task_id, tenant_id=TENANT).status


def _runner(stack: WorkerStack, task_id: str) -> WorkerRunner:
    """只处理本用例任务，避免与库里其它残留候选互相干扰。"""

    def discover(collected: Any, *, limit: int = 10) -> list[Any]:
        return [
            item
            for item in discover_candidates(collected, limit=limit)
            if item.task_id == task_id
        ]

    return WorkerRunner(stack, discover=discover)


def _assert_no_duplicate_effect(
    stack: WorkerStack,
    case: dict[str, Any],
    *,
    max_tool_calls: int = 1,
) -> None:
    """AC-18 的统一硬断言：增量 ≤1、note 只变一次、seq 连续、无重试事件。"""
    client = stack.control_client
    task_id = case["task_id"]

    tool_calls = _count(client, "tool_calls", task_id)
    assert tool_calls <= max_tool_calls, f"tool_calls 增量超过 1：{tool_calls}"

    seq = _seq(client, task_id)
    assert seq == list(range(len(seq))), f"task_events.seq 不连续：{seq}"

    types = _event_types(client, task_id)
    assert "TOOL_CALL_RETRIED" not in types, "崩溃路径不得出现重试事件"

    # 目标行 note 只可能停在基线或目标值（=最多变一次）。
    assert _note(stack.rw_client) in {case["before"], case["target"]}
    # 同一 tool_call 不可能产生第二条执行后证据。
    assert _count(client, "step_observed", task_id) <= 1


# ---------------------------------------------------------------------------
# 用例工厂：任务 + 步骤 + 已批准审批（每例独立 task_id）
# ---------------------------------------------------------------------------


@pytest.fixture
def make_case(worker_stack: WorkerStack):
    created: list[dict[str, Any]] = []

    def _make(*, status: TaskStatus = TaskStatus.EXECUTING) -> dict[str, Any]:
        service = worker_stack.task_service
        before = _note(worker_stack.rw_client)
        target = f"w08-t06-{uuid4().hex[:10]}"
        params = {
            "database": "dg_lab",
            "table": "seed_orders",
            "order_id": ORDER_ID,
            "keys": f"order_id={ORDER_ID}",
            "note": target,
            "expected_note": before,
        }
        task = service.create_task(f"W08 T06 崩溃矩阵 {status.value}")
        task = service.transition(task.task_id, TaskStatus.PLANNING)
        task = service.transition(task.task_id, status)

        step = TaskStep(
            step_id=f"step_{uuid4().hex[:12]}",
            task_id=task.task_id,
            run_id=task.run_id,
            step_index=0,
            title="单行 note 更新（崩溃矩阵载体）",
            tool_name=TOOL_NAME,
            params=params,
            status="PENDING",
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        worker_stack.repository.insert_step(step)

        operator = Principal(
            user_id="user_w08_crash_operator",
            tenant_id=TENANT,
            roles=(Role.OPERATOR,),
            session_id="sess_w08_crash_operator",
        )
        approver = Principal(
            user_id="user_w08_crash_approver",
            tenant_id=TENANT,
            roles=(Role.APPROVER,),
            session_id="sess_w08_crash_approver",
        )
        approval = worker_stack.approvals.create(
            principal=operator,
            task_id=task.task_id,
            step_id=step.step_id,
            tool_name=TOOL_NAME,
            params=params,
            target_objects=[],
        )
        worker_stack.approvals.approve(
            approval.approval_id, principal=approver, reason="W08 T06 崩溃矩阵"
        )

        case = {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "step_id": step.step_id,
            "approval_id": approval.approval_id,
            "params": params,
            "before": before,
            "target": target,
        }
        created.append(case)
        return case

    try:
        yield _make
    finally:
        for case in created:
            _teardown(worker_stack, case)


def _teardown(stack: WorkerStack, case: dict[str, Any]) -> None:
    """撤销未消费审批（防止残留候选）+ 恢复 LAB 目标行。"""
    try:
        current = stack.approvals.get(case["approval_id"], tenant_id=TENANT)
        if current.status is ApprovalStatus.APPROVED:
            stack.approvals.revoke(
                case["approval_id"],
                principal=Principal(
                    user_id="user_w08_crash_approver",
                    tenant_id=TENANT,
                    roles=(Role.APPROVER,),
                    session_id="sess_w08_crash_cleanup",
                ),
                reason="W08 T06 清理：避免残留候选被其它用例执行",
            )
    except DBGuardianError as exc:  # 已消费/已撤销都正常
        warnings.warn(f"审批清理跳过：{exc.code.value}", stacklevel=1)
    try:
        stack.rw_client.execute(RESTORE_SQL, (case["before"], ORDER_ID))
    except DBGuardianError as exc:
        warnings.warn(f"LAB 目标行恢复失败：{exc.code.value}", stacklevel=1)


def _insert_prepared(
    stack: WorkerStack,
    case: dict[str, Any],
    *,
    tool_call_id: str | None = None,
) -> str:
    """注入"已 Prepared、无 Observed"的证据行（等价于执行前崩溃）。"""
    identity = tool_call_id or f"tc_w08_crash_{uuid4().hex[:12]}"
    stack.step_records.insert_prepared(
        StepPrepared(
            prepared_id=f"prep_{uuid4().hex}",
            tenant_id=TENANT,
            task_id=case["task_id"],
            run_id=case["run_id"],
            tool_call_id=identity,
            tool_name=TOOL_NAME,
            tool_version="1.0",
            policy_version=str(stack.settings.policy_version),
            environment_id=stack.environment_id,
            step_id=case["step_id"],
            approval_id=case["approval_id"],
            action_digest="sha256:" + uuid4().hex,
            idempotency_key=f"idem_{uuid4().hex}",
            request={"param_keys": sorted(case["params"])},
            prepared_at=utc_now() - timedelta(seconds=PREPARED_AGE_S),
        )
    )
    return identity


def _update_note(client: Any, note: str) -> None:
    client.execute(RESTORE_SQL, (note, ORDER_ID))


def _tool_calls_total(client: Any) -> int:
    rows = client.fetch_all("SELECT COUNT(*) AS total FROM dg_system.tool_calls", ())
    return int(rows[0]["total"]) if rows else 0


# ---------------------------------------------------------------------------
# 1) Worker 在 StepPrepared 后崩溃
# ---------------------------------------------------------------------------


def test_01_worker_crash_after_step_prepared(worker_stack: WorkerStack, make_case) -> None:
    """执行前崩溃：只有 Prepared 证据，核对只能收敛为 NEEDS_RECONCILIATION。"""
    case = make_case(status=TaskStatus.EXECUTING)
    _insert_prepared(worker_stack, case)
    before_calls = _tool_calls_total(worker_stack.control_client)

    report = _runner(worker_stack, case["task_id"]).run_once()

    assert _task_status(worker_stack, case["task_id"]) is TaskStatus.NEEDS_RECONCILIATION
    assert _tool_calls_total(worker_stack.control_client) == before_calls, "核对不得产生工具调用"
    assert report.reconciled >= 1
    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=0)
    assert _note(worker_stack.rw_client) == case["before"], "执行前崩溃不得改动目标行"


# ---------------------------------------------------------------------------
# 2) 数据库已执行但响应丢失
# ---------------------------------------------------------------------------


def test_02_database_committed_but_response_lost(
    worker_stack: WorkerStack, make_case
) -> None:
    """写已提交、证据链停在 Prepared：不得重放，只能标"需核对"。"""
    case = make_case(status=TaskStatus.EXECUTING)
    _update_note(worker_stack.rw_client, case["target"])  # 模拟已提交的 UPDATE
    _insert_prepared(worker_stack, case)
    before_calls = _tool_calls_total(worker_stack.control_client)

    report = _runner(worker_stack, case["task_id"]).run_once()

    assert _task_status(worker_stack, case["task_id"]) is TaskStatus.NEEDS_RECONCILIATION
    assert _tool_calls_total(worker_stack.control_client) == before_calls, "禁止二次写入"
    assert report.reconciled >= 1
    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=0)
    assert _note(worker_stack.rw_client) == case["target"], "已提交的写必须保留原样"


# ---------------------------------------------------------------------------
# 3) 连接中断（核对期数据库不可用）
# ---------------------------------------------------------------------------


def test_03_connection_interrupted_during_reconciliation(
    worker_stack: WorkerStack, make_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """核对端口故障：`run_once()` 记录失败但**照常执行**本轮候选。"""
    case = make_case(status=TaskStatus.WAITING_APPROVAL)
    failure = DatabaseQueryError("控制库连接中断", details={"reason": "TEST_FAULT"})

    def broken_sweep(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    # W11-CORR-01（主理人授权，`docs/w10-w12-lead-rulings.md` §12.2）：W09 T06 KD-2 之后
    # `workers/runner.py::_invoke_sweep()` **优先调用 `sweep_detailed()`**，只打桩
    # `sweep` 已不再生效（打桩目标过期），故此处改为打桩实际被调用的入口。
    monkeypatch.setattr(worker_stack.reconciler, "sweep_detailed", broken_sweep)

    report = _runner(worker_stack, case["task_id"]).run_once()

    assert report.executed == 1, "核对失败不得让本轮候选停摆"
    assert report.clean is False
    assert report.failed == 1

    rows = worker_stack.control_client.fetch_all(
        "SELECT payload_json FROM dg_system.task_events "
        "WHERE task_id = %s AND event_type = 'RECONCILIATION_FAILED' ORDER BY seq DESC LIMIT 1",
        (SCHEDULER_TASK_ID,),
    )
    assert rows, "核对失败必须留下 RECONCILIATION_FAILED 事件"
    payload = rows[0]["payload_json"]
    text = payload if isinstance(payload, str) else str(payload)
    assert "RECONCILIATION_SWEEP_FAILED" in text
    assert "message_digest" in text

    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=1)
    assert _note(worker_stack.rw_client) == case["target"]


# ---------------------------------------------------------------------------
# 4) 租约过期：旧 Worker 被 fencing 拒绝
# ---------------------------------------------------------------------------


class _FrozenLeasePort:
    """把一份**已过期但仍在旧进程内存里**的租约原样交回（崩溃 Worker 的视角）。"""

    def __init__(self, lease: WorkerLease) -> None:
        self._lease = lease

    def acquire(self, **_kwargs: Any) -> WorkerLease:
        return self._lease


def test_04_expired_lease_old_worker_is_fenced(
    worker_stack: WorkerStack, make_case
) -> None:
    """旧 Worker 的租约过期 + 新 Worker 已拿更高 fencing token：旧的一定被拒。"""
    case = make_case(status=TaskStatus.EXECUTING)
    control = worker_stack.control_client
    past = utc_now() - timedelta(seconds=30)
    future = utc_now() + timedelta(seconds=60)

    worker_old = worker_stack.worker_for("user_operator")
    lease_old = worker_old.acquire_lease(
        case["task_id"], worker_stack.environment_id, ttl_s=30
    )
    control.execute(
        "UPDATE dg_system.worker_leases SET expires_at = %s WHERE lease_id = %s",
        (past.replace(tzinfo=None), lease_old.lease_id),
    )

    worker_new = worker_stack.worker_for("user_operator")
    lease_new = worker_new.acquire_lease(
        case["task_id"], worker_stack.environment_id, ttl_s=30
    )
    assert lease_new.fencing_token > lease_old.fencing_token
    worker_new.release()

    # 注入：旧租约记录仍显示 ACTIVE（崩溃进程未清理），而任务最大 token 已是 2。
    control.execute(
        "UPDATE dg_system.worker_leases SET status = 'ACTIVE', released_at = NULL, "
        "expires_at = %s WHERE lease_id = %s",
        (future.replace(tzinfo=None), lease_old.lease_id),
    )

    stale = lease_old.model_copy(
        update={"status": LeaseStatus.ACTIVE, "expires_at": future}
    )
    stale_worker = Worker(
        worker_stack.worker_id,
        TENANT,
        "user_operator",
        _FrozenLeasePort(stale),
        worker_stack.gateway,
        signing_key=worker_stack.settings.gateway_key(),
    )
    stale_worker.acquire_lease(case["task_id"], worker_stack.environment_id, ttl_s=30)

    state_version = worker_stack.repository.get_task(
        case["task_id"], tenant_id=TENANT
    ).state_version
    with pytest.raises(FencingTokenStaleError) as excinfo:
        stale_worker.submit(
            TOOL_NAME,
            case["params"],
            step_id=case["step_id"],
            state_version=state_version,
            approval_id=case["approval_id"],
        )
    assert excinfo.value.code.value == "FENCING_TOKEN_STALE"

    # 自然路径（租约在旧进程看来也已过期）：本地即拒，连信封都不会构造。
    expired_view = lease_old.model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    expired_worker = Worker(
        worker_stack.worker_id,
        TENANT,
        "user_operator",
        _FrozenLeasePort(expired_view),
        worker_stack.gateway,
        signing_key=worker_stack.settings.gateway_key(),
    )
    expired_worker.acquire_lease(
        case["task_id"], worker_stack.environment_id, ttl_s=30
    )
    with pytest.raises(WorkerLeaseInvalidError) as expired_exc:
        expired_worker.submit(
            TOOL_NAME,
            case["params"],
            step_id=case["step_id"],
            state_version=state_version,
            approval_id=case["approval_id"],
        )
    assert expired_exc.value.code.value == "WORKER_LEASE_INVALID"

    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=0)
    assert _note(worker_stack.rw_client) == case["before"], "被 fence 的提交不得产生副作用"


# ---------------------------------------------------------------------------
# 5) 双 Worker 并发
# ---------------------------------------------------------------------------


def test_05_two_workers_compete_for_the_same_task(
    worker_stack: WorkerStack, make_case
) -> None:
    """两个 Worker 争同一任务：只有一个能执行，`tool_calls` 增量不超过 1。"""
    case = make_case(status=TaskStatus.EXECUTING)
    worker_a = worker_stack.worker_for("user_operator")
    worker_b = worker_stack.worker_for("user_operator")

    lease_a = worker_a.acquire_lease(
        case["task_id"], worker_stack.environment_id, ttl_s=60
    )
    with pytest.raises(WorkerConflictError) as excinfo:
        worker_b.acquire_lease(case["task_id"], worker_stack.environment_id, ttl_s=60)
    assert excinfo.value.code.value == "WORKER_CONFLICT"

    state_version = worker_stack.repository.get_task(
        case["task_id"], tenant_id=TENANT
    ).state_version
    result = worker_a.submit(
        TOOL_NAME,
        case["params"],
        step_id=case["step_id"],
        state_version=state_version,
        approval_id=case["approval_id"],
    )
    assert result.status is ToolStatus.SUCCESS
    worker_a.release()

    assert _count(worker_stack.control_client, "tool_calls", case["task_id"]) == 1
    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=1)
    assert _note(worker_stack.rw_client) == case["target"]
    assert lease_a.fencing_token >= 1


# ---------------------------------------------------------------------------
# 6) 重复消息
# ---------------------------------------------------------------------------


def test_06_duplicate_message_does_not_replay(
    worker_stack: WorkerStack, make_case
) -> None:
    """同一条候选消息重复投递：第二轮不得再执行、不得再改目标行。"""
    case = make_case(status=TaskStatus.WAITING_APPROVAL)
    runner = _runner(worker_stack, case["task_id"])

    first = runner.run_once()
    assert first.executed == 1
    calls_after_first = _count(worker_stack.control_client, "tool_calls", case["task_id"])
    assert calls_after_first == 1
    assert _note(worker_stack.rw_client) == case["target"]

    second = runner.run_once()
    assert second.executed == 0, "重复消息不得二次执行"
    assert _count(worker_stack.control_client, "tool_calls", case["task_id"]) == 1
    assert _count(worker_stack.control_client, "step_observed", case["task_id"]) == 1

    _assert_no_duplicate_effect(worker_stack, case, max_tool_calls=1)
    assert _note(worker_stack.rw_client) == case["target"]


# ---------------------------------------------------------------------------
# 7) PAUSE / CANCEL 与正在执行 SQL 的竞态
# ---------------------------------------------------------------------------


def test_07_pause_and_cancel_race_with_in_flight_sql(
    worker_stack: WorkerStack, make_case
) -> None:
    """PAUSE 不伪造「已停止」；CANCEL 走 CANCEL_REQUESTED，全程不产生执行。"""
    service = worker_stack.task_service

    # --- PAUSE：在途（EXECUTING）任务只置标记，状态不得伪造成 PAUSED。
    paused = make_case(status=TaskStatus.EXECUTING)
    _insert_prepared(worker_stack, paused)
    service.request_pause(paused["task_id"])
    after_pause = _task_status(worker_stack, paused["task_id"])
    assert after_pause is TaskStatus.EXECUTING, "在途 SQL 不得被伪造成已暂停"

    pause_report = _runner(worker_stack, paused["task_id"]).run_once()
    assert pause_report.executed == 0
    assert pause_report.reconciled >= 1
    assert _task_status(worker_stack, paused["task_id"]) is TaskStatus.NEEDS_RECONCILIATION
    pause_events = _event_types(worker_stack.control_client, paused["task_id"])
    assert "TASK_PAUSE_REQUESTED" in pause_events
    _assert_no_duplicate_effect(worker_stack, paused, max_tool_calls=0)
    assert _note(worker_stack.rw_client) == paused["before"]

    # --- CANCEL：未在途任务经 CANCEL_REQUESTED 落 CANCELLED，且绝不执行。
    cancelled = make_case(status=TaskStatus.WAITING_APPROVAL)
    service.cancel(cancelled["task_id"])
    assert _task_status(worker_stack, cancelled["task_id"]) in {
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLED,
    }

    cancel_report = _runner(worker_stack, cancelled["task_id"]).run_once()
    assert cancel_report.executed == 0
    cancel_events = _event_types(worker_stack.control_client, cancelled["task_id"])
    assert "TASK_CANCEL_REQUESTED" in cancel_events
    final = _task_status(worker_stack, cancelled["task_id"])
    assert final in {TaskStatus.CANCELLED, TaskStatus.NEEDS_RECONCILIATION}, final
    _assert_no_duplicate_effect(worker_stack, cancelled, max_tool_calls=0)
    assert _note(worker_stack.rw_client) == cancelled["before"]
