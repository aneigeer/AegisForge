"""W12 T06 单元测试：表述禁令扫描、demo 排除、限制不可删与写面回归（AC-19…AC-22）。

口径：`docs/w12/03-task-cards.md` T06、`docs/w12/02-design.md` §7/§8、
`docs/w10-w12-lead-rulings.md` §4（W12-Q7/Q8）。

断言清单：

1. **AC-19**：`scan_claims(root)` 的三范围固定（11 严格 / 2 `spec_mode` / 1 排除）、
   `violations` 与 `demo_input_violations` 为空、`limitation_integrity_ok` 与
   `evidence_refs_ok` 为 `True`、`allowed_phrases` 含四条固定短语；
   扫描器**可证伪**：临时件里的肯定式表述会被捕到，否定语境行被豁免；
2. **AC-20**：`AcceptanceEvidence` 与三个结果模型无 demo 字段；`assert_no_demo_input`
   命中 → `DEMO_AS_ACCEPTANCE`；`assert_evidence_refs` 覆盖 W11/W10/W09 且路径真实存在；
3. **AC-21**：`assert_limitation_integrity` 对落盘清单通过；删自动项 → `LIMITATION_REMOVED`；
4. **AC-22**：`check_write_surface()` 实测 observer 10 全 `read`、PG 7 全 `read`、
   R3/R4 拒绝生效；**未提供注册表时 `single_write_tool` 记 `False`（"未核对"不冒充通过）**；
5. **报告**：`claim-scan-report.md` 记录三范围、排除理由、豁免计数与固定短语，
   **不静默跳过任何对外材料**（缺件记为违规）。

本文件不连库、不改任何生产文件；AC-22 的既存不变量用例由验收命令另行串行执行。

**交付件写入纪律**：本文件的 `scan_claims()` / `build_full_known_limitations()`
/ `assert_limitation_integrity()` 全是纯派生/只读调用（传 `_REPO_ROOT` 不产生
任何写入）；`docs/w12/**` 交付件只做**只读比对**（不一致即 FAIL，绝不写回仓库）。
生成交付件的调用集中在 `test_w12_doc_inventory.py`，已改为写 `tmp_path`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.release.claims import (
    ALLOWED_PHRASES,
    _scan_unit,
    CORR01_NEGATIONS,
    EXEMPTION_CONTEXT_CHARS,
    NEGATION_TOLERANT_FILES,
    EXCLUDED_FILES,
    NEGATION_TOLERANT_FILES,
    SPEC_MODE_FILES,
    STRICT_SCAN_FILES,
    W12_EXTRA_FORBIDDEN,
    AcceptanceEvidence,
    AcceptanceIntegrityError,
    assert_evidence_refs,
    assert_limitation_integrity,
    assert_no_demo_input,
    check_write_surface,
    scan_claims,
)
from core.release.docs import KnownLimitations, build_full_known_limitations, items_digest
from core.release.functional import DEMO_INPUT_FIELDS

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def scan() -> object:
    return scan_claims(_REPO_ROOT)


# ---------------------------------------------------------------------------
# AC-19：双范围扫描
# ---------------------------------------------------------------------------


def test_ac19_scan_result_is_clean(scan: object) -> None:
    """四个判据全绿：无违规、无 demo 输入、限制完整、证据引用完整。"""
    assert scan.violations == (), list(scan.violations)[:5]
    assert scan.demo_input_violations == ()
    assert scan.limitation_integrity_ok is True
    assert scan.evidence_refs_ok is True
    assert scan.ok is True


def test_ac19_three_scopes_are_fixed_and_complete(scan: object) -> None:
    """三范围固定：11 严格 / 2 `spec_mode` / 1 排除（`01-prd.md`）。"""
    assert len(STRICT_SCAN_FILES) == 11
    assert scan.scanned_files == STRICT_SCAN_FILES
    assert scan.spec_mode_files == SPEC_MODE_FILES == (
        "docs/w12/02-design.md",
        "docs/w12/03-task-cards.md",
    )
    assert scan.excluded_files == EXCLUDED_FILES == ("docs/w12/01-prd.md",)
    for relative in scan.scanned_files + scan.spec_mode_files:
        assert (_REPO_ROOT / relative).is_file(), f"扫描目标缺失：{relative}"


def test_ac19_allowed_phrases_are_frozen(scan: object) -> None:
    """固定允许短语必须原样在场（含 W07 权威口径）。"""
    assert scan.allowed_phrases == ALLOWED_PHRASES
    assert "有条件放行；门禁通过：否" in scan.allowed_phrases
    assert "配置层已实测 / 运行态 NOT_VERIFIED" in scan.allowed_phrases


def test_ac19_negation_tolerant_files_are_limitation_lists_only() -> None:
    """限制清单例外仅限两个文件（其余对外材料严格零容忍）。"""
    assert set(NEGATION_TOLERANT_FILES) == {
        "docs/w12/known-limitations.json",
        "docs/w12/known-limitations.md",
    }
    assert set(NEGATION_TOLERANT_FILES) <= set(STRICT_SCAN_FILES)


def _stub_root(tmp_path: Path) -> Path:
    """构造最小可扫描根：所有对外材料在场（`.json` 写合法 `{}`），以便单点注入。"""
    for relative in STRICT_SCAN_FILES + SPEC_MODE_FILES + EXCLUDED_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "{}\n" if relative.endswith(".json") else "# stub\n"
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_ac19_positive_claim_is_caught(tmp_path: Path) -> None:
    """可证伪：肯定式表述必须被捕到。"""
    root = _stub_root(tmp_path)
    (root / "docs" / "w12" / "release-notes.md").write_text(
        "# 发布说明\n\n本次发布" + W12_EXTRA_FORBIDDEN[0] + "。\n", encoding="utf-8"
    )
    result = scan_claims(root)
    assert any("release-notes.md" in item for item in result.violations), result.violations
    assert result.ok is False


def test_ac19_negated_claim_in_spec_mode_is_exempted(tmp_path: Path) -> None:
    """`spec_mode` 否定语境豁免生效（规范引用不算对外宣称）。"""
    root = _stub_root(tmp_path)
    (root / "docs" / "w12" / "03-task-cards.md").write_text(
        "# 任务卡\n\n- 本卡不得写" + W12_EXTRA_FORBIDDEN[0] + "。\n", encoding="utf-8"
    )
    result = scan_claims(root)
    assert result.violations == (), result.violations
    assert result.exempted_lines >= 1


def test_ac19_missing_material_is_a_violation_not_a_silent_skip(tmp_path: Path) -> None:
    """缺件记违规（不静默跳过任何对外材料）。"""
    root = _stub_root(tmp_path)
    (root / "docs" / "w12" / "security-materials.md").unlink()
    result = scan_claims(root)
    assert any("security-materials.md" in item for item in result.violations)
    assert any("SCAN_TARGET_MISSING" in item for item in result.violations)


# ---------------------------------------------------------------------------
# W12-CORR-01：值/句级否定语境豁免（台账 §15）
# ---------------------------------------------------------------------------


def test_corr01_constants_follow_ruling() -> None:
    """否定词闭集 / 豁免窗口 / 三条精确短语逐条与裁定一致。"""
    assert EXEMPTION_CONTEXT_CHARS == 12
    assert set(CORR01_NEGATIONS) == {
        "不得",
        "禁止",
        "不能",
        "不应",
        "避免",
        "不写",
        "不含",
        "禁用",
        "不声称",
    }
    # §15.2 第 3 条口径微调：allowed_phrases 维持 4 条，不追加精确短语白名单。
    assert len(ALLOWED_PHRASES) == 4
    for phrase in (
        "不得表述为完整 Alpha",
        "不得表述为 verification PASS",
        "不得表述为修复闭环已通过独立验证",
    ):
        assert phrase not in ALLOWED_PHRASES
    assert set(NEGATION_TOLERANT_FILES) == {
        "docs/w12/known-limitations.json",
        "docs/w12/known-limitations.md",
    }


def _write_limitation_json(root: Path, statements: list[str]) -> None:
    payload = {"items": [{"item_id": f"X-{index}", "statement": text} for index, text in enumerate(statements)]}
    (root / "docs" / "w12" / "known-limitations.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_corr01_negated_statement_is_exempted_with_trace(tmp_path: Path) -> None:
    """① 否定式披露被豁免，且**逐条留痕**（文件 / 值路径 / 命中 / 依据）。"""
    root = _stub_root(tmp_path)
    _write_limitation_json(root, ["发布结论为 TECH_PREVIEW_ONLY，不得表述为完整 Alpha。"])
    result = scan_claims(root)
    assert result.violations == (), result.violations
    assert result.exempted_lines == 1
    trace = result.exempted_items[0]
    assert "known-limitations.json" in trace
    assert "$.items[0].statement" in trace
    assert "W12_EXTRA:完整 Alpha" in trace
    assert "NEGATION_WINDOW:不得" in trace or "ALLOWED_PHRASE" in trace


def test_corr01_affirmative_rewrite_is_a_violation(tmp_path: Path) -> None:
    """② 证伪负测之一：改成肯定宣称（`已达成完整 Alpha`）→ 必须违规。"""
    root = _stub_root(tmp_path)
    _write_limitation_json(root, ["本次发布已达成完整 Alpha。"])
    result = scan_claims(root)
    assert any("完整 Alpha" in item for item in result.violations), result.violations
    assert result.exempted_lines == 0
    assert result.ok is False


def test_corr01_phrase_without_negation_is_a_violation(tmp_path: Path) -> None:
    """③ 证伪负测之二：删掉否定词只留短语 → 必须违规（豁免不是盲的）。"""
    root = _stub_root(tmp_path)
    _write_limitation_json(root, ["发布结论为完整 Alpha。"])
    result = scan_claims(root)
    assert any("完整 Alpha" in item for item in result.violations), result.violations
    assert result.ok is False


def test_corr01_json_is_scanned_per_value_not_per_line(tmp_path: Path) -> None:
    """④ 粒度：`.json` 按**值**判定——同一个值内否定即豁免，另一个值没有否定即违规。"""
    root = _stub_root(tmp_path)
    _write_limitation_json(
        root,
        ["不得表述为完整 Alpha。", "本次发布为完整 Alpha 版本。"],
    )
    result = scan_claims(root)
    flagged = [item for item in result.violations if "完整 Alpha" in item]
    assert len(flagged) == 1, result.violations
    assert "$.items[1].statement" in flagged[0]
    assert result.exempted_lines == 1


def test_corr01_exemption_does_not_spill_over(tmp_path: Path) -> None:
    """⑤ 外溢禁止：同样一句否定式放进**非限制清单**对外件仍算违规。"""
    root = _stub_root(tmp_path)
    (root / "docs" / "w12" / "release-notes.md").write_text(
        "# 发布说明\n\n不得表述为完整 Alpha。\n", encoding="utf-8"
    )
    result = scan_claims(root)
    assert any("release-notes.md" in item for item in result.violations), result.violations
    assert result.ok is False


def test_corr01_negation_prefix_is_also_exempted(tmp_path: Path) -> None:
    """⑥ 条件②：该值以否定词开头时也豁免（短语不在同句但仍在否定语境）。"""
    root = _stub_root(tmp_path)
    _write_limitation_json(root, ["不得作出如下宣称：完整 Alpha。"])
    result = scan_claims(root)
    assert result.violations == (), result.violations
    assert result.exempted_lines == 1
    assert "NEGATION_" in result.exempted_items[0]


# ---------------------------------------------------------------------------
# W12-CORR-01 §15.2 第 5 条：**两条证伪负测 + 一条反向对照**（内核级）
#
# 说明：实现里 `_line_hits(text, negations=...)` 在 CORR-01 重构中拆成了
# `_hits_in_text()` + `_exemption_basis()`，对外入口等价物是
# `_scan_unit(unit, exemptable=True, label=...) -> (violations, exemptions)`；
# 本组用例直接对该内核断言（集成级对应用例见 `test_corr01_*` 系列）。
# ---------------------------------------------------------------------------


def test_corr01_kernel_affirmative_rewrite_violates() -> None:
    """① 证伪负测：改成肯定宣称（无否定词）→ 必须计入 `violations`。"""
    violations, exemptions = _scan_unit(
        "本版本已达成完整 Alpha", exemptable=True, label="probe"
    )
    assert any("完整 Alpha" in item for item in violations), violations
    assert exemptions == []


def test_corr01_kernel_negation_removed_violates() -> None:
    """② 证伪负测：原句删掉否定词、只留短语 → 必须计入 `violations`。"""
    violations, exemptions = _scan_unit("完整 Alpha", exemptable=True, label="probe")
    assert any("完整 Alpha" in item for item in violations), violations
    assert exemptions == []


def test_corr01_kernel_original_sentence_is_exempted() -> None:
    """③ 反向对照：原句 `不得表述为完整 Alpha` → 豁免 1 条且不进 `violations`。"""
    violations, exemptions = _scan_unit(
        "不得表述为完整 Alpha", exemptable=True, label="probe"
    )
    assert violations == [], violations
    assert len(exemptions) == 1
    assert "NEGATION_WINDOW:不得" in exemptions[0]


# ---------------------------------------------------------------------------
# AC-20：demo 输入排除 + 证据引用
# ---------------------------------------------------------------------------


def test_ac20_no_demo_fields_in_result_models() -> None:
    """四个模型的字段集与 demo 键交集为空。"""
    from core.release.functional import FunctionalAcceptanceResult
    from core.release.publish import AlphaAcceptanceReport
    from core.release.security import SecurityAcceptanceResult

    for model in (
        FunctionalAcceptanceResult,
        SecurityAcceptanceResult,
        AlphaAcceptanceReport,
        AcceptanceEvidence,
    ):
        fields = set(getattr(model, "model_fields", {})) or set(model.__dataclass_fields__)
        assert set(DEMO_INPUT_FIELDS) & fields == set(), model.__name__


def test_ac20_demo_input_is_rejected() -> None:
    """`demo_passed` 进 AC 判定输入 → `DEMO_AS_ACCEPTANCE`。"""
    payload = {
        "ac_id": "AC-01",
        "evidence_refs": ["docs/w11/02-design.md"],
        "status": "PASSED",
        "demo_passed": True,
    }
    with pytest.raises(AcceptanceIntegrityError) as excinfo:
        assert_no_demo_input(payload)
    assert excinfo.value.code == "DEMO_AS_ACCEPTANCE"
    # 干净载荷通过。
    assert_no_demo_input({"ac_id": "AC-01", "evidence_refs": [], "status": "PASSED"})


def test_ac20_evidence_refs_must_cover_three_weeks() -> None:
    """缺任一周 → `EVIDENCE_REF_INCOMPLETE`；路径不存在 → `EVIDENCE_REF_MISSING_FILE`。"""
    assert_evidence_refs(
        ("docs/w11/02-design.md", "docs/w10/02-design.md", "docs/w09/12-w09-qa-closeout.md")
    )
    with pytest.raises(AcceptanceIntegrityError) as excinfo:
        assert_evidence_refs(("docs/w11/02-design.md", "docs/w10/02-design.md"))
    assert excinfo.value.code == "EVIDENCE_REF_INCOMPLETE"
    assert "W09" in str(excinfo.value.details)
    with pytest.raises(AcceptanceIntegrityError) as excinfo:
        assert_evidence_refs(
            (
                "docs/w11/02-design.md",
                "docs/w10/02-design.md",
                "docs/w09/no-such-file.md",
            )
        )
    assert excinfo.value.code == "EVIDENCE_REF_MISSING_FILE"


def test_ac20_report_evidence_refs_are_verified(scan: object) -> None:
    """落盘报告的 `evidence_refs` 已通过三周覆盖与存在性校验。"""
    assert scan.evidence_refs_ok is True
    text = (_REPO_ROOT / "docs" / "w12" / "alpha-acceptance-report.md").read_text(encoding="utf-8")
    for week in ("docs/w11/", "docs/w10/", "docs/w09/"):
        assert week in text, week


# ---------------------------------------------------------------------------
# AC-21：限制清单不可删
# ---------------------------------------------------------------------------


def test_ac21_limitation_integrity_passes_on_disk() -> None:
    """落盘清单（16 项 = 13 AUTO + 3 MANUAL，`KD-W12-01/02` 均已关闭）通过 AC-21 校验。"""
    limitations = build_full_known_limitations(_REPO_ROOT)
    assert_limitation_integrity(limitations)
    assert len(limitations.items) == 16
    payload = json.loads(
        (_REPO_ROOT / "docs" / "w12" / "known-limitations.json").read_text(encoding="utf-8")
    )
    assert KnownLimitations.model_validate(payload).items_digest == payload["items_digest"]
    # 只读对账：仓库落盘件必须与代码派生清单一致（失败即 FAIL，绝不写回仓库）。
    assert payload["items_digest"] == limitations.items_digest
    assert payload["w11_source_digest"] == limitations.w11_source_digest


def test_ac21_removal_is_rejected() -> None:
    """删除任一自动项 → 构造期 `LIMITATION_REMOVED`；绕过构造器的篡改也被函数层拦下。"""
    limitations = build_full_known_limitations(_REPO_ROOT)
    kept = tuple(item for item in limitations.items if item.item_id != "ENV-RESIDUE")

    # ① 模型层（构造期）：直接构造即被校验器拒绝。
    with pytest.raises(ValidationError) as excinfo:
        KnownLimitations(
            items=kept,
            w11_source_status=limitations.w11_source_status,
            w11_source_digest=limitations.w11_source_digest,
            items_digest=items_digest(kept),
        )
    assert "LIMITATION_REMOVED" in str(excinfo.value)

    # ② 函数层：`model_construct` 绕过构造器后，`assert_limitation_integrity` 仍要拒绝。
    tampered = KnownLimitations.model_construct(
        version=limitations.version,
        items=kept,
        w11_source_status=limitations.w11_source_status,
        w11_source_digest=limitations.w11_source_digest,
        items_digest=items_digest(kept),
        source_path=limitations.source_path,
    )
    with pytest.raises(AcceptanceIntegrityError) as excinfo:
        assert_limitation_integrity(tampered)
    assert excinfo.value.code == "LIMITATION_REMOVED"
    assert "ENV-RESIDUE" in str(excinfo.value.details)


# ---------------------------------------------------------------------------
# AC-22：写面不回退
# ---------------------------------------------------------------------------


def test_ac22_write_surface_is_unchanged() -> None:
    """observer 10 全 `read`、PG 7 全 `read`、R3/R4 拒绝生效；未装配注册表时不冒充通过。"""
    verdicts = check_write_surface()
    assert verdicts["observer_all_read"] is True
    assert verdicts["pg_all_read"] is True
    assert verdicts["r3_r4_denied"] is True
    # 未提供 LAB 注册表 → 记为"未核对"，绝不冒充 True。
    assert verdicts["single_write_tool"] is False


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def test_claim_scan_report_documents_everything() -> None:
    """报告记录三范围、排除理由、豁免计数、固定短语，且不自称"全绿"式结论。"""
    path = _REPO_ROOT / "docs" / "w12" / "claim-scan-report.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    for relative in STRICT_SCAN_FILES:
        assert relative in text, relative
    for relative in SPEC_MODE_FILES:
        assert relative in text, relative
    assert "docs/w12/01-prd.md" in text
    assert "排除理由" in text
    assert "exempted_lines" in text
    for phrase in ALLOWED_PHRASES:
        assert phrase in text, phrase
    assert "有条件放行；门禁通过：否" in text
    assert "W09 门禁未评" in text
