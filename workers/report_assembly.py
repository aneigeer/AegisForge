"""W09 收口 L2：报告发布器（Worker 生产装配的唯一出口）。

只做三件事：**取事实 → 调 T03 的 ReportBuilder → 如实计数**。
不改 `core/report/**`、不新增 API 端点、不发新事件类型、不持有 DSN、
不写裸 SQL（数据只经既有仓储）。

失败语义（设计 §4.5）：报告是执行事实**下游**的物化产物，
失败不回滚任务状态、不阻塞主循环、不冒泡，只用结构化日志 + RunReport 计数如实登记。

为什么触发点在 `WorkerRunner._execute()` 的 `ToolResult` 之后（设计 §4.1）：
Worker 从不把任务推到终态（执行成功后任务仍是 `EXECUTING`），所以"任务终态"这个
触发点在本进程内永不发生；而拿到 `ToolResult` 时是唯一同时持有 `task`、
`tool_call_id` 与 `outcome` 的位置。

`REPORT_GENERATED` 由 **builder 内部** `emit_required` 发（必成功语义）；本模块
自己**不发任何审计事件**，因此不需要 `try_emit`，也不会把调度层的降级口径混进
业务事件。

装配纪律（`tests/unit/test_worker_layering.py` 静态扫描本文件）：

- 本模块**不**构造执行网关，也**不**构造工具注册表——写入口只有
  `adapters/lab_assembly.py` 一处；
- 本模块**不**直接依赖数据库驱动，连接与账号由组合根注入的仓储持有；
- 本模块只经既有仓储读写，因此源码中不出现任何裸 SQL 片段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.evidence.models import ReportFacts
from core.evidence.repository import EvidenceRepository
from core.logging import get_logger
from core.report.builder import ReportBuilder, StepChain
from core.report.models import Recommendation
from core.report.repository import ReportRepository
from core.security.canonical import canonical_digest
from core.tasks.models import Task
from core.verification.repository import VerificationResultRepository

logger = get_logger(__name__)

#: 结构化日志告警名（**不是**审计事件类型；`core/audit/events.py` 零改动）。
ALERT_REPORT_FAILED = "REPORT_FAILED"

#: 幂等跳过原因：该任务已有快照，不重复生成（`report_id` 非确定性，PK 不能承担幂等）。
REASON_ALREADY_GENERATED = "ALREADY_GENERATED"

#: 证据分页被上限截断（不伪造 id，只如实标注）。
REASON_EVIDENCE_TRUNCATED = "EVIDENCE_TRUNCATED"

#: 建议文本必须避开 AC-11 的完成措辞黑名单
#: （已修复 / 已执行 / 已完成 / 已恢复 / 已解决 / fixed / resolved / already applied）。
RECOMMENDATION_EXECUTED = "建议人工核对目标库当前状态与验证结论，再决定后续处置"
RECOMMENDATION_UNKNOWN = "建议先补齐执行回执与验证结论，再判断动作是否生效"

#: 单页 50 条 × 5 页 = 250 条上限；超出记 `EVIDENCE_TRUNCATED`，不伪造 id。
MAX_EVIDENCE_PAGES = 5
EVIDENCE_PAGE_SIZE = 50


@dataclass(frozen=True)
class ReportOutcome:
    """一次发布的结论；`skipped_reason`、`failed`、`generated` 三者互斥。

    - `generated=True`：快照已落库且 `REPORT_GENERATED` 已发出，`report_id` 为落库主键；
    - `skipped_reason=ALREADY_GENERATED`：已有快照，本次未写任何行、未发任何事件；
    - `failed=True`：取事实/组装/落库任一步抛错，已降级为结构化日志。
    """

    generated: bool
    failed: bool = False
    skipped_reason: str | None = None
    #: 成功落库时的 `report_id`（`rpt_` 前缀）；供运行器日志对账，失败/跳过时为 `None`。
    report_id: str | None = None


@dataclass(frozen=True)
class _ChainAdapter:
    """`StepPort` 适配器：审批状态 + 步骤证据存在性（两次单行只读查询）。

    直接复用 `core/report/builder.py` 的 `StepChain` dataclass，**不改 builder**。
    """

    approvals: Any
    step_records: Any
    tenant_id: str

    def chain_for_task(self, *, task_id: str, tenant_id: str) -> StepChain:
        tenant = tenant_id or self.tenant_id
        approval_status: str | None = None
        approval_id: str | None = None
        rows, _ = self.approvals.list(tenant_id=tenant, task_id=task_id, limit=1)
        if rows:
            status = getattr(rows[0], "status", None)
            approval_status = getattr(status, "value", status)
            approval_id = getattr(rows[0], "approval_id", None)
        prepared = False
        observed = False
        if approval_id:
            evidence = self.step_records.evidence_for_approvals([approval_id]) or {}
            row = evidence.get(approval_id) or {}
            prepared = bool(row.get("step_prepared"))
            observed = bool(row.get("step_observed_outcome"))
        return StepChain(
            approval_status=approval_status,
            has_step_prepared=prepared,
            has_step_observed=observed,
        )


@dataclass(frozen=True)
class _VerificationForCall:
    """`VerificationPort` 适配器：**每次 publish 新建**，绑定本轮 `tool_call_id`。

    为什么按 `tool_call_id` 而不是按任务查：`core/verification/repository.py` 只有
    「按 tool_call 查」没有「按 task 查」，本卡不改 `core/verification/**`，因此以
    本轮 `ToolResult.tool_call_id` 锚定；同一调用可有多轮验证，取**最后一条**。
    """

    results: Any
    tenant_id: str
    tool_call_id: str

    def latest_for_task(self, *, task_id: str, tenant_id: str) -> Any | None:
        rows = self.results.list_by_tool_call(
            tenant_id or self.tenant_id, self.tool_call_id
        )
        return rows[-1] if rows else None


class TaskReportPublisher:
    """把"一次执行事实"物化成一份只追加快照；失败只计数、不冒泡。"""

    def __init__(
        self,
        *,
        evidence: Any,
        reports: Any,
        approvals: Any,
        step_records: Any,
        verifications: Any,
        audit: Any,
        gate: Any,
        tenant_id: str,
        policy_version: str,
        database_id: str,
    ) -> None:
        self._evidence = evidence
        self._reports = reports
        self._approvals = approvals
        self._step_records = step_records
        self._verifications = verifications
        self._audit = audit
        self._gate = gate
        self._tenant_id = tenant_id
        self._policy_version = policy_version
        self._database_id = database_id
        self._chain = _ChainAdapter(
            approvals=approvals, step_records=step_records, tenant_id=tenant_id
        )

    @property
    def redaction(self) -> Any:
        """报告通道所用的脱敏闸门（供 `is stack.redaction` 断言同一实例）。"""
        return self._gate

    def publish(
        self, task: Task, *, tool_call_id: str, succeeded: bool
    ) -> ReportOutcome:
        """生成一份报告快照；**绝不抛异常**。

        Args:
            task: 本轮执行的任务（只读使用 `task_id`/`tenant_id`/`environment_id`）。
            tool_call_id: 本轮 `ToolResult.tool_call_id`，用于锚定验证结论。
            succeeded: 是否 `ToolStatus.SUCCESS`；**只用于选一条不含完成措辞的建议
                文本**（AC-11），不参与任何状态推导——`ActionState`/`FactStatus` 仍
                完全由 `StepPort` + `ReportEvidencePort` 的事实推导。

        Returns:
            `ReportOutcome`；调用方据此计数，不据此改变执行结论。
        """
        try:
            existing = self._reports.list_by_task(
                task.task_id, self._tenant_id, limit=1
            )
            if existing:
                return ReportOutcome(
                    generated=False, skipped_reason=REASON_ALREADY_GENERATED
                )
            facts = self._collect_facts(task)
            builder = ReportBuilder(
                evidence=self._evidence,
                verifications=_VerificationForCall(
                    results=self._verifications,
                    tenant_id=self._tenant_id,
                    tool_call_id=tool_call_id,
                ),
                steps=self._chain,
                audit=self._audit,
                gate=self._gate,
                reports=self._reports,
                policy_version=self._policy_version,
                database_id=self._database_id,
            )
            report = builder.build(
                task=task,
                facts=facts,
                recommendations=[
                    Recommendation(
                        text=(
                            RECOMMENDATION_EXECUTED
                            if succeeded
                            else RECOMMENDATION_UNKNOWN
                        )
                    )
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 报告失败不得污染执行结论
            return self._record_failure(task, exc)
        return ReportOutcome(generated=True, report_id=report.report_id)

    # ---- 内部 ----------------------------------------------------------
    def _collect_facts(self, task: Task) -> ReportFacts:
        """逐页采集本任务证据（≤ `MAX_EVIDENCE_PAGES` 页），不臆造任何字段。

        两点**刻意的克制**：

        - `contradicting_evidence_ids` 恒为空列表：`evidence` 表没有"支持/反证"
          列，凭工具名猜正负会伪造事实；反证只能由 W11 的验证链路产出；
        - 超出页数上限只记 `EVIDENCE_TRUNCATED` 告警并**按已采集部分**报事实，
          **不伪造 id**、不把"没读到"写成"不存在"。
        """
        ids: list[str] = []
        cursor: str | None = None
        pages = 0
        while pages < MAX_EVIDENCE_PAGES:
            items, cursor = self._evidence.list_by_task(
                task.task_id,
                self._tenant_id,
                limit=EVIDENCE_PAGE_SIZE,
                cursor=cursor,
            )
            ids.extend(str(item.evidence_id) for item in items)
            pages += 1
            if not cursor:
                break
        if cursor:
            logger.warning(
                "证据分页达到上限，只按已采集部分生成报告（不伪造证据 id）",
                extra={
                    "alert": REASON_EVIDENCE_TRUNCATED,
                    "task_id": task.task_id,
                    "tenant_id": self._tenant_id,
                    "pages": pages,
                    "collected": len(ids),
                },
            )
        return ReportFacts(
            tenant_id=self._tenant_id,
            task_id=task.task_id,
            supporting_evidence_ids=list(dict.fromkeys(ids)),
            contradicting_evidence_ids=[],
            missing_evidence=[],
        )

    def _record_failure(self, task: Task, exc: BaseException) -> ReportOutcome:
        """把失败降级为结构化日志（不打原文，只打摘要）。"""
        code = getattr(getattr(exc, "code", None), "value", None)
        logger.error(
            "报告生成失败，本轮继续（不回滚、不阻塞、不冒泡）",
            extra={
                "alert": ALERT_REPORT_FAILED,
                "task_id": getattr(task, "task_id", None),
                "tenant_id": self._tenant_id,
                "code": code,
                "error_type": type(exc).__name__,
                "message_digest": canonical_digest(
                    {"type": type(exc).__name__, "message": str(exc)}
                ),
            },
        )
        return ReportOutcome(generated=False, failed=True)


def build_report_publisher(
    *,
    settings: Any,
    tenant_id: str,
    control_client: Any,
    audit: Any,
    gate: Any,
    approvals: Any,
    step_records: Any,
) -> TaskReportPublisher:
    """组合根入口：把控制库客户端装配成一条只读事实 + 只追加快照的发布器。

    `policy_version` / `database_id` 取**配置事实**（`settings`），不吃 builder 的
    缺省常量；`gate` 由组合根传入**唯一实例**，使报告通道与未来在 Worker 内装配的
    闸门②共用同一对象（设计 §4.3）。
    """
    return TaskReportPublisher(
        evidence=EvidenceRepository(control_client),
        reports=ReportRepository(control_client),
        approvals=approvals,
        step_records=step_records,
        verifications=VerificationResultRepository(control_client),
        audit=audit,
        gate=gate,
        tenant_id=tenant_id,
        policy_version=settings.policy_version,
        database_id=settings.database_id,
    )


__all__ = [
    "ALERT_REPORT_FAILED",
    "EVIDENCE_PAGE_SIZE",
    "MAX_EVIDENCE_PAGES",
    "REASON_ALREADY_GENERATED",
    "REASON_EVIDENCE_TRUNCATED",
    "RECOMMENDATION_EXECUTED",
    "RECOMMENDATION_UNKNOWN",
    "ReportOutcome",
    "TaskReportPublisher",
    "build_report_publisher",
]
