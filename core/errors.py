"""统一错误模型。

约束：
- 错误码稳定、可被 API/审计/测试引用；
- message 面向用户，details 只放安全字段；
- 任何错误都不得携带 DSN、密码或凭据片段。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    CONFIG_INVALID = "CONFIG_INVALID"
    REQUEST_INVALID = "REQUEST_INVALID"
    DB_AUTH_FAILED = "DB_AUTH_FAILED"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    DB_TIMEOUT = "DB_TIMEOUT"
    DB_PERMISSION_DENIED = "DB_PERMISSION_DENIED"
    DB_QUERY_ERROR = "DB_QUERY_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    ENVELOPE_INVALID = "ENVELOPE_INVALID"
    NONCE_REPLAY = "NONCE_REPLAY"
    WORKER_LEASE_INVALID = "WORKER_LEASE_INVALID"
    FENCING_TOKEN_STALE = "FENCING_TOKEN_STALE"
    WORKER_CONFLICT = "WORKER_CONFLICT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    STATE_INVALID = "STATE_INVALID"
    STATE_CONFLICT = "STATE_CONFLICT"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    TENANT_INVALID = "TENANT_INVALID"
    # P1-BYOK：供应商配置 / SSRF / 配额
    MODEL_PROVIDER_NOT_FOUND = "MODEL_PROVIDER_NOT_FOUND"
    MODEL_PROVIDER_INVALID = "MODEL_PROVIDER_INVALID"
    SECRET_KEY_MISSING = "SECRET_KEY_MISSING"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    # P3-T3：知识工作台
    KNOWLEDGE_NOT_FOUND = "KNOWLEDGE_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    # W06：审批事务与追加式审计
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_DIGEST_MISMATCH = "APPROVAL_DIGEST_MISMATCH"
    APPROVAL_PREREQUISITE_FAILED = "APPROVAL_PREREQUISITE_FAILED"
    # W07：披露三节（风险/补偿/验证计划）缺失，审批不可通过（HTTP 409）。
    DISCLOSURE_INCOMPLETE = "DISCLOSURE_INCOMPLETE"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    PLAN_INVALID = "PLAN_INVALID"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    LOOP_DETECTED = "LOOP_DETECTED"
    NO_PROGRESS = "NO_PROGRESS"
    INTERNAL = "INTERNAL"
    # W09 T01：脱敏与知识/证据闸门（后两个枚举供 T03/T04 使用，本卡只加枚举）。
    REDACTION_BLOCKED = "REDACTION_BLOCKED"
    REDACTION_REVIEW_REQUIRED = "REDACTION_REVIEW_REQUIRED"
    EVIDENCE_REF_INVALID = "EVIDENCE_REF_INVALID"
    KNOWLEDGE_INVALID = "KNOWLEDGE_INVALID"
    # W10 T01：Oracle 隔离与评测基座（**只追加成员**，不改任何既有字面与顺序）。
    ORACLE_ACCESS_DENIED = "ORACLE_ACCESS_DENIED"
    ORACLE_LEAK_DETECTED = "ORACLE_LEAK_DETECTED"
    BENCHMARK_INVALID = "BENCHMARK_INVALID"


class DBGuardianError(Exception):
    """所有可预期错误的基类。"""

    default_status_code = 500

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code or self.default_status_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        return self.message


class ConfigError(DBGuardianError):
    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.CONFIG_INVALID, message, details=details)


class DatabaseAuthError(DBGuardianError):
    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DB_AUTH_FAILED, message, details=details)


class DatabaseUnavailableError(DBGuardianError):
    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DB_UNAVAILABLE, message, details=details)


class DatabaseTimeoutError(DBGuardianError):
    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DB_TIMEOUT, message, details=details)


class DatabasePermissionError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DB_PERMISSION_DENIED, message, details=details)


class DatabaseQueryError(DBGuardianError):
    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DB_QUERY_ERROR, message, details=details)


class ToolNotFoundError(DBGuardianError):
    default_status_code = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.TOOL_NOT_FOUND, message, details=details)


class PolicyDeniedError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.POLICY_DENIED, message, details=details)


class AuthRequiredError(DBGuardianError):
    default_status_code = 401

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.AUTH_REQUIRED, message, details=details)


class AuthInvalidError(DBGuardianError):
    default_status_code = 401

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.AUTH_INVALID, message, details=details)


class AuthExpiredError(DBGuardianError):
    default_status_code = 401

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.AUTH_EXPIRED, message, details=details)


class ForbiddenError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FORBIDDEN, message, details=details)


class EnvelopeInvalidError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.ENVELOPE_INVALID, message, details=details)


class NonceReplayError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.NONCE_REPLAY, message, details=details)


class WorkerLeaseInvalidError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.WORKER_LEASE_INVALID, message, details=details)


class FencingTokenStaleError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FENCING_TOKEN_STALE, message, details=details)


class WorkerConflictError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.WORKER_CONFLICT, message, details=details)


class TaskNotFoundError(DBGuardianError):
    default_status_code = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.TASK_NOT_FOUND, message, details=details)


class StateInvalidError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.STATE_INVALID, message, details=details)


class StateConflictError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.STATE_CONFLICT, message, details=details)


class RequestInvalidError(DBGuardianError):
    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.REQUEST_INVALID, message, details=details)


class EvidenceInvalidError(DBGuardianError):
    default_status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.EVIDENCE_INVALID, message, details=details)


class TenantInvalidError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.TENANT_INVALID, message, details=details)


class IdempotencyConflictError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_CONFLICT, message, details=details)


class ApprovalRequiredError(DBGuardianError):
    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_REQUIRED, message, details=details)


class ApprovalNotFoundError(DBGuardianError):
    """审批不存在（含跨租户访问：按不存在处理，不泄漏存在性）。"""

    default_status_code = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_NOT_FOUND, message, details=details)


class ApprovalNotApprovedError(DBGuardianError):
    """审批尚未批准（仍为 PENDING）。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_NOT_APPROVED, message, details=details)


