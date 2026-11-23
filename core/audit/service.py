"""审计服务：查询、完整性校验与"必须成功"的事件追加（ADR-0006 §3/§4）。

设计要点：
- 审计真相只有一份：全部落在 `dg_system.task_events`，链的边界是 `task_id`；
- 跨任务查询只能按 `created_at` 排序，**这不是一条可链式校验的链**；
- `emit_required()` 是 fail closed 落点：任何写入异常都转 `AuditUnavailableError`
  并产生 `AUDIT_WRITE_FAILED` 告警（结构化日志 + best-effort 事件）。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any, Sequence

from core.audit.events import AuditEventType, make_event, redact_payload
from core.audit.integrity import ChainVerification, verify_event_chain
from core.errors import AuditUnavailableError, DBGuardianError, RequestInvalidError, TaskNotFoundError
from core.logging import get_logger
from core.tasks.models import TaskEvent
from core.tasks.repository import TaskRepository

logger = get_logger(__name__)

ALERT_AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
# 链被读取上限截断时复用既有 reason 字段（不新增 ChainVerification 字段）。
REASON_CHAIN_TRUNCATED = "CHAIN_TRUNCATED"
ALERT_AUDIT_CHAIN_TRUNCATED = "AUDIT_CHAIN_TRUNCATED"

# 单次完整性校验最多读取的事件数（防止一次读取过大链）；超出按页累积。
_CHAIN_PAGE = 200
_CHAIN_MAX_EVENTS = 20_000

# event_types 的占位符数量上限与枚举白名单，防止构造超大 IN (...)。
_MAX_EVENT_TYPES = 50
_ALLOWED_EVENT_TYPES = frozenset(member.value for member in AuditEventType)


def _to_db(value: datetime) -> datetime:
    """入库时间统一为 naive UTC（与既有仓储一致）。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _encode_cursor(event: TaskEvent) -> str:
    raw = f"{event.created_at.isoformat()}|{event.event_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        timestamp, event_id = raw.split("|", 1)
        moment = datetime.fromisoformat(timestamp)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise RequestInvalidError("审计游标非法", details={"cursor_invalid": True}) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment, event_id


