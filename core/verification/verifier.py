"""W08 验证执行器（T03）：状态推进、双路采集、汇总判定、只追加落表与四种出边。

本模块只做**编排**，判定逻辑全部复用 T02 已交付的实现：

- `VerificationPlan.from_disclosure()` 从审批披露快照构造计划（fail closed）；
- `evaluate_plan()` 按 `source` 取异路径证据并逐条比对（缺失即 UNKNOWN）；
- `aggregate()` 最差法汇总（FAIL > UNKNOWN > PASS）；
- `resolve_disposition()` 九格矩阵分派；
- `VerificationResultRepository.insert()` 只追加落表。

结构约束（可逐条核对）：

1. `verify()` 形参只有 `(task, *, tool_call_id, approval_id)`——**不存在**
   `model_output` / `llm` / `narrative`，模型文本没有入口（AC-04）；
2. 第一步即 `EXECUTING → VERIFYING`（A3b），且在读取计划与任何采集之前；
   已是 `VERIFYING` 则跳过（幂等，不重复涨 `state_version`）；
3. 任一步抛错都被包住：落 `NEEDS_RECONCILIATION` + 发
   `VERIFICATION_FAILED(reason="VERIFIER_ERROR")`，**绝不停留在 `EXECUTING`**；
4. 每条出边前先 `can_transition()`；非法则记录 `NO_LEGAL_TRANSITION` 并升级人工；
5. `target_readback` 只吃 `ReadbackPort.read_target(TargetObject)` 的独立样本，
   **不复用** `step_observed.result`，采集失败一律 UNKNOWN（不得 PASS）。

关于 `definition`：`from_disclosure(approval, *, definition, parameters)` 的
`definition` 用于与工具契约交叉校验快照是否同源。T03 卡把依赖面固定为
`plan_source.get(approval_id) -> ApprovalDetail`（不注入注册表、不改
`core/approval/*`），因此此处传 `None`（跳过契约交叉校验）；「断言 >= 3 条、
覆盖 `step_observed` 与 `target_readback` 两种来源」仍由 `from_disclosure`
按快照自身完整性强制（不足即 `DISCLOSURE_INCOMPLETE`，fail closed）。
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.audit.events import AuditEventType, make_event
from core.security.canonical import canonical_digest
from core.tasks.models import Task, TaskStatus, new_id, utc_now
from core.tasks.state_machine import can_transition
from core.verification.disposition import DispositionDecision, resolve_disposition
from core.verification.models import AssertionOutcome, VerificationResult
from core.verification.plan import VerificationPlan, aggregate, evaluate_plan
from core.verification.readback import ReadbackPort, ReadbackSample, TargetObject
from core.verification.repository import VerificationResultRepository

# `VERIFICATION_FAILED.reason` 的稳定取值（设计 §5 第 3 行 + §6 派工说明）。
VERIFIER_ERROR = "VERIFIER_ERROR"
NO_LEGAL_TRANSITION = "NO_LEGAL_TRANSITION"

# 回读目标列在断言 metric 中的写法：`<database>.<table>.<column>`。
_METRIC_PARTS = 3


def _observed_digest(outcomes: Sequence[AssertionOutcome]) -> str:
    """对实测值取摘要：只含时间无关的事实，供 `VERIFICATION_RESULT` 检索比对。"""
    payload = [
        {
            "assertion_id": item.assertion_id,
            "source": item.source,
            "observed": item.observed,
        }
        for item in sorted(outcomes, key=lambda outcome: outcome.assertion_id)
    ]
    return canonical_digest(payload)


def _evidence_sufficient(
    outcomes: Sequence[AssertionOutcome],
    *,
    step_observed: Mapping[str, Any] | None,
    sample: ReadbackSample | None,
) -> bool:
    """设计 R1 的可判定三条件合取（不打分）：

    ① `FAIL` 的断言 `observed` 非 `None`；② `step_observed` 行存在；
    ③ `target_readback` 成功（`sample_id` 非空）。
    """
    if step_observed is None:
        return False
    if sample is None or not sample.sample_id:
        return False
    return all(
        item.observed is not None for item in outcomes if item.verdict == "FAIL"
    )


def _parse_target_key(keys: Any) -> tuple[str, int] | None:
    """把 `target_objects[].keys` 里的 `"<column>=<int>"` 解析成 (列名, 主键值)。"""
    if not isinstance(keys, (list, tuple)):
        return None
    for item in keys:
        column, separator, raw = str(item).partition("=")
        if not separator or not column.strip():
            continue
        try:
            return column.strip(), int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _build_target(
    metric: str, targets: Sequence[Mapping[str, Any]]
) -> TargetObject | None:
    """从审批快照的 `target_objects` + 断言 metric 构造**唯一**回读目标。

    任一环节对不上（metric 非三段、快照无同名目标、主键不可解析、目标不在
    `READBACK_TARGETS` 白名单）都返回 `None`——由调用方把对应断言判 UNKNOWN，
    绝不猜一个目标去读。
    """
    parts = metric.split(".")
    if len(parts) != _METRIC_PARTS:
        return None
    database, table, column = parts
    for target in targets:
        if target.get("database") != database or target.get("name") != table:
            continue
        parsed = _parse_target_key(target.get("keys"))
        if parsed is None:
            continue
        key_column, key_value = parsed
        return TargetObject(
            database=database,
            table=table,
            key_column=key_column,
            key_value=key_value,
            column=column,
        )
    return None


class Verifier:
    """验证编排器：入口推进 → 采集 → 判定 → 落表 → 出边。"""

    def __init__(
        self,
        *,
        plan_source: Any,
        readback: ReadbackPort,
        step_records: Any,
        results: VerificationResultRepository,
        tasks: Any,
        audit: Any,
        settings: Any,
    ) -> None:
        """`plan_source` 只要求 `get(approval_id) -> ApprovalDetail`（T06 装配注入）。

        `settings` 为装配面保留（T03 不读取任何配置项），不做隐式降级。
        """
        self._plan_source = plan_source
        self._readback = readback
        self._step_records = step_records
        self._results = results
        self._tasks = tasks
        self._audit = audit
        self._settings = settings

    # ---- 入口 ----------------------------------------------------------
    def verify(
        self, task: Task, *, tool_call_id: str, approval_id: str
    ) -> VerificationResult:
        """验证一次工具调用的执行结果；返回落表的那条 `VerificationResult`。"""
        collected_at = utc_now()
        plan_digest_text = ""
        try:
            # A3b：必须在读取计划、任何采集之前推进，且幂等。
            task = self._advance_to_verifying(task)

            detail = self._plan_source.get(approval_id)
            parameters = self._parameters_of(detail)
            plan = VerificationPlan.from_disclosure(
                detail, definition=None, parameters=parameters
            )
            plan_digest_text = plan.plan_digest
            self._emit_planned(task, tool_call_id, approval_id, plan)

            step_observed = self._observe_step(tool_call_id)
            sample = self._read_sample(detail, plan)
            outcomes = evaluate_plan(
                plan,
                parameters=parameters,
                step_observed=step_observed,
                readback=sample,
                collected_at=collected_at,
            )
            verdict = aggregate(outcomes)
            # UNKNOWN 本义即证据不足：绝不按「证据充分」自动补偿（设计 §3.2）。
            evidence_sufficient = verdict == "FAIL" and _evidence_sufficient(
                outcomes, step_observed=step_observed, sample=sample
            )
            decision = resolve_disposition(
                verdict, evidence_sufficient=evidence_sufficient
            )
            result = VerificationResult(
                result_id=new_id("vres"),
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                tool_call_id=tool_call_id,
                plan_digest=plan.plan_digest,
                verdict=verdict,
                outcomes=outcomes,
                disposition=decision.disposition,
                approval_required=decision.approval_required,
                # 只有全部断言（含 step_observed 事实）通过才宣称成功。
                claimed_success=verdict == "PASS",
                collected_at=collected_at,
            )
            self._results.insert(result)
            self._emit_result(task, tool_call_id, result)
            if verdict != "PASS":
                self._emit_failed(task, tool_call_id, result)
            self._apply_edge(task, tool_call_id, verdict, decision)
            return result
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不停留在 EXECUTING
            return self._fail_closed(
                task,
                tool_call_id,
                approval_id,
                plan_digest=plan_digest_text,
                collected_at=collected_at,
                error=exc,
            )

    # ---- 入口推进（A3b） ------------------------------------------------
    def _advance_to_verifying(self, task: Task) -> Task:
        if task.status is TaskStatus.VERIFYING:
            return task
        return self._tasks.transition(task.task_id, TaskStatus.VERIFYING)

    # ---- 采集 ----------------------------------------------------------
    @staticmethod
    def _parameters_of(detail: Any) -> dict[str, Any]:
        """`$params.*` 的唯一来源：审批快照 `action_descriptor.parameters`。"""
        descriptor = getattr(detail, "action_descriptor", None)
        if not isinstance(descriptor, Mapping):
            return {}
        params = descriptor.get("parameters")
        return dict(params) if isinstance(params, Mapping) else {}

    def _observe_step(self, tool_call_id: str) -> dict[str, Any] | None:
        """只读只追加证据表；行缺失返回 `None`（对应断言一律 UNKNOWN）。"""
        record = self._step_records.get_observed(tool_call_id)
        if record is None:
            return None
        if is_dataclass(record):
            return asdict(record)
        if isinstance(record, Mapping):
            return dict(record)
        return None

    def _read_sample(
        self, detail: Any, plan: VerificationPlan
    ) -> ReadbackSample | None:
        """独立只读回读；端口缺失/目标不唯一/采集异常 → `None`（断言 UNKNOWN）。"""
        metrics = {
            assertion.metric
            for assertion in plan.assertions
            if assertion.source == "target_readback"
        }
        if len(metrics) != 1:
            # 0 条 → 无需回读；多条 → 单样本接口无法无歧义覆盖，均不猜。
            return None
        descriptor = getattr(detail, "action_descriptor", None)
        targets = (
            descriptor.get("target_objects") if isinstance(descriptor, Mapping) else None
        )
        if not isinstance(targets, list):
            return None
        try:
            target = _build_target(metrics.pop(), [t for t in targets if isinstance(t, Mapping)])
            if target is None:
                return None
            sample = self._readback.read_target(target)
        except Exception:  # noqa: BLE001 - 采集失败只能收敛为 UNKNOWN，不得 PASS
            return None
        return sample if isinstance(sample, ReadbackSample) else None

    # ---- 出边 ----------------------------------------------------------
    @staticmethod
    def _edge_target(verdict: str, decision: DispositionDecision) -> TaskStatus:
        if verdict == "PASS":
            return TaskStatus.COMPLETED
        if verdict == "FAIL":
            return (
                TaskStatus.FAILED
                if decision.evidence_sufficient
                else TaskStatus.MANUAL_INTERVENTION
            )
        return TaskStatus.NEEDS_RECONCILIATION

    def _apply_edge(
        self,
        task: Task,
        tool_call_id: str,
        verdict: str,
        decision: DispositionDecision,
    ) -> None:
        """先 `can_transition` 再出边；非法则记录 `NO_LEGAL_TRANSITION` 并升级人工。"""
        target = self._edge_target(verdict, decision)
        if task.status is target:
            return
        if can_transition(task.status, target):
            self._tasks.transition(task.task_id, target)
            return
        self._emit_best_effort(
            AuditEventType.VERIFICATION_FAILED,
            task,
            tool_call_id,
            {
                "verdict": verdict,
                "failed_assertions": [],
                "unknown_assertions": [],
                "disposition": decision.disposition,
                "approval_required": decision.approval_required,
                "reason": NO_LEGAL_TRANSITION,
                "from": task.status.value,
                "rejected_target": target.value,
            },
        )
        if can_transition(task.status, TaskStatus.MANUAL_INTERVENTION):
            self._tasks.transition(task.task_id, TaskStatus.MANUAL_INTERVENTION)

    # ---- 异常兜底 ------------------------------------------------------
    def _fail_closed(
        self,
        task: Task,
        tool_call_id: str,
        approval_id: str,
        *,
        plan_digest: str,
        collected_at: datetime,
        error: Exception,
    ) -> VerificationResult:
        """Verifier 内部异常：收敛到可核对的终态，绝不停留在 `EXECUTING`。"""
        result = VerificationResult(
            result_id=new_id("vres"),
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            tool_call_id=tool_call_id,
            plan_digest=plan_digest,
            verdict="UNKNOWN",
            outcomes=(),
            disposition=None,
            approval_required=False,
            claimed_success=False,
            collected_at=collected_at,
        )
        transition_note = self._recover_transition(task)
        self._emit_best_effort(
            AuditEventType.VERIFICATION_RESULT,
            task,
            tool_call_id,
            {
                "verdict": result.verdict,
                "outcomes": [],
                "observed_digest": _observed_digest(result.outcomes),
                "collected_at": collected_at.isoformat(),
                "plan_digest": plan_digest,
                "result_ids": [],
                "disposition": None,
                "approval_required": False,
            },
        )
        self._emit_best_effort(
            AuditEventType.VERIFICATION_FAILED,
            task,
            tool_call_id,
            {
                "verdict": result.verdict,
                "failed_assertions": [],
                "unknown_assertions": [],
                "disposition": None,
                "approval_required": False,
                "approval_id": approval_id,
                "reason": VERIFIER_ERROR,
                "transition": transition_note,
                "error_type": type(error).__name__,
                "error_code": _error_code(error),
            },
        )
        return result

    def _recover_transition(self, task: Task) -> str:
        """错误回落：`NEEDS_RECONCILIATION` → 非法则 `MANUAL_INTERVENTION` → 记录。"""
        if task.status in (
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.MANUAL_INTERVENTION,
        ):
            return task.status.value
        for target in (
            TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.MANUAL_INTERVENTION,
        ):
            if not can_transition(task.status, target):
                continue
            try:
                self._tasks.transition(task.task_id, target)
                return target.value
            except Exception:  # noqa: BLE001 - 兜底路径本身不得再抛
                continue
        return NO_LEGAL_TRANSITION

    # ---- 审计 ----------------------------------------------------------
    def _emit(
        self,
        event_type: AuditEventType,
        task: Task,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> None:
        event = make_event(
            event_type,
            task_id=task.task_id,
            run_id=task.run_id,
            tool_call_id=tool_call_id,
            payload=payload,
        )
        self._audit.emit_required(event)

    def _emit_best_effort(
        self,
        event_type: AuditEventType,
        task: Task,
        tool_call_id: str,
        payload: dict[str, Any],
    ) -> None:
        """兜底路径专用：审计写失败不得掩盖「不停留 EXECUTING」这一首要目标。"""
        try:
            self._emit(event_type, task, tool_call_id, payload)
        except Exception:  # noqa: BLE001
            return

    def _emit_planned(
        self,
        task: Task,
        tool_call_id: str,
        approval_id: str,
        plan: VerificationPlan,
    ) -> None:
        self._emit(
            AuditEventType.VERIFICATION_PLANNED,
            task,
            tool_call_id,
            {
                "task_id": task.task_id,
                "tool_call_id": tool_call_id,
                "approval_id": approval_id,
                "plan_digest": plan.plan_digest,
                "assertion_ids": [item.assertion_id for item in plan.assertions],
                "assertion_count": len(plan.assertions),
                "sources": sorted({item.source for item in plan.assertions}),
            },
        )

    def _emit_result(
        self, task: Task, tool_call_id: str, result: VerificationResult
    ) -> None:
        self._emit(
            AuditEventType.VERIFICATION_RESULT,
            task,
            tool_call_id,
            {
                "verdict": result.verdict,
                "outcomes": [
                    {
                        "assertion_id": item.assertion_id,
                        "verdict": item.verdict,
                        "source": item.source,
                        "tolerance": item.tolerance,
                        "has_observed": item.observed is not None,
                    }
                    for item in result.outcomes
                ],
                "observed_digest": _observed_digest(result.outcomes),
                "collected_at": result.collected_at.isoformat(),
                "plan_digest": result.plan_digest,
                "result_ids": [result.result_id],
                "disposition": result.disposition,
                "approval_required": result.approval_required,
            },
        )

    def _emit_failed(
        self, task: Task, tool_call_id: str, result: VerificationResult
    ) -> None:
        self._emit(
            AuditEventType.VERIFICATION_FAILED,
            task,
            tool_call_id,
            {
                "verdict": result.verdict,
                "failed_assertions": [
                    item.assertion_id
                    for item in result.outcomes
                    if item.verdict == "FAIL"
                ],
                "unknown_assertions": [
                    item.assertion_id
                    for item in result.outcomes
                    if item.verdict == "UNKNOWN"
                ],
                "disposition": result.disposition,
                "approval_required": result.approval_required,
                "plan_digest": result.plan_digest,
                "result_ids": [result.result_id],
            },
        )


def _error_code(error: Exception) -> str | None:
    """错误码归一化为字符串（`ErrorCode` 枚举取其 value），供审计检索。"""
    code = getattr(error, "code", None)
    if code is None:
        return None
    return str(getattr(code, "value", code))


__all__ = ["NO_LEGAL_TRANSITION", "VERIFIER_ERROR", "Verifier"]
