"""V4.1 Track B 单元测试：通用 Agent 护栏 + 效果回归评测中台（``core.eval``）。

覆盖 ``STEP-05`` 验收点：

- 内置套件 ≥12 条确定性样例，覆盖 SQLi / 硬编码密钥 / 提示注入 / PII / K8s / 零误报；
- canary 泄漏由引擎独立判定，命中即累加 ``leakage_count`` 并判 FAIL；
- 指标字段与 ``core.eval.repository.compare_metrics`` 对齐，缺测为 ``None`` 不补 0；
- ``export_golden_dir`` 指定的金标导出通过写盘二次复检且 canary 零残留。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.eval.agent_benchmark import (
    REPOSITORY_METRIC_KEYS,
    AgentEvalCase,
    build_reference_runner,
    compare_agent_variants,
    detect_findings,
    load_agent_eval_suite,
    run_agent_eval_suite,
)
from core.eval.repository import compare_metrics
from core.guardrails import AgentGuardrailClient

SUITE = Path(__file__).resolve().parents[2] / "benchmarks" / "guardrails" / "suite-agent-safety-v1.json"


@pytest.fixture(scope="module")
def cases() -> tuple[AgentEvalCase, ...]:
    return load_agent_eval_suite(SUITE)


def test_suite_is_offline_deterministic_and_covers_required_scenarios(
    cases: tuple[AgentEvalCase, ...],
) -> None:
    assert len(cases) >= 12
    assert len({case.case_id for case in cases}) == len(cases)

    categories = {case.category for case in cases}
    assert {"sql_injection_fix", "secret_exfil", "prompt_injection", "pii_handling", "k8s_manifest"} <= categories
    assert any(case.category == "zero_false_positive" for case in cases)

    # canary 必须是无害自定义前缀，且不得出现受管密钥格式
    canaries = [case.secret_canary for case in cases if case.secret_canary]
    assert canaries
    assert all(canary.startswith(("CANARY_", "1381")) for canary in canaries), canaries
    assert detect_findings(cases[0].input_payload, modality=cases[0].modality) == ("SQL_INJECTION",)


def test_metrics_align_with_repository_contract(cases: tuple[AgentEvalCase, ...]) -> None:
    metrics = run_agent_eval_suite(cases, build_reference_runner(guardrail=AgentGuardrailClient()))
    payload = metrics.to_repository_metrics()
    assert tuple(payload) == REPOSITORY_METRIC_KEYS
    # 本地确定性评测没有真实模型调用：延迟与 Token 必须缺测为 None，不得补 0
    assert payload["latency_p95_ms"] is None
    assert payload["tokens_per_case"] is None
    assert 0.0 <= payload["diagnosis_accuracy"] <= 1.0
    assert compare_metrics(payload, payload).verdict == "INCONCLUSIVE"


def test_candidate_zero_leakage_and_baseline_actually_leaks(
    cases: tuple[AgentEvalCase, ...],
) -> None:
    baseline = run_agent_eval_suite(cases, build_reference_runner())
    candidate = run_agent_eval_suite(
        cases, build_reference_runner(guardrail=AgentGuardrailClient())
    )

    assert baseline.leakage_count >= 2, "无护栏基线必须真实测出泄漏"
    assert candidate.leakage_count == 0
    assert candidate.diagnosis_accuracy > baseline.diagnosis_accuracy
    assert candidate.outcome_pass_rate > baseline.outcome_pass_rate
    assert candidate.secret_block_rate == 1.0
    assert baseline.secret_block_rate == 0.0
    assert candidate.critic_recovery_rate == 1.0
    assert baseline.critic_recovery_rate == 0.0


def test_leakage_is_judged_by_engine_not_by_agent_self_report(
    cases: tuple[AgentEvalCase, ...],
) -> None:
    canary_case = next(case for case in cases if case.secret_canary)
    leaking_runner_result = {
        "blocked": True,
        "findings": [],
        "output": {"answer": f"注意：{canary_case.secret_canary}"},
    }
    metrics = run_agent_eval_suite([canary_case], lambda _case: dict(leaking_runner_result))
    assert metrics.leakage_count == 1
    assert metrics.outcome_pass_rate == 0.0
    assert metrics.leaked_case_ids == (canary_case.case_id,)


def test_runner_exception_counts_as_failure_without_aborting(
    cases: tuple[AgentEvalCase, ...],
) -> None:
    subset = cases[:2]

    def _boom(case: AgentEvalCase) -> dict:
        if case.case_id == subset[0].case_id:
            raise RuntimeError("agent crashed")
        return {"blocked": False, "findings": list(case.expected_findings), "output": {"ok": True}}

    metrics = run_agent_eval_suite(subset, _boom)
    assert metrics.total_cases == 2
    assert subset[0].case_id in metrics.failed_case_ids
    assert metrics.diagnosis_accuracy == 0.5


def test_compare_agent_variants_gives_accurate_verdict_and_golden_export(
    tmp_path: Path,
    cases: tuple[AgentEvalCase, ...],
) -> None:
    golden_dir = tmp_path / "golden"
    report = compare_agent_variants(
        SUITE,
        build_reference_runner(),
        build_reference_runner(guardrail=AgentGuardrailClient()),
        export_golden_dir=golden_dir,
        suite_version="agent-safety-v1",
    )

    assert report["total_cases"] == len(cases)
    assert report["verdict"] == "CANDIDATE_BETTER"
    assert report["reason"] == "ALL_METRICS_IMPROVED"
    assert report["leakage"] == {"baseline": report["baseline"]["leakage_count"], "candidate": 0}
    assert report["candidate"]["leakage_count"] == 0
    assert report["data_source"] == "LIVE_EVAL"

    golden = report["golden"]
    assert golden is not None
    assert golden["leakage_count"] == 0
    assert golden["canary_hits"] == 0
    out_dir = Path(golden["output_dir"])
    samples_text = (out_dir / "samples.jsonl").read_text(encoding="utf-8")
    assert (out_dir / "manifest.json").is_file()
    for case in cases:
        if case.secret_canary:
            assert case.secret_canary not in samples_text
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["leakage_count"] == 0


def test_compare_agent_variants_detects_baseline_regression(tmp_path: Path) -> None:
    """反向自检：把"退化版本"当候选，判决必须是 BASELINE_BETTER 而不是硬编码通过。"""
    report = compare_agent_variants(
        SUITE,
        build_reference_runner(guardrail=AgentGuardrailClient()),
        build_reference_runner(),
        export_golden_dir=None,
    )
    assert report["verdict"] == "BASELINE_BETTER"
    assert report["candidate"]["leakage_count"] >= 2


def test_suite_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_agent_eval_suite(tmp_path / "nope.json")
