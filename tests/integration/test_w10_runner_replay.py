"""W10 T02 集成用例：AC-03 可重放 / AC-04 取消与失败 / AC-05 只读，
以及产物落盘、PG 环境阻塞（``ENV-PG5432``）与 Oracle 真值不入产物。

纪律：零 DB、零网络（注入/重置/观测/oracle 全部注入离线替身，产物写
``tmp_path``）；冻结资产只读；只追加、不清理、不 DELETE；不放宽断言、
不加 skip/xfail。控制库/PG 5432 未解不影响本文件判定。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from core.benchmark import runner as runner_module
from core.benchmark.manifest import RunManifest, RunStatus
from core.benchmark.runner import (
    ADAPTER_READ_TOOLS,
    BenchmarkRunner,
    InjectionState,
    RegistryToolObserver,
)
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
REAL_SUITE_PATH = ROOT / "benchmarks" / "w10" / "suite_manifest.json"


class _Binding:
    def __init__(self, definition: Any) -> None:
        self.definition = definition


class _FakeInjection:
    injected = True


class _FakeInjector:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, scenario_id: str) -> _FakeInjection:
        self.calls.append(scenario_id)
        return _FakeInjection()


class _FakeResetter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, scenario_id: str) -> None:
        self.calls.append(scenario_id)


class _BoomObserver:
    def observe(self, tool_plan: tuple[str, ...]) -> tuple[str, ...]:
        raise RuntimeError("observer boom")


class _NoReadOracle:
    @property
    def records(self) -> tuple[Any, ...]:
        return ()

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any:
        raise AssertionError("HARNESS 模式不应读取 Oracle 真值")


class _CancelToken:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return self._reason


def _real_registry() -> ToolRegistry:
    """适配器 7 个固定只读工具的**真实** ToolDefinition（不是 10 工具 observer 面）。"""

    from adapters.postgres_mcp.tools import TOOL_DEFINITIONS

    registry = ToolRegistry()
    for name in ADAPTER_READ_TOOLS:
        registry.register(_Binding(TOOL_DEFINITIONS[name]))
    return registry


def _case(
    case_id: str,
    engine: str,
    scenario: str,
    scenario_id: str,
    fault_seed: int,
    *,
    repetitions: int = 3,
    blocker: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": case_id,
        "engine": engine,
        "scenario": scenario,
        "scenario_id": scenario_id,
        "fault_seed": fault_seed,
        "repetitions": repetitions,
    }
    if blocker is not None:
        payload["env_blocked_reason"] = blocker
    return payload


def _write_suite(path: Path, cases: list[dict[str, Any]]) -> Path:
    payload = {
        "manifest_version": "w10-suite-v1",
        "suite_version": "suite-v1",
        "testset_version": "testset-v0-preview",
        "repetitions_per_seed": 3,
        "tool_plan": list(ADAPTER_READ_TOOLS),
        "cases": cases,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _runner(
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    suite_path: Path,
    *,
    observer: Any | None = None,
    injector: Any | None = None,
    resetter: Any | None = None,
) -> BenchmarkRunner:
    # runner 在 __init__ 时把模块级常量固化到 self._suite_path；
    # monkeypatch 必须在构造前完成（不新增构造参数，签名照抄任务卡）。
    monkeypatch.setattr(runner_module, "DEFAULT_SUITE_PATH", suite_path)
    registry = _real_registry()
    return BenchmarkRunner(
        manifest=RunManifest.from_baseline(suite_version="suite-v1"),
        registry=registry,
        observer=observer if observer is not None else RegistryToolObserver(registry),
        oracle=_NoReadOracle(),
        artifact_dir=artifact_dir,
        injector=injector if injector is not None else _FakeInjector(),
        resetter=resetter if resetter is not None else _FakeResetter(),
    )


# --------------------------------------------------------------------------- #
# AC-03 可重放
# --------------------------------------------------------------------------- #


def test_ac03_two_runs_have_single_valued_case_seed_and_digest_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(
        tmp_path / "suite.json",
        [_case("mysql-slow_query", "mysql", "slow_query", "mysql_slow_query", 4101)],
    )
    first_dir = tmp_path / "run1"
    second_dir = tmp_path / "run2"
    first = _runner(first_dir, monkeypatch, suite).run()
    second = _runner(second_dir, monkeypatch, suite).run()

    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.COMPLETED
    assert len(first.cases) == len(second.cases) == 3

    # 三个集合大小各 == 1（同一 Manifest、同一 seed、同一固定工具序列）。
    assert len({item.case_id for item in first.cases}) == 1
    assert len({item.fault_seed for item in first.cases}) == 1
    assert len({item.tool_sequence_digest for item in first.cases}) == 1
    # 两跑的对应集合各自相同（外层集合大小 == 1 = 可重放）。
    for field in ("case_id", "fault_seed", "tool_sequence_digest"):
        left = frozenset(getattr(item, field) for item in first.cases)
        right = frozenset(getattr(item, field) for item in second.cases)
        assert len({left, right}) == 1, field
    # 逐字段完全相等（比集合更严格）。
    assert [item.model_dump(mode="json") for item in first.cases] == [
        item.model_dump(mode="json") for item in second.cases
    ]

    case = first.cases[0]
    assert case.execution_mode == "HARNESS"
    assert case.tool_sequence == tuple(f"{name}@1.0" for name in ADAPTER_READ_TOOLS)
    assert len(case.tool_sequence) == 7
    assert case.injection_state is InjectionState.INJECTED
    assert case.rca_match is None  # HARNESS 模式模型侧指标未测得
    assert case.schema_valid is None
    assert first.scores is None and first.metrics is None
    assert first.run_id == second.run_id  # 内容寻址，同一 Manifest


def test_ac03_full_frozen_suite_replays_case_seed_and_digest_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _runner(tmp_path / "full1", monkeypatch, REAL_SUITE_PATH).run()
    second = _runner(tmp_path / "full2", monkeypatch, REAL_SUITE_PATH).run()
    assert first.status is RunStatus.COMPLETED
    assert len(first.cases) == 18
    for field in ("case_id", "fault_seed", "tool_sequence_digest"):
        left = frozenset(getattr(item, field) for item in first.cases)
        right = frozenset(getattr(item, field) for item in second.cases)
        assert len({left, right}) == 1, field
    assert [item.case_id for item in first.cases] == [
        item.case_id for item in second.cases
    ]


# --------------------------------------------------------------------------- #
# AC-04 取消与失败原因
# --------------------------------------------------------------------------- #


def test_ac04_cancel_sets_status_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(
        tmp_path / "suite.json",
        [_case("mysql-slow_query", "mysql", "slow_query", "mysql_slow_query", 4101)],
    )
    runner = _runner(tmp_path / "cancelled", monkeypatch, suite)
    runner.cancel(reason="定向取消：用户请求")
    result = runner.run()
    assert result.status is RunStatus.CANCELLED
    assert result.cancel_reason == "定向取消：用户请求"
    assert result.cases == ()
    assert result.scores is None and result.metrics is None


def test_ac04_cancel_token_sets_status_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(
        tmp_path / "suite.json",
        [_case("mysql-slow_query", "mysql", "slow_query", "mysql_slow_query", 4101)],
    )
    result = _runner(tmp_path / "token", monkeypatch, suite).run(
        cancel=_CancelToken("TOKEN_CANCEL")
    )
    assert result.status is RunStatus.CANCELLED
    assert result.cancel_reason == "TOKEN_CANCEL"


def test_ac04_case_exception_records_failure_reason_and_fails_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(
        tmp_path / "suite.json",
        [_case("mysql-slow_query", "mysql", "slow_query", "mysql_slow_query", 4101)],
    )
    result = _runner(
        tmp_path / "failed", monkeypatch, suite, observer=_BoomObserver()
    ).run()
    assert result.status is RunStatus.FAILED
    assert result.failure_reason and "observer boom" in result.failure_reason
    assert result.cases
    assert all(item.failure_reason for item in result.cases)


# --------------------------------------------------------------------------- #
# AC-05 只读
# --------------------------------------------------------------------------- #


def test_ac05_registry_has_no_write_tools_and_signature_is_readonly() -> None:
    registry = _real_registry()
    # 等价断言（设计 D-02：ToolRegistry 没有 write_tools()）。
    assert [item for item in registry.definitions() if item.side_effect == "write"] == []
    assert set(registry.names()) == set(ADAPTER_READ_TOOLS)
    assert len(registry.definitions()) == 7
    signature_text = str(inspect.signature(BenchmarkRunner.__init__))
    for forbidden in ("ToolGateway", "ApprovalService", "Worker", "apps."):
        assert forbidden not in signature_text


# --------------------------------------------------------------------------- #
# 产物 / PG 环境阻塞 / 真值隔离
# --------------------------------------------------------------------------- #


def test_artifacts_are_written_and_cases_jsonl_has_no_oracle_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _write_suite(
        tmp_path / "suite.json",
        [_case("mysql-slow_query", "mysql", "slow_query", "mysql_slow_query", 4101)],
    )
    artifact_dir = tmp_path / "artifacts"
    result = _runner(artifact_dir, monkeypatch, suite).run()
    for name in ("run_manifest.json", "cases.jsonl", "oracle_access.jsonl"):
        assert (artifact_dir / name).is_file(), name

    manifest_json = json.loads(
        (artifact_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_json["harness_version"] == "44e4f9d"
    assert manifest_json["model_version"] is None
    assert manifest_json["env_image_digest"] == "UNKNOWN"
    assert manifest_json["run_id"] == result.run_id
    assert manifest_json["run_status"] == "COMPLETED"
    assert manifest_json["testset_version"] == "testset-v0-preview"

    cases_text = (artifact_dir / "cases.jsonl").read_text(encoding="utf-8")
    lines = [line for line in cases_text.splitlines() if line.strip()]
    assert len(lines) == len(result.cases) == 3
    for forbidden in ("expected_root_cause", "injection_params", "truth_assertions"):
        assert forbidden not in cases_text, forbidden
    # 允许的公开定位字段与「只外泄一个比特」的 rca_match 仍在。
    assert '"fault_seed"' in cases_text
    assert '"rca_match"' in cases_text
    assert json.loads(lines[0])["rca_match"] is None

    oracle_text = (artifact_dir / "oracle_access.jsonl").read_text(encoding="utf-8")
    assert oracle_text == ""  # HARNESS 模式不读 Oracle，无决策记录


def test_pg_cases_are_env_blocked_and_never_judged_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    injector = _FakeInjector()
    result = _runner(
        tmp_path / "full", monkeypatch, REAL_SUITE_PATH, injector=injector
    ).run()
    assert result.status is RunStatus.COMPLETED  # 环境阻塞不判 FAIL
    assert result.failure_reason is None

    pg_cases = [item for item in result.cases if item.engine == "postgresql"]
    mysql_cases = [item for item in result.cases if item.engine == "mysql"]
    assert len(pg_cases) == 9 and len(mysql_cases) == 9
    for item in pg_cases:
        assert item.env_blocked is True
        assert item.excluded_reason == "ENV-PG5432"
        assert item.injection_state is InjectionState.UNKNOWN
        assert item.failure_reason is None
        assert item.tool_sequence == ()
    for item in mysql_cases:
        assert item.env_blocked is False
        assert item.excluded_reason is None
        assert item.injection_state is InjectionState.INJECTED
        assert len(item.tool_sequence) == 7
    # PG 场景从未调用注入器（不连不可达的 PG）。
    assert all("pg_" not in call for call in injector.calls)
