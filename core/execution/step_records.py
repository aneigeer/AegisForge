"""StepPrepared / StepObserved：执行前与执行后证据（P0-16/P0-17）。

硬约束：
- 两张表**只 INSERT**：`dg_ctl` 在数据库层没有 UPDATE/DELETE 授权；
- `step_observed` 刻意不含 `reconciliation_state`，核对结论写 `task_events` 与 `tasks.status`；
- `insert_prepared()` 是 fail closed 落点：写不进去就**不得发起执行**
  （失败转 `AuditUnavailableError` + `AUDIT_WRITE_FAILED` 告警）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from core.audit.events import AuditEventType, make_event
from core.errors import (
    AuditUnavailableError,
    DBGuardianError,
    RequestInvalidError,
    StateConflictError,
)
from core.logging import get_logger
from core.tasks.models import utc_now
from database.mysql.connection import MysqlClient

logger = get_logger(__name__)

PREPARED_TABLE = "dg_system.step_prepared"
OBSERVED_TABLE = "dg_system.step_observed"
MYSQL_DUPLICATE_KEY = 1062


def to_db_time(value: datetime | None) -> datetime | None:
    """进程内 tz-aware UTC → 入库 naive UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def from_db_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    return None


def json_dumps(value: Any) -> str:
    """证据字段只做存储（不参与摘要），但仍拒绝无法序列化的值（fail closed）。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_reject)
    except (TypeError, ValueError) as exc:
        raise RequestInvalidError(
            "证据字段无法序列化为 JSON", details={"reason": str(exc)}
        ) from None


def _reject(value: Any) -> Any:
    raise TypeError(f"不支持的值类型：{type(value).__name__}")


def json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StepPrepared:
    """执行前证据：一旦落盘即不可修改，用于崩溃后核对"是否可能已执行"。"""

    prepared_id: str
    tenant_id: str
    task_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_version: str
    policy_version: str
    environment_id: str
    request: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None
    envelope_id: str | None = None
    approval_id: str | None = None
    action_digest: str | None = None
    idempotency_key: str | None = None
    prepared_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class StepObserved:
    """执行后证据：如实记录结果分类，`UNKNOWN_OUTCOME` 不得被解释成失败或成功。"""

    observed_id: str
    tenant_id: str
    task_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    outcome: str
    started_at: datetime
    result: dict[str, Any] | None = None
    error_code: str | None = None
    step_id: str | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    observed_at: datetime = field(default_factory=utc_now)


class StepRecordRepository:
    """只 INSERT 的证据仓储；不提供任何 update/delete 方法（与授权边界一致）。"""

    def __init__(self, client: MysqlClient, *, audit: Any | None = None) -> None:
        """`audit` 仅用于 `insert_prepared` 失败时的 best-effort 告警事件。"""
        self._client = client
        self._audit = audit

    @property
    def client(self) -> MysqlClient:
        return self._client

    # ---- 写入 ----------------------------------------------------------
    def insert_prepared(self, record: StepPrepared) -> None:
        """强制持久化执行前证据；任何失败都阻断执行（fail closed）。"""
        try:
            self._client.execute(
                f"INSERT INTO {PREPARED_TABLE} (prepared_id, tenant_id, task_id, run_id, "
                "step_id, tool_call_id, envelope_id, approval_id, tool_name, tool_version, "
                "action_digest, idempotency_key, policy_version, environment_id, "
                "request_json, prepared_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.prepared_id,
                    record.tenant_id,
                    record.task_id,
                    record.run_id,
                    record.step_id,
                    record.tool_call_id,
                    record.envelope_id,
                    record.approval_id,
                    record.tool_name,
                    record.tool_version,
                    record.action_digest,
                    record.idempotency_key,
                    record.policy_version,
                    record.environment_id,
                    json_dumps(record.request),
                    to_db_time(record.prepared_at),
                ),
            )
        except DBGuardianError as exc:
            if exc.details.get("mysql_error_code") == MYSQL_DUPLICATE_KEY:
                # 同一 tool_call 已有 prepared 记录：不允许重复准备后再次执行。
                raise StateConflictError(
                    "该工具调用已存在 Prepared 证据，禁止重复准备执行",
                    details={
                        "tool_call_id": record.tool_call_id,
                        "task_id": record.task_id,
                        "prepared_id": record.prepared_id,
                    },
                ) from None
            self._alert(record, exc)
            raise AuditUnavailableError(
                "执行前证据写入失败，已阻断执行",
                details={
                    "tool_call_id": record.tool_call_id,
                    "task_id": record.task_id,
                    "reason_code": exc.code.value,
                },
            ) from None

    def insert_observed(self, record: StepObserved) -> None:
        """追加执行后证据；重复写入同一 tool_call 视为状态冲突。"""
        try:
            self._client.execute(
                f"INSERT INTO {OBSERVED_TABLE} (observed_id, tenant_id, task_id, run_id, "
                "step_id, tool_call_id, tool_name, outcome, result_json, error_code, "
                "started_at, finished_at, duration_ms, observed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.observed_id,
                    record.tenant_id,
                    record.task_id,
                    record.run_id,
                    record.step_id,
                    record.tool_call_id,
                    record.tool_name,
                    record.outcome,
                    json_dumps(record.result) if record.result is not None else None,
                    record.error_code,
                    to_db_time(record.started_at),
                    to_db_time(record.finished_at),
                    record.duration_ms,
                    to_db_time(record.observed_at),
                ),
            )
        except DBGuardianError as exc:
            if exc.details.get("mysql_error_code") == MYSQL_DUPLICATE_KEY:
                raise StateConflictError(
                    "该工具调用已存在 Observed 证据",
                    details={
                        "tool_call_id": record.tool_call_id,
                        "observed_id": record.observed_id,
                    },
                ) from None
            raise

    # ---- 查询 ----------------------------------------------------------
    def find_unobserved(
        self,
        *,
        tenant_id: str,
        older_than_s: float,
        limit: int = 50,
        offset: int = 0,
        task_id: str | None = None,
    ) -> list[StepPrepared]:
        """找出"已 Prepared 但超过阈值仍无 Observed"的调用（崩溃恢复入口）。

        `offset` 供 `Reconciler.sweep()` 分页遍历：单次查询上限 500 条，
        历史残留较多时必须翻页，否则待核对记录会被挤出窗口。
        排序键追加 `prepared_id` 保证同一时间戳下分页顺序稳定。

        `task_id` 为**可选的任务级收窄**，供 `Reconciler.sweep_task()` 使用：
        传入时 SQL 追加 `p.task_id = %s` 谓词（走既有索引
        `idx_prepared_task (tenant_id, task_id, prepared_at)`）；缺省 `None` 时
        SQL 与历史逐字等价，不改变任何既有调用方的行为。
        """
        if older_than_s < 0:
            raise RequestInvalidError(
                "older_than_s 不能为负", details={"older_than_s": older_than_s}
            )
        if limit < 1 or limit > 500:
            raise RequestInvalidError("limit 必须在 1..500 之间", details={"limit": limit})
        if offset < 0:
            raise RequestInvalidError("offset 不能为负", details={"offset": offset})
        if task_id is not None and not str(task_id).strip():
            raise RequestInvalidError("task_id 不能为空字符串", details={"task_id": task_id})
        from datetime import timedelta

        cutoff = to_db_time(utc_now() - timedelta(seconds=older_than_s))
        # 条件与 params 必须**同序**拼装：tenant → cutoff → [task] → limit → offset。
        conditions = ["p.tenant_id = %s", "o.tool_call_id IS NULL", "p.prepared_at <= %s"]
        params: list[Any] = [tenant_id, cutoff]
        if task_id is not None:
            conditions.append("p.task_id = %s")
            params.append(task_id)
        rows = self._client.fetch_all(
            "SELECT p.* FROM dg_system.step_prepared p "
            "LEFT JOIN dg_system.step_observed o ON o.tool_call_id = p.tool_call_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY p.prepared_at ASC, p.prepared_id ASC LIMIT %s OFFSET %s",
            (*params, limit, offset),
            max_rows=limit,
        )
        return [self._to_prepared(row) for row in rows]

    def get_observed(self, tool_call_id: str) -> StepObserved | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {OBSERVED_TABLE} WHERE tool_call_id = %s LIMIT 1",
            (tool_call_id,),
        )
        return self._to_observed(rows[0]) if rows else None

    def evidence_for_approvals(
        self, approval_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """批量汇总审批的执行证据（两条固定 SQL，占位符拼装但不拼值）。

        返回 `{approval_id: {"step_prepared": bool, "step_observed_outcome": str|None}}`。
        刻意做成**批量**接口：`GET /v1/approvals` 列表页若逐条查询就是 N+1。
        参数为空时不发查询（返回空字典），避免拼出 `IN ()` 语法错误。
        """
        ids = [str(item) for item in approval_ids if str(item or "").strip()]
        if not ids:
            return {}
        unique = list(dict.fromkeys(ids))
        placeholders = ",".join(["%s"] * len(unique))

        prepared_rows = self._client.fetch_all(
            "SELECT approval_id, tool_call_id FROM "
            f"{PREPARED_TABLE} WHERE approval_id IN ({placeholders})",
            tuple(unique),
            max_rows=len(unique),
        )
        evidence: dict[str, dict[str, Any]] = {
            str(row["approval_id"]): {
                "step_prepared": True,
                "step_observed_outcome": None,
            }
            for row in prepared_rows
        }
        observed_rows = self._client.fetch_all(
            "SELECT p.approval_id AS approval_id, o.outcome AS outcome, "
            f"o.observed_at AS observed_at FROM {PREPARED_TABLE} p "
            f"JOIN {OBSERVED_TABLE} o ON o.tool_call_id = p.tool_call_id "
            f"WHERE p.approval_id IN ({placeholders}) "
            "ORDER BY o.observed_at ASC",
            tuple(unique),
            max_rows=len(unique) * 4,
        )
        for row in observed_rows:
            approval_id = str(row["approval_id"])
            entry = evidence.setdefault(
                approval_id, {"step_prepared": True, "step_observed_outcome": None}
            )
            # 同一审批若有多条回执，取最后一条（按 observed_at 升序遍历）。
            entry["step_observed_outcome"] = str(row["outcome"])
        return evidence

    # ---- 映射 ----------------------------------------------------------
    @staticmethod
    def _to_prepared(row: dict[str, Any]) -> StepPrepared:
        return StepPrepared(
            prepared_id=str(row["prepared_id"]),
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            tool_version=str(row["tool_version"]),
            policy_version=str(row["policy_version"]),
            environment_id=str(row["environment_id"]),
            request=json_loads(row.get("request_json"), {}),
            step_id=row.get("step_id"),
            envelope_id=row.get("envelope_id"),
            approval_id=row.get("approval_id"),
            action_digest=row.get("action_digest"),
            idempotency_key=row.get("idempotency_key"),
            prepared_at=from_db_time(row.get("prepared_at")) or utc_now(),
        )

    @staticmethod
    def _to_observed(row: dict[str, Any]) -> StepObserved:
        return StepObserved(
            observed_id=str(row["observed_id"]),
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            outcome=str(row["outcome"]),
            started_at=from_db_time(row.get("started_at")) or utc_now(),
            result=json_loads(row.get("result_json"), None),
            error_code=row.get("error_code"),
            step_id=row.get("step_id"),
            finished_at=from_db_time(row.get("finished_at")),
            duration_ms=row.get("duration_ms"),
            observed_at=from_db_time(row.get("observed_at")) or utc_now(),
        )

    # ---- 告警 ----------------------------------------------------------
    def _alert(self, record: StepPrepared, exc: DBGuardianError) -> None:
        logger.error(
            "执行前证据写入失败，R2 已阻断",
            extra={
                "alert": "AUDIT_WRITE_FAILED",
                "tool_call_id": record.tool_call_id,
                "task_id": record.task_id,
                "reason_code": exc.code.value,
            },
        )
        if self._audit is None:
            return
        # best-effort：账本本身不可写时只剩结构化日志（ADR-0006 已知限制 5）。
        try:
            self._audit.try_emit(
                make_event(
                    AuditEventType.AUDIT_WRITE_FAILED,
                    task_id=record.task_id,
                    run_id=record.run_id,
                    tool_call_id=record.tool_call_id,
                    payload={
                        "failed_table": PREPARED_TABLE,
                        "reason_code": exc.code.value,
                        "tenant_id": record.tenant_id,
                        "tool_name": record.tool_name,
                    },
                )
            )
        except DBGuardianError:
            logger.error(
                "告警事件无法写入，告警退化为结构化日志",
                extra={"alert": "AUDIT_WRITE_FAILED", "task_id": record.task_id},
            )


__all__ = [
    "OBSERVED_TABLE",
    "PREPARED_TABLE",
    "StepObserved",
    "StepPrepared",
    "StepRecordRepository",
    "from_db_time",
    "json_dumps",
    "json_loads",
    "to_db_time",
]
