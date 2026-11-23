"""API 统一错误结构与 trace_id 辅助。

错误响应固定为：
    {"status": "error", "error": {"error_code", "message", "retryable",
     "trace_id", "details"}}
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.errors import DBGuardianError, ErrorCode

TRACE_ID_HEADER = "X-Trace-Id"

_RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.DB_UNAVAILABLE.value,
        ErrorCode.DB_TIMEOUT.value,
        ErrorCode.MODEL_UNAVAILABLE.value,
        ErrorCode.MODEL_TIMEOUT.value,
    }
)


def new_trace_id() -> str:
    return f"trace_{uuid4().hex}"


def get_trace_id(request: Request) -> str:
    trace_id = getattr(request.state, "trace_id", None)
    if not trace_id:
        trace_id = new_trace_id()
        request.state.trace_id = trace_id
    return str(trace_id)


def is_retryable(error_code: ErrorCode | str) -> bool:
    value = error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
    return value in _RETRYABLE_ERROR_CODES


def _error_body(
    error_code: str, message: str, retryable: bool, trace_id: str, details: dict[str, Any]
) -> dict[str, Any]:
    return {
        "status": "error",
        "error": {
            "error_code": error_code,
            "message": message,
            "retryable": retryable,
            "trace_id": trace_id,
            "details": details,
        },
    }


def error_response(error: DBGuardianError, request: Request) -> JSONResponse:
    trace_id = get_trace_id(request)
    return JSONResponse(
        status_code=error.status_code,
        content=_error_body(
            error.code.value,
            error.message,
            is_retryable(error.code),
            trace_id,
            error.details,
        ),
        headers={TRACE_ID_HEADER: trace_id},
    )


def validation_error_response(
    exc: RequestValidationError, request: Request
) -> JSONResponse:
    trace_id = get_trace_id(request)
    errors = [
        {
            "loc": list(item.get("loc", ())),
            "msg": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body(
            ErrorCode.REQUEST_INVALID.value,
            "请求参数校验失败",
            is_retryable(ErrorCode.REQUEST_INVALID),
            trace_id,
            {"errors": errors},
        ),
        headers={TRACE_ID_HEADER: trace_id},
    )


__all__ = [
    "TRACE_ID_HEADER",
    "error_response",
    "get_trace_id",
    "is_retryable",
    "new_trace_id",
    "validation_error_response",
]
