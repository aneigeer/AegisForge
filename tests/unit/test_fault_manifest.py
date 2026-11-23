"""W04 单元测试：Fault Manifest、脚本哈希、工具白名单与 Oracle 隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.faults.models import FaultCaseManifest
from core.faults.store import (
    INJECTOR_PATH,
    MANIFEST_DIR,
    ORACLE_DIR,
    PUBLIC_TOOL_ALLOWLIST,
    ManifestValidationError,
    OracleLoadError,
    compute_fault_script_hash,
    list_manifests,
    load_manifest,
    load_oracle,
    manifest_summary,
)

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_IDS = {
    "mysql_slow_query",
    "mysql_connection_pressure",
    "mysql_lock_wait",
    "pg_slow_query",
    "pg_connection_pressure",
    "pg_lock_wait",
}
ENGINE_VERSION = {"mysql": "8.0.46", "postgresql": "12.18"}


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_IDS))
def test_manifest_loads_with_complete_fields(scenario_id: str) -> None:
    manifest = load_manifest(scenario_id)
    assert manifest.scenario_id == scenario_id
    assert manifest.db_engine.value in ENGINE_VERSION
    assert manifest.db_version == ENGINE_VERSION[manifest.db_engine.value]
    assert manifest.adapter_version == "1.0"
    assert manifest.migration_version == "004"
    assert manifest.scenario.value in {"slow_query", "connection_pressure", "lock_wait"}
    assert isinstance(manifest.data_seed, int) and manifest.data_seed > 0
    assert manifest.workload_config
    assert manifest.verification_assertions
    assert manifest.db_image_digest is None
    assert "db_image_digest" in str(manifest.workload_config)
    assert set(manifest.allowed_tools) <= PUBLIC_TOOL_ALLOWLIST
    assert len(manifest.fault_script_hash) >= 16


def test_all_six_manifests_and_engines_are_covered() -> None:
    ids = {manifest.scenario_id for manifest in list_manifests()}
    assert ids == EXPECTED_IDS
    combos = {
        (manifest.db_engine.value, manifest.scenario.value) for manifest in list_manifests()
    }
    assert combos == {
        (engine, scenario)
        for engine in ENGINE_VERSION
        for scenario in {"slow_query", "connection_pressure", "lock_wait"}
    }
    assert len({manifest.data_seed for manifest in list_manifests()}) == len(EXPECTED_IDS)


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_IDS))
def test_fault_script_hash_matches_injector_and_runtime_params(scenario_id: str) -> None:
    manifest = load_manifest(scenario_id)
    expected = compute_fault_script_hash(
        INJECTOR_PATH,
        workload_config=manifest.workload_config,
        data_seed=manifest.data_seed,
        db_engine=manifest.db_engine.value,
        allowed_tools=manifest.allowed_tools,
    )
    assert manifest.fault_script_hash == expected


def test_forbidden_sql_tool_rejected_by_model() -> None:
    payload = load_manifest("mysql_slow_query").model_dump(mode="json")
    payload["allowed_tools"] = ["execute_sql"]
    with pytest.raises(ValidationError):
        FaultCaseManifest.model_validate(payload)


def test_non_allowlisted_tool_rejected_by_store(monkeypatch, tmp_path) -> None:
    payload = load_manifest("mysql_slow_query").model_dump(mode="json")
    payload["scenario_id"] = "bad_tool_manifest"
    payload["allowed_tools"] = ["get_activity"]
    (tmp_path / "bad_tool_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr("core.faults.store.MANIFEST_DIR", tmp_path)
    with pytest.raises(ManifestValidationError):
        load_manifest("bad_tool_manifest")


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_IDS))
def test_oracle_scenario_id_and_seed_match_manifest(scenario_id: str) -> None:
    manifest = load_manifest(scenario_id)
    truth = load_oracle(scenario_id)
    assert truth.scenario_id == manifest.scenario_id
    assert truth.fault_seed == manifest.data_seed
    assert truth.expected_root_cause
    assert truth.injection_params
    assert truth.truth_assertions


def test_oracle_loader_is_explicit_for_unknown_scenario() -> None:
    with pytest.raises(OracleLoadError):
        load_oracle("does_not_exist")


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_IDS))
def test_manifest_summary_has_no_oracle_fields(scenario_id: str) -> None:
    summary = manifest_summary(scenario_id)
    text = json.dumps(summary, ensure_ascii=False)
    assert "expected_root_cause" not in text
    assert "fault_seed" not in text
    assert "truth_assertions" not in text
    assert summary["scenario_id"] == scenario_id


def test_manifest_files_never_contain_oracle_fields() -> None:
    for path in MANIFEST_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "expected_root_cause" not in text
        assert "truth_assertions" not in text


def test_oracle_truths_are_unique_and_not_in_manifests() -> None:
    root_causes = {load_oracle(scenario_id).expected_root_cause for scenario_id in EXPECTED_IDS}
    assert len(root_causes) == len(EXPECTED_IDS)
    public_text = json.dumps(
        {scenario_id: manifest_summary(scenario_id) for scenario_id in EXPECTED_IDS},
        ensure_ascii=False,
    )
    for root_cause in root_causes:
        assert root_cause not in public_text


def test_agent_and_diagnosis_do_not_import_oracle() -> None:
    forbidden = ("load_oracle", "core.faults.store", "OracleTruth", "expected_root_cause")
    for directory in (ROOT / "core" / "agent", ROOT / "core" / "diagnosis"):
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{path} 引用了 Oracle：{token}"


def test_oracle_files_live_only_in_oracle_dir() -> None:
    oracle_files = sorted(ORACLE_DIR.glob("*.json"))
    assert {path.stem for path in oracle_files} == EXPECTED_IDS


def test_fault_lab_list_never_prints_oracle_truth(capsys) -> None:
    from scripts import fault_lab

    exit_code = fault_lab.main(["list"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "mysql_slow_query" in captured.out
    for scenario_id in EXPECTED_IDS:
        root_cause = load_oracle(scenario_id).expected_root_cause
        assert root_cause not in captured.out
        assert "expected_root_cause" not in captured.out
