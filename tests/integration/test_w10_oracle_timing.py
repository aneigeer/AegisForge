"""W10 T01｜AC-19 Oracle 时序闸门（三态 + 真实 ``task_events`` 审计行）。

任务卡 S10 / 设计 ``02-design.md`` §9.2：

    建真实任务 → ``EXECUTING`` 时 ``read()`` 被拒（``NOT_TERMINAL``）
    → 迁到 ``COMPLETED`` 后首次成功（``TERMINAL_OK``）
    → 第二次被拒（``ALREADY_READ``）；
    断言 ``task_events`` 有对应审计行，且断言文本**不含**
    ``expected_root_cause`` 的值。

纪律（任务卡全局纪律 4、主理人裁定 C-2 / W10-Q2、设计 R-09）：

- 只依赖 ``control_client``（dg_ctl）；**不依赖** ``DG_ADMIN_DSN``、
  **不依赖** PG 5432；控制库不可用时 ``skip`` 并写明原因，不判 FAIL、
  不冒充通过、不放宽断言；
- 只 INSERT（tasks / task_events / checkpoints），不做清理、不 DELETE、
  不 TRUNCATE；状态迁移沿用既有 ``TaskService.transition`` 惯例；
- 只按**本任务 id** 断言事件，不统计全表（``ENV-RESIDUE`` 140 行不重测）；
- 真值只在测试局部变量内存活，不写日志、不写断言文本、不写产物。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.audit.service import AuditService
from core.benchmark.oracle import (
    REASON_ALREADY_READ,
    REASON_NOT_TERMINAL,
    REASON_TERMINAL_OK,
    OracleAccessDenied,
    OracleGate,
)
from core.errors import DBGuardianError
from core.tasks.models import Task, TaskStatus, new_id, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tasks.state_machine import ensure_transition, is_terminal
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

TENANT = "tenant_local"
SCENARIO_ID = "mysql_slow_query"
FORBIDDEN_TRUTH_LITERALS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
)


class _TenantBoundAudit:
    """组合根装配的审计端口：把服务端 ``tenant_id`` 绑给 best-effort 写入。

    ``OracleGate`` 的关键签名不带 ``tenant_id``（任务卡 :75-77 照抄），因此
    租户过滤列由组合根在装配审计端口时显式携带（W06 冻结口径：不信任 payload）。
    """

    def __init__(self, service: AuditService, tenant_id: str) -> None:
        self._service = service
        self.tenant_id = tenant_id

    def try_emit(
        self,
        event: Any,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> bool:
        return self._service.try_emit(
            event,
            tenant_id=tenant_id or self.tenant_id,
            actor_user_id=actor_user_id,
        )


class _RecordingAudit:
    """零 DB 用例用的记录型审计端口（不落库）。"""

    def __init__(self, tenant_id: str = TENANT) -> None:
        self.tenant_id = tenant_id
        self.events: list[Any] = []

    def try_emit(
        self,
        event: Any,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> bool:
        self.events.append(event)
        return True


@pytest.fixture()
def control_db(control_client: Any) -> Any:
    """控制库可用性探针：不可达一律 skip（带原因），绝不判 FAIL。"""

    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}；不冒充通过")
    return control_client


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row["payload_json"]
    return json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)


def test_oracle_timing_three_states_and_task_events_rows(control_db: Any) -> None:
    """真实控制库：三态 + 三条审计行 + 断言文本不含真值。"""

    repository = TaskRepository(control_db)
    service = TaskService(repository, ToolRegistry())
    audit = _TenantBoundAudit(AuditService(repository), tenant_id=TENANT)

    task = service.create_task("W10 T01 AC-19 Oracle 时序闸门", tenant_id=TENANT)
    service.transition(task.task_id, TaskStatus.PLANNING)
    service.transition(task.task_id, TaskStatus.EXECUTING)
    assert service.get_task(task.task_id).status is TaskStatus.EXECUTING

    gate = OracleGate(
        status_reader=lambda task_id: service.get_task(task_id).status,
        audit=audit,
    )

    # ① NOT_TERMINAL：状态由闸门自己读，调用方无法伪造成终态。
    with pytest.raises(OracleAccessDenied) as first:
        gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert first.value.details["reason"] == REASON_NOT_TERMINAL
    assert first.value.details["task_status"] == "EXECUTING"

    # 迁到 COMPLETED（真实状态机合法路径：EXECUTING → VERIFYING → COMPLETED）。
    service.transition(task.task_id, TaskStatus.VERIFYING)
    service.transition(task.task_id, TaskStatus.COMPLETED)
    assert is_terminal(service.get_task(task.task_id).status) is True

    # ② TERMINAL_OK：终态后首次读取成功；真值只活在局部变量。
    truth = gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert truth.scenario_id == SCENARIO_ID
    assert truth.expected_root_cause

    # ③ ALREADY_READ：同一 (run_id, scenario_id) 第二次读取被拒。
    with pytest.raises(OracleAccessDenied) as third:
        gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert third.value.details["reason"] == REASON_ALREADY_READ

    records = gate.records
    assert [item.reason for item in records] == [
        REASON_NOT_TERMINAL,
        REASON_TERMINAL_OK,
        REASON_ALREADY_READ,
    ]
    assert [item.allowed for item in records] == [False, True, False]

    # 审计行：只按本任务 id 断言，不统计全表（R-09）。
    rows = control_db.fetch_all(
        "SELECT event_type, payload_json, seq, tenant_id "
        "FROM dg_system.task_events WHERE task_id = %s "
        "AND event_type IN ('ORACLE_READ', 'ORACLE_ACCESS_DENIED') "
        "ORDER BY seq ASC",
        (task.task_id,),
    )
    assert [row["event_type"] for row in rows] == [
        "ORACLE_ACCESS_DENIED",
        "ORACLE_READ",
        "ORACLE_ACCESS_DENIED",
    ]
    assert [_payload(row)["reason"] for row in rows] == [
        REASON_NOT_TERMINAL,
        REASON_TERMINAL_OK,
        REASON_ALREADY_READ,
    ]
    assert all(row["tenant_id"] == TENANT for row in rows)

    # AC-19 / 禁止项 5：断言文本不含 expected_root_cause 的值，
    # 也不含任何真值字段名；审计 payload 里只有判定事实。
    asserted_text = json.dumps(rows, ensure_ascii=False, default=str)
    assert truth.expected_root_cause not in asserted_text
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in asserted_text
    for row in rows:
        payload = _payload(row)
        assert set(payload).issubset(
            {"scenario_id", "task_id", "run_id", "task_status", "reason", "allowed"}
        )


def test_oracle_timing_three_states_with_in_memory_state_machine() -> None:
    """零 DB 兜底：用真实 ``TaskStatus`` + ``ensure_transition`` 复验三态。

    不依赖控制库可达性，确保任何环境下三态与审计留痕都有可执行证据；
    真实控制库的 ``task_events`` 行由上一个用例覆盖。
    """

    now = utc_now()
    task = Task(
        task_id=new_id("task"),
        run_id=new_id("run"),
        goal="W10 T01 三态内存复验",
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
    )
    state: dict[str, Task] = {"task": task}

    def status_reader(task_id: str) -> TaskStatus:
        assert task_id == task.task_id
        return state["task"].status

    def move(target: TaskStatus) -> None:
        ensure_transition(state["task"].status, target)
        state["task"] = state["task"].model_copy(update={"status": target})

    move(TaskStatus.PLANNING)
    move(TaskStatus.EXECUTING)
    audit = _RecordingAudit()
    gate = OracleGate(status_reader=status_reader, audit=audit)

    with pytest.raises(OracleAccessDenied) as first:
        gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert first.value.details["reason"] == REASON_NOT_TERMINAL

    move(TaskStatus.VERIFYING)
    move(TaskStatus.COMPLETED)
    assert is_terminal(state["task"].status) is True
    truth = gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert truth.scenario_id == SCENARIO_ID

    with pytest.raises(OracleAccessDenied) as third:
        gate.read(SCENARIO_ID, task_id=task.task_id, run_id=task.run_id)
    assert third.value.details["reason"] == REASON_ALREADY_READ

    assert [item.reason for item in gate.records] == [
        REASON_NOT_TERMINAL,
        REASON_TERMINAL_OK,
        REASON_ALREADY_READ,
    ]
    assert [event.event_type for event in audit.events] == [
        "ORACLE_ACCESS_DENIED",
        "ORACLE_READ",
        "ORACLE_ACCESS_DENIED",
    ]
    asserted_text = json.dumps(
        [event.payload for event in audit.events], ensure_ascii=False
    )
    assert truth.expected_root_cause not in asserted_text
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in asserted_text
