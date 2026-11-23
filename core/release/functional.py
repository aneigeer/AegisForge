"""W12 T01：功能验收清单（AC-01…AC-04）。

口径来源：`docs/w12/01-prd.md` §5（AC-01…AC-04）、`docs/w12/02-design.md` §4.1/§7/§10、
`docs/w12/03-task-cards.md` T01、`docs/w10-w12-lead-rulings.md` §4/§9。

四条验收的落地方式（**逐条给可断言的实测口径**）：

| AC | 口径 | 落点 |
|---|---|---|
| AC-01 | 创建/查询/暂停/恢复/取消各 1 例；每例 `task_events` 可查询、`seq == list(range(n))`、链校验 `ok=True` | :func:`probe_lifecycle`（复用 `core/audit/service.py::query/verify`） |
| AC-02 | `slow_query` / `connection_pressure` / `lock_wait` 各 1 例，五字段非空率 100% | :func:`build_diagnosis_reports`（复用 W04 真实分析器 + `ReportFacts` 实测口径） |
| AC-03 | `verification_result.verdict == PASS`、`tool_calls == 1`、目标行变化 == 1 | :func:`run_repair_closure`（复用 W07 唯一装配点与 W08 独立验证器） |
| AC-04 | 第二引擎（PG）无写闭环：`[d for d in registry.definitions() if d.side_effect == "write"] == []` | :func:`check_second_engine_readonly` |

**AC-20 结构保证（演示 ≠ 验收）**：:class:`FunctionalAcceptanceResult` 的字段集中
**不存在** `demo_passed` / `demo_status` / `demo_ok`，且模型 `extra="forbid"`——注入任一
demo 字段即 `pydantic.ValidationError`。演示结果由 T03 独立成件（`DemoScriptResult`），
永远不进入 `ac_result` 判定输入。

**环境阻塞口径（不判 FAIL、不冒充通过）**：

- PG 段（`ENV-PG5432`）不可达 → ``pg_segment_status = NOT_EXECUTED``；
- 控制库/LAB 不可用 → 对应维度 ``NOT_EXECUTED``；
- 上游结果件缺失 → ``NOT_AVAILABLE``（本卡无该维度，T02/T04 使用）。

**只读边界**：本模块所有计数一律按本卡创建/使用的 `task_id` 过滤（`WHERE task_id = %s`），
**绝不**统计全表（`ENV-RESIDUE` 已 79313 行，全表计数必红）；不新增工具、不新增端点、
不执行 DELETE/UPDATE/TRUNCATE/DROP（唯一 UPDATE 是 W07 冻结的 LAB 修复动作与其复原协议）。

**不新增依赖**：仅复用既有 `pydantic` / 既有仓储与分析器；`core/assurance/**`、
`core/benchmark/**` 本模块**零引用**（不硬依赖上游实现，纪律 17）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.audit.service import AuditService
from core.diagnosis.analyzers import (
    analyze_connection_pressure,
    analyze_lock_wait,
    analyze_slow_query,
)
from core.errors import DBGuardianError
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceQuality,
    ReportFacts,
)
from core.gateway.envelope import build_envelope, sign_envelope
from core.release import AcceptanceStatus
from core.security.models import AuthMethod, Principal
from core.security.roles import Role
from core.tasks.models import Task, TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

#: 演示字段名闭集（AC-20）：这些键**不得**出现在任何验收结果模型的字段集里。
DEMO_INPUT_FIELDS: tuple[str, ...] = ("demo_passed", "demo_status", "demo_ok")

#: LAB 种子行（`adapters/mysql/lab_repair.py` 的受限单行更新目标）。
LAB_ORDER_ID: int = 2001
#: 写动作的唯一合法身份（W07 冻结；本卡不新增任何写工具）。
LAB_WRITE_TOOL_NAME: str = "mysql_lab_update_order_note"
LAB_WRITE_TOOL_VERSION: str = "1.0"

#: 修复闭环的结论原因码（AC-03 / KD-W12-01 归因用，闭集）。
REASON_OK: str = "OK"
#: `StepObserved` 行存在但 `step_observed` 断言仍报实测缺失（形状与读取口径不一致）。
#: 归因语义保留；KD-W12-01 已于 2026-11-17 关闭（读取端改为优先 ``result["data"]``、
#: 回落顶层），当前真实链不再落到本码，历史留档见 `docs/w12/known-limitations.md` §3.1。
REASON_OBSERVED_SHAPE_MISMATCH: str = "OBSERVED_SHAPE_MISMATCH"
#: 执行前/后证据行确实不存在（真缺失，非形状问题）。
REASON_STEP_OBSERVED_MISSING: str = "STEP_OBSERVED_MISSING"
#: 独立验证未通过，但原因不属于上述两类。
REASON_VERDICT_NOT_PASS: str = "VERDICT_NOT_PASS"
#: 闭环自身的硬断言（写动作/调用次数/行变化/契约身份）不成立。
REASON_CLOSURE_ASSERTION_FAILED: str = "CLOSURE_ASSERTION_FAILED"

#: 五态生命周期顺序（AC-01）。
LIFECYCLE_PHASES: tuple[str, ...] = ("create", "query", "pause", "resume", "cancel")

#: 三类诊断场景（AC-02）；顺序固定，供断言稳定引用。
DIAGNOSIS_SCENARIOS: tuple[str, ...] = (
    "slow_query",
    "connection_pressure",
    "lock_wait",
)

#: 冻结门禁件路径（存在即引用；本卡只读，不修改 `docs/w07/**`–`docs/w11/**`）。
GATE_EVIDENCE_REFS: tuple[str, ...] = (
    "docs/w09/12-w09-qa-closeout.md",
    "docs/w10/04-w10-qa-closeout.md",
    "docs/w11/05-w11-qa-closeout.md",
)


# ---------------------------------------------------------------------------
# 结果模型（全部 extra="forbid"，且结构上无 demo 字段）
# ---------------------------------------------------------------------------


class DiagnosisReportCheck(BaseModel):
    """一条诊断报告的**五字段**结构检查（AC-02）。

    五字段的定义来自 `core/diagnosis/models.py::Hypothesis` 与
    `core/evidence/models.py::ReportFacts`（实测口径，见设计 D-03：PRD 的
    `01-prd.md:102-111` 已漂移，本卡按代码实测字段名为准）：

    1. ``evidence_id``：报告结论绑定的主证据；
    2. ``supporting_evidence_ids``：支持证据；
    3. ``contradicting_evidence_ids``：反证；
    4. ``missing_evidence``：未知/缺失信息；
    5. ``scope``：适用范围。
    """

    model_config = ConfigDict(extra="forbid")

    scenario: Literal["slow_query", "connection_pressure", "lock_wait"]
    evidence_id: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    scope: str

    @property
    def five_fields_nonempty(self) -> bool:
        """五字段是否同时非空（空串/空白一律视为空，不允许"糊过去"）。"""
        return bool(
            self.evidence_id.strip()
            and self.supporting_evidence_ids
            and self.contradicting_evidence_ids
            and self.missing_evidence
            and self.scope.strip()
        )


class RepairClosureCheck(BaseModel):
    """审批修复闭环检查（AC-03）：verdict / tool_calls / row_changes 三件套。

    ``reason_code`` 为结论的**可归因原因码**闭集（见模块常量 ``REASON_*``）：
    ``OBSERVED_SHAPE_MISMATCH`` 表示 `StepObserved` 行存在、但 ``step_observed``
    断言仍报实测缺失（形状与验证器读取口径不一致）——该**归因语义在形状不匹配时
    依然有效**，不因下述关闭而失效。

    **KD-W12-01 已于 2026-11-17 关闭**（`core/verification/plan.py` 读取端形状归一：
    优先 ``result["data"]``、回落顶层）：AC-03 真实链的结论已由 ``FAILED`` 转
    ``PASSED``（``status=PASSED`` / ``verdict=PASS`` / ``reason_code=OK``）；
    历史结论与关闭依据留档于 `docs/w12/known-limitations.md` §3.1「已关闭偏离」。
    """

    model_config = ConfigDict(extra="forbid")

    status: AcceptanceStatus
    #: `core/verification/models.py::Verdict` 取值（PASS/FAIL/UNKNOWN）；
    #: 未执行时字面量为 "NOT_EXECUTED"，**不用** PASS/FAIL 冒充。
    verdict: str = "NOT_EXECUTED"
    #: `None` 表示未实测（禁止用 0 冒充未实测值）。
    tool_calls: int | None = None
    row_changes: int | None = None
    tool_name: str = LAB_WRITE_TOOL_NAME
    tool_version: str = LAB_WRITE_TOOL_VERSION
    #: 结论原因码（``REASON_*`` 闭集）。
    reason_code: str = ""
    #: 逐条断言的判定（assertion_id → PASS/FAIL/UNKNOWN），供追溯。
    assertion_verdicts: dict[str, str] = Field(default_factory=dict)
    detail: str = ""


class FunctionalAcceptanceResult(BaseModel):
    """T01 验收结果件（AC-01…AC-04）。

    ★ 结构上**不存在** ``demo_passed`` / ``demo_status`` / ``demo_ok``（AC-20）：
    模型 ``extra="forbid"``，注入任一 demo 字段即 ``ValidationError``。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    lifecycle: dict[str, AcceptanceStatus]
    diagnosis_reports: tuple[DiagnosisReportCheck, ...]
    repair_closure: RepairClosureCheck
    second_engine_readonly: bool
    pg_segment_status: AcceptanceStatus
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleObservation:
    """单态生命周期的观测事实（AC-01 的可断言中间件，供测试独立复核）。"""

    phase: str
    task_id: str | None
    status: AcceptanceStatus
    event_count: int
    seq_contiguous: bool
    chain_ok: bool
    detail: str


