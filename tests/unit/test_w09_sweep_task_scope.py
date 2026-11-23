"""W10 L1：`Reconciler.sweep_task()` 的任务级核对范围（全离线替身，不连库）。

被测命题（`docs/w09/13-l1-l2-design.md` §3.4 六条）：

1. `sweep_task()` 把 `task_id` 传给仓储，其余分页参数与 `sweep()` 一致；
2. `sweep()` **不**传 `task_id` —— 逐字证明 T06 替身（形参表不含该键、
   `self.calls` 被逐字断言）不会被喂新关键字参数，即向后兼容；
3. `sweep_task()` 只产出本任务的结论，`scanned_total` 只计本任务被扫到的行；
4. `sweep()` / `sweep_detailed()` 签名冻结，`sweep_task()` 形参表逐字确定；
5. 空 `task_id` 抛 `RequestInvalidError`（fail closed，不静默退化为全租户）；
6. 同一数据下 `sweep_task()` 的结论与 `sweep()` 按任务过滤后的结论一致。

不连库、不依赖 `DG_ADMIN_DSN` / PG：只用注入替身（同
`tests/unit/test_w09_sweep_scanned.py` 的做法），不扫真实历史残留。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from core.errors import RequestInvalidError, TaskNotFoundError
from core.execution.reconciler import (
    CONCLUSION_NEEDS_RECONCILIATION,
    REASON_ALREADY_CONVERGED,
    Reconciler,
)
from core.execution.step_records import StepPrepared
from core.tasks.models import Task, TaskStatus

TENANT = "tenant_local"
OTHER_TENANT = "tenant_other"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prepared(
    task_id: str, *, tenant_id: str = TENANT, index: int = 0
) -> StepPrepared:
    return StepPrepared(
        prepared_id=f"prep_{tenant_id}_{task_id}_{index}",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=f"run_{task_id}",
        tool_call_id=f"tc_{tenant_id}_{task_id}_{index}",
        tool_name="mysql_lab_update_order_note",
        tool_version="v1",
        policy_version="w06-v1",
        environment_id="env_mysql_local",
        prepared_at=_now(),
    )


def _task(
    task_id: str,
    *,
    tenant_id: str = TENANT,
    status: TaskStatus = TaskStatus.NEEDS_RECONCILIATION,
) -> Task:
    now = _now()
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        goal=f"goal {task_id}",
        status=status,
        created_at=now,
        updated_at=now,
        tenant_id=tenant_id,
        environment_id="env_mysql_local",
    )


# ---------------------------------------------------------------------------
# 替身：只覆盖 `Reconciler` 实际触达的接口面（含可选 task_id 收窄）
# ---------------------------------------------------------------------------


class _FakeClient:
    """只为 `_tenants_with_prepared()` 提供 `fetch_all`。"""

    def __init__(self, tenants: list[str]) -> None:
        self._tenants = list(tenants)
        self.queries: list[str] = []

    def fetch_all(self, sql: str, params: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return [{"tenant_id": item} for item in self._tenants]


class _FakeStepRecords:
    """记录每次调用的**完整可见 kwargs**，用于证明「传了什么/没传什么」。

    `find_unobserved` 用 `**extra` 收尾（而不是显式 `task_id=None`）：只有这样才能
    区分「没传 `task_id`」与「传了 `task_id=None`」——第 2 条用例的正是不传。
    """

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
        **extra: Any,
    ) -> list[StepPrepared]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "older_than_s": older_than_s,
                "limit": limit,
                "offset": offset,
                "extra": dict(extra),
            }
        )
        rows = [item for item in self._prepared if item.tenant_id == tenant_id]
        narrowed = extra.get("task_id", None)
        if narrowed is not None:
            rows = [item for item in rows if item.task_id == narrowed]
        return rows[offset : offset + limit]

    def get_observed(self, tool_call_id: str) -> Any:
        if tool_call_id in self._observed:
            return object()  # 只用于 `is not None` 判定
        return None


class _FakeTasks:
    """任务读取替身；已收敛任务不应发生状态迁移，一旦被调用即失败。"""

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


def _three_task_dataset() -> tuple[list[StepPrepared], list[Task]]:
    """目标任务 A 有 2 条未观测，B / C 各 1 条（用于证明收窄真的生效）。"""
    prepared = [
        _prepared("task_a", index=0),
        _prepared("task_a", index=1),
        _prepared("task_b", index=0),
        _prepared("task_c", index=0),
    ]
    tasks = [_task("task_a"), _task("task_b"), _task("task_c")]
    return prepared, tasks


# ---------------------------------------------------------------------------
# 1) `sweep_task` 只把 `task_id` 传给仓储
# ---------------------------------------------------------------------------


def test_sweep_task_passes_task_id_and_keeps_other_paging_arguments() -> None:
    prepared, tasks = _three_task_dataset()
    reconciler, step_records, _, _ = _reconciler(prepared, tasks=tasks)

    reconciler.sweep_task(tenant_id=TENANT, task_id="task_a", older_than_s=0)

    assert len(step_records.calls) == 1
    call = step_records.calls[0]
    assert call["extra"] == {"task_id": "task_a"}
    assert call["tenant_id"] == TENANT
    assert call["older_than_s"] == 0
    # 分页参数与 `sweep()` 一致（单页上限 500、首屏 offset 0）。
    assert call["limit"] == 500
    assert call["offset"] == 0


def test_sweep_task_passes_task_id_on_every_page() -> None:
    """翻页时 `task_id` 必须逐页透传，否则第二页会退化成全租户。"""
    prepared = [_prepared("task_a", index=index) for index in range(501)]
    prepared += [_prepared("task_b", index=0)]
    reconciler, step_records, _, _ = _reconciler(
        prepared, tasks=[_task("task_a"), _task("task_b")]
    )

    reconciler.sweep_task(tenant_id=TENANT, task_id="task_a", older_than_s=0)

    assert [call["offset"] for call in step_records.calls] == [0, 500]
    assert all(call["extra"] == {"task_id": "task_a"} for call in step_records.calls)
    assert all(call["limit"] == 500 for call in step_records.calls)


# ---------------------------------------------------------------------------
# 2) `sweep()` 不传 `task_id`（向后兼容的逐字证明）
# ---------------------------------------------------------------------------


def test_sweep_does_not_pass_task_id_keyword_at_all() -> None:
    prepared = [_prepared("task_a", index=0), _prepared("task_b", index=0)]
    reconciler, step_records, _, _ = _reconciler(
        prepared, tasks=[_task("task_a"), _task("task_b")]
    )

    reconciler.sweep(tenant_id=TENANT, older_than_s=0)

    assert len(step_records.calls) == 1
    # 逐字证明：kwargs 里**不含** `task_id` 键（不是 "task_id=None"）。
    assert "task_id" not in step_records.calls[0]["extra"]
    assert step_records.calls[0]["extra"] == {}


def test_sweep_detailed_does_not_pass_task_id_keyword_at_all() -> None:
    prepared = [_prepared("task_a", index=0)]
    reconciler, step_records, _, _ = _reconciler(prepared, tasks=[_task("task_a")])

    reconciler.sweep_detailed(tenant_id=TENANT, older_than_s=0)

    assert len(step_records.calls) == 1
    assert "task_id" not in step_records.calls[0]["extra"]


# ---------------------------------------------------------------------------
# 3) `sweep_task` 只产出本任务结论、`scanned_total` 只计本任务行
# ---------------------------------------------------------------------------


def test_sweep_task_returns_only_target_task_conclusions() -> None:
    prepared, tasks = _three_task_dataset()
    reconciler, _, _, audit = _reconciler(prepared, tasks=tasks)

    results = reconciler.sweep_task(tenant_id=TENANT, task_id="task_a", older_than_s=0)

    assert {item.task_id for item in results} == {"task_a"}
    assert len(results) == 2
    assert all(
        item.conclusion == CONCLUSION_NEEDS_RECONCILIATION for item in results
    )
    assert all(item.reason == REASON_ALREADY_CONVERGED for item in results)
    # 其它任务的结论没有产出，因此审计事件也只有本任务的 2 条。
    assert len(audit.events) == 2


def test_sweep_task_scanned_total_counts_only_target_task_rows() -> None:
    """`scanned_total` 是「本任务被扫到的行数」，不含其它任务的残留。"""
    prepared, tasks = _three_task_dataset()
    reconciler, _, _, _ = _reconciler(prepared, tasks=tasks)

    report = reconciler._sweep(  # noqa: SLF001 - 口径由设计 §3.4 第 3 条指定
        tenant_id=TENANT, older_than_s=0, task_id="task_a"
    )

    assert report.scanned_total == 2
    assert len(report.conclusions) == 2
    assert report.scanned_tenants == (TENANT,)


def test_sweep_task_keeps_scanned_total_when_rows_are_filtered_out() -> None:
    """扫描范围内被 `_reconcile_one()` 过滤的行仍计入 `scanned_total`（KD-2）。"""
    prepared = [_prepared("task_a", index=0), _prepared("task_a", index=1)]
    observed = {prepared[0].tool_call_id, prepared[1].tool_call_id}
    reconciler, _, _, audit = _reconciler(
        prepared, tasks=[_task("task_a")], observed=observed
    )

    report = reconciler._sweep(  # noqa: SLF001
        tenant_id=TENANT, older_than_s=0, task_id="task_a"
    )

    assert report.scanned_total == 2
    assert report.conclusions == ()
    assert audit.events == []


# ---------------------------------------------------------------------------
# 4) 签名冻结（`sweep` / `sweep_detailed` 逐字不变，`sweep_task` 逐字确定）
# ---------------------------------------------------------------------------


def test_signatures_are_frozen() -> None:
    sweep_signature = inspect.signature(Reconciler.sweep)
    detailed_signature = inspect.signature(Reconciler.sweep_detailed)
    task_signature = inspect.signature(Reconciler.sweep_task)

    assert list(sweep_signature.parameters) == ["self", "tenant_id", "older_than_s"]
    assert list(detailed_signature.parameters) == ["self", "tenant_id", "older_than_s"]
    assert list(task_signature.parameters) == [
        "self",
        "task_id",
        "tenant_id",
        "older_than_s",
    ]

    # 默认值逐字不变（`sweep()` 的 A6 契约）。
    assert sweep_signature.parameters["tenant_id"].default is None
    assert sweep_signature.parameters["older_than_s"].default == 60.0
    assert detailed_signature.parameters["tenant_id"].default is None
    assert detailed_signature.parameters["older_than_s"].default == 60.0

    # `sweep_task` 的三个参数都是 keyword-only（防止位置实参静默错位）。
    for name in ("task_id", "tenant_id", "older_than_s"):
        assert (
            task_signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        )
    assert task_signature.parameters["task_id"].default is inspect.Parameter.empty
    assert task_signature.parameters["tenant_id"].default is None
    assert task_signature.parameters["older_than_s"].default == 60.0


# ---------------------------------------------------------------------------
# 5) 空 `task_id` 抛 `RequestInvalidError`（fail closed）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_task_id", ["", "   ", "\t", "\n"])
def test_sweep_task_rejects_blank_task_id(bad_task_id: str) -> None:
    prepared, tasks = _three_task_dataset()
    reconciler, step_records, _, _ = _reconciler(prepared, tasks=tasks)

    with pytest.raises(RequestInvalidError):
        reconciler.sweep_task(tenant_id=TENANT, task_id=bad_task_id, older_than_s=0)

    # fail closed：校验失败时一次仓储查询都不发（不会退化成全租户扫描）。
    assert step_records.calls == []


def test_sweep_task_requires_task_id_keyword() -> None:
    prepared, tasks = _three_task_dataset()
    reconciler, step_records, _, _ = _reconciler(prepared, tasks=tasks)

    with pytest.raises(TypeError):
        reconciler.sweep_task(tenant_id=TENANT, older_than_s=0)  # type: ignore[call-arg]

    assert step_records.calls == []


# ---------------------------------------------------------------------------
# 6) 同任务下 `sweep_task` 与 `sweep()` 过滤后一致
# ---------------------------------------------------------------------------


def test_sweep_task_agrees_with_sweep_filtered_by_task() -> None:
    prepared, tasks = _three_task_dataset()

    narrow_reconciler, _, _, _ = _reconciler(list(prepared), tasks=list(tasks))
    wide_reconciler, _, _, _ = _reconciler(list(prepared), tasks=list(tasks))

    narrowed = narrow_reconciler.sweep_task(
        tenant_id=TENANT, task_id="task_a", older_than_s=0
    )
    wide = [
        item
        for item in wide_reconciler.sweep(tenant_id=TENANT, older_than_s=0)
        if item.task_id == "task_a"
    ]

    assert [item.to_public_dict() for item in narrowed] == [
        item.to_public_dict() for item in wide
    ]


def test_sweep_task_agrees_with_sweep_detailed_on_conclusion_count() -> None:
    prepared, tasks = _three_task_dataset()
    reconciler, _, _, _ = _reconciler(prepared, tasks=tasks)

    narrowed = reconciler.sweep_task(tenant_id=TENANT, task_id="task_b", older_than_s=0)
    report = reconciler._sweep(  # noqa: SLF001
        tenant_id=TENANT, older_than_s=0, task_id="task_b"
    )

    assert len(narrowed) == report.conclusion_count == 1
    assert narrowed[0].task_id == "task_b"


def test_sweep_task_respects_explicit_tenant_and_does_not_enumerate_tenants() -> None:
    """给了 `tenant_id` 就不查 `SELECT DISTINCT tenant_id`（不多做全表扫）。"""
    prepared = [_prepared("task_a", tenant_id=OTHER_TENANT, index=0)]
    reconciler, step_records, _, _ = _reconciler(
        prepared, tasks=[_task("task_a", tenant_id=OTHER_TENANT)], tenants=[OTHER_TENANT]
    )

    results = reconciler.sweep_task(
        tenant_id=OTHER_TENANT, task_id="task_a", older_than_s=0
    )

    assert step_records.client.queries == []
    assert [item.task_id for item in results] == ["task_a"]
