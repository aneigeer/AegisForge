"""W08 AC-01/02/03/04/09：验证计划 / 封闭文法 / 最差法汇总 / 异路径比对。

AC-04 的 `inspect.signature(Verifier.verify)` 半条依赖 T03 的 Verifier，
本卡（T02）只覆盖「向 VerificationPlan 注入模型文本字段 → ValidationError」半条。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from core.audit.events import redact_payload
from core.errors import DisclosureIncompleteError, ErrorCode, RequestInvalidError
from core.tasks.models import utc_now
from core.tools.contracts import (
    ToolResult,
    ToolSource,
    ToolStatus,
    VerificationAssertion,
)
from core.verification.models import AssertionOutcome, VerificationResult
from core.verification.plan import (
    EXPECTED_UNSUPPORTED,
    OBSERVED_MISSING,
    STEP_OBSERVED_MISSING,
    UNRESOLVED_PLACEHOLDER,
    VerificationPlan,
    aggregate,
    evaluate_assertion,
    evaluate_plan,
    plan_digest,
    resolve_expected,
)
from core.verification.readback import ReadbackSample
from core.verification.repository import VerificationResultRepository

PARAMS: dict[str, Any] = {"note": "repaired", "expected_note": "broken"}


def _assertions() -> tuple[VerificationAssertion, ...]:
    """W07 冻结的三条断言（docs/w07/02-design.md:345-347）。"""
    return (
        VerificationAssertion(
            assertion_id="affected_rows", metric="affected_rows",
            expected="1", tolerance="exact", source="step_observed",
        ),
        VerificationAssertion(
            assertion_id="target_note_final", metric="dg_lab.seed_orders.note",
            expected="$params.note", tolerance="exact", source="target_readback",
        ),
        VerificationAssertion(
            assertion_id="snapshot_comparable", metric="before_after_snapshot",
            expected="before==$params.expected_note && after==$params.note",
            tolerance="exact", source="step_observed",
        ),
    )


def _plan(assertions: tuple[VerificationAssertion, ...] | None = None) -> VerificationPlan:
    items = assertions or _assertions()
    snapshot = {
        "verification_plan": {
            "assertions": [a.model_dump() for a in items],
            "min_assertions": 3,
            "applicable": True,
        }
    }
    return VerificationPlan.from_disclosure(
        {"disclosure": snapshot},
        definition=SimpleNamespace(verification_plan=list(items)),
        parameters=PARAMS,
    )


def _outcome(assertion_id: str, verdict: str) -> AssertionOutcome:
    return AssertionOutcome(
        assertion_id=assertion_id, metric="m", expected="1", tolerance="exact",
        source="step_observed", observed=1, verdict=verdict,  # type: ignore[arg-type]
        reason=None, evidence_ref=None, collected_at=utc_now(),
    )


def _step_observed() -> dict[str, Any]:
    return {
        "observed_id": "obs_1",
        "result": {"before": "broken", "after": "repaired", "affected_rows": 1},
    }


def _readback() -> ReadbackSample:
    return ReadbackSample(value="repaired", sample_id="rb_1", collected_at=utc_now())


# ---- AC-01 ---------------------------------------------------------------
def test_from_disclosure_exposes_three_frozen_assertions() -> None:
    plan = _plan()
    assert [a.assertion_id for a in plan.assertions] == [
        "affected_rows", "target_note_final", "snapshot_comparable",
    ]
    assert all(a.source in {"step_observed", "target_readback"} for a in plan.assertions)
    assert all(a.expected and a.tolerance for a in plan.assertions)
    assert len(plan.plan_digest) == 64
    assert plan.plan_digest == plan_digest(plan.assertions)


def test_evaluate_plan_three_assertions_all_pass_with_observed() -> None:
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS,
        step_observed=_step_observed(), readback=_readback(),
    )
    assert len(outcomes) == 3
    assert all(o.verdict in {"PASS", "FAIL", "UNKNOWN"} for o in outcomes)
    assert all(o.observed is not None for o in outcomes)
    assert all(o.source for o in outcomes)
    assert [o.verdict for o in outcomes] == ["PASS", "PASS", "PASS"]


# ---- AC-02 ---------------------------------------------------------------
def test_resolve_expected_closed_grammar() -> None:
    assert resolve_expected("1", PARAMS).kind == "literal"
    assert resolve_expected("$params.note", PARAMS).value == "repaired"
    assert resolve_expected("$params.expected_note", PARAMS).value == "broken"
    conjunction = resolve_expected(
        "before==$params.expected_note && after==$params.note", PARAMS
    )
    assert conjunction.kind == "conjunction"
    assert (conjunction.before, conjunction.after) == ("broken", "repaired")


def test_unknown_placeholder_is_unknown_and_never_raises() -> None:
    resolved = resolve_expected("$params.x", PARAMS)
    assert resolved.kind == "unresolved"
    assert resolved.reason == UNRESOLVED_PLACEHOLDER
    assertion = VerificationAssertion(
        assertion_id="a1", metric="m", expected="$params.x",
        tolerance="exact", source="step_observed",
    )
    outcome = evaluate_assertion(assertion, parameters=PARAMS, observed="anything")
    assert outcome.verdict == "UNKNOWN"
    assert outcome.reason == UNRESOLVED_PLACEHOLDER
    assert resolve_expected("$params.note && foo", PARAMS).reason == EXPECTED_UNSUPPORTED


def test_expected_is_never_evaluated() -> None:
    """文法外表达式按字面量比较，绝不 eval/exec。"""
    expression = "__import__('os').system('echo pwned')"
    assert resolve_expected(expression, PARAMS).kind == "literal"
    assertion = VerificationAssertion(
        assertion_id="a1", metric="m", expected=expression,
        tolerance="exact", source="step_observed",
    )
    outcome = evaluate_assertion(assertion, parameters=PARAMS, observed=expression)
    assert outcome.verdict == "PASS"


# ---- AC-03 ---------------------------------------------------------------
def test_aggregate_worst_case_never_inflates() -> None:
    assert aggregate([_outcome("a", "PASS"), _outcome("b", "PASS"), _outcome("c", "UNKNOWN")]) == "UNKNOWN"
    assert aggregate([_outcome("a", "PASS"), _outcome("b", "FAIL"), _outcome("c", "UNKNOWN")]) == "FAIL"
    assert aggregate([_outcome("a", "UNKNOWN"), _outcome("b", "UNKNOWN"), _outcome("c", "UNKNOWN")]) == "UNKNOWN"
    assert aggregate([_outcome("a", "PASS"), _outcome("b", "PASS"), _outcome("c", "PASS")]) == "PASS"
    assert aggregate([]) == "UNKNOWN"


def test_aggregate_is_order_independent() -> None:
    first = [_outcome("a", "PASS"), _outcome("b", "FAIL"), _outcome("c", "UNKNOWN")]
    assert aggregate(first) == aggregate(list(reversed(first))) == "FAIL"


# ---- AC-04（extra=forbid 半条）-------------------------------------------
def test_plan_extra_forbid_blocks_model_text() -> None:
    plan = _plan()
    with pytest.raises(ValidationError):
        VerificationPlan(**{**plan.model_dump(), "model_output": "自报"})
    with pytest.raises(ValidationError):
        VerificationPlan(**{**plan.model_dump(), "narrative": "模型文本"})


# ---- AC-09 ---------------------------------------------------------------
def test_missing_step_observed_yields_unknown_overall() -> None:
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS, step_observed=None, readback=_readback(),
    )
    by_id = {o.assertion_id: o for o in outcomes}
    assert by_id["affected_rows"].verdict == "UNKNOWN"
    assert by_id["affected_rows"].reason == STEP_OBSERVED_MISSING
    assert by_id["snapshot_comparable"].verdict == "UNKNOWN"
    assert by_id["snapshot_comparable"].reason == STEP_OBSERVED_MISSING
    assert aggregate(outcomes) == "UNKNOWN"


def test_readback_does_not_reuse_step_observed_result() -> None:
    """篡改 step_observed.result 不改变 target_readback 的判定（异路径）。"""
    tampered = {"observed_id": "obs_1", "result": {
        "before": "WRONG", "after": "WRONG", "affected_rows": 99,
    }}
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS, step_observed=tampered, readback=_readback(),
    )
    by_id = {o.assertion_id: o.verdict for o in outcomes}
    assert by_id["target_note_final"] == "PASS"
    assert by_id["affected_rows"] == "FAIL"


# ---- KD-W12-01：`step_observed.result` 的真实落盘形状 ---------------------
def _real_tool_result_dump(
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造执行器**真实落盘**的 `result`：`ToolResult.model_dump(mode="json")`。

    `executor.py:486` 落的是 `redact_payload(result.model_dump(mode="json"))`，
    业务指标位于 `result["data"]` 之下，`status` / `error` 等键同在顶层。
    这正是历史扁平注入用例（`_step_observed()`）从未覆盖的形状。
    """
    payload = (
        data
        if data is not None
        else {"affected_rows": 1, "before": "broken", "after": "repaired"}
    )
    dumped = ToolResult(
        tool_name="lab.repair_seed_order_note",
        status=ToolStatus.SUCCESS,
        task_id="task_w12_shape",
        run_id="run_w12_shape",
        tool_call_id="call_w12_shape",
        started_at="2026-02-01T00:00:00+00:00",
        duration_ms=12.5,
        data=payload,
        source=ToolSource(
            database_id="db_lab",
            engine="mysql",
            db_version="8.0.36",
            collected_at="2026-02-01T00:00:00+00:00",
            adapter_version="1.0.0",
        ),
    ).model_dump(mode="json")
    # 形状自检：真实快照必须同时具备 status / data / error，且指标在 data 下。
    assert {"status", "data", "error"} <= set(dumped)
    assert "affected_rows" not in dumped
    return redact_payload(dumped)


