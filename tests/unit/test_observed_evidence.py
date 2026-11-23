"""W06 批次 1：执行后回执与结果未知事件映射的单元测试。

不连接数据库。用途：当本机管理员账号失效、集成测试数据残留导致
`find_unobserved(limit=50)` 被历史记录占满时，仍能对本次新增逻辑给出
可复现的机器证据（不受脏数据库影响）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.audit.events import AuditEventType
from core.errors import AuditUnavailableError
from core.tools.contracts import ToolResult, ToolSource, ToolStatus
from core.tools.executor import ToolExecutor

TOOL_NAME = "stub_r2"


class _FakeEvidenceRepository:
    """同时扮演 step_records 与 task repository；只记录调用，不碰数据库。"""

    def __init__(self, *, fail_observed: bool = False) -> None:
        self.observed: list[Any] = []
        self.events: list[Any] = []
        self.fail_observed = fail_observed

    def insert_observed(self, record: Any) -> None:
        if self.fail_observed:
            raise AuditUnavailableError("模拟执行后证据写入失败")
        self.observed.append(record)

    def insert_event(self, event: Any) -> None:
        self.events.append(event)


def _result(status: ToolStatus) -> ToolResult:
    return ToolResult(
        tool_name=TOOL_NAME,
        status=status,
        task_id="task_1",
        run_id="run_1",
        tool_call_id="call_1",
        started_at="2026-11-10T00:00:00.000Z",
        duration_ms=12.5,
        source=ToolSource(
            database_id="lab_mysql_01",
            db_version="8.0.46",
            collected_at="2026-11-10T00:00:00.000Z",
        ),
    )


def _definition() -> SimpleNamespace:
    # _observe_step / _emit_final 只访问这两个属性，无需构造完整 ToolRegistry。
    return SimpleNamespace(name=TOOL_NAME, audit_policy="metadata")


def _executor(repo: _FakeEvidenceRepository) -> ToolExecutor:
    return ToolExecutor(
        SimpleNamespace(),
        repo,
        step_records=repo,  # type: ignore[arg-type]
    )


def _observe(executor: ToolExecutor, result: ToolResult) -> ToolResult:
    return executor._observe_step(  # noqa: SLF001 - 被测目标就是该内部方法
        _definition(),
        result=result,
        task=SimpleNamespace(tenant_id="tenant_local"),
        task_id="task_1",
        run_id="run_1",
        call_id="call_1",
        step_id=None,
        started_at=datetime.now(timezone.utc),
    )


def test_success_write_leaves_observed_evidence() -> None:
    repo = _FakeEvidenceRepository()
    result = _observe(_executor(repo), _result(ToolStatus.SUCCESS))

    assert result.status is ToolStatus.SUCCESS
    assert len(repo.observed) == 1
    record = repo.observed[0]
    assert record.outcome == ToolStatus.SUCCESS.value
    assert record.tool_call_id == "call_1"
    assert record.tenant_id == "tenant_local"


def test_unknown_outcome_keeps_call_unobserved() -> None:
    repo = _FakeEvidenceRepository()
    result = _observe(_executor(repo), _result(ToolStatus.UNKNOWN_OUTCOME))

    # 结果未知不得伪造成"已观测"，必须留给 Reconciler 按未观测调用收敛。
    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert repo.observed == []


def test_observed_write_failure_degrades_to_unknown() -> None:
    repo = _FakeEvidenceRepository(fail_observed=True)
    result = _observe(_executor(repo), _result(ToolStatus.SUCCESS))

    assert repo.observed == []
    assert result.status is ToolStatus.UNKNOWN_OUTCOME
    assert any(
        warning.startswith("observed_evidence_unavailable:")
        for warning in result.warnings
    )


def test_unknown_outcome_is_audited_as_unknown_not_failed() -> None:
    repo = _FakeEvidenceRepository()
    executor = _executor(repo)
    executor._emit_final(  # noqa: SLF001 - 被测目标就是该内部方法
        _definition(),
        "task_1",
        "run_1",
        "call_1",
        _result(ToolStatus.UNKNOWN_OUTCOME),
        1,
    )

    assert len(repo.events) == 1
    event_type = repo.events[0].event_type
    value = getattr(event_type, "value", event_type)
    assert value == AuditEventType.TOOL_CALL_UNKNOWN_OUTCOME.value
    assert value != AuditEventType.TOOL_CALL_FAILED.value
