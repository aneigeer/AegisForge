"""W12 T04 单元测试：文档清单、OpenAPI 3.1 与已知限制清单（AC-13 / AC-14 / AC-15）。

口径：`docs/w12/03-task-cards.md` T04、`docs/w12/02-design.md` §4.3/§6/§9.3–§9.5、
`docs/w10-w12-lead-rulings.md` §4（W12-Q5/Q8）、§13（KD-W11-01/02/03）、§14（KD-W12-01/02）。

断言清单：

1. **AC-13**：`export_openapi_31()` 实测 `openapi` 主版本 `3.1`、导出件落
   `docs/w12/openapi.json` 且与合同一致；四类文档清单逐项 `exists is True`，
   `PARTIAL` 项必须写明缺什么（不声称齐全）；
2. **AC-14**：`baseline_score is None` + `未微调` / `指标未测得` / `BLOCKED_MODEL_ENV_MISSING`，
   填入任何数值 → 构造失败；
3. **AC-15**：六类不承诺能力逐条在清单内并带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`；
4. **13 自动项不可删（W12-Q8）**：删一项 → 失败；改 marker/statement → 失败；
   人工项复用自动 id → 失败；人工项缺 `added_by`/`reason` → 失败；
5. **W11 同源对账**：`w11_source_status=PASSED` + `w11_source_digest` 等于 W11 六项摘要；
   文件缺失时走常量 fallback → `NOT_AVAILABLE`（不伪造 W11 输出）；
6. **单一数据源**：`known-limitations.md` 内嵌 JSON 块与 `.json` 逐字一致，
   `items_digest` 可对账；3 条在册已知偏离（KD-W11-01/02/03）为 `origin=MANUAL`，
   `KD-W12-01`（2026-11-17）与 `KD-W12-02`（2026-11-19）已关闭、只在 `CLOSED_DEVIATIONS` 留档。

本文件只读文件与 `create_app().openapi()`：不新增端点、不改 `apps/api/main.py`、
不改 `docs/migrations.md` / `README.md` / `.env.example` / `benchmarks/**`。

**交付件写入纪律**：所有「生成交付件」的调用（`export_openapi_31` /
`write_known_limitations`）一律输出到 `tmp_path`，仓库里的 `docs/w12/**`
交付件只做**只读逐字节比对**（不一致即 FAIL，绝不写回仓库），避免跑单测时
重写仓库文件产生无意义 diff。`build_full_known_limitations()` /
`collect_doc_inventory()` / `read_w11_limitations()` / `scan_claims()` 是纯
派生/只读函数，传 `_REPO_ROOT` 不产生任何写入。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.release import AcceptanceStatus
from core.release.docs import (
    AUTO_ITEM_IDS,
    BASELINE_BLOCKED_REASON,
    CLOSED_DEVIATIONS,
    DOC_PARTIAL_ITEM_ID,
    KNOWN_DEVIATIONS,
    LIMITATIONS_JSON,
    LIMITATIONS_MD,
    MANUAL_ADDED_BY,
    NON_PROMISES,
    W11_AUTO_BASE_SPEC,
    DocCategory,
    DocEntry,
    DocInventory,
    KnownLimitations,
    LimitationItem,
    LimitationMarker,
    LimitationOrigin,
    append_manual_limitation,
    build_full_known_limitations,
    build_known_limitations,
    collect_doc_inventory,
    export_openapi_31,
    items_digest,
    known_deviation_items,
    read_w11_limitations,
    write_known_limitations,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# AC-13：OpenAPI 3.1
# ---------------------------------------------------------------------------


def test_ac13_openapi_is_31_and_exported(tmp_path: Path) -> None:
    """`openapi` 主版本必须是 3.1，导出件与合同一致。

    导出只写 `tmp_path`；仓库落盘件仅做**只读逐字节比对**（不一致即 FAIL），
    绝不写回仓库，避免跑单测时重写交付件产生无意义 diff。
    """
    contract = export_openapi_31(tmp_path)
    assert contract.openapi_version.startswith("3.1"), contract.openapi_version
    assert contract.source == "apps.api.main.create_app().openapi()"
    assert contract.exported_path == "docs/w12/openapi.json"
    assert contract.info_title
    assert contract.info_version
    assert contract.path_count > 0

    exported_path = tmp_path / "docs" / "w12" / "openapi.json"
    assert exported_path.is_file()
    exported = json.loads(exported_path.read_text(encoding="utf-8"))
    assert exported["openapi"] == contract.openapi_version
    assert exported["info"]["title"] == contract.info_title
    assert exported["info"]["version"] == contract.info_version
    assert len(exported["paths"]) == contract.path_count
    # 路径数以**导出件**为准（P1-BYOK 新增 4 条 model-providers 路由后由 31 → 35）；
    # 这里只断言导出件与运行期一致，避免写死数字后与后续 Pillar 漂移。
    assert contract.path_count == len(exported["paths"])
    assert contract.path_count >= 31

    # 只读对账：代码导出结果必须与仓库交付件逐字节一致（失败即 FAIL，不写回）。
    repo_path = _REPO_ROOT / "docs" / "w12" / "openapi.json"
    assert repo_path.is_file()
    assert repo_path.read_bytes() == exported_path.read_bytes(), (
        "docs/w12/openapi.json 与 export_openapi_31() 导出结果不一致"
    )


# ---------------------------------------------------------------------------
# AC-13 / AC-14：四类文档清单与基线
# ---------------------------------------------------------------------------


def test_ac13_doc_inventory_covers_four_categories() -> None:
    """四类文档齐全且逐项存在（`exists is True`）。"""
    inventory = collect_doc_inventory(_REPO_ROOT)
    assert {entry.category for entry in inventory.entries} == set(DocCategory)
    for entry in inventory.entries:
        assert entry.exists is True, f"文档缺失：{entry.path}"
    counts = {category: 0 for category in DocCategory}
    for entry in inventory.entries:
        counts[entry.category] += 1
    assert all(count > 0 for count in counts.values()), counts


def test_ac13_partial_entries_are_registered_not_hidden() -> None:
    """完整性滞后必须如实标 `PARTIAL` 并写明缺项（不声称齐全）。"""
    inventory = collect_doc_inventory(_REPO_ROOT)
    partial = [entry for entry in inventory.entries if entry.status == "PARTIAL"]
    assert partial, "冻结缺件必须以 PARTIAL 登记"
    for entry in partial:
        assert entry.note.strip(), entry.path
    by_path = {entry.path: entry for entry in inventory.entries}
    # 迁移说明已补齐 008–011：按实测转为 EXISTS，但仍在 note 里写清口径。
    assert by_path["docs/migrations.md"].status == "EXISTS"
    assert "011" in by_path["docs/migrations.md"].note
    assert by_path["docs/w07/07-gate-review.md"].status == "PARTIAL"
    assert "05-freeze.md" in by_path["docs/w07/07-gate-review.md"].note


def test_ac14_baseline_is_not_fabricated() -> None:
    """`baseline_score is None` + 三个冻结字面量；填数值即构造失败。"""
    inventory = collect_doc_inventory(_REPO_ROOT)
    assert inventory.baseline_score is None
    assert inventory.baseline_model_status == "未微调"
    assert inventory.baseline_metrics_status == "指标未测得"
    assert inventory.baseline_blocked_reason == BASELINE_BLOCKED_REASON == "BLOCKED_MODEL_ENV_MISSING"

    entries = inventory.entries
    with pytest.raises(ValidationError):
        DocInventory(entries=entries, baseline_score=0.0)
    with pytest.raises(ValidationError):
        DocInventory(entries=entries, baseline_blocked_reason="ENV-NV1")

    results = json.loads(
        (_REPO_ROOT / "benchmarks" / "baseline_v0" / "results.json").read_text(encoding="utf-8")
    )
    # 只读复核：本文件未改，故基线仍是未执行状态。
    assert results["status"] == "NOT_RUN"
    assert results["scores"] is None
    assert results["metrics"] is None


def test_ac13_partial_entry_requires_note() -> None:
    """`PARTIAL` 无说明 → 拒绝；`EXISTS` 与不存在冲突 → 拒绝。"""
    with pytest.raises(ValidationError):
        DocEntry(
            path="docs/x.md",
            category=DocCategory.API_CONTRACT,
            exists=True,
            status="PARTIAL",
        )
    with pytest.raises(ValidationError):
        DocEntry(
            path="docs/x.md",
            category=DocCategory.API_CONTRACT,
            exists=False,
            status="EXISTS",
        )


# ---------------------------------------------------------------------------
# AC-15 + W12-Q8：限制清单
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def limitations() -> KnownLimitations:
    return build_full_known_limitations(_REPO_ROOT)


def test_ac15_auto_items_are_exactly_13(limitations: KnownLimitations) -> None:
    """13 个自动项 = W11 六项 + 六类不承诺 + `DOC-PARTIAL`。"""
    assert len(AUTO_ITEM_IDS) == 13
    auto_ids = {item.item_id for item in limitations.items if item.origin is LimitationOrigin.AUTO}
    assert auto_ids == set(AUTO_ITEM_IDS)
    assert {spec["item_id"] for spec in W11_AUTO_BASE_SPEC} <= auto_ids
    assert {item_id for item_id, _cap, _marker in NON_PROMISES} <= auto_ids
    assert DOC_PARTIAL_ITEM_ID in auto_ids


def test_ac15_non_promises_carry_markers(limitations: KnownLimitations) -> None:
    """六类不承诺能力逐条入清单且带标记（NOT_SUPPORTED / PARTIAL）。"""
    by_id = {item.item_id: item for item in limitations.items}
    markers = {
        "NP-PROD-AUTO-WRITE": "NOT_SUPPORTED",
        "NP-GENERIC-AUTO-REPAIR": "NOT_SUPPORTED",
        "NP-ALWAYS-DIAGNOSE": "NOT_SUPPORTED",
        "NP-FULL-DEADLOCK-INDEX": "PARTIAL",
        "NP-EXACTLY-ONCE": "NOT_SUPPORTED",
        "NP-NETWORK-ISOLATION-VERIFIED": "NOT_SUPPORTED",
    }
    assert set(markers) == {item_id for item_id, _cap, _marker in NON_PROMISES}
    for item_id, marker in markers.items():
        assert by_id[item_id].marker.value == marker
        assert by_id[item_id].origin is LimitationOrigin.AUTO
        assert by_id[item_id].statement.strip()
    assert by_id[DOC_PARTIAL_ITEM_ID].marker is LimitationMarker.PARTIAL


def test_q8_removing_auto_item_is_rejected(limitations: KnownLimitations) -> None:
    """删除任一自动项 → `LIMITATION_REMOVED`（以 `ValidationError` 承载，W11 L-6）。"""
    kept = tuple(item for item in limitations.items if item.item_id != "ENV-PG5432")
    with pytest.raises(ValidationError) as excinfo:
        KnownLimitations(
            items=kept,
            w11_source_status=limitations.w11_source_status,
            w11_source_digest=limitations.w11_source_digest,
            items_digest=items_digest(kept),
        )
    assert "LIMITATION_REMOVED" in str(excinfo.value)


def test_q8_modifying_auto_item_is_rejected(limitations: KnownLimitations) -> None:
    """改写自动项 marker/statement → `LIMITATION_MODIFIED`。"""
    mutated = tuple(
        item.model_copy(update={"statement": "被改写"})
        if item.item_id == "ENV-NV1"
        else item
        for item in limitations.items
    )
    with pytest.raises(ValidationError) as excinfo:
        KnownLimitations(
            items=mutated,
            w11_source_status=limitations.w11_source_status,
            w11_source_digest=limitations.w11_source_digest,
            items_digest=items_digest(mutated),
        )
    assert "LIMITATION_MODIFIED" in str(excinfo.value)


def test_q8_manual_item_cannot_reuse_auto_id(limitations: KnownLimitations) -> None:
    """人工项复用自动 `item_id` → `LIMITATION_ID_CONFLICT`。"""
    with pytest.raises(ValueError):
        append_manual_limitation(
            limitations,
            item=LimitationItem(
                item_id="ENV-NV1",
                marker=LimitationMarker.PARTIAL,
                origin=LimitationOrigin.MANUAL,
                statement="重复 id",
                added_by="user_w12",
                reason="负测",
            ),
        )


def test_manual_item_requires_attribution() -> None:
    """人工项缺 `added_by` / `reason` → 构造失败。"""
    with pytest.raises(ValidationError):
        LimitationItem(
            item_id="KD-X",
            marker=LimitationMarker.PARTIAL,
            origin=LimitationOrigin.MANUAL,
            statement="无登记人",
            reason="有原因",
        )
    with pytest.raises(ValidationError):
        LimitationItem(
            item_id="KD-X",
            marker=LimitationMarker.PARTIAL,
            origin=LimitationOrigin.MANUAL,
            statement="无原因",
            added_by="user_w12",
        )


def test_append_manual_limitation_only_appends(limitations: KnownLimitations) -> None:
    """人工项只能追加：`items_digest` 重算、自动项保持不变。"""
    before = len(limitations.items)
    item = LimitationItem(
        item_id="KD-W12-TEST",
        marker=LimitationMarker.UNKNOWN,
        origin=LimitationOrigin.MANUAL,
        statement="测试用人工项",
        added_by="user_w12_test",
        reason="单测负测/正测隔离",
    )
    updated = append_manual_limitation(limitations, item=item)
    assert len(updated.items) == before + 1
    assert updated.items_digest == items_digest(updated.items)
    assert updated.items_digest != limitations.items_digest
    # 自动项逐项未变。
    auto_before = {
        item.item_id: (item.marker, item.statement)
        for item in limitations.items
        if item.origin is LimitationOrigin.AUTO
    }
    auto_after = {
        item.item_id: (item.marker, item.statement)
        for item in updated.items
        if item.origin is LimitationOrigin.AUTO
    }
    assert auto_before == auto_after
    # 重复追加同一 id → 拒绝。
    with pytest.raises(ValueError):
        append_manual_limitation(updated, item=item)
    # 非 MANUAL 不得追加。
    with pytest.raises(ValueError):
        append_manual_limitation(updated, item=limitations.items[0])


def test_known_deviations_are_three_manual_items(limitations: KnownLimitations) -> None:
    """3 条在册已知偏离（KD-W11-01/02/03）为 `origin=MANUAL` 且可追溯。"""
    expected_ids = ["KD-W11-01", "KD-W11-02", "KD-W11-03"]
    assert [spec["item_id"] for spec in KNOWN_DEVIATIONS] == expected_ids
    manual = [item for item in limitations.items if item.origin is LimitationOrigin.MANUAL]
    assert sorted(item.item_id for item in manual) == sorted(expected_ids)
    for item in manual:
        assert item.added_by == MANUAL_ADDED_BY
        assert (item.reason or "").strip()
        assert item.marker in set(LimitationMarker)
        assert "docs/w10-w12-lead-rulings.md" in (item.reason or "")
    assert len(known_deviation_items()) == 3
    # 已知偏离不得混进自动集合。
    assert not (set(expected_ids) & set(AUTO_ITEM_IDS))
    # KD-W12-01（2026-11-17）与 KD-W12-02（2026-11-19）均已关闭：**不得**再出现在在册
    # `items` 里，其关闭依据必须留在 `CLOSED_DEVIATIONS` 只留档（可追溯、不参与 items_digest）。
    closed_ids = {item.item_id for item in limitations.items}
    assert "KD-W12-01" not in closed_ids
    assert "KD-W12-02" not in closed_ids
    closed = {spec["item_id"]: spec for spec in CLOSED_DEVIATIONS}
    assert "KD-W12-01" in closed
    assert closed["KD-W12-01"]["closed_at"] == "2026-11-17"
    assert "PASSED" in closed["KD-W12-01"]["closure"]
    assert "plan.py" in closed["KD-W12-01"]["closure"]
    assert "KD-W12-02" in closed
    assert closed["KD-W12-02"]["closed_at"] == "2026-11-19"
    assert "analyzers.py" in closed["KD-W12-02"]["closure"]
    assert "test_diagnosis_analyzers.py" in closed["KD-W12-02"]["verification"]
    # 关闭项只在 `.md` 的「已关闭偏离」小节留档，且明确标为已关闭。
    md_text = (_REPO_ROOT / LIMITATIONS_MD).read_text(encoding="utf-8")
    assert "已关闭偏离" in md_text
    assert "2026-11-17" in md_text
    assert "2026-11-19" in md_text


# ---------------------------------------------------------------------------
# W11 同源对账
# ---------------------------------------------------------------------------


def test_w11_same_source_digest(limitations: KnownLimitations) -> None:
    """`w11_source_status=PASSED` 且 `w11_source_digest` 等于 W11 六项摘要。"""
    w11_items, status, digest = read_w11_limitations(_REPO_ROOT)
    assert status is AcceptanceStatus.PASSED
    assert w11_items is not None and len(w11_items) == 6
    assert digest == items_digest(w11_items)
    assert limitations.w11_source_status is AcceptanceStatus.PASSED
    assert limitations.w11_source_digest == digest
    # W11 六项在清单内逐字保留（同源，不改写）。
    by_id = {item.item_id: item for item in limitations.items}
    for item in w11_items:
        assert by_id[item.item_id].statement == item.statement
        assert by_id[item.item_id].marker == item.marker


def test_w11_missing_falls_back_to_constants_not_fabrication(tmp_path: Path) -> None:
    """W11 文件缺失 → 常量 fallback + `NOT_AVAILABLE`（不伪造 W11 输出）。"""
    fallback = build_known_limitations(w11_items=None)
    assert fallback.w11_source_status is AcceptanceStatus.NOT_AVAILABLE
    assert fallback.w11_source_digest is None
    assert len(fallback.items) == 13
    assert {item.item_id for item in fallback.items} == set(AUTO_ITEM_IDS)
    # 空目录同样降级，且不得写任何 docs/w11 文件。
    assert read_w11_limitations(tmp_path)[1] is AcceptanceStatus.NOT_AVAILABLE
    assert not (tmp_path / "docs").exists()


# ---------------------------------------------------------------------------
# 单一数据源：`.json` 与 `.md` 内嵌块对账
# ---------------------------------------------------------------------------


def test_json_is_single_source_of_truth(
    limitations: KnownLimitations, tmp_path: Path
) -> None:
    """`.json` 与 `.md` 内嵌块逐字一致，`items_digest` 可对账。

    写出函数只写 `tmp_path`，仓库落盘件仅做**只读逐字节比对**（不一致即 FAIL）。
    """
    json_path = _REPO_ROOT / LIMITATIONS_JSON
    md_path = _REPO_ROOT / LIMITATIONS_MD
    assert json_path.is_file() and md_path.is_file()

    disk = json.loads(json_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", md_text, re.S)
    assert len(blocks) == 1, "known-limitations.md 必须内嵌且仅内嵌一个 JSON 块"
    embedded = json.loads(blocks[0])
    assert embedded == disk

    assert disk["version"] == "w12-limitations-v1"
    assert disk["items_digest"] == embedded["items_digest"]
    assert disk["w11_source_digest"] == embedded["w11_source_digest"]
    assert len(disk["items"]) == 16
    assert disk["w11_source_status"] == AcceptanceStatus.PASSED.value

    # 与重新派生（同一函数）结果一致：单一数据源不漂移。
    rebuilt = build_full_known_limitations(_REPO_ROOT)
    assert rebuilt.items_digest == disk["items_digest"]
    assert rebuilt.w11_source_digest == disk["w11_source_digest"]

    # 写出函数幂等（内容不变）：改为写 tmp_path，再与仓库落盘件只读比对。
    # 比较失败即 FAIL（这本身是有价值的契约校验），但绝不写回仓库。
    json_path2, md_path2 = write_known_limitations(tmp_path, rebuilt)
    assert json_path2.parent == tmp_path / "docs" / "w12", "写出目标必须是 tmp_path"
    assert md_path2.parent == tmp_path / "docs" / "w12", "写出目标必须是 tmp_path"
    assert json_path2.read_text(encoding="utf-8") == json_path.read_text(encoding="utf-8")
    assert md_path2.read_text(encoding="utf-8") == md_text
    assert json_path2.read_bytes() == json_path.read_bytes(), (
        "docs/w12/known-limitations.json 与代码派生结果不一致"
    )
    assert md_path2.read_bytes() == md_path.read_bytes(), (
        "docs/w12/known-limitations.md 与代码派生结果不一致"
    )


def test_markdown_reports_both_digests(limitations: KnownLimitations) -> None:
    """`known-limitations.md` 必须同时写出 `items_digest` 与 `w11_source_digest`。"""
    md_text = (_REPO_ROOT / LIMITATIONS_MD).read_text(encoding="utf-8")
    assert limitations.items_digest in md_text
    assert str(limitations.w11_source_digest) in md_text
    assert "自动基础项（13 项，不可删）" in md_text
    assert "已知偏离（人工项" in md_text
