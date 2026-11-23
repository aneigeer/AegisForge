"""W12 T01 集成测试：功能验收清单（AC-01…AC-04 + AC-20 结构保证）。

口径：`docs/w12/01-prd.md` §5、`docs/w12/02-design.md` §4.1/§7/§10、
`docs/w12/03-task-cards.md` T01、`docs/w10-w12-lead-rulings.md` §9（D-06/D-09）。

覆盖清单：

1. **AC-01** 五态生命周期（创建/查询/暂停/恢复/取消）各 1 例，`task_events` 可查询、
   `seq == list(range(n))`、事件链校验 `ok=True`；
2. **AC-02** 三类诊断报告（`slow_query`/`connection_pressure`/`lock_wait`）五字段非空率 100%，
   并用**实测** `ReportFacts` 口径（`core/evidence/models.py:121-130`）承载；
3. **AC-03** 审批修复闭环：`verdict == PASS`、`tool_calls == 1`、目标行变化恰 1 次，
   写动作仍是 `mysql_lab_update_order_note`/`1.0`；
4. **AC-04** 第二引擎只读：`[d for d in registry.definitions() if d.side_effect == "write"] == []`
   等价断言（`write_tools()` 不存在，D-06）+ PG 段 `NOT_EXECUTED` 时**不判 FAIL**；
5. **AC-20** 结构保证：`FunctionalAcceptanceResult` 注入 `demo_passed`（及 `demo_status`/
   `demo_ok`）→ `ValidationError`，且 `DEMO_INPUT_FIELDS` 与 `model_fields` 交集为空。

环境边界：控制库/LAB 不可用 → 整文件或对应用例 `skip` 并带原因（`ENV-*` 不判 FAIL）；
本文件不依赖 `DG_ADMIN_DSN`、不依赖 PG 5432 可连、不做 DELETE/UPDATE/TRUNCATE/DROP
（唯一 UPDATE 是 W07 冻结的 LAB 单行修复动作与其复原协议）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.lab_assembly import build_lab_registry
from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOL_DEFINITIONS
from core.errors import ConfigError, DBGuardianError
from core.evidence.models import ReportFacts
from core.release import AcceptanceStatus
from core.release.functional import (
    DEMO_INPUT_FIELDS,
    DIAGNOSIS_SCENARIOS,
    GATE_EVIDENCE_REFS,
    LAB_ORDER_ID,
    LAB_WRITE_TOOL_VERSION,
    LIFECYCLE_PHASES,
    FunctionalAcceptanceResult,
    build_diagnosis_reports,
    check_second_engine_readonly,
    default_diagnosis_evidence,
    probe_lifecycle,
    probe_pg_segment,
    run_functional_acceptance,
)
from core.tasks.repository import TaskRepository
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    """控制库可用性探针：不可用一律 skip（带原因），绝不判 FAIL。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@pytest.fixture(scope="module")
def lab_registry(control_db: Any, settings: Any) -> Any:
    """经装配点构建 LAB 注册表（含唯一 R2 写工具）；不新增任何写入口。"""
    repository = TaskRepository(control_db)
    environment_row = repository.get_environment(
        settings.default_environment_id, tenant_id=settings.default_tenant_id
    )
    if environment_row is None:
        pytest.skip("默认 MySQL 环境不存在，请先执行 bootstrap_mysql.py")
    return build_lab_registry(None, environment_row, "mysql")


class _PgContractTool:
    """第二引擎契约壳：只为让 `ToolRegistry.definitions()` 枚举到 PG 契约定义。"""

    def __init__(self, definition: Any) -> None:
        self.definition = definition


@pytest.fixture(scope="module")
def pg_registry() -> Any:
    """第二引擎（PostgreSQL / MCP）注册表：**只装读契约**，供 AC-04 等价断言使用。"""
    registry = ToolRegistry()
    for definition in PG_TOOL_DEFINITIONS.values():
        registry.register(_PgContractTool(definition))
    return registry


@pytest.fixture(scope="module")
def worker_stack(settings: Any) -> Any:
    """W07/W08 唯一组合根（含独立回读 + 独立验证器），供 AC-03 使用。"""
    from workers.stack import build_worker_stack

    try:
        return build_worker_stack(settings)
    except DBGuardianError as exc:
        pytest.skip(f"执行栈装配失败（{exc.code.value}）：{exc.message}")


