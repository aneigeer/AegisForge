"""PostgreSQL MCP 子进程客户端。

DSN 只通过子进程环境变量传递，不出现在命令行、日志或异常消息中。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from adapters.base import AdapterToolSpec, CapabilityReport, CapabilityStatus, DbEngine
from adapters.postgres_mcp.protocol import PROTOCOL_VERSION
from adapters.postgres_mcp.tools import ADAPTER_VERSION, INTERNAL_CAPABILITIES_TOOL, TOOL_DEFINITIONS
from core.tools.contracts import ToolContext, ToolError, ToolResult, ToolSource, ToolStatus

_SERVER_EXITED = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresMcpClient:
    """启动 `adapters.postgres_mcp.server` 并通过 stdio 调用固定只读工具。"""

    def __init__(
        self,
        dsn: str | SecretStr | None = None,
        *,
        python_executable: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须大于 0")
        raw_dsn = dsn.get_secret_value() if isinstance(dsn, SecretStr) else dsn
        if raw_dsn is None:
            raw_dsn = os.environ.get("DG_PG_RO_DSN")
        if not raw_dsn or not str(raw_dsn).strip():
            raise ValueError("未提供 PostgreSQL 只读 DSN，且环境变量 DG_PG_RO_DSN 为空")
        self._dsn = str(raw_dsn).strip()
        self._python_executable = python_executable or sys.executable
        self._timeout_s = float(timeout_s)
        self.engine = DbEngine.POSTGRESQL
        self.adapter_version = ADAPTER_VERSION
        self.database_id = os.environ.get("DG_PG_DATABASE_ID", "lab_pg_01")
        self.read_only = True
        self._messages: queue.Queue[object] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._start()
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def __repr__(self) -> str:
        return f"PostgresMcpClient(database=None, pid={self.pid})"

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        return (
            not self._closed
            and self._process is not None
            and self._process.poll() is None
        )

    def _project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _start(self) -> None:
        root = self._project_root()
        env = os.environ.copy()
        env["DG_PG_RO_DSN"] = self._dsn
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(root) + (os.pathsep + existing if existing else "")
        try:
            self._process = subprocess.Popen(
                [self._python_executable, "-m", "adapters.postgres_mcp.server"],
                cwd=str(root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"无法启动 PostgreSQL MCP Server：{type(exc).__name__}") from None

        self._stdout_thread = threading.Thread(
            target=self._pump_stdout, name="pg-mcp-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr, name="pg-mcp-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._messages.put(_SERVER_EXITED)
            return
        try:
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                self._messages.put(payload)
        except (ValueError, OSError):
            pass
        finally:
            self._messages.put(_SERVER_EXITED)

    def _pump_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._stderr_lines.append(line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def _stderr_tail(self) -> str:
        return " | ".join(list(self._stderr_lines)[-5:])

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("MCP Server 未启动")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError(f"MCP Server 已退出：{self._stderr_tail()}") from None

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("PostgresMcpClient 已关闭")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = params
            self._write(payload)

            deadline = time.monotonic() + self._timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._abort()
                    raise TimeoutError(f"MCP 请求超时：{method}")
                try:
                    item = self._messages.get(timeout=remaining)
                except queue.Empty:
                    self._abort()
                    raise TimeoutError(f"MCP 请求超时：{method}") from None
                if item is _SERVER_EXITED:
                    raise RuntimeError(f"MCP Server 已退出：{self._stderr_tail()}")
                if isinstance(item, dict) and item.get("id") == request_id:
                    return item

    def _initialize(self) -> None:
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "dbguardian-postgres-mcp-client", "version": ADAPTER_VERSION},
            },
        )
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(f"initialize 失败：{error.get('message', 'unknown')}")
        result = response.get("result") or {}
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise RuntimeError("MCP Server 协议版本不兼容")
        self._notify("notifications/initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        response = self._request("tools/list", {})
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(f"tools/list 失败：{error.get('message', 'unknown')}")
        tools = (response.get("result") or {}).get("tools") or []
        return [item for item in tools if isinstance(item, dict)]

    def _fallback_result(self, name: str, *, code: str, message: str, data: dict[str, Any] | None = None) -> ToolResult:
        now = _now_iso()
        return ToolResult(
            tool_name=name,
            status=ToolStatus.ERROR,
            started_at=now,
            duration_ms=0.0,
            data=data or {},
            error=ToolError(code=code, message=message),
            source=ToolSource(
                database_id="unknown",
                engine="postgresql",
                db_version="unknown",
                collected_at=now,
                adapter_version=ADAPTER_VERSION,
            ),
            observed_at=now,
            capability_ref=None,
            source_metadata={"adapter": "postgres-mcp"},
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        if name != INTERNAL_CAPABILITIES_TOOL and name not in TOOL_DEFINITIONS:
            result = self._fallback_result(
                name,
                code="TOOL_NOT_FOUND",
                message="未知的 MCP 工具，未执行任何 SQL",
            )
            result.status = ToolStatus.DENIED
            if context is not None:
                result.task_id = context.task_id
                result.run_id = context.run_id
                result.tool_call_id = context.tool_call_id
            return result

        response = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in response:
            error = response.get("error") or {}
            result = self._fallback_result(
                name,
                code="MCP_ERROR",
                message=str(error.get("message") or "MCP 调用失败"),
                data={"rpc_error_code": error.get("code")},
            )
        else:
            payload = response.get("result") or {}
            content = payload.get("content") or []
            text = ""
            if content and isinstance(content[0], dict):
                text = str(content[0].get("text") or "")
            try:
                result = ToolResult.model_validate_json(text)
            except Exception:  # noqa: BLE001 - 非 ToolResult 载荷统一结构化为错误
                result = self._fallback_result(
                    name,
                    code="INVALID_RESPONSE",
                    message="MCP 工具结果不是有效的 ToolResult",
                    data={"is_error": bool(payload.get("isError"))},
                )

        if context is not None:
            result.task_id = context.task_id
            result.run_id = context.run_id
            result.tool_call_id = context.tool_call_id
        return result

    def identity(self):
        """Executor 错误路径需要的只读身份；正常工具调用不触发。"""
        from database.postgres.connection import PgClient

        return PgClient(
            self._dsn,
            database_id=self.database_id,
            read_only=True,
        ).identity()

    def tool_specs(self) -> list[AdapterToolSpec]:
        specs: list[AdapterToolSpec] = []
        for item in self.list_tools():
            name = item.get("name")
            definition = TOOL_DEFINITIONS.get(str(name))
            if definition is None:
                continue
            specs.append(AdapterToolSpec(definition=definition, engine=DbEngine.POSTGRESQL))
        return specs

    def probe_capabilities(self) -> CapabilityReport:
        checked_at = _now_iso()
        result = self.call_tool(INTERNAL_CAPABILITIES_TOOL, {})
        raw = result.data.get("capabilities") if isinstance(result.data, dict) else None
        raw_caps = raw if isinstance(raw, dict) else {}
        capabilities: dict[str, CapabilityStatus] = {}
        for key in (
            "pg_stat_activity",
            "pg_locks",
            "pg_blocking_pids",
            "pg_stat_statements",
        ):
            try:
                capabilities[key] = CapabilityStatus(str(raw_caps.get(key, "UNKNOWN")))
            except ValueError:
                capabilities[key] = CapabilityStatus.UNKNOWN
        warnings = list(result.warnings)
        if result.error is not None:
            warnings.append(f"{result.error.code}: {result.error.message}")
        return CapabilityReport(
            engine=DbEngine.POSTGRESQL,
            database_id=result.source.database_id,
            server_version=str(result.data.get("server_version") or result.source.db_version),
            adapter_version=ADAPTER_VERSION,
            checked_at=result.observed_at or checked_at,
            capabilities=capabilities,
            warnings=warnings,
        )

    def _abort(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)

    def __enter__(self) -> "PostgresMcpClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


__all__ = ["PostgresMcpClient"]