def _real_step_observed(
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"observed_id": "obs_w12_shape", "result": _real_tool_result_dump(data)}


def test_step_observed_real_toolresult_shape_reads_metrics_from_data() -> None:
    """真实形状（指标在 `result["data"]` 下）必须可判定为 PASS，不再是 UNKNOWN。"""
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS,
        step_observed=_real_step_observed(), readback=_readback(),
    )
    by_id = {o.assertion_id: o for o in outcomes}
    assert by_id["affected_rows"].verdict == "PASS", by_id["affected_rows"].model_dump()
    assert by_id["affected_rows"].observed == 1
    assert by_id["snapshot_comparable"].verdict == "PASS", (
        by_id["snapshot_comparable"].model_dump()
    )
    assert by_id["snapshot_comparable"].observed == "before=broken|after=repaired"
    # 异路径证据不受影响：target_readback 仍只读独立回读样本。
    assert by_id["target_note_final"].verdict == "PASS"
    assert by_id["target_note_final"].observed == "repaired"
    assert aggregate(outcomes) == "PASS"


def test_step_observed_real_shape_mismatch_fails_not_unknown() -> None:
    """真实形状下指标**取得到但值不符** → FAIL（区分 UNKNOWN 与 FAIL）。"""
    observed = _real_step_observed(
        {"affected_rows": 2, "before": "WRONG", "after": "repaired"}
    )
    by_id = {
        o.assertion_id: o
        for o in evaluate_plan(
            _plan(), parameters=PARAMS, step_observed=observed, readback=_readback(),
        )
    }
    assert by_id["affected_rows"].verdict == "FAIL"
    assert by_id["affected_rows"].reason is None
    assert by_id["snapshot_comparable"].verdict == "FAIL"
    assert by_id["snapshot_comparable"].reason is None
    assert by_id["target_note_final"].verdict == "PASS"
    assert aggregate(by_id.values()) == "FAIL"


