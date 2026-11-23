"""工具执行器：注册校验、Schema 校验、权限/风险预检、超时、幂等、重试与审计。

关键约束：
- 未知工具、非法参数、权限不足、R4 与未审批的 R2/R3 一律在执行前阻断；
- 写操作必须携带幂等键，且永不盲目重试；
- 仅对明确可重试的瞬时错误（DB_TIMEOUT / DB_UNAVAILABLE）重试只读且幂等的工具；
- 所有事件与工具调用记录都关联 task_id / run_id / tool_call_id。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from core.approval.models import ApprovalGrant
from core.audit.events import AuditEventType, make_event, redact_payload
from core.audit.service import AuditService
from core.errors import (
    AuditUnavailableError,
    DatabaseQueryError,
    DBGuardianError,
    ErrorCode,
    ToolNotFoundError,
)
from core.execution.step_records import StepObserved, StepPrepared, StepRecordRepository
from core.logging import get_logger
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tasks.models import (
    Task,
    ToolCallRecord,
    new_id,
    utc_now,
)
from core.tasks.repository import TaskRepository
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.registry import ToolRegistry
from database.observers.utils import utc_now_iso

logger = get_logger(__name__)

_RETRYABLE_CODES = {ErrorCode.DB_TIMEOUT.value, ErrorCode.DB_UNAVAILABLE.value}
_RETRYABLE_STATUSES = {ToolStatus.TIMEOUT, ToolStatus.ERROR}


@dataclass(frozen=True)
class ExecutionPolicy:
    max_retries: int = 2
    backoff_base_ms: int = 100
    backoff_max_ms: int = 2000


@dataclass
class _Denial(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def _is_pg_client(client: object) -> bool:
    from database.postgres.connection import PgClient

    return isinstance(client, PgClient)


def _traced_tool_call(func: Any) -> Any:
    """P4-T3：在执行器这一**唯一出口**记录工具指标与 span。

    只记录工具名/状态/风险与耗时：不记录参数、不记录 SQL、不记录返回内容，
    因此不存在把字面量或凭据带进遥测的路径。
    """
    import functools
    import time as _time

    from core.telemetry.metrics import METRICS
    from core.telemetry.tracing import TRACER

    @functools.wraps(func)
    def wrapper(self: Any, tool_name: str, params: Any, context: Any, **kwargs: Any) -> Any:
        started = _time.perf_counter()
        risk = ""
        try:
            definition = getattr(self._registry.get(tool_name), "definition", None)
            risk = str(getattr(definition, "risk_level", "") or "")
        except Exception:  # noqa: BLE001 - 遥测不得影响主流程
            risk = ""
        try:
            result = func(self, tool_name, params, context, **kwargs)
        except Exception:
            METRICS.inc(
                "dg_tool_calls_total", {"tool": tool_name, "status": "error", "risk": risk}
            )
            with TRACER.span("tool.execute", tool=tool_name, risk=risk, status="error"):
                pass
            raise
        status = str(getattr(getattr(result, "status", ""), "value", ""))
        elapsed = _time.perf_counter() - started
        METRICS.inc("dg_tool_calls_total", {"tool": tool_name, "status": status, "risk": risk})
        METRICS.observe("dg_tool_latency_seconds", elapsed, {"tool": tool_name})
        with TRACER.span(
            "tool.execute", tool=tool_name, risk=risk, status=status, duration_ms=round(elapsed * 1000, 3)
        ):
            pass
        return result

    return wrapper


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        repository: TaskRepository | None = None,
        *,
        policy: ExecutionPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        step_records: StepRecordRepository | None = None,
        audit: AuditService | None = None,
        redaction_gate: RedactionGate | None = None,
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._policy = policy or ExecutionPolicy()
        self._sleep = sleep
        # W06：写操作的执行前证据仓储（未装配时 R2 直接 fail closed）。
        self._step_records = step_records
        # W06：R2 的关键事件必须"写成功才继续"，未装配时退化为普通仓储写入。
        self._audit = audit
        # W09 闸门①（`docs/w09/02-design.md:253`）：所有适配器结果的唯一汇聚点。
        # **恒开**：未显式注入时在此默认构造（引擎无状态、无 DSN、无依赖），
        # 不存在 enabled/skip 之类配置项，因此不是"可关闭的开关"；显式注入只用于
        # 附带 audit / quarantine 落点。
        self._redaction = redaction_gate or RedactionGate(DataRedactionEngine())

    # ---- public --------------------------------------------------------
    @_traced_tool_call
    def execute(
        self,
        tool_name: str,
        params: dict[str, Any] | None,
        context: ToolContext,
        *,
        task: Task | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        step_id: str | None = None,
        idempotency_key: str | None = None,
        approval_grant: ApprovalGrant | None = None,
    ) -> ToolResult:
        task_id = task.task_id if task else context.task_id
        run_id = run_id or (task.run_id if task else context.run_id) or new_id("run")
        call_id = tool_call_id or context.tool_call_id or new_id("call")
        context.task_id = task_id
        context.run_id = run_id
        context.tool_call_id = call_id

        started_at = utc_now_iso()
        started_dt = utc_now()
        started = time.perf_counter()
        params = params or {}
        # 只有本次确实落了 StepPrepared 的调用，才允许补 StepObserved 回执。
        prepared = False

        try:
            tool = self._registry.get(tool_name)
        except ToolNotFoundError as exc:
            return self._deny(
                tool_name,
                context,
                start=started,
                started_at=started_at,
                task=task,
                task_id=task_id,
                run_id=run_id,
                call_id=call_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                params=params,
                code=ErrorCode.TOOL_NOT_FOUND,
                message=exc.message,
                details=exc.details,
            )

        definition: ToolDefinition = tool.definition  # type: ignore[attr-defined]
        try:
            self._precheck(
                definition,
                params,
                context,
                task=task,
                approval_grant=approval_grant,
                idempotency_key=idempotency_key,
            )
            replayed = self._replay_if_possible(
                definition, task_id, run_id, call_id, context, idempotency_key
            )
            if replayed is not None:
                return replayed
            # P0-16：写操作的执行前证据必须先落盘，未落盘不得发起执行。
            prepared = self._prepare_step(
                definition,
                task=task,
                task_id=task_id,
                run_id=run_id,
                call_id=call_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                params=params,
                approval_grant=approval_grant,
            )
            # R2 的"开始执行"事件必须写成功（审计不可用即阻断）。
            self._emit_started(definition, task_id, run_id, call_id, params)
            self._insert_running_call(
                definition, task_id, run_id, call_id, step_id, idempotency_key, params
            )
        except _Denial as denial:
            return self._deny(
                tool_name,
                context,
                start=started,
                started_at=started_at,
                task=task,
                task_id=task_id,
                run_id=run_id,
                call_id=call_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                params=params,
                code=denial.code,
                message=denial.message,
                details=denial.details,
            )
        except AuditUnavailableError as exc:
            # 审计不可写：工具绝不执行，返回结构化拒绝（P0-14 / T7）。
            return self._deny(
                tool_name,
                context,
                start=started,
                started_at=started_at,
                task=task,
                task_id=task_id,
                run_id=run_id,
                call_id=call_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                params=params,
                code=ErrorCode.AUDIT_UNAVAILABLE,
                message=exc.message,
                details=exc.details,
            )

        attempts = 1
        result = self._normalize_write_outcome(
            definition, self._invoke(tool, context, params, definition.timeout_ms)
        )
        while self._should_retry(definition, result, attempts):
            delay_ms = min(
                self._policy.backoff_base_ms * (2 ** (attempts - 1)),
                self._policy.backoff_max_ms,
            )
            self._emit(
                AuditEventType.TOOL_CALL_RETRIED,
                task_id,
                run_id,
                call_id,
                {"tool_name": definition.name, "next_attempt": attempts + 1, "delay_ms": delay_ms},
            )
            self._sleep(delay_ms / 1000)
            attempts += 1
            result = self._normalize_write_outcome(
                definition, self._invoke(tool, context, params, definition.timeout_ms)
            )

        result = result.model_copy(
            update={"task_id": task_id, "run_id": run_id, "tool_call_id": call_id}
        )
        # W09 闸门①：适配器结果的唯一汇聚点，在 `_observe_step` 落盘前完成脱敏。
        # `CLEAN/REDACTED` → data 换成脱敏后内容；`REVIEW_REQUIRED/BLOCKED` → data={}
        # + truncated=True；**ToolStatus 一律不动**（隔离不是权限事实、也不是采集失败）。
        # `ToolContext` 不携带 tenant_id（既有契约，不为此改签名）：有 task 时取
        # task.tenant_id，否则留空 —— 留空只影响隔离区归属，不影响判定与 clear 行为。
        result = self._redaction.for_tool_result(
            result,
            tenant_id=getattr(task, "tenant_id", "") or "",
            task_id=task_id or "",
        )
        if prepared:
            # P0-16：结果确定时补 StepObserved 回执；结果未知刻意保留"未观测"，
            # 由 Reconciler 按"已 Prepared 未 Observed"收敛，不得伪造成已观测。
            result = self._observe_step(
                definition,
                result=result,
                task=task,
                task_id=task_id,
                run_id=run_id,
                call_id=call_id,
                step_id=step_id,
                started_at=started_dt,
            )
        self._finish_call(definition, call_id, result, attempts)
        self._emit_final(definition, task_id, run_id, call_id, result, attempts)
        return result

    # ---- prechecks -----------------------------------------------------
    def _precheck(
        self,
        definition: ToolDefinition,
        params: dict[str, Any],
        context: ToolContext,
        *,
        task: Task | None,
        approval_grant: ApprovalGrant | None,
        idempotency_key: str | None,
    ) -> None:
        if definition.risk_level == "R4":
            raise _Denial(
                ErrorCode.POLICY_DENIED,
                "R4 破坏性操作在 Alpha 阶段禁止执行",
                {"tool_name": definition.name, "risk_level": definition.risk_level},
            )
        if definition.risk_level == "R3":
            # W05：R3 拒绝 Agent 执行，审批不能把 R3 降级为可执行。
            raise _Denial(
                ErrorCode.POLICY_DENIED,
                "R3 高危操作禁止 Agent 执行",
                {"tool_name": definition.name, "risk_level": definition.risk_level},
            )
        if definition.risk_level == "R2":
            # W06：唯一合法的"人工同意"是 ApprovalService.consume() 签发的一次性 grant。
            # 不存在"传 approved=True 即通过"的布尔旁路：类型不符一律拒绝。
            if approval_grant is None:
                raise _Denial(
                    ErrorCode.APPROVAL_REQUIRED,
                    "该操作需要人工审批（必须携带一次性 ApprovalGrant）",
                    {"tool_name": definition.name, "risk_level": definition.risk_level},
                )
            if not isinstance(approval_grant, ApprovalGrant):
                raise _Denial(
                    ErrorCode.APPROVAL_REQUIRED,
                    "审批凭据类型非法：只接受 ApprovalService.consume() 返回的 ApprovalGrant",
                    {
                        "tool_name": definition.name,
                        "provided_type": type(approval_grant).__name__,
                    },
                )
            self._ensure_grant_matches(definition, approval_grant, task=task)

        engine = "postgresql" if "postgresql" in definition.db_engines else "mysql"
        read_only = bool(getattr(context.client, "read_only", True))
        granted = {f"{engine}:read"}
        if not read_only:
            granted.add(f"{engine}:write")
        missing = sorted(set(definition.permissions) - granted)
        if missing:
            raise _Denial(
                ErrorCode.POLICY_DENIED,
                "当前数据库身份缺少工具所需权限",
                {"tool_name": definition.name, "missing_permissions": missing},
            )

        if definition.side_effect == "write" and not idempotency_key:
            raise _Denial(
                ErrorCode.REQUEST_INVALID,
                "写操作必须提供幂等键",
                {"tool_name": definition.name},
            )

        if task is not None and definition.side_effect == "write":
            if task.cancel_requested or task.status.value == "CANCELLED":
                raise _Denial(
                    ErrorCode.POLICY_DENIED,
                    "任务已取消，禁止发起新的写操作",
                    {"task_id": task.task_id, "tool_name": definition.name},
                )

        model = getattr(self._registry.get(definition.name), "input_model", None)
        if isinstance(model, type) and issubclass(model, BaseModel):
            try:
                model.model_validate(params)
            except ValidationError as exc:
                raise _Denial(
                    ErrorCode.REQUEST_INVALID,
                    "工具入参校验失败",
                    {
                        "tool_name": definition.name,
                        "errors": [
                            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                            for item in exc.errors()
                        ],
                    },
                ) from None

    def _ensure_grant_matches(
        self,
        definition: ToolDefinition,
        grant: ApprovalGrant,
        *,
        task: Task | None,
    ) -> None:
        """凭证与本次操作必须严格对应（防跨工具/跨任务复用同一张审批）。"""
        mismatch: dict[str, Any] = {}
        if grant.tool_name != definition.name:
            mismatch["tool_name"] = {"grant": grant.tool_name, "actual": definition.name}
        if grant.tool_version != definition.version:
            mismatch["tool_version"] = {
                "grant": grant.tool_version,
                "actual": definition.version,
            }
        if task is not None:
            if grant.task_id != task.task_id:
                mismatch["task_id"] = {"grant": grant.task_id, "actual": task.task_id}
            if grant.environment_id != task.environment_id:
                mismatch["environment_id"] = {
                    "grant": grant.environment_id,
                    "actual": task.environment_id,
                }
        if mismatch:
            raise _Denial(
                ErrorCode.APPROVAL_DIGEST_MISMATCH,
                "审批凭证与本次操作不匹配",
                {
                    "tool_name": definition.name,
                    "approval_id": grant.approval_id,
                    "channel": "grant",
                    "mismatch": mismatch,
                },
            )

    def _prepare_step(
        self,
        definition: ToolDefinition,
        *,
        task: Task | None,
        task_id: str | None,
        run_id: str,
        call_id: str,
        step_id: str | None,
        idempotency_key: str | None,
        params: dict[str, Any],
        approval_grant: ApprovalGrant | None,
    ) -> bool:
        """写操作执行前落盘 StepPrepared；返回本次是否真的落了执行前证据。

        W07 D7 收紧：删除 W06 遗留的"无 grant 只记 warning 并放行"旁路。
        写操作没有一次性 `ApprovalGrant` 即拒绝（`DENIED / APPROVAL_REQUIRED`），
        且**不落** `step_prepared`（没有执行前证据就不允许执行写入）。
        这样"写调用一定有 prepared 行"成为可断言的不变量，而不是靠日志发现。
        """
        if definition.side_effect != "write":
            return False
        if approval_grant is None:
            raise _Denial(
                ErrorCode.APPROVAL_REQUIRED,
                "写操作必须携带一次性 ApprovalGrant（无审批凭据不得执行写）",
                {
                    "tool_name": definition.name,
                    "risk_level": definition.risk_level,
                    "reason": "WRITE_WITHOUT_APPROVAL_GRANT",
                },
            )
        if self._step_records is None or task is None or task_id is None:
            raise AuditUnavailableError(
                "缺少执行前证据仓储或任务上下文，禁止执行已批准写操作",
                details={"tool_name": definition.name, "task_id": task_id},
            )
        self._step_records.insert_prepared(
            StepPrepared(
                prepared_id=new_id("spr"),
                tenant_id=task.tenant_id,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=call_id,
                tool_name=definition.name,
                tool_version=definition.version,
                policy_version=approval_grant.policy_version,
                environment_id=task.environment_id,
                request=redact_payload(params),
                step_id=step_id,
                approval_id=approval_grant.approval_id,
                action_digest=approval_grant.action_digest,
                idempotency_key=idempotency_key,
                prepared_at=utc_now(),
            )
        )
        return True

    def _observe_step(
        self,
        definition: ToolDefinition,
        *,
        result: ToolResult,
        task: Task | None,
        task_id: str | None,
        run_id: str,
        call_id: str,
        step_id: str | None,
        started_at: datetime,
    ) -> ToolResult:
        """执行后回执（P0-16）：结果确定才落盘，结果未知保持"未观测"。

        回执写不进去时不得把已发生的写操作回报为成功：降级为 UNKNOWN_OUTCOME
        并留 AUDIT_WRITE_FAILED 告警，交由 Gateway/Reconciler 收敛与人工核对。
        """
        if result.status is ToolStatus.UNKNOWN_OUTCOME:
            return result
        if self._step_records is None or task is None or task_id is None:
            return self._unknown_because_no_evidence(result, "EVIDENCE_REPOSITORY_MISSING")
        observed = StepObserved(
            observed_id=new_id("sob"),
            tenant_id=task.tenant_id,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=call_id,
            tool_name=definition.name,
            outcome=result.status.value,
            started_at=started_at,
            result=redact_payload(result.model_dump(mode="json")),
            error_code=result.error.code if result.error else None,
            step_id=step_id,
            finished_at=utc_now(),
            duration_ms=(
                int(result.duration_ms) if result.duration_ms is not None else None
            ),
            observed_at=utc_now(),
        )
        try:
            self._step_records.insert_observed(observed)
        except DBGuardianError as exc:
            reason = exc.code.value if getattr(exc, "code", None) else type(exc).__name__
            logger.error(
                "执行后证据写入失败，结果按未知处理",
                extra={
                    "alert": "AUDIT_WRITE_FAILED",
                    "task_id": task_id,
                    "tool_call_id": call_id,
                    "reason_code": reason,
                },
            )
            return self._unknown_because_no_evidence(result, reason)
        return result

    @staticmethod
    def _unknown_because_no_evidence(result: ToolResult, reason: str) -> ToolResult:
        """证据缺失时统一降级为结果未知，绝不冒充成功或失败。"""
        return result.model_copy(
            update={
                "status": ToolStatus.UNKNOWN_OUTCOME,
                "warnings": [
                    *result.warnings,
                    f"observed_evidence_unavailable:{reason}",
                ],
            }
        )

    def _emit_started(
        self,
        definition: ToolDefinition,
        task_id: str | None,
        run_id: str,
        call_id: str,
        params: dict[str, Any],
    ) -> None:
        """记录"开始执行"；R2 走 `emit_required`（写失败即阻断执行）。"""
        payload = {
            "tool_name": definition.name,
            "version": definition.version,
            "risk_level": definition.risk_level,
            "side_effect": definition.side_effect,
            "params": redact_payload(params),
            "tenant_id": None,
        }
        if definition.risk_level == "R2" and self._audit is not None and task_id:
            self._audit.emit_required(
                make_event(
                    AuditEventType.TOOL_CALL_STARTED,
                    task_id=task_id,
                    run_id=run_id,
                    tool_call_id=call_id,
                    payload=payload,
                )
            )
            return
        self._emit(AuditEventType.TOOL_CALL_STARTED, task_id, run_id, call_id, payload)

    @staticmethod
    def _normalize_write_outcome(
        definition: ToolDefinition, result: ToolResult
    ) -> ToolResult:
        """写操作超时不得被解释为失败：转为 UNKNOWN_OUTCOME（P0-17）。"""
        if definition.side_effect != "write" or result.status is not ToolStatus.TIMEOUT:
            return result
        return result.model_copy(
            update={
                "status": ToolStatus.UNKNOWN_OUTCOME,
                "warnings": [*result.warnings, "write_timeout_outcome_unknown"],
            }
        )

    # ---- idempotency ---------------------------------------------------
    def _replay_if_possible(
        self,
        definition: ToolDefinition,
        task_id: str | None,
        run_id: str,
        call_id: str,
        context: ToolContext,
        idempotency_key: str | None,
    ) -> ToolResult | None:
        if not idempotency_key or not task_id or self._repository is None:
            return None
        existing = self._repository.find_tool_call_by_idempotency(task_id, idempotency_key)
        if existing is None:
            return None
        if existing.status == "running":
            raise _Denial(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键的调用仍在执行中",
                {"task_id": task_id, "idempotency_key": idempotency_key},
            )
        if existing.result is None:
            raise _Denial(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "幂等调用已有记录但缺少可重放结果",
                {"task_id": task_id, "idempotency_key": idempotency_key},
            )
        result = ToolResult.model_validate(existing.result)
        result = result.model_copy(
            update={
                "task_id": task_id,
                "run_id": run_id,
                "tool_call_id": existing.tool_call_id,
                "warnings": [*result.warnings, "idempotent_replay"],
            }
        )
        self._emit(
            AuditEventType.TOOL_CALL_REPLAYED,
            task_id,
            run_id,
            existing.tool_call_id,
            {"tool_name": definition.name, "idempotency_key": idempotency_key},
        )
        return result

    # ---- execution -----------------------------------------------------
    def _invoke(
        self,
        tool: object,
        context: ToolContext,
        params: dict[str, Any],
        timeout_ms: int,
    ) -> ToolResult:
        started = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dg-tool")
        try:
            future = pool.submit(tool.run, context, params)  # type: ignore[attr-defined]
            try:
                result = future.result(timeout=timeout_ms / 1000)
            except FuturesTimeout:
                future.cancel()
                return self._synthetic_result(
                    context,
                    tool_name=getattr(tool, "definition").name,
                    status=ToolStatus.TIMEOUT,
                    code=ErrorCode.DB_TIMEOUT,
                    message="工具执行超时",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    warnings=["工具线程可能仍在后台收尾，写操作需执行后核对"],
                )
            except Exception as exc:
                tool_definition = getattr(tool, "definition")
                is_write = tool_definition.side_effect == "write"
                logger.exception("工具执行抛出未归一化异常")
                # 写工具的连接断开 / 响应丢失 / IO 异常不得解释为"失败"：
                # 目标库可能已提交，结果必须按未知处理并进入核对（P0-17）。
                return self._synthetic_result(
                    context,
                    tool_name=tool_definition.name,
                    status=(
                        ToolStatus.UNKNOWN_OUTCOME if is_write else ToolStatus.ERROR
                    ),
                    code=ErrorCode.INTERNAL,
                    message=(
                        "写工具结果未知，禁止解释为失败"
                        if is_write
                        else "工具内部错误"
                    ),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    warnings=(
                        [f"write_exception_outcome_unknown:{type(exc).__name__}"]
                        if is_write
                        else None
                    ),
                )
            if not isinstance(result, ToolResult):
                return self._synthetic_result(
                    context,
                    tool_name=getattr(tool, "definition").name,
                    status=ToolStatus.ERROR,
                    code=ErrorCode.INTERNAL,
                    message="工具返回类型不符合 ToolResult 契约",
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            return result
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _should_retry(
        self, definition: ToolDefinition, result: ToolResult, attempts: int
    ) -> bool:
        if attempts > self._policy.max_retries:
            return False
        if definition.side_effect == "write":
            return False
        if not definition.idempotent:
            return False
        if result.status not in _RETRYABLE_STATUSES:
            return False
        return bool(result.error and result.error.code in _RETRYABLE_CODES)

    # ---- audit ---------------------------------------------------------
    def _deny(
        self,
        tool_name: str,
        context: ToolContext,
        *,
        start: float,
        started_at: str,
        task: Task | None,
        task_id: str | None,
        run_id: str,
        call_id: str,
        step_id: str | None,
        idempotency_key: str | None,
        params: dict[str, Any],
        code: ErrorCode,
        message: str,
        details: dict[str, Any],
    ) -> ToolResult:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        result = ToolResult(
            tool_name=tool_name,
            status=ToolStatus.DENIED,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=call_id,
            started_at=started_at,
            duration_ms=duration_ms,
            data={},
            warnings=[],
            error=ToolError(code=code.value, message=message, details=details),
            source=self._safe_source(context),
        )
        if task_id:
            self._insert_denied_call(
                tool_name, task_id, run_id, call_id, step_id, idempotency_key, params, result
            )
            self._emit(
                AuditEventType.TOOL_CALL_DENIED,
                task_id,
                run_id,
                call_id,
                {
                    "tool_name": tool_name,
                    "code": code.value,
                    "message": message,
                    "details": redact_payload(details),
                },
            )
        return result

    def _insert_running_call(
        self,
        definition: ToolDefinition,
        task_id: str | None,
        run_id: str,
        call_id: str,
        step_id: str | None,
        idempotency_key: str | None,
        params: dict[str, Any],
    ) -> None:
        if self._repository is None or not task_id:
            return
        record = ToolCallRecord(
            tool_call_id=call_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=definition.name,
            idempotency_key=idempotency_key,
            status="running",
            attempt=1,
            request=redact_payload(params),
            started_at=utc_now(),
        )
        try:
            self._repository.insert_tool_call(record)
        except DatabaseQueryError as exc:
            if exc.details.get("mysql_error_code") == 1062:
                raise _Denial(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同幂等键的调用已存在",
                    {"task_id": task_id, "idempotency_key": idempotency_key},
                ) from None
            raise

    def _insert_denied_call(
        self,
        tool_name: str,
        task_id: str,
        run_id: str,
        call_id: str,
        step_id: str | None,
        idempotency_key: str | None,
        params: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if self._repository is None:
            return
        record = ToolCallRecord(
            tool_call_id=call_id,
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            status=result.status.value,
            attempt=0,
            request=redact_payload(params),
            result=result.model_dump(mode="json"),
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
            started_at=utc_now(),
            finished_at=utc_now(),
            duration_ms=result.duration_ms,
        )
        try:
            self._repository.insert_tool_call(record)
        except DatabaseQueryError as exc:
            if exc.details.get("mysql_error_code") != 1062:
                raise

    def _finish_call(
        self, definition: ToolDefinition, call_id: str, result: ToolResult, attempts: int
    ) -> None:
        if self._repository is None:
            return
        self._repository.update_tool_call(
            call_id,
            status=result.status.value,
            attempt=attempts,
            result=result.model_dump(mode="json"),
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
            duration_ms=result.duration_ms,
        )

    def _emit_final(
        self,
        definition: ToolDefinition,
        task_id: str | None,
        run_id: str,
        call_id: str,
        result: ToolResult,
        attempts: int,
    ) -> None:
        if not task_id:
            return
        event_type = {
            ToolStatus.SUCCESS: AuditEventType.TOOL_CALL_SUCCEEDED,
            ToolStatus.PARTIAL: AuditEventType.TOOL_CALL_SUCCEEDED,
            ToolStatus.TIMEOUT: AuditEventType.TOOL_CALL_TIMEOUT,
            ToolStatus.DENIED: AuditEventType.TOOL_CALL_DENIED,
            ToolStatus.ERROR: AuditEventType.TOOL_CALL_FAILED,
            ToolStatus.NOT_SUPPORTED: AuditEventType.TOOL_CALL_FAILED,
            ToolStatus.UNKNOWN_OUTCOME: AuditEventType.TOOL_CALL_UNKNOWN_OUTCOME,
        }[result.status]
        payload: dict[str, Any] = {
            "tool_name": definition.name,
            "status": result.status.value,
            "attempts": attempts,
            "duration_ms": result.duration_ms,
            "warnings": result.warnings,
            "error": result.error.model_dump(mode="json") if result.error else None,
            "source": result.source.model_dump(mode="json"),
        }
        if definition.audit_policy == "full":
            payload["data"] = result.data
        self._emit(event_type, task_id, run_id, call_id, payload)

    def _emit(
        self,
        event_type: AuditEventType,
        task_id: str | None,
        run_id: str,
        tool_call_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        if self._repository is None or not task_id:
            return
        self._repository.insert_event(
            make_event(
                event_type,
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                payload=payload,
            )
        )

    def _safe_source(self, context: ToolContext) -> ToolSource:
        try:
            identity = context.identity()
            db_version = identity.server_version
        except DBGuardianError:
            db_version = "unknown"
        except Exception:
            db_version = "unknown"
        return ToolSource(
            database_id=context.client.database_id,
            db_version=db_version,
            collected_at=utc_now_iso(),
        )

    def _synthetic_result(
        self,
        context: ToolContext,
        *,
        tool_name: str,
        status: ToolStatus,
        code: ErrorCode,
        message: str,
        duration_ms: float,
        warnings: list[str] | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            status=status,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=utc_now_iso(),
            duration_ms=round(duration_ms, 2),
            data={},
            warnings=warnings or [],
            error=ToolError(code=code.value, message=message),
            source=self._safe_source(context),
        )
