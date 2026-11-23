"""P2-T3：LAB 的第二/第三个受控 R2 修复动作（订单状态迁移）。

为什么要两个工具而不是一个：**补偿必须是独立审批的 R2 动作**（W07/W08 口径）。
若只给一个"可正可反"的工具，反向迁移就成了同一审批下的自由参数，等于给了
"任意改状态"的能力。因此拆成：

| 工具 | 允许的迁移 | 用途 |
|---|---|---|
| `mysql_lab_advance_order_status` | pending→paid / pending→cancelled / paid→refunded | 修复动作本体 |
| `mysql_lab_revert_order_status` | paid→pending / cancelled→pending / refunded→paid | 补偿；必须新 Prepare + 新审批 |

共同约束（与 `lab_repair.py` 的 note 工具一致）：

- 服务端硬编码库/表/列与主键区间，不接受任意 SQL / 表名 / 列名；
- 只改 `dg_lab.seed_orders` 的单行 `status`，`LIMIT 1`，并用 `<=>` 复核基线；
- 目标对象 keys 必须与主键同源；写连接必须可写；
- 执行前 `preflight` 只读复核：行存在、状态等于 `expected_status`、迁移在允许集合内；
- 反向工具**只**覆盖正向迁移的逆，不构成"任意状态可写"。
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Literal

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

ALLOWED_DATABASE = "dg_lab"
ALLOWED_TABLE = "seed_orders"
ALLOWED_COLUMN = "status"
MIN_ORDER_ID = 2001
MAX_ORDER_ID = 2200

ALLOWED_STATUSES: tuple[str, ...] = ("pending", "paid", "cancelled", "refunded")

ADVANCE_TOOL_NAME = "mysql_lab_advance_order_status"
REVERT_TOOL_NAME = "mysql_lab_revert_order_status"
TOOL_VERSION = "1.0"

#: 正向迁移（修复）。方向敏感：不接受 paid→pending 这类回退。
FORWARD_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {("pending", "paid"), ("pending", "cancelled"), ("paid", "refunded")}
)
#: 补偿迁移（恰好是正向迁移的逆，逐条显式列出，不做 `not in forward` 的推断）。
REVERT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {("paid", "pending"), ("cancelled", "pending"), ("refunded", "paid")}
)

_SELECT_STATUS = (
    f"SELECT {ALLOWED_COLUMN} FROM {ALLOWED_DATABASE}.{ALLOWED_TABLE} "
    "WHERE order_id = %s LIMIT 1"
)
_UPDATE_STATUS = (
    f"UPDATE {ALLOWED_DATABASE}.{ALLOWED_TABLE} SET {ALLOWED_COLUMN} = %s "
    f"WHERE order_id = %s AND {ALLOWED_COLUMN} <=> %s LIMIT 1"
)

StatusLiteral = Literal["pending", "paid", "cancelled", "refunded"]


class OrderStatusUpdateInput(BaseModel):
    """固定参数 Schema：字段集封闭，任何多余字段都会导致拒绝。"""

    model_config = ConfigDict(extra="forbid")

    database: Literal["dg_lab"] = ALLOWED_DATABASE
    table: Literal["seed_orders"] = ALLOWED_TABLE
    order_id: int = Field(ge=MIN_ORDER_ID, le=MAX_ORDER_ID)
    keys: str = Field(min_length=1, max_length=64)
    expected_status: StatusLiteral
    target_status: StatusLiteral


class _OrderStatusTool:
    """两个状态工具的公共实现；差异只有 ``name`` 与允许的迁移集合。"""

    name: ClassVar[str]
    transitions: ClassVar[frozenset[tuple[str, str]]]
    description: ClassVar[str]
    compensation_action: ClassVar[str]

    def __init__(self) -> None:
        self.input_model = OrderStatusUpdateInput
        self._definition = ToolDefinition(
            name=self.name,
            description=self.description,
            version=TOOL_VERSION,
            risk_level="R2",
            side_effect="write",
            permissions=["mysql:write"],
            timeout_ms=5000,
            idempotent=True,
            audit_policy="metadata",
            input_schema=OrderStatusUpdateInput.model_json_schema(),
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
            prepare_required=True,
            preflight_required=True,
            max_affected_rows=1,
            compensation=CompensationSpec(
                action=self.compensation_action,
                description=(
                    f"用 {self.compensation_action} 把 status 写回执行前的值；"
                    "补偿是独立 R2 动作，需新的 Prepare、新的 approval_id 与新审批，不称为回滚"
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
                    assertion_id="target_status_final",
                    metric=f"{ALLOWED_DATABASE}.{ALLOWED_TABLE}.{ALLOWED_COLUMN}",
                    expected="$params.target_status",
                    tolerance="exact",
                    source="target_readback",
                ),
                VerificationAssertion(
                    assertion_id="snapshot_comparable",
                    metric="before_after_snapshot",
                    expected=(
                        "before==$params.expected_status && after==$params.target_status"
                    ),
                    tolerance="exact",
                    source="step_observed",
                ),
            ],
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    # ---- 公共校验 --------------------------------------------------------
    def _validate(
        self, params: dict[str, Any] | None
    ) -> tuple[OrderStatusUpdateInput | None, ToolError | None]:
        try:
            request = OrderStatusUpdateInput.model_validate(params or {})
        except ValidationError:
            return None, ToolError(
                code=ErrorCode.REQUEST_INVALID.value,
                message="工具入参校验失败或含非白名单字段",
                details={"tool_name": self.name},
            )
        if request.database != ALLOWED_DATABASE or request.table != ALLOWED_TABLE:
            return None, ToolError(
                code="TARGET_NOT_ALLOWED",
                message="目标库或表不在白名单内",
                details={"tool_name": self.name},
            )
        if request.keys != f"order_id={request.order_id}":
            return None, ToolError(
                code="TARGET_KEY_MISMATCH",
                message="目标对象 keys 与主键不一致，禁止执行（批准对象与执行对象必须同源）",
                details={"tool_name": self.name},
            )
        if (request.expected_status, request.target_status) not in self.transitions:
            return None, ToolError(
                code="TRANSITION_NOT_ALLOWED",
                message=(
                    f"不允许的迁移 {request.expected_status}→{request.target_status}；"
                    f"本工具允许 {sorted(self.transitions)}"
                ),
                details={"tool_name": self.name},
            )
        return request, None

    # ---- preflight -------------------------------------------------------
    def preflight(
        self, context: ToolContext, params: dict[str, Any] | None = None
    ) -> PreflightEstimate:
        """只读 dry-run：行存在、基线一致且迁移合法才是"可写 1 行"。"""
        request, error = self._validate(params)
        if request is None:
            return PreflightEstimate(affected_rows=0, detail=f"入参不可执行：{error.code}")
        if getattr(context.client, "read_only", True):
            return PreflightEstimate(affected_rows=0, detail="只读连接不可写")
        rows = context.client.fetch_all(_SELECT_STATUS, (request.order_id,))
        if not rows:
            return PreflightEstimate(affected_rows=0, detail="目标行不存在")
        before = rows[0].get(ALLOWED_COLUMN)
        if before != request.expected_status:
            return PreflightEstimate(
                affected_rows=0, detail="基线已变化（PREREQUISITE_CHANGED）"
            )
        return PreflightEstimate(affected_rows=1, detail="命中 1 行且基线一致")

    # ---- run -------------------------------------------------------------
    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        started = time.perf_counter()
        request, error = self._validate(params)
        if request is None:
            return self._result(context, started, ToolStatus.DENIED, {}, error)
        if getattr(context.client, "read_only", True):
            return self._result(
                context,
                started,
                ToolStatus.DENIED,
                {},
                ToolError(
                    code="READ_ONLY_CONNECTION",
                    message="写操作必须使用可写账号连接",
                    details={"tool_name": self.name},
                ),
            )

        before_rows = context.client.fetch_all(_SELECT_STATUS, (request.order_id,))
        if not before_rows:
            return self._denied(context, started, "TARGET_NOT_FOUND", "目标行不存在")
        before = before_rows[0].get(ALLOWED_COLUMN)
        if before != request.expected_status:
            return self._denied(
                context,
                started,
                "PREREQUISITE_CHANGED",
                "执行前基线已变化，旧审批失效，必须重新 Prepare 并重新审批",
            )

        affected = int(
            context.client.execute(
                _UPDATE_STATUS,
                (request.target_status, request.order_id, request.expected_status),
            )
        )
        after_rows = context.client.fetch_all(_SELECT_STATUS, (request.order_id,))
        after = after_rows[0].get(ALLOWED_COLUMN) if after_rows else None

        if affected == 0:
            if after == request.target_status:
                return self._result(
                    context,
                    started,
                    ToolStatus.SUCCESS,
                    {
                        "before": before,
                        "after": after,
                        "affected_rows": 0,
                        "idempotent_noop": True,
                    },
                )
            return self._denied(
                context,
                started,
                "PREREQUISITE_CHANGED",
                "对象在两次读取之间发生变化，未执行任何更新",
            )
        return self._result(
            context,
            started,
            ToolStatus.SUCCESS,
            {
                "before": before,
                "after": after,
                "affected_rows": affected,
                "idempotent_noop": False,
            },
        )

    # ---- 结果构造 --------------------------------------------------------
    def _denied(
        self, context: ToolContext, started: float, code: str, message: str
    ) -> ToolResult:
        return self._result(
            context,
            started,
            ToolStatus.DENIED,
            {},
            ToolError(code=code, message=message, details={"tool_name": self.name}),
        )

    def _result(
        self,
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
            tool_name=self.name,
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


class LabOrderStatusAdvanceTool(_OrderStatusTool):
    name = ADVANCE_TOOL_NAME
    transitions = FORWARD_TRANSITIONS
    compensation_action = REVERT_TOOL_NAME
    description = (
        "在隔离 LAB 中把 seed_orders 单行的 status 按固定状态机前进一步"
        "（pending→paid / pending→cancelled / paid→refunded）；"
        "需人工审批、带执行前基线校验，不提供通用 SQL 写能力。"
    )


class LabOrderStatusRevertTool(_OrderStatusTool):
    name = REVERT_TOOL_NAME
    transitions = REVERT_TRANSITIONS
    compensation_action = ADVANCE_TOOL_NAME
    description = (
        "补偿专用：把 seed_orders 单行 status 按固定状态机回退一步"
        "（paid→pending / cancelled→pending / refunded→paid）；"
        "必须新 Prepare + 新审批，不得复用原审批。"
    )


def build_status_repair_tools() -> tuple[_OrderStatusTool, _OrderStatusTool]:
    return (LabOrderStatusAdvanceTool(), LabOrderStatusRevertTool())


__all__ = [
    "ADVANCE_TOOL_NAME",
    "ALLOWED_STATUSES",
    "FORWARD_TRANSITIONS",
    "LabOrderStatusAdvanceTool",
    "LabOrderStatusRevertTool",
    "OrderStatusUpdateInput",
    "REVERT_TOOL_NAME",
    "REVERT_TRANSITIONS",
    "build_status_repair_tools",
]