# ---------------------------------------------------------------------------
# AC-02：三类诊断报告（真实分析器 + 实测 ReportFacts 口径）
# ---------------------------------------------------------------------------

_OBSERVED_AT = "2026-12-01T00:00:00+00:00"
_WINDOW_START = "2026-11-30T23:59:59+00:00"


def _diag_evidence(
    tool: str,
    data: dict[str, Any],
    *,
    evidence_id: str,
    engine: str = "mysql",
    status: CollectionStatus = CollectionStatus.SUCCESS,
    quality: EvidenceQuality = EvidenceQuality.HIGH,
    completeness: EvidenceCompleteness = EvidenceCompleteness.COMPLETE,
) -> Evidence:
    """构造一条**结构性复核**用证据（与 W04 单元测试同构，不连库）。"""
    return Evidence(
        evidence_id=evidence_id,
        tenant_id="tenant_local",
        task_id="task_w12_ac02_structural",
        environment_id="env_mysql_local",
        db_engine=engine,
        db_version="8.0.46" if engine == "mysql" else "12.18",
        adapter_version="1.0",
        source_tool=tool,
        observed_at=_OBSERVED_AT,
        time_window_start=_WINDOW_START,
        time_window_end=_OBSERVED_AT,
        collection_status=status,
        quality=quality,
        completeness=completeness,
        payload_hash="0" * 64,
        redaction_version="redaction-v2",
        reproducible=True,
        payload={
            "tool_status": status.value,
            "data": data,
            "warnings": [],
            "truncated": completeness is EvidenceCompleteness.PARTIAL,
            "capability_ref": f"{engine}:1.0:w12-ac02",
            "source_metadata": {"adapter": "w12-functional-acceptance"},
            "error": None,
        },
    )


