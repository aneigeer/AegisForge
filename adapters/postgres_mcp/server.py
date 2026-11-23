"""PostgreSQL 只读 MCP Server（stdio，JSON-RPC 2.0 按行分隔）。

stdout 只输出 JSON-RPC 响应；诊断日志一律写 stderr，且绝不输出 DSN/密码。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from pydantic import SecretStr, ValidationError

from adapters.postgres_mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    InitializeResult,
    JsonRpcRequest,
    McpCallToolResult,
    McpContent,
    ServerInfo,
    error_response,
)
from adapters.postgres_mcp.tools import ADAPTER_VERSION, execute_tool, tool_list_payload
from core.tools.contracts import ToolStatus
from database.postgres.connection import PgClient

logger = logging.getLogger("dbguardian.postgres_mcp.server")

_IS_ERROR_STATUSES = {
    ToolStatus.ERROR,
    ToolStatus.TIMEOUT,
    ToolStatus.DENIED,
    ToolStatus.NOT_SUPPORTED,
    ToolStatus.UNKNOWN_OUTCOME,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rpc_result(request_id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(
    request_id: int | str | None,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return error_response(request_id, code, message, data=data).model_dump(
        exclude_none=True
    )


class PostgresMcpServer:
    """协议处理与固定工具分发；不暴露任意 SQL。"""

    def __init__(
        self,
        client: PgClient,
        *,
        database_id: str,
        checked_at: str | None = None,
    ) -> None:
        self._client = client
        self._database_id = database_id
        self._checked_at = checked_at or _now_iso()
        self._db_version: str | None = None

    @property
    def capability_ref(self) -> str:
        return f"postgresql:{ADAPTER_VERSION}:{self._checked_at}"

    def _server_version(self) -> str:
        if self._db_version is None:
            try:
                self._db_version = self._client.identity().server_version
            except Exception as exc:  # noqa: BLE001 - 启动后 DB 不可用时仍需稳定响应
                logger.warning("无法获取 PostgreSQL 版本：%s", type(exc).__name__)
                self._db_version = "unknown"
        return self._db_version

    def handle_line(self, line: str) -> dict[str, Any] | None:
        if not line.strip():
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return _rpc_error(None, PARSE_ERROR, "Parse error")
        if not isinstance(raw, dict):
            return _rpc_error(None, INVALID_REQUEST, "Invalid Request")

        has_id = "id" in raw
        request_id = raw.get("id") if has_id else None
        try:
            request = JsonRpcRequest.model_validate(raw)
        except ValidationError:
            return _rpc_error(request_id, INVALID_REQUEST, "Invalid Request")

        if not has_id:
            # notifications/initialized 以及其它通知都不产生响应。
            return None

        try:
            return self._dispatch(request, request_id)
        except Exception as exc:  # noqa: BLE001 - 协议层兜底，不泄漏细节
            logger.error("处理请求失败：%s", type(exc).__name__)
            return _rpc_error(request_id, -32603, "Internal error")

    def _dispatch(
        self, request: JsonRpcRequest, request_id: int | str | None
    ) -> dict[str, Any] | None:
        method = request.method

        if method == "initialize":
            result = InitializeResult(
                protocolVersion=PROTOCOL_VERSION,
                capabilities={"tools": {}},
                serverInfo=ServerInfo(name=SERVER_NAME, version=SERVER_VERSION),
            )
            return _rpc_result(request_id, result.to_payload())

        if method == "notifications/initialized":
            return _rpc_result(request_id, {})

        if method == "tools/list":
            return _rpc_result(request_id, {"tools": tool_list_payload()})

        if method == "tools/call":
            return self._call_tool(request, request_id)

        if method == "notifications/cancelled":
            return _rpc_result(request_id, {})

        return _rpc_error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _call_tool(
        self, request: JsonRpcRequest, request_id: int | str | None
    ) -> dict[str, Any]:
        params = request.params or {}
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _rpc_error(request_id, INVALID_PARAMS, "Missing or invalid tool name")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _rpc_error(request_id, INVALID_PARAMS, "arguments must be an object")

        result = execute_tool(
            name,
            arguments,
            client=self._client,
            database_id=self._database_id,
            db_version=self._server_version(),
            capability_ref=self.capability_ref,
            include_internal=True,
        )
        payload = McpCallToolResult(
            content=[McpContent(type="text", text=result.model_dump_json())],
            isError=result.status in _IS_ERROR_STATUSES,
        )
        return _rpc_result(request_id, payload.to_payload())


def _build_client() -> tuple[PgClient, str] | None:
    dsn = os.environ.get("DG_PG_RO_DSN", "").strip()
    if not dsn:
        logger.error("缺少环境变量 DG_PG_RO_DSN，PostgreSQL MCP Server 拒绝启动")
        return None
    database_id = os.environ.get("DG_PG_DATABASE_ID", "lab_pg_01").strip() or "lab_pg_01"
    try:
        client = PgClient(
            SecretStr(dsn),
            database_id=database_id,
            read_only=True,
            connect_timeout_s=5.0,
            statement_timeout_ms=30_000,
            max_result_rows=500,
        )
    except Exception as exc:  # noqa: BLE001 - 只输出错误类型，绝不回显 DSN
        logger.error("DG_PG_RO_DSN 无效：%s", type(exc).__name__)
        return None
    return client, database_id


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("DG_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    built = _build_client()
    if built is None:
        return 2
    client, database_id = built
    server = PostgresMcpServer(client, database_id=database_id)
    logger.info("PostgreSQL MCP Server 已启动（database_id=%s）", database_id)

    for line in sys.stdin:
        response = server.handle_line(line)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["PostgresMcpServer", "main"]