class AuditService:
    """审计查询与完整性校验编排；所有写入都经 `insert_event` 维护哈希链。"""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> TaskRepository:
        return self._repository

    # ---- 查询 ----------------------------------------------------------
    def query(
        self,
        *,
        tenant_id: str,
        task_id: str | None = None,
        approval_id: str | None = None,
        event_types: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[TaskEvent], str | None]:
        """按任务/审批/类型/时间查询审计事件；返回 (事件列表, 下一页游标)。

        租户隔离以 `tasks.tenant_id` 为准（存量事件 `tenant_id` 列可能为 NULL），
        因此 here 使用 JOIN 而不是只过滤事件表的冗余列。
        """
        if limit < 1 or limit > 500:
            raise RequestInvalidError("limit 必须在 1..500 之间", details={"limit": limit})
        if task_id is not None and not self._repository.task_belongs_to(task_id, tenant_id):
            raise TaskNotFoundError(
                "任务不存在", details={"task_id": task_id, "tenant_id": tenant_id}
            )

        conditions: list[str] = ["t.tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if task_id is not None:
            conditions.append("e.task_id = %s")
            params.append(task_id)
        if approval_id is not None:
            # 审批事件统一把 approval_id 写进 payload；此处按需过滤。
            conditions.append(
                "JSON_UNQUOTE(JSON_EXTRACT(e.payload_json, '$.approval_id')) = %s"
            )
            params.append(approval_id)
        if event_types:
            if len(event_types) > _MAX_EVENT_TYPES:
                raise RequestInvalidError(
                    "event_types 数量超过上限",
                    details={"event_types_invalid": True, "max": _MAX_EVENT_TYPES},
                )
            normalized: list[str] = []
            for item in event_types:
                text = item.value if isinstance(item, AuditEventType) else str(item)
                if text not in _ALLOWED_EVENT_TYPES:
                    raise RequestInvalidError(
                        "event_type 非法", details={"event_type_invalid": True}
                    )
                normalized.append(text)
            placeholders = ",".join(["%s"] * len(normalized))
            conditions.append(f"e.event_type IN ({placeholders})")
            params.extend(normalized)
        if since is not None:
            conditions.append("e.created_at >= %s")
            params.append(_to_db(since))
        if until is not None:
            conditions.append("e.created_at <= %s")
            params.append(_to_db(until))
        if cursor:
            moment, event_id = _decode_cursor(cursor)
            conditions.append("(e.created_at > %s OR (e.created_at = %s AND e.event_id > %s))")
            params.extend([_to_db(moment), _to_db(moment), event_id])

        params.append(limit + 1)
        rows = self._repository.client.fetch_all(
            "SELECT e.* FROM dg_system.task_events e "
            "JOIN dg_system.tasks t ON t.task_id = e.task_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY e.created_at ASC, e.event_id ASC LIMIT %s",
            tuple(params),
            max_rows=limit + 1,
        )
        events = [TaskRepository._to_event(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(events[-1]) if len(rows) > limit and events else None
        return events, next_cursor

    # ---- 完整性校验 ----------------------------------------------------
    def verify(self, *, tenant_id: str, task_id: str) -> ChainVerification:
        """校验某任务的事件链；断裂时返回精确位置与受影响区间。

        达到 `_CHAIN_MAX_EVENTS` 上限时链可能不完整，此时绝不静默报绿：
        返回 `ok=False` + `reason=CHAIN_TRUNCATED` 并留结构化告警日志。
        """
        if not self._repository.task_belongs_to(task_id, tenant_id):
            raise TaskNotFoundError(
                "任务不存在", details={"task_id": task_id, "tenant_id": tenant_id}
            )
        events, truncated = self._load_chain_with_status(task_id)
        if truncated:
            logger.error(
                "审计链达到读取上限，校验结论不可信",
                extra={
                    "alert": ALERT_AUDIT_CHAIN_TRUNCATED,
                    "task_id": task_id,
                    "loaded": len(events),
                    "limit": _CHAIN_MAX_EVENTS,
                },
            )
            return ChainVerification(
                ok=False,
                checked=len(events),
                reason=REASON_CHAIN_TRUNCATED,
            )
        return verify_event_chain(events)

    def _load_chain_with_status(
        self, task_id: str
    ) -> tuple[list[TaskEvent], bool]:
        """分页读取整条事件链；返回 (事件列表, 是否因达到上限被截断)。"""
        events: list[TaskEvent] = []
        after_seq = 0
        truncated = False
        while len(events) < _CHAIN_MAX_EVENTS:
            page = self._repository.list_events(task_id, _CHAIN_PAGE, after_seq=after_seq)
            if not page:
                break
            events.extend(page)
            after_seq = int(page[-1].seq)
            if len(page) < _CHAIN_PAGE:
                break
            if len(events) >= _CHAIN_MAX_EVENTS:
                truncated = True
                break
        return events, truncated

    def _load_chain(self, task_id: str) -> list[TaskEvent]:
        """分页读取整条事件链（`seq` 升序，从链头开始）。

        保留 list 返回结构，供既有编排与断言（`len(chain)`）使用。
        """
        return self._load_chain_with_status(task_id)[0]

    # ---- 追加 ----------------------------------------------------------
    def emit_required(
        self,
        event: TaskEvent,
        *,
        actor_user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> TaskEvent:
        """追加"必须成功"的事件；失败抛 `AuditUnavailableError` 并告警。

        R2 执行链上任何"先执行后补审计"的做法都违反 P0-14，因此调用方必须
        让异常向上传播并阻断执行。

        过滤列来源（见 05-freeze.md A.3）：
        - `tenant_id` 缺省取控制库事实 `tasks.tenant_id`（`_resolve_tenant`），
          不信任 payload；W09 T06（KD-3）新增的**可选**入参只允许由服务端组合根
          显式传入（如 `WorkerStack.tenant_id`）——用于"没有对应任务行"的调度层
          伪任务事件，使 `query(tenant_id=..., task_id="system:worker_scheduler")`
          可检索；**禁止**用 `event.payload["tenant_id"]` 作过滤列来源
          （W06 冻结口径不变，历史 NULL 行不改写）；
        - `actor_user_id` 只来自服务端 Principal（显式传参），不信任 payload。
        """
        try:
            return self._repository.insert_event(
                event,
                trace_id=event.trace_id,
                tenant_id=tenant_id or self._resolve_tenant(event.task_id),
                actor_user_id=actor_user_id,
            )
        except DBGuardianError as exc:
            self._alert_write_failed(event, exc)
            raise AuditUnavailableError(
                "审计账本写入失败，已阻断执行",
                details={
                    "event_type": event.event_type,
                    "task_id": event.task_id,
                    "reason_code": exc.code.value,
                },
            ) from None

    def try_emit(
        self,
        event: TaskEvent,
        *,
        actor_user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> bool:
        """best-effort 追加（不抛异常）；仅用于告警事件等降级路径。

        `tenant_id` 语义与 `emit_required` 完全一致（缺省仍由任务行解析）。
        """
        try:
            self.emit_required(
                event, actor_user_id=actor_user_id, tenant_id=tenant_id
            )
        except AuditUnavailableError:
            return False
        return True

    def _resolve_tenant(self, task_id: str) -> str | None:
        """租户过滤列的唯一权威来源：控制库 `tasks.tenant_id`。"""
        try:
            return self._repository.get_task(task_id).tenant_id
        except DBGuardianError:
            # 任务行已不存在时留 NULL：审计写入不得因"任务被清理"而失败，
            # 租户隔离仍由查询侧的 tasks JOIN 兜底（历史 NULL 行同理）。
            return None

    def _alert_write_failed(self, event: TaskEvent, exc: DBGuardianError) -> None:
        logger.error(
            "审计写入失败，R2 已阻断",
            extra={
                "alert": ALERT_AUDIT_WRITE_FAILED,
                "event_type": event.event_type,
                "task_id": event.task_id,
                "reason_code": exc.code.value,
            },
        )
        # best-effort：若账本本身不可写，只剩结构化日志（ADR-0006 已知限制 5）。
        alert = make_event(
            AuditEventType.AUDIT_WRITE_FAILED,
            task_id=event.task_id,
            run_id=event.run_id,
            payload={
                "failed_event_type": event.event_type,
                "failed_event_id": event.event_id,
                "reason_code": exc.code.value,
                "tenant_id": _payload_text(event, "tenant_id"),
                "actor_user_id": _payload_text(event, "actor_user_id"),
                "details": redact_payload(exc.details),
            },
        )
        try:
            self._repository.insert_event(alert, trace_id=alert.trace_id)
        except DBGuardianError:
            logger.error(
                "告警事件同样无法写入，告警退化为结构化日志",
                extra={"alert": ALERT_AUDIT_WRITE_FAILED, "task_id": event.task_id},
            )


def _payload_text(event: TaskEvent, key: str) -> str | None:
    """仅供告警 payload 使用（**不得**作为过滤列来源）。"""
    value = event.payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["ALERT_AUDIT_WRITE_FAILED", "AuditService"]