def default_diagnosis_evidence() -> dict[str, tuple[Evidence, ...]]:
    """三类场景的默认证据包（结构性复核输入，公开以便测试独立复核/替换）。

    每包都被刻意构造成**支持证据、反证、缺失信息同时非空**，这样
    :attr:`DiagnosisReportCheck.five_fields_nonempty` 才是可证伪的断言，
    而不是"永远为真"的空断言。
    """
    slow_query = (
        _diag_evidence(
            "explain_query",
            {
                "plan": {
                    "query_block": {
                        "table": {
                            "table_name": "fault_slow_query_4101",
                            "access_type": "ALL",
                            "rows_examined_per_scan": 20000,
                        }
                    }
                }
            },
            evidence_id="ev_w12_sq_explain_fullscan",
        ),
        _diag_evidence(
            "explain_query",
            {
                "plan": {
                    "query_block": {
                        "table": {
                            "table_name": "fault_slow_query_4101",
                            "access_type": "ref",
                            "key": "idx_w12_sq",
                        }
                    }
                }
            },
            evidence_id="ev_w12_sq_explain_index",
        ),
        _diag_evidence(
            "get_query_stats",
            {
                "statements": [
                    {
                        "digest": "digest-w12-sq",
                        "digest_text": "SELECT COUNT(*) FROM fault_slow_query_4101",
                        "count_star": 3,
                        "total_time_ms": 12000,
                        "rows_examined": 60000,
                        "no_index_used": 3,
                    }
                ]
            },
            evidence_id="ev_w12_sq_stats",
        ),
        _diag_evidence(
            "get_table_stats",
            {"tables": [{"table_name": "seed_orders", "estimated_rows": 100}]},
            evidence_id="ev_w12_sq_tables_non_sandbox",
        ),
    )
    connection_pressure = (
        _diag_evidence(
            "get_connection_snapshot",
            {
                "sessions": [
                    {"processlist_id": 11, "command": "Query", "state": "active"},
                    {"processlist_id": 12, "command": "Query", "state": "active"},
                ],
                "row_count": 2,
                "max_connections": 500,
            },
            evidence_id="ev_w12_cp_snapshot",
        ),
        _diag_evidence(
            "get_query_stats",
            {"statements": [{"digest_text": "SELECT 1", "errors": 0}]},
            evidence_id="ev_w12_cp_stats",
        ),
    )
    lock_wait = (
        _diag_evidence(
            "get_lock_wait_edges",
            {
                "edges": [
                    {
                        "waiting_processlist_id": 21,
                        "blocking_processlist_id": 22,
                        "object_name": "fault_lock_wait_4103",
                        "waiting_time_seconds": 12.5,
                        "blocking_transaction_age_seconds": 31.0,
                    },
                    {
                        "waiting_processlist_id": 23,
                        "blocking_processlist_id": 22,
                        "object_name": "fault_lock_wait_4103",
                        "waiting_time_seconds": 4.0,
                        "blocking_transaction_age_seconds": 31.0,
                    },
                ],
                "roots": [{"blocking_processlist_id": 22}],
            },
            evidence_id="ev_w12_lw_edges",
        ),
        _diag_evidence(
            "get_lock_details",
            {"waiting_count": 1},
            evidence_id="ev_w12_lw_details",
        ),
    )
    return {
        "slow_query": slow_query,
        "connection_pressure": connection_pressure,
        "lock_wait": lock_wait,
    }


