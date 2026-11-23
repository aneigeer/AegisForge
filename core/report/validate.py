"""W09 T03 报告引用校验与状态推导（AC-09 / AC-10 / AC-12）。

设计依据：`docs/w09/02-design.md` §4.2 / §4.3、`docs/w09/03-task-cards.md:349-355`。

- 跨租户 id 与不存在 id **返回同一个** ``ErrorCode.EVIDENCE_REF_INVALID``，
  不泄漏「别人的证据是否存在」（沿用 ``core/evidence/repository.py:128-137`` 口径）。
- ``environment_id`` 不符记入 ``env_mismatch_ids`` 并把事实等级降为 ``STALE``；
  ``STALE`` 时 ``missing_evidence`` 必须非空，不得静默剔除失效 id 后仍报 ``CONFIRMED``。
- 弱证据（``WEAK_COLLECTION_STATUSES`` 或 ``quality ∈ {LOW, UNKNOWN}``）被引用时
  ``fact_status != CONFIRMED``；弱集合只在 ``core/evidence/models.py`` 定义，本模块复用不改。
- ``ActionState`` 由**证据链阶段**唯一确定：先由 ``derive_chain_stage`` 得到阶段，
  再由 ``_STAGE_TO_ACTION_STATE`` 一一映射，禁止把若干独立布尔量组合出状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Sequence

from core.errors import DBGuardianError, ErrorCode
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceQuality,
    WEAK_COLLECTION_STATUSES,
)
from core.report.models import (
    ActionState,
    FactInputs,
    FactStatus,
    StructuredReport,
    VerificationState,
    derive_fact_status_from_inputs,
)

_WEAK_QUALITIES: frozenset[EvidenceQuality] = frozenset(
    {EvidenceQuality.LOW, EvidenceQuality.UNKNOWN}
)


class ReportEvidencePort(Protocol):
    """证据取回端口；实现见 ``EvidenceRepository.fetch_by_ids``（T02 交付）。"""

    def fetch_by_ids(
        self, tenant_id: str, evidence_ids: Sequence[str]
    ) -> list[Evidence]: ...


class ReportRefLike(Protocol):
    """引用校验所需的最小报告视图（``StructuredReport`` 天然满足）。"""

    tenant_id: str
    task_id: str
    environment_id: str
    supporting_evidence_ids: Sequence[str]
    contradicting_evidence_ids: Sequence[str]
    missing_evidence: Sequence[str]


class EvidenceRefInvalidError(DBGuardianError):
    """引用无效：跨租户与不存在同码，不区分二者（AC-09）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.EVIDENCE_REF_INVALID, message, details=details)


@dataclass(frozen=True)
class EvidenceRefCheck:
    """引用校验结果；``inspect_evidence_refs`` 只记录不抛，供组装器产出 STALE 报告。"""

    fact_status: FactStatus
    missing_evidence: tuple[str, ...]
    weak_ids: tuple[str, ...]
    env_mismatch_ids: tuple[str, ...]
    invalid_ids: tuple[str, ...]
    error_code: ErrorCode | None
    found_evidence: tuple[Evidence, ...]

    def raise_if_invalid(self) -> None:
        """严格模式：存在失效引用即抛 ``EVIDENCE_REF_INVALID``（AC-09）。"""
        if self.invalid_ids:
            raise EvidenceRefInvalidError(
                "报告引用的证据无效",
                details={
                    "code": ErrorCode.EVIDENCE_REF_INVALID.value,
                    "invalid_evidence_ids": list(self.invalid_ids),
                    "missing_evidence": list(self.missing_evidence),
                    "fact_status": self.fact_status.value,
                },
            )


@dataclass(frozen=True)
class _Analysis:
    inputs: FactInputs
    missing_evidence: tuple[str, ...]
    weak_ids: tuple[str, ...]
    env_mismatch_ids: tuple[str, ...]
    invalid_ids: tuple[str, ...]
    found_evidence: tuple[Evidence, ...]


