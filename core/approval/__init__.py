"""审批领域层（W06 D2）：状态机、摘要、前置条件探测、仓储与用例。"""

from __future__ import annotations

from core.approval.digest import (
    ACTION_DIGEST_VERSION,
    PREREQUISITES_DIGEST_VERSION,
    build_action_descriptor,
    build_normalized_operation,
    compute_action_digest,
    compute_prerequisites_digest,
    environment_fingerprint_digest,
    environment_fingerprint_from_row,
    fingerprint_completeness,
    normalize_target_objects,
    normalize_tool_parameters,
)
from core.approval.models import (
    ALLOWED_APPROVAL_TRANSITIONS,
    Approval,
    ApprovalDetail,
    ApprovalGrant,
    ApprovalStatus,
    PrerequisiteCheck,
    can_transition,
    ensure_approval_transition,
    ensure_segregation_of_duty,
    error_for_status,
)
from core.approval.prerequisites import PrerequisiteProbe, TargetProbePort
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService

__all__ = [
    "ACTION_DIGEST_VERSION",
    "ALLOWED_APPROVAL_TRANSITIONS",
    "PREREQUISITES_DIGEST_VERSION",
    "Approval",
    "ApprovalDetail",
    "ApprovalGrant",
    "ApprovalRepository",
    "ApprovalService",
    "ApprovalStatus",
    "PrerequisiteCheck",
    "PrerequisiteProbe",
    "TargetProbePort",
    "build_action_descriptor",
    "build_normalized_operation",
    "can_transition",
    "compute_action_digest",
    "compute_prerequisites_digest",
    "ensure_approval_transition",
    "ensure_segregation_of_duty",
    "environment_fingerprint_digest",
    "environment_fingerprint_from_row",
    "error_for_status",
    "fingerprint_completeness",
    "normalize_target_objects",
    "normalize_tool_parameters",
]
