"""Worker 主循环：poll → transition → acquire lease → submit → release。

硬约束（W07 收口 rev.2）：

- **候选只来自 `WAITING_APPROVAL`**，执行前必须先把任务迁到 `EXECUTING`：
  1) `ApprovalService.approve()` 不改任务状态，审批通过后任务仍停在
     `WAITING_APPROVAL`；
  2) `ToolGateway` 第 5 步只校验 `state_version`/`environment_id`，不校验 status，
     所以"不迁移也能执行成功"——但 `_converge_unknown_outcome()` 用
     `can_transition(current.status, NEEDS_RECONCILIATION)` 做门禁，而
     `WAITING_APPROVAL` 在状态机里**没有**到 `NEEDS_RECONCILIATION` 的边，
     一旦结果未知就会静默不落账、任务永久卡住。
- **顺序不可反**：transition → 重新读取 `state_version` → 建信封 → execute。
  transition 会让 `state_version + 1`；用迁移前的值建信封必被 Gateway 判
  `StateConflictError`。审批不绑定 `state_version`，所以先迁移再 consume 不会
  触发摘要不符。
- **fail closed**：参数还原失败/审批缺失/任务状态变化一律跳过，绝不猜测执行；
- **不重试**：任何异常都不重试工具调用（写操作可能已经生效，重试等于二次写入）；
  未知结果由 Gateway 立即收敛为 `NEEDS_RECONCILIATION`，本模块只如实记录；
- **不抢任务**：`WorkerConflictError`（已有活动租约）只跳过该候选，不换任务、不延时重试；
- **续租失败即停**：心跳 `lost=True` 后不再 `submit()`，只释放并能释放的租约。

W08 追加（AC-17）：`run_once()` 每轮**先**调用一次 `Reconciler.sweep()` 收敛
"已 Prepared 未 Observed"的历史调用，**再**发现新候选；成功（含空结果）写
`RECONCILIATION_SWEPT`，抛错写 `RECONCILIATION_FAILED` 并让该轮 `clean=False`，
两种情况都**不中断**主循环，也绝不因此重放任何写操作（核对器本身不持有工具注册表）。

并发：本轮 `worker_max_concurrency` 固定为 1（`core/config.py` 的 `ge=1 le=1`），
因此循环严格串行——跨任务并发会放大 R2 写风险且无法用现有证据链证伪。

W09 T06 增量（KD-2 / KD-3 / KD-4 / KD-5，`docs/w09/02-design.md` §7）：

- **KD-2**：核对优先走 `sweep_detailed()`，把 `scanned_total`（真实扫描行数）与
  `scanned`（= 产出结论条数，W08 口径**只增不改**）同时写进
  `RECONCILIATION_SWEPT`；`RunReport.reconcile_scanned_total` 跨轮累加。
  核对端口只有 `sweep()` 的历史替身仍可工作，此时 `scanned_total` 退化为
  `len(results)` 并在事件里标 `scanned_total_estimated=True`（不假装知道真实行数）。
- **KD-3**：`_emit_scheduler_event` 把 `stack.tenant_id` 作为 `tenant_id=` 传给
  `AuditService.try_emit`，使调度事件（挂在伪任务上、无对应任务行）也能按租户检索。
  该值只来自服务端组合根，**不读 payload**。
- **KD-4**：`reconcile_interval_s` 生效为跨周期节流（时钟可注入）。未到间隔的轮次
  **不发** `RECONCILIATION_SWEPT`（避免把"没扫"记成"扫了 0 条"）、不计任何计数，
  只记日志 `reason=RECONCILE_INTERVAL_NOT_DUE`。缺省 `0.0` ⇒ 历史替身不节流，
  W08 冻结用例（3 轮 3 次 sweep）逐字不变。
- **KD-5**：新增 `reconcile_failed`（`failed` 的**子集**）。`failed` 与 `clean`
  语义逐字不变（`tests/unit/test_worker_scheduler.py:402` 的 `failed == 1` 仍成立）。

W10 L2 增量（报告接 Worker 装配，`docs/w09/13-l1-l2-design.md` §4.1 / §4.5 / §4.7）：

- **触发点**：`_execute()` 内、拿到 `ToolResult` 且 `outcome = _classify(result)`
  之后（≈ `step_observed` 落账之后）。这是唯一同时持有 `task`、`tool_call_id`
  与 `outcome` 的位置；"任务终态"在 Worker 内永不发生（执行成功后任务仍是
  `EXECUTING`），因此不采用。
- **三项不生成**：`stack.report_publisher` 未装配（历史替身）、`outcome` 是
  `SKIPPED`/`DENIED`（无执行事实）、快照已存在（幂等，由发布器判定）。
- **失败不阻塞**：`_publish_report()` 全包 `try/except Exception`，只把失败记进
  `RunReport.report_failed` 并打 `alert=REPORT_FAILED` 结构化日志，**绝不 raise**、
  不改变 `_execute()` 的返回码、不回滚任务状态。
- **`clean` 与退出码不变**：`report_failed` **不并入** `failed`；`clean` 仍只含
  `denied/skipped/failed/unknown_outcome`（主理人已裁定，设计 §4.8）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from core.audit.events import AuditEventType, make_event
from core.errors import DBGuardianError, WorkerConflictError, WorkerLeaseInvalidError
from core.logging import get_logger
from core.security.canonical import canonical_digest
from core.tasks.models import Task, TaskStatus
from core.tools.contracts import ToolResult, ToolStatus
from workers.discovery import (
    CANDIDATE_TASK_STATUSES,
    Candidate,
    DiscoveryStackPort,
    discover_candidates,
)
from workers.heartbeat import HeartbeatWorkerPort, LeaseHeartbeat
from workers.report_assembly import ALERT_REPORT_FAILED

logger = get_logger(__name__)

# 单次执行的结果分类（用于报告与退出码，不参与安全判定）。
OUTCOME_EXECUTED = "executed"
OUTCOME_DENIED = "denied"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown_outcome"

# 调度层审计落点：`task_events.task_id` 为 NOT NULL，而 `sweep()` 失败是**租户级**
# 事实、没有具体任务。用固定的调度伪任务承载，不写 `task_id` 为空的脏行。
SCHEDULER_TASK_ID = "system:worker_scheduler"

# `RECONCILIATION_FAILED.payload.reason` 的稳定取值。
REASON_RECONCILIATION_SWEEP_FAILED = "RECONCILIATION_SWEEP_FAILED"

# KD-4：未到 `reconcile_interval_s` 的轮次只记日志，不写事件。
REASON_RECONCILE_INTERVAL_NOT_DUE = "RECONCILE_INTERVAL_NOT_DUE"


class WorkerPort(HeartbeatWorkerPort, Protocol):
    """执行所需的 `Worker` 最小面（见 `core/worker/worker.py`）。"""

    def acquire_lease(
        self, task_id: str, environment_id: str, ttl_s: float
    ) -> Any: ...

    def submit(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        step_id: str | None,
        state_version: int,
        tool_version: str | None = None,
        ttl_s: float = 60,
        approval_id: str | None = None,
    ) -> ToolResult: ...

    def release(self) -> Any: ...


class TaskServicePort(Protocol):
    """`core.tasks.service.TaskService` 的最小面（只用到受控状态迁移）。"""

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        **kwargs: Any,
    ) -> Task: ...


class RunnerStackPort(DiscoveryStackPort, Protocol):
    """运行器所需的 `WorkerStack` 最小面。

    `reconciler` 是 **可选** 依赖（W08 T06）：只有生产装配
    （`workers/stack.py::build_worker_stack`）会注入；不含该属性的栈
    （历史单测替身）保持原行为——本轮 `reconciled` 恒为 0，不做任何核对。
    """

    settings: Any
    task_service: TaskServicePort

    def worker_for(self, actor_user_id: str) -> WorkerPort: ...


@dataclass(frozen=True)
class RunReport:
    """一轮（或累计）执行结果；不含任何参数原文。"""

    cycles: int = 0
    discovered: int = 0
    executed: int = 0
    denied: int = 0
    skipped: int = 0
    failed: int = 0
    unknown_outcome: int = 0
    #: W08：本轮由核对（`Reconciler.sweep()`）**推进收敛**的调用数。
    #: 核对成功不代表"有副作用发生"，因此不计入 `clean` 的不干净条件。
    reconciled: int = 0
    #: W09 T06 / KD-5：`failed` 的**子集**——其中由核对失败贡献的部分。
    #: 单独计数只是为了可观测（`failed` 语义与 `clean` 判定逐字不变）。
    reconcile_failed: int = 0
    #: W09 T06 / KD-2：本轮**真实扫描行数**（跨轮累加），与 `reconciled` 不同轴。
    reconcile_scanned_total: int = 0
    #: W10 L2：本轮成功落库并发出 `REPORT_GENERATED` 的报告数。
    reports_generated: int = 0
    #: W10 L2：本轮报告生成失败数（**不进入 `failed`、不影响 `clean`**，设计 §4.5）。
    report_failed: int = 0

    @property
    def clean(self) -> bool:
        """是否"全部收敛"：没有任何跳过、失败、拒绝或未知结果。

        判据**逐字不变**（W10 L2 不把 `reports_generated` / `report_failed` 纳入）：
        报告的立法定义是"执行事实之后的下游产物"，把它塞进 `clean` 会让 `--once`
        的运维语义从"任务都执行完了"漂移成"报告也都生成了"。
        """
        return (
            self.denied == 0
            and self.skipped == 0
            and self.failed == 0
            and self.unknown_outcome == 0
        )

    def merged(self, other: "RunReport") -> "RunReport":
        return RunReport(
            cycles=self.cycles + other.cycles,
            discovered=self.discovered + other.discovered,
            executed=self.executed + other.executed,
            denied=self.denied + other.denied,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
            unknown_outcome=self.unknown_outcome + other.unknown_outcome,
            reconciled=self.reconciled + other.reconciled,
            reconcile_failed=self.reconcile_failed + other.reconcile_failed,
            reconcile_scanned_total=(
                self.reconcile_scanned_total + other.reconcile_scanned_total
            ),
            reports_generated=self.reports_generated + other.reports_generated,
            report_failed=self.report_failed + other.report_failed,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "cycles": self.cycles,
            "discovered": self.discovered,
            "executed": self.executed,
            "denied": self.denied,
            "skipped": self.skipped,
            "failed": self.failed,
            "unknown_outcome": self.unknown_outcome,
            "reconciled": self.reconciled,
            "reconcile_failed": self.reconcile_failed,
            "reconcile_scanned_total": self.reconcile_scanned_total,
            "reports_generated": self.reports_generated,
            "report_failed": self.report_failed,
            "clean": self.clean,
        }


@dataclass(frozen=True)
class _SweepOutcome:
    """一次核对的内部结果（私有；对外只暴露 `RunReport` 的字段）。

    未注入核对端口、以及被 `reconcile_interval_s` 节流跳过，都产出**全零**结果：
    两种情况的事实相同——本轮没有扫、没有失败、没有收敛，因此不设第二个标志位。
    """

    reconciled: int = 0
    failed: bool = False
    scanned_total: int = 0


def _classify(result: ToolResult) -> str:
    if result.status is ToolStatus.SUCCESS:
        return OUTCOME_EXECUTED
    if result.status is ToolStatus.DENIED:
        return OUTCOME_DENIED
    if result.status is ToolStatus.UNKNOWN_OUTCOME:
        return OUTCOME_UNKNOWN
    return OUTCOME_FAILED


class WorkerRunner:
    """串行执行候选的 Worker 主循环。"""

    def __init__(
        self,
        stack: RunnerStackPort,
        *,
        poll_interval_s: float | None = None,
        shutdown_grace_s: float | None = None,
        discover: Callable[..., list[Candidate]] = discover_candidates,
        heartbeat_factory: Callable[..., LeaseHeartbeat] = LeaseHeartbeat,
        limit: int = 10,
        max_cycles: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings = stack.settings
        if int(getattr(settings, "worker_max_concurrency", 1)) != 1:
            raise ValueError("本轮 Worker 只支持单并发（worker_max_concurrency=1）")
        self._stack = stack
        self._settings = settings
        self._poll_interval_s = float(
            settings.worker_poll_interval_s if poll_interval_s is None else poll_interval_s
        )
        self._shutdown_grace_s = float(
            settings.worker_shutdown_grace_s
            if shutdown_grace_s is None
            else shutdown_grace_s
        )
        self._discover = discover
        self._heartbeat_factory = heartbeat_factory
        # W08：核对端口是可选接缝（生产栈注入，历史替身没有该属性）。
        self._reconciler = getattr(stack, "reconciler", None)
        self._consecutive_sweep_failures = 0
        # W10 L2：报告发布端口同样是可选接缝（`None` ⇒ 零行为变化）。
        self._reporter = getattr(stack, "report_publisher", None)
        # P6-T1：只读诊断派发消费者（可选接缝，`None` ⇒ 零行为变化）。
        self._readonly_dispatcher = getattr(stack, "readonly_dispatcher", None)
        self._reports_generated = 0
        self._report_failed = 0
        # KD-4：节流时钟可注入（测试不需要 sleep）；`None` 表示"本次运行还没扫过"。
        self._monotonic = monotonic
        self._last_sweep_at: float | None = None
        self._limit = max(1, int(limit))
        self._max_cycles = max_cycles
        self._stop = threading.Event()
        self._stop_signal: int | None = None
        self._last_report = RunReport()

    # ---- control -------------------------------------------------------
    def request_stop(self, signum: int | None = None) -> None:
        """请求停止（信号处理或测试调用）；幂等。"""
        if signum is not None:
            self._stop_signal = int(signum)
        self._stop.set()
        logger.info(
            "Worker 收到停止请求，将在当前任务收敛后退出",
            extra={"worker_id": getattr(self._stack, "worker_id", None), "signal": signum},
        )

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def last_report(self) -> RunReport:
        return self._last_report

    @property
    def shutdown_grace_s(self) -> float:
        return self._shutdown_grace_s

    def outstanding_candidates(self) -> int:
        """当前仍可被发现的候选数（用于退出码 1 的判定）。"""
        try:
            return len(self._discover(self._stack, limit=self._limit))
        except DBGuardianError as exc:
            logger.warning(
                "退出前探测在途候选失败，按未收敛处理",
                extra={"code": exc.code.value, "reason": "OUTSTANDING_PROBE_FAILED"},
            )
            return 1

    # ---- loops ---------------------------------------------------------
    def run_once(self) -> RunReport:
        """核对一轮历史未观测调用，再发现并串行处理一轮候选。

        顺序固定为 **先核对、后发现**（AC-17）：`sweep()` 收敛的是"上一轮已
        `Prepared` 但无 `Observed`"的历史调用，与"本轮新候选"没有交集；先做核对
        可以保证同一周期内先释放被卡住的任务，再领取新工作，且失败不影响本次发现。

        `sweep()` **恰好调用一次**；抛错时本周期仍继续处理候选（不中断主循环），
        并写 `RECONCILIATION_FAILED`。核对失败是"存在未收敛事实"，因此该轮
        `clean=False`——`RunReport` 既有字段的语义**逐字不变**（只新增字段，
        不改 `failed` / `clean` 的判定），这里把失败计入 `failed` 并保留结构化
        审计事件作为权威证据。

        W09 T06 追加：失败**同时**计入 `reconcile_failed`（KD-5，`failed` 的子集）；
        真实扫描行数计入 `reconcile_scanned_total`（KD-2）；被间隔节流跳过的轮次
        三项都不计（KD-4：没扫就不能记成"扫了 0 条"）。
        """
        outcome = self._sweep_once()
        # P6-T1：先消费 API 经控制库派发的只读诊断请求（失败不中断 R2 调度）。
        if self._readonly_dispatcher is not None:
            try:
                self._readonly_dispatcher.drain_once(limit=self._limit)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "只读派发队列消费失败（跳过本轮，不影响 R2 主循环）",
                    extra={"error": str(exc)},
                )
        # W10 L2：两个报告计数按**轮**重置（与 `cycles=1` 的语义一致）。
        self._reports_generated = 0
        self._report_failed = 0
        candidates = self._discover(self._stack, limit=self._limit)
        counters = {
            OUTCOME_EXECUTED: 0,
            OUTCOME_DENIED: 0,
            OUTCOME_SKIPPED: 0,
            OUTCOME_FAILED: 0,
            OUTCOME_UNKNOWN: 0,
        }
        for candidate in candidates:
            if self._stop.is_set():
                # 停止请求后不再领取新的租约；剩余候选计入 skipped 由退出码体现。
                counters[OUTCOME_SKIPPED] += 1
                continue
            outcome_code = self._execute(candidate)
            counters[outcome_code] = counters.get(outcome_code, 0) + 1
        reconcile_failed = 1 if outcome.failed else 0
        report = RunReport(
            cycles=1,
            discovered=len(candidates),
            executed=counters[OUTCOME_EXECUTED],
            denied=counters[OUTCOME_DENIED],
            skipped=counters[OUTCOME_SKIPPED],
            # `failed` 语义逐字不变：核对失败仍占 1 个 failed（保护既有断言）。
            failed=counters[OUTCOME_FAILED] + reconcile_failed,
            unknown_outcome=counters[OUTCOME_UNKNOWN],
            reconciled=outcome.reconciled,
            reconcile_failed=reconcile_failed,
            reconcile_scanned_total=outcome.scanned_total,
            reports_generated=self._reports_generated,
            report_failed=self._report_failed,
        )
        self._last_report = report
        return report

    def run_forever(self) -> RunReport:
        """循环执行直到 `request_stop()`；返回累计报告。"""
        total = RunReport()
        while not self._stop.is_set():
            cycle = self.run_once()
            total = total.merged(cycle)
            if self._max_cycles is not None and total.cycles >= self._max_cycles:
                break
            if self._stop.is_set():
                break
            if cycle.discovered == 0:
                # 只有"空转"才等待；已就绪的候选立即继续处理。
                self._stop.wait(self._poll_interval_s)
        self._last_report = total
        return total

    # ---- reconciliation sweep ------------------------------------------
    def _sweep_once(self) -> _SweepOutcome:
        """调用一次核对；返回 `_SweepOutcome`（收敛数 / 是否失败 / 真实扫描行数）。

        - 未注入核对端口（历史替身/`--dry-run` 之外的旧栈）→ 全零，不伪造核对结果；
        - **KD-4 节流**：`reconcile_interval_s > 0` 且距上次核对不足间隔 → 直接返回
          全零：**不发** `RECONCILIATION_SWEPT`、不计任何计数，只记
          `reason=RECONCILE_INTERVAL_NOT_DUE` 的结构化日志。缺省 `0.0`（替身/历史栈
          没有该配置项）⇒ 不节流，W08 冻结用例「3 轮 3 次 sweep」逐字不变；
        - 成功（含结果为空）→ 写 `RECONCILIATION_SWEPT`（设计 §5 第 8 行），
          payload 全部取自实际返回值与本轮实测耗时；
        - 抛错 → 计数连续失败、写 `RECONCILIATION_FAILED`、返回 `failed=True`，
          **绝不向上抛**（否则一轮数据库抖动就会打挂整个 Worker 循环）。
        """
        if self._reconciler is None:
            return _SweepOutcome()

        interval = float(getattr(self._settings, "reconcile_interval_s", 0.0) or 0.0)
        now = float(self._monotonic())
        if interval > 0.0 and self._last_sweep_at is not None:
            elapsed = now - self._last_sweep_at
            if elapsed < interval:
                logger.info(
                    "未到核对间隔，本轮跳过核对（不发事件、不计计数）",
                    extra={
                        "reason": REASON_RECONCILE_INTERVAL_NOT_DUE,
                        "elapsed_s": round(max(elapsed, 0.0), 6),
                        "interval_s": interval,
                    },
                )
                return _SweepOutcome()
        self._last_sweep_at = now

        older_than_s = float(getattr(self._settings, "reconcile_older_than_s", 60.0))
        started_at = time.perf_counter()
        try:
            rows, scanned_total, estimated = self._invoke_sweep(older_than_s)
        except Exception as exc:  # noqa: BLE001 - 核对失败不得中断主循环
            self._consecutive_sweep_failures += 1
            self._emit_reconciliation_failed(exc)
            return _SweepOutcome(failed=True)
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        self._consecutive_sweep_failures = 0
        # 只统计**真的推进了状态**的结论：已收敛/转人工但未迁移的不重复计数。
        reconciled = sum(1 for item in rows if getattr(item, "transitioned", False))
        self._emit_reconciliation_swept(
            rows,
            reconciled=reconciled,
            duration_ms=duration_ms,
            scanned_total=scanned_total,
            scanned_total_estimated=estimated,
        )
        return _SweepOutcome(
            reconciled=reconciled, failed=False, scanned_total=scanned_total
        )

    def _invoke_sweep(self, older_than_s: float) -> tuple[list[Any], int, bool]:
        """优先用 `sweep_detailed()`；只有 `sweep()` 的替身退回 `len(results)`。

        Returns:
            `(结论列表, 真实扫描行数, 扫描行数是否为估计值)`。
        """
        reconciler = self._reconciler
        tenant_id = getattr(self._stack, "tenant_id", None)
        detailed = getattr(reconciler, "sweep_detailed", None)
        if callable(detailed):
            report = detailed(tenant_id=tenant_id, older_than_s=older_than_s)
            rows = list(getattr(report, "conclusions", None) or ())
            scanned_total = int(getattr(report, "scanned_total", len(rows)))
            return rows, scanned_total, False
        rows = list(reconciler.sweep(  # type: ignore[union-attr]
            tenant_id=tenant_id, older_than_s=older_than_s
        ) or ())
        # 老替身不提供扫描行数：如实退回"产出结论条数"，并标记为估计值。
        return rows, len(rows), True

    def _emit_reconciliation_swept(
        self,
        results: list[Any],
        *,
        reconciled: int,
        duration_ms: float,
        scanned_total: int,
        scanned_total_estimated: bool,
    ) -> None:
        """调度层审计：`RECONCILIATION_SWEPT`（结果为空也发，表示"本轮确实扫过"）。

        两个口径**必须分开**（W09 T06 / AC-20 / KD-2）：

        - `scanned`：产出结论条数（= `len(conclusions)`）。W08 口径，**只增不改**，
          既有断言 `payload["scanned"] == 2` 仍成立；
        - `scanned_total`：真实扫描行数（过滤前计数）。核对端口没有
          `sweep_detailed()` 时只能退回结论条数，此时
          `scanned_total_estimated=True`，不把估计值冒充真实值。
        """
        payload = {
            "scanned": len(results),
            "scanned_total": scanned_total,
            "scanned_total_estimated": scanned_total_estimated,
            "converged": reconciled,
            "conclusions": [
                {
                    "task_id": getattr(item, "task_id", None),
                    "tool_call_id": getattr(item, "tool_call_id", None),
                    "conclusion": getattr(item, "conclusion", None),
                }
                for item in results
            ],
            "duration_ms": round(max(duration_ms, 0.0), 3),
            "tenant_id": getattr(self._stack, "tenant_id", None),
            "worker_id": getattr(self._stack, "worker_id", None),
            # 核对不产生工具调用：与 RECONCILIATION_FAILED 同口径的可断言事实。
            "tool_calls_delta": 0,
        }
        logger.info(
            "未观测调用核对完成",
            extra={
                "scanned": payload["scanned"],
                "scanned_total": scanned_total,
                "converged": reconciled,
                "duration_ms": payload["duration_ms"],
            },
        )
        self._emit_scheduler_event(AuditEventType.RECONCILIATION_SWEPT, payload)

    def _emit_reconciliation_failed(self, error: BaseException) -> None:
        """调度层审计：`RECONCILIATION_FAILED`（payload 只含可检索事实，无原文）。"""
        code = getattr(getattr(error, "code", None), "value", None)
        payload = {
            "code": code,
            "error_code": code,
            "reason": REASON_RECONCILIATION_SWEEP_FAILED,
            "error_type": type(error).__name__,
            "message_digest": canonical_digest(
                {"type": type(error).__name__, "message": str(error)}
            ),
            "consecutive_failures": self._consecutive_sweep_failures,
            "tenant_id": getattr(self._stack, "tenant_id", None),
            "worker_id": getattr(self._stack, "worker_id", None),
            "replay_attempted": False,
        }
        logger.error(
            "未观测调用核对失败，本轮继续处理候选（不中断主循环）",
            extra={
                "alert": AuditEventType.RECONCILIATION_FAILED.value,
                "reason": REASON_RECONCILIATION_SWEEP_FAILED,
                "error_type": type(error).__name__,
                "code": code,
                "consecutive_failures": self._consecutive_sweep_failures,
            },
        )
        self._emit_scheduler_event(AuditEventType.RECONCILIATION_FAILED, payload)

    def _emit_scheduler_event(
        self, event_type: AuditEventType, payload: dict[str, Any]
    ) -> None:
        """写一条调度层事件；无审计端口或写入失败时只降级为结构化日志。

        降级（`try_emit`）而非 `emit_required`：调度事件不是 R2 执行前置条件，
        它自己写失败不能反过来打断主循环。

        KD-3：显式传 `tenant_id=stack.tenant_id`（服务端组合根事实）。调度事件挂在
        伪任务 `system:worker_scheduler` 上、没有对应任务行，`_resolve_tenant()` 只能
        得到 NULL，导致按租户检索不到；该入参**不是** payload 兜底——绝不读
        `payload["tenant_id"]`。历史 NULL 行不 UPDATE。
        """
        audit = getattr(self._stack, "audit", None)
        if audit is None:
            return
        event = make_event(
            event_type,
            task_id=SCHEDULER_TASK_ID,
            run_id=f"scheduler:{getattr(self._stack, 'worker_id', 'worker')}",
            payload=payload,
        )
        try:
            audit.try_emit(
                event,
                actor_user_id="system:worker_scheduler",
                tenant_id=getattr(self._stack, "tenant_id", None),
            )
        except Exception:  # noqa: BLE001 - 审计降级不得掩盖"循环继续"这一目标
            logger.error(
                "调度层审计写入失败（已退化为结构化日志）",
                extra={"alert": event_type.value},
            )

    # ---- single candidate ----------------------------------------------
    def _execute(self, candidate: Candidate) -> str:
        stack = self._stack
        settings = self._settings
        try:
            task = stack.repository.get_task(
                candidate.task_id, tenant_id=stack.tenant_id
            )
        except DBGuardianError as exc:
            logger.warning(
                "候选任务在领取前已不可见，跳过",
                extra={
                    "task_id": candidate.task_id,
                    "approval_id": candidate.approval_id,
                    "code": exc.code.value,
                },
            )
            return OUTCOME_SKIPPED
        if task.status not in CANDIDATE_TASK_STATUSES:
            logger.info(
                "候选任务状态已变化，跳过",
                extra={
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "approval_id": candidate.approval_id,
                },
            )
            return OUTCOME_SKIPPED
        if task.cancel_requested or task.pause_requested:
            # PAUSE/CANCEL 只置标记不改状态，必须在这里显式拦住。
            logger.info(
                "候选任务已被请求暂停/取消，跳过",
                extra={
                    "task_id": task.task_id,
                    "approval_id": candidate.approval_id,
                    "pause_requested": task.pause_requested,
                    "cancel_requested": task.cancel_requested,
                },
            )
            return OUTCOME_SKIPPED

        # ① 先迁移到 EXECUTING（WAITING_APPROVAL → EXECUTING），
        # ② 迁移会 +1 state_version，必须重新读取后再建信封。
        task = self._ensure_executing(task)
        if task is None:
            return OUTCOME_SKIPPED

        actor_user_id = candidate.requested_by or str(settings.worker_actor_user_id)
        worker = stack.worker_for(actor_user_id)
        try:
            worker.acquire_lease(
                task.task_id,
                task.environment_id,
                ttl_s=float(settings.worker_lease_ttl_s),
            )
        except WorkerConflictError as exc:
            # 已有活动租约：跳过该候选，不延迟重试、不改换任务。
            logger.info(
                "候选被其他 Worker 持有租约，跳过",
                extra={
                    "task_id": task.task_id,
                    "approval_id": candidate.approval_id,
                    "reason": "LEASE_CONFLICT",
                    "owner": exc.details.get("worker_id"),
                },
            )
            return OUTCOME_SKIPPED

        heartbeat = self._heartbeat_factory(
            worker,
            interval_s=float(settings.worker_heartbeat_interval_s),
            ttl_s=float(settings.worker_lease_ttl_s),
        )
        heartbeat.start()
        try:
            if heartbeat.lost:
                return OUTCOME_SKIPPED
            result = worker.submit(
                candidate.tool_name,
                candidate.params,
                step_id=candidate.step_id,
                state_version=task.state_version,
                ttl_s=float(settings.gateway_envelope_ttl_s),
                approval_id=candidate.approval_id,
            )
        except WorkerConflictError:
            return OUTCOME_SKIPPED
        except WorkerLeaseInvalidError as exc:
            logger.warning(
                "租约在执行前失效，跳过（不重试）",
                extra={
                    "task_id": task.task_id,
                    "approval_id": candidate.approval_id,
                    "code": exc.code.value,
                },
            )
            return OUTCOME_SKIPPED
        except DBGuardianError as exc:
            # 不重试：写操作可能已经生效，重试会产生第二次写入。
            logger.error(
                "Worker 提交失败，按失败收敛（不重试）",
                extra={
                    "task_id": task.task_id,
                    "approval_id": candidate.approval_id,
                    "code": exc.code.value,
                    "reason": "SUBMIT_FAILED_NO_RETRY",
                },
            )
            return OUTCOME_FAILED
        finally:
            heartbeat.stop(timeout_s=self._shutdown_grace_s)
            self._release_quietly(worker, task.task_id)

        outcome = _classify(result)
        logger.info(
            "Worker 执行完成",
            extra={
                "task_id": task.task_id,
                "approval_id": candidate.approval_id,
                "tool_name": candidate.tool_name,
                "step_id": candidate.step_id,
                "status": result.status.value,
                "outcome": outcome,
                "worker_id": getattr(worker, "worker_id", None),
                "actor_user_id": actor_user_id,
            },
        )
        # W10 L2：执行事实落定后尝试物化报告（不改变返回码，失败只计数）。
        if outcome in (OUTCOME_EXECUTED, OUTCOME_FAILED, OUTCOME_UNKNOWN):
            self._publish_report(
                task,
                tool_call_id=getattr(result, "tool_call_id", None),
                succeeded=result.status is ToolStatus.SUCCESS,
            )
        return outcome

    def _publish_report(
        self, task: Task, *, tool_call_id: str | None, succeeded: bool
    ) -> None:
        """尝试物化一份报告；**绝不抛异常、绝不改变执行结论**。

        三层隔离（设计 §4.5）：

        1. 未装配发布器（`stack.report_publisher is None`，历史替身/单测）→ 立即返回，
           零调用、零计数、零日志（零行为变化）；
        2. 发布器自己已把内部异常降级为 `ReportOutcome(failed=True)`：这里只计数 +
           打 `alert=REPORT_FAILED` 结构化日志，**不重复抛**；
        3. 发布器意外抛错（实现缺陷）→ 同样只计数 + 记日志，**不让下游缺陷打断
           主循环**（`noqa: BLE001` 的理由同 `_sweep_once()` 的核对兜底）。

        注意：`report_failed` **不并入** `failed`，`clean` 判定逐字不变（设计 §4.8）。
        """
        reporter = self._reporter
        if reporter is None:
            return
        try:
            outcome = reporter.publish(
                task, tool_call_id=tool_call_id, succeeded=succeeded
            )
        except Exception as exc:  # noqa: BLE001 - 下游失败不得中断主循环
            self._report_failed += 1
            logger.error(
                "报告发布抛错，本轮继续（不回滚、不阻塞、不冒泡）",
                extra={
                    "alert": ALERT_REPORT_FAILED,
                    "reason_code": "REPORT_PUBLISH_RAISED",
                    "task_id": getattr(task, "task_id", None),
                    "tool_call_id": tool_call_id,
                    "code": getattr(getattr(exc, "code", None), "value", None),
                    "error_type": type(exc).__name__,
                    "message_digest": canonical_digest(
                        {"type": type(exc).__name__, "message": str(exc)}
                    ),
                },
            )
            return
        if getattr(outcome, "generated", False):
            self._reports_generated += 1
            logger.info(
                "报告已生成",
                extra={
                    "task_id": getattr(task, "task_id", None),
                    "report_id": getattr(outcome, "report_id", None),
                    "tool_call_id": tool_call_id,
                },
            )
            return
        if getattr(outcome, "failed", False):
            self._report_failed += 1
            logger.error(
                "报告生成失败，本轮继续（不回滚、不阻塞、不冒泡）",
                extra={
                    "alert": ALERT_REPORT_FAILED,
                    "reason_code": "REPORT_PUBLISH_FAILED",
                    "task_id": getattr(task, "task_id", None),
                    "tool_call_id": tool_call_id,
                    "error_type": "ReportOutcome",
                    "message_digest": canonical_digest(
                        {
                            "generated": bool(getattr(outcome, "generated", False)),
                            "failed": True,
                            "task_id": getattr(task, "task_id", None),
                        }
                    ),
                },
            )

    def _ensure_executing(self, task: Task) -> Task | None:
        """把候选任务迁到 `EXECUTING` 并返回**重新读取**后的任务。

        必要性：审批通过不改任务状态，任务停在 `WAITING_APPROVAL`；而
        `WAITING_APPROVAL` 在状态机里没有到 `NEEDS_RECONCILIATION` 的边，
        直接执行会让"结果未知"静默不落账。

        幂等：`TaskService.transition()` 在 `previous == target` 时直接返回
        （不涨版本、不写事件），因此重复调用安全。

        Returns:
            迁移并重新读取后的任务；迁移失败/状态不允许则返回 `None`（调用方跳过）。
        """
        if task.status is TaskStatus.EXECUTING:
            return task
        if task.status not in CANDIDATE_TASK_STATUSES:
            return None
        try:
            self._stack.task_service.transition(task.task_id, TaskStatus.EXECUTING)
        except DBGuardianError as exc:
            logger.warning(
                "候选任务迁移到 EXECUTING 失败，跳过（不重试）",
                extra={
                    "task_id": task.task_id,
                    "from_status": task.status.value,
                    "code": exc.code.value,
                },
            )
            return None
        try:
            # 必须重新读取：state_version 已 +1，信封只能用迁移后的值。
            refreshed = self._stack.repository.get_task(
                task.task_id, tenant_id=self._stack.tenant_id
            )
        except DBGuardianError as exc:
            logger.warning(
                "任务迁移后重新读取失败，跳过",
                extra={"task_id": task.task_id, "code": exc.code.value},
            )
            return None
        if refreshed.status is not TaskStatus.EXECUTING:
            logger.warning(
                "任务迁移后状态不是 EXECUTING，跳过",
                extra={"task_id": task.task_id, "status": refreshed.status.value},
            )
            return None
        return refreshed

    def _release_quietly(self, worker: WorkerPort, task_id: str) -> None:
        try:
            worker.release()
        except DBGuardianError as exc:
            logger.warning(
                "租约释放失败（已过期或被接管），按过期处理",
                extra={"task_id": task_id, "code": exc.code.value},
            )


__all__ = [
    "OUTCOME_DENIED",
    "OUTCOME_EXECUTED",
    "OUTCOME_FAILED",
    "OUTCOME_SKIPPED",
    "OUTCOME_UNKNOWN",
    "REASON_RECONCILE_INTERVAL_NOT_DUE",
    "REASON_RECONCILIATION_SWEEP_FAILED",
    "SCHEDULER_TASK_ID",
    "RunReport",
    "RunnerStackPort",
    "WorkerPort",
    "WorkerRunner",
]
