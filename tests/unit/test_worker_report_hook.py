"""W10 L2-T02：运行器报告接线（假 stack + 假发布器，全离线）。

覆盖设计 §5.5 六条：

1. `EXECUTED` 触发：`publish` 恰 1 次、`reports_generated == 1`、`run_once` 的
   执行结论与"无发布器"时**逐字相同**；
2. `UNKNOWN_OUTCOME` / `FAILED` 各自触发 1 次；
3. `SKIPPED` / `DENIED` 零调用（无执行事实，不生成报告）；
4. `stack.report_publisher is None`：零调用、零计数、零异常（历史替身零行为变化）；
5. 失败不阻塞：`publish` 抛错 → `report_failed == 1`、`failed`/`clean` 与基线逐字
   相同、`run_once` 不抛；
6. `RunReport` 增量：`merged` 相加、`to_public_dict()` 含两键、
   `RunReport().clean is True`、`RunReport(report_failed=9).clean is True`。

不连库、不连 PG、不写任何表：全部端口为本地替身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.tasks.models import Task, TaskStatus, utc_now
from core.tools.contracts import ToolStatus
from workers.discovery import Candidate
from workers.runner import (
    OUTCOME_DENIED,
    OUTCOME_EXECUTED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_UNKNOWN,
    RunReport,
    WorkerRunner,
)
from workers.report_assembly import ALERT_REPORT_FAILED, ReportOutcome

TENANT = "tenant_local"
TOOL_CALL_ID = "call_hook_0001"


# ---------------------------------------------------------------------------
# 假依赖
# ---------------------------------------------------------------------------


def _task(*, cancel_requested: bool = False) -> Task:
    now = utc_now()
    return Task(
        task_id="task_hook_01",
        run_id="run_hook_01",
        goal="W10 L2-T02 报告接线用例",
        status=TaskStatus.WAITING_APPROVAL,
        created_at=now,
        updated_at=now,
        tenant_id=TENANT,
        environment_id="env_mysql_local",
        cancel_requested=cancel_requested,
        state_version=1,
    )


@dataclass
class _FakeRepository:
    task: Task
    get_task_calls: list[str] = field(default_factory=list)

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        self.get_task_calls.append(task_id)
        return self.task


class _FakeTaskService:
    """`transition()` 替身：迁移 + 涨版本（与真实语义一致）。"""

    def __init__(self, repository: _FakeRepository) -> None:
        self._repository = repository

    def transition(self, task_id: str, target: TaskStatus, **kwargs: Any) -> Task:
        updated = self._repository.task.model_copy(
            update={
                "status": target,
                "state_version": self._repository.task.state_version + 1,
            }
        )
        self._repository.task = updated
        return updated


class _FakeWorker:
    """按脚本返回 `ToolResult` 最小面（`status` + `tool_call_id`）。"""

    def __init__(self, status: ToolStatus, *, tool_call_id: str | None = TOOL_CALL_ID):
        self.worker_id = "worker_test_l2"
        self.status = status
        self.tool_call_id = tool_call_id
        self.acquire_calls = 0
        self.submit_calls = 0
        self.release_calls = 0

    def acquire_lease(
        self, task_id: str, environment_id: str, ttl_s: float
    ) -> Any:
        self.acquire_calls += 1
        return SimpleNamespace(lease_id="lease_l2", task_id=task_id)

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.submit_calls += 1
        return SimpleNamespace(status=self.status, tool_call_id=self.tool_call_id)

    def release(self) -> Any:
        self.release_calls += 1
        return SimpleNamespace(lease_id="lease_l2")


class _FakeHeartbeat:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.lost = False
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self.stopped = True


class _FakePublisher:
    """记录每次 `publish` 调用；可预置抛错或返回失败结论。"""

    def __init__(
        self,
        *,
        raises: BaseException | None = None,
        outcome: ReportOutcome | None = None,
    ) -> None:
        self._raises = raises
        self._outcome = outcome or ReportOutcome(
            generated=True, report_id="rpt_hook_0001"
        )
        self.calls: list[dict[str, Any]] = []

    def publish(
        self, task: Task, *, tool_call_id: str, succeeded: bool
    ) -> ReportOutcome:
        self.calls.append(
            {
                "task_id": getattr(task, "task_id", None),
                "tool_call_id": tool_call_id,
                "succeeded": succeeded,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._outcome


class _Stack(SimpleNamespace):
    """`RunnerStackPort` 的最小可运行替身（只提供运行器真正触达的面）。"""


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        worker_poll_interval_s=0.01,
        worker_shutdown_grace_s=30.0,
        worker_lease_ttl_s=30.0,
        worker_heartbeat_interval_s=10.0,
        gateway_envelope_ttl_s=60.0,
        worker_actor_user_id="system:worker",
        worker_max_concurrency=1,
    )


def _stack(
    *,
    reporter: Any = None,
    status: ToolStatus = ToolStatus.SUCCESS,
    cancel_requested: bool = False,
) -> tuple[_Stack, _FakePublisher | None, _FakeWorker]:
    repository = _FakeRepository(task=_task(cancel_requested=cancel_requested))
    worker = _FakeWorker(status)
    stack = _Stack(
        settings=_settings(),
        tenant_id=TENANT,
        worker_id="worker_test_l2",
        repository=repository,
        task_service=_FakeTaskService(repository),
        report_publisher=reporter,
    )
    stack.worker_for = lambda actor_user_id: worker
    publisher = reporter
    return stack, publisher, worker


def _candidate() -> Candidate:
    approval = SimpleNamespace(
        approval_id="apr_hook_01",
        requested_by="user_operator",
        created_at=utc_now(),
        expires_at=utc_now() + timedelta(minutes=10),
    )
    return Candidate(
        task=_task(),
        approval=approval,  # type: ignore[arg-type]
        tool_name="mysql_lab_update_order_note",
        params={"order_id": 2001, "note": "hook"},
        step_id="step_hook_01",
    )


def _runner(stack: _Stack, candidates: list[Candidate]) -> WorkerRunner:
    return WorkerRunner(
        stack,  # type: ignore[arg-type]
        discover=lambda _stack, limit: list(candidates)[:limit],
        heartbeat_factory=_FakeHeartbeat,
    )


# ---------------------------------------------------------------------------
# 1) EXECUTED 触发
# ---------------------------------------------------------------------------


def test_executed_outcome_publishes_once_and_keeps_run_report_semantics() -> None:
    publisher = _FakePublisher()
    stack, _, worker = _stack(reporter=publisher)
    baseline_stack, _, baseline_worker = _stack(reporter=None)

    with_report = _runner(stack, [_candidate()]).run_once()
    baseline = _runner(baseline_stack, [_candidate()]).run_once()

    assert worker.submit_calls == baseline_worker.submit_calls == 1
    assert publisher.calls == [
        {
            "task_id": "task_hook_01",
            "tool_call_id": TOOL_CALL_ID,
            "succeeded": True,
        }
    ]
    assert with_report.reports_generated == 1
    assert with_report.report_failed == 0
    # 执行结论逐字相同：只有两个报告字段不同。
    assert with_report.executed == baseline.executed == 1
    assert with_report.denied == baseline.denied
    assert with_report.skipped == baseline.skipped
    assert with_report.failed == baseline.failed
    assert with_report.unknown_outcome == baseline.unknown_outcome
    assert with_report.clean == baseline.clean
    assert baseline.reports_generated == 0 and baseline.report_failed == 0
    expected = baseline.to_public_dict()
    expected.update({"reports_generated": 1, "report_failed": 0})
    assert with_report.to_public_dict() == expected


# ---------------------------------------------------------------------------
# 2) UNKNOWN / FAILED 触发
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "outcome_code", "succeeded"),
    [
        (ToolStatus.UNKNOWN_OUTCOME, OUTCOME_UNKNOWN, False),
        (ToolStatus.ERROR, OUTCOME_FAILED, False),
        (ToolStatus.SUCCESS, OUTCOME_EXECUTED, True),
    ],
)
def test_publish_fires_for_every_execution_fact(
    status: ToolStatus, outcome_code: str, succeeded: bool
) -> None:
    publisher = _FakePublisher()
    stack, _, _ = _stack(reporter=publisher, status=status)
    runner = _runner(stack, [])

    result = runner._execute(_candidate())  # noqa: SLF001 - 触发点就在此方法内

    assert result == outcome_code
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["succeeded"] is succeeded
    assert publisher.calls[0]["tool_call_id"] == TOOL_CALL_ID


# ---------------------------------------------------------------------------
# 3) SKIPPED / DENIED 不触发
# ---------------------------------------------------------------------------


def test_skipped_candidate_does_not_publish() -> None:
    publisher = _FakePublisher()
    stack, _, worker = _stack(reporter=publisher, cancel_requested=True)

    outcome = _runner(stack, [])._execute(_candidate())  # noqa: SLF001

    assert outcome == OUTCOME_SKIPPED
    assert worker.submit_calls == 0
    assert publisher.calls == []


def test_denied_outcome_does_not_publish() -> None:
    publisher = _FakePublisher()
    stack, _, _ = _stack(reporter=publisher, status=ToolStatus.DENIED)

    outcome = _runner(stack, [])._execute(_candidate())  # noqa: SLF001

    assert outcome == OUTCOME_DENIED
    assert publisher.calls == []


# ---------------------------------------------------------------------------
# 4) 未装配发布器：零行为变化
# ---------------------------------------------------------------------------


def test_missing_publisher_changes_nothing() -> None:
    baseline_stack, _, _ = _stack(reporter=None)
    runner = _runner(baseline_stack, [_candidate()])

    report = runner.run_once()

    assert report.executed == 1
    assert report.reports_generated == 0
    assert report.report_failed == 0
    assert report.clean is True
    assert runner._reporter is None  # noqa: SLF001 - 显式断言接缝为空


def test_stack_without_report_attribute_changes_nothing() -> None:
    """不含 `report_publisher` 属性的历史替身也必须零行为变化。"""
    stack, _, _ = _stack(reporter=None)
    del stack.report_publisher

    runner = _runner(stack, [_candidate()])
    report = runner.run_once()

    assert report.executed == 1
    assert report.reports_generated == 0
    assert report.report_failed == 0


# ---------------------------------------------------------------------------
# 5) 失败不阻塞
# ---------------------------------------------------------------------------


def test_publish_exception_is_counted_without_breaking_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    publisher = _FakePublisher(raises=RuntimeError("模拟发布器崩溃"))
    stack, _, _ = _stack(reporter=publisher)
    baseline_stack, _, _ = _stack(reporter=None)

    with caplog.at_level(logging.ERROR, logger="workers.runner"):
        report = _runner(stack, [_candidate()]).run_once()
    baseline = _runner(baseline_stack, [_candidate()]).run_once()

    assert len(publisher.calls) == 1
    assert report.report_failed == 1
    assert report.reports_generated == 0
    # `failed` / `clean` 与基线逐字相同：报告失败不污染执行结论。
    assert report.failed == baseline.failed
    assert report.clean == baseline.clean
    assert report.executed == baseline.executed == 1
    alerts = [
        getattr(record, "alert", None)
        for record in caplog.records
        if record.name == "workers.runner"
    ]
    assert ALERT_REPORT_FAILED in alerts


def test_publisher_reported_failure_is_counted_without_breaking_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    publisher = _FakePublisher(outcome=ReportOutcome(generated=False, failed=True))
    stack, _, _ = _stack(reporter=publisher)

    with caplog.at_level(logging.ERROR, logger="workers.runner"):
        report = _runner(stack, [_candidate()]).run_once()

    assert report.report_failed == 1
    assert report.reports_generated == 0
    assert report.clean is True
    alerts = [
        getattr(record, "alert", None)
        for record in caplog.records
        if record.name == "workers.runner"
    ]
    assert ALERT_REPORT_FAILED in alerts


def test_already_generated_outcome_counts_as_neither_success_nor_failure() -> None:
    publisher = _FakePublisher(
        outcome=ReportOutcome(generated=False, skipped_reason="ALREADY_GENERATED")
    )
    stack, _, _ = _stack(reporter=publisher)

    report = _runner(stack, [_candidate()]).run_once()

    assert len(publisher.calls) == 1
    assert report.reports_generated == 0
    assert report.report_failed == 0
    assert report.clean is True


# ---------------------------------------------------------------------------
# 6) `RunReport` 增量
# ---------------------------------------------------------------------------


def test_run_report_new_fields_do_not_touch_clean() -> None:
    baseline = RunReport()
    assert baseline.clean is True
    assert baseline.to_public_dict()["reports_generated"] == 0
    assert baseline.to_public_dict()["report_failed"] == 0
    # 报告失败不清零干净度（主理人裁定：不并入 `failed`）。
    assert RunReport(report_failed=9).clean is True
    assert RunReport(reports_generated=9).clean is True


def test_run_report_merged_adds_both_report_counters() -> None:
    left = RunReport(reports_generated=2, report_failed=1)
    right = RunReport(reports_generated=3, report_failed=4)

    merged = left.merged(right)

    assert merged.reports_generated == 5
    assert merged.report_failed == 5
    # 既有字段语义不变。
    assert merged.clean is True


def test_run_report_public_dict_exposes_both_counters() -> None:
    payload = RunReport(reports_generated=4, report_failed=2).to_public_dict()

    assert payload["reports_generated"] == 4
    assert payload["report_failed"] == 2
    assert payload["clean"] is True
    assert set(payload) >= {
        "cycles",
        "discovered",
        "executed",
        "denied",
        "skipped",
        "failed",
        "unknown_outcome",
        "reconciled",
        "reconcile_failed",
        "reconcile_scanned_total",
        "reports_generated",
        "report_failed",
        "clean",
    }
