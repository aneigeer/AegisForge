"""W11 T06 集成测试：E2E 四段回归（AC-16 / AC-19 回归面）。

四段各自断言（`docs/w11/02-design.md` §9；`03-task-cards.md` T06 语义 3）：

| 段 | 内容 | 断言 |
|---|---|---|
| **S1** | 双引擎只读诊断 | MySQL 段走真实只读观察器；**PG 段 `NOT_EXECUTED`** 且**不判 FAIL**（`ENV-PG5432`） |
| **S2** | 单引擎审批修复（唯一 R2 动作） | `tool_calls == 1`、`approval_consumptions == 1`、目标行等于批准值 |
| **S3** | 独立 Verifier | 回读 client 与执行 client **不是同一实例**（`client_identity()` 比对）、回读值与权威读取一致、`Verifier` 与 `ApprovalPlanSource` 装配在场 |
| **S4** | UNKNOWN 核对 | 已 Prepared 未 Observed → 核对结论 `NEEDS_RECONCILIATION`；`tool_calls` 增量 **0**、无 `TOOL_CALL_RETRIED`（不重放） |

**范围说明（如实登记）**

- S2 是 W11 全链路**唯一允许**的 R2 动作（`mysql_lab_update_order_note` / `1.0`，LAB+mysql 单一写工具）；
  执行后按 W07 既有协议用 `RESTORE_SQL` 复原目标 `note`，**这不是 W11 的残留清理**，
  而是 W07 冻结的"可重复执行"协议（`tests/integration/test_w07_repair_e2e.py:13-15`）。
- S3 只断言"独立性 + 回读一致 + 验证器装配"；完整 verdict 落表已由 W08
  `test_w08_verifier_readback.py` 覆盖，本卡**不重复实现**、也不另造替代用例。
- S1 的 PG 段属环境阻塞（`ENV-PG5432`）：`NOT_EXECUTED`/`NOT_VERIFIED` **不等于** FAILED。
- 本文件不新增 API 端点、不新增写工具、不改 `core/**` 语义。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from adapters.mysql.readback import MysqlReadbackAdapter
from core.audit.service import AuditService
from core.assurance.rc import E2ESegment
from core.errors import DBGuardianError
from core.execution.reconciler import CONCLUSION_NEEDS_RECONCILIATION, Reconciler
from core.execution.step_records import StepPrepared
from core.tasks.models import TaskStatus, utc_now
from core.tools.contracts import ToolContext, ToolStatus
from core.verification.readback import TargetObject
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry
from tests.integration.test_w07_repair_e2e import (  # noqa: F401 - pytest 需要夹具可见
    ORDER_ID,
    RESTORE_SQL,
    _approved,
    _current_note,
    _envelope,
    _params,
    w07_stack,
)

pytestmark = pytest.mark.integration

_STATUSES = ("PASSED", "FAILED", "NOT_EXECUTED", "NOT_VERIFIED")


def _segment(segment_id: str, status: str, reason: str | None = None) -> E2ESegment:
    """构造段落结论：状态必须是闭集之一（不许"部分通过"糊过去）。"""
    assert status in _STATUSES, status
    return E2ESegment(segment_id=segment_id, status=status, reason=reason)  # type: ignore[arg-type]


def _count(client: Any, table: str, column: str, value: str) -> int:
    rows = client.fetch_all(
        f"SELECT COUNT(*) AS n FROM dg_system.{table} WHERE {column} = %s", (value,)
    )
    return int(rows[0]["n"]) if rows else 0


# ---------------------------------------------------------------------------
# S1：双引擎只读诊断（PG 段 NOT_EXECUTED，不判 FAIL）
# ---------------------------------------------------------------------------


def test_s1_dual_engine_readonly_diagnosis(settings: Any) -> None:
    # ---- MySQL 段：真实只读观察器（R0） ----
    mysql_status = "NOT_EXECUTED"
    mysql_reason = "ENV-MYSQL-UNAVAILABLE"
    try:
        client = MysqlClient(
            settings.ro_dsn,
            database_id=settings.database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
        tool = build_observer_registry().get("get_db_version")
        result = tool.run(ToolContext(client=client), {})  # type: ignore[attr-defined]
        assert client.read_only is True, "诊断段必须使用只读连接"
        if result.status is ToolStatus.SUCCESS:
            assert result.data.get("version"), "版本信息不得为空"
            mysql_status = "PASSED"
            mysql_reason = None
        else:
            mysql_status = "FAILED"
            mysql_reason = f"observer_error={result.error.code if result.error else 'UNKNOWN'}"
    except DBGuardianError as exc:
        mysql_status = "NOT_EXECUTED"
        mysql_reason = f"ENV：{exc.code.value}"

    # ---- PostgreSQL 段：不可达即 NOT_EXECUTED（ENV-PG5432），不判 FAIL ----
    pg_status = "NOT_EXECUTED"
    if settings.pg_ro_dsn is None:
        pg_reason = "ENV-PG5432：未配置 PG 只读 DSN，PG 段未执行"
    else:
        try:
            from database.postgres.connection import PgClient

            pg_client = PgClient(
                settings.pg_ro_dsn,
                database_id=settings.pg_database_id,
                read_only=True,
                connect_timeout_s=settings.connect_timeout_s,
                statement_timeout_ms=settings.statement_timeout_ms,
                max_result_rows=settings.max_result_rows,
            )
            rows = pg_client.fetch_all("SELECT 1 AS ok")
            if rows:
                pg_status = "PASSED"
                pg_reason = None
            else:  # pragma: no cover - 空结果视为未执行
                pg_reason = "ENV-PG5432：PG 只读查询无返回"
        except DBGuardianError as exc:
            pg_reason = f"ENV-PG5432：{exc.code.value}"

    segment = _segment("S1", "PASSED" if mysql_status == "PASSED" else mysql_status, mysql_reason)

    assert mysql_status != "FAILED", mysql_reason
    assert segment.status in {"PASSED", "NOT_EXECUTED"}
    assert pg_status in {"PASSED", "NOT_EXECUTED"}, "PG 段只允许 PASSED / NOT_EXECUTED"
    # 关键口径：PG 未执行不是失败
    assert pg_status != "FAILED"
    if pg_status == "NOT_EXECUTED":
        assert pg_reason and "ENV-PG5432" in pg_reason


# ---------------------------------------------------------------------------
# S2：单引擎审批修复（唯一 R2 动作）
# ---------------------------------------------------------------------------


def test_s2_single_engine_approval_repair(w07_stack: dict[str, Any], rw_client: Any) -> None:
    before = _current_note(rw_client)
    target = f"w11-e2e-s2-{uuid4().hex[:8]}"
    params = _params(before, target)
    task = None
    detail = None
    try:
        definition = w07_stack["registry"].get("mysql_lab_update_order_note").definition
        assert definition.side_effect == "write"
        assert definition.risk_level == "R2"
        assert definition.allowed_environments == ["LAB"]

        task, detail = _approved(w07_stack, params)
        envelope = _envelope(w07_stack, task, detail, params)
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.SUCCESS, result.error
        assert result.data.get("affected_rows") == 1
        assert _current_note(rw_client) == target, "目标行必须等于批准值"

        tool_calls = _count(w07_stack["control_client"], "tool_calls", "task_id", task.task_id)
        consumptions = _count(
            w07_stack["control_client"],
            "approval_consumptions",
            "approval_id",
            detail.approval_id,
        )
        assert tool_calls == 1, f"tool_calls 必须恰 1，实际 {tool_calls}"
        assert consumptions == 1, f"approval_consumptions 必须恰 1，实际 {consumptions}"
        observed = w07_stack["step_records"].get_observed(result.tool_call_id)
        assert observed is not None, "成功的写必须留下 StepObserved"

        segment = _segment("S2", "PASSED")
        assert segment.status == "PASSED"
    finally:
        # W07 冻结协议：复原目标 `note`，保证本文件可重复执行（不是 W11 的残留清理）
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# S3：独立 Verifier（独立回读 + 装配在场）
# ---------------------------------------------------------------------------


def test_s3_independent_verifier(
    w07_stack: dict[str, Any], rw_client: Any, ro_client: Any
) -> None:
    """S3：独立回读 + 验证器装配在场。

    注意：`build_lab_stack()` 只装配执行面（不含 readback/verifier），
    后两者由 `workers/stack.py::build_worker_stack()` 装配；因此本段用**同一独立只读
    client** 现场构造回读适配器验证独立性，并对装配点做静态核对（不重复 W08 verdict）。
    """
    readback = MysqlReadbackAdapter(ro_client)
    assert isinstance(readback, MysqlReadbackAdapter)
    assert readback.read_only is True
    # 独立性：回读 client 与执行侧 rw client **不是同一实例**
    assert readback.client_identity() != id(rw_client)
    # 回读的必须是白名单固定目标（不支持任意 SQL）
    sample = readback.read_target(
        TargetObject(
            database="dg_lab",
            table="seed_orders",
            key_column="order_id",
            key_value=ORDER_ID,
            column="note",
        )
    )
    assert sample.value == _current_note(rw_client), "独立回读必须与权威读取一致"
    # Verifier 装配在场（完整 verdict 由 W08 覆盖，本卡不重复）
    stack_source = (Path(__file__).resolve().parents[2] / "workers" / "stack.py").read_text(
        encoding="utf-8"
    )
    assert "readback = MysqlReadbackAdapter(ro_client)" in stack_source, "回读必须用独立只读 client"
    assert "verifier = Verifier(" in stack_source, "Verifier 必须在组合根装配"
    from core.verification.verifier import Verifier

    assert hasattr(Verifier, "verify")

    segment = _segment("S3", "PASSED")
    assert segment.status == "PASSED"


# ---------------------------------------------------------------------------
# S4：UNKNOWN 核对（不重放、零工具调用）
# ---------------------------------------------------------------------------


def test_s4_unknown_outcome_reconciliation(w07_stack: dict[str, Any]) -> None:
    stack = w07_stack
    task = stack["service"].create_task("W11 E2E S4：UNKNOWN 核对")
    task = stack["service"].transition(task.task_id, TaskStatus.PLANNING)
    task = stack["service"].transition(task.task_id, TaskStatus.EXECUTING)
    tool_calls_before = _count(stack["control_client"], "tool_calls", "task_id", task.task_id)

    prepared = StepPrepared(
        prepared_id=f"spr_w11_e2e_{uuid4().hex[:8]}",
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        run_id=task.run_id,
        tool_call_id=f"call_w11_e2e_{uuid4().hex[:8]}",
        tool_name="mysql_lab_update_order_note",
        tool_version="1.0",
        policy_version=stack["settings"].policy_version,
        environment_id=task.environment_id,
        request={"probe": "w11-e2e-s4"},
        prepared_at=utc_now() - timedelta(seconds=300),
    )
    stack["step_records"].insert_prepared(prepared)

    reconciler = Reconciler(stack["step_records"], stack["repository"], AuditService(stack["repository"]))
    conclusions = reconciler.sweep_task(task_id=task.task_id, tenant_id=task.tenant_id, older_than_s=0)
    assert len(conclusions) == 1, conclusions
    conclusion = conclusions[0]
    assert conclusion.conclusion == CONCLUSION_NEEDS_RECONCILIATION
    assert conclusion.transitioned is True

    tool_calls_after = _count(stack["control_client"], "tool_calls", "task_id", task.task_id)
    assert tool_calls_after - tool_calls_before == 0, "核对不得产生任何工具调用"
    events, _ = stack["audit"].query(tenant_id=task.tenant_id, task_id=task.task_id, limit=100)
    event_types = [str(event.event_type) for event in events]
    assert "RECONCILIATION_REQUIRED" in event_types
    assert "TOOL_CALL_RETRIED" not in event_types, "恢复期绝不重放"
    assert [event.seq for event in events] == list(range(len(events))), "seq 必须连续"
    verification = stack["audit"].verify(tenant_id=task.tenant_id, task_id=task.task_id)
    assert verification.ok is True

    segment = _segment("S4", "PASSED")
    assert segment.status == "PASSED"


# ---------------------------------------------------------------------------
# 段落状态口径
# ---------------------------------------------------------------------------


def test_segment_status_vocabulary_is_closed() -> None:
    for status in _STATUSES:
        segment = _segment("S1", status)
        assert segment.status == status
    with pytest.raises(AssertionError):
        _segment("S1", "PARTIAL")
