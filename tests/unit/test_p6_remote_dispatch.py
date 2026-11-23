"""P6-T1 单元测试：API -> 控制库只追加队列 -> Worker 只读执行闭环与隔离守卫。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from adapters.mysql.lab_repair import LabOrderNoteUpdateTool
from core.errors import RequestInvalidError
from core.execution.dispatch_queue import (
    ReadonlyDispatchQueue,
    WorkerReadonlyDispatcher,
    build_remote_readonly_registry,
)
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.tools.contracts import ToolContext, ToolDefinition, ToolResult, ToolSource, ToolStatus
from core.tools.registry import ToolRegistry


class _FakeControlDb:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if "INTO dg_system.readonly_tool_requests" in sql:
            req_id, tenant_id, task_id, engine, tool_name, params_json, requested_by = params
            self.requests.append(
                {
                    "request_id": req_id,
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "engine": engine,
                    "tool_name": tool_name,
                    "params_json": params_json,
                    "requested_by": requested_by,
                }
            )
            return 1
        if "INTO dg_system.readonly_tool_responses" in sql:
            req_id, worker_id, status, result_json, redaction_ver = params
            if req_id not in self.responses:
                self.responses[req_id] = {
                    "request_id": req_id,
                    "worker_id": worker_id,
                    "status": status,
                    "result_json": result_json,
                    "redaction_version": redaction_ver,
                }
                return 1
            return 0
        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM dg_system.readonly_tool_responses" in sql:
            req_id = params[0]
            row = self.responses.get(req_id)
            return [row] if row else []
        if "FROM dg_system.readonly_tool_requests" in sql:
            limit = int(params[0])
            pending = [r for r in self.requests if r["request_id"] not in self.responses]
            return pending[:limit]
        raise AssertionError(f"Unexpected SQL: {sql}")


class _DummyReadTool:
    def __init__(self, name: str, payload: dict[str, Any]) -> None:
        self.definition = ToolDefinition(
            name=name,
            description="dummy read",
            side_effect="read",
            risk_level="R0",
            permissions=["mysql:read"],
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        )
        self._payload = payload

    def run(self, context: ToolContext, params: dict[str, Any]) -> ToolResult:
        return ToolResult(
            tool_name=self.definition.name,
            status=ToolStatus.SUCCESS,
            data=dict(self._payload),
            started_at="2026-11-22T00:00:00+00:00",
            duration_ms=1.0,
            source=ToolSource(
                database_id="lab_mysql_01",
                engine="mysql",
                db_version="8.0.46",
                collected_at="2026-11-22T00:00:00+00:00",
            ),
        )


def test_remote_dispatch_end_to_end_and_redaction() -> None:
    db = _FakeControlDb()
    queue = ReadonlyDispatchQueue(db)
    base = ToolRegistry()
    base.register(_DummyReadTool("get_conn", {"active": 12, "note": "phone 13800138000"}))
    base.register(LabOrderNoteUpdateTool())

    remote = build_remote_readonly_registry(
        queue, base, tenant_id="t1", engine="mysql", timeout_s=2.0
    )
    # 写工具被自动过滤，不进入远程只读注册表
    assert [d.name for d in remote.definitions()] == ["get_conn"]

    dispatcher = WorkerReadonlyDispatcher(
        queue,
        base,
        ro_client=object(),
        worker_id="worker_01",
        redaction_gate=RedactionGate(DataRedactionEngine()),
    )

    def _worker_tick() -> None:
        for _ in range(30):
            if dispatcher.drain_once(limit=5) > 0:
                return
            time.sleep(0.01)

    t = threading.Thread(target=_worker_tick, daemon=True)
    t.start()
    result = remote.get("get_conn").run(ToolContext(client=None, task_id="task_1"), {})
    t.join(timeout=2.0)

    assert result.status is ToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["active"] == 12
    # 手机号已被 Worker 侧脱敏闸门改写，不回传明文给 API
    assert "13800138000" not in json.dumps(result.data)


def test_dispatch_queue_rejects_write_tools_and_times_out_cleanly() -> None:
    db = _FakeControlDb()
    queue = ReadonlyDispatchQueue(db)

    with pytest.raises(RequestInvalidError):
        queue.enqueue(
            tenant_id="t1",
            task_id="task_1",
            tool_name="mysql_lab_update_order_note",
            params={},
            side_effect="write",
        )

    req_id = queue.enqueue(
        tenant_id="t1",
        task_id="task_1",
        tool_name="get_conn",
        params={},
        side_effect="read",
    )
    res = queue.wait_for_response(req_id, tool_name="get_conn", timeout_s=0.05, poll_interval_s=0.01)
    assert res.status is ToolStatus.TIMEOUT
    assert res.error is not None and res.error.code == "WORKER_DISPATCH_TIMEOUT"
