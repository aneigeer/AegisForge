"""Evidence 服务：归属校验、payload 哈希、质量约束与报告事实校验。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core.errors import EvidenceInvalidError, TenantInvalidError
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCreate,
    EvidenceQuality,
    ReportFacts,
)
from core.evidence.repository import EvidenceRepository
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tasks.models import new_id
from core.tasks.repository import TaskRepository

_DEGRADED_STATUSES = frozenset(
    {
        CollectionStatus.DENIED,
        CollectionStatus.TIMEOUT,
        CollectionStatus.NOT_SUPPORTED,
    }
)


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256(canonical JSON)：排序键、保留非 ASCII，保证同内容不同键序稳定。"""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvidenceService:
    def __init__(
        self,
        repository: EvidenceRepository,
        task_repository: TaskRepository,
        *,
        redaction_gate: RedactionGate | None = None,
    ) -> None:
        self._repository = repository
        self._task_repository = task_repository
        # W09 闸门②（`docs/w09/02-design.md:254`）：**恒开**，未显式注入时默认构造
        # （引擎无状态、无 DSN）。这里没有 enabled/配置项，不提供"跳过脱敏"路径；
        # 显式注入仅用于附带 audit / quarantine 落点。
        self._redaction = redaction_gate or RedactionGate(DataRedactionEngine())

    @property
    def repository(self) -> EvidenceRepository:
        return self._repository

    @property
    def redaction_gate(self) -> RedactionGate:
        """组合根共享的闸门实例（`evidence_mapping` 应注入同一实例）。

        为什么要共享：`to_evidence_create` 是**原文最后一次露面**的地方（闸门①
        之后 `data` 已脱敏，但 `_payload()` 还要重新组装）。如果这里用另建的默认
        闸门，`REVIEW_REQUIRED` 会在 mapping 处提前抛出而**没有审计事件、没有隔离
        记录** —— 判定正确但留痕丢失。共享实例保证"判定 + 事件 + 隔离"三件事
        永远落在同一个落点上。
        """
        return self._redaction

    def create(self, request: EvidenceCreate) -> Evidence:
        if not self._task_repository.task_belongs_to(request.task_id, request.tenant_id):
            raise TenantInvalidError(
                "任务不存在或不属于当前租户",
                details={"task_id": request.task_id},
            )
        if not self._task_repository.environment_belongs_to(
            request.environment_id, request.tenant_id
        ):
            raise TenantInvalidError(
                "环境不存在或不属于当前租户",
                details={"environment_id": request.environment_id},
            )

        task = self._task_repository.get_task(
            request.task_id, tenant_id=request.tenant_id
        )
        if task.environment_id != request.environment_id:
            raise EvidenceInvalidError(
                "证据环境与任务环境不一致",
                details={
                    "task_id": request.task_id,
                    "task_environment_id": task.environment_id,
                    "environment_id": request.environment_id,
                },
            )

        if (
            request.collection_status in _DEGRADED_STATUSES
            and request.quality == EvidenceQuality.HIGH
        ):
            raise EvidenceInvalidError(
                "证据采集状态不允许标记为 HIGH 质量",
                details={
                    "collection_status": request.collection_status.value,
                    "quality": request.quality.value,
                },
            )

        # W09 闸门②：**哈希计算之前**断言。`status ∉ {CLEAN, REDACTED}` 直接抛
        # `RedactionBlockedError` / `RedactionReviewRequiredError`（且不写任何行），
        # 因此 AC-05 的 "0 新增行" 由结构保证，而不是靠调用方自觉。
        request = self._redaction.for_evidence(
            request, tenant_id=request.tenant_id, task_id=request.task_id
        )

        payload_hash = canonical_payload_hash(request.payload)
        redacted_payload_hash: str | None = None
        if request.redacted_payload is not None:
            redacted_payload_hash = canonical_payload_hash(request.redacted_payload)

        evidence = Evidence(
            evidence_id=new_id("ev"),
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            step_id=request.step_id,
            tool_call_id=request.tool_call_id,
            environment_id=request.environment_id,
            db_engine=request.db_engine,
            db_version=request.db_version,
            adapter_version=request.adapter_version,
            source_tool=request.source_tool,
            observed_at=request.observed_at,
            time_window_start=request.time_window_start,
            time_window_end=request.time_window_end,
            collection_status=request.collection_status,
            quality=request.quality,
            completeness=request.completeness,
            payload_hash=payload_hash,
            redacted_payload_hash=redacted_payload_hash,
            # A2：`source_payload_hash` 是**原文锚点**（引擎给出），只落库，
            # 不参与上面两项哈希计算，更不覆盖 `payload_hash`。
            source_payload_hash=request.source_payload_hash,
            redaction_version=request.redaction_version,
            reproducible=request.reproducible,
            payload=request.payload,
            redacted_payload=request.redacted_payload,
        )
        return self._repository.insert(evidence)

    def get(self, evidence_id: str, tenant_id: str) -> Evidence:
        return self._repository.get(evidence_id, tenant_id)

    def list(
        self,
        task_id: str,
        tenant_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Evidence], str | None]:
        return self._repository.list_by_task(task_id, tenant_id, limit, cursor)

    def latest_observed_at(self, task_id: str, tenant_id: str) -> str | None:
        return self._repository.latest_observed_at(task_id, tenant_id)

    def validate_report_facts(self, facts: ReportFacts) -> list[str]:
        evidence_ids = [
            *facts.supporting_evidence_ids,
            *facts.contradicting_evidence_ids,
        ]
        # missing_evidence 仅记录缺失项，不参与归属校验。
        return self._repository.validate_task_references(
            facts.task_id, facts.tenant_id, evidence_ids
        )
