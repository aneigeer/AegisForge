"""P2-T1B 单元测试：外部基准转换 + suite-v2 扩张套件。

验收口径（`docs/enterprise-upgrade-roadmap.json` P2-T1B）：

1. 扩张套件通过 Oracle 隔离检查（`assert_no_oracle_leak`）与 `RedactionGate`；
2. 冻结的 W10 `suite-v1` **逐字节不变**（sha256 固定值比对）。

另覆盖转换器的"缺失即失败"契约：语料不存在时抛 `CorpusMissingError`，
绝不产出占位案例；外部案例无本地故障清单时进入 `external_unmapped`，不进 cases。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.external.convert import SUPPORTED_SOURCES, convert
from benchmarks.external.schema import CorpusMissingError, ExternalCase
from benchmarks.external.suite_v2 import build_suite_v2, split_external, write_suite_v2
from core.benchmark.oracle import assert_no_oracle_leak
from core.benchmark.runner import load_suite, plan_cases
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionChannel

ROOT = Path(__file__).resolve().parents[2]
SUITE_V1 = ROOT / "benchmarks" / "w10" / "suite_manifest.json"
SUITE_V2 = ROOT / "benchmarks" / "w10" / "suite-v2.json"

#: suite-v1 的冻结指纹；任何改动都会让该断言失败（P2-T1B 的硬约束）。
SUITE_V1_SHA256 = "b2b9d55eafcaac4efd36477dd67558b46af82354ff4dcf384573716126d29f4a"


def test_frozen_suite_v1_is_byte_identical() -> None:
    assert hashlib.sha256(SUITE_V1.read_bytes()).hexdigest() == SUITE_V1_SHA256


def test_suite_v2_is_loadable_and_expanded() -> None:
    spec = load_suite(SUITE_V2)
    assert spec.suite_version == "suite-v2"
    planned = plan_cases(spec)
    assert len(planned) >= 30, "扩张套件至少 30 个 case 实例"
    assert {case.engine for case in planned} == {"mysql", "postgresql"}
    assert {case.scenario for case in planned} == {
        "slow_query",
        "connection_pressure",
        "lock_wait",
    }


def test_suite_v2_passes_oracle_isolation_and_redaction() -> None:
    payload = json.loads(SUITE_V2.read_text(encoding="utf-8"))
    # ① Oracle 隔离：套件里 `fault_seed` 是**公开**的 data_seed（设计允许），
    #    真正需要扫描的是"可能进入模型上下文的那部分"——即 case 的可见投影。
    visible = [
        json.dumps(
            {key: case[key] for key in ("case_id", "engine", "scenario")},
            ensure_ascii=False,
        )
        for case in payload["cases"]
    ]
    assert_no_oracle_leak(visible)
    # ② 脱敏闸门：作为导出文档扫描，必须 CLEAN（套件只有 id/引擎/场景/种子）。
    _, result = RedactionGate(DataRedactionEngine()).for_document(
        payload, channel=RedactionChannel.EXPORT, task_id="suite_v2_probe"
    )
    assert result.status.value in {"CLEAN", "REDACTED"}
    assert not {"PHONE", "EMAIL", "ID_CARD"} & {
        item.value for item in result.detected_categories
    }


def test_suite_v2_does_not_smuggle_truth_fields() -> None:
    text = SUITE_V2.read_text(encoding="utf-8")
    for forbidden in ("expected_root_cause", "injection_params", "truth_assertions"):
        assert forbidden not in text


def test_converter_missing_corpus_fails_closed(tmp_path: Path) -> None:
    for source in SUPPORTED_SOURCES:
        with pytest.raises(CorpusMissingError) as excinfo:
            convert(source, tmp_path / f"{source}_cases.jsonl")
        assert excinfo.value.code == "EXTERNAL_CORPUS_MISSING"


def test_converter_normalizes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "diagdb_cases.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"id": "d-1", "db": "mysql", "fault": "slow_query", "seed": 4101}),
                json.dumps({"id": "d-2", "db": "postgresql", "fault": "lock_wait", "seed": 4203}),
            ]
        ),
        encoding="utf-8",
    )
    cases = convert("diagdb", path)
    assert [case.source_case_id for case in cases] == ["d-1", "d-2"]
    assert {case.engine for case in cases} == {"mysql", "postgresql"}


def test_converter_rejects_unknown_scenario(tmp_path: Path) -> None:
    path = tmp_path / "dbot_cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "x-1",
                "database": "mysql",
                "issue_type": "replication_lag",
                "data_seed": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusMissingError):
        convert("dbot", path)


def test_unmapped_external_cases_are_not_in_suite(tmp_path: Path) -> None:
    # 只提供一个 mysql/slow_query 清单，再把一个 PG 案例挂进来 → 必然 unmapped。
    only_one = tmp_path / "manifests"
    only_one.mkdir()
    (only_one / "mysql_slow_query.json").write_text(
        json.dumps(
            {
                "scenario_id": "mysql_slow_query",
                "scenario": "slow_query",
                "db_engine": "mysql",
                "data_seed": 4101,
            }
        ),
        encoding="utf-8",
    )
    external = (
        ExternalCase(
            source="bird_critic",
            source_case_id="q-1",
            engine="postgresql",
            scenario="connection_pressure",
            fault_seed=9999,
        ),
    )
    suite = build_suite_v2(repetitions=2, external=external, directory=only_one)
    assert suite["provenance"]["external_attached"] == []
    assert suite["provenance"]["external_unmapped"][0]["source_case_id"] == "q-1"
    assert all("q-1" not in case["case_id"] for case in suite["cases"])


def test_write_suite_v2_is_deterministic(tmp_path: Path) -> None:
    first = write_suite_v2(tmp_path / "a.json", repetitions=3)
    second = write_suite_v2(tmp_path / "b.json", repetitions=3)
    assert first.read_bytes() == second.read_bytes()