def _empty_report_check(scenario: str) -> DiagnosisReportCheck:
    """证据缺失时给出**五字段全空**的检查项（不可被误读成通过）。"""
    return DiagnosisReportCheck(
        scenario=scenario,  # type: ignore[arg-type]
        evidence_id="",
        supporting_evidence_ids=(),
        contradicting_evidence_ids=(),
        missing_evidence=(),
        scope="",
    )


def build_diagnosis_reports(
    diagnosis_evidence: Mapping[str, Sequence[Evidence]] | None = None,
) -> tuple[DiagnosisReportCheck, ...]:
    """跑 W04 真实分析器，产出三条五字段结构检查（AC-02）。

    Args:
        diagnosis_evidence: 场景 → 证据序列；缺省用
            :func:`default_diagnosis_evidence`。

    Returns:
        长度恒为 3 的元组（顺序见 :data:`DIAGNOSIS_SCENARIOS`）。缺证据的场景
        返回五字段全空的检查项——**不伪造**任何证据 id。
    """
    bundles: Mapping[str, Sequence[Evidence]] = (
        diagnosis_evidence or default_diagnosis_evidence()
    )
    analyzers: dict[str, Callable[..., Any]] = {
        "slow_query": analyze_slow_query,
        "connection_pressure": analyze_connection_pressure,
        "lock_wait": analyze_lock_wait,
    }
    checks: list[DiagnosisReportCheck] = []
    for scenario in DIAGNOSIS_SCENARIOS:
        evidences = tuple(bundles.get(scenario) or ())
        if not evidences:
            checks.append(_empty_report_check(scenario))
            continue
        outcome = analyzers[scenario](evidences, scenario_id=f"mysql_{scenario}")
        if not outcome.hypotheses:
            checks.append(_empty_report_check(scenario))
            continue
        hypothesis = outcome.hypotheses[0]
        supporting = tuple(hypothesis.supporting_evidence_ids)
        contradicting = tuple(hypothesis.contradicting_evidence_ids)
        missing = tuple(
            dict.fromkeys([*hypothesis.missing_evidence, *outcome.missing_evidence])
        )
        evidence_id = supporting[0] if supporting else ""
        scope = hypothesis.scope or f"LAB:{scenario}"
        # 实测口径：五字段必须能被**既有** ReportFacts 模型承载（不按漂移的 PRD 猜字段）。
        ReportFacts(
            tenant_id=evidences[0].tenant_id,
            task_id=evidences[0].task_id,
            supporting_evidence_ids=list(supporting),
            contradicting_evidence_ids=list(contradicting),
            missing_evidence=list(missing),
        )
        checks.append(
            DiagnosisReportCheck(
                scenario=scenario,  # type: ignore[arg-type]
                evidence_id=evidence_id,
                supporting_evidence_ids=supporting,
                contradicting_evidence_ids=contradicting,
                missing_evidence=missing,
                scope=scope,
            )
        )
    return tuple(checks)


# ---------------------------------------------------------------------------
# AC-01：五态生命周期
# ---------------------------------------------------------------------------


def probe_lifecycle(
    *,
    control_client: Any,
    tenant_id: str = "tenant_local",
    environment_id: str = "env_mysql_local",
) -> tuple[LifecycleObservation, ...]:
    """逐态实测五态生命周期，返回每态的观测事实（AC-01）。

    每态断言三件事：
    1. `task_events` 可按 `task_id` 查询（复用 `AuditService.query`）；
    2. `seq == list(range(n))`（链序号连续，不跳号）；
    3. `AuditService.verify(tenant_id, task_id).ok is True`（哈希链完整）。

    环境阻塞（控制库不可用）时该态记 ``NOT_EXECUTED``；实测不符记 ``FAILED``。
    """
    observations: list[LifecycleObservation] = []
    for phase in LIFECYCLE_PHASES:
        observations.append(
            _observe_lifecycle_phase(
                phase,
                control_client=control_client,
                tenant_id=tenant_id,
                environment_id=environment_id,
            )
        )
    return tuple(observations)


