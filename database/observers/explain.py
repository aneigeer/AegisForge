"""执行计划工具：默认只读 EXPLAIN FORMAT=JSON，禁用 EXPLAIN ANALYZE。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import PolicyDeniedError
from core.tools.contracts import ToolContext
from database.observers.base import ObserverTool
from database.observers.utils import safe_sql_preview, validate_explain_sql

_MAX_PLAN_BYTES = 50_000


class ExplainQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=20_000)
    analyze: bool = False


class ExplainQueryTool(ObserverTool):
    name = "explain_query"
    description = "对单条只读 SELECT/WITH 查询执行 EXPLAIN FORMAT=JSON，不执行 ANALYZE。"
    input_model = ExplainQueryInput
    timeout_ms = 20_000
    output_schema = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "plan": {"type": ["object", "string"]},
            "plan_truncated": {"type": "boolean"},
            "query_preview": {"type": "string"},
            "query_truncated": {"type": "boolean"},
            "analyze": {"type": "boolean"},
        },
    }

    def _execute(self, context: ToolContext, params: BaseModel) -> tuple[dict[str, Any], list[str]]:
        request: ExplainQueryInput = params  # type: ignore[assignment]
        if request.analyze:
            raise PolicyDeniedError(
                "Alpha 版本禁用 EXPLAIN ANALYZE（会真实执行查询）",
                details={"tool_name": self.name},
            )
        ok, reason = validate_explain_sql(request.sql)
        if not ok:
            raise PolicyDeniedError(reason, details={"tool_name": self.name})

        warnings: list[str] = []
        rows = context.client.fetch_all(f"EXPLAIN FORMAT=JSON {request.sql}")
        if not rows:
            return {
                "format": "json",
                "plan": {},
                "plan_truncated": False,
                "query_preview": None,
                "query_truncated": False,
                "analyze": False,
            }, ["EXPLAIN 未返回结果"]

        raw = next(iter(rows[0].values()))
        raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        plan: dict[str, Any] | str
        try:
            parsed = json.loads(raw_text)
            plan = parsed
        except (json.JSONDecodeError, TypeError):
            plan = raw_text
            warnings.append("EXPLAIN 返回内容不是合法 JSON，已返回原文")

        plan_truncated = len(raw_text) > _MAX_PLAN_BYTES
        if plan_truncated:
            plan = raw_text[:_MAX_PLAN_BYTES]
            warnings.append(f"执行计划超过 {_MAX_PLAN_BYTES} 字符，已截断")

        preview, preview_truncated = safe_sql_preview(request.sql, limit=200)
        return {
            "format": "json",
            "plan": plan,
            "plan_truncated": plan_truncated,
            "query_preview": preview,
            "query_truncated": preview_truncated,
            "analyze": False,
        }, warnings
