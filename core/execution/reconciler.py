"""未观测调用的核对（P0-18/P0-19）：收敛到 NEEDS_RECONCILIATION，绝不重放。

设计要点：
- 只读证据 + 状态迁移，**不持有任何工具注册表**，因此结构上不可能触发工具重放；
- `UNKNOWN_OUTCOME` / 超时 / 进程崩溃都按"结果未知"处理，不得解释为失败或成功；
- 证据不足（任务已终态、任务不存在、Observed 已补齐但无法判定）转 `MANUAL_INTERVENTION`；
- 核对结论写 `task_events`（只追加）与 `tasks.status`，`step_observed` 无更新列。

W09 T06（KD-2 / AC-20）增量：`sweep()` 的返回值**契约不变**（仍是
`list[ReconcileResult]`），只新增 `sweep_detailed() -> SweepReport`。区别是后者把
两个此前被混为一谈的量分开：

- `scanned_total`：真实扫描行数 = 各租户分页 `find_unobserved()` 返回行数之和，
  **在 `_reconcile_one()` 过滤之前累加**（二次确认已补齐 Observed 的调用会被过滤，
  但它**确实被扫到了**，必须计入）；
- `conclusions`：产出结论的条目（`len(conclusions)` 才是 W08 口径的 `scanned`）。

为什么不让 `sweep()` 直接返回 `SweepReport`：仓库里有 7 处既有调用点按
`results == []` / `results[0]` / `len(results)` 使用返回值，改返回类型会让它们
全部变红（`docs/w09/03-task-cards.md:666` 明令禁止）。

W10 L1 增量（C6 环境残留污染）：新增 `sweep_task()`，把扫描范围按
`(tenant_id, task_id)` 收窄到**单个任务**；`sweep()` / `sweep_detailed()` 的
参数表、默认值、返回类型与行为**逐字不变**（`_sweep()` 是两者的唯一实现，
`sweep_detailed()` 一行委托、`task_id=None`）。

为什么不给 `sweep()` 加参数：`tests/unit/test_w09_sweep_scanned.py:292-297` 用
`inspect.signature(Reconciler.sweep)` 逐字断言参数表为
`["self", "tenant_id", "older_than_s"]`。加参数会让这条冻结断言变红，唯一"修法"
是改断言 —— 那属于"为让代码过而改测试"，明确避免。新能力以**新方法**提供，
与 `sweep_detailed()` 当初"新增而非改 `sweep()`"的先例完全同构。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.errors import RequestInvalidError, StateConflictError, TaskNotFoundError
from core.execution.step_records import StepPrepared, StepRecordRepository
from core.tasks.models import TaskStatus
from core.tasks.repository import TaskRepository
from core.tasks.state_machine import can_transition

CONCLUSION_NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
CONCLUSION_MANUAL_INTERVENTION = "MANUAL_INTERVENTION"

REASON_NOT_OBSERVED = "PREPARED_WITHOUT_OBSERVED"
REASON_NO_LEGAL_TRANSITION = "NO_LEGAL_TRANSITION"
REASON_TASK_MISSING = "TASK_NOT_FOUND"
REASON_ALREADY_CONVERGED = "ALREADY_CONVERGED"


@dataclass(frozen=True)
class ReconcileResult:
    """单条未观测调用的核对结论；`transitioned` 表示本次是否推进了任务状态。"""

    prepared_id: str
    tool_call_id: str
    task_id: str
    conclusion: str
    task_status: str
    transitioned: bool
    reason: str
    approval_id: str | None = None
    action_digest: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "prepared_id": self.prepared_id,
            "tool_call_id": self.tool_call_id,
            "task_id": self.task_id,
            "conclusion": self.conclusion,
            "task_status": self.task_status,
            "transitioned": self.transitioned,
            "reason": self.reason,
            "approval_id": self.approval_id,
            "action_digest": self.action_digest,
        }


@dataclass(frozen=True)
class SweepReport:
    """一轮核对的**完整**口径（W09 T06 / AC-20 / KD-2）。

    `scanned_total` 与 `conclusions` 是两件事，不得互相冒充：

    - `scanned_total`：真实扫描行数（各租户分页 `find_unobserved()` 返回行数之和，
      过滤前计数）；
    - `conclusions`：产出结论的条目；`len(conclusions)` 等于 W08 口径的 `scanned`。
    """

    scanned_total: int
    conclusions: tuple[ReconcileResult, ...]
    scanned_tenants: tuple[str, ...]

    @property
    def conclusion_count(self) -> int:
        """产出结论条数（= `len(conclusions)`，供调用方免去 `len()` 拼写差异）。"""
        return len(self.conclusions)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "scanned_total": self.scanned_total,
            "conclusions": [item.to_public_dict() for item in self.conclusions],
            "scanned_tenants": list(self.scanned_tenants),
        }


class Reconciler:
    """收敛"已 prepared 未 observed"的调用；全程不调用任何工具。"""

    def __init__(
        self,
        step_records: StepRecordRepository,
        task_repository: TaskRepository,
        audit: AuditService,
    ) -> None:
        self._step_records = step_records
        self._tasks = task_repository
        self._audit = audit

    def sweep(
        self, *, tenant_id: str | None = None, older_than_s: float = 60.0
    ) -> list[ReconcileResult]:
        """扫描并收敛；返回每条调用的核对结论。

        分页遍历到取完为止。`_reconcile_one()` 只写审计事件与任务状态，
        **不会**写 `step_observed`，因此结果集在遍历中不变，按 offset 递增
        不会重复处理同一条；单次上限 500 条时继续翻页，避免历史残留把
        待核对记录挤出窗口（超过 50 条未观测调用时旧实现会漏收敛）。

        **签名与返回类型是冻结契约**（`03-task-cards.md:666`）：需要真实扫描行数
        时用 `sweep_detailed()`，不要改本方法的返回值。
        """
        return list(
            self.sweep_detailed(tenant_id=tenant_id, older_than_s=older_than_s).conclusions
        )

    def sweep_detailed(
        self, *, tenant_id: str | None = None, older_than_s: float = 60.0
    ) -> SweepReport:
        """同 `sweep()`，但返回 `SweepReport`（含真实扫描行数口径，AC-20）。

        `scanned_total` 在 `_reconcile_one()` **过滤前**按 `len(batch)` 累加：
        已补齐 Observed、任务不存在等情形都会产出结论；而"二次确认已补齐"的条目
        会被过滤掉、不产出结论——它仍占用了扫描成本，必须计入 `scanned_total`。
        反过来，把计数放在过滤之后会让"扫了 500 条、只收敛 2 条"被记成"扫了 2 条"，
        正是 W08 KD-2 点名要修的口径错误。

        本方法只做**契约层**的事：把租户级扫描（`task_id=None`）委托给 `_sweep()`。
        """
        return self._sweep(tenant_id=tenant_id, older_than_s=older_than_s, task_id=None)

    def sweep_task(
        self, *, task_id: str, tenant_id: str | None = None, older_than_s: float = 60.0
    ) -> list[ReconcileResult]:
        """只核对**指定任务**的未观测调用；返回类型与 `sweep()` 相同。

        与 `sweep()` 的唯一区别是扫描范围：仓储层按 `(tenant_id, task_id)` 过滤
        （走既有索引 `idx_prepared_task (tenant_id, task_id, prepared_at)`）。
        用途：① 崩溃恢复只关心当前任务时，避免被历史残留挤出分页窗口；
              ② 运维按任务重放核对（本仓 UI/API 未暴露，仅进程内使用）。

        `sweep()` 的参数表是 T06 冻结契约（`tests/unit/test_w09_sweep_scanned.py:292-297`），
        因此本能力以**新方法**提供，不改既有方法签名与返回类型。
        `task_id` 是必填 keyword-only 参数：缺失即 `TypeError`，空串即
        `RequestInvalidError`（fail closed，不可能静默退化为全租户核对）。
        """
        if not str(task_id or "").strip():
            raise RequestInvalidError("task_id 不能为空", details={"task_id": task_id})
        return list(
            self._sweep(
                tenant_id=tenant_id, older_than_s=older_than_s, task_id=task_id
            ).conclusions
        )

    def _sweep(
        self, *, tenant_id: str | None, older_than_s: float, task_id: str | None
    ) -> SweepReport:
        """`sweep()` / `sweep_detailed()` / `sweep_task()` 的唯一实现（分页遍历）。

        内部方法，承接 T06 时 `sweep_detailed()` 的原主体，逐字保留三个口径：

        - `page_size = 500`（单页上限，避免历史残留把待核对记录挤出窗口）；
        - `scanned_total += len(batch)` 仍在 `_reconcile_one()` **过滤前**累加
          （KD-2 / AC-20 硬要求：扫到即计入）；
        - `offset += len(batch)` 且 `len(batch) < page_size` 时 break。

        `task_id` 只作为"扫描范围"透传给 `_find_page()`，不参与任何判定逻辑；
        传 `None` 即历史行为（租户级）。
        """
        page_size = 500
        tenants = [tenant_id] if tenant_id else self._tenants_with_prepared()
        results: list[ReconcileResult] = []
        scanned_total = 0
        for current in tenants:
            offset = 0
            while True:
                batch = self._find_page(
                    tenant_id=current,
                    older_than_s=older_than_s,
                    limit=page_size,
                    offset=offset,
                    task_id=task_id,
                )
                if not batch:
                    break
                # 过滤前计数：本批**确实被扫到**的行数（KD-2 / AC-20 硬要求）。
                scanned_total += len(batch)
                for prepared in batch:
                    result = self._reconcile_one(prepared, tenant_id=current)
                    if result is not None:
                        results.append(result)
                offset += len(batch)
                if len(batch) < page_size:
                    break
        return SweepReport(
            scanned_total=scanned_total,
            conclusions=tuple(results),
            scanned_tenants=tuple(tenants),
        )

    def _find_page(
        self,
        *,
        tenant_id: str,
        older_than_s: float,
        limit: int,
        offset: int,
        task_id: str | None,
    ) -> list[StepPrepared]:
        """分页适配：`task_id is None` 时**不传**该关键字参数。

        为什么必须条件分派（不是风格问题）：T06 单测替身
        `_FakeStepRecords.find_unobserved`（`tests/unit/test_w09_sweep_scanned.py:102-119`）
        的形参表不含 `task_id`，且它把每次调用的 kwargs 记进 `self.calls`，被
        `:202` / `:225` / `:242-243` 逐字断言——无条件透传会让 T06 的 10 条用例变红，
        那属于"为让代码过而改测试"，明确避免。真实仓储的 `task_id` 是可选参数
        （缺省 `None` 语义等于历史 SQL），因此这里的分派只影响替身兼容性，
        不改变生产语义。
        """
        if task_id is None:
            return self._step_records.find_unobserved(
                tenant_id=tenant_id,
                older_than_s=older_than_s,
                limit=limit,
                offset=offset,
            )
        return self._step_records.find_unobserved(
            tenant_id=tenant_id,
            task_id=task_id,
            older_than_s=older_than_s,
            limit=limit,
            offset=offset,
        )

    # ---- 单条收敛 ------------------------------------------------------
    def _reconcile_one(
        self, prepared: StepPrepared, *, tenant_id: str
    ) -> ReconcileResult | None:
        # 1) 二次确认仍无 Observed：若已被执行链补齐，则本次无需核对。
        if self._step_records.get_observed(prepared.tool_call_id) is not None:
            return None

        # 2) 读取任务；任务不存在时无法判定影响面，转人工介入。
        try:
            task = self._tasks.get_task(prepared.task_id, tenant_id=tenant_id)
        except TaskNotFoundError:
            return self._record(
                prepared,
                conclusion=CONCLUSION_MANUAL_INTERVENTION,
                task_status="UNKNOWN",
                transitioned=False,
                reason=REASON_TASK_MISSING,
            )

        # 3) 已收敛过：只记录核对事实，不重复迁移状态。
        if task.status is TaskStatus.NEEDS_RECONCILIATION:
            return self._record(
                prepared,
                conclusion=CONCLUSION_NEEDS_RECONCILIATION,
                task_status=task.status.value,
                transitioned=False,
                reason=REASON_ALREADY_CONVERGED,
            )

        # 4) 允许迁移则推进状态；否则转人工介入（绝不猜测执行结果）。
        target = self._target_status(task.status)
        if target is None:
            return self._record(
                prepared,
                conclusion=CONCLUSION_MANUAL_INTERVENTION,
                task_status=task.status.value,
                transitioned=False,
                reason=REASON_NO_LEGAL_TRANSITION,
            )

        transitioned = False
        final_status = task.status
        try:
            updated = self._tasks.update_task_status(
                task.task_id,
                target,
                expected_state_version=task.state_version,
                error_code="NEEDS_RECONCILIATION"
                if target is TaskStatus.NEEDS_RECONCILIATION
                else "MANUAL_INTERVENTION",
                error_message="检测到已 Prepared 未 Observed 的写操作，需核对真实结果",
            )
            transitioned = True
            final_status = updated.status
        except StateConflictError:
            # 其他进程已推进该任务：以最新状态为准，不覆盖、不重放。
            latest = self._tasks.get_task(task.task_id, tenant_id=tenant_id)
            final_status = latest.status
            if latest.status is TaskStatus.NEEDS_RECONCILIATION:
                return self._record(
                    prepared,
                    conclusion=CONCLUSION_NEEDS_RECONCILIATION,
                    task_status=latest.status.value,
                    transitioned=False,
                    reason=REASON_ALREADY_CONVERGED,
                )

        conclusion = (
            CONCLUSION_NEEDS_RECONCILIATION
            if final_status is TaskStatus.NEEDS_RECONCILIATION
            else CONCLUSION_MANUAL_INTERVENTION
        )
        return self._record(
            prepared,
            conclusion=conclusion,
            task_status=final_status.value,
            transitioned=transitioned,
            reason=REASON_NOT_OBSERVED,
        )

    @staticmethod
    def _target_status(current: TaskStatus) -> TaskStatus | None:
        if can_transition(current, TaskStatus.NEEDS_RECONCILIATION):
            return TaskStatus.NEEDS_RECONCILIATION
        if can_transition(current, TaskStatus.MANUAL_INTERVENTION):
            return TaskStatus.MANUAL_INTERVENTION
        return None

    def _record(
        self,
        prepared: StepPrepared,
        *,
        conclusion: str,
        task_status: str,
        transitioned: bool,
        reason: str,
    ) -> ReconcileResult:
        """把核对结论写入审计链（只追加），并返回结构化结果。"""
        result = ReconcileResult(
            prepared_id=prepared.prepared_id,
            tool_call_id=prepared.tool_call_id,
            task_id=prepared.task_id,
            conclusion=conclusion,
            task_status=task_status,
            transitioned=transitioned,
            reason=reason,
            approval_id=prepared.approval_id,
            action_digest=prepared.action_digest,
        )
        self._audit.emit_required(
            make_event(
                AuditEventType.RECONCILIATION_REQUIRED,
                task_id=prepared.task_id,
                run_id=prepared.run_id,
                tool_call_id=prepared.tool_call_id,
                payload={
                    "prepared_id": prepared.prepared_id,
                    "tool_call_id": prepared.tool_call_id,
                    "approval_id": prepared.approval_id,
                    "action_digest": prepared.action_digest,
                    "idempotency_key": prepared.idempotency_key,
                    "tool_name": prepared.tool_name,
                    "tool_version": prepared.tool_version,
                    "policy_version": prepared.policy_version,
                    "conclusion": conclusion,
                    "task_status": task_status,
                    "transitioned": transitioned,
                    "reason": reason,
                    # 明确记录"未发生重放"，供事后核对与 P0-18 断言。
                    "replay_attempted": False,
                    "tenant_id": prepared.tenant_id,
                },
            ),
            actor_user_id="system:reconciler",
        )
        return result

    def _tenants_with_prepared(self) -> list[str]:
        rows = self._step_records.client.fetch_all(
            "SELECT DISTINCT tenant_id FROM dg_system.step_prepared ORDER BY tenant_id"
        )
        return [str(row["tenant_id"]) for row in rows]


__all__ = [
    "CONCLUSION_MANUAL_INTERVENTION",
    "CONCLUSION_NEEDS_RECONCILIATION",
    "REASON_ALREADY_CONVERGED",
    "REASON_NOT_OBSERVED",
    "REASON_TASK_MISSING",
    "REASON_NO_LEGAL_TRANSITION",
    "ReconcileResult",
    "Reconciler",
    "SweepReport",
]
