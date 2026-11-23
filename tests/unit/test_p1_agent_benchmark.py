"""P1-T2 单元测试：BenchmarkRunner 的 AGENT 模式分支（不依赖真实模型）。

覆盖：

1. **AGENT 必须注入执行器**：`execution_mode="AGENT"` 且无 `case_executor` → 装配期拒绝，
   避免出现「声明 AGENT 但没跑模型」的假结果；
2. **case 字段映射**：执行器 outcome → `CaseResult`（task_status / tool_sequence /
   rca_match / abstained / latency / cost_units / attribution）；
3. **Oracle 隔离纪律**：Oracle 拒绝时 `rca_match=None` 且 `failure_reason` 带拒绝码，
   不猜结论；环境阻塞 case **不调用**执行器、不读 Oracle；
4. **HARNESS 不变**：默认仍是 HARNESS，`rca_match`/`schema_valid` 恒 `None`。

用 tmp 目录内的最小 suite（1 个 MySQL case + 1 个 PG 阻塞 case），不连库、不注入真实故障。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.benchmark.agent_mode import AgentCaseOutcome
from core.benchmark.manifest import RunBudget, RunManifest
from core.benchmark.runner import ADAPTER_READ_TOOLS, BenchmarkRunner, RegistryToolObserver
from core.errors import BenchmarkInvalidError, OracleAccessDenied
from core.tasks.models import TaskStatus
from core.tools.contracts import ToolDefinition, ToolStatus
from core.tools.registry import ToolRegistry


class _Tool:
    """最小只读工具壳：提供 definition，供 RegistryToolObserver 读取版本。"""

    def __init__(self, name: str = "get_activity") -> None:
        self.definition = ToolDefinition(
            name=name,
            description="只读测试工具",
            risk_level="R0",
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ADAPTER_READ_TOOLS:
        registry.register(_Tool(name))
    return registry


def _suite(tmp_path: Path) -> Path:
    payload = {
        "manifest_version": "w10-suite-v1",
        "suite_version": "suite-v1",
        "testset_version": "testset-v0-preview",
        "harness_version": "44e4f9d",
        "repetitions_per_seed": 1,
        "tool_plan": list(ADAPTER_READ_TOOLS),
        "cases": [
            {
                "case_id": "mysql-lock_wait",
                "engine": "mysql",
                "scenario": "lock_wait",
                "scenario_id": "mysql_lock_wait",
                "fault_seed": 4103,
            },
            {
                "case_id": "pg-lock_wait",
                "engine": "postgresql",
                "scenario": "lock_wait",
                "scenario_id": "pg_lock_wait",
                "fault_seed": 4203,
                "env_blocked_reason": "ENV-PG5432",
            },
        ],
    }
    path = tmp_path / "suite_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _manifest() -> RunManifest:
    return RunManifest.from_baseline(
        suite_version="suite-v1",
        model_name="stub-model",
        model_version="stub/1.0",
        budget=RunBudget(max_cases=4, max_wall_s=60.0, max_tool_calls_per_case=4),
    )


class _Executor:
    def __init__(self, outcome: AgentCaseOutcome) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def run_case(
        self, *, case_id: str, repetition: int, run_id: str = "", engine: str = "mysql"
    ) -> AgentCaseOutcome:
        self.calls.append(
            {"case_id": case_id, "repetition": repetition, "run_id": run_id, "engine": engine}
        )
        return self._outcome


class _Oracle:
    def __init__(self, truth: Any = None, *, deny: bool = False) -> None:
        self._truth = truth
        self._deny = deny
        self.reads: list[tuple[str, str, str]] = []

    @property
    def records(self) -> tuple[Any, ...]:
        return ()

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any:
        self.reads.append((scenario_id, task_id, run_id))
        if self._deny:
            raise OracleAccessDenied("任务未到终态，拒绝读取真值")
        return self._truth


class _Truth:
    scenario_id = "mysql_lock_wait"


def _runner(
    tmp_path: Path,
    *,
    executor: Any | None,
    oracle: Any,
    mode: str = "AGENT",
) -> BenchmarkRunner:
    registry = _registry()
    runner = BenchmarkRunner(
        manifest=_manifest(),
        registry=registry,
        observer=RegistryToolObserver(registry),
        oracle=oracle,
        artifact_dir=tmp_path / "artifacts",
        injector=lambda scenario_id: type("I", (), {"injected": True})(),
        resetter=lambda scenario_id: None,
        case_executor=executor,
        execution_mode=mode,  # type: ignore[arg-type]
    )
    # 覆盖冻结清单路径：本用例用 tmp 内最小 suite（构造签名不允许传 suite path）。
    runner._suite_path = _suite(tmp_path)  # noqa: SLF001 - 测试注入
    return runner


def test_agent_mode_requires_case_executor(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkInvalidError):
        BenchmarkRunner(
            manifest=_manifest(),
            registry=_registry(),
            observer=RegistryToolObserver(_registry()),
            oracle=_Oracle(),
            artifact_dir=tmp_path / "a",
            execution_mode="AGENT",
        )


def test_agent_case_maps_outcome_and_matches_oracle(tmp_path: Path) -> None:
    outcome = AgentCaseOutcome(
        task_id="task_1",
        task_status=TaskStatus.COMPLETED,
        tool_sequence=("get_activity",),
        latency_ms=1234.5,
        total_tokens=900,
        schema_valid=True,
        ranked_root_causes=("lock_wait",),
        abstained=False,
        evidence_coverage=0.5,
        loop_stop_reason="planned_stop",
    )
    executor = _Executor(outcome)
    oracle = _Oracle(_Truth())
    runner = _runner(tmp_path, executor=executor, oracle=oracle)

    result = runner.run()

    assert result.execution_mode == "AGENT"
    assert result.status.value == "COMPLETED"
    real = [c for c in result.cases if not c.env_blocked]
    assert len(real) == 1
    case = real[0]
    assert case.execution_mode == "AGENT"
    assert case.task_status is TaskStatus.COMPLETED
    assert case.rca_match is True
    assert case.schema_valid is True
    assert case.cost_units == 900.0
    assert case.latency_ms == 1234.5
    # 环境阻塞 case 不调用执行器、不读 Oracle。
    assert executor.calls and executor.calls[0]["case_id"] == "mysql-lock_wait"
    assert all(read[0] == "mysql_lock_wait" for read in oracle.reads)


def test_agent_env_blocked_case_skips_executor_and_oracle(tmp_path: Path) -> None:
    executor = _Executor(AgentCaseOutcome())
    oracle = _Oracle(_Truth())
    runner = _runner(tmp_path, executor=executor, oracle=oracle)

    result = runner.run()

    blocked = [c for c in result.cases if c.env_blocked]
    assert blocked and blocked[0].excluded_reason == "ENV-PG5432"
    assert all(call["case_id"] != "pg-lock_wait" for call in executor.calls)
    assert all(read[0] != "pg_lock_wait" for read in oracle.reads)


def test_agent_oracle_refusal_is_recorded_not_guessed(tmp_path: Path) -> None:
    outcome = AgentCaseOutcome(
        task_id="task_1",
        task_status=TaskStatus.DIAGNOSING,
        tool_sequence=("get_activity",),
        schema_valid=True,
        ranked_root_causes=("lock_wait",),
        latency_ms=10.0,
        total_tokens=10,
    )
    runner = _runner(tmp_path, executor=_Executor(outcome), oracle=_Oracle(deny=True))

    result = runner.run()

    case = next(c for c in result.cases if not c.env_blocked)
    assert case.rca_match is None, "Oracle 拒绝时不得猜结论"
    assert case.failure_reason and "ORACLE_REFUSED" in case.failure_reason


def test_harness_mode_unchanged(tmp_path: Path) -> None:
    runner = _runner(tmp_path, executor=None, oracle=_Oracle(), mode="HARNESS")

    result = runner.run()

    assert result.execution_mode == "HARNESS"
    for case in result.cases:
        assert case.execution_mode == "HARNESS"
        assert case.rca_match is None
        assert case.schema_valid is None
