"""W07：MySQL LAB 唯一受限 R2 写工具（按主键更新单行 note）。

边界（对应 docs/w07/00-action-spec.md 与 ADR-0007）：
- 服务端硬编码目标白名单，不接受任意 SQL / host / port / 表名 / 列名；
- 只更新 dg_lab.seed_orders 的 note 列，恰好一行，LIMIT 1；
- 执行前基线 expected_note 参与摘要，并在 SQL 层用 <=> 复核；
- 前置条件变化返回 DENIED / PREREQUISITE_CHANGED，绝不猜测执行结果；
- 不宣称 Exactly Once：重复执行且结果已达成时标记幂等空操作。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.errors import ErrorCode
from core.tools.contracts import (
    CompensationSpec,
    PreflightEstimate,
    TargetObjectSpec,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolSource,
    ToolStatus,
    VerificationAssertion,
)

TOOL_NAME = "mysql_lab_update_order_note"
TOOL_VERSION = "1.0"

ALLOWED_DATABASE = "dg_lab"
ALLOWED_TABLE = "seed_orders"
ALLOWED_COLUMN = "note"
MIN_ORDER_ID = 2001
MAX_ORDER_ID = 2200
MAX_NOTE_LENGTH = 128

_SELECT_NOTE = (
    f"SELECT {ALLOWED_COLUMN} FROM {ALLOWED_DATABASE}.{ALLOWED_TABLE} "
    "WHERE order_id = %s LIMIT 1"
)
_UPDATE_NOTE = (
    f"UPDATE {ALLOWED_DATABASE}.{ALLOWED_TABLE} SET {ALLOWED_COLUMN} = %s "
    f"WHERE order_id = %s AND {ALLOWED_COLUMN} <=> %s LIMIT 1"
)


class OrderNoteUpdateInput(BaseModel):
    """固定参数 Schema：字段集封闭，任何多余字段都会导致拒绝。"""

    model_config = ConfigDict(extra="forbid")

    database: Literal["dg_lab"] = ALLOWED_DATABASE
    table: Literal["seed_orders"] = ALLOWED_TABLE
    order_id: int = Field(ge=MIN_ORDER_ID, le=MAX_ORDER_ID)
    keys: str = Field(min_length=1, max_length=64)
    note: str = Field(max_length=MAX_NOTE_LENGTH)
    expected_note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class LabOrderNoteUpdateTool:
    """单一 R2 动作；只在 MySQL LAB 的可写账号下装配。"""

    def __init__(self) -> None:
        self.input_model = OrderNoteUpdateInput
        self._definition = ToolDefinition(
            name=TOOL_NAME,
            description=(
                "在隔离 LAB 中按主键更新 seed_orders 单行的 note 列；"
                "需人工审批、带执行前基线校验，不提供通用 SQL 写能力。"
            ),
            version=TOOL_VERSION,
            risk_level="R2",
            side_effect="write",
            permissions=["mysql:write"],
            timeout_ms=5000,
            idempotent=True,
            audit_policy="metadata",
            input_schema=OrderNoteUpdateInput.model_json_schema(),
            output_schema={
                "type": "object",
                "properties": {
                    "before": {"type": ["string", "null"]},
                    "after": {"type": ["string", "null"]},
                    "affected_rows": {"type": "integer"},
                    "idempotent_noop": {"type": "boolean"},
                },
            },
            db_engines=["mysql"],
            allowed_environments=["LAB"],
            required_role="dg_rw",
            # 能力矩阵既有键；缺失即 ⑧ tool_capabilities FAIL（W07 D2 检查）。
            required_capabilities=["optimistic_update"],
            operation_class="repair",
            target_objects=[
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="table",
                    keys_field="keys",
                    required=True,
                )
            ],
            # W07：启用 9 类 D2 前置条件检查（探针缺失即 UNKNOWN 阻断）。
            prepare_required=True,
            # P2-T3：单行更新 → 爆炸半径上限 1；执行前用只读 SELECT 做 dry-run。
            preflight_required=True,
            max_affected_rows=1,
            # W07 §4.2：补偿方案与验证计划是"唯一动作声明"的一部分，不由文案解释。
            compensation=CompensationSpec(
                action=TOOL_NAME,
                description=(
                    "把 note 写回执行前 before.note；补偿是独立 R2 动作，"
                    "需新的 Prepare、新的 approval_id 与新审批，不称为回滚"
                ),
                is_rollback=False,
                requires_own_approval=True,
            ),
            verification_plan=[
                VerificationAssertion(
                    assertion_id="affected_rows",
                    metric="affected_rows",
                    expected="1",
                    tolerance="exact",
                    source="step_observed",
                ),
                VerificationAssertion(
                    assertion_id="target_note_final",
                    metric=f"{ALLOWED_DATABASE}.{ALLOWED_TABLE}.{ALLOWED_COLUMN}",
                    expected="$params.note",
                    tolerance="exact",
                    source="target_readback",
                ),
                VerificationAssertion(
                    assertion_id="snapshot_comparable",
                    metric="before_after_snapshot",
                    expected="before==$params.expected_note && after==$params.note",
                    tolerance="exact",
                    source="step_observed",
                ),
            ],
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        started = time.perf_counter()
        try:
            request = OrderNoteUpdateInput.model_validate(params or {})
        except ValidationError:
            return self._result(
                context, started, ToolStatus.DENIED, {},
                ToolError(
                    code=ErrorCode.REQUEST_INVALID.value,
                    message="工具入参校验失败或含非白名单字段",
                    details={"tool_name": TOOL_NAME},
                ),
            )

        # 服务端二次校验：即使审批已通过，也不信任请求里携带的目标描述。
        if request.database != ALLOWED_DATABASE or request.table != ALLOWED_TABLE:
            return self._denied(context, started, "TARGET_NOT_ALLOWED", "目标库或表不在白名单内")
        if request.keys != f"order_id={request.order_id}":
            return self._denied(
                context, started, "TARGET_KEY_MISMATCH",
                "目标对象 keys 与主键不一致，禁止执行（批准对象与执行对象必须同源）",
            )
        if not MIN_ORDER_ID <= request.order_id <= MAX_ORDER_ID:
            return self._denied(context, started, "TARGET_OUT_OF_RANGE", "主键超出允许范围")
        if getattr(context.client, "read_only", True):
            return self._denied(context, started, "READ_ONLY_CONNECTION", "写操作必须使用可写账号连接")
        if len(request.note) > MAX_NOTE_LENGTH:
            return self._denied(context, started, "NOTE_TOO_LONG", "note 超出长度上限")

        before_rows = context.client.fetch_all(_SELECT_NOTE, (request.order_id,))
        if not before_rows:
            return self._denied(context, started, "TARGET_NOT_FOUND", "目标行不存在")
        before = before_rows[0].get(ALLOWED_COLUMN)
        if before != request.expected_note:
            return self._denied(
                context, started, "PREREQUISITE_CHANGED",
                "执行前基线已变化，旧审批失效，必须重新 Prepare 并重新审批",
            )

        affected = int(
            context.client.execute(
                _UPDATE_NOTE, (request.note, request.order_id, request.expected_note)
            )
        )
        after_rows = context.client.fetch_all(_SELECT_NOTE, (request.order_id,))
        after = after_rows[0].get(ALLOWED_COLUMN) if after_rows else None

        if affected == 0:
            if after == request.note:
                # 幂等重放：结果已达成，不算失败，也不重复计数。
                return self._result(
                    context, started, ToolStatus.SUCCESS,
                    {
                        "before": before,
                        "after": after,
                        "affected_rows": 0,
                        "idempotent_noop": True,
                    },
                )
            return self._denied(
                context, started, "PREREQUISITE_CHANGED",
                "对象在两次读取之间发生变化，未执行任何更新",
            )

        return self._result(
            context, started, ToolStatus.SUCCESS,
            {
                "before": before,
                "after": after,
                "affected_rows": affected,
                "idempotent_noop": False,
            },
        )

    def preflight(
        self, context: ToolContext, params: dict[str, Any] | None = None
    ) -> PreflightEstimate:
        """P2-T3：执行前 dry-run——只读统计"本次写将影响几行"。

        判定口径与 `run` 保持一致：目标行存在且 `note` 仍等于 `expected_note`
        才算"可写 1 行"；任何偏差都返回 0（网关随后以 `PREFLIGHT_NO_TARGET` 拒绝，
        且**不消耗审批**）。
        """
        try:
            request = OrderNoteUpdateInput.model_validate(params or {})
        except ValidationError:
            return PreflightEstimate(affected_rows=0, detail="入参非法")
        if getattr(context.client, "read_only", True):
            return PreflightEstimate(affected_rows=0, detail="只读连接不可写")
        rows = context.client.fetch_all(_SELECT_NOTE, (request.order_id,))
        if not rows:
            return PreflightEstimate(affected_rows=0, detail="目标行不存在")
        before = rows[0].get(ALLOWED_COLUMN)
        if before != request.expected_note:
            return PreflightEstimate(affected_rows=0, detail="基线已变化（PREREQUISITE_CHANGED）")
        return PreflightEstimate(affected_rows=1, detail="命中 1 行且基线一致")

    def _denied(
        self, context: ToolContext, started: float, code: str, message: str
    ) -> ToolResult:
        return self._result(
            context, started, ToolStatus.DENIED, {},
            ToolError(code=code, message=message, details={"tool_name": TOOL_NAME}),
        )

    @staticmethod
    def _result(
        context: ToolContext,
        started: float,
        status: ToolStatus,
        data: dict[str, Any],
        error: ToolError | None = None,
    ) -> ToolResult:
        try:
            db_version = context.identity().server_version
        except Exception:
            db_version = "unknown"
        from database.observers.utils import utc_now_iso

        return ToolResult(
            tool_name=TOOL_NAME,
            status=status,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=utc_now_iso(),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            data=data,
            warnings=[],
            error=error,
            source=ToolSource(
                database_id=context.client.database_id,
                db_version=db_version,
                collected_at=utc_now_iso(),
            ),
        )


__all__ = [
    "ALLOWED_DATABASE",
    "ALLOWED_TABLE",
    "LabOrderNoteUpdateTool",
    "OrderNoteUpdateInput",
    "TOOL_NAME",
]