def _referenced_ids(report: ReportRefLike) -> list[str]:
    """支持 + 反证去重保序；``missing_evidence`` 只记录缺失项，不参与归属校验。"""
    return list(
        dict.fromkeys(
            [*report.supporting_evidence_ids, *report.contradicting_evidence_ids]
        )
    )


def _analyse(report: ReportRefLike, rows: Sequence[Evidence]) -> _Analysis:
    """三查：归属（tenant/task）、环境、采集状态与质量；不做任何外部 IO。"""
    found = {item.evidence_id: item for item in rows}
    supporting_set = set(report.supporting_evidence_ids)
    invalid_ids: list[str] = []
    env_mismatch_ids: list[str] = []
    weak_ids: list[str] = []
    denied_ids: list[str] = []
    supporting_rows: list[Evidence] = []

    for evidence_id in _referenced_ids(report):
        row = found.get(evidence_id)
        if (
            row is None
            or row.tenant_id != report.tenant_id
            or row.task_id != report.task_id
        ):
            # 跨租户 / 不存在 / 跨任务一律同一个失效码，不泄漏存在性。
            invalid_ids.append(evidence_id)
            continue
        if row.environment_id != report.environment_id:
            env_mismatch_ids.append(evidence_id)
        if (
            row.collection_status in WEAK_COLLECTION_STATUSES
            or row.quality in _WEAK_QUALITIES
        ):
            weak_ids.append(evidence_id)
        if row.collection_status is CollectionStatus.DENIED:
            denied_ids.append(evidence_id)
        if evidence_id in supporting_set:
            supporting_rows.append(row)

    missing_evidence = tuple(
        dict.fromkeys(
            [*report.missing_evidence, *invalid_ids, *env_mismatch_ids]
        )
    )
    inputs = FactInputs(
        supporting_evidence_ids=tuple(report.supporting_evidence_ids),
        contradicting_evidence_ids=tuple(report.contradicting_evidence_ids),
        missing_evidence=missing_evidence,
        weak_ids=tuple(weak_ids),
        denied_ids=tuple(denied_ids),
        env_mismatch_ids=tuple(env_mismatch_ids),
        all_supporting_denied=bool(supporting_rows)
        and all(
            row.collection_status is CollectionStatus.DENIED
            for row in supporting_rows
        ),
        verification_state=getattr(
            report, "verification_state", VerificationState.NONE
        ),
    )
    return _Analysis(
        inputs=inputs,
        missing_evidence=missing_evidence,
        weak_ids=tuple(weak_ids),
        env_mismatch_ids=tuple(env_mismatch_ids),
        invalid_ids=tuple(invalid_ids),
        found_evidence=tuple(rows),
    )


def inspect_evidence_refs(
    report: ReportRefLike, port: ReportEvidencePort
) -> EvidenceRefCheck:
    """只记录不抛：供 ``ReportBuilder`` 产出带 ``STALE`` / ``missing_evidence`` 的报告。

    注意：本函数**不会**静默剔除失效 id；它们同时进入 ``invalid_ids`` 与
    ``missing_evidence``，事实等级按 §4.2 第 1 条降为 ``STALE``。
    """
    referenced = _referenced_ids(report)
    rows = port.fetch_by_ids(report.tenant_id, referenced) if referenced else []
    analysis = _analyse(report, rows)
    error_code = (
        ErrorCode.EVIDENCE_REF_INVALID if analysis.invalid_ids else None
    )
    return EvidenceRefCheck(
        fact_status=derive_fact_status_from_inputs(analysis.inputs),
        missing_evidence=analysis.missing_evidence,
        weak_ids=analysis.weak_ids,
        env_mismatch_ids=analysis.env_mismatch_ids,
        invalid_ids=analysis.invalid_ids,
        error_code=error_code,
        found_evidence=analysis.found_evidence,
    )


def validate_evidence_refs(
    report: ReportRefLike, port: ReportEvidencePort
) -> EvidenceRefCheck:
    """严格入口：命中失效引用时抛 ``EVIDENCE_REF_INVALID``（AC-09）。"""
    check = inspect_evidence_refs(report, port)
    check.raise_if_invalid()
    return check


