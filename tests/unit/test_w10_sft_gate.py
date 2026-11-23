"""W10 T06 单测：条件性 SFT 闸门（AC-18①②③ / W10-Q7）。

零 DB、零网络；不放宽断言、不加 skip/xfail。

覆盖：

- AC-18① ``shortcoming_located is False`` ⇒ ``sft_allowed False``；
- AC-18② 允许时 ``10 <= sample_count <= 50``，越界返回 ``False`` **不抛错**；
  ``harness_version`` / ``testset_version`` 与 Baseline **逐字相等**否则 ``False``；
  ``reproducible_gain=False`` ⇒ ``False``；
- AC-18③ ``dpo_allowed`` / ``rl_allowed`` / ``full_14b_allowed`` **恒 False**
  （``ClassVar`` + 只读 property），实例赋值 / 字典注入 / ``model_copy`` 注入
  都改不动；
- W10-Q7：``SftGate`` 只做记录，不纳入 W10→W11 门禁口径（不写 ``publish_decision``
  一类的门禁判定字段，也不改变 ``BaselineReport`` 的判定）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.benchmark.report import (
    BaselineReport,
    SftGate,
    assert_no_fabricated_metrics,
    build_baseline_report,
)
from core.benchmark.metrics import MetricsEngine
from core.benchmark.runner import RunResult, RunStatus

ROOT = Path(__file__).resolve().parents[2]
REPORT_MODULE = ROOT / "core" / "benchmark" / "report.py"

BASELINE_HARNESS = "44e4f9d"
BASELINE_TESTSET = "testset-v0-preview"


def _gate(**overrides: object) -> SftGate:
    payload: dict[str, object] = {
        "shortcoming_located": True,
        "reproducible_gain": True,
        "sample_count": 20,
        "harness_version": BASELINE_HARNESS,
        "testset_version": BASELINE_TESTSET,
        "baseline_harness_version": BASELINE_HARNESS,
        "baseline_testset_version": BASELINE_TESTSET,
    }
    payload.update(overrides)
    return SftGate.model_validate(payload)


# --------------------------------------------------------------------------- #
# 模型契约
# --------------------------------------------------------------------------- #


def test_sft_gate_is_closed_model_with_exact_fields() -> None:
    assert set(SftGate.model_fields) == {
        "shortcoming_located",
        "reproducible_gain",
        "sample_count",
        "harness_version",
        "testset_version",
        "baseline_harness_version",
        "baseline_testset_version",
    }
    assert SftGate.model_fields["reproducible_gain"].default is False
    with pytest.raises(ValidationError):
        SftGate.model_validate({**_gate().model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        _gate(sample_count=-1)
    with pytest.raises(ValidationError):
        _gate(sample_count=1001)
    # 1000 是字段上界，仍合法（只是 sft_allowed=False）。
    assert _gate(sample_count=1000).sft_allowed is False


# --------------------------------------------------------------------------- #
# AC-18① 短板未定位
# --------------------------------------------------------------------------- #


def test_ac18_shortcoming_not_located_disallows_sft() -> None:
    gate = _gate(shortcoming_located=False)
    assert gate.sft_allowed is False
    # 其余条件全满足也不能放行。
    assert gate.reproducible_gain is True
    assert gate.sample_count == 20
    assert gate.harness_version == gate.baseline_harness_version
    assert gate.testset_version == gate.baseline_testset_version


def test_ac18_no_reproducible_gain_disallows_sft() -> None:
    assert _gate(reproducible_gain=False).sft_allowed is False


# --------------------------------------------------------------------------- #
# AC-18② 样本量 / 版本逐字相等
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sample_count, expected",
    [
        (0, False),
        (9, False),
        (10, True),
        (25, True),
        (50, True),
        (51, False),
        (100, False),
        (1000, False),
    ],
)
def test_ac18_sample_count_bounds_return_false_not_raise(
    sample_count: int, expected: bool
) -> None:
    gate = _gate(sample_count=sample_count)
    # 越界只返回 False，绝不抛错（AC-18②）。
    assert gate.sft_allowed is expected


def test_ac18_harness_version_must_match_baseline_verbatim() -> None:
    assert _gate(harness_version="44e4f9e").sft_allowed is False
    assert _gate(harness_version=" 44e4f9d").sft_allowed is False
    assert _gate(harness_version="44E4F9D").sft_allowed is False
    assert _gate(baseline_harness_version="44e4f9e").sft_allowed is False
    assert _gate(harness_version=BASELINE_HARNESS).sft_allowed is True


def test_ac18_testset_version_must_match_baseline_verbatim() -> None:
    assert _gate(testset_version="testset-v0-preview2").sft_allowed is False
    assert _gate(baseline_testset_version="testset-v1").sft_allowed is False
    assert _gate(testset_version="TESTSET-V0-PREVIEW").sft_allowed is False
    assert _gate(testset_version=BASELINE_TESTSET).sft_allowed is True


def test_ac18_all_conditions_must_hold_together() -> None:
    assert _gate().sft_allowed is True
    # 任一条件不满足即 False（逐项单独翻转）。
    assert _gate(shortcoming_located=False).sft_allowed is False
    assert _gate(reproducible_gain=False).sft_allowed is False
    assert _gate(sample_count=9).sft_allowed is False
    assert _gate(harness_version="other").sft_allowed is False
    assert _gate(testset_version="other").sft_allowed is False


# --------------------------------------------------------------------------- #
# AC-18③ 三个开关恒 False、不可注入
# --------------------------------------------------------------------------- #


def test_ac18_classvars_are_false_and_properties_always_false() -> None:
    assert SftGate.DPO_ALLOWED is False
    assert SftGate.RL_ALLOWED is False
    assert SftGate.FULL_14B_ALLOWED is False
    for gate in (
        _gate(),
        _gate(shortcoming_located=False, sample_count=0),
        _gate(sample_count=50),
    ):
        assert gate.dpo_allowed is False
        assert gate.rl_allowed is False
        assert gate.full_14b_allowed is False
    # 不在字段集合里，也不在序列化结果里（无法作为可配置项暴露）。
    for name in ("dpo_allowed", "rl_allowed", "full_14b_allowed"):
        assert name not in SftGate.model_fields
        assert name not in _gate().model_dump()


@pytest.mark.parametrize(
    "attribute", ["dpo_allowed", "rl_allowed", "full_14b_allowed"]
)
def test_ac18_instance_assignment_cannot_change_switches(attribute: str) -> None:
    gate = _gate()
    with pytest.raises((ValueError, AttributeError)):
        setattr(gate, attribute, True)
    assert getattr(gate, attribute) is False


@pytest.mark.parametrize(
    "name",
    [
        "dpo_allowed",
        "rl_allowed",
        "full_14b_allowed",
        "DPO_ALLOWED",
        "RL_ALLOWED",
        "FULL_14B_ALLOWED",
    ],
)
def test_ac18_dict_injection_is_rejected_by_extra_forbid(name: str) -> None:
    payload = _gate().model_dump()
    payload[name] = True
    with pytest.raises(ValidationError):
        SftGate.model_validate(payload)


def test_ac18_model_copy_injection_cannot_change_readonly_properties() -> None:
    """``model_copy(update=...)`` 绕过校验器，但 property 是数据描述符，仍恒 False。"""

    forged = _gate().model_copy(
        update={
            "dpo_allowed": True,
            "rl_allowed": True,
            "full_14b_allowed": True,
            "DPO_ALLOWED": True,
            "RL_ALLOWED": True,
            "FULL_14B_ALLOWED": True,
        }
    )
    assert forged.dpo_allowed is False
    assert forged.rl_allowed is False
    assert forged.full_14b_allowed is False
    # 类级常量本身未被实例注入污染。
    assert SftGate.DPO_ALLOWED is False
    assert SftGate.RL_ALLOWED is False
    assert SftGate.FULL_14B_ALLOWED is False


def test_ac18_object_setattr_on_property_is_rejected() -> None:
    gate = _gate()
    with pytest.raises(AttributeError):
        object.__setattr__(gate, "dpo_allowed", True)
    assert gate.dpo_allowed is False


def test_ac18_module_source_returns_literal_false() -> None:
    """property 返回字面 False，不读任何实例/类属性 → 无任何注入路径。"""

    source = REPORT_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SftGate"
    )
    for name in ("dpo_allowed", "rl_allowed", "full_14b_allowed"):
        func = next(
            node
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        returns = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        ]
        assert returns and returns[0].value.value is False, name


# --------------------------------------------------------------------------- #
# W10-Q7：SFT 只记录、不纳入门禁
# --------------------------------------------------------------------------- #


def test_w10_q7_sft_gate_is_record_only() -> None:
    """报告模型不含任何门禁判定字段；SftGate 不改变报告判定。"""

    assert not any(
        "publish" in name or "gate_decision" in name or "gate_passed" in name
        for name in BaselineReport.model_fields
    )
    cases: tuple = ()
    metrics = MetricsEngine().compute([])
    result = RunResult(run_id="", status=RunStatus.BLOCKED, cases=cases)
    plain = build_baseline_report(result=result, metrics=metrics)
    allowed_gate = _gate()
    with_gate = build_baseline_report(
        result=result, metrics=metrics, sft=allowed_gate
    )
    assert allowed_gate.sft_allowed is True  # 即便闸门允许，也不改报告判定
    for field in (
        "status",
        "model_commit",
        "metrics",
        "metric_denominators",
        "attribution_counts",
        "total_cases",
        "blocked_reason",
        "environment_blocked",
        "failure_cases",
        "confidence_intervals",
        "cost",
    ):
        assert getattr(with_gate, field) == getattr(plain, field), field
    assert len(with_gate.notes) == len(plain.notes) + 1
    assert any("SftGate 记录" in note for note in with_gate.notes)
    assert_no_fabricated_metrics(with_gate)


def test_w10_q7_note_never_claims_gate_decision() -> None:
    metrics = MetricsEngine().compute([])
    result = RunResult(run_id="", status=RunStatus.BLOCKED, cases=())
    report = build_baseline_report(result=result, metrics=metrics, sft=_gate())
    joined = " ".join(report.notes)
    assert "仅记录" in joined
    for banned in (
        "上游已通过",
        "回归全绿",
        "环境已清零",
        "Alpha 已就绪",
        "门禁通过",
        "W10 开工",
    ):
        assert banned not in joined
