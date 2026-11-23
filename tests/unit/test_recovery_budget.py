"""W08 门禁前置：启动恢复护栏（max_tasks / budget_s）与语义不变量。

`TestClient(app)` 每次触发 lifespan，历史未完成任务会被逐条 transition / emit，
而每条 SQL 都新建 TCP 连接 → 恢复风暴。本测试只验证护栏收敛处理规模、
未到限时行为与原实现一致，以及「EXECUTING 只标记待人工核对、不续跑」的
语义不变量，不连数据库。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.tasks.models import TaskStatus
from core.tasks.service import TaskService


class _FakeRepository:
    def __init__(self, tasks: list[Any]) -> None:
        self._tasks = list(tasks)
        self.list_calls = 0
        self.pages: list[tuple[int, int]] = []

    def list_unfinished_tasks(self, *, limit: int, offset: int) -> list[Any]:
        self.list_calls += 1
        self.pages.append((limit, offset))
        return self._tasks[offset : offset + limit]

    def latest_checkpoint(self, task_id: str) -> None:
        return None


class _FakeClock:
    """每次调用前进 `step` 秒；无需 sleep 即可触发 budget_s。"""

    def __init__(self, step: float = 0.6) -> None:
        self._now = 0.0
        self._step = step

    def monotonic(self) -> float:
        self._now += self._step
        return self._now


def _task(task_id: str, status: TaskStatus) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, status=status)


def _service(
    tasks: list[Any],
) -> tuple[TaskService, _FakeRepository, list[tuple[str, TaskStatus]]]:
    repository = _FakeRepository(tasks)
    service = TaskService.__new__(TaskService)
    service._repository = repository
    service._registry = None
    service._executor = None

    calls: list[tuple[str, TaskStatus]] = []

    def _transition(task_id: str, target: TaskStatus, **kwargs: Any) -> Any:
        calls.append((task_id, target))
        return SimpleNamespace(task_id=task_id, status=target)

    def _get_task(task_id: str) -> Any:
        return SimpleNamespace(task_id=task_id, status=TaskStatus.NEEDS_RECONCILIATION)

    service.transition = _transition  # type: ignore[method-assign]
    service.get_task = _get_task  # type: ignore[method-assign]
    service._emit = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return service, repository, calls


# ---- ① max_tasks 到达即停 -------------------------------------------------
def test_max_tasks_stops_early_and_returns_only_processed() -> None:
    tasks = [_task(f"t{i}", TaskStatus.PENDING) for i in range(250)]
    service, repository, _ = _service(tasks)

    recovered = service.recover_unfinished_tasks(max_tasks=5)

    assert [t.task_id for t in recovered] == ["t0", "t1", "t2", "t3", "t4"]
    # 只拉取了一页（100 条），没有把 250 条全部拉出来后再处理。
    assert repository.list_calls == 1


def test_max_tasks_exactly_one_page() -> None:
    tasks = [_task(f"t{i}", TaskStatus.PENDING) for i in range(250)]
    service, repository, _ = _service(tasks)

    recovered = service.recover_unfinished_tasks(max_tasks=100)

    assert len(recovered) == 100
    assert repository.list_calls == 1


# ---- ② budget_s 到达即停 --------------------------------------------------
def test_budget_s_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = [_task(f"t{i}", TaskStatus.PENDING) for i in range(250)]
    service, repository, _ = _service(tasks)
    # 只替换 core.tasks.service 模块内的 time 引用，不改全局 time 模块。
    monkeypatch.setattr("core.tasks.service.time", _FakeClock(step=0.6))

    recovered = service.recover_unfinished_tasks(budget_s=1.0)

    assert 0 < len(recovered) < len(tasks)
    assert repository.list_calls == 1


# ---- ③ 未到限时行为与原实现一致 ------------------------------------------
def test_no_limit_recovers_all_tasks_in_page_order() -> None:
    tasks = [_task(f"t{i}", TaskStatus.PENDING) for i in range(250)]
    service, repository, _ = _service(tasks)

    recovered = service.recover_unfinished_tasks()

    assert [t.task_id for t in recovered] == [f"t{i}" for i in range(250)]
    # 分页 100 + 100 + 50 → 3 次拉取，第 3 页不足一页即结束。
    assert repository.list_calls == 3
    assert repository.pages == [(100, 0), (100, 100), (100, 200)]


# ---- ④ 语义不变量：EXECUTING 只标记待核对、绝不续跑 ------------------------
def test_executing_only_marked_for_reconciliation_never_replayed() -> None:
    tasks = [
        _task("exec", TaskStatus.EXECUTING),
        _task("pending", TaskStatus.PENDING),
        _task("recon", TaskStatus.NEEDS_RECONCILIATION),
    ]
    service, _, calls = _service(tasks)

    recovered = service.recover_unfinished_tasks()
    by_id = {t.task_id: t for t in recovered}

    assert by_id["exec"].status == TaskStatus.NEEDS_RECONCILIATION
    assert by_id["pending"].status == TaskStatus.PENDING
    assert by_id["recon"].status == TaskStatus.NEEDS_RECONCILIATION
    # 只有 EXECUTING 触发一次 transition，且目标固定为 NEEDS_RECONCILIATION；
    # 不存在任何「续跑/重放工具」的转移。
    assert calls == [("exec", TaskStatus.NEEDS_RECONCILIATION)]
