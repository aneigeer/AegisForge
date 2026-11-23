"""W12 T03 集成测试：可复现演示（AC-12）。

口径：`docs/w12/03-task-cards.md` T03、`docs/w12/02-design.md` §4.2/§9.2、
`docs/w10-w12-lead-rulings.md` §4（W12-Q7：无 Web 控制台）。

断言清单：

1. 四段演示（S1…S4）各 1 例，均为 `NOT_EXECUTED` + 非空原因（本轮未实跑，不冒充通过）；
2. 隔离不变量：`lab_only is True`、`production_dsn_referenced is False`、
   `web_console_used` 是 `ClassVar`（非字段）且为 `False`；
3. PG 段 `NOT_EXECUTED`（`ENV-PG5432`），**不是** `FAILED`；
4. 措辞红线：`report_has_completion_wording_for_advice is False`，且扫描函数对
   「建议被写成已修复」可证伪（正向命中 + 禁令语境豁免）；
5. 负测：3 段 / `lab_only=False` / `production_dsn_referenced=True` → 构造失败；
6. 文档：`docs/w12/demo-script.md` 存在，含四段脚本与隔离检查记录。

本文件不实跑演示（不写库、不连 PG、不启容器），只校验结果件与文档口径。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.release import AcceptanceStatus
from core.release.deploy import (
    DEMO_SEGMENT_IDS,
    DemoScriptResult,
    DemoSegment,
    build_demo_result,
    completion_wording_for_advice,
    describe_isolation,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def demo() -> DemoScriptResult:
    return build_demo_result(_REPO_ROOT)


def _segment(segment_id: str, status: AcceptanceStatus = AcceptanceStatus.NOT_EXECUTED) -> DemoSegment:
    return DemoSegment(
        segment_id=segment_id,  # type: ignore[arg-type]
        name=f"W12 演示 {segment_id}",
        status=status,
        reason="本轮未实跑演示脚本",
    )


# ---------------------------------------------------------------------------
# 四段结构 + 未执行口径
# ---------------------------------------------------------------------------


def test_ac12_four_segments_present(demo: DemoScriptResult) -> None:
    """S1/S2/S3/S4 各 1 例。"""
    assert len(demo.segments) == 4
    assert [item.segment_id for item in demo.segments] == list(DEMO_SEGMENT_IDS)
    assert len({item.segment_id for item in demo.segments}) == 4


def test_ac12_segments_are_honest_not_executed(demo: DemoScriptResult) -> None:
    """四段本轮均未实跑：状态为 `NOT_EXECUTED` 且逐条带原因（不冒充通过）。"""
    for item in demo.segments:
        assert item.status is AcceptanceStatus.NOT_EXECUTED, item.segment_id
        assert item.status is not AcceptanceStatus.PASSED
        assert item.status is not AcceptanceStatus.FAILED
        assert (item.reason or "").strip(), item.segment_id


def test_ac12_pg_segment_is_not_executed(demo: DemoScriptResult) -> None:
    """PG 段 `NOT_EXECUTED`（`ENV-PG5432`），不判 FAIL、不冒充通过。"""
    assert demo.pg_segment_status is AcceptanceStatus.NOT_EXECUTED
    assert demo.pg_segment_status is not AcceptanceStatus.FAILED
    assert demo.pg_segment_status is not AcceptanceStatus.PASSED
    s1 = [item for item in demo.segments if item.segment_id == "S1"][0]
    assert "ENV-PG5432" in (s1.reason or "")


# ---------------------------------------------------------------------------
# 隔离与措辞不变量
# ---------------------------------------------------------------------------


def test_ac12_isolation_invariants(demo: DemoScriptResult) -> None:
    """只走隔离 LAB、不引用生产 DSN、无 Web 控制台。"""
    assert demo.lab_only is True
    assert demo.production_dsn_referenced is False
    assert "web_console_used" not in DemoScriptResult.model_fields
    assert DemoScriptResult.web_console_used is False
    assert type(demo).web_console_used is False

    isolation = describe_isolation()
    assert isolation["lab_only"] is True
    assert isolation["production_dsn_referenced"] is False
    assert isolation["web_console_used"] is False
    assert isolation["allowed_write_tool"] == "mysql_lab_update_order_note"
    assert isolation["allowed_write_tool_version"] == "1.0"
    assert "已验证" not in isolation["note"]


def test_ac12_no_completion_wording_for_advice(demo: DemoScriptResult) -> None:
    """演示稿不得用「已修复」描述「建议」。"""
    assert demo.report_has_completion_wording_for_advice is False
    doc = (_REPO_ROOT / "docs" / "w12" / "demo-script.md").read_text(encoding="utf-8")
    assert completion_wording_for_advice(doc) is False


def test_ac12_completion_wording_scanner_is_falsifiable() -> None:
    """扫描函数可证伪：正向命中，禁令/否定语境豁免。"""
    assert completion_wording_for_advice("建议项：已修复索引缺失") is True
    assert completion_wording_for_advice("本条为建议，已修复后需复核") is True
    # 否定语境（规范引用）不算违规。
    assert completion_wording_for_advice("不得用「已修复」描述建议项") is False
    assert completion_wording_for_advice("禁止把建议写成已修复") is False
    # 不含关键词语句不算违规。
    assert completion_wording_for_advice("索引缺失已修复") is False


# ---------------------------------------------------------------------------
# 负测：构造期 fail closed
# ---------------------------------------------------------------------------


def test_three_segments_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoScriptResult(
            segments=(_segment("S1"), _segment("S2"), _segment("S3")),
            pg_segment_status=AcceptanceStatus.NOT_EXECUTED,
        )


def test_duplicate_segment_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoScriptResult(
            segments=(_segment("S1"), _segment("S1"), _segment("S3"), _segment("S4")),
            pg_segment_status=AcceptanceStatus.NOT_EXECUTED,
        )


def test_lab_only_false_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoScriptResult(
            segments=tuple(_segment(item) for item in DEMO_SEGMENT_IDS),  # type: ignore[arg-type]
            lab_only=False,
            pg_segment_status=AcceptanceStatus.NOT_EXECUTED,
        )


def test_production_dsn_reference_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DemoScriptResult(
            segments=tuple(_segment(item) for item in DEMO_SEGMENT_IDS),  # type: ignore[arg-type]
            production_dsn_referenced=True,
            pg_segment_status=AcceptanceStatus.NOT_EXECUTED,
        )


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


def test_demo_script_doc_has_four_segments_and_isolation_record() -> None:
    """演示脚本存在，含四段、隔离检查记录与复用接缝。"""
    doc_path = _REPO_ROOT / "docs" / "w12" / "demo-script.md"
    assert doc_path.is_file()
    text = doc_path.read_text(encoding="utf-8")
    for segment in ("S1", "S2", "S3", "S4"):
        assert f"{segment}｜" in text, segment
    assert "隔离检查记录" in text
    assert "scripts/demo_agent_run.py" in text
    assert "restore" in text.lower() or "复原" in text
    # 不连生产库、无 Web 控制台的口径必须写明。
    assert "production_dsn_referenced" in text
    assert "web_console_used" in text
