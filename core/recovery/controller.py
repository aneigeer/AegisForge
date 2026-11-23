"""W08 恢复控制器（T05）：循环边界、无进展、反证、一次修复性重试、UNKNOWN 收敛。

结构约束（可逐条 `inspect.signature` / `vars()` 核对）：

1. **不持有 `registry` / `gateway` / DSN**——构造参数只有 `audit` 与 `tasks`，
   实例属性里没有任何可发起工具调用的对象，因此「重放写」在结构上不可达；
2. 循环与无进展**复用**既有事件 `AGENT_LOOP_DETECTED` / `AGENT_NO_PROGRESS`，
   不新建同类事件（设计 §5 冻结表注）；
3. 每条返回都带 `explanation`（恒非空）：停止必须可解释；
4. `Hypothesis` 反证只改状态、追加版本，**不物理删除**历史；
5. 非法模型输出**只重试一次**（`max_repair_retries` 上限硬编码为 1，见
   `_validate_thresholds`），第 2 次直接停，`retry_count` 恒为 1；
6. `reconcile_unknown()` 只做收敛判定 + 状态推进，**不调用任何工具**，
   `tool_calls_delta` 恒为 0。

任务上下文（`task_id` / `run_id`）用于审计落点：审计行必须挂在具体任务链上，
因此由构造参数或 `bind()` 显式提供；未绑定时仍返回判定结果，但不写审计
（`skipped_events` 计数可观测），绝不写 `task_id` 为空的脏事件。
"""

from __future__ import annotations

from typing import Any, Sequence

from core.audit.events import AuditEventType, make_event
from core.errors import RequestInvalidError
from core.recovery.models import (
    Hypothesis,
    RecoveryAction,
    RecoveryOutcome,
    normalize_targets,
)
from core.tasks.models import TaskStatus, new_id, utc_now
from core.tasks.state_machine import can_transition

# `reason` 的稳定取值（测试与审计按值断言，不得改写）。
LOOP_DETECTED = "LOOP_DETECTED"
NO_PROGRESS = "NO_PROGRESS"
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
UNKNOWN_OUTCOME_EVIDENCE_SUFFICIENT = "UNKNOWN_OUTCOME_EVIDENCE_SUFFICIENT"
UNKNOWN_OUTCOME_EVIDENCE_INSUFFICIENT = "UNKNOWN_OUTCOME_EVIDENCE_INSUFFICIENT"
NO_LEGAL_TRANSITION = "NO_LEGAL_TRANSITION"

# `AGENT_RECOVERY_STOPPED.outcome` 的取值沿用设计 §5 第 11 行的冻结集合。
OUTCOME_STOP_INVALID_OUTPUT = "STOP_INVALID_OUTPUT"
OUTCOME_ESCALATE = "ESCALATE"

# `AGENT_RECOVERY_STARTED.trigger` 的取值（设计 §5 第 10 行）。
TRIGGER_LOOP_DETECTED = "LOOP_DETECTED"
TRIGGER_NO_PROGRESS = "NO_PROGRESS"
TRIGGER_UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"

# 非法输出修复性重试的硬上限：AC-14 冻结「只重试一次」。
MAX_REPAIR_RETRIES_LIMIT = 1


