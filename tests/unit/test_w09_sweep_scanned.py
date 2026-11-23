"""W09 T06 / AC-20（KD-2）：`scanned_total` 与 `conclusions` 分离（全离线构造数据）。

口径（`docs/w09/03-task-cards.md:658`）：

- `scanned_total` = 各租户分页 `find_unobserved()` 返回行数之和，**在
  `_reconcile_one()` 过滤之前**累加；
- `conclusions` = 产出结论的条目，`len(conclusions)` 等于 W08 口径的 `scanned`；
- `sweep()` 的签名与返回类型**不变**（仍 `list[ReconcileResult]`），
  `tests/integration/test_step_prepared.py:211` 的 `results == []` 依赖此契约。

不连库、不依赖 `DG_ADMIN_DSN` / PG 5432：控制库有 830+ 历史行，`sweep` 断言一律用
注入替身（`03-task-cards.md:734`），不扫真实历史。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from core.errors import TaskNotFoundError
from core.execution.reconciler import (
    CONCLUSION_NEEDS_RECONCILIATION,
    REASON_ALREADY_CONVERGED,
    ReconcileResult,
    Reconciler,
    SweepReport,
)
from core.execution.step_records import StepPrepared
from core.tasks.models import Task, TaskStatus

TENANT = "tenant_local"


# ---------------------------------------------------------------------------
# 替身：只覆盖 `Reconciler` 实际触达的接口面
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prepared(task_id: str, *, tenant_id: str = TENANT, index: int = 0) -> StepPrepared:
    return StepPrepared(
        prepared_id=f"prep_{task_id}_{index}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=f"run_{task_id}",
        tool_call_id=f"tc_{task_id}_{index}",
        tool_name="mysql_lab_update_order_note",
        tool_version="v1",
        policy_version="w06-v1",
        environment_id="env_mysql_local",
        prepared_at=_now(),
    )


def _task(task_id: str, *, status: TaskStatus = TaskStatus.NEEDS_RECONCILIATION) -> Task:
    now = _now()
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        goal=f"goal {task_id}",
        status=status,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id="env_mysql_local",
    )


class _FakeClient:
    """只为 `_tenants_with_prepared()` 提供 `fetch_all`。"""

    def __init__(self, tenants: list[str]) -> None:
        self._tenants = list(tenants)
        self.queries: list[str] = []

    def fetch_all(self, sql: str, params: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return [{"tenant_id": item} for item in self._tenants]


class _FakeStepRecords:
    """`find_unobserved` 分页 + `get_observed` 过滤的最小替身。"""

    def __init__(
        self,
        prepared: list[StepPrepared],
        *,
        observed: set[str] | None = None,
        tenants: list[str] | None = None,
    ) -> None:
        self._prepared = list(prepared)
        self._observed = set(observed or ())
        self.client = _FakeClient(tenants if tenants is not None else [TENANT])
        self.calls: list[dict[str, Any]] = []

    def find_unobserved(
        self,
        *,
        tenant_id: str,
        older_than_s: float,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StepPrepared]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "older_than_s": older_than_s,
                "limit": limit,
                "offset": offset,
            }
        )
        rows = [item for item in self._prepared if item.tenant_id == tenant_id]
        return rows[offset : offset + limit]

    def get_observed(self, tool_call_id: str) -> Any:
        if tool_call_id in self._observed:
            return object()  # 只用于 `is not None` 判定
        return None


class _FakeTasks:
    """任务读取替身；本文件不需要状态迁移，一旦被调用即失败（防止隐性写）。"""

    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = {item.task_id: item for item in tasks}
        self.updates: list[Any] = []

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        if task_id not in self._tasks:
            raise TaskNotFoundError(
                "任务不存在", details={"task_id": task_id, "tenant_id": tenant_id}
            )
        return self._tasks[task_id]

    def update_task_status(self, *args: Any, **kwargs: Any) -> Any:
        self.updates.append((args, kwargs))
        raise AssertionError("本用例的任务已收敛，不应发生状态迁移")


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event


def _reconciler(
    prepared: list[StepPrepared],
    *,
    tasks: list[Task] | None = None,
    observed: set[str] | None = None,
    tenants: list[str] | None = None,
) -> tuple[Reconciler, _FakeStepRecords, _FakeTasks, _FakeAudit]:
    step_records = _FakeStepRecords(prepared, observed=observed, tenants=tenants)
    task_repository = _FakeTasks(tasks if tasks is not None else [])
    audit = _FakeAudit()
    return (
        Reconciler(step_records, task_repository, audit),  # type: ignore[arg-type]
        step_records,
        task_repository,
        audit,
    )


# ---------------------------------------------------------------------------
# AC-20 主判据：5 行候选仅 2 行产出结论 → scanned_total == 5、conclusions == 2
# ---------------------------------------------------------------------------


def test_five_scanned_rows_two_conclusions() -> None:
    # 5 行都是"已 Prepared 未 Observed"，但其中 3 行在本轮二次确认时已补齐 Observed
    # （模拟"执行链刚补上回执"），因此只有 2 行产出结论。
    prepared = [_prepared(f"task_{index}", index=index) for index in range(5)]
    observed = {f"tc_task_{index}_{index}" for index in range(3)}
    tasks = [_task("task_3"), _task("task_4")]
    reconciler, step_records, task_repository, audit = _reconciler(
        prepared, tasks=tasks, observed=observed
    )

    report = reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert report.scanned_total == 5, "扫描行数必须在过滤前计数"
    assert len(report.conclusions) == 2
    assert report.conclusion_count == 2
    assert report.scanned_tenants == (TENANT,)
    assert {item.task_id for item in report.conclusions} == {"task_3", "task_4"}
    assert all(
        item.conclusion == CONCLUSION_NEEDS_RECONCILIATION for item in report.conclusions
    )
    assert all(item.reason == REASON_ALREADY_CONVERGED for item in report.conclusions)
    # 已收敛的任务不得再迁移状态（核对器只读证据 + 只追加审计）。
    assert task_repository.updates == []
    assert len(audit.events) == 2
    assert len(step_records.calls) == 1


def test_scanned_total_is_not_reduced_by_conclusion_filtering() -> None:
    """反向证明：全部行都被过滤时 `scanned_total` 仍等于真实行数（不为 0）。"""
    prepared = [_prepared(f"task_{index}", index=index) for index in range(4)]
    observed = {item.tool_call_id for item in prepared}
    reconciler, _, _, audit = _reconciler(prepared, observed=observed)

    report = reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert report.scanned_total == 4
    assert report.conclusions == ()
    assert audit.events == []


def test_empty_scan_reports_zero_total() -> None:
    reconciler, step_records, _, _ = _reconciler([])

    report = reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert report.scanned_total == 0
    assert report.conclusions == ()
    assert len(step_records.calls) == 1


# ---------------------------------------------------------------------------
# 分页：单页上限 500，翻页累计行数不得丢
# ---------------------------------------------------------------------------


def test_scanned_total_accumulates_across_pages() -> None:
    prepared = [_prepared(f"task_{index}", index=index) for index in range(501)]
    observed = {item.tool_call_id for item in prepared}
    reconciler, step_records, _, _ = _reconciler(prepared, observed=observed)

    report = reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert report.scanned_total == 501
    assert report.conclusions == ()
    assert [call["offset"] for call in step_records.calls] == [0, 500]
    assert all(call["limit"] == 500 for call in step_records.calls)


# ---------------------------------------------------------------------------
# 多租户：`tenant_id=None` 时逐个租户扫，`scanned_tenants` 如实登记
# ---------------------------------------------------------------------------


def test_scanned_tenants_lists_every_scanned_tenant() -> None:
    prepared = [
        _prepared("task_a", tenant_id="tenant_a", index=0),
        _prepared("task_b", tenant_id="tenant_b", index=0),
    ]
    reconciler, _, _, _ = _reconciler(
        prepared,
        tasks=[_task("task_a"), _task("task_b")],
        tenants=["tenant_a", "tenant_b"],
    )

    report = reconciler.sweep_detailed(older_than_s=0)

    assert report.scanned_tenants == ("tenant_a", "tenant_b")
    assert report.scanned_total == 2
    assert len(report.conclusions) == 2


def test_explicit_tenant_does_not_enumerate_tenants() -> None:
    """给了 `tenant_id` 就不该再查 `SELECT DISTINCT tenant_id`（避免多余全表扫）。"""
    reconciler, step_records, _, _ = _reconciler([], tenants=["tenant_a"])
    reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)
    assert step_records.client.queries == []


# ---------------------------------------------------------------------------
# A6：`sweep()` 契约不变
# ---------------------------------------------------------------------------


def test_sweep_still_returns_list_and_signature_is_unchanged() -> None:
    prepared = [_prepared("task_0", index=0)]
    reconciler, _, _, audit = _reconciler(prepared, tasks=[_task("task_0")])

    results = reconciler.sweep(tenant_id=TENANT, older_than_s=0)

    assert isinstance(results, list)
    assert results and isinstance(results[0], ReconcileResult)
    assert len(results) == 1
    assert len(audit.events) == 1

    # 签名冻结：关键字参数名字与默认值逐字不变。
    signature = inspect.signature(Reconciler.sweep)
    assert list(signature.parameters) == ["self", "tenant_id", "older_than_s"]
    assert signature.parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["tenant_id"].default is None
    assert signature.parameters["older_than_s"].default == 60.0


def test_sweep_returns_empty_list_when_nothing_to_reconcile() -> None:
    """`tests/integration/test_step_prepared.py:211` 的 `results == []` 契约。"""
    reconciler, _, _, _ = _reconciler([])

    results = reconciler.sweep(tenant_id=TENANT, older_than_s=0)

    assert results == []
    assert type(results) is list


def test_sweep_and_sweep_detailed_agree_on_conclusions() -> None:
    prepared = [_prepared("task_0", index=0)]
    tasks = [_task("task_0")]
    reconciler_a, _, _, _ = _reconciler(prepared, tasks=list(tasks))
    reconciler_b, _, _, _ = _reconciler(prepared, tasks=list(tasks))

    flat = reconciler_a.sweep(tenant_id=TENANT, older_than_s=0)
    detailed = reconciler_b.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert [item.to_public_dict() for item in flat] == [
        item.to_public_dict() for item in detailed.conclusions
    ]


# ---------------------------------------------------------------------------
# SweepReport 自身形状
# ---------------------------------------------------------------------------


def test_sweep_report_is_frozen_and_serialisable() -> None:
    conclusion = ReconcileResult(
        prepared_id="prep_1",
        tool_call_id="tc_1",
        task_id="task_1",
        conclusion=CONCLUSION_NEEDS_RECONCILIATION,
        task_status="NEEDS_RECONCILIATION",
        transitioned=False,
        reason=REASON_ALREADY_CONVERGED,
    )
    report = SweepReport(
        scanned_total=5,
        conclusions=(conclusion,),
        scanned_tenants=(TENANT,),
    )

    assert report.conclusion_count == 1
    payload = report.to_public_dict()
    assert payload["scanned_total"] == 5
    assert payload["scanned_tenants"] == [TENANT]
    assert payload["conclusions"][0]["task_id"] == "task_1"
    with pytest.raises(Exception):
        report.scanned_total = 6  # type: ignore[misc]