@pytest.fixture(scope="module")
def pg_readonly_probe(settings: Any) -> Any:
    """PG 只读探针：只做 `SELECT 1`；不可达即让 `probe_pg_segment` 记 NOT_EXECUTED。"""

    def probe() -> bool:
        if settings.pg_ro_dsn is None:
            raise ConfigError("未配置 PG 只读 DSN（ENV-PG5432）")
        from adapters.postgres_mcp.tools import execute_tool
        from database.postgres.connection import PgClient

        client = PgClient(
            settings.pg_ro_dsn,
            database_id=settings.pg_database_id,
            read_only=True,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_ms=settings.statement_timeout_ms,
            max_result_rows=settings.max_result_rows,
        )
        rows = client.fetch_all("SELECT 1 AS ok")
        if not rows:
            return False
        # P2-T1：PG 段"可用"的判据不止 SELECT 1——必须确认 pg_stat_statements 可查
        # （扩展只能在 shared_preload_libraries 预加载后创建，故 available=True
        #  已经等价证明预加载生效；`SHOW`/`current_setting` 被只读守卫拒绝，不能用）。
        stats = execute_tool(
            "get_query_stats",
            {"limit": 5},
            client=client,
            database_id=settings.pg_database_id,
            db_version="16.4",
        )
        return bool((stats.data or {}).get("available"))

    return probe


@pytest.fixture(scope="module")
def functional_result(
    control_db: Any,
    worker_stack: Any,
    pg_registry: Any,
    pg_readonly_probe: Any,
) -> FunctionalAcceptanceResult:
    """整份 T01 验收只跑一次（5 态 + 1 次写闭环 + 3 类报告），逐条断言分开写。"""
    return run_functional_acceptance(
        control_client=control_db,
        repair_stack=worker_stack,
        second_engine_registry=pg_registry,
        pg_probe=pg_readonly_probe,
    )


# ---------------------------------------------------------------------------
# AC-20：演示 ≠ 验收（结构保证）
# ---------------------------------------------------------------------------


def test_ac20_result_model_has_no_demo_fields() -> None:
    """结构上不存在 demo 字段：常量与 `model_fields` 交集为空（AC-20）。"""
    fields = set(FunctionalAcceptanceResult.model_fields)
    assert set(DEMO_INPUT_FIELDS) & fields == set()
    for name in DEMO_INPUT_FIELDS:
        assert name not in fields


@pytest.mark.parametrize("demo_field", DEMO_INPUT_FIELDS)
def test_ac20_demo_field_injection_is_rejected(demo_field: str) -> None:
    """注入 `demo_passed=True`（及另两个 demo 键）必须抛 `ValidationError`。"""
    payload: dict[str, Any] = {
        "run_id": "w12-func-demo-injection",
        "lifecycle": {phase: AcceptanceStatus.PASSED for phase in LIFECYCLE_PHASES},
        "diagnosis_reports": build_diagnosis_reports(),
        "repair_closure": {
            "status": AcceptanceStatus.PASSED,
            "verdict": "PASS",
            "tool_calls": 1,
            "row_changes": 1,
        },
        "second_engine_readonly": True,
        "pg_segment_status": AcceptanceStatus.NOT_EXECUTED,
        "evidence_refs": ("core/release/functional.py",),
        demo_field: True,
    }
    with pytest.raises(ValidationError):
        FunctionalAcceptanceResult(**payload)

    # 去掉 demo 键后同一载荷必须能构造（证明拒绝原因就是 demo 字段，而不是别的字段错）。
    payload.pop(demo_field)
    assert FunctionalAcceptanceResult(**payload).run_id == "w12-func-demo-injection"


# ---------------------------------------------------------------------------
# AC-01：五态生命周期
# ---------------------------------------------------------------------------


def test_ac01_five_state_lifecycle_is_auditable(control_db: Any) -> None:
    """创建/查询/暂停/恢复/取消各 1 例，seq 连续且事件链完整。"""
    observations = probe_lifecycle(control_client=control_db)

    assert [item.phase for item in observations] == list(LIFECYCLE_PHASES)
    assert len(observations) == 5
    blocked = [item for item in observations if item.status is AcceptanceStatus.NOT_EXECUTED]
    if blocked:  # 环境阻塞：不判 FAIL，但必须带原因
        pytest.skip(f"控制库阻塞：{[item.detail for item in blocked]}")
    for item in observations:
        assert item.status is AcceptanceStatus.PASSED, f"{item.phase}: {item.detail}"
        assert item.task_id, f"{item.phase}: 缺少 task_id"
        assert item.event_count >= 1, f"{item.phase}: task_events 不可查询"
        assert item.seq_contiguous is True, f"{item.phase}: seq 不连续（{item.detail}）"
        assert item.chain_ok is True, f"{item.phase}: 事件链校验失败（{item.detail}）"


