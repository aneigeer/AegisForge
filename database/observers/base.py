"""只读观察工具基类：统一入参校验、错误映射、来源信息与耗时。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from core.errors import (
    DBGuardianError,
    DatabasePermissionError,
    DatabaseTimeoutError,
    ErrorCode,
    PolicyDeniedError,
)
from core.logging import get_logger
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from database.observers.utils import utc_now_iso

logger = get_logger(__name__)


class ObserverTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_schema: ClassVar[dict[str, Any]]
    timeout_ms: ClassVar[int] = 30_000

    def __init__(self) -> None:
        self._definition = ToolDefinition(
            name=self.name,
            description=self.description,
            permissions=["mysql:read"],
            timeout_ms=self.timeout_ms,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_schema,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        started_at = utc_now_iso()
        started = time.perf_counter()
        status = ToolStatus.SUCCESS
        error: ToolError | None = None
        warnings: list[str] = []
        data: dict[str, Any] = {}

        try:
            validated = self.input_model.model_validate(params or {})
            data, warnings = self._execute(context, validated)
        except ValidationError as exc:
            status = ToolStatus.ERROR
            error = ToolError(
                code=ErrorCode.REQUEST_INVALID.value,
                message="工具入参校验失败",
                details={
                    "errors": [
                        {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                        for item in exc.errors()
                    ]
                },
            )
        except DatabaseTimeoutError as exc:
            status = ToolStatus.TIMEOUT
            error = ToolError(code=exc.code.value, message=exc.message, details=exc.details)
        except DatabasePermissionError as exc:
            status = ToolStatus.DENIED
            error = ToolError(code=exc.code.value, message=exc.message, details=exc.details)
        except PolicyDeniedError as exc:
            status = ToolStatus.DENIED
            error = ToolError(code=exc.code.value, message=exc.message, details=exc.details)
        except DBGuardianError as exc:
            status = ToolStatus.ERROR
            error = ToolError(code=exc.code.value, message=exc.message, details=exc.details)
        except Exception:
            logger.exception("工具 %s 执行异常", self.name)
            status = ToolStatus.ERROR
            error = ToolError(
                code=ErrorCode.INTERNAL.value,
                message="工具内部错误",
                details={"tool_name": self.name},
            )

        try:
            identity = context.identity()
            db_version = identity.server_version
        except DBGuardianError:
            db_version = "unknown"
        except Exception:
            db_version = "unknown"

        return ToolResult(
            tool_name=self.name,
            status=status,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            data=data,
            warnings=warnings,
            error=error,
            source=ToolSource(
                database_id=context.client.database_id,
                db_version=db_version,
                collected_at=utc_now_iso(),
            ),
        )

    @abstractmethod
    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        """返回 data 与 warnings；数据库错误向上抛出由基类映射。"""