def test_step_observed_flat_shape_is_still_supported() -> None:
    """对照用例：历史扁平注入（指标在顶层）兼容性未退化。"""
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS,
        step_observed=_step_observed(), readback=_readback(),
    )
    assert [o.verdict for o in outcomes] == ["PASS", "PASS", "PASS"]
    assert aggregate(outcomes) == "PASS"


def test_step_observed_data_without_snapshot_keys_falls_back_to_top_level() -> None:
    """`data` 只带普通指标、快照双等值在顶层时，快照断言仍可判定。"""
    result = _real_tool_result_dump({"affected_rows": 1})
    result["before"] = "broken"
    result["after"] = "repaired"
    outcomes = evaluate_plan(
        _plan(), parameters=PARAMS,
        step_observed={"observed_id": "obs_mixed", "result": result},
        readback=_readback(),
    )
    assert [o.verdict for o in outcomes] == ["PASS", "PASS", "PASS"], [
        o.model_dump() for o in outcomes
    ]
    assert aggregate(outcomes) == "PASS"


def test_step_observed_real_shape_missing_metric_stays_unknown() -> None:
    """真实形状下 `data` 与顶层都没有该指标 → 仍 UNKNOWN（不注水成 PASS）。"""
    observed = _real_step_observed({"before": "broken", "after": "repaired"})
    by_id = {
        o.assertion_id: o
        for o in evaluate_plan(
            _plan(), parameters=PARAMS, step_observed=observed, readback=_readback(),
        )
    }
    assert by_id["affected_rows"].verdict == "UNKNOWN"
    assert by_id["affected_rows"].reason == OBSERVED_MISSING
    assert by_id["snapshot_comparable"].verdict == "PASS"
    assert aggregate(by_id.values()) == "UNKNOWN"