def test_ac01_result_lifecycle_covers_exactly_five_phases(
    functional_result: FunctionalAcceptanceResult,
) -> None:
    """结果件的 lifecycle 键集恰为五态，且全部 PASSED（不含 NOT_EXECUTED 之外的糊弄值）。"""
    assert set(functional_result.lifecycle) == set(LIFECYCLE_PHASES)
    assert all(
        status is AcceptanceStatus.PASSED
        for status in functional_result.lifecycle.values()
    ), functional_result.lifecycle


# ---------------------------------------------------------------------------
# AC-02：三类诊断报告
# ---------------------------------------------------------------------------


def test_ac02_three_diagnosis_reports_have_five_nonempty_fields() -> None:
    """三类场景各 1 例，五字段非空率 100%（按实测 ReportFacts 口径）。"""
    reports = build_diagnosis_reports()

    assert len(reports) == 3
    assert [item.scenario for item in reports] == list(DIAGNOSIS_SCENARIOS)
    assert all(item.five_fields_nonempty for item in reports), [
        (item.scenario, item.model_dump()) for item in reports
    ]
    for item in reports:
        # 五字段逐一非空（不靠聚合属性糊过去）。
        assert item.evidence_id.strip()
        assert item.supporting_evidence_ids
        assert item.contradicting_evidence_ids
        assert item.missing_evidence
        assert item.scope.strip()
        # 实测口径：既有 ReportFacts 模型必须能承载这四类证据 id 列表。
        facts = ReportFacts(
            tenant_id="tenant_local",
            task_id="task_w12_ac02_structural",
            supporting_evidence_ids=list(item.supporting_evidence_ids),
            contradicting_evidence_ids=list(item.contradicting_evidence_ids),
            missing_evidence=list(item.missing_evidence),
        )
        assert facts.supporting_evidence_ids == list(item.supporting_evidence_ids)
        assert facts.contradicting_evidence_ids == list(item.contradicting_evidence_ids)
        assert facts.missing_evidence == list(item.missing_evidence)


def test_ac02_missing_evidence_bundle_is_not_reported_as_passing() -> None:
    """证据包缺失时必须给出五字段全空（不可被误读成通过）——反证断言可证伪。"""
    reports = build_diagnosis_reports({"slow_query": ()})
    empty = [item for item in reports if item.scenario == "slow_query"][0]
    assert empty.five_fields_nonempty is False
    assert empty.evidence_id == ""


def test_ac02_default_evidence_bundle_is_structural_and_documented() -> None:
    """默认证据包必须三场景齐全，且明确标注为结构性复核输入（不假装是实跑诊断）。"""
    bundles = default_diagnosis_evidence()
    assert set(bundles) == set(DIAGNOSIS_SCENARIOS)
    for scenario, evidences in bundles.items():
        assert evidences, f"{scenario}: 默认证据包为空"
        for evidence in evidences:
            assert "w12-functional-acceptance" in str(
                evidence.payload.get("source_metadata")
            ), f"{scenario}: 证据来源未标注"


# ---------------------------------------------------------------------------
# AC-03：审批修复闭环
# ---------------------------------------------------------------------------


