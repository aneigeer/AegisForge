"""Agent Loop v0.1：Plan -> Policy Check -> Tool Call -> Observation -> Update Plan -> Stop。

设计约束（总纲 3.2 / 4.2 / 4.4）：
- 模型输出只作为建议，状态转换、权限判定与工具选择校验由系统代码执行；
- 模型只能调用注册表内的只读工具，非法计划/输出一律不执行工具；
- 设置步骤、工具调用、模型调用、Token 与总耗时预算，检测重复调用和无进展循环；
- 所有状态变化、模型调用摘要、工具调用与停止原因写入审计事件与本地轨迹；
- 暂停/取消后不再发起新的工具调用；模型超时或非法输出时任务进入 FAILED 安全状态。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from core.agent.budget import AgentBudget, BudgetTracker
from core.agent.contracts import Plan, PlanStep, PlannerDecision, ToolCallProposal
from core.agent.planner import Planner
from core.agent.trajectory import Trajectory, TrajectoryRecorder, TrajectoryStore
from core.audit.events import AuditEventType, make_event
from core.context.builder import ContextBuilder
from core.context.contracts import ContextBundle, EvidenceItem
from core.errors import (
    BudgetExceededError,
    DBGuardianError,
    ErrorCode,
    LoopDetectedError,
    ModelCallFailedError,
    ModelOutputInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
    NoProgressError,
    PlanInvalidError,
    PolicyDeniedError,
    StateInvalidError,
)
from core.logging import get_logger
from core.models.contracts import ModelResponse
from core.models.degradation import DegradationController, DegradeReason
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.report.brief import DegradedBrief, build_rule_brief
from core.tasks.models import Task, TaskStatus, TERMINAL_STATUSES
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext, ToolResult, ToolStatus
from core.tools.registry import ToolRegistry

logger = get_logger(__name__)


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    stop_reason: str
    conclusion: str | None = None
    plan: Plan | None = None
    trajectory_id: str
    trajectory_path: str | None = None
    model_version: str | None = None
    usage: dict[str, Any] = {}
    evidence: list[EvidenceItem] = []
    model_calls: int = 0
    tool_calls: int = 0


class _StopLoop(Exception):
    """内部控制流：携带停止原因，交由统一收尾逻辑处理。"""

    def __init__(self, stop_reason: str, *, error_code: str | None = None, message: str = "") -> None:
        super().__init__(message or stop_reason)
        self.stop_reason = stop_reason
        self.error_code = error_code
        self.message = message


def _is_usable_tool_result(result: Any) -> bool:
    """P2-T3：工具结果是否可作为**证据**继续推理。

    `PARTIAL` 表示"有数据但被截断"（warning 已记录），不是失败；此前一律按
    `tool_{status}` 终止，导致大结果集/带 limit 的工具在推理中途被判死
    （PG `get_query_stats` 实测触发）。带 error 的 PARTIAL 仍视为失败。
    """
    if result.status == ToolStatus.SUCCESS:
        return True
    return result.status == ToolStatus.PARTIAL and result.error is None


class AgentLoop:
    def __init__(
        self,
        service: TaskService,
        registry: ToolRegistry,
        gateway: Any,
        planner: Planner,
        context_builder: ContextBuilder,
        *,
        context_factory: Callable[[str], ToolContext],
        budget: AgentBudget | None = None,
        trajectory_store: TrajectoryStore | None = None,
        max_duplicate_calls: int = 2,
        no_progress_threshold: int = 2,
        clock: Callable[[], float] = time.monotonic,
        degradation: DegradationController | None = None,
        redaction_gate: RedactionGate | None = None,
        knowledge_loader: Callable[[Any], Any] | None = None,
    ) -> None:
        self._service = service
        self._registry = registry
        self._gateway = gateway
        self._planner = planner
        self._context_builder = context_builder
        self._context_factory = context_factory
        self._budget = budget or AgentBudget()
        self._trajectory_store = trajectory_store
        self._max_duplicate_calls = max_duplicate_calls
        self._no_progress_threshold = no_progress_threshold
        self._clock = clock
        # W09 T05：降级控制器显式注入（未注入 = 保持 W08 行为，不读全局单例）。
        self._degradation = degradation
        self._redaction_gate = redaction_gate
        # P3-T2：可选的"已验证知识"加载器（返回 EvidenceItem 列表）；缺省不注入。
        self._knowledge_loader = knowledge_loader

    # ---- public --------------------------------------------------------
    def run(self, task_id: str) -> AgentRunResult | DegradedBrief:
        task = self._service.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            raise StateInvalidError(
                "任务已结束，无法运行 Agent Loop",
                details={"task_id": task_id, "status": task.status.value},
            )

        # W09 T05 / AC-16：进入 planner **之前**检查暂停位；暂停时不规划、不选工具，
        # 直接返回规则简报（degraded=True / model_used=False）。
        if self._degradation is not None and self._degradation.planning_suspended:
            return self._degraded_run(task)

        model_version = self._adapter_version()
        recorder = TrajectoryRecorder(
            task_id=task.task_id,
            run_id=task.run_id,
            goal=task.goal,
            model_version=model_version,
        )
        tracker = BudgetTracker(self._budget, clock=self._clock)
        tracker.start()
        evidence: list[EvidenceItem] = []
        plan: Plan | None = None
        conclusion: str | None = None
        stop_reason = "completed"
        error_code: str | None = None
        error_message: str | None = None
        task_steps: list[Any] = []
        executed_keys: Counter[str] = Counter()
        last_digest: str | None = None
        same_digest_count = 0

        task = self._service.get_task(task_id)
        if self._knowledge_loader is not None:
            # 知识只是"参考证据"：加载失败不影响诊断（加载器自身已兜底）。
            try:
                evidence.extend(self._knowledge_loader(task))
            except Exception:  # noqa: BLE001 - 绝不因知识加载中断任务
                pass
        try:
            if task.cancel_requested:
                task = self._cancel(task)
                raise _StopLoop("cancelled")
            if task.pause_requested:
                self._emit(task, AuditEventType.AGENT_PAUSED, {"reason": "pause_requested_before_run"})
                recorder.record("stop", {"stop_reason": "paused", "phase": "before_plan"})
                raise _StopLoop("paused")

            task = self._transition(task, TaskStatus.PLANNING)
            bundle = self._build_context(task, evidence, tracker)
            plan, response = self._call_model(
                task,
                recorder,
                tracker,
                "plan",
                lambda: self._planner.create_plan(bundle),
                input_summary={"goal": task.goal, "evidence_count": len(evidence)},
                output_summary=lambda value: value[0].model_dump(mode="json"),
            )
            task_steps = self._persist_plan(task, plan)
            self._emit(
                task,
                AuditEventType.AGENT_PLAN_CREATED,
                {
                    "step_count": len(plan.steps),
                    "steps": [step.model_dump(mode="json") for step in plan.steps],
                    "model_version": response.model_version,
                },
            )
            recorder.record("plan", plan.model_dump(mode="json"))
            self._service.save_checkpoint(
                task_id,
                {
                    "status": task.status.value,
                    "reason": "plan_created",
                    "plan": plan.model_dump(mode="json"),
                },
            )

            index = 0
            while index < len(plan.steps):
                task = self._service.get_task(task_id)
                if task.cancel_requested:
                    task = self._cancel(task)
                    raise _StopLoop("cancelled")
                if task.pause_requested:
                    self._emit(task, AuditEventType.AGENT_PAUSED, {"reason": "pause_requested", "step_index": index})
                    recorder.record("stop", {"stop_reason": "paused", "step_index": index})
                    raise _StopLoop("paused")

                step = plan.steps[index]
                key = self._call_key(step)
                if executed_keys[key] >= self._max_duplicate_calls:
                    self._emit(
                        task,
                        AuditEventType.AGENT_LOOP_DETECTED,
                        {"tool_name": step.tool_name, "params": step.params, "occurrences": executed_keys[key]},
                    )
                    raise LoopDetectedError(
                        "检测到重复工具调用，停止以避免无进展循环",
                        details={"tool_name": step.tool_name, "params": step.params, "occurrences": executed_keys[key]},
                    )

                tracker.check_step()
                tracker.check_tool_call()
                self._planner.validate_step(step)

                task = self._ensure_observing(task)
                task_step = task_steps[index]
                self._service.repository.update_step_status(task_step.step_id, "RUNNING")
                try:
                    result = self._service.run_tool(
                        task_id,
                        step.tool_name,
                        step.params,
                        self._context_factory(task_id),
                        step_id=task_step.step_id,
                    )
                except PolicyDeniedError:
                    if self._service.get_task(task_id).pause_requested:
                        raise _StopLoop("paused") from None
                    raise

                executed_keys[key] += 1
                tracker.record_tool(result.duration_ms)
                tracker.ensure_time()

                evidence_item = self._context_builder.summarize_result(
                    result, evidence_id=f"ev_{index:03d}_{task_step.step_id[-6:]}"
                )
                evidence.append(evidence_item)
                recorder.record(
                    "tool_result",
                    {
                        "step_index": index,
                        "tool_name": step.tool_name,
                        "status": result.status.value,
                        "duration_ms": result.duration_ms,
                        "warnings": result.warnings,
                        "evidence_summary": evidence_item.summary,
                        "data_digest": evidence_item.data_digest,
                        "error": result.error.model_dump(mode="json") if result.error else None,
                    },
                )

                if _is_usable_tool_result(result):
                    self._service.repository.update_step_status(task_step.step_id, "COMPLETED")
                    completed_payload: dict[str, Any] = {
                        "step_index": index,
                        "tool_name": step.tool_name,
                        "duration_ms": result.duration_ms,
                        "evidence_id": evidence_item.evidence_id,
                    }
                    if result.status != ToolStatus.SUCCESS:
                        # 只在非 SUCCESS 的可用路径补一个字段，既有事件形状不变。
                        completed_payload["tool_status"] = result.status.value
                    self._emit(
                        task,
                        AuditEventType.AGENT_STEP_COMPLETED,
                        completed_payload,
                    )
                else:
                    self._service.repository.update_step_status(task_step.step_id, "FAILED")
                    self._emit(
                        task,
                        AuditEventType.AGENT_STEP_FAILED,
                        {
                            "step_index": index,
                            "tool_name": step.tool_name,
                            "status": result.status.value,
                            "error": result.error.model_dump(mode="json") if result.error else None,
                        },
                    )
                    error_code = (result.error.code if result.error else ErrorCode.INTERNAL.value)
                    raise _StopLoop(
                        f"tool_{result.status.value}",
                        error_code=error_code,
                        message=result.error.message if result.error else "工具调用失败",
                    )

                if evidence_item.data_digest == last_digest:
                    same_digest_count += 1
                else:
                    same_digest_count = 0
                last_digest = evidence_item.data_digest
                if same_digest_count >= self._no_progress_threshold:
                    self._emit(
                        task,
                        AuditEventType.AGENT_NO_PROGRESS,
                        {"tool_name": step.tool_name, "repeats": same_digest_count + 1},
                    )
                    raise NoProgressError(
                        "连续观测结果未发生变化，停止以避免无进展循环",
                        details={"tool_name": step.tool_name, "repeats": same_digest_count + 1},
                    )

                task = self._ensure_diagnosing(task)
                bundle = self._build_context(task, evidence, tracker)
                decision, response = self._call_model(
                    task,
                    recorder,
                    tracker,
                    "assess",
                    lambda: self._planner.assess(bundle, plan, evidence),
                    input_summary={
                        "goal": task.goal,
                        "evidence_count": len(evidence),
                        "step_index": index,
                    },
                    output_summary=lambda value: value[0].model_dump(mode="json"),
                )
                recorder.record("decision", decision.model_dump(mode="json"))
                self._service.save_checkpoint(
                    task_id,
                    {
                        "status": task.status.value,
                        "reason": "decision",
                        "step_index": index,
                        "decision": decision.model_dump(mode="json"),
                    },
                )

                if decision.action == "stop":
                    conclusion = decision.conclusion
                    stop_reason = "planned_stop"
                    break

                if decision.next_step is not None:
                    self._planner.validate_step(decision.next_step)
                    plan.steps.insert(index + 1, decision.next_step)
                    task_steps.insert(index + 1, self._add_step(task, decision.next_step))
                    recorder.record("plan", {"added_step": decision.next_step.model_dump(mode="json")})

                index += 1

                if index >= len(plan.steps):
                    proposal = self._propose_tool_call(task, plan, evidence, recorder, tracker)
                    if proposal is None:
                        stop_reason = "no_further_steps"
                        break
                    new_step = PlanStep(
                        title=f"追加观察：{proposal.tool_name}",
                        tool_name=proposal.tool_name,
                        params=proposal.arguments,
                        expected_evidence=proposal.expected_evidence,
                        risk_level="R0",
                    )
                    self._planner.validate_step(new_step)
                    plan.steps.append(new_step)
                    task_steps.append(self._add_step(task, new_step))
                    recorder.record("plan", {"added_step": new_step.model_dump(mode="json")})

            if stop_reason == "completed" and conclusion is None:
                conclusion = "只读诊断循环已完成观察阶段；根因结论由后续诊断引擎基于证据生成。"
        except _StopLoop as stop:
            stop_reason = stop.stop_reason
            error_code = error_code or stop.error_code
            error_message = error_message or stop.message or None
        except BudgetExceededError as exc:
            stop_reason = "budget_exceeded"
            error_code = exc.code.value
            error_message = exc.message
            self._emit(task, AuditEventType.AGENT_BUDGET_EXCEEDED, {"details": exc.details})
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except LoopDetectedError as exc:
            stop_reason = "loop_detected"
            error_code = exc.code.value
            error_message = exc.message
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except NoProgressError as exc:
            stop_reason = "no_progress"
            error_code = exc.code.value
            error_message = exc.message
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except PlanInvalidError as exc:
            stop_reason = "plan_invalid"
            error_code = exc.code.value
            error_message = exc.message
            self._emit(task, AuditEventType.AGENT_PLAN_REJECTED, {"reason": exc.message, "details": exc.details})
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except ModelTimeoutError as exc:
            stop_reason = "model_timeout"
            error_code = exc.code.value
            error_message = exc.message
            self._record_degradation(task, DegradeReason.MODEL_TIMEOUT)
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except (ModelUnavailableError, ModelCallFailedError, ModelOutputInvalidError) as exc:
            stop_reason = "model_failed"
            error_code = exc.code.value
            error_message = exc.message
            if isinstance(exc, ModelOutputInvalidError):
                reason = DegradeReason.MODEL_OUTPUT_INVALID
            elif isinstance(exc, ModelUnavailableError):
                reason = DegradeReason.MODEL_DISABLED
            else:
                reason = DegradeReason.MODEL_CALL_FAILED
            self._record_degradation(task, reason)
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except PolicyDeniedError as exc:
            stop_reason = "policy_denied"
            error_code = exc.code.value
            error_message = exc.message
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except DBGuardianError as exc:
            stop_reason = "error"
            error_code = exc.code.value
            error_message = exc.message
            recorder.record("error", {"reason": stop_reason, "details": exc.details})
        except Exception:
            logger.exception("Agent Loop 未归一化异常")
            stop_reason = "internal_error"
            error_code = ErrorCode.INTERNAL.value
            error_message = "Agent Loop 内部错误"
            recorder.record("error", {"reason": stop_reason})

        return self._finalize(
            task_id,
            recorder=recorder,
            tracker=tracker,
            evidence=evidence,
            plan=plan,
            conclusion=conclusion,
            stop_reason=stop_reason,
            error_code=error_code,
            error_message=error_message,
            model_version=model_version,
        )

    # ---- degraded path -------------------------------------------------
    def _degraded_run(self, task: Task) -> DegradedBrief:
        """暂停新规划：只发降级事件并基于既有证据生成规则简报。

        降级判定发生在 planner 之前，因此结构上不可能调用 planner / tool_call；
        `AgentLoop` 也不持有工具执行入口以外的写能力，`tool_calls` 增量为 0。
        """
        controller = self._degradation
        if controller is None:  # pragma: no cover - 调用点已保证非 None
            raise StateInvalidError(
                "降级路径缺少 DegradationController",
                details={"task_id": task.task_id},
            )
        reason = controller.last_reason or DegradeReason.MODEL_DISABLED
        controller.record_failure(reason, task_id=task.task_id, run_id=task.run_id)
        gate = self._redaction_gate or RedactionGate(DataRedactionEngine())
        # 运行开始时尚未采集新证据：规则简报只基于既有（此处为空）证据，
        # 不调用任何工具、不伪造模型结论。
        return build_rule_brief(
            task=task,
            evidence=[],
            summaries=[],
            reason=reason,
            gate=gate,
        )

    def _record_degradation(self, task: Task, reason: DegradeReason) -> None:
        """模型超时/失败时通知降级控制器；审计写失败不得掩盖原始停止原因。"""
        if self._degradation is None:
            return
        try:
            self._degradation.record_failure(
                reason, task_id=task.task_id, run_id=task.run_id
            )
        except DBGuardianError:
            logger.exception("记录降级事件失败，保持原始停止原因不变")

    # ---- plan / steps --------------------------------------------------
    def _persist_plan(self, task: Task, plan: Plan) -> list[Any]:
        steps = [self._add_step(task, plan_step) for plan_step in plan.steps]
        return steps

    def _add_step(self, task: Task, step: PlanStep) -> Any:
        return self._service.add_step(
            task.task_id,
            title=step.title,
            tool_name=step.tool_name,
            params=step.params,
        )

    def _propose_tool_call(
        self,
        task: Task,
        plan: Plan,
        evidence: list[EvidenceItem],
        recorder: TrajectoryRecorder,
        tracker: BudgetTracker,
    ) -> ToolCallProposal | None:
        bundle = self._build_context(task, evidence, tracker)
        proposal, response = self._call_model(
            task,
            recorder,
            tracker,
            "tool_call",
            lambda: self._planner.propose_tool_call(bundle, plan, evidence),
            input_summary={"goal": task.goal, "evidence_count": len(evidence)},
            output_summary=lambda value: value[0].model_dump(mode="json") if value[0] else None,
        )
        return proposal

    # ---- context / model ---------------------------------------------------
    def _build_context(
        self, task: Task, evidence: list[EvidenceItem], tracker: BudgetTracker
    ) -> ContextBundle:
        identity = self._context_factory(task.task_id).identity()
        return self._context_builder.build(
            goal=task.goal,
            identity=identity,
            read_only=True,
            budget=tracker.remaining(),
            evidence=evidence,
        )

    def _call_model(
        self,
        task: Task,
        recorder: TrajectoryRecorder,
        tracker: BudgetTracker,
        purpose: str,
        call: Callable[[], Any],
        *,
        input_summary: dict[str, Any],
        output_summary: Callable[[Any], Any],
    ) -> Any:
        tracker.check_model_call()
        tracker.ensure_time()
        self._emit(
            task,
            AuditEventType.MODEL_CALL_STARTED,
            {"purpose": purpose, "model_version": self._adapter_version()},
        )
        try:
            value = call()
        except DBGuardianError as exc:
            self._emit(
                task,
                AuditEventType.MODEL_CALL_FAILED,
                {"purpose": purpose, "code": exc.code.value, "message": exc.message},
            )
            recorder.record(
                "error",
                {"purpose": purpose, "code": exc.code.value, "message": exc.message},
            )
            raise

        response: ModelResponse = value[1]
        tracker.record_model(response)
        self._emit(
            task,
            AuditEventType.MODEL_CALL_SUCCEEDED,
            {
                "purpose": purpose,
                "model_version": response.model_version,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "latency_ms": response.latency_ms,
            },
        )
        recorder.record(
            "model_call",
            {
                "purpose": purpose,
                "model_version": response.model_version,
                "usage": response.usage.model_dump(mode="json"),
                "latency_ms": response.latency_ms,
                "input_summary": input_summary,
                "output_summary": self._safe_summary(output_summary, value),
            },
        )
        return value

    @staticmethod
    def _safe_summary(summary: Callable[[Any], Any], value: Any) -> Any:
        try:
            return summary(value)
        except Exception:  # 摘要失败不得影响主流程
            return None

    # ---- state helpers -------------------------------------------------
    def _transition(self, task: Task, target: TaskStatus) -> Task:
        if task.status == target:
            return task
        return self._service.transition(task.task_id, target)

    def _ensure_observing(self, task: Task) -> Task:
        if task.status == TaskStatus.OBSERVING:
            return task
        if task.status in {TaskStatus.PLANNING, TaskStatus.DIAGNOSING}:
            return self._transition(task, TaskStatus.OBSERVING)
        raise StateInvalidError(
            "当前任务状态不能进入观察阶段",
            details={"task_id": task.task_id, "status": task.status.value},
        )

    def _ensure_diagnosing(self, task: Task) -> Task:
        if task.status == TaskStatus.DIAGNOSING:
            return task
        if task.status in {TaskStatus.OBSERVING, TaskStatus.PLANNING}:
            return self._transition(task, TaskStatus.DIAGNOSING)
        raise StateInvalidError(
            "当前任务状态不能进入诊断阶段",
            details={"task_id": task.task_id, "status": task.status.value},
        )

    def _cancel(self, task: Task) -> Task:
        if task.status in TERMINAL_STATUSES:
            return task
        return self._service.transition(task.task_id, TaskStatus.CANCELLED)

    # ---- finalize ------------------------------------------------------
    def _finalize(
        self,
        task_id: str,
        *,
        recorder: TrajectoryRecorder,
        tracker: BudgetTracker,
        evidence: list[EvidenceItem],
        plan: Plan | None,
        conclusion: str | None,
        stop_reason: str,
        error_code: str | None,
        error_message: str | None,
        model_version: str | None,
    ) -> AgentRunResult:
        task = self._service.get_task(task_id)
        # 未执行/未继续的步骤标记为 SKIPPED，避免任务表留下永久 PENDING 的孤儿步骤。
        for step in self._service.repository.list_steps(task_id):
            if step.status == "PENDING":
                self._service.repository.update_step_status(step.step_id, "SKIPPED")
        paused = stop_reason == "paused"
        cancelled = stop_reason == "cancelled"
        failed = stop_reason not in {"completed", "planned_stop", "no_further_steps", "paused", "cancelled"}

        if paused:
            self._emit(task, AuditEventType.AGENT_PAUSED, {"reason": "paused", "status": task.status.value})
        elif cancelled:
            if task.status not in TERMINAL_STATUSES:
                task = self._service.transition(
                    task_id,
                    TaskStatus.CANCELLED,
                    error_code=error_code,
                    error_message=error_message,
                )
        elif failed:
            if task.status not in TERMINAL_STATUSES:
                task = self._service.transition(
                    task_id,
                    TaskStatus.FAILED,
                    error_code=error_code or ErrorCode.INTERNAL.value,
                    error_message=error_message or stop_reason,
                )
        else:
            task = self._ensure_diagnosing(task) if task.status != TaskStatus.DIAGNOSING else task
            if task.status == TaskStatus.DIAGNOSING:
                task = self._service.transition(task_id, TaskStatus.COMPLETED)

        usage = tracker.usage()
        self._service.save_checkpoint(
            task_id,
            {
                "status": task.status.value,
                "reason": "agent_loop_stop",
                "stop_reason": stop_reason,
                "conclusion": conclusion,
                "evidence_count": len(evidence),
                "usage": usage.model_dump(mode="json"),
            },
        )

        recorder.record(
            "stop",
            {
                "stop_reason": stop_reason,
                "status": task.status.value,
                "error_code": error_code,
                "usage": usage.model_dump(mode="json"),
                "evidence_count": len(evidence),
            },
        )
        trajectory: Trajectory = recorder.finish(
            stop_reason=stop_reason, usage=usage.model_dump(mode="json")
        )
        trajectory_path: str | None = None
        if self._trajectory_store is not None:
            try:
                trajectory_path = str(self._trajectory_store.save(trajectory))
            except Exception:  # 轨迹保存失败不应改变任务状态
                logger.exception("保存轨迹失败，任务状态保持不变")

        self._emit(
            task,
            AuditEventType.AGENT_STOPPED,
            {
                "stop_reason": stop_reason,
                "status": task.status.value,
                "usage": usage.model_dump(mode="json"),
                "trajectory_id": trajectory.trajectory_id,
                "evidence_count": len(evidence),
                "error_code": error_code,
            },
        )
        return AgentRunResult(
            task=task,
            stop_reason=stop_reason,
            conclusion=conclusion,
            plan=plan,
            trajectory_id=trajectory.trajectory_id,
            trajectory_path=trajectory_path,
            model_version=model_version,
            usage=usage.model_dump(mode="json"),
            evidence=evidence,
            model_calls=usage.model_calls,
            tool_calls=usage.tool_calls,
        )

    # ---- audit ---------------------------------------------------------
    def _emit(self, task: Task, event_type: AuditEventType, payload: dict[str, Any]) -> None:
        self._service.repository.insert_event(
            make_event(event_type, task_id=task.task_id, run_id=task.run_id, payload=payload)
        )

    def _adapter_version(self) -> str | None:
        adapter = getattr(self._gateway, "adapter", None)
        if adapter is None:
            return None
        return (
            f"{getattr(adapter, 'provider', 'unknown')}:"
            f"{getattr(adapter, 'model_name', 'unknown')}:"
            f"{getattr(adapter, 'adapter_version', 'unknown')}"
        )

    @staticmethod
    def _call_key(step: PlanStep) -> str:
        payload = json.dumps(
            {"tool_name": step.tool_name, "params": step.params},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["AgentLoop", "AgentRunResult"]
