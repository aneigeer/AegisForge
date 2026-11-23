"""审批用例（W06 D2）：创建 / 批准 / 拒绝 / 撤销 / 过期 / **原子消费**。

设计约束：
- 消费时**重新计算**全部摘要与指纹，不信任登记时刻写入的值（P0-04）；
- 九项校验的前 8 项只读不持锁，第 9 项进控制库单事务（`SELECT ... FOR UPDATE`
  → 复核状态 → `INSERT approval_consumptions` → 条件 `UPDATE ... WHERE status='APPROVED'`
  → `COMMIT`）；单次消费由 `uq_consumption_approval`/`uq_consumption_nonce` 兜底；
- `ApprovalGrant` 只能由本模块 `consume()` 用模块私有 seal 构造；
- 审批事件一律 `emit_required`：写不进审计就失败（不留"无审计的审批"）。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from core.approval.digest import (
    build_action_descriptor,
    build_normalized_operation,
    compute_action_digest,
    compute_prerequisites_digest,
    environment_fingerprint_digest,
    environment_fingerprint_from_row,
    extract_target_objects,
    normalize_target_objects,
    normalize_tool_parameters,
)
from core.approval.disclosure import (
    DISCLOSURE_TAMPERED,
    DISCLOSURE_VERSION,
    build_disclosure,
    disclosure_digest,
    verify_disclosure,
)
from core.approval.models import (
    DECIDABLE_STATUSES,
    REVOCABLE_STATUSES,
    Approval,
    ApprovalDetail,
    ApprovalGrant,
    ApprovalStatus,
    PrerequisiteCheck,
    ensure_approval_transition,
    ensure_segregation_of_duty,
    error_for_status,
    issue_grant,
)
from core.approval.prerequisites import (
    PrerequisiteProbe,
    blocking_checks,
    merge_with_stored_observations,
)
from core.approval.repository import ApprovalRepository, duplicate_key
from core.approval.state_semantics import derive_state, evidence_from_mapping
from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.config import Settings
from core.execution.step_records import StepRecordRepository
from core.logging import get_logger
from core.security.canonical import CanonicalizationError
from core.security.policy import RiskResolver
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalDigestMismatchError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalPrerequisiteFailedError,
    DBGuardianError,
    DatabaseQueryError,
    DisclosureIncompleteError,
    ErrorCode,
    ForbiddenError,
    RequestInvalidError,
    StateConflictError,
    StateInvalidError,
    ToolNotFoundError,
)
from core.security.canonical import CanonicalizationError, canonical_digest, sha256_hex
from core.security.models import EnvironmentContext, Principal
from core.security.roles import Permission
from core.tasks.models import TERMINAL_STATUSES, Task, new_id, utc_now
from core.tasks.repository import TaskRepository
from core.tools.contracts import ToolDefinition
from core.tools.registry import ToolRegistry
from database.mysql.transaction import transaction

logger = get_logger(__name__)

# 审批单状态列的 ID 前缀：apr / acn / spr / sob（沿用 new_id(prefix) 约定）
APPROVAL_ID_PREFIX = "apr"
CONSUMPTION_ID_PREFIX = "acn"

MAX_DECISION_REASON = 512
MAX_OPERATOR_NOTE = 2000

# W09 T03 / W08 KD-1：与 core.verification.compensation 中的同名原因保持**同值**，
# 但本模块刻意不导入 core.verification.*（避免 approval → verification 反向依赖）；
# 漂移由 tests/unit/test_w09_compensation_denied.py 的同值断言兜住。
COMPENSATION_REUSES_ORIGINAL_APPROVAL = "COMPENSATION_REUSES_ORIGINAL_APPROVAL"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ApprovalService:
    """审批事实的唯一写入方；对外只暴露只读视图与受控状态迁移。"""

    def __init__(
        self,
        repository: ApprovalRepository,
        task_repository: TaskRepository,
        probe: PrerequisiteProbe,
        audit: AuditService,
        *,
        settings: Settings,
        registry: ToolRegistry | None = None,
        risk_resolver: RiskResolver | None = None,
        step_records: StepRecordRepository | None = None,
    ) -> None:
        self._repository = repository
        self._tasks = task_repository
        self._probe = probe
        self._audit = audit
        self._settings = settings
        self._registry = registry
        # W07：风险解析独立于策略引擎——`PolicyEngine.decide()` 对 R2 直接 deny，
        # 语义不同；披露需要的是"解析出的有效风险 + 因子"，不是放行/拒绝结论。
        self._risk = risk_resolver or RiskResolver()
        # W07 T5：状态语义的证据端口（未装配时 CONSUMED 一律判"结果未知"并 degraded）。
        self._step_records = step_records
        if probe.policy_version is None:
            probe.bind_policy_version(settings.policy_version)

    @property
    def repository(self) -> ApprovalRepository:
        return self._repository

    # ---- 创建 ----------------------------------------------------------
    def create(
        self,
        *,
        principal: Principal,
        task_id: str,
        step_id: str | None,
        tool_name: str,
        params: Mapping[str, Any] | None,
        target_objects: Sequence[Mapping[str, Any]],
        operator_note: str | None = None,
        ttl_s: float | None = None,
        compensation_of: str | None = None,
    ) -> ApprovalDetail:
        """创建待批审批单；任一阻断条件命中即拒绝，不产生 PENDING 行。

        `compensation_of`（W08）：标记"本审批是某个已执行动作的补偿"。
        它只在插入时写一次（唯一键 `uq_approvals_compensation_of`），
        **不进 `action_digest`**，也不参与任何状态迁移字段集。
        """
        self._require_permission(principal, Permission.APPROVAL_CREATE)
        task = self._tasks.get_task(task_id, tenant_id=principal.tenant_id)
        if task.status in TERMINAL_STATUSES:
            raise StateConflictError(
                "任务已结束，禁止建立新的审批",
                details={"task_id": task_id, "status": task.status.value},
            )
        environment_row = self._tasks.get_environment(
            task.environment_id, tenant_id=task.tenant_id
        )
        if environment_row is None:
            raise StateInvalidError(
                "任务所属环境不存在，禁止建立审批",
                details={"task_id": task_id, "environment_id": task.environment_id},
            )
        if str(environment_row.get("status") or "").strip().upper() != "LAB":
            # 不得为 PROD 建立任何待批单。
            raise ForbiddenError(
                "仅 LAB 环境允许建立审批",
                details={
                    "task_id": task_id,
                    "environment_id": task.environment_id,
                    "reason": "ENVIRONMENT_NOT_LAB",
                },
            )

        definition = self.resolve_definition(tool_name)
        db_engine = str(environment_row.get("db_engine") or "")
        if db_engine not in definition.db_engines:
            raise RequestInvalidError(
                "工具不支持当前环境的数据库引擎",
                details={
                    "tool_name": definition.name,
                    "db_engine": db_engine,
                    "supported": list(definition.db_engines),
                },
            )

        normalized_parameters = normalize_tool_parameters(definition, params)
        # 显式传入的目标对象（测试桩/迁移路径）优先；否则按工具契约声明从已规范化
        # 参数中抽取，保证"参数里的目标"与"摘要里的目标"同源（P0-03b）。
        targets = normalize_target_objects(target_objects) or extract_target_objects(
            definition, normalized_parameters
        )
        if definition.side_effect == "write" and not targets:
            # P0-03b：写工具未声明或未给出目标对象即 fail closed，不存在"声明缺失即可绕过"。
            raise ApprovalPrerequisiteFailedError(
                "写操作未声明或未给出 target_objects，禁止建立审批",
                details={"tool_name": definition.name, "reason": "TARGET_OBJECTS_UNDECLARED"},
            )

        fingerprint = environment_fingerprint_from_row(environment_row)
        fingerprint_digest = environment_fingerprint_digest(fingerprint)
        environment = self._environment_context(task, environment_row)
        checks = self._probe.probe(
            definition=definition,
            environment=environment,
            target_objects=targets,
            actor=principal,
            params=normalized_parameters,
        )
        blocking = blocking_checks(checks)
        if blocking:
            # PRD P0-W07-02 末条：阻断明细必须**全量**上报，不得只报第一项。
            raise ApprovalPrerequisiteFailedError(
                "前置条件未全部通过，拒绝建立审批",
                details={
                    "tool_name": definition.name,
                    "blocking_checks": [
                        {
                            "check_id": item.get("check_id"),
                            "status": item.get("status"),
                            "observed": dict(item.get("observed") or {}),
                        }
                        for item in blocking
                    ],
                },
            )

        policy_version = self._settings.policy_version
        now = utc_now()
        # W07：披露三节在 create() 时刻生成快照（不进 action_digest，客户端不可提交）。
        resolution = self._risk.resolve_risk(
            definition, environment, model_risk_hint=None
        )
        disclosure = build_disclosure(
            resolution=resolution,
            definition=definition,
            captured_at=now,
            policy_version=policy_version,
        )
        snapshot_digest = disclosure_digest(disclosure)
        operation = build_normalized_operation(definition, targets)
        descriptor = build_action_descriptor(
            tenant_id=task.tenant_id,
            environment_id=task.environment_id,
            db_engine=db_engine,
            fingerprint=fingerprint,
            definition=definition,
            normalized_operation=operation,
            target_objects=targets,
            parameters=normalized_parameters,
            policy_version=policy_version,
            action_digest_version=self._settings.action_digest_version,
        )
        action_digest, parameters_digest, targets_digest = compute_action_digest(descriptor)
        prerequisites_digest = compute_prerequisites_digest(
            action_digest=action_digest, checks=checks
        )

        now = utc_now()
        ttl = self._resolve_ttl(ttl_s)
        nonce = secrets.token_urlsafe(32)
        approval = Approval(
            approval_id=new_id(APPROVAL_ID_PREFIX),
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            step_id=step_id,
            environment_id=task.environment_id,
            db_engine=db_engine,  # type: ignore[arg-type]
            environment_fingerprint=fingerprint,
            environment_fingerprint_digest=fingerprint_digest,
            action_digest=action_digest,
            parameters_digest=parameters_digest,
            target_objects_digest=targets_digest,
            prerequisites_digest=prerequisites_digest,
            prerequisites=[PrerequisiteCheck.model_validate(dict(item)) for item in checks],
            action_descriptor=descriptor,
            policy_version=policy_version,
            tool_name=definition.name,
            tool_version=definition.version,
            requested_by=principal.user_id,
            requested_session_id=principal.session_id,
            authn_context={
                "auth_method": principal.auth_method.value,
                "roles": sorted(role.value for role in principal.roles),
                "session_bound": principal.session_id is not None,
            },
            nonce_hash=sha256_hex(nonce),
            status=ApprovalStatus.PENDING,
            operator_note=(operator_note or None),
            created_at=now,
            expires_at=now + ttl,
            disclosure=disclosure,
            disclosure_digest=snapshot_digest,
            disclosure_version=DISCLOSURE_VERSION,
            disclosed_at=now,
        )
        self._validate_note(approval.operator_note)
        self._repository.insert(approval, compensation_of=compensation_of)
        self._emit(
            AuditEventType.APPROVAL_REQUESTED,
            approval,
            principal=principal,
            run_id=task.run_id,
            payload={
                "status": approval.status.value,
                "action_digest": approval.action_digest,
                "parameters_digest": approval.parameters_digest,
                "target_objects_digest": approval.target_objects_digest,
                "prerequisites_digest": approval.prerequisites_digest,
                "environment_fingerprint": approval.environment_fingerprint,
                "policy_version": approval.policy_version,
                "tool_name": approval.tool_name,
                "tool_version": approval.tool_version,
                "target_object_count": len(targets),
                "expires_at": approval.expires_at.isoformat(),
                "step_id": approval.step_id,
                # W07：披露快照的取证件（只进事件，不进 action_digest）。
                "disclosure_digest": approval.disclosure_digest,
                "disclosure_version": approval.disclosure_version,
                "effective_risk": resolution.effective_risk,
            },
        )
        return approval.to_detail(now=now)

    # ---- 读取 ----------------------------------------------------------
    def get(self, approval_id: str, *, tenant_id: str) -> ApprovalDetail:
        approval = self._repository.get(approval_id, tenant_id=tenant_id)
        return approval.to_detail()

    def list(
        self,
        *,
        tenant_id: str,
        status: ApprovalStatus | None = None,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[ApprovalDetail], str | None]:
        approvals, next_cursor = self._repository.list(
            tenant_id=tenant_id,
            status=status,
            task_id=task_id,
            limit=limit,
            cursor=cursor,
        )
        now = utc_now()
        return [approval.to_detail(now=now) for approval in approvals], next_cursor

    # ---- 裁决 ----------------------------------------------------------
    def approve(
        self, approval_id: str, *, principal: Principal, reason: str | None = None
    ) -> ApprovalDetail:
        """批准：职责分离 + 字段完整性 + 状态白名单 + 未过期，四道门禁。"""
        self._require_permission(principal, Permission.APPROVAL_DECIDE)
        approval = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._ensure_segregation(approval, principal, action="approval:approve")

        now = utc_now()
        detail = approval.to_detail(now=now)
        missing = detail.missing_required_fields()
        if missing:
            # PRD §4 硬规则①：缺任一必需字段即阻止批准（fail closed）。
            # W07：缺披露内容与"状态不允许"严格区分——前者补不了就不能批，
            # 且审批状态保持不变（缺内容不是状态迁移事件）。
            disclosure_missing = [
                item for item in missing if item.startswith("disclosure.")
            ]
            if disclosure_missing:
                raise DisclosureIncompleteError(
                    "审批单缺少必需披露内容，禁止批准",
                    details={
                        "approval_id": approval_id,
                        "missing_fields": missing,
                        "sections": [
                            item.split(".", 1)[1] for item in disclosure_missing
                        ],
                        "reason": "DISCLOSURE_INCOMPLETE",
                    },
                )
            raise StateConflictError(
                "审批单缺少必需披露字段，禁止批准",
                details={"approval_id": approval_id, "missing_fields": missing},
            )
        self._ensure_not_expired(approval, principal, action="approval:approve")

        if approval.status not in DECIDABLE_STATUSES:
            raise error_for_status(approval.status, approval_id=approval_id)

        decision_reason = self._clean_reason(reason, required=False)
        affected = self._repository.transition(
            approval_id,
            expected=[ApprovalStatus.PENDING],
            target=ApprovalStatus.APPROVED,
            fields={
                "approver_id": principal.user_id,
                "approver_session_id": principal.session_id,
                "decided_at": now,
                "decision_reason": decision_reason,
            },
        )
        if affected != 1:
            raise error_for_status(
                self._repository.get(approval_id, tenant_id=principal.tenant_id).status,
                approval_id=approval_id,
            )
        updated = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._emit(
            AuditEventType.APPROVAL_APPROVED,
            updated,
            principal=principal,
            payload={
                "from": ApprovalStatus.PENDING.value,
                "to": ApprovalStatus.APPROVED.value,
                "approver_id": principal.user_id,
                "decision_reason": decision_reason,
                "expires_at": updated.expires_at.isoformat(),
                "action_digest": updated.action_digest,
            },
        )
        return updated.to_detail()

    def reject(self, approval_id: str, *, principal: Principal, reason: str) -> ApprovalDetail:
        """拒绝：理由必填（审计可解释性要求）。"""
        self._require_permission(principal, Permission.APPROVAL_DECIDE)
        approval = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._ensure_segregation(approval, principal, action="approval:reject")
        self._ensure_not_expired(approval, principal, action="approval:reject")
        if approval.status not in DECIDABLE_STATUSES:
            raise error_for_status(approval.status, approval_id=approval_id)

        decision_reason = self._clean_reason(reason, required=True)
        now = utc_now()
        affected = self._repository.transition(
            approval_id,
            expected=[ApprovalStatus.PENDING],
            target=ApprovalStatus.REJECTED,
            fields={
                "approver_id": principal.user_id,
                "approver_session_id": principal.session_id,
                "decided_at": now,
                "decision_reason": decision_reason,
            },
        )
        if affected != 1:
            raise error_for_status(
                self._repository.get(approval_id, tenant_id=principal.tenant_id).status,
                approval_id=approval_id,
            )
        updated = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._emit(
            AuditEventType.APPROVAL_REJECTED,
            updated,
            principal=principal,
            payload={
                "from": ApprovalStatus.PENDING.value,
                "to": ApprovalStatus.REJECTED.value,
                "approver_id": principal.user_id,
                "decision_reason": decision_reason,
            },
        )
        return updated.to_detail()

    def revoke(
        self, approval_id: str, *, principal: Principal, reason: str | None = None
    ) -> ApprovalDetail:
        """撤销：申请人本人或具备 `approval:decide` 的角色；已消费不可撤销。"""
        approval = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        is_requester = approval.requested_by == principal.user_id
        can_decide = principal.has_permission(Permission.APPROVAL_DECIDE)
        if not (is_requester or can_decide):
            raise ForbiddenError(
                "无权撤销该审批",
                details={"approval_id": approval_id, "reason": "REVOKE_NOT_ALLOWED"},
            )
        if approval.status is ApprovalStatus.CONSUMED:
            raise StateConflictError(
                "已消费的审批不可撤销",
                details={
                    "approval_id": approval_id,
                    "status": approval.status.value,
                    "reason": "APPROVAL_ALREADY_CONSUMED",
                },
            )
        ensure_approval_transition(approval.status, ApprovalStatus.REVOKED)
        if approval.status not in REVOCABLE_STATUSES:
            raise StateConflictError(
                "当前状态的审批不可撤销",
                details={"approval_id": approval_id, "status": approval.status.value},
            )

        now = utc_now()
        decision_reason = self._clean_reason(reason, required=False)
        affected = self._repository.transition(
            approval_id,
            expected=[ApprovalStatus.PENDING, ApprovalStatus.APPROVED],
            target=ApprovalStatus.REVOKED,
            fields={
                "revoked_at": now,
                "revoked_by": principal.user_id,
                "decision_reason": decision_reason,
            },
        )
        if affected != 1:
            raise error_for_status(
                self._repository.get(approval_id, tenant_id=principal.tenant_id).status,
                approval_id=approval_id,
            )
        updated = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._emit(
            AuditEventType.APPROVAL_REVOKED,
            updated,
            principal=principal,
            payload={
                "from_status": approval.status.value,
                "to": ApprovalStatus.REVOKED.value,
                "revoked_by": principal.user_id,
                "decision_reason": decision_reason,
                "by_requester": is_requester,
            },
        )
        return updated.to_detail()

    def expire_due(self, *, limit: int = 100) -> int:
        """把到期未处理的审批标记为 EXPIRED；返回实际标记数量。"""
        now = utc_now()
        expired = 0
        for approval in self._repository.list_due_expiry(now=now, limit=limit):
            affected = self._repository.transition(
                approval.approval_id,
                expected=[ApprovalStatus.PENDING, ApprovalStatus.APPROVED],
                target=ApprovalStatus.EXPIRED,
                fields={"decided_at": now, "decision_reason": "TTL_EXPIRED"},
            )
            if affected != 1:
                continue
            expired += 1
            updated = self._repository.get(
                approval.approval_id, tenant_id=approval.tenant_id
            )
            self._emit(
                AuditEventType.APPROVAL_EXPIRED,
                updated,
                principal=None,
                payload={
                    "from_status": approval.status.value,
                    "to": ApprovalStatus.EXPIRED.value,
                    "expires_at": approval.expires_at.isoformat(),
                    "reason": "TTL_EXPIRED",
                },
            )
        return expired

    # ---- 消费 ----------------------------------------------------------
    def consume(
        self,
        *,
        approval_id: str,
        principal: Principal,
        task: Task,
        definition: ToolDefinition,
        params: Mapping[str, Any] | None,
        environment_row: Mapping[str, Any],
        policy_version: str,
        envelope_id: str | None,
        tool_call_id: str | None,
    ) -> ApprovalGrant:
        """九项校验后原子消费；成功返回唯一可用的 `ApprovalGrant`。"""
        now = utc_now()
        # 1) 存在且租户匹配（跨租户按不存在处理）
        approval = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        # 2) 状态必须为 APPROVED
        if approval.status is not ApprovalStatus.APPROVED:
            # W09 T03 / W08 KD-1：只在「已消费」这条拒绝路径上补发一条
            # COMPENSATION_DENIED。事件写在抛错**之前**，用 try_emit 拒绝优先，
            # 审计写失败不改变拒绝判定；不重排、不绕过任何一项 consume() 校验，
            # 也不进 Gateway。
            if approval.status is ApprovalStatus.CONSUMED:
                compensation_approval_id = self._repository.find_compensation_for(
                    approval_id
                )
                if compensation_approval_id is not None:
                    self._emit_compensation_denied(
                        approval,
                        principal=principal,
                        task_run_id=task.run_id,
                        compensation_approval_id=compensation_approval_id,
                    )
            raise error_for_status(approval.status, approval_id=approval_id)
        # 3) 未过期（服务端时间；过期即落 EXPIRED 并拒绝）
        self._ensure_not_expired(approval, principal, action="approval:consume")

        # 3b) 审批记录自洽：展示全文必须能复算出登记的 action_digest（PRD §4 同源）。
        #     快照被具备 UPDATE 权限的账号改写 → 展示内容与摘要不再同源，必须发现。
        try:
            descriptor_digest = canonical_digest(approval.action_descriptor)
        except CanonicalizationError:
            descriptor_digest = None
        if descriptor_digest != approval.action_digest:
            raise self._mismatch(
                "action",
                approval,
                expected=approval.action_digest,
                actual=descriptor_digest,
                actor=principal,
                reason="DESCRIPTOR_DIGEST_MISMATCH",
            )

        # 4) 任务/环境/引擎一致
        db_engine = str(environment_row.get("db_engine") or "")
        if (
            approval.task_id != task.task_id
            or approval.environment_id != task.environment_id
            or approval.db_engine != db_engine
        ):
            raise self._mismatch(
                "environment",
                approval,
                actor=principal,
                expected={
                    "task_id": approval.task_id,
                    "environment_id": approval.environment_id,
                    "db_engine": approval.db_engine,
                },
                actual={
                    "task_id": task.task_id,
                    "environment_id": task.environment_id,
                    "db_engine": db_engine,
                },
            )
        # 5) 重算环境指纹
        fingerprint = environment_fingerprint_from_row(environment_row)
        recomputed_fingerprint = environment_fingerprint_digest(fingerprint)
        if recomputed_fingerprint != approval.environment_fingerprint_digest:
            raise self._mismatch(
                "environment_fingerprint",
                approval,
                actor=principal,
                expected=approval.environment_fingerprint_digest,
                actual=recomputed_fingerprint,
            )
        # 6) 策略版本
        if policy_version != approval.policy_version:
            raise self._mismatch(
                "policy_version",
                approval,
                actor=principal,
                expected=approval.policy_version,
                actual=policy_version,
            )
        # 7) 工具名称 + 版本
        if definition.name != approval.tool_name or definition.version != approval.tool_version:
            raise self._mismatch(
                "tool_version",
                approval,
                actor=principal,
                expected={"name": approval.tool_name, "version": approval.tool_version},
                actual={"name": definition.name, "version": definition.version},
            )

        # 8) 重算参数 → 目标对象 → 整体摘要（先分通道定位，再整体门禁）
        # 规范化/抽取失败（写工具必填字段缺失、契约漂移）不得裸抛 400：
        # 统一转成带 channel 的摘要不符，保证拒绝落 APPROVAL_CONSUME_DENIED。
        try:
            parameters = normalize_tool_parameters(definition, params)
        except CanonicalizationError:
            raise self._mismatch(
                "parameters",
                approval,
                expected=approval.parameters_digest,
                actual="canonicalization_failed",
                actor=principal,
            ) from None
        # 目标对象与其余四条摘要一样，在消费时用**当前契约 + 当前参数**重算：
        # 抽取规则原地漂移（spec 改了但 tool_version 没提升）会让审批失效。
        try:
            extracted_targets = normalize_target_objects(
                extract_target_objects(definition, parameters)
            )
        except CanonicalizationError:
            raise self._mismatch(
                "target_objects",
                approval,
                expected=approval.target_objects_digest,
                actual="canonicalization_failed",
                actor=principal,
            ) from None
        if definition.side_effect == "write" and not extracted_targets:
            # 写操作抽不出目标对象即无法证明"批准的对象 == 执行的对象"，fail closed。
            raise self._mismatch(
                "target_objects",
                approval,
                expected=approval.target_objects,
                actual=[],
                actor=principal,
                reason="EXTRACTED_TARGETS_EMPTY",
            )
        # 契约没有声明抽取规则时（读工具/显式目标对象）回落到登记时的目标对象。
        targets = extracted_targets or approval.target_objects
        operation = build_normalized_operation(definition, targets)
        descriptor = build_action_descriptor(
            tenant_id=approval.tenant_id,
            environment_id=approval.environment_id,
            db_engine=approval.db_engine,
            fingerprint=fingerprint,
            definition=definition,
            normalized_operation=operation,
            target_objects=targets,
            parameters=parameters,
            policy_version=approval.policy_version,
            action_digest_version=str(
                approval.action_descriptor.get("action_digest_version")
                or self._settings.action_digest_version
            ),
        )
        action_digest, parameters_digest, targets_digest = compute_action_digest(descriptor)
        if parameters_digest != approval.parameters_digest:
            raise self._mismatch(
                "parameters",
                approval,
                expected=approval.parameters_digest,
                actual=parameters_digest,
                actor=principal,
            )
        if targets_digest != approval.target_objects_digest:
            raise self._mismatch(
                "target_objects",
                approval,
                actor=principal,
                expected=approval.target_objects_digest,
                actual=targets_digest,
            )
        if action_digest != approval.action_digest:
            raise self._mismatch(
                "action",
                approval,
                expected=approval.action_digest,
                actual=action_digest,
                actor=principal,
            )

        # 9) 重算前置条件 → 原子消费
        # `prepare_required` 工具的目标库观测（threads_running / disk_free / 锁 /
        # 行签名等）每次探测都会漂移，若整体参与摘要比较则任何审批都无法消费。
        # 因此消费路径只重算**控制面**事实，目标库观测沿用审批登记值；"目标行是否
        # 仍是批准时那一行"由 action_digest（含 expected_note 基线）与工具 SQL 的
        # `WHERE note <=> ?` 双重保证（见 `merge_with_stored_observations` 说明）。
        if definition.prepare_required:
            fresh_checks = self._probe.control_plane_checks(
                definition=definition,
                environment=self._environment_context(task, environment_row),
                target_objects=targets,
                actor=principal,
            )
            checks = merge_with_stored_observations(
                fresh_checks,
                [check.to_digest_dict() for check in approval.prerequisites],
            )
        else:
            checks = self._probe.probe(
                definition=definition,
                environment=self._environment_context(task, environment_row),
                target_objects=targets,
                actor=principal,
                params=parameters,
            )
        prerequisites_digest = compute_prerequisites_digest(
            action_digest=approval.action_digest, checks=checks
        )
        if prerequisites_digest != approval.prerequisites_digest:
            raise self._mismatch(
                "prerequisites",
                approval,
                actor=principal,
                expected=approval.prerequisites_digest,
                actual=prerequisites_digest,
            )

        consumed_at = utc_now()
        with transaction(self._repository.client) as conn:
            row = self._repository.lock_row(conn, approval_id, tenant_id=principal.tenant_id)
            if row is None:
                raise ApprovalNotFoundError(
                    "审批不存在", details={"approval_id": approval_id}
                )
            status = ApprovalStatus(str(row["status"]))
            if status is not ApprovalStatus.APPROVED:
                raise error_for_status(status, approval_id=approval_id)
            locked_expires = row.get("expires_at")
            if isinstance(locked_expires, datetime):
                expires_at = _as_utc(locked_expires)
                if consumed_at >= expires_at:
                    # 行锁内发现的过期：本事务回滚，过期标记由 expire_due 落库。
                    raise ApprovalExpiredError(
                        "审批已过期，禁止消费",
                        details={"approval_id": approval_id, "expires_at": expires_at.isoformat()},
                    )
            # 锁内复核：get() 与 lock_row() 之间若有主体改写摘要/身份列，
            # 台账会记旧值而授权对象取新值。逐列比对锁外快照，不一致即阻断。
            for column, expected in (
                ("tenant_id", approval.tenant_id),
                ("task_id", approval.task_id),
                ("nonce_hash", approval.nonce_hash),
                ("action_digest", approval.action_digest),
                ("parameters_digest", approval.parameters_digest),
                ("target_objects_digest", approval.target_objects_digest),
                ("prerequisites_digest", approval.prerequisites_digest),
                ("policy_version", approval.policy_version),
                ("tool_name", approval.tool_name),
                ("tool_version", approval.tool_version),
                # W07：披露快照摘要也参与 TOCTOU 比对（批准后、消费前被改写必须发现）。
                ("disclosure_digest", approval.disclosure_digest),
            ):
                actual = row.get(column)
                if actual is None or str(actual) != str(expected):
                    raise StateConflictError(
                        "审批关键字段在消费期间发生变化，已阻断",
                        details={
                            "approval_id": approval_id,
                            "field": column,
                            "reason": "LOCKED_ROW_DRIFT",
                        },
                    )
            # W07 L3：用**锁内** JSON 重算 `disclosure_digest` 并与列值比对。
            # 这一层覆盖"读取之后、加锁之前"被改写的窗口（L2 只覆盖读取时刻）。
            self._verify_locked_disclosure(approval_id, row)
            try:
                self._repository.insert_consumption(
                    conn,
                    consumption_id=new_id(CONSUMPTION_ID_PREFIX),
                    approval_id=approval_id,
                    tenant_id=str(row["tenant_id"]),
                    task_id=str(row["task_id"]),
                    nonce_hash=str(row["nonce_hash"]),
                    action_digest=str(row["action_digest"]),
                    policy_version=str(row["policy_version"]),
                    consumed_by=principal.user_id,
                    consumed_at=consumed_at,
                    tool_call_id=tool_call_id,
                    envelope_id=envelope_id,
                )
            except DatabaseQueryError as exc:
                if duplicate_key(exc):
                    # 双唯一键（approval_id / nonce_hash）撞键 = 已被消费。
                    raise ApprovalAlreadyConsumedError(
                        "审批已被消费，禁止重复消费",
                        details={"approval_id": approval_id, "reason": "CONSUMPTION_EXISTS"},
                    ) from None
                raise
            affected = self._repository.mark_consumed(
                conn,
                approval_id,
                consumed_by=principal.user_id,
                consumed_at=consumed_at,
            )
            if affected != 1:
                current = self._repository.reread_status(conn, approval_id)
                if current is None:
                    raise ApprovalNotFoundError(
                        "审批不存在", details={"approval_id": approval_id}
                    )
                raise error_for_status(current, approval_id=approval_id)

        updated = self._repository.get(approval_id, tenant_id=principal.tenant_id)
        self._emit(
            AuditEventType.APPROVAL_CONSUMED,
            updated,
            principal=principal,
            run_id=task.run_id,
            payload={
                "from": ApprovalStatus.APPROVED.value,
                "to": ApprovalStatus.CONSUMED.value,
                "consumed_by": principal.user_id,
                "consumed_at": consumed_at.isoformat(),
                "envelope_id": envelope_id,
                "tool_call_id": tool_call_id,
                "action_digest": updated.action_digest,
                "prerequisites_digest": updated.prerequisites_digest,
            },
        )
        return issue_grant(
            approval_id=updated.approval_id,
            tenant_id=updated.tenant_id,
            task_id=updated.task_id,
            step_id=updated.step_id,
            tool_name=updated.tool_name,
            tool_version=updated.tool_version,
            environment_id=updated.environment_id,
            action_digest=updated.action_digest,
            prerequisites_digest=updated.prerequisites_digest,
            policy_version=updated.policy_version,
            consumed_by=principal.user_id,
            consumed_at=consumed_at,
        )

    # ---- 状态语义（W07 D4）---------------------------------------------
    def state_of(self, detail: ApprovalDetail) -> dict[str, Any]:
        """单条审批的状态语义（四态 + 失败/关闭的精确拆分）。"""
        return self.states_of([detail])[detail.approval_id]

    def states_of(
        self, details: Sequence[ApprovalDetail]
    ) -> dict[str, dict[str, Any]]:
        """批量推导状态语义（列表页不产生 N+1 查询）。

        证据端口未装配时不给"已执行"的结论：`CONSUMED` 一律
        `UNKNOWN_OUTCOME` + `degraded=true`（宁可让 SRE 去核对）。
        """
        if not details:
            return {}
        evidence_map: dict[str, Any] = {}
        if self._step_records is not None:
            evidence_map = self._step_records.evidence_for_approvals(
                [detail.approval_id for detail in details]
            )
        return {
            detail.approval_id: derive_state(
                approval_status=detail.status.value,
                evidence=(
                    None
                    if self._step_records is None
                    else evidence_from_mapping(evidence_map.get(detail.approval_id))
                ),
            )
            for detail in details
        }

    # ---- 内部工具 ------------------------------------------------------
    @staticmethod
    def _verify_locked_disclosure(approval_id: str, row: Mapping[str, Any]) -> None:
        """锁内披露快照复核（L3）：用锁内 JSON 重算摘要并与列值比对。

        不一致即 `STATE_CONFLICT` + `reason=DISCLOSURE_TAMPERED`，且**不插入**
        `approval_consumptions`、不迁移状态（异常向上抛出，事务随 `transaction()`
        上下文回滚）。
        """
        raw = row.get("disclosure_json")
        if raw is None:
            disclosure: dict[str, Any] = {}
        elif isinstance(raw, dict):
            disclosure = dict(raw)
        else:
            try:
                loaded = json.loads(str(raw))
            except (TypeError, ValueError):
                raise StateConflictError(
                    "审批披露快照不可解析，禁止消费",
                    details={
                        "approval_id": approval_id,
                        "reason": DISCLOSURE_TAMPERED,
                        "cause": "DISCLOSURE_NOT_JSON",
                    },
                ) from None
            if not isinstance(loaded, dict):
                raise StateConflictError(
                    "审批披露快照结构非法，禁止消费",
                    details={
                        "approval_id": approval_id,
                        "reason": DISCLOSURE_TAMPERED,
                        "cause": "DISCLOSURE_NOT_OBJECT",
                    },
                )
            disclosure = loaded
        digest_value = row.get("disclosure_digest")
        try:
            verify_disclosure(
                disclosure, str(digest_value) if digest_value is not None else None
            )
        except StateConflictError as exc:
            details = dict(exc.details)
            details["approval_id"] = approval_id
            raise StateConflictError(exc.message, details=details) from None

    def resolve_definition(self, tool_name: str) -> ToolDefinition:
        """从注册表解析工具契约；未装配注册表时按工具不存在处理（fail closed）。"""
        if self._registry is None:
            raise ToolNotFoundError(
                "未装配工具注册表，无法解析工具契约",
                details={"tool_name": tool_name},
            )
        tool = self._registry.get(tool_name)
        definition = getattr(tool, "definition", None)
        if not isinstance(definition, ToolDefinition):
            raise ToolNotFoundError(
                "工具缺少契约定义", details={"tool_name": tool_name}
            )
        return definition

    def _resolve_ttl(self, ttl_s: float | None) -> timedelta:
        if ttl_s is None:
            return timedelta(seconds=float(self._settings.approval_ttl_s))
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool):
            raise RequestInvalidError("ttl_s 必须是数字", details={"ttl_s": str(ttl_s)})
        if ttl_s <= 0:
            raise RequestInvalidError("ttl_s 必须大于 0", details={"ttl_s": ttl_s})
        if ttl_s > float(self._settings.approval_max_ttl_s):
            raise RequestInvalidError(
                "ttl_s 超过允许上限",
                details={"ttl_s": ttl_s, "max_ttl_s": self._settings.approval_max_ttl_s},
            )
        return timedelta(seconds=float(ttl_s))

    @staticmethod
    def _clean_reason(reason: str | None, *, required: bool) -> str | None:
        text = (reason or "").strip()
        if not text:
            if required:
                raise RequestInvalidError(
                    "拒绝审批必须填写理由", details={"field": "reason", "required": True}
                )
            return None
        if len(text) > MAX_DECISION_REASON:
            raise RequestInvalidError(
                "理由长度超限",
                details={"field": "reason", "max_length": MAX_DECISION_REASON},
            )
        return text

    @staticmethod
    def _validate_note(note: str | None) -> None:
        if note is not None and len(note) > MAX_OPERATOR_NOTE:
            raise RequestInvalidError(
                "操作备注长度超限",
                details={"field": "operator_note", "max_length": MAX_OPERATOR_NOTE},
            )

    @staticmethod
    def _require_permission(principal: Principal, permission: Permission) -> None:
        if not principal.has_permission(permission):
            raise ForbiddenError(
                "当前主体缺少所需审批权限",
                details={"permission": permission.value, "user_id": principal.user_id},
            )

    def _ensure_segregation(
        self, approval: Approval, principal: Principal, *, action: str
    ) -> None:
        """职责分离：违反即写审计事件并拒绝（P0-09）。"""
        try:
            ensure_segregation_of_duty(
                requested_by=approval.requested_by, approver_id=principal.user_id
            )
        except ForbiddenError as exc:
            self._emit(
                AuditEventType.POLICY_DECISION,
                approval,
                principal=principal,
                payload={
                    "decision": "DENY",
                    "action": action,
                    "deny_code": ErrorCode.FORBIDDEN.value,
                    "reason": "SEGREGATION_OF_DUTY",
                    "requested_by": approval.requested_by,
                    "approver_id": principal.user_id,
                },
            )
            raise exc

    def _ensure_not_expired(
        self, approval: Approval, principal: Principal | None, *, action: str
    ) -> None:
        now = utc_now()
        if now < _as_utc(approval.expires_at):
            return
        affected = self._repository.transition(
            approval.approval_id,
            expected=[ApprovalStatus.PENDING, ApprovalStatus.APPROVED],
            target=ApprovalStatus.EXPIRED,
            fields={"decided_at": now, "decision_reason": "TTL_EXPIRED"},
        )
        if affected == 1:
            expired = self._repository.get(
                approval.approval_id, tenant_id=approval.tenant_id
            )
            self._emit(
                AuditEventType.APPROVAL_EXPIRED,
                expired,
                principal=principal,
                payload={
                    "from_status": approval.status.value,
                    "to": ApprovalStatus.EXPIRED.value,
                    "expires_at": approval.expires_at.isoformat(),
                    "action": action,
                    "reason": "TTL_EXPIRED",
                },
            )
        raise ApprovalExpiredError(
            "审批已过期，禁止继续",
            details={
                "approval_id": approval.approval_id,
                "expires_at": _as_utc(approval.expires_at).isoformat(),
            },
        )

    def _mismatch(
        self,
        channel: str,
        approval: Approval,
        *,
        expected: Any,
        actual: Any,
        actor: Principal | None = None,
        reason: str | None = None,
    ) -> ApprovalDigestMismatchError:
        """摘要不符：`channel` 精确指出是参数、目标对象、环境、策略版本还是工具版本。

        拒绝**先落审计事件**（`APPROVAL_CONSUME_DENIED`），保证"被拒"本身可追溯；
        事件写失败时拒绝照常生效（deny 优先），只降级为结构化告警日志。
        """
        self._emit_consume_denied(
            approval, channel=channel, actor=actor, reason=reason
        )
        details: dict[str, Any] = {
            "approval_id": approval.approval_id,
            "channel": channel,
            "expected": expected,
            "actual": actual,
        }
        if reason is not None:
            details["reason"] = reason
        return ApprovalDigestMismatchError(
            "审批摘要与当前操作不一致，禁止消费",
            details=details,
        )

    def _emit_consume_denied(
        self,
        approval: Approval,
        *,
        channel: str,
        actor: Principal | None,
        reason: str | None = None,
    ) -> None:
        """记录一次消费拒绝（只在此处落一次，Gateway 不再重复写）。"""
        event = make_event(
            AuditEventType.APPROVAL_CONSUME_DENIED,
            task_id=approval.task_id,
            run_id=self._task_run_id(approval.task_id),
            payload={
                "approval_id": approval.approval_id,
                "task_id": approval.task_id,
                "tenant_id": approval.tenant_id,
                "channel": channel,
                "deny_code": ErrorCode.APPROVAL_DIGEST_MISMATCH.value,
                "actor_user_id": actor.user_id if actor else None,
                "trace_id": None,
                "reason": reason,
                "status": approval.status.value,
                "tool_name": approval.tool_name,
                "tool_version": approval.tool_version,
                "action_digest": approval.action_digest,
                "policy_version": approval.policy_version,
            },
        )
        if not self._audit.try_emit(
            event, actor_user_id=actor.user_id if actor else None
        ):
            # 拒绝优先：事件写不进去也不能把"拒绝"变成 500 或放行。
            logger.error(
                "消费拒绝事件写入失败，拒绝仍生效",
                extra={
                    "alert": "AUDIT_WRITE_FAILED",
                    "approval_id": approval.approval_id,
                    "channel": channel,
                    "task_id": approval.task_id,
                },
            )

    def _emit_compensation_denied(
        self,
        approval: Approval,
        *,
        principal: Principal | None,
        task_run_id: str,
        compensation_approval_id: str,
    ) -> None:
        """KD-1：复用原审批驱动补偿被拒时，补发一条可检索的拒绝事件。

        `reason` 用本模块常量（与 verification 侧同值）；`deny_code` 复用
        `APPROVAL_ALREADY_CONSUMED`。只发一条，走 `try_emit`：拒绝优先，
        事件写不进去也不把拒绝改成放行或 500。
        """
        event = make_event(
            AuditEventType.COMPENSATION_DENIED,
            task_id=approval.task_id,
            run_id=task_run_id,
            payload={
                "reason": COMPENSATION_REUSES_ORIGINAL_APPROVAL,
                "deny_code": ErrorCode.APPROVAL_ALREADY_CONSUMED.value,
                "original_approval_id": approval.approval_id,
                "compensation_approval_id": compensation_approval_id,
                "task_id": approval.task_id,
                "tenant_id": approval.tenant_id,
                "status": approval.status.value,
                "actor_user_id": principal.user_id if principal else None,
            },
        )
        if not self._audit.try_emit(
            event, actor_user_id=principal.user_id if principal else None
        ):
            logger.error(
                "补偿拒绝事件写入失败，拒绝仍生效",
                extra={
                    "alert": "AUDIT_WRITE_FAILED",
                    "approval_id": approval.approval_id,
                    "compensation_approval_id": compensation_approval_id,
                    "task_id": approval.task_id,
                },
            )

    def _environment_context(
        self, task: Task, environment_row: Mapping[str, Any]
    ) -> EnvironmentContext:
        status_value = str(environment_row.get("status") or "").strip().upper()
        return EnvironmentContext(
            environment_id=task.environment_id,
            tenant_id=task.tenant_id,
            db_engine=str(environment_row.get("db_engine") or "mysql"),  # type: ignore[arg-type]
            tier="LAB" if status_value == "LAB" else "PROD",
            db_version=(
                str(environment_row.get("db_version"))
                if environment_row.get("db_version") is not None
                else None
            ),
        )

    def _emit(
        self,
        event_type: AuditEventType,
        approval: Approval,
        *,
        principal: Principal | None,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        """审批事件一律走 `emit_required`：写不进审计就失败（fail closed）。"""
        resolved_run_id = run_id or self._task_run_id(approval.task_id)
        event_payload = {
            **payload,
            "approval_id": approval.approval_id,
            "tenant_id": approval.tenant_id,
            "task_id": approval.task_id,
            "actor_user_id": principal.user_id if principal else None,
            "policy_version": approval.policy_version,
            "tool_name": approval.tool_name,
            "tool_version": approval.tool_version,
            "action_digest": approval.action_digest,
        }
        self._audit.emit_required(
            make_event(
                event_type,
                task_id=approval.task_id,
                run_id=resolved_run_id,
                payload=event_payload,
            ),
            actor_user_id=principal.user_id if principal else None,
        )

    def _task_run_id(self, task_id: str) -> str:
        try:
            return self._tasks.get_task(task_id).run_id
        except DBGuardianError:
            # 任务已被清理时仍要能追加审计事件（run_id 仅用于关联，不参与哈希语义）。
            return "run_unknown"


__all__ = [
    "APPROVAL_ID_PREFIX",
    "COMPENSATION_REUSES_ORIGINAL_APPROVAL",
    "CONSUMPTION_ID_PREFIX",
    "ApprovalService",
]