def test_ac03_repair_closure_is_single_write(
    functional_result: FunctionalAcceptanceResult,
) -> None:
    """AC-03 实测结论：闭环执行事实成立，且**独立验证通过** → `PASSED`。

    KD-W12-01 已关闭（2026-11-17）：执行器把 `StepObserved.result` 落成
    `ToolResult.model_dump()`（`executor.py:486`，业务指标在 `result["data"]` 下），
    原 W08 `_collect_observed` 只在顶层取指标导致 `affected_rows=UNKNOWN` /
    `snapshot_comparable=FAIL`；`core/verification/plan.py` 读取端改为
    **优先 `result["data"]`、回落顶层**后，两条 step_observed 断言可判定，
    最差法汇总 `PASS`。

    本用例断言**修复后的真实结果**（不是放宽）：`status=PASSED`、
    `verdict=="PASS"`、`reason_code=="OK"`、三条断言全 `PASS`；
    执行事实（唯一写动作/恰 1 次调用/目标行恰变化 1 次）与工具身份不变。
    历史 `FAILED` 结论留档于 `docs/w12/known-limitations.md` §3.1「已关闭偏离」。
    """
    closure = functional_result.repair_closure
    # ① 结论：独立验证通过（如实记录，不注水）。
    assert closure.status is AcceptanceStatus.PASSED, closure.detail
    assert closure.verdict == "PASS", closure.detail
    assert closure.reason_code == "OK", closure.detail
    # ② 执行事实仍然成立：唯一写动作、恰 1 次调用、目标行恰变化 1 次。
    assert closure.tool_calls == 1, closure.detail
    assert closure.row_changes == 1, closure.detail
    assert closure.tool_name == LAB_WRITE_TOOL_NAME
    assert closure.tool_version == LAB_WRITE_TOOL_VERSION
    # ③ 逐条断言归因：两条 step_observed 取到真实形状，独立回读那条同样通过。
    assert closure.assertion_verdicts == {
        "affected_rows": "PASS",
        "target_note_final": "PASS",
        "snapshot_comparable": "PASS",
    }, closure.assertion_verdicts


def test_ac03_r2_write_tools_are_closed_set(lab_registry: Any) -> None:
    """P2-T3：写动作扩为三支闭集（note / 状态前进 / 状态回退），全部 R2 + LAB 限定。"""
    from adapters.mysql.lab_status_repair import (
        ADVANCE_TOOL_NAME,
        REVERT_TOOL_NAME,
    )

    writes = {item.name: item for item in lab_registry.definitions() if item.side_effect == "write"}
    assert sorted(writes) == sorted(
        [LAB_WRITE_TOOL_NAME, ADVANCE_TOOL_NAME, REVERT_TOOL_NAME]
    )
    note = writes[LAB_WRITE_TOOL_NAME]
    assert note.risk_level == "R2"
    assert note.version == LAB_WRITE_TOOL_VERSION
    for definition in writes.values():
        assert definition.risk_level == "R2"
        assert definition.allowed_environments == ["LAB"]
        assert definition.compensation is not None


# ---------------------------------------------------------------------------
# AC-04：第二引擎只读
# ---------------------------------------------------------------------------


def test_ac04_second_engine_has_no_write_definition() -> None:
    """等价断言（`write_tools()` 不存在，D-06）：第二引擎写定义集合为空。"""
    assert check_second_engine_readonly() is True
    assert [
        item.name for item in PG_TOOL_DEFINITIONS.values() if item.side_effect == "write"
    ] == []
    assert all(
        item.side_effect in {"read", "none"} for item in PG_TOOL_DEFINITIONS.values()
    )


def test_ac04_pg_segment_not_executed_is_not_failed(pg_readonly_probe: Any) -> None:
    """PG 段状态只允许 PASSED / NOT_EXECUTED；NOT_EXECUTED 绝不等于 FAILED。"""
    status = probe_pg_segment(pg_probe=pg_readonly_probe)
    assert status is not AcceptanceStatus.FAILED
    assert status in {AcceptanceStatus.PASSED, AcceptanceStatus.NOT_EXECUTED}
    # 未提供探针时也必须是 NOT_EXECUTED（不冒充通过）。
    assert probe_pg_segment() is AcceptanceStatus.NOT_EXECUTED


# ---------------------------------------------------------------------------
# 整份结果件
# ---------------------------------------------------------------------------


def test_functional_result_is_complete_and_honest(
    functional_result: FunctionalAcceptanceResult,
) -> None:
    """结果件字段齐全、无 demo 字段、PG 段不判 FAIL、证据引用路径真实存在。"""
    assert functional_result.run_id.startswith("w12-func-")
    assert functional_result.second_engine_readonly is True
    assert functional_result.pg_segment_status is not AcceptanceStatus.FAILED
    assert len(functional_result.diagnosis_reports) == 3
    assert all(item.five_fields_nonempty for item in functional_result.diagnosis_reports)

    assert functional_result.evidence_refs
    for relative in functional_result.evidence_refs:
        assert (_REPO_ROOT / relative).is_file(), f"证据引用不存在：{relative}"
    for gate_ref in GATE_EVIDENCE_REFS:
        assert gate_ref in functional_result.evidence_refs

    dumped = functional_result.model_dump()
    for name in DEMO_INPUT_FIELDS:
        assert name not in dumped