class ApprovalRejectedError(DBGuardianError):
    """审批已被拒绝。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_REJECTED, message, details=details)


class ApprovalExpiredError(DBGuardianError):
    """审批已过期。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_EXPIRED, message, details=details)


class ApprovalRevokedError(DBGuardianError):
    """审批已被撤销。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_REVOKED, message, details=details)


class ApprovalAlreadyConsumedError(DBGuardianError):
    """审批已被消费（一次性凭证）。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_ALREADY_CONSUMED, message, details=details)


class ApprovalDigestMismatchError(DBGuardianError):
    """消费时重算摘要与审批记录不符；`details.channel` 指明具体不符通道。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_DIGEST_MISMATCH, message, details=details)


class ApprovalPrerequisiteFailedError(DBGuardianError):
    """前置条件探测未全部 PASS，拒绝建立审批（不产生 PENDING 行）。"""

    default_status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.APPROVAL_PREREQUISITE_FAILED, message, details=details)


class DisclosureIncompleteError(DBGuardianError):
    """披露三节缺失：审批不可通过（与"状态不允许"的 `STATE_CONFLICT` 严格区分）。

    `details.missing_fields` 列出全量缺失键（如 `disclosure.risk`），审批状态保持
    不变——缺内容不是状态迁移事件，补不了就不能批。
    """

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DISCLOSURE_INCOMPLETE, message, details=details)


class AuditUnavailableError(DBGuardianError):
    """审计账本不可写：必须阻断 R2 执行并告警（fail closed）。"""

    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.AUDIT_UNAVAILABLE, message, details=details)


class ModelUnavailableError(DBGuardianError):
    default_status_code = 503

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_UNAVAILABLE, message, details=details)


class ModelTimeoutError(DBGuardianError):
    default_status_code = 504

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_TIMEOUT, message, details=details)


class ModelCallFailedError(DBGuardianError):
    default_status_code = 502

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_CALL_FAILED, message, details=details)


class ModelOutputInvalidError(DBGuardianError):
    default_status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_OUTPUT_INVALID, message, details=details)


class PlanInvalidError(DBGuardianError):
    default_status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.PLAN_INVALID, message, details=details)


class BudgetExceededError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.BUDGET_EXCEEDED, message, details=details)


class LoopDetectedError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.LOOP_DETECTED, message, details=details)


class NoProgressError(DBGuardianError):
    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.NO_PROGRESS, message, details=details)


class RedactionBlockedError(DBGuardianError):
    """W09 T01：命中连接串/私钥/证书或二进制叶子，载荷不可继续外发。

    与 `REDACTION_REVIEW_REQUIRED` 严格区分：BLOCKED 是结构性不可安全改写，
    调用方必须丢弃载荷、发 `REDACTION_BLOCKED` 事件，不写 evidence 行。
    """

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.REDACTION_BLOCKED, message, details=details)


class RedactionReviewRequiredError(DBGuardianError):
    """W09 T01：存在未解决 span / 结构超限，需人工复核后才能外发。"""

    default_status_code = 409

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.REDACTION_REVIEW_REQUIRED, message, details=details)


class OracleAccessDenied(DBGuardianError):
    """W10 T01：Oracle 读取被闸门拒绝（非终态任务，或同一 (run_id, scenario_id) 已读过）。

    `details` 只允许放**可检索的判定事实**（`reason` / `task_id` / `run_id` /
    `scenario_id` / `task_status`）；**绝不**放 `expected_root_cause`、
    `injection_params`、`truth_assertions` 等真值字段——拒绝也要守住隔离边界。
    """

    default_status_code = 403

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.ORACLE_ACCESS_DENIED, message, details=details)


class OracleLeakDetected(DBGuardianError):
    """W10 T01：待外发文本命中 Oracle 真值字段名（prompt / 上下文 / 训练样本 / 运行产物）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.ORACLE_LEAK_DETECTED, message, details=details)


class BenchmarkInvalidError(DBGuardianError):
    """W10：评测输入/清单/口径不合法（Manifest 字段缺失、指标定义不符等）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.BENCHMARK_INVALID, message, details=details)


class ModelProviderNotFoundError(DBGuardianError):
    """P1-BYOK：租户下不存在该供应商（跨租户一律 404，不泄露存在性）。"""

    default_status_code = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_PROVIDER_NOT_FOUND, message, details=details)


class ModelProviderInvalidError(DBGuardianError):
    """P1-BYOK：供应商配置不合法（类型/base_url/角色映射/启用状态）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MODEL_PROVIDER_INVALID, message, details=details)


class SecretKeyMissingError(DBGuardianError):
    """P1-BYOK：未配置 `DG_SECRET_MASTER_KEY`，无法加解密 BYOK 凭据。"""

    default_status_code = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.SECRET_KEY_MISSING, message, details=details)


class SsrfBlockedError(DBGuardianError):
    """P1-BYOK：base_url 未通过 SSRF 校验（loopback/link-local/内网/受控端口）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.SSRF_BLOCKED, message, details=details)


class QuotaExceededError(DBGuardianError):
    """P1-BYOK：租户 token/调用配额耗尽，按 fail-closed 中止。"""

    default_status_code = 429

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.QUOTA_EXCEEDED, message, details=details)


class KnowledgeNotFoundError(DBGuardianError):
    """P3-T3：知识条目不存在（跨租户一律 404，不泄露存在性）。"""

    default_status_code = 404

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.KNOWLEDGE_NOT_FOUND, message, details=details)
