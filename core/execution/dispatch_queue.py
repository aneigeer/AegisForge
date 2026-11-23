"""P6-T1：API -> Worker 只读诊断工具派发队列（控制库只追加通信，实现 API 零 lab_net 隔离）。

架构不变量：
- API 侧（control_net，无 lab_net）通过 :class:`RemoteReadonlyTool` 把只读观测请求
  写入 ``dg_system.readonly_tool_requests`` 并轮询 ``readonly_tool_responses``；
- Worker 侧（control_net + lab_net，无 model_net）通过
  :class:`WorkerReadonlyDispatcher` 领取未响应的只读请求，在 ``lab_net`` 执行只读工具，
  经 ``RedactionGate`` 脱敏后写入 ``readonly_tool_responses``；
- **硬边界**：仅允许 ``side_effect == "read"`` 工具入队与执行；任何写工具一律拒绝
  （写操作必须走审批 + ToolGateway 唯一执行链，INV-01）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from core.errors import RequestInvalidError
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionStatus
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.registry import ToolRegistry

_REQUESTS_TABLE = "dg_system.readonly_tool_requests"
_RESPONSES_TABLE = "dg_system.readonly_tool_responses"


def _error_result(
    tool_name: str,
    *,
    status: ToolStatus,
    code: str,
    message: str,
    started_at: str | None = None,
    duration_ms: float = 0.0,
) -> ToolResult:
    now = started_at or datetime.now(timezone.utc).isoformat()
    return ToolResult(
        tool_name=tool_name,
        status=status,
        error=ToolError(code=code, message=message),
        started_at=now,
        duration_ms=duration_ms,
        source=ToolSource(
            database_id="worker_dispatch_queue",
            engine="mysql",
            db_version="unknown",
            collected_at=now,
        ),
    )


class ControlSqlPort(Protocol):
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int: ...
    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ReadonlyDispatchRequest:
    request_id: str
    tenant_id: str
    task_id: str
    engine: str
    tool_name: str
    params: dict[str, Any]
    requested_by: str


class ReadonlyDispatchQueue:
    """只追加（INSERT/SELECT）只读工具派发队列，兼容 dg_ctl 零 UPDATE 权限。"""

    def __init__(self, control_client: ControlSqlPort) -> None:
        self._client = control_client

    def enqueue(
        self,
        *,
        tenant_id: str,
        task_id: str,
        tool_name: str,
        params: Mapping[str, Any],
        engine: str = "mysql",
        requested_by: str = "api",
        side_effect: str = "read",
    ) -> str:
        if side_effect != "read":
            raise RequestInvalidError(
                "只读派发队列严禁接收写工具（写操作必须走审批与 ToolGateway）",
                details={"tool_name": tool_name, "side_effect": side_effect},
            )
        request_id = f"roreq_{uuid.uuid4().hex}"
        self._client.execute(
            f"INSERT INTO {_REQUESTS_TABLE} "
            "(request_id, tenant_id, task_id, engine, tool_name, params_json, requested_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                request_id,
                tenant_id,
                task_id,
                engine,
                tool_name,
                json.dumps(dict(params), ensure_ascii=False),
                requested_by,
            ),
        )
        return request_id

    def get_response(self, request_id: str) -> ToolResult | None:
        rows = self._client.fetch_all(
            f"SELECT status, result_json FROM {_RESPONSES_TABLE} WHERE request_id = %s LIMIT 1",
            (request_id,),
        )
        if not rows:
            return None
        raw = rows[0].get("result_json")
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(payload, Mapping):
            return ToolResult.model_validate(payload)
        return _error_result(
            "unknown",
            status=ToolStatus.ERROR,
            code="DISPATCH_RESPONSE_INVALID",
            message="无法解析 Worker 响应",
        )

    def wait_for_response(
        self,
        request_id: str,
        *,
        tool_name: str = "remote_readonly",
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> ToolResult:
        started_iso = datetime.now(timezone.utc).isoformat()
        started = clock()
        deadline = started + max(0.05, timeout_s)
        while True:
            result = self.get_response(request_id)
            if result is not None:
                return result
            if clock() >= deadline:
                return _error_result(
                    tool_name,
                    status=ToolStatus.TIMEOUT,
                    code="WORKER_DISPATCH_TIMEOUT",
                    message=f"等待 Worker 只读执行超时（request_id={request_id}）",
                    started_at=started_iso,
                    duration_ms=max(0.0, (clock() - started) * 1000.0),
                )
            sleeper(poll_interval_s)

    def list_pending(self, *, limit: int = 20) -> list[ReadonlyDispatchRequest]:
        rows = self._client.fetch_all(
            f"SELECT r.request_id, r.tenant_id, r.task_id, r.engine, r.tool_name, "
            f"r.params_json, r.requested_by "
            f"FROM {_REQUESTS_TABLE} r "
            f"LEFT JOIN {_RESPONSES_TABLE} resp ON r.request_id = resp.request_id "
            f"WHERE resp.request_id IS NULL "
            f"ORDER BY r.created_at ASC LIMIT %s",
            (max(1, limit),),
        )
        out: list[ReadonlyDispatchRequest] = []
        for row in rows:
            raw_params = row.get("params_json")
            params = json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
            out.append(
                ReadonlyDispatchRequest(
                    request_id=str(row["request_id"]),
                    tenant_id=str(row["tenant_id"]),
                    task_id=str(row["task_id"]),
                    engine=str(row.get("engine") or "mysql"),
                    tool_name=str(row["tool_name"]),
                    params=dict(params) if isinstance(params, Mapping) else {},
                    requested_by=str(row.get("requested_by") or "api"),
                )
            )
        return out

    def record_response(
        self,
        *,
        request_id: str,
        worker_id: str,
        result: ToolResult,
        redaction_version: str | None = None,
    ) -> None:
        self._client.execute(
            f"INSERT IGNORE INTO {_RESPONSES_TABLE} "
            "(request_id, worker_id, status, result_json, redaction_version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                request_id,
                worker_id,
                result.status.value,
                json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                redaction_version,
            ),
        )


class RemoteReadonlyTool:
    """API 进程内使用的远程只读工具代理：零 lab_net 依赖。"""

    def __init__(
        self,
        definition: ToolDefinition,
        queue: ReadonlyDispatchQueue,
        *,
        tenant_id: str,
        engine: str = "mysql",
        requested_by: str = "api",
        timeout_s: float = 30.0,
    ) -> None:
        if definition.side_effect != "read":
            raise RequestInvalidError(
                "RemoteReadonlyTool 只允许包装 read 工具",
                details={"tool_name": definition.name, "side_effect": definition.side_effect},
            )
        self.definition = definition
        self._queue = queue
        self._tenant_id = tenant_id
        self._engine = engine
        self._requested_by = requested_by
        self._timeout_s = timeout_s

    def run(self, context: ToolContext, params: dict[str, Any]) -> ToolResult:
        req_id = self._queue.enqueue(
            tenant_id=self._tenant_id,
            task_id=context.task_id or "remote_diag",
            tool_name=self.definition.name,
            params=params,
            engine=self._engine,
            requested_by=self._requested_by,
            side_effect=self.definition.side_effect,
        )
        return self._queue.wait_for_response(
            req_id, tool_name=self.definition.name, timeout_s=self._timeout_s
        )


def build_remote_readonly_registry(
    queue: ReadonlyDispatchQueue,
    base_registry: ToolRegistry,
    *,
    tenant_id: str,
    engine: str = "mysql",
    requested_by: str = "api",
    timeout_s: float = 30.0,
) -> ToolRegistry:
    """基于基础只读工具定义构造远程派发注册表（过滤掉任何非 read 工具）。"""
    remote = ToolRegistry()
    for definition in base_registry.definitions():
        if definition.side_effect != "read":
            continue
        remote.register(
            RemoteReadonlyTool(
                definition,
                queue,
                tenant_id=tenant_id,
                engine=engine,
                requested_by=requested_by,
                timeout_s=timeout_s,
            )
        )
    return remote


class WorkerReadonlyDispatcher:
    """Worker 侧只读工具执行器：消费控制库请求，在 lab_net 跑只读工具并脱敏回写。"""

    def __init__(
        self,
        queue: ReadonlyDispatchQueue,
        observer_registry: ToolRegistry,
        *,
        ro_client: Any,
        worker_id: str,
        redaction_gate: RedactionGate | None = None,
    ) -> None:
        self._queue = queue
        self._registry = observer_registry
        self._ro_client = ro_client
        self._worker_id = worker_id
        self._gate = redaction_gate

    def drain_once(self, *, limit: int = 10) -> int:
        """处理最多 limit 条未响应只读请求，返回本轮处理条数。"""
        pending = self._queue.list_pending(limit=limit)
        handled = 0
        for req in pending:
            result, redaction_ver = self._execute_one(req)
            self._queue.record_response(
                request_id=req.request_id,
                worker_id=self._worker_id,
                result=result,
                redaction_version=redaction_ver,
            )
            handled += 1
        return handled

    def _execute_one(self, req: ReadonlyDispatchRequest) -> tuple[ToolResult, str | None]:
        try:
            tool = self._registry.get(req.tool_name)
        except Exception:  # noqa: BLE001
            return (
                _error_result(
                    req.tool_name,
                    status=ToolStatus.DENIED,
                    code="TOOL_NOT_FOUND",
                    message=f"未知只读工具：{req.tool_name}",
                ),
                None,
            )
        definition = getattr(tool, "definition", None)
        if definition is None or definition.side_effect != "read":
            return (
                _error_result(
                    req.tool_name,
                    status=ToolStatus.DENIED,
                    code="WRITE_TOOL_FORBIDDEN",
                    message="只读派发通道拒绝写工具",
                ),
                None,
            )
        try:
            ctx = ToolContext(client=self._ro_client, task_id=req.task_id)
            raw_result = tool.run(ctx, dict(req.params))
        except Exception as exc:  # noqa: BLE001
            return (
                _error_result(
                    req.tool_name,
                    status=ToolStatus.ERROR,
                    code="TOOL_EXEC_ERROR",
                    message=str(exc),
                ),
                None,
            )
        if self._gate is not None and raw_result.data is not None:
            scan = self._gate.scan_event(
                dict(raw_result.data),
                task_id=req.task_id,
                tenant_id=req.tenant_id,
            )
            if scan.status not in (RedactionStatus.CLEAN, RedactionStatus.REDACTED):
                return (
                    _error_result(
                        req.tool_name,
                        status=ToolStatus.DENIED,
                        code="REDACTION_BLOCKED",
                        message="只读工具输出未通过脱敏闸门，禁止回传 API",
                    ),
                    scan.redaction_version,
                )
            sanitized = raw_result.model_copy(update={"data": dict(scan.payload_redacted)})
            return sanitized, scan.redaction_version
        return raw_result, None


__all__ = [
    "ReadonlyDispatchQueue",
    "ReadonlyDispatchRequest",
    "RemoteReadonlyTool",
    "WorkerReadonlyDispatcher",
    "build_remote_readonly_registry",
]
