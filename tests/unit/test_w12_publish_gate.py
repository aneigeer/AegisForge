"""W12 T05 单元测试：发布派生、验收报告与发布说明（AC-16 / AC-17 / AC-18）。

口径：`docs/w12/03-task-cards.md` T05、`docs/w12/02-design.md` §4.4/§5/§9.5、
`docs/w10-w12-lead-rulings.md` §4（W12-Q1/Q4/Q5）、§14.1（KD-W12-01）。

断言清单：

1. **AC-16①**：`decision=TECH_PREVIEW_ONLY` → `TECH_PREVIEW` 且 `release_name` 不含
   `alpha`（大小写不敏感）；**AC-16②**：`ALPHA_ALLOWED` → `ALPHA`，只写「Alpha」；
2. **W12-Q4 默认**：`None` → `TECH_PREVIEW` + `PUBLISH_DECISION_MISSING`；
   未知取值 → `UNKNOWN_DECISION_VALUE`（两者都 fail-closed 到 `TECH_PREVIEW`）；
3. **AC-17 纯函数**：签名只有 `decision`，无 `force_alpha`/`override`/任何 `force_*`；
   无 `utc_now`/文件/环境变量读取；`ReleaseGate` 无时间字段；同输入逐位相等；
4. **AC-18 诚实**：`model="baseline"`、`sft_reproducible_gain=False`、
   `dpo/full_14b/rl_deferred` 是 `ClassVar` 且 `True`、`baseline_score=None`（填数值即失败）；
5. **七类章节 + 延期项**：缺章/空章/未声明章节 → 构造失败；正文不得出现「完整 Alpha」；
6. **文档**：`alpha-acceptance-report.md` 与 `release-notes.md` 由同一 `ReleaseGate` 口径，
   `TECH_PREVIEW` 名称不含 alpha，限制清单随包发布。

本文件不连库、不读环境：报告组装用最小桩对象，只验证派生与结构。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.assurance.publish import Decision, PublishDecision
from core.release import AcceptanceStatus
from core.release.publish import (
    ALPHA_RELEASE_NAME,
    DECISION_ALPHA_ALLOWED,
    DECISION_TECH_PREVIEW_ONLY,
    FORBIDDEN_RELEASE_NAME_FRAGMENT,
    ISSUE_PUBLISH_DECISION_MISSING,
    ISSUE_UNKNOWN_DECISION_VALUE,
    SECTION_IDS,
    TECH_PREVIEW_RELEASE_NAME,
    AlphaAcceptanceReport,
    ReleaseGate,
    ReleaseType,
    ReleaseTypeDerivationError,
    build_alpha_acceptance_report,
    derive_release_type,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _Decision:
    """W11 `publish_decision` 的最小 duck-type 实现（不 import W11 代码）。"""

    decision: str
    failed_test_ids: tuple[str, ...] = ()
    environment_blocks: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# AC-16：类型与名称
# ---------------------------------------------------------------------------


def test_ac16_tech_preview_only_yields_tech_preview() -> None:
    gate = derive_release_type(decision=_Decision(DECISION_TECH_PREVIEW_ONLY))
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.release_name == TECH_PREVIEW_RELEASE_NAME
    assert "alpha" not in gate.release_name.casefold()
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "W11_PUBLISH_DECISION"
    assert gate.registered_issues == ()
    assert gate.decision_digest and len(gate.decision_digest) == 64


def test_ac16_alpha_allowed_yields_alpha_without_overclaim() -> None:
    gate = derive_release_type(decision=_Decision(DECISION_ALPHA_ALLOWED))
    assert gate.release_type is ReleaseType.ALPHA
    assert gate.release_name == ALPHA_RELEASE_NAME == "DBGuardian Alpha"
    assert gate.effective_decision == DECISION_ALPHA_ALLOWED
    assert gate.source == "W11_PUBLISH_DECISION"
    # 只写「Alpha」，不写「完整 Alpha」。
    assert FORBIDDEN_RELEASE_NAME_FRAGMENT not in gate.release_name


def test_w12q4_none_defaults_to_tech_preview() -> None:
    gate = derive_release_type(decision=None)
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_PUBLISH_DECISION_MISSING in gate.registered_issues
    assert gate.decision_digest is None


@pytest.mark.parametrize("value", ["", "  ", "alpha_allowed", "ALPHA", "TECH_PREVIEW", "FORCE_ALPHA"])
def test_w12q4_unknown_decision_fails_closed(value: str) -> None:
    gate = derive_release_type(decision=_Decision(value))
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_UNKNOWN_DECISION_VALUE in gate.registered_issues


# ---------------------------------------------------------------------------
# 真实 W11 枚举回归：``_decision_payload`` 不得再对 Enum 用裸 str()
# （Python 3.11+ 下 str(Decision.ALPHA_ALLOWED) == "Decision.ALPHA_ALLOWED"）
# ---------------------------------------------------------------------------


def _real_decision(value: Decision) -> PublishDecision:
    """构造真实 W11 `PublishDecision`（`decision` 是真实 Enum，不是裸 str 替身）。"""

    return PublishDecision(
        decision=value,
        failed_test_ids=(),
        red_line_failures=(),
        environment_blocks=(),
        reason="W12 枚举派生回归",
        inputs_digest="0" * 64,
    )


def test_real_enum_alpha_allowed_yields_alpha() -> None:
    gate = derive_release_type(decision=_real_decision(Decision.ALPHA_ALLOWED))
    assert gate.release_type is ReleaseType.ALPHA
    assert gate.release_type.value == "ALPHA"
    assert gate.effective_decision == DECISION_ALPHA_ALLOWED
    assert gate.release_name == ALPHA_RELEASE_NAME
    assert gate.source == "W11_PUBLISH_DECISION"
    assert gate.registered_issues == ()
    assert gate.decision_digest and len(gate.decision_digest) == 64


def test_real_enum_tech_preview_only_yields_tech_preview() -> None:
    gate = derive_release_type(decision=_real_decision(Decision.TECH_PREVIEW_ONLY))
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.release_type.value == "TECH_PREVIEW"
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.release_name == TECH_PREVIEW_RELEASE_NAME
    assert "alpha" not in gate.release_name.casefold()
    assert gate.source == "W11_PUBLISH_DECISION"
    assert gate.registered_issues == ()
    assert gate.decision_digest and len(gate.decision_digest) == 64


def test_bare_str_stub_and_real_enum_are_equivalent() -> None:
    """向后兼容：裸 str 替身与真实 Enum 在同一取值下派生结果逐位相等。"""

    for value in (Decision.ALPHA_ALLOWED, Decision.TECH_PREVIEW_ONLY):
        stub_gate = derive_release_type(decision=_Decision(value.value))
        real_gate = derive_release_type(decision=_real_decision(value))
        assert stub_gate == real_gate
        assert stub_gate.to_public_dict() == real_gate.to_public_dict()
        assert stub_gate.decision_digest == real_gate.decision_digest


@pytest.mark.parametrize("value", ["SOMETHING_ELSE", 123, object()])
def test_unknown_decision_value_fails_closed_without_exception(value: Any) -> None:
    """方向安全：未知/非法取值 → TECH_PREVIEW + 登记问题，且不抛异常、结果确定。"""

    probe = _Decision(value)  # type: ignore[arg-type]
    gate = derive_release_type(decision=probe)
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_UNKNOWN_DECISION_VALUE in gate.registered_issues
    assert gate.decision_digest and len(gate.decision_digest) == 64
    # 非字符串/非枚举兜底为空串，不把对象地址写进摘要 → 同入参逐位相等。
    assert derive_release_type(decision=probe) == gate


# ---------------------------------------------------------------------------
# O-W12-1：裸标量字符串不得被当成 W11 发布决定值（收窄 A 轮枚举修复的副作用）
#
# A 轮把 ``getattr(decision, "decision", decision)`` 作为兼容裸 str 替身的手段，
# 副作用是**没有 ``.decision`` 属性的裸标量**被回落成入参自身：
#   derive_release_type(decision="ALPHA_ALLOWED") → ALPHA + W11_PUBLISH_DECISION
# 这是唯一朝「更容易出 ALPHA」方向的变化，必须用模块级哨兵收回。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [DECISION_ALPHA_ALLOWED, DECISION_TECH_PREVIEW_ONLY],
)
def test_o_w12_1_bare_scalar_string_fails_closed(value: str) -> None:
    """裸标量（无 ``.decision`` 属性）→ TECH_PREVIEW + UNKNOWN_DECISION_VALUE。

    恢复 A 轮修复前的 fail-closed 行为；且必须与 ``decision is None`` 的
    ``PUBLISH_DECISION_MISSING`` 区分开（两者是不同登记项）。
    """

    gate = derive_release_type(decision=value)  # type: ignore[arg-type]
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.release_type.value == "TECH_PREVIEW"
    assert gate.release_name == TECH_PREVIEW_RELEASE_NAME
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_UNKNOWN_DECISION_VALUE in gate.registered_issues
    assert ISSUE_PUBLISH_DECISION_MISSING not in gate.registered_issues
    assert gate.decision_digest and len(gate.decision_digest) == 64
    # 裸标量不得出现 ALPHA，也不得冒用 W11 决定来源。
    assert "alpha" not in gate.release_name.casefold()
    assert gate.source != "W11_PUBLISH_DECISION"


def test_o_w12_1_bare_enum_member_without_decision_attribute_fails_closed() -> None:
    """裸 ``Decision`` 枚举成员本身不带 ``.decision`` 属性，同样必须 fail-closed。

    只有 W11 ``PublishDecision``（包住枚举的决定对象）才被接受，防止绕过
    ``publish_decision()`` 直接把枚举常量塞进派生链。
    """

    gate = derive_release_type(decision=Decision.ALPHA_ALLOWED)  # type: ignore[arg-type]
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_UNKNOWN_DECISION_VALUE in gate.registered_issues


def test_o_w12_1_stub_with_decision_attribute_still_yields_alpha() -> None:
    """收窄只挡「无 ``.decision`` 属性」的裸标量；既有裸 str 替身仍派生 ALPHA。"""

    gate = derive_release_type(decision=_Decision(DECISION_ALPHA_ALLOWED))
    assert gate.release_type is ReleaseType.ALPHA
    assert gate.release_type.value == "ALPHA"
    assert gate.release_name == ALPHA_RELEASE_NAME
    assert gate.effective_decision == DECISION_ALPHA_ALLOWED
    assert gate.source == "W11_PUBLISH_DECISION"
    assert gate.registered_issues == ()
    assert gate.decision_digest and len(gate.decision_digest) == 64


def test_o_w12_1_real_enum_decisions_remain_unaffected() -> None:
    """哨兵收窄不改变真实 W11 枚举的派生结果（ALPHA / TECH_PREVIEW 各一）。"""

    alpha_gate = derive_release_type(decision=_real_decision(Decision.ALPHA_ALLOWED))
    assert alpha_gate.release_type is ReleaseType.ALPHA
    assert alpha_gate.effective_decision == DECISION_ALPHA_ALLOWED
    assert alpha_gate.source == "W11_PUBLISH_DECISION"
    assert alpha_gate.registered_issues == ()

    preview_gate = derive_release_type(decision=_real_decision(Decision.TECH_PREVIEW_ONLY))
    assert preview_gate.release_type is ReleaseType.TECH_PREVIEW
    assert preview_gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert preview_gate.source == "W11_PUBLISH_DECISION"
    assert preview_gate.registered_issues == ()


def test_o_w12_1_decision_attribute_none_is_not_publish_decision_missing() -> None:
    """哨兵区分「缺属性」与「``.decision`` 值本身为 ``None``」。

    后者不是 ``decision is None``（裁定缺失），只能按未知取值 fail-closed，
    不得误记 ``PUBLISH_DECISION_MISSING``。
    """

    class _AttrNoneDecision:
        decision: None = None

    gate = derive_release_type(decision=_AttrNoneDecision())  # type: ignore[arg-type]
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert ISSUE_UNKNOWN_DECISION_VALUE in gate.registered_issues
    assert ISSUE_PUBLISH_DECISION_MISSING not in gate.registered_issues
    assert gate.decision_digest and len(gate.decision_digest) == 64


def test_o_w12_1_none_still_means_publish_decision_missing() -> None:
    """``decision is None`` 仍走既有默认分支，不与「缺属性」混为一谈。"""

    gate = derive_release_type(decision=None)
    assert gate.release_type is ReleaseType.TECH_PREVIEW
    assert gate.effective_decision == DECISION_TECH_PREVIEW_ONLY
    assert gate.release_name == TECH_PREVIEW_RELEASE_NAME
    assert gate.source == "DEFAULT_TECH_PREVIEW_ONLY"
    assert gate.registered_issues == (ISSUE_PUBLISH_DECISION_MISSING,)
    assert ISSUE_UNKNOWN_DECISION_VALUE not in gate.registered_issues
    assert gate.decision_digest is None


# ---------------------------------------------------------------------------
# AC-17：纯函数、不可覆盖
# ---------------------------------------------------------------------------


def test_ac17_signature_has_no_override_parameters() -> None:
    parameters = inspect.signature(derive_release_type).parameters
    assert list(parameters) == ["decision"]
    assert parameters["decision"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in parameters:
        assert not name.startswith("force"), name
        assert name not in {"override", "manual_override", "demo_passed", "ac_results"}, name


def test_ac17_is_pure_and_reproducible() -> None:
    source = inspect.getsource(derive_release_type)
    for forbidden in ("utc_now", "datetime", "open(", "os.environ", "getenv", "Path("):
        assert forbidden not in source, f"纯函数不得使用 {forbidden}"
    probe = _Decision(DECISION_TECH_PREVIEW_ONLY)
    first = derive_release_type(decision=probe)
    second = derive_release_type(decision=probe)
    assert first == second
    assert first.to_public_dict() == second.to_public_dict()
    # ReleaseGate 无时间字段。
    assert not any("time" in name or "date" in name for name in ReleaseGate.__dataclass_fields__)
    assert set(ReleaseGate.__dataclass_fields__) == {
        "release_type",
        "release_name",
        "effective_decision",
        "decision_digest",
        "registered_issues",
        "source",
    }


def test_ac17_gate_rejects_incoherent_combinations() -> None:
    with pytest.raises(ReleaseTypeDerivationError):
        ReleaseGate(
            release_type=ReleaseType.TECH_PREVIEW,
            release_name="DBGuardian Alpha",
            effective_decision=DECISION_TECH_PREVIEW_ONLY,
            decision_digest=None,
        )
    with pytest.raises(ReleaseTypeDerivationError):
        ReleaseGate(
            release_type=ReleaseType.ALPHA,
            release_name=f"DBGuardian {FORBIDDEN_RELEASE_NAME_FRAGMENT}",
            effective_decision=DECISION_ALPHA_ALLOWED,
            decision_digest="0" * 64,
        )
    with pytest.raises(ReleaseTypeDerivationError):
        ReleaseGate(
            release_type=ReleaseType.ALPHA,
            release_name=ALPHA_RELEASE_NAME,
            effective_decision=DECISION_TECH_PREVIEW_ONLY,
            decision_digest="0" * 64,
        )
    with pytest.raises(ReleaseTypeDerivationError):
        ReleaseGate(
            release_type=ReleaseType.TECH_PREVIEW,
            release_name=TECH_PREVIEW_RELEASE_NAME,
            effective_decision="ALPHA_ALLOWED",
            decision_digest=None,
        )


# ---------------------------------------------------------------------------
# AC-18：训练收益诚实
# ---------------------------------------------------------------------------


def _sections() -> dict[str, str]:
    return {key: f"{key} 已实测记录" for key in SECTION_IDS}


def test_ac18_baseline_and_deferred_flags() -> None:
    report = AlphaAcceptanceReport(sections=_sections(), evidence_refs=("docs/w11/02-design.md",))
    assert report.baseline_score is None
    assert report.model == "baseline"
    assert report.sft_reproducible_gain is False
    assert report.report_version == "w12-alpha-report-v1"
    for name in ("dpo_deferred", "full_14b_deferred", "rl_deferred"):
        assert name not in AlphaAcceptanceReport.model_fields
        assert getattr(AlphaAcceptanceReport, name) is True
        assert getattr(report, name) is True


@pytest.mark.parametrize("value", [0.0, 0, 0.42, 1.0])
def test_ac18_baseline_score_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(
            sections=_sections(), evidence_refs=("docs/w11/02-design.md",), baseline_score=value
        )


def test_ac18_no_model_upgrade_claim() -> None:
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(
            sections=_sections(), evidence_refs=("docs/w11/02-design.md",), model="sft"
        )
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(
            sections=_sections(),
            evidence_refs=("docs/w11/02-design.md",),
            sft_reproducible_gain=True,
        )


# ---------------------------------------------------------------------------
# 七类章节 + 延期项
# ---------------------------------------------------------------------------


def test_sections_must_be_exactly_eight() -> None:
    assert len(SECTION_IDS) == 8
    incomplete = dict(_sections())
    incomplete.pop("performance")
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(sections=incomplete, evidence_refs=("docs/w11/02-design.md",))
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(
            sections={**_sections(), "extra_section": "多余"},
            evidence_refs=("docs/w11/02-design.md",),
        )
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(
            sections={**_sections(), "cost": "   "},
            evidence_refs=("docs/w11/02-design.md",),
        )
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(sections=_sections(), evidence_refs=())


def test_report_rejects_forbidden_claim_in_sections() -> None:
    sections = dict(_sections())
    sections["known_issues"] = f"本次发布为{FORBIDDEN_RELEASE_NAME_FRAGMENT}版本"
    with pytest.raises(ValidationError):
        AlphaAcceptanceReport(sections=sections, evidence_refs=("docs/w11/02-design.md",))


# ---------------------------------------------------------------------------
# 报告组装（最小桩）
# ---------------------------------------------------------------------------


class _Closure:
    def __init__(self) -> None:
        self.status = AcceptanceStatus.FAILED
        self.verdict = "FAIL"
        self.reason_code = "OBSERVED_SHAPE_MISMATCH"
        self.assertion_verdicts = {
            "affected_rows": "UNKNOWN",
            "target_note_final": "PASS",
            "snapshot_comparable": "FAIL",
        }


class _Functional:
    def __init__(self) -> None:
        self.lifecycle = {phase: AcceptanceStatus.PASSED for phase in (
            "create", "query", "pause", "resume", "cancel"
        )}
        self.diagnosis_reports = ()
        self.repair_closure = _Closure()
        self.second_engine_readonly = True
        self.pg_segment_status = AcceptanceStatus.NOT_EXECUTED
        self.evidence_refs = ("core/release/functional.py",)


class _Security:
    def __init__(self) -> None:
        self.negative_review = ()
        self.r4_not_registered = True
        self.r3_denied = True
        self.bypass_rejections = {name: True for name in ("model", "sql_comment", "mcp_param", "ui")}
        self.unknown_no_retry = True
        self.fail_closed_ok = True
        self.redaction_ok = True
        self.knowledge_indexable_gate_ok = True
        self.w11_source_status = AcceptanceStatus.PASSED
        self.evidence_refs = ("core/release/security.py",)


class _Deploy:
    def __init__(self) -> None:
        self.checks = ()
        self.commit = "0" * 40
        self.image_digest = "UNKNOWN"
        self.artifact_hash = "1" * 64
        self.container_runtime_status = "NOT_VERIFIED"
        self.registered_issues = ()


class _Docs:
    def __init__(self) -> None:
        self.baseline_score = None
        self.baseline_model_status = "未微调"
        self.baseline_metrics_status = "指标未测得"
        self.baseline_blocked_reason = "BLOCKED_MODEL_ENV_MISSING"


def test_build_report_is_honest_and_covers_upstream_weeks() -> None:
    report = build_alpha_acceptance_report(
        functional=_Functional(),
        security=_Security(),
        deploy=_Deploy(),
        docs=_Docs(),
        decision=None,
        root=_REPO_ROOT,
    )
    assert set(report.sections) == set(SECTION_IDS)
    for text in report.sections.values():
        assert text.strip()
    assert FORBIDDEN_RELEASE_NAME_FRAGMENT not in " ".join(report.sections.values())
    # AC-03 红线必须写进「已知问题」，且不得表述为 verification PASS。
    known = report.sections["known_issues"]
    assert "KD-W12-01" in known
    assert "verification PASS" in known  # 只出现在「不得表述为」语境
    assert "FAILED" in known or "FAIL" in known
    # 模型收益口径。
    assert report.sections["model_gain"].count("指标未测得") == 1
    assert report.baseline_score is None and report.model == "baseline"
    # 证据引用覆盖 W09/W10/W11，且路径真实存在。
    refs = report.evidence_refs
    for week in ("docs/w11/", "docs/w10/", "docs/w09/"):
        assert any(ref.startswith(week) for ref in refs), week
    for ref in refs:
        assert (_REPO_ROOT / ref).is_file(), ref


def test_render_uses_gate_for_both_documents() -> None:
    from core.release.publish import _render_release_notes, _render_report_markdown

    gate_none = derive_release_type(decision=None)
    report = build_alpha_acceptance_report(
        functional=_Functional(),
        security=_Security(),
        deploy=_Deploy(),
        docs=_Docs(),
        decision=None,
        root=_REPO_ROOT,
    )
    report_md = _render_report_markdown(report, gate_none)
    notes_md = _render_release_notes(report, gate_none)
    for text in (report_md, notes_md):
        assert gate_none.release_type.value in text
        assert gate_none.release_name in text
        assert "alpha" not in gate_none.release_name.casefold()
        assert FORBIDDEN_RELEASE_NAME_FRAGMENT not in text
        assert "known-limitations.json" in text
    # ALPHA 分支不得改变「完整 Alpha」禁令。
    gate_alpha = derive_release_type(decision=_Decision(DECISION_ALPHA_ALLOWED))
    alpha_md = _render_release_notes(report, gate_alpha)
    assert gate_alpha.release_name in alpha_md
    assert FORBIDDEN_RELEASE_NAME_FRAGMENT not in alpha_md


# ---------------------------------------------------------------------------
# 已落盘文档
# ---------------------------------------------------------------------------


def test_release_documents_written_and_consistent() -> None:
    report_path = _REPO_ROOT / "docs" / "w12" / "alpha-acceptance-report.md"
    notes_path = _REPO_ROOT / "docs" / "w12" / "release-notes.md"
    assert report_path.is_file()
    assert notes_path.is_file()

    report_text = report_path.read_text(encoding="utf-8")
    notes_text = notes_path.read_text(encoding="utf-8")
    for title in ("功能验收", "安全验收", "可靠性", "性能", "模型收益", "成本", "已知问题", "延期项"):
        assert title in report_text, title
    for text in (report_text, notes_text):
        assert "KD-W12-01" in text
        assert FORBIDDEN_RELEASE_NAME_FRAGMENT not in text
        assert "known-limitations.json" in text
    assert "TECH_PREVIEW" in notes_text
    assert "alpha" not in notes_text.splitlines()[2].casefold()