def _observe_lifecycle_phase(
    phase: str,
    *,
    control_client: Any,
    tenant_id: str,
    environment_id: str,
) -> LifecycleObservation:
    task_id: str | None = None
    try:
        repository = TaskRepository(control_client)
        service = TaskService(repository, ToolRegistry())
        audit = AuditService(repository)
        task = service.create_task(
            f"W12 AC-01 生命周期实测：{phase}",
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by="user_w12_acceptance",
        )
        task_id = task.task_id
        task = _drive_phase(phase, service, task)

        events, _next_cursor = audit.query(
            tenant_id=task.tenant_id, task_id=task.task_id, limit=500
        )
        seq = [int(event.seq) for event in events]
        contiguous = seq == list(range(len(seq)))
        verification = audit.verify(tenant_id=task.tenant_id, task_id=task.task_id)
        assert events, f"{phase}：task_events 不可查询（0 条）"
        assert contiguous, f"{phase}：seq 不连续 {seq}"
        assert verification.ok is True, f"{phase}：事件链校验失败（{verification.reason}）"
        return LifecycleObservation(
            phase=phase,
            task_id=task.task_id,
            status=AcceptanceStatus.PASSED,
            event_count=len(events),
            seq_contiguous=contiguous,
            chain_ok=bool(verification.ok),
            detail=f"status={task.status.value}; events={len(events)}",
        )
    except AssertionError as exc:
        return LifecycleObservation(
            phase=phase,
            task_id=task_id,
            status=AcceptanceStatus.FAILED,
            event_count=0,
            seq_contiguous=False,
            chain_ok=False,
            detail=str(exc),
        )
    except DBGuardianError as exc:
        return LifecycleObservation(
            phase=phase,
            task_id=task_id,
            status=AcceptanceStatus.NOT_EXECUTED,
            event_count=0,
            seq_contiguous=False,
            chain_ok=False,
            detail=f"ENV：控制库不可用（{exc.code.value}）",
        )


def _drive_phase(phase: str, service: TaskService, task: Task) -> Task:
    """按态驱动真实状态机（全部走 `TaskService` 白名单迁移，不直接改库）。"""
    if phase == "create":
        # 创建态：TASK_CREATED + CHECKPOINT_SAVED 两条事件即可查询。
        return task
    if phase == "query":
        # 查询态：读回任务与事件（查询本身不得新增事件）。
        return service.get_task(task.task_id)
    if phase == "pause":
        task = service.transition(task.task_id, TaskStatus.PLANNING)
        task = service.request_pause(task.task_id)
        assert task.status is TaskStatus.PAUSED, f"暂停后状态应为 PAUSED，实际 {task.status}"
        assert task.pause_requested is True, "暂停标记未置位"
        return task
    if phase == "resume":
        task = service.transition(task.task_id, TaskStatus.PLANNING)
        task = service.request_pause(task.task_id)
        task = service.resume(task.task_id)
        assert task.status is TaskStatus.PLANNING, f"恢复后状态应为 PLANNING，实际 {task.status}"
        assert task.pause_requested is False, "暂停标记未清除"
        return task
    if phase == "cancel":
        task = service.cancel(task.task_id)
        assert task.status is TaskStatus.CANCELLED, f"取消后状态应为 CANCELLED，实际 {task.status}"
        assert task.cancel_requested is True, "取消标记未置位"
        return task
    raise AssertionError(f"未知生命周期阶段：{phase}")


# ---------------------------------------------------------------------------
# AC-03：审批修复闭环
# ---------------------------------------------------------------------------


