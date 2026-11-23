"""W04 Baseline v0 冻结资产校验。"""

from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = ROOT / "benchmarks" / "baseline_v0"

FIXED_TOOL_NAMES = {
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
}
ENGINES = {"mysql", "postgresql"}
SCENARIOS = {"slow_query", "connection_pressure", "lock_wait"}
REQUIRED_MANIFEST_FIELDS = {
    "model_name",
    "model_commit",
    "tokenizer_commit",
    "prompt_version",
    "tool_schema_version",
    "harness_version",
    "inference_config_version",
    "testset_version",
    "created_at",
    "status",
    "notes",
}


def _load_runner() -> ModuleType:
    path = BASELINE_DIR / "run_baseline.py"
    spec = importlib.util.spec_from_file_location("baseline_v0_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load_runner()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((BASELINE_DIR / "baseline_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def results() -> dict:
    return json.loads((BASELINE_DIR / "results.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tool_schema() -> dict:
    return json.loads((BASELINE_DIR / "tool_schema.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def testset() -> dict:
    return json.loads((BASELINE_DIR / "testset_manifest.json").read_text(encoding="utf-8"))


def test_manifest_required_fields_and_frozen_status(manifest: dict) -> None:
    assert REQUIRED_MANIFEST_FIELDS.issubset(manifest)
    assert manifest["status"] == "BLOCKED_MODEL_ENV_MISSING"
    assert manifest["model_name"] == "Qwen3-8B"
    assert manifest["model_commit"] is None
    assert manifest["tokenizer_commit"] is None
    assert manifest["harness_version"] == "44e4f9d"
    assert manifest["harness_commit"] == "44e4f9d"
    assert manifest["created_at"]
    assert manifest["notes"]


def test_manifest_contains_no_metric_scores(manifest: dict) -> None:
    lowered = json.dumps(manifest, ensure_ascii=False).lower()
    for metric in ("accuracy", '"f1"', "latency", "precision", "recall"):
        assert metric not in lowered


def test_results_placeholder_is_not_run(results: dict) -> None:
    assert results["status"] == "NOT_RUN"
    assert results["scores"] is None
    assert results["metrics"] is None
    assert results["run_id"] is None
    assert "模型权重/云端 GPU 未提供" in results["blocked_reason"]


def test_prompt_has_no_oracle_fields() -> None:
    prompt = (BASELINE_DIR / "prompt.txt").read_text(encoding="utf-8").lower()
    for forbidden in ("expected_root_cause", "fault_seed"):
        assert forbidden not in prompt


def test_prompt_requires_structured_hypothesis_and_confidence_bands() -> None:
    prompt = (BASELINE_DIR / "prompt.txt").read_text(encoding="utf-8")
    for term in (
        "JSON",
        "evidence_id",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "missing_evidence",
        "confidence_band",
        "HIGH",
        "MEDIUM",
        "LOW",
        "UNKNOWN",
        "禁止执行 SQL",
        "不可信",
    ):
        assert term in prompt
    assert "hypotheses" in prompt


def test_tool_schema_covers_two_engines_seven_tools_each(tool_schema: dict) -> None:
    assert tool_schema["tool_schema_version"] == "tool-schema-v0"
    assert tool_schema["harness_version"] == "44e4f9d"
    assert set(tool_schema["engines"]) == ENGINES
    total = 0
    for engine in ENGINES:
        tools = tool_schema["engines"][engine]["tools"]
        assert len(tools) == 7
        assert {tool["name"] for tool in tools} == FIXED_TOOL_NAMES
        for tool in tools:
            assert {
                "name",
                "version",
                "engine",
                "input_schema",
                "output_schema",
                "risk_level",
            }.issubset(tool)
            assert tool["engine"] == engine
            assert tool["risk_level"] == "R0"
            assert isinstance(tool["input_schema"], dict)
            assert isinstance(tool["output_schema"], dict)
        total += len(tools)
    assert total == 14


def test_testset_covers_six_combinations_and_group_split(testset: dict) -> None:
    assert testset["testset_version"] == "testset-v0-preview"
    assert testset["preview"] is True
    assert testset["executed"] is False
    assert testset["generated"] is False

    cases = testset["cases"]
    combos = {(case["engine"], case["scenario_id"]) for case in cases}
    assert combos == {(engine, scenario) for engine in ENGINES for scenario in SCENARIOS}
    assert len(cases) == 6
    for case in cases:
        assert len(case["seeds"]) == 20
        assert case["repetitions_per_seed"] == 3
        assert case["status"] == "NOT_GENERATED"

    split = testset["split"]
    assert split["strategy"] == "group_split"
    assert {"engine", "fault_family", "seed", "workload_group"}.issubset(
        set(split["group_keys"])
    )
    oracle = split["oracle_isolation"]
    assert oracle["enabled"] is True
    assert oracle["oracle_visible_to_agent"] is False
    assert oracle["oracle_visible_to_model"] is False
    assert oracle["oracle_in_logs"] is False
    assert oracle["oracle_in_training_samples"] is False


def test_run_baseline_dry_run_returns_zero_without_network(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_baseline 不允许联网")

    monkeypatch.setattr(socket, "socket", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)

    exit_code = runner.main(["--dry-run", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err

    plan = json.loads(captured.out)
    assert plan["mode"] == "dry-run"
    assert plan["network_access"] is False
    assert plan["model_loaded"] is False
    assert plan["training"] is False
    assert plan["external_api_calls"] is False
    assert plan["status"] == "BLOCKED_MODEL_ENV_MISSING"
    assert plan["assets"]["planned_episodes"] == 360
    assert plan["assets"]["tool_schema_tools"] == 14
    assert plan["assets"]["testset_cases"] == 6


def test_run_baseline_assets_validate_cleanly(runner: ModuleType) -> None:
    assets = runner.load_assets(BASELINE_DIR)
    assert runner.validate_assets(assets) == []