def derive_fact_status(
    report: ReportRefLike, evidence: Sequence[Evidence]
) -> FactStatus:
    """按 §4.2 优先级表推导事实等级（不访问数据库）。"""
    return derive_fact_status_from_inputs(_analyse(report, evidence).inputs)


class ActionChainStage(str, Enum):
    """证据链阶段；与 ``ActionState`` 一一对应，不新增第 7 态。"""

    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    NOT_EXECUTED = "NOT_EXECUTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    EXECUTED = "EXECUTED"
    VERIFIED = "VERIFIED"


_STAGE_TO_ACTION_STATE: dict[ActionChainStage, ActionState] = {
    ActionChainStage.PROPOSED: ActionState.PROPOSED,
    ActionChainStage.AWAITING_APPROVAL: ActionState.AWAITING_APPROVAL,
    ActionChainStage.NOT_EXECUTED: ActionState.NOT_EXECUTED,
    ActionChainStage.OUTCOME_UNKNOWN: ActionState.OUTCOME_UNKNOWN,
    ActionChainStage.EXECUTED: ActionState.EXECUTED,
    ActionChainStage.VERIFIED: ActionState.VERIFIED,
}


@dataclass(frozen=True)
class ActionChainObservation:
    """证据链观测事实（由组合根从 approvals / step_* / verification_results 取得）。"""

    approval_status: str | None = None
    has_step_prepared: bool = False
    has_step_observed: bool = False
    verdict: str | None = None
    verification_result_id: str | None = None


def derive_chain_stage(observation: ActionChainObservation) -> ActionChainStage:
    """由观测事实**唯一确定**一个阶段（设计 §4.2 表，自上而下首个命中）。

    这里只判定「阶段」这一个量；``ActionState`` 由阶段一一映射得到，
    不把若干独立布尔量拼成状态，也不允许 ``UNKNOWN`` 落到 ``EXECUTED``。
    """
    if observation.verdict == "PASS" and observation.verification_result_id:
        return ActionChainStage.VERIFIED
    if observation.verdict == "UNKNOWN":
        return ActionChainStage.OUTCOME_UNKNOWN
    if observation.verdict == "FAIL" and observation.has_step_observed:
        return ActionChainStage.EXECUTED
    if observation.has_step_observed:
        return ActionChainStage.EXECUTED
    if observation.has_step_prepared:
        return ActionChainStage.OUTCOME_UNKNOWN
    if observation.approval_status == "APPROVED":
        return ActionChainStage.NOT_EXECUTED
    if observation.approval_status == "PENDING":
        return ActionChainStage.AWAITING_APPROVAL
    return ActionChainStage.PROPOSED


def derive_action_state(observation: ActionChainObservation) -> ActionState:
    """``ActionState`` 由证据链阶段唯一确定（禁止布尔组合推导）。"""
    return _STAGE_TO_ACTION_STATE[derive_chain_stage(observation)]


def derive_verification_state(
    observation: ActionChainObservation,
) -> VerificationState:
    """从 ``VerificationResult.verdict`` 映射验证状态（W08 三态不升格）。"""
    if observation.verdict == "PASS":
        return VerificationState.PASS
    if observation.verdict == "FAIL":
        return VerificationState.FAIL
    if observation.verdict == "UNKNOWN":
        return VerificationState.UNKNOWN
    if observation.approval_status == "PENDING":
        return VerificationState.PENDING
    return VerificationState.NONE


__all__ = [
    "ActionChainObservation",
    "ActionChainStage",
    "EvidenceRefCheck",
    "EvidenceRefInvalidError",
    "ReportEvidencePort",
    "ReportRefLike",
    "derive_action_state",
    "derive_chain_stage",
    "derive_fact_status",
    "derive_verification_state",
    "inspect_evidence_refs",
    "validate_evidence_refs",
]
