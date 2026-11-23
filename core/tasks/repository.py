"""任务持久化仓储（MySQL 8，dg_system 库）。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from core.errors import (
    DatabaseQueryError,
    StateConflictError,
    StateInvalidError,
    TaskNotFoundError,
)
from core.tasks.models import (
    Checkpoint,
    Task,
    TaskEvent,
    TaskStatus,
    TaskStep,
    ToolCallRecord,
)
from database.mysql.connection import MysqlClient

TASKS = "dg_system.tasks"
STEPS = "dg_system.task_steps"
TOOL_CALLS = "dg_system.tool_calls"
EVENTS = "dg_system.task_events"
CHECKPOINTS = "dg_system.checkpoints"
ENVIRONMENTS = "dg_system.environments"

_EVENT_INSERT_ATTEMPTS = 3
_FLAG_UPDATE_ATTEMPTS = 3


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def event_hash(
    *,
    task_id: str,
    run_id: str,
    tool_call_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    created_at: datetime,
    seq: int,
    previous_event_hash: str | None,
) -> str:
    """按规范化 JSON 计算事件哈希，供仓储与审计校验复用。"""
    canonical = {
        "task_id": task_id,
        "run_id": run_id,
        "tool_call_id": tool_call_id,
        "event_type": event_type,
        "payload": payload,
        "created_at": created_at.isoformat(),
        "seq": seq,
        "previous_event_hash": previous_event_hash,
    }
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TaskRepository:
    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    # ---- tasks ---------------------------------------------------------
    def insert_task(self, task: Task) -> None:
        self._client.execute(
            f"INSERT INTO {TASKS} (task_id, run_id, goal, status, risk_level, "
            "cancel_requested, pause_requested, error_code, error_message, "
            "created_at, updated_at, finished_at, tenant_id, environment_id, "
            "created_by, state_version, deadline_at, max_steps, max_tool_calls, "
            "max_tokens, replan_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                task.task_id,
                task.run_id,
                task.goal,
                task.status.value,
                task.risk_level,
                int(task.cancel_requested),
                int(task.pause_requested),
                task.error_code,
                task.error_message,
                _to_db(task.created_at),
                _to_db(task.updated_at),
                _to_db(task.finished_at),
                task.tenant_id,
                task.environment_id,
                task.created_by,
                task.state_version,
                _to_db(task.deadline_at),
                task.max_steps,
                task.max_tool_calls,
                task.max_tokens,
                task.replan_count,
            ),
        )

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        if tenant_id is None:
            rows = self._client.fetch_all(
                f"SELECT * FROM {TASKS} WHERE task_id = %s LIMIT 1", (task_id,)
            )
        else:
            rows = self._client.fetch_all(
                f"SELECT * FROM {TASKS} WHERE task_id = %s AND tenant_id = %s LIMIT 1",
                (task_id, tenant_id),
            )
        if not rows:
            raise TaskNotFoundError(
                "任务不存在", details={"task_id": task_id, "tenant_id": tenant_id}
            )
        return self._to_task(rows[0])

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
        *,
        tenant_id: str | None = None,
    ) -> list[Task]:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = %s")
            params.append(status.value)
        if tenant_id is not None:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
        params.append(limit)
        rows = self._client.fetch_all(
            f"SELECT * FROM {TASKS} {where}ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )
        return [self._to_task(row) for row in rows]

    def list_unfinished_tasks(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        tenant_id: str | None = None,
    ) -> list[Task]:
        conditions = ["status NOT IN ('COMPLETED','FAILED','CANCELLED')"]
        params: list[Any] = []
        if tenant_id is not None:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)
        params.extend([limit, offset])
        rows = self._client.fetch_all(
            f"SELECT * FROM {TASKS} WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at ASC LIMIT %s OFFSET %s",
            tuple(params),
        )
        return [self._to_task(row) for row in rows]

    def update_task_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        expected_state_version: int | None = None,
        expected_status: TaskStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> Task:
        current = self.get_task(task_id)
        if expected_status is not None and current.status != expected_status:
            raise StateInvalidError(
                "任务状态已被其他操作修改",
                details={
                    "task_id": task_id,
                    "expected": expected_status.value,
                    "actual": current.status.value,
                },
            )
        expected = (
            current.state_version if expected_state_version is None else expected_state_version
        )
        if expected != current.state_version:
            raise StateConflictError(
                "任务状态版本冲突，请重新读取任务状态",
                details={
                    "task_id": task_id,
                    "expected": expected,
                    "actual": current.state_version,
                    "expected_state_version": expected,
                    "actual_state_version": current.state_version,
                },
            )
        affected = self._client.execute(
            f"UPDATE {TASKS} SET status = %s, error_code = %s, error_message = %s, "
            "updated_at = %s, finished_at = %s, state_version = state_version + 1 "
            "WHERE task_id = %s AND state_version = %s",
            (
                new_status.value,
                error_code,
                error_message,
                _to_db(datetime.now(timezone.utc)),
                _to_db(finished_at),
                task_id,
                expected,
            ),
        )
        if affected == 0:
            latest = self.get_task(task_id)
            raise StateConflictError(
                "任务状态版本冲突，请重新读取任务状态",
                details={
                    "task_id": task_id,
                    "expected": expected,
                    "actual": latest.state_version,
                    "expected_state_version": expected,
                    "actual_state_version": latest.state_version,
                    "attempted": new_status.value,
                },
            )
        return self.get_task(task_id)

    def set_task_flags(
        self,
        task_id: str,
        *,
        cancel_requested: bool | None = None,
        pause_requested: bool | None = None,
    ) -> Task:
        for _ in range(_FLAG_UPDATE_ATTEMPTS):
            current = self.get_task(task_id)
            assignments: list[str] = ["updated_at = %s", "state_version = state_version + 1"]
            params: list[Any] = [_to_db(datetime.now(timezone.utc))]
            if cancel_requested is not None:
                assignments.append("cancel_requested = %s")
                params.append(int(cancel_requested))
            if pause_requested is not None:
                assignments.append("pause_requested = %s")
                params.append(int(pause_requested))
            params.extend([task_id, current.state_version])
            affected = self._client.execute(
                f"UPDATE {TASKS} SET {', '.join(assignments)} "
                "WHERE task_id = %s AND state_version = %s",
                tuple(params),
            )
            if affected:
                return self.get_task(task_id)
        latest = self.get_task(task_id)
        raise StateConflictError(
            "任务标记更新冲突，请重新读取任务状态",
            details={
                "task_id": task_id,
                "expected": latest.state_version,
                "actual": latest.state_version,
            },
        )

    # ---- steps ---------------------------------------------------------
    def insert_step(self, step: TaskStep) -> None:
        self._client.execute(
            f"INSERT INTO {STEPS} (step_id, task_id, run_id, step_index, title, tool_name, "
            "params_json, status, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                step.step_id,
                step.task_id,
                step.run_id,
                step.step_index,
                step.title,
                step.tool_name,
                _json_dump(step.params),
                step.status,
                _to_db(step.created_at),
                _to_db(step.updated_at),
            ),
        )

    def update_step_status(
        self, step_id: str, status: str, *, params: dict[str, Any] | None = None
    ) -> None:
        if params is None:
            self._client.execute(
                f"UPDATE {STEPS} SET status = %s, updated_at = %s WHERE step_id = %s",
                (status, _to_db(datetime.now(timezone.utc)), step_id),
            )
        else:
            self._client.execute(
                f"UPDATE {STEPS} SET status = %s, params_json = %s, updated_at = %s "
                "WHERE step_id = %s",
                (status, _json_dump(params), _to_db(datetime.now(timezone.utc)), step_id),
            )

    def list_steps(self, task_id: str) -> list[TaskStep]:
        rows = self._client.fetch_all(
            f"SELECT * FROM {STEPS} WHERE task_id = %s ORDER BY step_index ASC", (task_id,)
        )
        return [self._to_step(row) for row in rows]

    # ---- tool calls ----------------------------------------------------
    def insert_tool_call(self, record: ToolCallRecord) -> None:
        self._client.execute(
            f"INSERT INTO {TOOL_CALLS} (tool_call_id, task_id, run_id, step_id, tool_name, "
            "idempotency_key, status, attempt, request_json, result_json, error_code, "
            "error_message, started_at, finished_at, duration_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record.tool_call_id,
                record.task_id,
                record.run_id,
                record.step_id,
                record.tool_name,
                record.idempotency_key,
                record.status,
                record.attempt,
                _json_dump(record.request),
                _json_dump(record.result) if record.result is not None else None,
                record.error_code,
                record.error_message,
                _to_db(record.started_at),
                _to_db(record.finished_at),
                record.duration_ms,
            ),
        )

    def update_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        attempt: int,
        result: dict[str, Any] | None,
        error_code: str | None,
        error_message: str | None,
        duration_ms: float,
    ) -> None:
        self._client.execute(
            f"UPDATE {TOOL_CALLS} SET status = %s, attempt = %s, result_json = %s, "
            "error_code = %s, error_message = %s, finished_at = %s, duration_ms = %s "
            "WHERE tool_call_id = %s",
            (
                status,
                attempt,
                _json_dump(result) if result is not None else None,
                error_code,
                error_message,
                _to_db(datetime.now(timezone.utc)),
                duration_ms,
                tool_call_id,
            ),
        )

    def find_tool_call_by_idempotency(self, task_id: str, key: str) -> ToolCallRecord | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {TOOL_CALLS} WHERE task_id = %s AND idempotency_key = %s LIMIT 1",
            (task_id, key),
        )
        return self._to_tool_call(rows[0]) if rows else None

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {TOOL_CALLS} WHERE tool_call_id = %s LIMIT 1", (tool_call_id,)
        )
        return self._to_tool_call(rows[0]) if rows else None

    # ---- events --------------------------------------------------------
    def insert_event(
        self,
        event: TaskEvent,
        trace_id: str | None = None,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> TaskEvent:
        """追加事件并维护 (task_id, seq) 哈希链；并发撞唯一键时重新计算并重试。

        W06 起额外写入 `tenant_id`/`actor_user_id` 两个过滤列（用于审计查询），
        二者**不参与** `event_hash()` 计算，因此存量哈希链校验不受影响。

        **只接受显式来源**：`tenant_id` 由调用方从控制库事实（`tasks.tenant_id`）取，
        `actor_user_id` 由服务端 Principal 取。禁止从 `payload` 兜底——
        否则客户端可控的 payload 键会污染租户过滤列，而哈希链无法发现
        （这两列不进哈希，见 docs/w06/05-freeze.md A.3）。
        """
        resolved_tenant = tenant_id
        resolved_actor = actor_user_id
        last_error: DatabaseQueryError | None = None
        for _ in range(_EVENT_INSERT_ATTEMPTS):
            rows = self._client.fetch_all(
                f"SELECT MAX(seq) AS max_seq FROM {EVENTS} WHERE task_id = %s",
                (event.task_id,),
            )
            max_seq = rows[0]["max_seq"] if rows else None
            seq = 0 if max_seq is None else int(max_seq) + 1
            previous = self._client.fetch_all(
                f"SELECT event_hash FROM {EVENTS} WHERE task_id = %s "
                "ORDER BY seq DESC LIMIT 1",
                (event.task_id,),
            )
            previous_hash = (
                str(previous[0]["event_hash"])
                if previous and previous[0]["event_hash"] is not None
                else None
            )
            trace = trace_id or event.trace_id
            stored = event.model_copy(
                update={
                    "seq": seq,
                    "previous_event_hash": previous_hash,
                    "trace_id": trace,
                }
            )
            stored = stored.model_copy(
                update={
                    "event_hash": event_hash(
                        task_id=stored.task_id,
                        run_id=stored.run_id,
                        tool_call_id=stored.tool_call_id,
                        event_type=stored.event_type,
                        payload=stored.payload,
                        created_at=stored.created_at,
                        seq=seq,
                        previous_event_hash=previous_hash,
                    )
                }
            )
            try:
                self._client.execute(
                    f"INSERT INTO {EVENTS} (event_id, task_id, run_id, tool_call_id, "
                    "event_type, payload_json, created_at, seq, previous_event_hash, "
                    "event_hash, trace_id, tenant_id, actor_user_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        stored.event_id,
                        stored.task_id,
                        stored.run_id,
                        stored.tool_call_id,
                        stored.event_type,
                        _json_dump(stored.payload),
                        _to_db(stored.created_at),
                        stored.seq,
                        stored.previous_event_hash,
                        stored.event_hash,
                        stored.trace_id,
                        resolved_tenant,
                        resolved_actor,
                    ),
                )
                return stored
            except DatabaseQueryError as exc:
                if exc.details.get("mysql_error_code") != 1062:
                    raise
                last_error = exc
        raise DatabaseQueryError(
            "事件追加失败：并发冲突重试后仍未获得唯一序号",
            details={
                "task_id": event.task_id,
                "attempts": _EVENT_INSERT_ATTEMPTS,
                "mysql_error_code": last_error.details.get("mysql_error_code")
                if last_error
                else None,
            },
        )

    def list_events(
        self, task_id: str, limit: int = 200, *, after_seq: int = 0
    ) -> list[TaskEvent]:
        # after_seq=0 表示从链头读取（seq 从 0 开始）；大于 0 时为严格游标。
        operator = ">=" if after_seq <= 0 else ">"
        rows = self._client.fetch_all(
            f"SELECT * FROM {EVENTS} WHERE task_id = %s AND seq {operator} %s "
            "ORDER BY seq ASC LIMIT %s",
            (task_id, after_seq, limit),
        )
        return [self._to_event(row) for row in rows]

    def task_belongs_to(self, task_id: str, tenant_id: str) -> bool:
        rows = self._client.fetch_all(
            f"SELECT 1 AS found FROM {TASKS} WHERE task_id = %s AND tenant_id = %s LIMIT 1",
            (task_id, tenant_id),
        )
        return bool(rows)

    def environment_belongs_to(self, environment_id: str, tenant_id: str) -> bool:
        rows = self._client.fetch_all(
            f"SELECT 1 AS found FROM {ENVIRONMENTS} "
            "WHERE environment_id = %s AND tenant_id = %s LIMIT 1",
            (environment_id, tenant_id),
        )
        return bool(rows)

    def get_environment(
        self, environment_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        """读取环境事实（状态/引擎/版本），供服务端风险解析使用。"""
        if tenant_id is None:
            rows = self._client.fetch_all(
                f"SELECT * FROM {ENVIRONMENTS} WHERE environment_id = %s LIMIT 1",
                (environment_id,),
            )
        else:
            rows = self._client.fetch_all(
                f"SELECT * FROM {ENVIRONMENTS} WHERE environment_id = %s "
                "AND tenant_id = %s LIMIT 1",
                (environment_id, tenant_id),
            )
        return dict(rows[0]) if rows else None

    # ---- checkpoints ---------------------------------------------------
    def save_checkpoint(self, task_id: str, run_id: str, state: dict[str, Any]) -> Checkpoint:
        rows = self._client.fetch_all(
            f"SELECT COALESCE(MAX(sequence), 0) AS seq FROM {CHECKPOINTS} WHERE task_id = %s",
            (task_id,),
        )
        sequence = int(rows[0]["seq"]) + 1 if rows else 1
        from core.tasks.models import new_id, utc_now

        checkpoint = Checkpoint(
            checkpoint_id=new_id("ckpt"),
            task_id=task_id,
            run_id=run_id,
            sequence=sequence,
            state=state,
            created_at=utc_now(),
        )
        self._client.execute(
            f"INSERT INTO {CHECKPOINTS} (checkpoint_id, task_id, run_id, sequence, "
            "state_json, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                checkpoint.checkpoint_id,
                task_id,
                run_id,
                sequence,
                _json_dump(state),
                _to_db(checkpoint.created_at),
            ),
        )
        return checkpoint

    def latest_checkpoint(self, task_id: str) -> Checkpoint | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {CHECKPOINTS} WHERE task_id = %s ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return Checkpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            state=_json_load(row["state_json"], {}),
            created_at=_from_db(row["created_at"]),  # type: ignore[arg-type]
        )

    # ---- row mapping ---------------------------------------------------
    @staticmethod
    def _to_task(row: dict[str, Any]) -> Task:
        return Task(
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            goal=str(row["goal"]),
            status=TaskStatus(str(row["status"])),
            risk_level=str(row["risk_level"]),  # type: ignore[arg-type]
            cancel_requested=bool(row["cancel_requested"]),
            pause_requested=bool(row["pause_requested"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=_from_db(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_from_db(row["updated_at"]),  # type: ignore[arg-type]
            finished_at=_from_db(row["finished_at"]),
            tenant_id=str(row.get("tenant_id") or "tenant_local"),
            environment_id=str(row.get("environment_id") or "env_mysql_local"),
            created_by=str(row.get("created_by") or "user_local"),
            state_version=int(row.get("state_version") or 1),
            deadline_at=_from_db(row.get("deadline_at")),
            max_steps=int(row.get("max_steps") or 8),
            max_tool_calls=int(row.get("max_tool_calls") or 10),
            max_tokens=int(row.get("max_tokens") or 20_000),
            replan_count=int(row.get("replan_count") or 0),
        )

    @staticmethod
    def _to_step(row: dict[str, Any]) -> TaskStep:
        return TaskStep(
            step_id=str(row["step_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            step_index=int(row["step_index"]),
            title=str(row["title"]),
            tool_name=row["tool_name"],
            params=_json_load(row["params_json"], {}),
            status=str(row["status"]),  # type: ignore[arg-type]
            created_at=_from_db(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_from_db(row["updated_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _to_tool_call(row: dict[str, Any]) -> ToolCallRecord:
        return ToolCallRecord(
            tool_call_id=str(row["tool_call_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            step_id=row["step_id"],
            tool_name=str(row["tool_name"]),
            idempotency_key=row["idempotency_key"],
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            request=_json_load(row["request_json"], {}),
            result=_json_load(row["result_json"], None),
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=_from_db(row["started_at"]),  # type: ignore[arg-type]
            finished_at=_from_db(row["finished_at"]),
            duration_ms=row["duration_ms"],
        )

    @staticmethod
    def _to_event(row: dict[str, Any]) -> TaskEvent:
        return TaskEvent(
            event_id=str(row["event_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            tool_call_id=row["tool_call_id"],
            event_type=str(row["event_type"]),
            payload=_json_load(row["payload_json"], {}),
            created_at=_from_db(row["created_at"]),  # type: ignore[arg-type]
            seq=int(row.get("seq") or 0),
            previous_event_hash=row.get("previous_event_hash"),
            event_hash=row.get("event_hash"),
            trace_id=row.get("trace_id"),
        )