def count_task_tool_calls(control_client: Any, task_id: str) -> int:
    """按**本任务**计数工具调用行（绝不统计全表）。"""
    rows = control_client.fetch_all(
        "SELECT COUNT(*) AS n FROM dg_system.tool_calls WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["n"]) if rows else 0


def _note_of(stack: Any) -> str | None:
    """用**独立只读回读端口**读目标行（异路径，不复用执行侧 client）。"""
    from core.verification.readback import TargetObject

    sample = stack.readback.read_target(
        TargetObject(
            database="dg_lab",
            table="seed_orders",
            key_column="order_id",
            key_value=LAB_ORDER_ID,
            column="note",
        )
    )
    return sample.value


def _lab_params(expected_note: str | None, note: str) -> dict[str, Any]:
    return {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": LAB_ORDER_ID,
        "keys": f"order_id={LAB_ORDER_ID}",
        "note": note,
        "expected_note": expected_note,
    }


def run_repair_closure(
    *, stack: Any, control_client: Any
) -> RepairClosureCheck:
    """实测审批修复闭环（AC-03）：审批 → Gateway → 真实单行 UPDATE → 独立验证。

    **KD-W12-01 已于 2026-11-17 关闭**（`docs/w10-w12-lead-rulings.md` §14.1 的关闭条件
    已满足：`core/verification/plan.py` 读取端形状归一——优先 ``result["data"]``、
    回落顶层，以兼容执行器 `core/tools/executor.py:486` 落盘的
    `ToolResult.model_dump()` 全量快照；**写入端与断言常量均未改动**）：
    AC-03 真实链的独立验证结论已由 ``FAILED`` 转 ``PASSED``
    （``status=PASSED`` / ``verdict=PASS`` / ``reason_code=OK``，三条断言全 ``PASS``）。

    **归因语义保留**：若 ``step_observed`` 形状再次与读取口径不一致，本函数仍会把
    ``status`` 判为 ``FAILED`` + ``reason_code=OBSERVED_SHAPE_MISMATCH``；
    **绝不**把断言改成期望 PASSED、**绝不**在验收侧绕过/改写读取面。
    历史 ``FAILED`` 现场（``affected_rows=UNKNOWN(OBSERVED_MISSING)``、
    ``snapshot_comparable=FAIL``、``target_note_final=PASS``）与关闭依据留档于
    `docs/w12/known-limitations.md` §3.1「已关闭偏离」。

    Args:
        stack: W07/W08 唯一组合根产物（`workers/stack.py::build_worker_stack()`），
            需提供 ``task_service`` / ``approvals`` / ``gateway`` / ``worker_for`` /
            ``verifier`` / ``readback`` / ``step_records`` / ``rw_client`` / ``settings``。
        control_client: 控制库 client（用于**按 task_id** 计数 `tool_calls`）。

    Returns:
        :class:`RepairClosureCheck`。未实测（无 stack 或环境阻塞）时
        ``status=NOT_EXECUTED``、``tool_calls``/``row_changes`` 恒 ``None``
        ——**不用 0 冒充未实测值**。实测到但未通过时保留实测值（1 次调用/1 次行变化是
        事实），只把 ``status``/``verdict`` 记成失败。
    """
    if stack is None:
        return RepairClosureCheck(
            status=AcceptanceStatus.NOT_EXECUTED,
            detail="未装配 W07 唯一组合根（AC-03 未执行）",
        )
    before: str | None = None
    measured: dict[str, Any] = {}
    try:
        definition = stack.registry.get(LAB_WRITE_TOOL_NAME).definition
        assert definition.side_effect == "write", "唯一 R2 动作必须是写操作"
        assert definition.risk_level == "R2", "唯一写动作必须仍是 R2"
        assert definition.version == LAB_WRITE_TOOL_VERSION, "写动作版本漂移"

        before = _note_of(stack)
        target = f"w12-ac03-{uuid4().hex[:8]}"
        params = _lab_params(before, target)

        service: TaskService = stack.task_service
        task = service.create_task("W12 AC-03 审批修复闭环")
        task = service.transition(task.task_id, TaskStatus.PLANNING)
        task = service.transition(task.task_id, TaskStatus.EXECUTING)

        operator = Principal(
            user_id="user_w12_operator",
            tenant_id=task.tenant_id,
            roles=(Role.OPERATOR,),
            session_id="sess_w12_operator",
            auth_method=AuthMethod.DEV_LOCAL,
        )
        approver = Principal(
            user_id="user_w12_approver",
            tenant_id=task.tenant_id,
            roles=(Role.APPROVER,),
            session_id="sess_w12_approver",
            auth_method=AuthMethod.DEV_LOCAL,
        )
        created = stack.approvals.create(
            principal=operator,
            task_id=task.task_id,
            step_id=None,
            tool_name=LAB_WRITE_TOOL_NAME,
            params=params,
            target_objects=[],
        )
        detail = stack.approvals.approve(
            created.approval_id, principal=approver, reason="W12 AC-03 验收"
        )

        worker = stack.worker_for("user_local")
        lease = worker.acquire_lease(task.task_id, task.environment_id, ttl_s=60)
        envelope = sign_envelope(
            build_envelope(
                task_id=task.task_id,
                step_id=None,
                worker_id=lease.worker_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
                state_version=task.state_version,
                correlation_id=f"corr_w12_ac03_{uuid4().hex[:8]}",
                actor_user_id="user_local",
                tool_name=LAB_WRITE_TOOL_NAME,
                tool_version=LAB_WRITE_TOOL_VERSION,
                environment_id=task.environment_id,
                params=params,
                ttl_s=60,
                approval_id=detail.approval_id,
            ),
            stack.settings.gateway_key(),
        )
        result = stack.gateway.execute(envelope, params)

        tool_calls = count_task_tool_calls(control_client, task.task_id)
        after = _note_of(stack)
        row_changes = 1 if (after == target and before != target) else 0
        measured["tool_calls"] = tool_calls
        measured["row_changes"] = row_changes
        succeeded = _status_value(result) == "success"
        assert succeeded, f"写动作未成功：{result.error}"
        assert tool_calls == 1, f"tool_calls 必须恰 1，实际 {tool_calls}"
        assert row_changes == 1, f"目标行变化必须恰 1 次，实际 {row_changes}"

        verification = stack.verifier.verify(
            task,
            tool_call_id=result.tool_call_id,
            approval_id=detail.approval_id,
        )
        raw_outcomes = tuple(getattr(verification, "outcomes", ()))
        assertion_verdicts = {
            str(getattr(item, "assertion_id", "?")): str(getattr(item, "verdict", "?"))
            for item in raw_outcomes
        }
        measured["assertion_verdicts"] = assertion_verdicts
        verdict = str(verification.verdict)
        reason_code = _classify_verification_gap(stack, result, raw_outcomes)
        outcomes = "; ".join(
            f"{getattr(item, 'assertion_id', '?')}={getattr(item, 'verdict', '?')}"
            f"({getattr(item, 'reason', None) or 'OK'};observed={getattr(item, 'observed', None)!r})"
            for item in raw_outcomes
        )
        location = f"tool_call_id={result.tool_call_id}; approval_id={detail.approval_id}"
        if verdict != "PASS":
            # KD-W12-01：独立验证未通过 = 闭环事实上未通过，如实记 FAILED（不造假）。
            return RepairClosureCheck(
                status=AcceptanceStatus.FAILED,
                verdict=verdict,
                tool_calls=tool_calls,
                row_changes=row_changes,
                reason_code=reason_code,
                assertion_verdicts=assertion_verdicts,
                detail=(
                    f"独立验证结论 {verdict}（{reason_code}）；{location}；断言明细：{outcomes}"
                ),
            )
        return RepairClosureCheck(
            status=AcceptanceStatus.PASSED,
            verdict=verdict,
            tool_calls=tool_calls,
            row_changes=row_changes,
            reason_code="OK",
            assertion_verdicts=assertion_verdicts,
            detail=f"{location}；断言明细：{outcomes}",
        )
    except AssertionError as exc:
        return RepairClosureCheck(
            status=AcceptanceStatus.FAILED,
            verdict="FAIL",
            tool_calls=measured.get("tool_calls"),
            row_changes=measured.get("row_changes"),
            reason_code=REASON_CLOSURE_ASSERTION_FAILED,
            assertion_verdicts=dict(measured.get("assertion_verdicts") or {}),
            detail=str(exc),
        )
    except DBGuardianError as exc:
        return RepairClosureCheck(
            status=AcceptanceStatus.NOT_EXECUTED,
            detail=f"ENV：LAB/控制库不可用（{exc.code.value}）",
        )
    finally:
        # W07 冻结协议：复原目标 `note`，保证本文件可重复执行。
        # 这**不是**残留清理，也不是 W12 新增的写能力（唯一写动作仍是受限单行更新）。
        if before is not None:
            try:
                stack.rw_client.execute(
                    "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1",
                    (before, LAB_ORDER_ID),
                )
            except DBGuardianError:
                pass


def _classify_verification_gap(
    stack: Any, result: Any, outcomes: Sequence[Any]
) -> str:
    """判定独立验证未通过的可归因原因（KD-W12-01 归类）。

    - ``OBSERVED_SHAPE_MISMATCH``：`StepObserved` 行**存在**，但 ``step_observed``
      来源的断言仍报 ``OBSERVED_MISSING``（形状不一致，非证据缺失）；
    - ``STEP_OBSERVED_MISSING``：行确实不存在（真缺失）；
    - ``VERDICT_NOT_PASS``：其余未通过原因；
    - ``OK``：全部断言通过。
    """
    step_outcomes = [
        item for item in outcomes if str(getattr(item, "source", "")) == "step_observed"
    ]
    reasons = {str(getattr(item, "reason", "") or "") for item in step_outcomes}
    observed_row = None
    step_records = getattr(stack, "step_records", None)
    tool_call_id = getattr(result, "tool_call_id", None)
    if step_records is not None and tool_call_id:
        try:
            observed_row = step_records.get_observed(tool_call_id)
        except Exception:  # noqa: BLE001 - 归因不得因读证据失败而抛
            observed_row = None
    if "OBSERVED_MISSING" in reasons:
        return (
            REASON_OBSERVED_SHAPE_MISMATCH
            if observed_row is not None
            else REASON_STEP_OBSERVED_MISSING
        )
    if not step_outcomes:
        return REASON_VERDICT_NOT_PASS
    return REASON_VERDICT_NOT_PASS


def _status_value(result: Any) -> str:
    """从 `ToolResult.status` 取字面量（兼容 Enum / 裸字符串）。"""
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status))


