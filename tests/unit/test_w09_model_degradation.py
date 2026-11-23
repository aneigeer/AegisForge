"""W09 T05 / AC-16、AC-17、AC-18：模型降级单测（离线）。

覆盖：`disabled` 可构造（A9）、暂停新规划、规则简报（永不 CONFIRMED + 再脱敏）、
非法输出只重试 1 次（无第 3 次调用）、质量回归无已验证版本、事件顺序与结构断言。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from core.agent.loop import AgentLoop
from core.audit.events import AuditEventType
from core.errors import (
    ModelOutputInvalidError,
    ModelUnavailableError,
    RequestInvalidError,
)
from core.evidence.models import CollectionStatus, Evidence, EvidenceQuality
from core.models.contracts import ModelMessage, ModelResponse, ModelUsage
from core.models.degradation import (
    DegradeReason,
    DegradationController,
    DisabledModelGateway,
    ModelState,
    ModelVersionPin,
)
from core.models.factory import build_model_gateway
from core.models.gateway import ModelGateway
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.report.brief import DegradedBrief, build_rule_brief
from core.report.models import FactStatus
from core.tasks.models import Task, TaskStatus, utc_now

ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = ROOT / "benchmarks" / "baseline_v0" / "baseline_manifest.json"
_TENANT = "tenant_local"


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        self.events.append(event)
        return event

    @property
    def types(self) -> list[str]:
        return [event.event_type for event in self.events]


def _settings(provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        model_provider=provider,
        model_name="fake-diagnostic-1",
        model_timeout_s=5,
        model_max_tokens=100,
        model_base_url=None,
        model_api_key=None,
    )


def _task(task_id: str = "task_w09_t05", run_id: str = "run_w09_t05") -> Task:
    now = utc_now()
    return Task(
        task_id=task_id,
        run_id=run_id,
        goal="W09 T05 降级",
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
        tenant_id=_TENANT,
        environment_id="env_mysql_local",
    )


def _evidence(evidence_id: str = "ev_w09_t05") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        tenant_id=_TENANT,
        task_id="task_w09_t05",
        environment_id="env_mysql_local",
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-t05",
        source_tool="get_locks",
        observed_at="2026-06-01T00:00:00+00:00",
        collection_status=CollectionStatus.SUCCESS,
        quality=EvidenceQuality.HIGH,
        payload_hash="a" * 64,
    )


# --------------------------------------------------------------------------- #
# A9：disabled 可构造
# --------------------------------------------------------------------------- #


def test_disabled_provider_returns_gateway_that_raises_model_unavailable() -> None:
    gateway = build_model_gateway(_settings("disabled"))
    assert isinstance(gateway, DisabledModelGateway)
    with pytest.raises(ModelUnavailableError):
        gateway.generate([ModelMessage(role="user", content="ping")])
    with pytest.raises(ModelUnavailableError):
        gateway.generate_structured([ModelMessage(role="user", content="ping")], object)
    with pytest.raises(ModelUnavailableError):
        gateway.tool_call([], registry=None)


# --------------------------------------------------------------------------- #
# 状态机与质量回归
# --------------------------------------------------------------------------- #


def test_controller_initial_state_follows_provider() -> None:
    disabled = DegradationController(audit=_FakeAudit(), provider="disabled")
    assert disabled.state is ModelState.SUSPENDED
    assert disabled.planning_suspended is True
    assert disabled.last_reason is DegradeReason.MODEL_DISABLED

    fake = DegradationController(audit=_FakeAudit(), provider="fake")
    assert fake.state is ModelState.AVAILABLE
    assert fake.planning_suspended is False
    assert fake.last_reason is None

    from_settings = DegradationController.from_settings(
        _settings("disabled"), audit=_FakeAudit()
    )
    assert from_settings.state is ModelState.SUSPENDED


def test_model_version_pin_rejects_null_or_blocked_commit(tmp_path: pathlib.Path) -> None:
    current = ModelVersionPin(manifest_path=_MANIFEST, current_version="")
    assert current.previous_verified() is None

    frozen = tmp_path / "frozen.json"
    frozen.write_text(
        json.dumps({"model_commit": "abc123", "status": "FROZEN"}), encoding="utf-8"
    )
    assert (
        ModelVersionPin(manifest_path=frozen, current_version="").previous_verified()
        == "abc123"
    )
    assert (
        ModelVersionPin(
            manifest_path=frozen, current_version="abc123"
        ).previous_verified()
        is None
    )

    blocked = tmp_path / "blocked.json"
    blocked.write_text(
        json.dumps({"model_commit": "abc123", "status": "BLOCKED_MODEL_ENV_MISSING"}),
        encoding="utf-8",
    )
    assert (
        ModelVersionPin(manifest_path=blocked, current_version="").previous_verified()
        is None
    )

    null_commit = tmp_path / "null.json"
    null_commit.write_text(
        json.dumps({"model_commit": None, "status": "OK"}), encoding="utf-8"
    )
    assert (
        ModelVersionPin(
            manifest_path=null_commit, current_version=""
        ).previous_verified()
        is None
    )


def test_quality_regression_without_verified_version_stays_suspended() -> None:
    audit = _FakeAudit()
    controller = DegradationController(
        audit=audit,
        provider="fake",
        pin=ModelVersionPin(manifest_path=_MANIFEST, current_version=""),
    )
    controller.record_failure(
        DegradeReason.QUALITY_REGRESSION,
        task_id="task_w09_t05",
        run_id="run_w09_t05",
    )
    assert controller.state is ModelState.SUSPENDED
    assert len(audit.events) == 1
    assert audit.events[0].event_type == AuditEventType.MODEL_DEGRADED.value
    payload = audit.events[0].payload
    assert payload["reason"] == DegradeReason.QUALITY_REGRESSION_NO_VERIFIED_VERSION.value
    assert payload["planning_suspended"] is True
    assert payload["model_used"] is False
    assert payload["retry_count"] == 0
    assert payload["model_version"] is None
    assert payload["pin_target"] is None


def test_quality_regression_routes_to_verified_version_when_available(
    tmp_path: pathlib.Path,
) -> None:
    manifest = tmp_path / "frozen.json"
    manifest.write_text(
        json.dumps({"model_commit": "abc123", "status": "FROZEN"}), encoding="utf-8"
    )
    audit = _FakeAudit()
    controller = DegradationController(
        audit=audit,
        provider="fake",
        pin=ModelVersionPin(manifest_path=manifest, current_version=""),
    )
    controller.record_failure(
        DegradeReason.QUALITY_REGRESSION, task_id="task_w09_t05", run_id="run_w09_t05"
    )
    payload = audit.events[0].payload
    assert payload["reason"] == DegradeReason.QUALITY_REGRESSION.value
    assert payload["pin_target"] == "abc123"


@pytest.mark.parametrize(
    "reason",
    [
        DegradeReason.MODEL_DISABLED,
        DegradeReason.MODEL_TIMEOUT,
        DegradeReason.MODEL_CALL_FAILED,
        DegradeReason.MODEL_OUTPUT_INVALID,
    ],
)
def test_availability_failures_suspend_and_emit_model_degraded(
    reason: DegradeReason,
) -> None:
    audit = _FakeAudit()
    controller = DegradationController(audit=audit, provider="fake")
    controller.record_failure(reason, task_id="task_w09_t05", run_id="run_w09_t05")
    assert controller.state is ModelState.SUSPENDED
    assert controller.planning_suspended is True
    assert audit.types == [AuditEventType.MODEL_DEGRADED.value]
    payload = audit.events[0].payload
    assert payload["reason"] == reason.value
    assert payload["planning_suspended"] is True
    assert payload["model_used"] is False
    assert payload["pin_target"] is None


# --------------------------------------------------------------------------- #
# AC-17：非法输出只重试 1 次
# --------------------------------------------------------------------------- #


def test_repair_attempt_retries_once_then_stops_with_event_order() -> None:
    audit = _FakeAudit()
    controller = DegradationController(audit=audit, provider="fake")

    assert (
        controller.repair_attempt(1, task_id="task_w09_t05", run_id="run_w09_t05")
        == "RETRY"
    )
    assert controller.retry_count == 1
    assert audit.types == [AuditEventType.AGENT_RECOVERY_STARTED.value]

    assert (
        controller.repair_attempt(2, task_id="task_w09_t05", run_id="run_w09_t05")
        == "STOP"
    )
    assert controller.state is ModelState.SUSPENDED
    assert audit.types == [
        AuditEventType.AGENT_RECOVERY_STARTED.value,
        AuditEventType.AGENT_RECOVERY_STOPPED.value,
        AuditEventType.MODEL_DEGRADED.value,
    ]
    stopped = audit.events[1].payload
    assert stopped["outcome"] == "STOP_INVALID_OUTPUT"
    assert stopped["reason"] == DegradeReason.MODEL_OUTPUT_INVALID.value
    assert stopped["retry_count"] == 1
    assert stopped["tool_calls_delta"] == 0
    degraded = audit.events[2].payload
    assert degraded["reason"] == DegradeReason.MODEL_OUTPUT_INVALID.value
    assert degraded["planning_suspended"] is True
    assert degraded["model_used"] is False

    # 即使被误调第 3 次，也不新增任何事件（重试/降级各只一次）。
    assert (
        controller.repair_attempt(3, task_id="task_w09_t05", run_id="run_w09_t05")
        == "STOP"
    )
    assert len(audit.events) == 3


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class _InvalidJsonAdapter:
    provider = "fake"
    adapter_version = "v1"
    model_name = "fake-invalid"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: Any) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="not-json",
            model_version="fake-invalid/v1",
            provider="fake",
            adapter_version="v1",
            usage=ModelUsage(),
            latency_ms=1.0,
        )


def test_no_third_adapter_call_when_output_stays_invalid() -> None:
    adapter = _InvalidJsonAdapter()
    gateway = ModelGateway(adapter, timeout_s=5)
    audit = _FakeAudit()
    controller = DegradationController(audit=audit, provider="fake")

    attempt = 0
    while True:
        attempt += 1
        with pytest.raises(ModelOutputInvalidError):
            gateway.generate_structured(
                [ModelMessage(role="user", content="ping")], _Output
            )
        action = controller.repair_attempt(
            attempt, task_id="task_w09_t05", run_id="run_w09_t05"
        )
        if action == "STOP":
            break

    assert adapter.calls == 2
    assert controller.retry_count == 1
    assert controller.state is ModelState.SUSPENDED
    assert audit.types == [
        AuditEventType.AGENT_RECOVERY_STARTED.value,
        AuditEventType.AGENT_RECOVERY_STOPPED.value,
        AuditEventType.MODEL_DEGRADED.value,
    ]


def test_repair_retry_cap_cannot_be_raised_above_one() -> None:
    with pytest.raises(RequestInvalidError):
        DegradationController(
            audit=_FakeAudit(), provider="fake", max_repair_retries=2
        )
    controller = DegradationController(
        audit=_FakeAudit(), provider="fake", max_repair_retries=1
    )
    assert controller.max_repair_retries == 1


# --------------------------------------------------------------------------- #
# resume 只认非空的已验证版本
# --------------------------------------------------------------------------- #


def test_resume_requires_non_empty_verified_version() -> None:
    controller = DegradationController(audit=_FakeAudit(), provider="disabled")
    assert controller.resume(None) is False
    assert controller.resume("   ") is False
    assert controller.resume("v1") is True
    assert controller.state is ModelState.AVAILABLE

    pinned = DegradationController(
        audit=_FakeAudit(),
        provider="disabled",
        pin=ModelVersionPin(manifest_path=_MANIFEST, current_version=""),
    )
    assert pinned.resume("v1") is False
    assert pinned.state is ModelState.SUSPENDED


# --------------------------------------------------------------------------- #
# AC-16 / AC-18：规则简报与结构断言
# --------------------------------------------------------------------------- #


def test_rule_brief_never_confirmed_and_redacts_observations() -> None:
    task = _task()
    gate = RedactionGate(DataRedactionEngine())
    brief = build_rule_brief(
        task=task,
        evidence=[_evidence()],
        summaries=[
            "锁等待超时，疑似长事务；联系电话 13812345678",
            "索引方案已修复",
        ],
        reason=DegradeReason.MODEL_DISABLED,
        gate=gate,
    )
    assert brief.degraded is True
    assert brief.model_used is False
    assert brief.planning_suspended is True
    assert brief.fact_status in (FactStatus.UNKNOWN, FactStatus.PARTIAL)
    assert brief.fact_status is FactStatus.PARTIAL
    assert brief.evidence_ids == ("ev_w09_t05",)
    joined = "".join(brief.observations)
    assert "13812345678" not in joined
    assert "已修复" not in joined
    assert brief.redaction is not None

    empty = build_rule_brief(
        task=task,
        evidence=[],
        summaries=[],
        reason=DegradeReason.MODEL_DISABLED,
        gate=gate,
    )
    assert empty.fact_status is FactStatus.UNKNOWN
    assert empty.observations == ()
    assert empty.redaction is None


def test_degraded_brief_rejects_confirmed_and_completion_wording() -> None:
    base: dict[str, Any] = {
        "task_id": "task_w09_t05",
        "degrade_reason": DegradeReason.MODEL_DISABLED,
        "fact_status": FactStatus.UNKNOWN,
    }
    assert DegradedBrief(**base).fact_status is FactStatus.UNKNOWN
    with pytest.raises(ValidationError):
        DegradedBrief(**{**base, "fact_status": FactStatus.CONFIRMED})
    with pytest.raises(ValidationError):
        DegradedBrief(**{**base, "observations": ("已修复索引",)})
    with pytest.raises(ValidationError):
        DegradedBrief(**{**base, "model_used": True})


def test_signatures_have_no_credential_params_and_write_default_unchanged() -> None:
    forbidden = {"ro_dsn", "rw_dsn", "control_dsn", "pg_ro_dsn"}
    import inspect

    for target in (
        DegradationController.__init__,
        DegradationController.from_settings,
        DegradationController.record_failure,
        DegradationController.repair_attempt,
        build_rule_brief,
    ):
        assert set(inspect.signature(target).parameters).isdisjoint(forbidden), target
    assert (
        inspect.signature(ModelGateway.tool_call).parameters["allow_write"].default
        is False
    )


# --------------------------------------------------------------------------- #
# AgentLoop：暂停新规划（planner / tool_call 都不调用）
# --------------------------------------------------------------------------- #


class _NeverPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def create_plan(self, bundle: Any) -> Any:
        self.calls += 1
        raise AssertionError("降级路径不得调用 planner")

    def assess(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("降级路径不得调用 planner")

    def propose_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("降级路径不得调用 planner")

    def validate_step(self, step: Any) -> None:
        raise AssertionError("降级路径不得调用 planner")


class _FakeService:
    def __init__(self, task: Task) -> None:
        self._task = task
        self.calls: list[tuple[str, Any]] = []

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        self.calls.append(("get_task", task_id))
        return self._task

    def transition(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("transition", args))
        raise AssertionError("降级路径不得推进任务状态")

    def run_tool(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("run_tool", args))
        raise AssertionError("降级路径不得调用工具")


def test_agent_loop_returns_degraded_brief_before_planner() -> None:
    task = _task()
    service = _FakeService(task)
    planner = _NeverPlanner()
    audit = _FakeAudit()
    controller = DegradationController(audit=audit, provider="disabled")
    loop = AgentLoop(
        service,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        None,
        planner,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        context_factory=lambda task_id: None,
        degradation=controller,
        redaction_gate=RedactionGate(DataRedactionEngine()),
    )

    result = loop.run(task.task_id)

    assert isinstance(result, DegradedBrief)
    assert result.degraded is True
    assert result.model_used is False
    assert result.planning_suspended is True
    assert result.fact_status in (FactStatus.UNKNOWN, FactStatus.PARTIAL)
    assert planner.calls == 0
    assert service.calls == [("get_task", task.task_id)]
    assert audit.types == [AuditEventType.MODEL_DEGRADED.value]
    payload = audit.events[0].payload
    assert payload["reason"] == DegradeReason.MODEL_DISABLED.value
    assert payload["model_used"] is False
