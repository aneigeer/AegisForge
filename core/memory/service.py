"""W09 T04 记忆用例：入库前脱敏、审核、撤销、过期清单、版本追溯。

设计依据：``docs/w09/02-design.md`` §5.3/§5.5、``docs/w09/03-task-cards.md:474-494``。

- ``record``：只允许 ``SEMANTIC/PROCEDURAL/ENVIRONMENT`` 落库；``WORKING`` /
  ``EPISODIC`` 抛 ``KNOWLEDGE_INVALID``（前两者在 ``working.py`` / ``episodic.py``）。
  ``content`` 先走闸门的 ``EVIDENCE`` 通道复扫（复用 ``RedactionGate.scan_event``，
  未改 ``gate.py``、未新增通道方法），未通过则拒绝入库；``redaction_version`` 落库。
- ``review`` / ``revoke``：**追加** ``revision+1``，原行不改（``dg_ctl`` 无 UPDATE）；
  撤销成功发 ``KNOWLEDGE_REVOKED``。
- ``expire_due``：只读给出应置 ``EXPIRED`` 的清单，不自动改历史行。
- 记忆不授权执行：本类只依赖仓储与审计，不持有 registry / gateway / DSN。

KD-W09-03 如实登记（唯一对外口径来源：``docs/w09/07-pm-ruling.md`` §4.3 / §4.4）：
记忆内容入库前走闸门的 ``EVIDENCE`` 通道复扫（复用 ``RedactionGate.scan_event``），
**不把** ``StepPrepared.request`` / ``TrajectoryRecorder`` 等内部通道原文当作对外
通道输入。本卡不改 ``redact_payload``、不升 ``redaction_version``（保持
``redaction-v2``）、不补冻结回归、不新增 AC；引用
``docs/w09/06-qa-report.md`` §11.5 时必须并列引用 ``docs/w09/07-pm-ruling.md`` §4。
该发现项定性为**已声明的已知限制，不是本轮 FAIL**，W10 再议；对外不得写
「脱敏全链路无死角 / PII 零落盘」。T05/T06 的「未开工、未验证」状态如实保留。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from core.audit.events import AuditEventType, make_event
from core.memory.models import (
    CONFIRMATION_STAGES,
    Confirmation,
    content_digest,
    PERSISTABLE_KINDS,
    KnowledgeEntry,
    KnowledgeInvalidError,
    KnowledgeStatus,
    ValidationLevel,
)
from core.memory.repository import KnowledgeRepository
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionStatus
from core.tasks.models import utc_now

_PASSABLE = frozenset({RedactionStatus.CLEAN, RedactionStatus.REDACTED})


class MemoryService:
    """知识条目的唯一写入方；只追加、不 UPDATE、不授权执行。"""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        gate: RedactionGate,
        audit: Any,
    ) -> None:
        self._repository = repository
        self._gate = gate
        self._audit = audit

    @property
    def repository(self) -> KnowledgeRepository:
        return self._repository

    def record(self, entry: KnowledgeEntry, *, principal: Any) -> KnowledgeEntry:
        """入库前脱敏并落一条 revision；不通过则拒绝入库。"""
        if entry.kind not in PERSISTABLE_KINDS:
            raise KnowledgeInvalidError(
                "WORKING/EPISODIC 不进入 knowledge_entries 落库路径",
                details={
                    "knowledge_id": entry.knowledge_id,
                    "kind": entry.kind.value,
                    "reason": "KIND_NOT_PERSISTABLE",
                },
            )
        scan = self._gate.scan_event(
            dict(entry.content),
            task_id=entry.knowledge_id,
            tenant_id=entry.tenant_id,
        )
        if scan.status not in _PASSABLE:
            raise KnowledgeInvalidError(
                "知识内容未通过 EVIDENCE 通道脱敏复扫",
                details={
                    "knowledge_id": entry.knowledge_id,
                    "status": scan.status.value,
                    "reason": "REDACTION_NOT_PASSABLE",
                },
            )
        stored = self._validated(
            {
                **entry.model_dump(),
                "content": dict(scan.payload_redacted),
                "redaction_version": scan.redaction_version,
                # P3-T1：写入（含内容修订）一律重置确认——签核只对**当时的内容**有效。
                "confirmations": (),
                "content_digest": None,
            }
        )
        self._repository.insert(stored)
        return stored

    def review(
        self,
        knowledge_id: str,
        revision: int,
        *,
        reviewed_by: str,
        validation_level: ValidationLevel,
    ) -> KnowledgeEntry:
        """人工审核：追加 ``revision+1``（``INDEXED`` + 审核人/时间 + 验证等级）。"""
        base = self._require(knowledge_id, revision)
        if not base.is_multi_confirmed:
            raise KnowledgeInvalidError(
                "知识尚未完成两级人工确认，不得晋升 INDEXED",
                details={
                    "knowledge_id": knowledge_id,
                    "reason": "CONFIRMATION_INCOMPLETE",
                    "stages": sorted(base.confirmed_stages),
                    "confirmers": sorted(base.confirmer_ids),
                },
            )
        if not reviewed_by or not reviewed_by.strip():
            raise KnowledgeInvalidError(
                "审核人不能为空",
                details={"knowledge_id": knowledge_id, "reason": "REVIEWED_BY_EMPTY"},
            )
        if validation_level is ValidationLevel.UNVERIFIED:
            raise KnowledgeInvalidError(
                "审核后验证等级不能仍为 UNVERIFIED",
                details={
                    "knowledge_id": knowledge_id,
                    "reason": "VALIDATION_LEVEL_UNVERIFIED",
                },
            )
        indexed = self._append(
            {
                **base.model_dump(),
                "revision": int(base.revision) + 1,
                "status": KnowledgeStatus.INDEXED,
                "reviewed_by": reviewed_by.strip(),
                "reviewed_at": utc_now().isoformat(),
                "validation_level": validation_level,
                "confirmations": base.confirmations,
                "supersedes": knowledge_id,
                "revoked_at": None,
                "revoked_reason": None,
            }
        )
        self._emit_knowledge_event(
            AuditEventType.KNOWLEDGE_INDEXED,
            indexed,
            principal=None,
            extra={"reviewed_by": indexed.reviewed_by},
        )
        return indexed

    def confirm(
        self,
        knowledge_id: str,
        revision: int,
        *,
        stage: int,
        confirmed_by: str,
        principal: Any = None,
    ) -> KnowledgeEntry:
        """记录一级人工确认（``stage`` ∈ {1,2}），追加 ``revision+1``。

        红线（P3-T1）：

        - **同一人不得包办两级**：确认人已出现在任一阶段即拒绝；
        - **顺序**：阶段 2 必须先有阶段 1；
        - **内容一致性**：已有确认的 ``content_digest`` 与当前内容不符（内容被改过）
          时，旧确认自动失效——阶段 2 直接拒绝，要求从阶段 1 重新确认；
        - 状态保持 ``PROPOSED``：只有 ``review()`` 在两级齐备后才晋升 ``INDEXED``。
        """
        if int(stage) not in CONFIRMATION_STAGES:
            raise KnowledgeInvalidError(
                "确认阶段只能是 1 或 2",
                details={"knowledge_id": knowledge_id, "stage": stage, "reason": "STAGE_INVALID"},
            )
        if not confirmed_by or not confirmed_by.strip():
            raise KnowledgeInvalidError(
                "确认人不能为空",
                details={"knowledge_id": knowledge_id, "reason": "CONFIRMED_BY_EMPTY"},
            )
        base = self._require(knowledge_id, revision)
        digest = content_digest(base.content)

        # 内容改动 → 旧签核失效（只保留与当前内容一致的记录）。
        valid = tuple(item for item in base.confirmations if item.content_digest == digest)
        invalidated = len(valid) != len(base.confirmations)

        actor = confirmed_by.strip()
        if actor in {item.confirmed_by for item in valid}:
            raise KnowledgeInvalidError(
                "同一人不得完成两级确认（职责分离）",
                details={
                    "knowledge_id": knowledge_id,
                    "confirmed_by": actor,
                    "reason": "SAME_USER_TWO_STAGES",
                },
            )
        stages = {item.stage for item in valid}
        if int(stage) == 2 and 1 not in stages:
            raise KnowledgeInvalidError(
                "阶段 2 确认前必须完成阶段 1",
                details={
                    "knowledge_id": knowledge_id,
                    "reason": "STAGE_OUT_OF_ORDER",
                    "invalidated": invalidated,
                },
            )
        if int(stage) == 1 and 1 in stages:
            raise KnowledgeInvalidError(
                "阶段 1 已确认，无需重复确认",
                details={"knowledge_id": knowledge_id, "reason": "STAGE_ALREADY_CONFIRMED"},
            )

        record = Confirmation(
            stage=int(stage),  # type: ignore[arg-type]
            confirmed_by=actor,
            confirmed_at=utc_now().isoformat(),
            content_digest=digest,
        )
        updated = self._append(
            {
                **base.model_dump(),
                "revision": int(base.revision) + 1,
                "status": base.status,
                "confirmations": (*valid, record),
                "content_digest": digest,
            }
        )
        self._emit_knowledge_event(
            AuditEventType.KNOWLEDGE_CONFIRMED,
            updated,
            principal=principal,
            extra={
                "stage": int(stage),
                "confirmed_by": actor,
                "confirmers": sorted(updated.confirmer_ids),
                "content_digest": digest,
                "invalidated_previous": invalidated,
            },
        )
        return updated

    def revoke(
        self,
        knowledge_id: str,
        revision: int,
        *,
        reason: str,
        principal: Any,
    ) -> KnowledgeEntry:
        """撤销：追加 ``revision+1``（``REVOKED`` + 时间/原因），原行不改。"""
        if not reason or not reason.strip():
            raise KnowledgeInvalidError(
                "撤销原因不能为空",
                details={"knowledge_id": knowledge_id, "reason": "REVOKE_REASON_EMPTY"},
            )
        base = self._require(knowledge_id, revision)
        revoked = self._append(
            {
                **base.model_dump(),
                "revision": int(base.revision) + 1,
                "status": KnowledgeStatus.REVOKED,
                "revoked_at": utc_now().isoformat(),
                "revoked_reason": reason.strip(),
                "supersedes": knowledge_id,
            }
        )
        self._emit_revoked(revoked, principal=principal)
        return revoked

    def expire_due(self, now: datetime | None = None) -> tuple[str, ...]:
        """只读给出应置 ``EXPIRED`` 的 ``knowledge_id`` 清单（不自动改历史行）。"""
        moment = now if now is not None else utc_now()
        return self._repository.list_expired(moment)

    def list_versions(
        self, knowledge_id: str, *, tenant_id: str
    ) -> tuple[KnowledgeEntry, ...]:
        """返回全部 revision；撤销后仍带 ``revoked_reason`` 可读。"""
        return self._repository.list_versions(knowledge_id, tenant_id=tenant_id)

    # ---- internals -------------------------------------------------------
    def _require(self, knowledge_id: str, revision: int) -> KnowledgeEntry:
        entry = self._repository.get(knowledge_id, revision)
        if entry is None:
            raise KnowledgeInvalidError(
                "知识条目不存在",
                details={
                    "knowledge_id": knowledge_id,
                    "revision": int(revision),
                    "reason": "KNOWLEDGE_NOT_FOUND",
                },
            )
        return entry

    @staticmethod
    def _validated(payload: dict[str, Any]) -> KnowledgeEntry:
        try:
            return KnowledgeEntry.model_validate(payload)
        except ValidationError as exc:
            raise KnowledgeInvalidError(
                "知识条目字段不满足约束",
                details={"reason": "VALIDATION_ERROR", "errors": exc.error_count()},
            ) from exc

    def _append(self, payload: dict[str, Any]) -> KnowledgeEntry:
        entry = self._validated(payload)
        self._repository.insert(entry)
        return entry

    def _emit_knowledge_event(
        self,
        event_type: "AuditEventType",
        entry: KnowledgeEntry,
        *,
        principal: Any,
        extra: dict[str, Any],
    ) -> None:
        event = make_event(
            event_type,
            task_id=f"system:memory:{entry.tenant_id}",
            run_id="memory",
            payload={
                "knowledge_id": entry.knowledge_id,
                "revision": entry.revision,
                "tenant_id": entry.tenant_id,
                "kind": entry.kind.value,
                "status": entry.status.value,
                "actor_user_id": getattr(principal, "user_id", None),
                **extra,
            },
        )
        self._audit.emit_required(
            event, actor_user_id=getattr(principal, "user_id", None)
        )

    def _emit_revoked(self, entry: KnowledgeEntry, *, principal: Any) -> None:
        event = make_event(
            AuditEventType.KNOWLEDGE_REVOKED,
            task_id=f"system:memory:{entry.tenant_id}",
            run_id="memory",
            payload={
                "knowledge_id": entry.knowledge_id,
                "revision": entry.revision,
                "tenant_id": entry.tenant_id,
                "kind": entry.kind.value,
                "status": entry.status.value,
                "revoked_at": entry.revoked_at,
                "revoked_reason": entry.revoked_reason,
                "supersedes": entry.supersedes,
                "actor_user_id": getattr(principal, "user_id", None),
            },
        )
        self._audit.emit_required(
            event, actor_user_id=getattr(principal, "user_id", None)
        )


__all__ = ["MemoryService"]