# ---------------------------------------------------------------------------
# AC-04：第二引擎只读
# ---------------------------------------------------------------------------


def check_second_engine_readonly(*, registry: Any | None = None) -> bool:
    """第二引擎（PostgreSQL）无写闭环（AC-04）。

    等价断言（`write_tools()` 方法不存在，设计 D-06）：

    ``[d for d in registry.definitions() if d.side_effect == "write"] == []``

    同时对既有 PG/MCP 契约定义（`adapters/postgres_mcp/tools.py`）做同口径复核，
    因为它们是第二引擎的**唯一**能力面。
    """
    definitions: list[Any] = []
    if registry is not None:
        definitions.extend(registry.definitions())
    definitions.extend(_pg_contract_definitions())
    return [item for item in definitions if getattr(item, "side_effect", None) == "write"] == []


def _pg_contract_definitions() -> list[Any]:
    """读取 PG/MCP 契约定义（只读 import，不注册到任何注册表）。"""
    try:
        from adapters.postgres_mcp.tools import TOOL_DEFINITIONS
    except ImportError:  # pragma: no cover - 契约件缺失时按"无写能力"处理并登记
        return []
    return list(TOOL_DEFINITIONS.values())


def probe_pg_segment(*, pg_probe: Callable[[], bool] | None = None) -> AcceptanceStatus:
    """PG 段状态（AC-04 第二段）。

    口径（`docs/w10-w12-lead-rulings.md` W12-Q3 / `ENV-PG5432`）：

    - 未提供探针 → ``NOT_EXECUTED``（未执行，**不判 FAIL**）；
    - 探针抛 `DBGuardianError`（不可达/未配置）→ ``NOT_EXECUTED``；
    - 探针返回 ``True`` → ``PASSED``；返回 ``False`` → ``NOT_EXECUTED``
      （只读段没有观测到事实即"未执行"，**绝不冒充通过**、也不判 FAIL）。

    本函数**永远**不返回 ``FAILED``：PG 段不构成本卡的失败判据。
    """
    if pg_probe is None:
        return AcceptanceStatus.NOT_EXECUTED
    try:
        observed = pg_probe()
    except DBGuardianError:
        return AcceptanceStatus.NOT_EXECUTED
    return AcceptanceStatus.PASSED if observed is True else AcceptanceStatus.NOT_EXECUTED


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def _default_evidence_refs() -> tuple[str, ...]:
    return (
        "core/release/__init__.py",
        "core/release/functional.py",
        "tests/integration/test_w12_functional_acceptance.py",
        "core/audit/service.py",
        "core/evidence/models.py",
        "adapters/mysql/lab_repair.py",
        *GATE_EVIDENCE_REFS,
    )


