"""W11 T04 单元测试：``publish_decision`` 纯函数与限制清单（AC-17 / AC-18）。

覆盖：

- **AC-17①** 三份结果全绿 + ``environment_blocks=()`` → ``ALPHA_ALLOWED``、
  ``failed_test_ids == ()``；**AC-17②** 注入 1 条红线失败 → ``TECH_PREVIEW_ONLY``
  且 ``failed_test_ids`` 含该 ``test_id``；
- **完备性守卫（非红线，Q-W11-A）**：结果集缺项或 ``environment_blocks`` 非空 →
  ``TECH_PREVIEW_ONLY`` + ``reason="ENV_BLOCK_UNRESOLVED"``，但
  ``failed_test_ids`` **必须为空**（PG 段仍 ``NOT_EXECUTED``，不判 FAIL）；
- **AC-18①** 纯函数：无 I/O / 无时间 / 同输入逐位相等 / ``inputs_digest`` 用
  ``canonical_digest``；**AC-18②** 签名无任何覆盖开关；**AC-18③** 6 个自动限制项
  且 marker 三值齐备；
- **自动项不可删**（W12-Q8）：删除/改 marker/改 statement → ``LIMITATION_REMOVED``；
  人工项只能追加且必须带 ``added_by``/``reason``。

本文件只跑纯逻辑：不连库、不读 .env、不写文件（``docs/w11/limitations.json`` 只读比对）。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.assurance.concurrency import CONCURRENCY_CASE_IDS, ConcurrencyResult
from core.assurance.faults import FAULT_BOUNDARIES, FAULT_CLASSES, FaultInjectionResult, RecoveryCheck
from core.assurance.negative import DenyObservation, NegativeResult
from core.assurance.publish import (
    AUTO_ITEM_PREFIXES,
    CONCURRENCY_CASE_TOTAL,
    ENVIRONMENT_BLOCK_IDS,
    FAULT_CLASS_TOTAL,
    LIMITATION_MARKERS,
    METRIC_DEFINITION_VERSION,
    REASON_ALL_CLEAR,
    REASON_ENV_BLOCK_UNRESOLVED,
    REASON_RED_LINE_FAILED,
    Decision,
    LimitationError,
    LimitationItem,
    LimitationList,
    LimitationMarker,
    LimitationOrigin,
    PublishDecision,
    RedLineFailure,
    append_manual_limitation,
    build_limitations,
    limitations_to_json,
    publish_decision,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIMITATIONS_PATH = _REPO_ROOT / "docs" / "w11" / "limitations.json"
_PUBLISH_SOURCE = _REPO_ROOT / "core" / "assurance" / "publish.py"


# ---------------------------------------------------------------------------
# 三份结果构造器（全绿基线）
# ---------------------------------------------------------------------------


def _green_negative(test_id: str) -> NegativeResult:
    return NegativeResult(
        test_id=test_id,
        expected_deny_reason="POLICY_DENIED",
        actual=DenyObservation(
            denied=True,
            deny_reason="POLICY_DENIED",
            error_code=None,
            target_before=0,
            target_after=0,
        ),
        evidence={"mode": "LIVE", "deny_source": "LIVE_SEAM"},
    )


def _negatives(count: int = 29) -> list[NegativeResult]:
    return [_green_negative(f"g1_case_{index:02d}") for index in range(count)]


def _green_concurrency(case_id: str) -> ConcurrencyResult:
    expected_code = {
        "old_worker_fencing": "FENCING_TOKEN_STALE",
        "wrong_fencing_token": "FENCING_TOKEN_STALE",
        "lease_expired": "WORKER_LEASE_INVALID",
        "two_workers_same_task": "WORKER_CONFLICT",
        "state_version_conflict": "STATE_CONFLICT",
        "nonce_replay": "NONCE_REPLAY",
        "audit_write_failure": "AUDIT_WRITE_FAILED",
    }.get(case_id)
    return ConcurrencyResult(
        test_id=f"w11-t02-{case_id}",
        case_id=case_id,
        denied=True if expected_code else None,
        expected_code=expected_code,
        tool_calls_delta=1,
        target_row_changes=0,
        seq=(0, 1),
        event_types=("TOOL_CALL_STARTED",),
        state_version_before=1,
        state_version_after=1,
    )


def _concurrencies() -> list[ConcurrencyResult]:
    return [_green_concurrency(case_id) for case_id in CONCURRENCY_CASE_IDS]


def _green_recovery() -> RecoveryCheck:
    return RecoveryCheck(
        seq_continuous=True,
        audit_chain_ok=True,
        evidence_refs_resolvable=True,
        state_version_monotonic=True,
        tool_calls_delta=0,
        replay_occurred=False,
    )


def _green_fault(fault_class: Any) -> FaultInjectionResult:
    return FaultInjectionResult(
        test_id=f"fault-{fault_class.value.lower().replace('_', '-')}",
        fault_class=fault_class,
        injected=True,
        fail_closed_ok=True,
        alert_emitted=fault_class.value == "AUDIT_WRITER",
        recovery=_green_recovery(),
        boundary=FAULT_BOUNDARIES[fault_class],
    )


def _faults() -> list[FaultInjectionResult]:
    return [_green_fault(fault_class) for fault_class in FAULT_CLASSES]


def _decide(**overrides: Any) -> PublishDecision:
    payload: dict[str, Any] = {
        "negative": _negatives(),
        "concurrency": _concurrencies(),
        "faults": _faults(),
        "environment_blocks": (),
    }
    payload.update(overrides)
    return publish_decision(**payload)


# ---------------------------------------------------------------------------
# AC-17①：全绿 → ALPHA_ALLOWED
# ---------------------------------------------------------------------------


def test_all_green_without_env_blocks_allows_alpha() -> None:
    decision = _decide()
    assert decision.decision is Decision.ALPHA_ALLOWED
    assert decision.failed_test_ids == ()
    assert decision.red_line_failures == ()
    assert decision.reason == REASON_ALL_CLEAR
    assert decision.environment_blocks == ()
    assert len(decision.inputs_digest) == 64


# ---------------------------------------------------------------------------
# AC-17②：任一红线 → TECH_PREVIEW_ONLY + failed_test_ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, expected_reason",
    [
        ("not_denied", "NOT_DENIED"),
        ("deny_reason_mismatch", "DENY_REASON_MISMATCH"),
        ("target_changed", "TARGET_CHANGED"),
    ],
)
def test_negative_red_lines_flip_to_tech_preview(mutate: str, expected_reason: str) -> None:
    negatives = _negatives()
    broken = negatives[3]
    if mutate == "not_denied":
        broken = dataclasses.replace(
            broken,
            actual=dataclasses.replace(broken.actual, denied=False),
        )
    elif mutate == "deny_reason_mismatch":
        broken = dataclasses.replace(
            broken, actual=dataclasses.replace(broken.actual, deny_reason="R4_NOT_EXECUTABLE")
        )
    else:
        broken = dataclasses.replace(
            broken, actual=dataclasses.replace(broken.actual, target_after=1)
        )
    negatives[3] = broken

    decision = _decide(negative=negatives)
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_RED_LINE_FAILED
    assert broken.test_id in decision.failed_test_ids
    assert {item.reason for item in decision.red_line_failures} == {expected_reason}
    assert all(item.source == "NEGATIVE" for item in decision.red_line_failures)


def test_concurrency_duplicate_side_effect_is_a_red_line() -> None:
    concurrency = _concurrencies()
    broken = concurrency[5]
    concurrency[5] = dataclasses.replace(broken, tool_calls_delta=2)
    decision = _decide(concurrency=concurrency)
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_RED_LINE_FAILED
    assert "TOOL_CALLS_DUPLICATED" in {
        item.reason for item in decision.red_line_failures if item.source == "CONCURRENCY"
    }
    assert broken.test_id in decision.failed_test_ids


def test_concurrency_missing_expected_denial_is_a_red_line() -> None:
    concurrency = _concurrencies()
    index = CONCURRENCY_CASE_IDS.index("old_worker_fencing")
    broken = concurrency[index]
    concurrency[index] = dataclasses.replace(broken, denied=False)
    decision = _decide(concurrency=concurrency)
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert "EXPECTED_DENIAL_MISSING" in {item.reason for item in decision.red_line_failures}
    assert broken.test_id in decision.failed_test_ids


@pytest.mark.parametrize(
    "override, expected_reason",
    [
        ({"fail_closed_ok": False}, "FAIL_OPEN"),
        ({"seq_continuous": False}, "SEQ_NOT_CONTINUOUS"),
        ({"replay_occurred": True}, "REPLAY_OCCURRED"),
        ({"tool_calls_delta": 1}, "TOOL_CALLS_DELTA"),
    ],
)
def test_fault_red_lines_flip_to_tech_preview(
    override: dict[str, Any], expected_reason: str
) -> None:
    faults = _faults()
    target = faults[1]
    if "fail_closed_ok" in override:
        faults[1] = dataclasses.replace(target, fail_closed_ok=False)
    else:
        faults[1] = dataclasses.replace(
            target,
            recovery=dataclasses.replace(target.recovery, **override),
        )
    decision = _decide(faults=faults)
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_RED_LINE_FAILED
    assert target.test_id in decision.failed_test_ids
    assert expected_reason in {item.reason for item in decision.red_line_failures}
    assert {item.source for item in decision.red_line_failures} == {"FAULT"}


# ---------------------------------------------------------------------------
# 完备性守卫：不是红线，failed_test_ids 必须保持为空
# ---------------------------------------------------------------------------


def test_env_blocks_alone_do_not_fail_tests() -> None:
    decision = _decide(environment_blocks=ENVIRONMENT_BLOCK_IDS)
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_ENV_BLOCK_UNRESOLVED
    assert decision.failed_test_ids == (), "环境阻塞不是红线失败：不得进 failed_test_ids"
    assert decision.red_line_failures == ()
    assert decision.environment_blocks == tuple(sorted(ENVIRONMENT_BLOCK_IDS))


def test_missing_results_do_not_fail_tests() -> None:
    decision = _decide(negative=_negatives(28))
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_ENV_BLOCK_UNRESOLVED
    assert decision.failed_test_ids == ()


def test_missing_concurrency_case_is_a_completeness_gap() -> None:
    decision = _decide(concurrency=_concurrencies()[:-1])
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_ENV_BLOCK_UNRESOLVED
    assert decision.failed_test_ids == ()


def test_missing_fault_class_is_a_completeness_gap() -> None:
    decision = _decide(faults=_faults()[:-1])
    assert decision.decision is Decision.TECH_PREVIEW_ONLY
    assert decision.reason == REASON_ENV_BLOCK_UNRESOLVED
    assert decision.failed_test_ids == ()


def test_red_line_wins_over_completeness_guard() -> None:
    negatives = _negatives()
    broken = negatives[0]
    negatives[0] = dataclasses.replace(
        broken, actual=dataclasses.replace(broken.actual, denied=False)
    )
    decision = _decide(negative=negatives, environment_blocks=ENVIRONMENT_BLOCK_IDS)
    assert decision.reason == REASON_RED_LINE_FAILED
    assert decision.failed_test_ids == (broken.test_id,)


# ---------------------------------------------------------------------------
# AC-18①：纯函数
# ---------------------------------------------------------------------------


def test_same_input_produces_identical_decision() -> None:
    first = _decide(environment_blocks=ENVIRONMENT_BLOCK_IDS)
    second = _decide(environment_blocks=ENVIRONMENT_BLOCK_IDS)
    assert first == second
    assert first.inputs_digest == second.inputs_digest
    assert first.failed_test_ids == second.failed_test_ids


def test_inputs_digest_changes_with_input() -> None:
    baseline = _decide()
    changed = _decide(faults=_faults()[:-1])
    assert baseline.inputs_digest != changed.inputs_digest
    assert re.fullmatch(r"[0-9a-f]{64}", baseline.inputs_digest)


def test_publish_decision_has_no_time_fields() -> None:
    names = {field.name for field in dataclasses.fields(PublishDecision)}
    assert names == {
        "decision",
        "failed_test_ids",
        "red_line_failures",
        "environment_blocks",
        "reason",
        "inputs_digest",
        "metric_definition_version",
        "limitation_markers",
    }
    for name in names:
        assert "time" not in name and "date" not in name


def test_publish_decision_body_has_no_io_or_clock() -> None:
    source = inspect.getsource(publish_decision)
    forbidden = (
        "utc_now",
        "datetime.now",
        "time.time",
        "open(",
        "Path(",
        ".connect(",
        "os.environ",
        "getenv",
        "json.load",
    )
    for token in forbidden:
        assert token not in source, f"publish_decision 内出现非纯调用：{token}"


def test_metric_definition_version_is_literal_reference_only() -> None:
    decision = _decide()
    assert decision.metric_definition_version == METRIC_DEFINITION_VERSION == "metrics-v1"
    assert decision.limitation_markers == LIMITATION_MARKERS


# ---------------------------------------------------------------------------
# AC-18②：不可覆盖
# ---------------------------------------------------------------------------


def test_signature_has_no_override_parameters() -> None:
    parameters = inspect.signature(publish_decision).parameters
    assert list(parameters) == ["negative", "concurrency", "faults", "environment_blocks"]
    for name, parameter in parameters.items():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert not name.startswith("allow"), name
        assert name not in {"force_allow", "override", "manual_override", "enabled", "strict"}
    assert parameters["environment_blocks"].default == ()


def test_publish_decision_rejects_fabricated_alpha() -> None:
    """L-5：值对象不能手工拼出"Alpha 通过但带失败项"。"""
    with pytest.raises(ValueError) as excinfo:
        PublishDecision(
            decision=Decision.ALPHA_ALLOWED,
            failed_test_ids=("x",),
            red_line_failures=(),
            environment_blocks=(),
            reason=REASON_ALL_CLEAR,
            inputs_digest="0" * 64,
        )
    assert "PUBLISH_DECISION_INCONSISTENT" in str(excinfo.value)

    with pytest.raises(ValueError):
        PublishDecision(
            decision=Decision.ALPHA_ALLOWED,
            failed_test_ids=(),
            red_line_failures=(RedLineFailure(test_id="y", source="NEGATIVE", reason="NOT_DENIED"),),
            environment_blocks=(),
            reason=REASON_ALL_CLEAR,
            inputs_digest="0" * 64,
        )
    with pytest.raises(ValueError):
        PublishDecision(
            decision=Decision.ALPHA_ALLOWED,
            failed_test_ids=(),
            red_line_failures=(),
            environment_blocks=("ENV-PG5432",),
            reason=REASON_ALL_CLEAR,
            inputs_digest="0" * 64,
        )


def test_replace_cannot_bypass_alpha_consistency() -> None:
    """`dataclasses.replace` 也走 `__init__`/`__post_init__`，旁路同样被拦。"""
    legitimate = _decide()  # ALPHA_ALLOWED + 三个集合全空
    assert legitimate.decision is Decision.ALPHA_ALLOWED
    for overrides in (
        {"failed_test_ids": ("x",)},
        {"environment_blocks": ("ENV-PG5432",)},
        {"red_line_failures": (RedLineFailure(test_id="z", source="FAULT", reason="FAIL_OPEN"),)},
    ):
        with pytest.raises(ValueError) as excinfo:
            dataclasses.replace(legitimate, **overrides)
        assert "PUBLISH_DECISION_INCONSISTENT" in str(excinfo.value), overrides
    # 反向对照：TECH_PREVIEW_ONLY 允许带失败项与阻塞项
    downgraded = dataclasses.replace(
        legitimate, decision=Decision.TECH_PREVIEW_ONLY, failed_test_ids=("x",)
    )
    assert downgraded.decision is Decision.TECH_PREVIEW_ONLY


def test_publish_module_has_no_switches() -> None:
    """AST 层核对：没有覆盖开关形参、没有读环境变量/配置的调用点。

    只扫代码结构（不扫文档字符串），因为模块头上明确**禁止**这些名字。
    """
    tree = ast.parse(_PUBLISH_SOURCE.read_text(encoding="utf-8"))
    banned = {"force_allow", "manual_override", "override", "enabled", "strict"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)}
            assert not names & banned, f"{node.name} 出现覆盖开关形参：{names & banned}"
            assert not any(name.startswith("allow") for name in names), node.name
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            raise AssertionError("禁止在 publish.py 读取环境变量")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"getenv"}, "禁止在 publish.py 读取环境变量"


# ---------------------------------------------------------------------------
# AC-18③：自动限制清单
# ---------------------------------------------------------------------------


def test_build_limitations_has_six_auto_items_with_three_markers() -> None:
    decision = _decide(environment_blocks=ENVIRONMENT_BLOCK_IDS)
    limitations = build_limitations(decision)
    assert limitations.version == "w11-limitations-v1"
    assert limitations.decision is Decision.TECH_PREVIEW_ONLY
    assert len(limitations.items) == 6
    assert [item.item_id for item in limitations.items] == [
        "ENV-PG5432",
        "ENV-1045",
        "ENV-RESIDUE",
        "ENV-NV1",
        "ENV-MODEL",
        "DECISION-TECH-PREVIEW",
    ]
    assert {item.marker for item in limitations.items} == {
        LimitationMarker.NOT_SUPPORTED,
        LimitationMarker.PARTIAL,
        LimitationMarker.UNKNOWN,
    }
    assert all(item.origin is LimitationOrigin.AUTO for item in limitations.items)
    assert all(item.added_by is None and item.reason is None for item in limitations.items)


def test_build_limitations_is_pure_and_repeatable() -> None:
    decision = _decide()
    assert build_limitations(decision) == build_limitations(decision)


def test_auto_item_cannot_be_removed() -> None:
    limitations = build_limitations(_decide(environment_blocks=ENVIRONMENT_BLOCK_IDS))
    remaining = tuple(item for item in limitations.items if item.item_id != "ENV-RESIDUE")
    with pytest.raises(ValidationError) as excinfo:
        LimitationList(decision=limitations.decision, items=remaining)
    assert "LIMITATION_REMOVED" in str(excinfo.value)


def test_auto_item_marker_cannot_be_changed() -> None:
    limitations = build_limitations(_decide())
    tampered = tuple(
        item.model_copy(update={"marker": LimitationMarker.PARTIAL})
        if item.item_id == "ENV-NV1"
        else item
        for item in limitations.items
    )
    with pytest.raises(ValidationError) as excinfo:
        LimitationList(decision=limitations.decision, items=tampered)
    assert "LIMITATION_REMOVED" in str(excinfo.value)


def test_auto_item_statement_cannot_be_changed() -> None:
    limitations = build_limitations(_decide())
    tampered = tuple(
        item.model_copy(update={"statement": "被改写为「已验证」"})
        if item.item_id == "ENV-PG5432"
        else item
        for item in limitations.items
    )
    with pytest.raises(ValidationError) as excinfo:
        LimitationList(decision=limitations.decision, items=tampered)
    assert "LIMITATION_REMOVED" in str(excinfo.value)


def test_auto_item_cannot_be_downgraded_to_manual() -> None:
    limitations = build_limitations(_decide())
    tampered = tuple(
        item.model_copy(update={"origin": LimitationOrigin.MANUAL, "added_by": "x", "reason": "y"})
        if item.item_id == "ENV-MODEL"
        else item
        for item in limitations.items
    )
    with pytest.raises(ValidationError) as excinfo:
        LimitationList(decision=limitations.decision, items=tampered)
    assert "LIMITATION_REMOVED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 人工项：只能追加、必须署名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"added_by": None, "reason": "需要人工记录"},
        {"added_by": "qa", "reason": None},
        {"added_by": "  ", "reason": "需要人工记录"},
        {"added_by": "qa", "reason": "   "},
    ],
    ids=["no_added_by", "no_reason", "blank_added_by", "blank_reason"],
)
def test_manual_item_requires_attribution(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LimitationItem(
            item_id="MANUAL-PROBE-1",
            marker=LimitationMarker.PARTIAL,
            origin=LimitationOrigin.MANUAL,
            statement="人工登记的已知限制",
            **payload,
        )


def test_append_manual_limitation_keeps_auto_items() -> None:
    limitations = build_limitations(_decide(environment_blocks=ENVIRONMENT_BLOCK_IDS))
    manual = LimitationItem(
        item_id="MANUAL-PROBE-1",
        marker=LimitationMarker.PARTIAL,
        origin=LimitationOrigin.MANUAL,
        statement="人工登记的已知限制：仅在 LAB 环境验证过",
        added_by="user_w11_t04",
        reason="W11 发布评审记录",
    )
    extended = append_manual_limitation(limitations, item=manual)
    assert len(extended.items) == 7
    assert extended.items[-1].item_id == "MANUAL-PROBE-1"
    assert {item.item_id for item in extended.items[:6]} == {
        item.item_id for item in limitations.items
    }
    # 原清单不可变（纯函数语义）
    assert len(limitations.items) == 6


def test_append_manual_limitation_rejects_auto_origin() -> None:
    limitations = build_limitations(_decide())
    auto_item = limitations.items[0]
    with pytest.raises(LimitationError) as excinfo:
        append_manual_limitation(limitations, item=auto_item)
    assert excinfo.value.code == "MANUAL_APPEND_REQUIRED"


def test_append_manual_limitation_rejects_auto_prefix() -> None:
    limitations = build_limitations(_decide())
    manual = LimitationItem(
        item_id=f"{AUTO_ITEM_PREFIXES[0]}FAKE",
        marker=LimitationMarker.UNKNOWN,
        origin=LimitationOrigin.MANUAL,
        statement="试图占用自动项命名空间",
        added_by="user_w11_t04",
        reason="负测",
    )
    with pytest.raises(LimitationError) as excinfo:
        append_manual_limitation(limitations, item=manual)
    assert excinfo.value.code == "AUTO_ITEM_PREFIX_CONFLICT"


def test_append_manual_limitation_rejects_duplicate_id() -> None:
    limitations = build_limitations(_decide())
    manual = LimitationItem(
        item_id="MANUAL-PROBE-2",
        marker=LimitationMarker.PARTIAL,
        origin=LimitationOrigin.MANUAL,
        statement="第一条",
        added_by="user_w11_t04",
        reason="负测",
    )
    extended = append_manual_limitation(limitations, item=manual)
    with pytest.raises(LimitationError) as excinfo:
        append_manual_limitation(extended, item=manual)
    assert excinfo.value.code == "LIMITATION_DUPLICATE_ID"


# ---------------------------------------------------------------------------
# docs/w11/limitations.json 是单一数据源（由 build_limitations + 序列化生成）
# ---------------------------------------------------------------------------


def _generated_payload() -> dict[str, Any]:
    decision = publish_decision(
        negative=(),
        concurrency=(),
        faults=(),
        environment_blocks=ENVIRONMENT_BLOCK_IDS,
    )
    text = limitations_to_json(build_limitations(decision))
    return json.loads(text)


def test_limitations_json_matches_generated_output() -> None:
    on_disk = json.loads(_LIMITATIONS_PATH.read_text(encoding="utf-8"))
    assert on_disk == _generated_payload()
    assert on_disk["version"] == "w11-limitations-v1"
    assert on_disk["decision"] == Decision.TECH_PREVIEW_ONLY.value
    assert len(on_disk["items"]) == 6
    assert {item["origin"] for item in on_disk["items"]} == {"AUTO"}
    assert {item["marker"] for item in on_disk["items"]} == set(LIMITATION_MARKERS)


def test_limitations_json_is_revalidatable() -> None:
    payload = json.loads(_LIMITATIONS_PATH.read_text(encoding="utf-8"))
    limitations = LimitationList.model_validate(payload)
    assert limitations.decision is Decision.TECH_PREVIEW_ONLY
    assert len(limitations.items) == 6


# ---------------------------------------------------------------------------
# 表述禁令与计数口径
# ---------------------------------------------------------------------------


def test_limitation_statements_have_no_estimates_or_overclaims() -> None:
    limitations = build_limitations(_decide(environment_blocks=ENVIRONMENT_BLOCK_IDS))
    statements = [item.statement for item in limitations.items]
    for statement in statements:
        for token in ("预计", "估计", "大约", "约等于", "绝对安全", "已验证 SLA"):
            assert token not in statement, (token, statement)
    joined = "\n".join(statements)
    assert "不得表述为完整 Alpha" in joined
    assert "Alpha 已就绪" not in joined


def test_counts_are_frozen() -> None:
    assert CONCURRENCY_CASE_TOTAL == 10
    assert FAULT_CLASS_TOTAL == 6
    assert len(ENVIRONMENT_BLOCK_IDS) == 5
    assert tuple(ENVIRONMENT_BLOCK_IDS) == (
        "ENV-PG5432",
        "ENV-1045",
        "ENV-RESIDUE",
        "ENV-NV1",
        "ENV-MODEL",
    )


def test_decision_and_marker_enums_are_closed() -> None:
    assert {item.value for item in Decision} == {"ALPHA_ALLOWED", "TECH_PREVIEW_ONLY"}
    assert {item.value for item in LimitationMarker} == {
        "NOT_SUPPORTED",
        "PARTIAL",
        "UNKNOWN",
    }
    assert {item.value for item in LimitationOrigin} == {"AUTO", "MANUAL"}
