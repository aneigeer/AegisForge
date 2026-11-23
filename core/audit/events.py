"""审计事件：统一事件类型、脱敏与关联 ID（task_id / run_id / tool_call_id）。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from core.logging import redact_text
from core.tasks.models import TaskEvent, new_id, utc_now

# 精确集合保留给既有引用/断言；实际键名判定改用 _is_sensitive_key 的归一化子串匹配。
SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "dsn",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "credentials",
}
# 归一化后的敏感词根：键名去掉大小写与非字母数字后，命中任一子串即脱敏。
# 覆盖 db_password / client_secret / access_token / mysql_dsn / Api-Key 等变体。
_SENSITIVE_MARKERS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "dsn",
    "authorization",
    "apikey",
    "credential",
)
MAX_AUDIT_STRING = 2000
MAX_AUDIT_ITEMS = 50


class AuditEventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    TASK_STATE_CHANGED = "TASK_STATE_CHANGED"
    TASK_PAUSE_REQUESTED = "TASK_PAUSE_REQUESTED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_CANCEL_REQUESTED = "TASK_CANCEL_REQUESTED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_RECOVERED = "TASK_RECOVERED"
    TASK_FAILED = "TASK_FAILED"
    CHECKPOINT_SAVED = "CHECKPOINT_SAVED"
    TOOL_CALL_STARTED = "TOOL_CALL_STARTED"
    TOOL_CALL_SUCCEEDED = "TOOL_CALL_SUCCEEDED"
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    TOOL_CALL_TIMEOUT = "TOOL_CALL_TIMEOUT"
    # W06 批次 1：结果未知不得记成"失败"，单独成类以便事后核对。
    TOOL_CALL_UNKNOWN_OUTCOME = "TOOL_CALL_UNKNOWN_OUTCOME"
    TOOL_CALL_DENIED = "TOOL_CALL_DENIED"
    TOOL_CALL_RETRIED = "TOOL_CALL_RETRIED"
    TOOL_CALL_REPLAYED = "TOOL_CALL_REPLAYED"
    MODEL_CALL_STARTED = "MODEL_CALL_STARTED"
    MODEL_CALL_SUCCEEDED = "MODEL_CALL_SUCCEEDED"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    AGENT_PLAN_CREATED = "AGENT_PLAN_CREATED"
    AGENT_PLAN_REJECTED = "AGENT_PLAN_REJECTED"
    AGENT_STEP_COMPLETED = "AGENT_STEP_COMPLETED"
    AGENT_STEP_FAILED = "AGENT_STEP_FAILED"
    AGENT_STOPPED = "AGENT_STOPPED"
    AGENT_PAUSED = "AGENT_PAUSED"
    AGENT_LOOP_DETECTED = "AGENT_LOOP_DETECTED"
    AGENT_NO_PROGRESS = "AGENT_NO_PROGRESS"
    AGENT_BUDGET_EXCEEDED = "AGENT_BUDGET_EXCEEDED"
    # W05：策略、Gateway 与 Worker 租约
    POLICY_DECISION = "POLICY_DECISION"
    GATEWAY_EXECUTION_ALLOWED = "GATEWAY_EXECUTION_ALLOWED"
    GATEWAY_EXECUTION_DENIED = "GATEWAY_EXECUTION_DENIED"
    ENVELOPE_REJECTED = "ENVELOPE_REJECTED"
    WORKER_LEASE_ACQUIRED = "WORKER_LEASE_ACQUIRED"
    WORKER_LEASE_HEARTBEAT = "WORKER_LEASE_HEARTBEAT"
    WORKER_LEASE_RELEASED = "WORKER_LEASE_RELEASED"
    WORKER_LEASE_EXPIRED = "WORKER_LEASE_EXPIRED"
    # W06：审批事务、Prepared/Observed 证据、核对与审计自身故障
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_APPROVED = "APPROVAL_APPROVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    APPROVAL_CONSUME_DENIED = "APPROVAL_CONSUME_DENIED"
    STEP_PREPARED = "STEP_PREPARED"
    STEP_OBSERVED = "STEP_OBSERVED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
    AUDIT_INTEGRITY_FAILED = "AUDIT_INTEGRITY_FAILED"
    # W08：独立验证三事件（设计 §5 第 1–3 行）；恢复事件归 T05，此处不建。
    VERIFICATION_PLANNED = "VERIFICATION_PLANNED"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    # W08：补偿四事件（设计 §5 第 4–7 行）；补偿是独立 R2 动作，与「回滚」无关。
    COMPENSATION_REQUESTED = "COMPENSATION_REQUESTED"
    COMPENSATION_APPROVED = "COMPENSATION_APPROVED"
    COMPENSATION_EXECUTED = "COMPENSATION_EXECUTED"
    COMPENSATION_DENIED = "COMPENSATION_DENIED"
    # W08 T06：恢复器接管/结束（设计 §5 第 10–11 行）。T05 阶段暂复用
    # `AGENT_STOPPED`，此处补上后停止事件改用 `AGENT_RECOVERY_STOPPED`（payload 不变）。
    AGENT_RECOVERY_STARTED = "AGENT_RECOVERY_STARTED"
    AGENT_RECOVERY_STOPPED = "AGENT_RECOVERY_STOPPED"
    # W08 T06：`Reconciler.sweep()` 单轮结束（结果为空也发）与抛错（调度层，不中断主循环）。
    RECONCILIATION_SWEPT = "RECONCILIATION_SWEPT"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    # W09 T01：脱敏三态事件；模型降级/报告生成/知识撤销的发射点在 T02/T03/T05。
    REDACTION_APPLIED = "REDACTION_APPLIED"
    REDACTION_BLOCKED = "REDACTION_BLOCKED"
    REDACTION_REVIEW_REQUIRED = "REDACTION_REVIEW_REQUIRED"
    MODEL_DEGRADED = "MODEL_DEGRADED"
    REPORT_GENERATED = "REPORT_GENERATED"
    KNOWLEDGE_REVOKED = "KNOWLEDGE_REVOKED"
    # P3-T1：两级人工确认与晋升（每条都落审计，便于追溯"谁在什么时候确认了什么"）。
    KNOWLEDGE_CONFIRMED = "KNOWLEDGE_CONFIRMED"
    KNOWLEDGE_INDEXED = "KNOWLEDGE_INDEXED"
    # W10 T01：评测基座四事件（**只追加成员**，不改任何既有字面与顺序）。
    # 口径（Q-W10-A 批准）：W09 A10 冻结的是 W09 新增的 6 条事件，不禁止 W10 追加。
    # payload 契约：`BENCHMARK_RUN_STARTED/FINISHED` 只带 run_id/manifest digest/计数；
    # `ORACLE_READ/ORACLE_ACCESS_DENIED` **不得含 Oracle 真值**（只带判定事实）。
    BENCHMARK_RUN_STARTED = "BENCHMARK_RUN_STARTED"
    BENCHMARK_RUN_FINISHED = "BENCHMARK_RUN_FINISHED"
    ORACLE_READ = "ORACLE_READ"
    ORACLE_ACCESS_DENIED = "ORACLE_ACCESS_DENIED"


def _is_sensitive_key(key: object) -> bool:
    """键名归一化（小写 + 仅保留字母数字）后按子串匹配敏感词。

    大小写、下划线、连字符、空格等变体一律归一化，避免 db_password /
    access_token / mysql_dsn / Client-Secret 等绕过精确集合。
    """
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


def redact_payload(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏审计载荷；超长字符串截断，避免把大结果写入事件表。

    - 键名按归一化子串匹配敏感词（覆盖 db_password / access_token / mysql_dsn 等变体）；
    - 非白名单类型（bytes/bytearray/memoryview/自定义对象等）一律 `<redacted>`，
      不再用 `str()` 兜底，避免 `"b'...'"` 形式的敏感字节流原样入库；
    - 保留 depth>8 / 2000 字符 / 50 条三项上限。
    """
    if depth > 8:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_AUDIT_ITEMS:
                result["<truncated>"] = f"{len(value) - MAX_AUDIT_ITEMS} more keys"
                break
            if _is_sensitive_key(key):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = redact_payload(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = [redact_payload(item, depth=depth + 1) for item in value[:MAX_AUDIT_ITEMS]]
        if len(value) > MAX_AUDIT_ITEMS:
            items.append(f"<truncated {len(value) - MAX_AUDIT_ITEMS} items>")
        return items
    if isinstance(value, str):
        text = redact_text(value)
        if len(text) > MAX_AUDIT_STRING:
            return text[:MAX_AUDIT_STRING] + "...<truncated>"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "<redacted>"


def make_event(
    event_type: AuditEventType | str,
    *,
    task_id: str,
    run_id: str,
    tool_call_id: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        event_id=new_id("evt"),
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        event_type=event_type.value if isinstance(event_type, AuditEventType) else event_type,
        payload=redact_payload(payload or {}),
        created_at=utc_now(),
        trace_id=trace_id,
    )