class RecoveryController:
    """恢复控制器：只判定与收敛，不执行、不重放、不补偿。"""

    def __init__(
        self,
        *,
        audit: Any,
        tasks: Any,
        max_duplicate_calls: int = 2,
        no_progress_threshold: int = 2,
        max_repair_retries: int = 1,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """`task_id` / `run_id` 只决定审计落点（可选，可后置 `bind()`）。

        阈值校验（越界即拒，不做隐式夹取）：
        - `max_duplicate_calls >= 2`：第 1 次调用是正常路径，1 会把正常调用判成循环；
        - `no_progress_threshold >= 2`：无进展需要「连续相同」才有意义；
        - `0 <= max_repair_retries <= 1`：AC-14 冻结「非法输出只重试一次」。
        """
        self._max_duplicate_calls = _threshold(
            max_duplicate_calls, "max_duplicate_calls", minimum=2
        )
        self._no_progress_threshold = _threshold(
            no_progress_threshold, "no_progress_threshold", minimum=2
        )
        if not isinstance(max_repair_retries, int) or isinstance(max_repair_retries, bool):
            raise RequestInvalidError(
                "max_repair_retries 必须是整数", details={"max_repair_retries": max_repair_retries}
            )
        if not 0 <= max_repair_retries <= MAX_REPAIR_RETRIES_LIMIT:
            raise RequestInvalidError(
                "非法输出最多只允许一次修复性重试",
                details={
                    "max_repair_retries": max_repair_retries,
                    "limit": MAX_REPAIR_RETRIES_LIMIT,
                },
            )
        self._audit = audit
        self._tasks = tasks
        self._max_repair_retries = max_repair_retries
        self._task_id = task_id
        self._run_id = run_id

        self._call_counts: dict[str, int] = {}
        self._last_digest: str | None = None
        self._digest_streak = 0
        self._retry_count = 0
        self._plan_version = 0
        self._skipped_events = 0
        self._hypotheses: dict[str, Hypothesis] = {}
        self._history: dict[str, list[Hypothesis]] = {}
        # W08 T06：接管事件（`AGENT_RECOVERY_STARTED`）的**幂等闸门**。
        # 一次接管 = 从首次判停/升级到"再次观测到进展"为止；同一次只发一条。
        self._takeover_open = False
        self._evidence_count = 0

    # ---- 只读视图 ------------------------------------------------------
    @property
    def task_id(self) -> str | None:
        return self._task_id

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def plan_version(self) -> int:
        return self._plan_version

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def skipped_events(self) -> int:
        return self._skipped_events

    @property
    def max_duplicate_calls(self) -> int:
        return self._max_duplicate_calls

    @property
    def no_progress_threshold(self) -> int:
        return self._no_progress_threshold

    @property
    def max_repair_retries(self) -> int:
        return self._max_repair_retries

    def bind(self, *, task_id: str, run_id: str) -> None:
        """后置绑定审计落点（`workers/stack.py` 装配用）。"""
        self._task_id = _require_text(task_id, "task_id")
        self._run_id = _require_text(run_id, "run_id")

    # ---- AC-13 循环 ----------------------------------------------------
    def observe_call(self, call_key: str) -> RecoveryAction:
        """同一 `call_key` 达到阈值即判循环：第 1 次 `CONTINUE`，第 N 次 `STOP_LOOP`。"""
        key = _require_text(call_key, "call_key")
        count = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = count
        self._evidence_count += 1

        if count >= self._max_duplicate_calls:
            explanation = (
                f"同一调用 call_key={key} 已出现 {count} 次，达到阈值 "
                f"{self._max_duplicate_calls}，判定为循环，停止继续调用模型与工具。"
            )
            self._begin_recovery(TRIGGER_LOOP_DETECTED, call_key=key)
            self._emit(
                AuditEventType.AGENT_LOOP_DETECTED,
                {
                    "call_key": key,
                    "count": count,
                    "threshold": self._max_duplicate_calls,
                    "repeated_call_keys": [key] * count,
                    "explanation": explanation,
                    "plan_version": self._plan_version,
                },
            )
            return self._action(
                action="STOP_LOOP",
                reason=LOOP_DETECTED,
                explanation=explanation,
                call_key=key,
                repeats=count,
            )

        # 观测到新的调用（未达阈值）= 有进展：本次接管结束，下次判停重新发 STARTED。
        self._takeover_open = False
        return self._action(
            action="CONTINUE",
            reason="WITHIN_CALL_THRESHOLD",
            explanation=(
                f"call_key={key} 第 {count} 次出现，未达阈值 "
                f"{self._max_duplicate_calls}，允许继续。"
            ),
            call_key=key,
            repeats=count,
        )

    # ---- AC-13 无进展 --------------------------------------------------
    def observe_evidence(self, evidence_digest: str) -> RecoveryAction:
        """连续相同 `evidence_digest` 达到阈值即判无进展。"""
        digest = _require_text(evidence_digest, "evidence_digest")
        self._evidence_count += 1
        if digest == self._last_digest:
            self._digest_streak += 1
        else:
            self._last_digest = digest
            self._digest_streak = 1

        if self._digest_streak >= self._no_progress_threshold:
            explanation = (
                f"证据摘要连续 {self._digest_streak} 次相同（digest={digest}，阈值 "
                f"{self._no_progress_threshold}），判定为无进展，停止重试。"
            )
            self._begin_recovery(TRIGGER_NO_PROGRESS)
            self._emit(
                AuditEventType.AGENT_NO_PROGRESS,
                {
                    "evidence_digest": digest,
                    "repeats": self._digest_streak,
                    "threshold": self._no_progress_threshold,
                    "explanation": explanation,
                    "plan_version": self._plan_version,
                },
            )
            return self._action(
                action="STOP_NO_PROGRESS",
                reason=NO_PROGRESS,
                explanation=explanation,
                evidence_digest=digest,
                repeats=self._digest_streak,
            )

        # 证据发生变化 = 有进展：本次接管结束。
        self._takeover_open = False
        return self._action(
            action="CONTINUE",
            reason="EVIDENCE_CHANGED",
            explanation=(
                f"证据摘要 {digest} 第 {self._digest_streak} 次出现，未达无进展阈值 "
                f"{self._no_progress_threshold}。"
            ),
            evidence_digest=digest,
            repeats=self._digest_streak,
        )

    # ---- AC-14 非法输出 ------------------------------------------------
    def handle_invalid_output(self, attempt: int) -> RecoveryAction:
        """第 1 次非法输出 → 唯一一次 `RETRY`；再次非法 → `STOP`（MODEL_OUTPUT_INVALID）。"""
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise RequestInvalidError(
                "attempt 必须是 >=1 的整数", details={"attempt": attempt}
            )

        if attempt == 1 and self._retry_count < self._max_repair_retries:
            self._retry_count += 1
            return self._action(
                action="RETRY",
                reason=MODEL_OUTPUT_INVALID,
                explanation=(
                    f"模型第 1 次输出非法，使用唯一一次修复性重试（上限 "
                    f"{self._max_repair_retries}），不推进任务状态、不调用工具。"
                ),
                attempt=attempt,
            )

        explanation = (
            f"模型第 {attempt} 次输出非法，修复性重试已用尽（已重试 "
            f"{self._retry_count} 次，上限 {self._max_repair_retries}），"
            f"停止并交由人工处理（reason={MODEL_OUTPUT_INVALID}）。"
        )
        self._emit(
            AuditEventType.AGENT_RECOVERY_STOPPED,
            {
                "outcome": OUTCOME_STOP_INVALID_OUTPUT,
                "reason": MODEL_OUTPUT_INVALID,
                "attempt": attempt,
                "retry_count": self._retry_count,
                "explanation": explanation,
                "plan_version": self._plan_version,
                "tool_calls_delta": 0,
            },
        )
        return self._action(
            action="STOP",
            reason=MODEL_OUTPUT_INVALID,
            explanation=explanation,
            attempt=attempt,
        )

    # ---- AC-15 反证 ----------------------------------------------------
    def register_hypothesis(
        self, statement: str, *, hypothesis_id: str | None = None
    ) -> Hypothesis:
        """登记一条待验证假设（反证的前提；不持久化，进程内保留全版本）。"""
        text = _require_text(statement, "statement")
        identity = _require_text(hypothesis_id, "hypothesis_id") if hypothesis_id else new_id("hyp")
        if identity in self._hypotheses:
            raise RequestInvalidError(
                "假设已存在", details={"hypothesis_id": identity}
            )
        created_at = utc_now()
        hypothesis = Hypothesis(
            hypothesis_id=identity,
            statement=text,
            status="OPEN",
            plan_version=self._plan_version,
            created_at=created_at,
            updated_at=created_at,
        )
        self._hypotheses[identity] = hypothesis
        self._history[identity] = [hypothesis]
        return hypothesis

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        """按 id 取当前版本；反证后仍可查到（不物理删除）。"""
        identity = _require_text(hypothesis_id, "hypothesis_id")
        hypothesis = self._hypotheses.get(identity)
        if hypothesis is None:
            raise RequestInvalidError("假设不存在", details={"hypothesis_id": identity})
        return hypothesis

    def history(self, hypothesis_id: str) -> tuple[Hypothesis, ...]:
        """取某假设的全部版本（旧版本在前，含当前版本）。"""
        identity = _require_text(hypothesis_id, "hypothesis_id")
        if identity not in self._history:
            raise RequestInvalidError("假设不存在", details={"hypothesis_id": identity})
        return tuple(self._history[identity])

    def refute(self, hypothesis_id: str, *, reason: str) -> Hypothesis:
        """反证关闭：`status=REFUTED` + `plan_version+1`，历史追加而非覆盖。

        重复反证同一假设是**幂等**的：已 `REFUTED` 时原样返回，不再涨版本
        （避免同一事实被重复计数成两次计划演进）。
        """
        current = self.get_hypothesis(hypothesis_id)
        text = _require_text(reason, "reason")
        if current.status == "REFUTED":
            return current

        self._plan_version += 1
        now = utc_now()
        refuted = Hypothesis(
            hypothesis_id=current.hypothesis_id,
            statement=current.statement,
            status="REFUTED",
            plan_version=self._plan_version,
            reason=text,
            created_at=current.created_at,
            updated_at=now,
            refuted_at=now,
        )
        self._hypotheses[refuted.hypothesis_id] = refuted
        self._history[refuted.hypothesis_id].append(refuted)
        return refuted

    # ---- AC-16 UNKNOWN 收敛 --------------------------------------------
    def reconcile_unknown(
        self,
        *,
        action_digest: str,
        idempotency_key: str,
        target_objects: Sequence[object],
        evidence_sufficient: bool,
    ) -> RecoveryOutcome:
        """UNKNOWN 收敛为 `NEEDS_RECONCILIATION` / `MANUAL_INTERVENTION`，绝不重放写。"""
        digest = _require_text(action_digest, "action_digest")
        key = _require_text(idempotency_key, "idempotency_key")
        if isinstance(target_objects, (str, bytes, bytearray)):
            raise RequestInvalidError(
                "target_objects 必须是序列，不得为字符串",
                details={"target_objects_invalid": True},
            )
        targets = normalize_targets(tuple(target_objects))
        if not isinstance(evidence_sufficient, bool):
            raise RequestInvalidError(
                "evidence_sufficient 必须是布尔值",
                details={"evidence_sufficient": evidence_sufficient},
            )

        if evidence_sufficient:
            status = TaskStatus.NEEDS_RECONCILIATION
            reason = UNKNOWN_OUTCOME_EVIDENCE_SUFFICIENT
        else:
            status = TaskStatus.MANUAL_INTERVENTION
            reason = UNKNOWN_OUTCOME_EVIDENCE_INSUFFICIENT

        applied, applied_note = self._converge(status)
        explanation = (
            f"结果未知（action_digest={_short(digest)}、idempotency_key={key}、"
            f"目标 {len(targets)} 个、证据{'充分' if evidence_sufficient else '不足'}）"
            f"收敛为 {status.value}；恢复器未调用任何工具、未重放写"
            f"（tool_calls_delta=0）；任务状态{applied_note}。"
        )
        self._begin_recovery(TRIGGER_UNKNOWN_OUTCOME)
        self._emit(
            AuditEventType.AGENT_RECOVERY_STOPPED,
            {
                "outcome": OUTCOME_ESCALATE,
                "reason": reason,
                "converged_status": status.value,
                "action_digest": digest,
                "idempotency_key": key,
                "target_object_count": len(targets),
                "evidence_sufficient": evidence_sufficient,
                "applied": applied,
                "explanation": explanation,
                "plan_version": self._plan_version,
                "tool_calls_delta": 0,
            },
        )
        return RecoveryOutcome(
            status=status,
            reason=reason,
            explanation=explanation,
            action_digest=digest,
            idempotency_key=key,
            target_objects=targets,
            evidence_sufficient=evidence_sufficient,
            applied=applied,
            tool_calls_delta=0,
            replayed=False,
            plan_version=self._plan_version,
            occurred_at=utc_now(),
        )

    # ---- 内部 ----------------------------------------------------------
    def _begin_recovery(self, trigger: str, *, call_key: str | None = None) -> None:
        """接管开始（`AGENT_RECOVERY_STARTED`）：**同一次接管只发一条**。

        幂等闸门 `_takeover_open`：首次判停/升级时置位并发事件；`observe_*`
        再次观测到进展（`CONTINUE`）时清零，于是"下一次接管"会重新发一条。
        payload 按设计 §5 第 10 行：`trigger` / `call_key` / `plan_version` /
        `evidence_count`。
        """
        if self._takeover_open:
            return
        self._takeover_open = True
        self._emit(
            AuditEventType.AGENT_RECOVERY_STARTED,
            {
                "trigger": trigger,
                "call_key": call_key,
                "plan_version": self._plan_version,
                "evidence_count": self._evidence_count,
                # 接管本身不产生工具调用（可断言证据）。
                "tool_calls_delta": 0,
            },
        )

    def _action(
        self,
        *,
        action: str,
        reason: str,
        explanation: str,
        call_key: str | None = None,
        evidence_digest: str | None = None,
        attempt: int | None = None,
        repeats: int = 0,
    ) -> RecoveryAction:
        return RecoveryAction(
            action=action,  # type: ignore[arg-type] - 取值由调用点封闭
            reason=reason,
            explanation=explanation,
            call_key=call_key,
            evidence_digest=evidence_digest,
            attempt=attempt,
            repeats=repeats,
            retry_count=self._retry_count,
            plan_version=self._plan_version,
        )

    def _converge(self, target: TaskStatus) -> tuple[bool, str]:
        """把任务推进到收敛态；非法迁移**不伪造**状态变更。"""
        if not self._task_id:
            raise RequestInvalidError(
                "未绑定任务上下文，无法推进任务状态", details={"task_id": None}
            )
        task = self._tasks.get_task(self._task_id)
        if task.status is target:
            return True, f"已是 {target.value}（幂等）"
        if not can_transition(task.status, target):
            return (
                False,
                f"停留在 {task.status.value}（{task.status.value} → {target.value} 非法，"
                f"reason={NO_LEGAL_TRANSITION}，交由人工核对）",
            )
        self._tasks.transition(self._task_id, target)
        return True, f"已从 {task.status.value} 推进到 {target.value}"

    def _emit(self, event_type: AuditEventType, payload: dict[str, Any]) -> None:
        """写审计；未绑定任务上下文时跳过并计数（绝不写 `task_id` 为空的脏行）。"""
        if not self._task_id or not self._run_id:
            self._skipped_events += 1
            return
        event = make_event(
            event_type,
            task_id=self._task_id,
            run_id=self._run_id,
            payload=payload,
        )
        self._audit.emit_required(event)


def _threshold(value: object, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestInvalidError(
            f"{label} 必须是整数", details={label: value}
        )
    if value < minimum:
        raise RequestInvalidError(
            f"{label} 不得小于 {minimum}", details={label: value, "minimum": minimum}
        )
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequestInvalidError(
            f"{label} 必须是非空字符串", details={label: value}
        )
    return value


def _short(digest: str, size: int = 12) -> str:
    return digest if len(digest) <= size else f"{digest[:size]}..."


__all__ = [
    "LOOP_DETECTED",
    "MAX_REPAIR_RETRIES_LIMIT",
    "MODEL_OUTPUT_INVALID",
    "NO_LEGAL_TRANSITION",
    "NO_PROGRESS",
    "OUTCOME_ESCALATE",
    "OUTCOME_STOP_INVALID_OUTPUT",
    "RecoveryController",
    "TRIGGER_LOOP_DETECTED",
    "TRIGGER_NO_PROGRESS",
    "TRIGGER_UNKNOWN_OUTCOME",
    "UNKNOWN_OUTCOME_EVIDENCE_INSUFFICIENT",
    "UNKNOWN_OUTCOME_EVIDENCE_SUFFICIENT",
]
