"""W10 T02 单测：RunManifest（AC-01/02）、Runner 只读装配（AC-05）、
不编造指标、状态不可回退、冻结清单确定性、惰性 import 打包安全（R-13）。

零 DB、零网络；冻结资产只读；不放宽断言、不加 skip/xfail。
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.manifest import (
    DEFAULT_BASELINE_DIR,
    FROZEN_VERSION_FIELDS,
    RunManifest,
    RunStatus,
)
from core.benchmark.runner import (
    ADAPTER_READ_TOOLS,
    BenchmarkRunner,
    RegistryToolObserver,
    RunResult,
    derive_run_id,
    ensure_run_transition,
    load_suite,
    plan_cases,
)
from core.errors import BenchmarkInvalidError
from core.security.canonical import canonical_digest
from core.tools.contracts import ToolDefinition
from core.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = ROOT / "benchmarks" / "baseline_v0"
SUITE_PATH = ROOT / "benchmarks" / "w10" / "suite_manifest.json"


class _Binding:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition


class _FakeOracle:
    @property
    def records(self) -> tuple[Any, ...]:
        return ()

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any:
        raise AssertionError("单测不应读取 Oracle 真值")


class _FakeObserver:
    def observe(self, tool_plan: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(f"{name}@1.0" for name in tool_plan)


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"只读观测工具 {name}",
        version="1.0",
        permissions=["lab:read"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect="read",
    )


class _WriteProbeDefinition:
    """只暴露守卫需要的两个属性，避免构造 W07 写工具契约的完整声明集。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.side_effect = "write"


class _WriteProbeRegistry:
    def definitions(self) -> list[Any]:
        return [_WriteProbeDefinition("lab_write_probe")]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ADAPTER_READ_TOOLS:
        registry.register(_Binding(_definition(name)))
    return registry


def _manifest(**overrides: Any) -> RunManifest:
    return RunManifest.from_baseline(suite_version="suite-v1", **overrides)


def _make_runner(registry: ToolRegistry, tmp_path: Path) -> BenchmarkRunner:
    return BenchmarkRunner(
        manifest=_manifest(),
        registry=registry,
        observer=_FakeObserver(),
        oracle=_FakeOracle(),
        artifact_dir=tmp_path / "artifacts",
    )


# --------------------------------------------------------------------------- #
# AC-01 / AC-02：Manifest 完整性与封闭性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["seed", "env_image_digest", "budget"])
def test_ac01_missing_required_field_raises_validation_error(missing: str) -> None:
    payload = _manifest().model_dump(mode="json")
    payload.pop(missing)
    with pytest.raises(ValidationError) as caught:
        RunManifest.model_validate(payload)
    assert missing in str(caught.value)


def test_ac01_from_baseline_versions_equal_frozen_assets() -> None:
    baseline = json.loads(
        (BASELINE_DIR / "baseline_manifest.json").read_text(encoding="utf-8")
    )
    inference = json.loads(
        (BASELINE_DIR / "inference_config.json").read_text(encoding="utf-8")
    )
    manifest = _manifest()
    for field in FROZEN_VERSION_FIELDS:
        assert getattr(manifest, field) == baseline[field], field
    assert manifest.model_name == baseline["model_name"] == "Qwen3-8B"
    assert manifest.model_version is None  # baseline_manifest.model_commit = null
    assert manifest.tokenizer_version is None
    assert manifest.schema_version == baseline["tool_schema_version"]
    assert manifest.manifest_version == "w10-run-v1"
    assert manifest.suite_version == "suite-v1"
    assert manifest.status is RunStatus.NOT_RUN
    assert manifest.seed == inference["decoding"]["seed"] == 42
    assert manifest.budget.max_tool_calls_per_case == inference["tool_use"]["max_tool_calls"] == 16
    # max_cases 取自冻结测试集真实计划 episode 数（6 × 20 × 3）。
    assert manifest.budget.max_cases == 360
    assert manifest.budget.max_wall_s > 0
    assert manifest.env_image_digest == "UNKNOWN"
    assert manifest.env_image_digest_source == "UNKNOWN"