# ---- from_disclosure fail closed -----------------------------------------
def test_from_disclosure_rejects_incomplete_plan() -> None:
    too_few = _assertions()[:2]
    with pytest.raises(DisclosureIncompleteError):
        _plan(too_few)
    missing_source = (
        VerificationAssertion(assertion_id="a1", metric="m", expected="1", tolerance="exact", source="step_observed"),
        VerificationAssertion(assertion_id="a2", metric="m", expected="1", tolerance="exact", source="step_observed"),
        VerificationAssertion(assertion_id="a3", metric="m", expected="1", tolerance="exact", source="step_observed"),
    )
    with pytest.raises(DisclosureIncompleteError):
        _plan(missing_source)


def test_from_disclosure_rejects_snapshot_contract_mismatch() -> None:
    items = _assertions()
    snapshot = {"verification_plan": {"assertions": [
        {**items[0].model_dump(), "expected": "999"},
        items[1].model_dump(), items[2].model_dump(),
    ]}}
    with pytest.raises(DisclosureIncompleteError):
        VerificationPlan.from_disclosure(
            {"disclosure": snapshot},
            definition=SimpleNamespace(verification_plan=list(items)),
            parameters=PARAMS,
        )


def test_from_disclosure_parameters_must_be_mapping() -> None:
    items = _assertions()
    snapshot = {"verification_plan": {"assertions": [a.model_dump() for a in items]}}
    with pytest.raises(RequestInvalidError) as exc:
        VerificationPlan.from_disclosure(
            {"disclosure": snapshot},
            definition=SimpleNamespace(verification_plan=list(items)),
            parameters="not-a-mapping",  # type: ignore[arg-type]
        )
    assert exc.value.code is ErrorCode.REQUEST_INVALID


# ---- repository：只追加 ---------------------------------------------------
class _FakeControlClient:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.fetched: list[str] = []
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        self.executed.append((sql, params))
        return 1

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] | None = None, *, max_rows: int | None = None
    ) -> list[dict[str, Any]]:
        self.fetched.append(sql)
        return list(self.rows)


def test_repository_is_insert_only_and_roundtrips() -> None:
    client = _FakeControlClient()
    repo = VerificationResultRepository(client)  # type: ignore[arg-type]
    result = VerificationResult(
        result_id="r1", tenant_id="t1", task_id="task1", tool_call_id="call1",
        plan_digest="d" * 64, verdict="PASS", outcomes=(_outcome("a1", "PASS"),),
        disposition=None, approval_required=False, claimed_success=True,
        collected_at=utc_now(),
    )
    assert repo.insert(result) == "r1"
    assert len(client.executed) == 1
    sql = client.executed[0][0].upper()
    assert sql.startswith("INSERT")
    assert "UPDATE" not in sql and "DELETE" not in sql

    client.rows = [{
        "result_id": "r1", "tenant_id": "t1", "task_id": "task1",
        "tool_call_id": "call1", "plan_digest": "d" * 64, "verdict": "PASS",
        "outcomes_json": json.dumps([o.model_dump(mode="json") for o in result.outcomes]),
        "disposition": None, "approval_required": 1, "claimed_success": 1,
        "collected_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }]
    loaded = repo.list_by_tool_call("t1", "call1")
    assert len(loaded) == 1
    assert loaded[0].verdict == "PASS"
    assert len(loaded[0].outcomes) == 1
    assert client.fetched[-1].upper().startswith("SELECT")