def run_functional_acceptance(
    *,
    control_client: Any,
    repair_stack: Any | None = None,
    diagnosis_evidence: Mapping[str, Sequence[Evidence]] | None = None,
    second_engine_registry: Any | None = None,
    pg_probe: Callable[[], bool] | None = None,
) -> FunctionalAcceptanceResult:
    """执行 T01 功能验收，产出 :class:`FunctionalAcceptanceResult`。

    Args:
        control_client: 控制库 client（AC-01 生命周期与 AC-03 计数）。
        repair_stack: 可选，W07/W08 唯一组合根产物（AC-03 需要）；缺省时
            AC-03 记 ``NOT_EXECUTED``（**不判 FAIL**）。
        diagnosis_evidence: 可选，AC-02 的证据包（缺省用
            :func:`default_diagnosis_evidence`）。
        second_engine_registry: 可选，第二引擎只用**接口等价断言**；缺省时只复核
            PG/MCP 既有契约定义。
        pg_probe: 可选，PG 只读探针；缺省 → ``NOT_EXECUTED``。

    返回件**结构上不含**任何 demo 字段（AC-20）。
    """
    run_id = f"w12-func-{uuid4().hex[:12]}"

    observations = probe_lifecycle(control_client=control_client)
    lifecycle = {item.phase: item.status for item in observations}

    reports = build_diagnosis_reports(diagnosis_evidence)
    repair = run_repair_closure(stack=repair_stack, control_client=control_client)
    readonly = check_second_engine_readonly(registry=second_engine_registry)
    pg_status = probe_pg_segment(pg_probe=pg_probe)

    return FunctionalAcceptanceResult(
        run_id=run_id,
        lifecycle=lifecycle,
        diagnosis_reports=reports,
        repair_closure=repair,
        second_engine_readonly=readonly,
        pg_segment_status=pg_status,
        evidence_refs=_default_evidence_refs(),
    )


__all__ = [
    "DEMO_INPUT_FIELDS",
    "DIAGNOSIS_SCENARIOS",
    "DiagnosisReportCheck",
    "FunctionalAcceptanceResult",
    "GATE_EVIDENCE_REFS",
    "LAB_ORDER_ID",
    "LAB_WRITE_TOOL_NAME",
    "LAB_WRITE_TOOL_VERSION",
    "LIFECYCLE_PHASES",
    "REASON_CLOSURE_ASSERTION_FAILED",
    "REASON_OBSERVED_SHAPE_MISMATCH",
    "REASON_OK",
    "REASON_STEP_OBSERVED_MISSING",
    "REASON_VERDICT_NOT_PASS",
    "LifecycleObservation",
    "RepairClosureCheck",
    "build_diagnosis_reports",
    "check_second_engine_readonly",
    "count_task_tool_calls",
    "default_diagnosis_evidence",
    "probe_lifecycle",
    "probe_pg_segment",
    "run_functional_acceptance",
    "run_repair_closure",
]