def test_ac01_from_baseline_respects_explicit_digest_and_overrides() -> None:
    manifest = _manifest(
        env_image_digest="sha256:abcdef", notes=("定向验收",), seed=7
    )
    assert manifest.env_image_digest == "sha256:abcdef"
    assert manifest.env_image_digest_source == "DOCKER_INSPECT"
    assert manifest.seed == 7
    assert manifest.notes == ("定向验收",)
    # 版本字段仍逐字取自冻结资产，覆盖 seed 不会连带改动版本。
    baseline = json.loads(
        (BASELINE_DIR / "baseline_manifest.json").read_text(encoding="utf-8")
    )
    for field in FROZEN_VERSION_FIELDS:
        assert getattr(manifest, field) == baseline[field], field


def test_ac01_from_baseline_rejects_conflicting_frozen_assets(tmp_path: Path) -> None:
    for name in (
        "baseline_manifest.json",
        "inference_config.json",
        "tool_schema.json",
        "testset_manifest.json",
    ):
        (tmp_path / name).write_bytes((BASELINE_DIR / name).read_bytes())
    tool_schema = json.loads((tmp_path / "tool_schema.json").read_text(encoding="utf-8"))
    tool_schema["tool_schema_version"] = "tool-schema-FORGED"
    (tmp_path / "tool_schema.json").write_text(
        json.dumps(tool_schema, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(BenchmarkInvalidError):
        RunManifest.from_baseline(tmp_path, suite_version="suite-v1")


def test_ac02_extra_fields_are_forbidden() -> None:
    payload = _manifest().model_dump(mode="json")
    payload["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        RunManifest.model_validate(payload)


def test_manifest_time_order_validator() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    good = _manifest(started_at=start, finished_at=start + timedelta(seconds=1))
    assert good.finished_at is not None
    with pytest.raises(ValidationError):
        _manifest(started_at=start, finished_at=start - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _manifest(finished_at=start)
    with pytest.raises(ValidationError):
        _manifest(started_at=datetime(2026, 1, 1))  # naive → 拒绝


# --------------------------------------------------------------------------- #
# AC-05：Runner 只读
# --------------------------------------------------------------------------- #


def test_ac05_constructor_signature_has_no_gateway_approval_or_worker() -> None:
    signature = inspect.signature(BenchmarkRunner.__init__)
    text = f"{signature} " + " ".join(
        str(parameter.annotation) for parameter in signature.parameters.values()
    )
    for forbidden in ("ToolGateway", "ApprovalService", "Worker", "apps."):
        assert forbidden not in text, forbidden
    # P1-T2 追加两个参数：`case_executor`（组合根注入的 AGENT 执行器，runner 只委托）
    # 与 `execution_mode`（HARNESS/AGENT 显式声明）。**仍然**不出现网关/审批/Worker。
    assert set(signature.parameters) == {
        "self",
        "manifest",
        "registry",
        "observer",
        "oracle",
        "artifact_dir",
        "injector",
        "resetter",
        "audit",
        "clock",
        "case_executor",
        "execution_mode",
        "blocker_resolver",
    }


def test_ac05_write_tool_registry_is_rejected(tmp_path: Path) -> None:
    # 等价断言（设计 D-02：registry.write_tools() 不存在）。
    assert [
        item for item in _registry().definitions() if item.side_effect == "write"
    ] == []
    with pytest.raises(BenchmarkInvalidError) as caught:
        BenchmarkRunner(
            manifest=_manifest(),
            registry=_WriteProbeRegistry(),  # type: ignore[arg-type]
            observer=_FakeObserver(),
            oracle=_FakeOracle(),
            artifact_dir=tmp_path / "artifacts",
        )
    assert "lab_write_probe" in json.dumps(caught.value.details, ensure_ascii=False)


def test_ac05_readonly_registry_assembles_and_observes_seven_tools(
    tmp_path: Path,
) -> None:
    registry = _registry()
    runner = _make_runner(registry, tmp_path)
    assert runner.status is RunStatus.NOT_RUN
    observer = RegistryToolObserver(registry)
    sequence = observer.observe(ADAPTER_READ_TOOLS)
    assert sequence == tuple(f"{name}@1.0" for name in ADAPTER_READ_TOOLS)
    assert len(sequence) == 7
    assert canonical_digest(list(sequence)) == canonical_digest(list(sequence))
    assert len(ADAPTER_READ_TOOLS) == 7


# --------------------------------------------------------------------------- #
# 惰性 import（打包安全，R-13）
# --------------------------------------------------------------------------- #


def test_import_runner_does_not_trigger_benchmarks_package_import() -> None:
    """子进程 sys.modules 快照：import core.benchmark.runner 不得带入 benchmarks.*。"""

    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import core.benchmark.runner\n"
        "after = set(sys.modules)\n"
        "hits = sorted(m for m in (after - before) "
        "if m == 'benchmarks' or m.startswith('benchmarks.'))\n"
        "assert hits == [], hits\n"
        "print('LAZY_IMPORT_OK')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "LAZY_IMPORT_OK" in completed.stdout, completed.stderr


def test_runner_source_has_no_top_level_benchmarks_import() -> None:
    source = (ROOT / "core" / "benchmark" / "runner.py").read_text(encoding="utf-8")
    assert "from benchmarks." not in source.split("def _default_injector")[0]
    assert "import benchmarks" not in source.split("def _default_injector")[0]
    # 惰性解析点必须在函数体内，且 3 个接缝都出现。
    for symbol in ("inject", "reset", "verify_assertions"):
        assert symbol in source


# --------------------------------------------------------------------------- #
# 不编造指标 / 状态不可回退
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [RunStatus.NOT_RUN, RunStatus.RUNNING, RunStatus.BLOCKED]
)
def test_run_result_scores_and_metrics_are_none_before_terminal(
    status: RunStatus,
) -> None:
    result = RunResult(run_id="run_probe", status=status)
    assert result.scores is None
    assert result.metrics is None
    for field in ("scores", "metrics"):
        with pytest.raises(ValidationError):
            RunResult(run_id="run_probe", status=status, **{field: {"x": 0.0}})
        with pytest.raises(ValidationError):
            RunResult(run_id="run_probe", status=status, **{field: {}})


def test_run_status_transitions_cannot_go_backwards() -> None:
    ensure_run_transition(RunStatus.NOT_RUN, RunStatus.RUNNING)
    for target in (
        RunStatus.COMPLETED,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
    ):
        ensure_run_transition(RunStatus.RUNNING, target)
    for terminal in (
        RunStatus.COMPLETED,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
    ):
        with pytest.raises(BenchmarkInvalidError):
            ensure_run_transition(terminal, RunStatus.RUNNING)
    with pytest.raises(BenchmarkInvalidError):
        ensure_run_transition(RunStatus.RUNNING, RunStatus.NOT_RUN)


# --------------------------------------------------------------------------- #
# 冻结清单与确定性
# --------------------------------------------------------------------------- #


def test_suite_manifest_and_plan_are_deterministic() -> None:
    suite = load_suite(SUITE_PATH)
    assert suite.suite_version == "suite-v1"
    assert suite.testset_version == "testset-v0-preview"
    assert suite.tool_plan == ADAPTER_READ_TOOLS
    assert len(suite.cases) == 6
    assert sum(1 for case in suite.cases if case.env_blocked_reason == "ENV-PG5432") == 3
    first = plan_cases(suite)
    second = plan_cases(load_suite(SUITE_PATH))
    assert first == second
    assert len(first) == 18
    assert [item.repetition for item in first[:3]] == [1, 2, 3]
    assert len({item.fault_seed for item in first}) == 6
    assert all(item.tool_plan == ADAPTER_READ_TOOLS for item in first)


def test_derive_run_id_is_content_addressed() -> None:
    manifest = _manifest()
    other = _manifest(seed=43)
    assert derive_run_id(manifest) == derive_run_id(_manifest())
    assert derive_run_id(manifest).startswith("run_")
    assert derive_run_id(manifest) != derive_run_id(other)


def test_plan_cli_payload_is_byte_deterministic() -> None:
    from core.benchmark import __main__ as cli

    first = cli._plan_payload(SUITE_PATH)
    second = cli._plan_payload(SUITE_PATH)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
        second, sort_keys=True, ensure_ascii=False
    )
    assert first["case_count"] == 18
    assert first["seed"] == 42
    assert first["model_version"] is None
    assert first["execution_mode"] == "HARNESS"
    assert first["plan_digest"] == canonical_digest(
        [
            [
                item.case_id,
                item.engine,
                item.scenario,
                item.fault_seed,
                item.repetition,
                list(item.tool_plan),
            ]
            for item in plan_cases(load_suite(SUITE_PATH))
        ]
    )
    # 不含运行期时间戳：两跑逐字节相等的前提。
    text = json.dumps(first, ensure_ascii=False)
    for forbidden in ("started_at", "finished_at", "timestamp", "created_at"):
        assert forbidden not in text


def test_default_baseline_dir_points_at_frozen_assets() -> None:
    assert DEFAULT_BASELINE_DIR == BASELINE_DIR
    assert (DEFAULT_BASELINE_DIR / "results.json").read_text(encoding="utf-8")
