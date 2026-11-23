"""唯一 Tool Gateway：Worker 执行信封的签名、重放、租约与策略校验入口。

职责边界：
- 只接受服务端可验证的信封；不信任 Worker 本地缓存的租约/风险/身份；
- 数据库访问仅限控制库（任务/事件/租约/信封）与执行器内部，绝不接收 DSN；
- 策略拒绝时返回结构化 `ToolResult(DENIED)` 并同时写审计事件。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from core.approval.models import ApprovalGrant
from core.approval.service import ApprovalService
from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.config import Settings
from core.errors import (
    AuditUnavailableError,
    DatabaseQueryError,
    DBGuardianError,
    EnvelopeInvalidError,
    ForbiddenError,
    NonceReplayError,
    StateConflictError,
    WorkerLeaseInvalidError,
)
from core.gateway.envelope import compute_args_digest, verify_envelope
from core.gateway.leases import LeaseService
from core.security.models import (
    AuthMethod,
    EnvironmentContext,
    ExecutionEnvelope,
    PolicyDecision,
    Principal,
    RiskLevel,
    environment_tier,
)
from core.security.roles import Permission, Role
from core.tasks.models import Task, TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.state_machine import can_transition
from core.tools.contracts import (
    PreflightEstimate,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry

ENVELOPES = "dg_system.execution_envelopes"


class PolicyEnginePort(Protocol):
    def decide(
        self,
        principal: Principal,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        *,
        model_risk_hint: RiskLevel | None = None,
    ) -> PolicyDecision: ...


def _to_db(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        task_repository: TaskRepository,
        lease_service: LeaseService,
        policy_engine: PolicyEnginePort,
        executor: ToolExecutor,
        principal_resolver: Callable[[str], Principal],
        context_factory: Callable[[ExecutionEnvelope, Task], ToolContext],
        *,
        settings: Settings,
        event_repository: TaskRepository | None = None,
        approval_service: ApprovalService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._registry = registry
        self._task_repository = task_repository
        self._lease_service = lease_service
        self._policy_engine = policy_engine
        self._executor = executor
        self._principal_resolver = principal_resolver
        self._context_factory = context_factory
        self._settings = settings
        self._events = event_repository or task_repository
        # 未装配审批服务时，R2 一律 APPROVAL_REQUIRED（fail closed，绝不放行）。
        self._approval_service = approval_service
        self._audit = audit or AuditService(self._events)

    # ---- public --------------------------------------------------------
    def execute(
        self,
        envelope: ExecutionEnvelope,
        params: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ToolResult:
        current = _normalize_utc(now or utc_now())

        # 1) 签名、有效期、时钟偏差与 nonce 完整性。
        verify_envelope(envelope, self._settings.gateway_key(), now=current)

        # 2) nonce 唯一登记（先占位，重放必然失败）。
        self._record_envelope(envelope, current)

        # 3) 租约与 fencing token 由控制库权威校验。
        lease = self._lease_service.verify(
            envelope.lease_id,
            envelope.task_id,
            envelope.worker_id,
            envelope.fencing_token,
        )

        # 4) 任务读取限定为租约租户，跨租户按不存在处理。
        task = self._task_repository.get_task(envelope.task_id, tenant_id=lease.tenant_id)

        # 5) 权威状态版本与环境必须与信封一致。
        if task.state_version != envelope.state_version:
            raise StateConflictError(
                "信封状态版本已过期",
                details={
                    "task_id": task.task_id,
                    "expected": envelope.state_version,
                    "actual": task.state_version,
                },
            )
        if task.environment_id != envelope.environment_id:
            raise EnvelopeInvalidError(
                "信封环境与任务环境不一致",
                details={
                    "task_id": task.task_id,
                    "envelope_environment_id": envelope.environment_id,
                    "task_environment_id": task.environment_id,
                },
            )

        # 6) 工具注册、版本与入参摘要必须匹配。
        tool = self._registry.get(envelope.tool_name)
        definition: ToolDefinition = getattr(tool, "definition")  # type: ignore[assignment]
        if definition.version != envelope.tool_version:
            raise EnvelopeInvalidError(
                "信封工具版本与注册表不一致",
                details={
                    "tool_name": envelope.tool_name,
                    "envelope_version": envelope.tool_version,
                    "registry_version": definition.version,
                },
            )
        if compute_args_digest(params) != envelope.args_digest:
            raise EnvelopeInvalidError(
                "工具入参摘要与信封不一致",
                details={
                    "tool_name": envelope.tool_name,
                    "task_id": task.task_id,
                },
            )

        # 7) 服务端构造 Worker 主体与请求者主体。
        worker_principal = Principal(
            user_id=f"worker:{envelope.worker_id}",
            tenant_id=lease.tenant_id,
            roles=(Role.SERVICE_WORKER,),
            auth_method=AuthMethod.SERVICE,
        )
        if not worker_principal.has_permission(Permission.GATEWAY_EXECUTE):
            raise ForbiddenError(
                "Worker 主体缺少 Gateway 执行权限",
                details={"worker_id": envelope.worker_id},
            )

        actor = self._principal_resolver(envelope.actor_user_id)
        if actor.tenant_id != lease.tenant_id:
            raise ForbiddenError(
                "请求者租户与租约租户不一致",
                details={"actor_user_id": actor.user_id},
            )

        # 8) 服务端环境事实与策略决策（环境状态来自控制库，未知失败关闭）。
        environment_row = self._task_repository.get_environment(
            task.environment_id, tenant_id=lease.tenant_id
        )
        if environment_row is None:
            raise EnvelopeInvalidError(
                "任务环境不存在或不属于租户",
                details={
                    "task_id": task.task_id,
                    "environment_id": task.environment_id,
                },
            )
        db_engine = str(environment_row.get("db_engine") or "")
        if db_engine not in {"mysql", "postgresql"}:
            db_engine = definition.db_engines[0] if definition.db_engines else "mysql"
        environment = EnvironmentContext(
            environment_id=task.environment_id,
            tenant_id=lease.tenant_id,
            db_engine=db_engine,  # type: ignore[arg-type]
            tier=environment_tier(environment_row.get("status")),
            db_version=str(environment_row.get("db_version") or "") or None,
        )
        decision = self._policy_engine.decide(
            actor,
            definition,
            environment,
            model_risk_hint=envelope.model_risk_hint,
        )
        self._emit(
            AuditEventType.POLICY_DECISION,
            envelope,
            task,
            {
                "allowed": decision.allowed,
                "effect": "ALLOW" if decision.allowed else "DENY",
                "effective_risk": decision.effective_risk,
                "requested_risk": definition.risk_level,
                "required_approval": decision.required_approval,
                "policy_version": decision.policy_version,
                "reason": decision.reason,
                "deny_code": decision.deny_code,
                "worker_id": envelope.worker_id,
                "envelope_id": envelope.envelope_id,
                "tool_name": envelope.tool_name,
                "tool_version": envelope.tool_version,
                "environment_id": environment.environment_id,
                "actor_user_id": envelope.actor_user_id,
                "nonce_prefix": envelope.nonce[:8],
            },
        )

        # 8a) P2-T3：写操作的 dry-run/爆炸半径守卫。放在审批消费**之前**，
        # 但只在"审批当前仍可用"时执行：若审批已消费/已撤销，直接落到下面的
        # 消费路径，由它给出权威的 APPROVAL_ALREADY_CONSUMED（KD-1 的信号优先）。
        denial = self._preflight_denial(envelope, definition, params, task, current)
        if denial is not None:
            return denial

        grant: ApprovalGrant | None = None
        if decision.required_approval:
            # 8b) R2：策略永不放行；必须先消费一次性审批，失败即拒绝且不调用工具。
            approval_id = envelope.approval_id
            if not approval_id:
                return self._denied(
                    envelope,
                    task,
                    environment,
                    current,
                    deny_code="APPROVAL_REQUIRED",
                    message="该操作需要人工审批：执行信封缺少 approval_id",
                    details={
                        "tool_name": envelope.tool_name,
                        "required_approval": True,
                        "policy_version": decision.policy_version,
                    },
                )
            if self._approval_service is None:
                return self._denied(
                    envelope,
                    task,
                    environment,
                    current,
                    deny_code="APPROVAL_REQUIRED",
                    message="审批服务未装配，拒绝执行需审批的操作",
                    details={
                        "tool_name": envelope.tool_name,
                        "approval_id": approval_id,
                    },
                )
            try:
                grant = self._approval_service.consume(
                    approval_id=approval_id,
                    principal=worker_principal,
                    task=task,
                    definition=definition,
                    params=params,
                    environment_row=environment_row,
                    policy_version=decision.policy_version,
                    envelope_id=envelope.envelope_id,
                    tool_call_id=None,
                )
            except AuditUnavailableError:
                # 审计不可用必须向上传播（需告警且必须阻断）。
                raise
            except DBGuardianError as exc:
                return self._denied(
                    envelope,
                    task,
                    environment,
                    current,
                    deny_code=exc.code.value,
                    message=exc.message,
                    details={
                        "tool_name": envelope.tool_name,
                        "approval_id": approval_id,
                        **exc.details,
                    },
                )

            # 8c) STEP_PREPARED 审计门禁：写不进去就不得执行（fail closed）。
            self._audit.emit_required(
                make_event(
                    AuditEventType.STEP_PREPARED,
                    task_id=task.task_id,
                    run_id=task.run_id,
                    payload={
                        "approval_id": grant.approval_id,
                        "action_digest": grant.action_digest,
                        "prerequisites_digest": grant.prerequisites_digest,
                        "tool_name": definition.name,
                        "tool_version": definition.version,
                        "environment_id": environment.environment_id,
                        "policy_version": grant.policy_version,
                        "envelope_id": envelope.envelope_id,
                        "worker_id": envelope.worker_id,
                        "lease_id": lease.lease_id,
                        "fencing_token": lease.fencing_token,
                        "state_version": envelope.state_version,
                        "tenant_id": lease.tenant_id,
                        "actor_user_id": envelope.actor_user_id,
                    },
                )
            )
        elif not decision.allowed:
            deny_code = decision.deny_code or "POLICY_DENIED"
            details = {
                "effective_risk": decision.effective_risk,
                "required_approval": decision.required_approval,
                "policy_version": decision.policy_version,
                "tool_name": envelope.tool_name,
                "reason": decision.reason,
            }
            self._emit(
                AuditEventType.GATEWAY_EXECUTION_DENIED,
                envelope,
                task,
                {
                    "deny_code": deny_code,
                    "worker_id": envelope.worker_id,
                    "envelope_id": envelope.envelope_id,
                    "tool_name": envelope.tool_name,
                    "actor_user_id": envelope.actor_user_id,
                    "nonce_prefix": envelope.nonce[:8],
                    **details,
                },
            )
            return ToolResult(
                tool_name=envelope.tool_name,
                status=ToolStatus.DENIED,
                task_id=task.task_id,
                run_id=task.run_id,
                tool_call_id=None,
                started_at=current.isoformat(),
                duration_ms=0.0,
                data={},
                warnings=[],
                error=ToolError(code=deny_code, message=decision.reason, details=details),
                source=ToolSource(
                    database_id=environment.environment_id,
                    engine=environment.db_engine,
                    db_version=environment.db_version or "unknown",
                    collected_at=current.isoformat(),
                ),
            )

        # 9) 允许后写审计事件，再由 Executor 执行（唯一的工具执行路径）。
        self._emit(
            AuditEventType.GATEWAY_EXECUTION_ALLOWED,
            envelope,
            task,
            {
                "worker_id": envelope.worker_id,
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "envelope_id": envelope.envelope_id,
                "correlation_id": envelope.correlation_id,
                "state_version": envelope.state_version,
                "tool_name": envelope.tool_name,
                "tool_version": envelope.tool_version,
                "effective_risk": decision.effective_risk,
                "policy_version": decision.policy_version,
                "actor_user_id": envelope.actor_user_id,
                "nonce_prefix": envelope.nonce[:8],
            },
        )
        result = self._executor.execute(
            envelope.tool_name,
            params,
            self._context_factory(envelope, task),
            task=task,
            step_id=envelope.step_id,
            run_id=task.run_id,
            tool_call_id=None,
            # 一张审批只允许一次写：审批 id 即最贴切的幂等键。
            idempotency_key=f"apr:{grant.approval_id}" if grant is not None else None,
            approval_grant=grant,
        )
        # 结果未知不得等待外部 sweep：唯一执行链上立即收敛，且绝不重试工具。
        self._converge_unknown_outcome(task, result, envelope)
        return result

    def _converge_unknown_outcome(
        self, task: Task, result: ToolResult, envelope: ExecutionEnvelope
    ) -> None:
        """结果未知立即迁移任务状态，不依赖外部 sweep；绝不重试工具（P0-18）。

        与 Reconciler 的分工：这里只做"即时收敛"并留一条状态变更事件；
        `RECONCILIATION_REQUIRED` 仍由 Reconciler 对"已 Prepared 未 Observed"
        的调用统一落账，两者不重复、也不互相替代。
        """
        if result.status is not ToolStatus.UNKNOWN_OUTCOME:
            return
        try:
            current = self._task_repository.get_task(task.task_id, tenant_id=task.tenant_id)
        except DBGuardianError:
            current = task
        if current.status is TaskStatus.NEEDS_RECONCILIATION:
            return
        if not can_transition(current.status, TaskStatus.NEEDS_RECONCILIATION):
            return
        before = current.status
        try:
            self._task_repository.update_task_status(
                current.task_id,
                TaskStatus.NEEDS_RECONCILIATION,
                expected_state_version=current.state_version,
                error_code="UNKNOWN_OUTCOME",
                error_message="工具结果未知，禁止自动重试，需核对真实执行结果",
            )
        except StateConflictError:
            # 并发已推进：以控制库最新状态为准，不覆盖、不重放。
            return
        self._emit(
            AuditEventType.TASK_STATE_CHANGED,
            envelope,
            task,
            {
                "from": before.value,
                "to": TaskStatus.NEEDS_RECONCILIATION.value,
                "reason": "UNKNOWN_OUTCOME",
                "envelope_id": envelope.envelope_id,
                "tool_name": envelope.tool_name,
                "actor_user_id": envelope.actor_user_id,
                "source": "gateway_immediate",
            },
        )

    def _preflight_denial(
        self,
        envelope: "ExecutionEnvelope",
        definition: Any,
        params: dict[str, Any],
        task: Any,
        current: datetime,
    ) -> ToolResult | None:
        """写操作执行前的 dry-run 判定；返回 None 表示放行。

        规则（P2-T3）：
        - 只有 `side_effect == "write"` 且 `preflight_required=True` 才检查；
        - 工具必须实现 `preflight(context, params) -> PreflightEstimate`，缺失即 fail closed；
        - `affected_rows == 0` → `PREFLIGHT_NO_TARGET`（没有可改的行，不执行）；
        - `affected_rows > definition.max_affected_rows` → `BLAST_RADIUS_EXCEEDED`。
        拒绝时发 `GATEWAY_EXECUTION_DENIED` 审计，且**不消耗审批、不调用工具**。
        """
        if definition.side_effect != "write" or not bool(
            getattr(definition, "preflight_required", False)
        ):
            return None

        # 只有"仍可消费"的审批才做预检（已批准 = 可消费）；已消费/撤销/过期时
        # 让消费路径给出权威拒绝码（KD-1 优先于爆炸半径）。
        approval_id = getattr(envelope, "approval_id", None)
        if approval_id and self._approval_service is not None:
            try:
                detail = self._approval_service.get(approval_id, tenant_id=task.tenant_id)
            except Exception:  # noqa: BLE001 - 读取失败不改变消费路径的判定
                return None
            raw_status = getattr(detail, "status", "")
            status_value = str(getattr(raw_status, "value", raw_status) or "")
            if status_value != "APPROVED":
                return None

        tool = self._registry.get(envelope.tool_name)
        preflight = getattr(tool, "preflight", None)
        if preflight is None:
            return self._preflight_denied(
                envelope, task, current, "PREFLIGHT_UNAVAILABLE", "工具未实现 pre-flight"
            )
        try:
            estimate = preflight(self._context_factory(envelope, task), params)
        except Exception as exc:  # noqa: BLE001 - 预检失败即 fail closed，不猜测
            return self._preflight_denied(
                envelope,
                task,
                current,
                "PREFLIGHT_FAILED",
                f"pre-flight 执行失败：{type(exc).__name__}",
            )
        if not isinstance(estimate, PreflightEstimate):
            return self._preflight_denied(
                envelope, task, current, "PREFLIGHT_UNAVAILABLE", "pre-flight 返回值非法"
            )

        limit = int(getattr(definition, "max_affected_rows", 1))
        if estimate.affected_rows == 0:
            return self._preflight_denied(
                envelope,
                task,
                current,
                "PREFLIGHT_NO_TARGET",
                f"pre-flight 命中 0 行：{estimate.detail}",
                affected_rows=0,
                max_affected_rows=limit,
            )
        if estimate.affected_rows > limit:
            return self._preflight_denied(
                envelope,
                task,
                current,
                "BLAST_RADIUS_EXCEEDED",
                f"预计影响 {estimate.affected_rows} 行，超过上限 {limit}",
                affected_rows=estimate.affected_rows,
                max_affected_rows=limit,
            )
        return None

    def _preflight_denied(
        self,
        envelope: "ExecutionEnvelope",
        task: Any,
        current: datetime,
        reason: str,
        message: str,
        **extra: Any,
    ) -> ToolResult:
        details = {
            "tool_name": envelope.tool_name,
            "task_id": task.task_id,
            "reason": reason,
            "policy_version": "preflight-v1",
            **extra,
        }
        self._emit(
            AuditEventType.GATEWAY_EXECUTION_DENIED,
            envelope,
            task,
            {
                "deny_code": reason,
                "worker_id": envelope.worker_id,
                "envelope_id": envelope.envelope_id,
                "tool_name": envelope.tool_name,
                "actor_user_id": envelope.actor_user_id,
                "nonce_prefix": envelope.nonce[:8],
                **details,
            },
        )
        return ToolResult(
            tool_name=envelope.tool_name,
            status=ToolStatus.DENIED,
            task_id=task.task_id,
            run_id=task.run_id,
            tool_call_id=None,
            started_at=current.isoformat(),
            duration_ms=0.0,
            data={},
            warnings=[],
            error=ToolError(code=reason, message=message, details=details),
            source=ToolSource(
                database_id=task.environment_id,
                engine="mysql",
                db_version="unknown",
                collected_at=current.isoformat(),
            ),
        )

    def _denied(
        self,
        envelope: ExecutionEnvelope,
        task: Task,
        environment: EnvironmentContext,
        current: datetime,
        *,
        deny_code: str,
        message: str,
        details: dict[str, Any],
    ) -> ToolResult:
        """审批/审计类拒绝：写审计事件并返回结构化 `ToolResult(DENIED)`。"""
        self._emit(
            AuditEventType.GATEWAY_EXECUTION_DENIED,
            envelope,
            task,
            {
                "deny_code": deny_code,
                "worker_id": envelope.worker_id,
                "envelope_id": envelope.envelope_id,
                "tool_name": envelope.tool_name,
                "actor_user_id": envelope.actor_user_id,
                "nonce_prefix": envelope.nonce[:8],
                **details,
            },
        )
        return ToolResult(
            tool_name=envelope.tool_name,
            status=ToolStatus.DENIED,
            task_id=task.task_id,
            run_id=task.run_id,
            tool_call_id=None,
            started_at=current.isoformat(),
            duration_ms=0.0,
            data={},
            warnings=[],
            error=ToolError(code=deny_code, message=message, details=details),
            source=ToolSource(
                database_id=environment.environment_id,
                engine=environment.db_engine,
                db_version=environment.db_version or "unknown",
                collected_at=current.isoformat(),
            ),
        )

    # ---- internals -----------------------------------------------------
    def _record_envelope(self, envelope: ExecutionEnvelope, now: datetime) -> None:
        nonce_hash = hashlib.sha256(envelope.nonce.encode("utf-8")).hexdigest()
        try:
            self._task_repository.client.execute(
                f"INSERT INTO {ENVELOPES} (envelope_id, task_id, step_id, worker_id, "
                "lease_id, fencing_token, state_version, correlation_id, actor_user_id, "
                "tool_name, tool_version, environment_id, args_digest, nonce_hash, "
                "issued_at, expires_at, signature, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    envelope.envelope_id,
                    envelope.task_id,
                    envelope.step_id,
                    envelope.worker_id,
                    envelope.lease_id,
                    envelope.fencing_token,
                    envelope.state_version,
                    envelope.correlation_id,
                    envelope.actor_user_id,
                    envelope.tool_name,
                    envelope.tool_version,
                    envelope.environment_id,
                    envelope.args_digest,
                    nonce_hash,
                    _to_db(envelope.issued_at),
                    _to_db(envelope.expires_at),
                    envelope.signature,
                    _to_db(now),
                ),
            )
        except DatabaseQueryError as exc:
            if exc.details.get("mysql_error_code") == 1062:
                raise NonceReplayError(
                    "执行信封 nonce 已被使用",
                    details={
                        "envelope_id": envelope.envelope_id,
                        "task_id": envelope.task_id,
                        "nonce_prefix": envelope.nonce[:8],
                    },
                ) from None
            if exc.details.get("mysql_error_code") == 1452:
                # 外键失败说明 lease/task 不存在或不属于该信封，按租约无效拒绝。
                raise WorkerLeaseInvalidError(
                    "信封引用的任务或租约不存在",
                    details={
                        "envelope_id": envelope.envelope_id,
                        "task_id": envelope.task_id,
                        "lease_id": envelope.lease_id,
                        "nonce_prefix": envelope.nonce[:8],
                    },
                ) from None
            # 任何其他登记失败都必须 fail closed，绝不继续执行。
            raise

    def _emit(
        self,
        event_type: AuditEventType,
        envelope: ExecutionEnvelope,
        task: Task,
        payload: dict[str, Any],
    ) -> None:
        self._events.insert_event(
            make_event(
                event_type,
                task_id=envelope.task_id,
                run_id=task.run_id,
                tool_call_id=None,
                payload=payload,
            )
        )


__all__ = ["PolicyEnginePort", "ToolGateway"]
