"""Tool 契约：所有工具输出统一为 ToolResult，便于 Planner/Executor/审计复用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.redaction.models import RedactionSummary
from database.mysql.connection import MysqlClient, MysqlIdentity


class ToolStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"
    TIMEOUT = "timeout"
    DENIED = "denied"
    NOT_SUPPORTED = "not_supported"
    UNKNOWN_OUTCOME = "unknown_outcome"


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_id: str
    engine: Literal["mysql", "postgresql"] = "mysql"
    db_version: str
    collected_at: str
    adapter_version: str | None = None


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: ToolStatus
    task_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    started_at: str
    duration_ms: float
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: ToolError | None = None
    source: ToolSource
    observed_at: str | None = None
    truncated: bool = False
    capability_ref: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    # W09 闸门①：脱敏摘要（不含 payload 本体）。**不参与任何摘要计算**：
    # `action_digest` 的输入是 `action_descriptor`（参数/目标对象），不是本模型。
    # 注意：字段虽置于末尾，`model_dump()` 仍会**多出** `redaction: null` 一个键；
    # 当前全仓无调用点对 `ToolResult.model_dump()` 计算哈希（`StepObserved` 落普通
    # `result_json` 列），故无摘要向量漂移。若将来新增此类调用点，须显式排除该键。
    redaction: RedactionSummary | None = None


class TargetObjectSpec(BaseModel):
    """工具契约对"目标对象"的声明（W06 P0-03b）。

    声明的是**从参数到目标对象字段的映射**，而不是具体对象本身：审批摘要中的
    `target_objects` 由 `core/approval/digest.py::extract_target_objects()` 依据本声明
    从已规范化参数中确定性抽取，因此"参数里的目标"与"摘要里的目标"必然同源。

    写工具（`side_effect="write"`）声明为空即 fail closed，不得建立审批。
    """

    model_config = ConfigDict(extra="forbid")

    object_type: Literal["table", "row", "index", "database", "schema"]
    # 参数中承载 database / name / schema / keys 的字段名。
    database_field: str = Field(min_length=1, max_length=64)
    name_field: str = Field(min_length=1, max_length=64)
    schema_field: str | None = Field(default=None, max_length=64)
    keys_field: str | None = Field(default=None, max_length=64)
    required: bool = True


# 面向人字段禁含的措辞（W08 AC-11 / 设计 §8.1）：补偿是独立审批的 R2 动作，
# 不是回滚；检查大小写不敏感，命中即构造期 fail closed。
_FORBIDDEN_COMPENSATION_WORD = "rollback"
# 公开视图里面向人的固定中文标签（不含禁用词）。
_COMPENSATION_LABEL = "补偿方案"


class CompensationSpec(BaseModel):
    """W07：写动作的**补偿**声明（补偿不是回滚，也不是自动回滚）。

    `action` 是补偿调用所使用的工具名。补偿本身是一次独立操作：
    需要新的 Prepare、新的 `approval_id` 与新的审批，因此 `requires_own_approval`
    恒为 True，`is_rollback` 恒为 False（口径见 `docs/w07/00-action-spec.md` §5）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1024)
    is_rollback: bool = False
    requires_own_approval: bool = True

    @model_validator(mode="after")
    def _reject_rollback_wording(self) -> "CompensationSpec":
        """面向人文案禁含 `rollback`（大小写不敏感），构造期 fail closed。

        AC-11 要求所有面向人字段（`label` / `description` / `report_text`）不得把
        补偿描述为回滚；本模型当前唯一的自由文案是 `description`，在此拦住，
        使任何下游公开视图都不可能携带该措辞。
        """
        if _FORBIDDEN_COMPENSATION_WORD in self.description.lower():
            raise ValueError(
                "补偿是独立审批的 R2 动作，面向人文案禁含 rollback（不称为回滚）"
            )
        return self

    def to_public_dict(self) -> dict[str, Any]:
        """面向审批人 / 报告的公开视图（W08 T01 / AC-11）。

        `label` 作为展示层标签在此产出，**不新增模型结构化字段**——因此
        `CompensationSpec` 字段集与披露快照口径（`action_digest` 字段集）保持不变。
        `label` 为固定中文常量、`description` 已由构造期校验保证，二者均不含
        `rollback`。
        """
        return {
            "label": _COMPENSATION_LABEL,
            "action": self.action,
            "description": self.description,
            "is_rollback": self.is_rollback,
            "requires_own_approval": self.requires_own_approval,
        }


class VerificationAssertion(BaseModel):
    """W07：写动作的**执行后验证**单条断言（W07 只披露，不执行比对）。

    `expected` 允许 `$params.*` 占位，供 W08 Verifier 解析；W07 只把它展示给审批人。
    """

    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    metric: str = Field(min_length=1, max_length=128)
    expected: str = Field(min_length=1, max_length=512)
    tolerance: str = Field(default="exact", min_length=1, max_length=128)
    source: Literal["step_observed", "target_readback", "audit_event"]


# 写工具验证计划必须覆盖的证据来源（缺任一项即构造失败）。
REQUIRED_VERIFICATION_SOURCES: tuple[str, ...] = ("step_observed", "target_readback")
# 写工具验证计划的最小断言数（affected 行数 / 目标终值 / 前后快照可比对）。
MIN_VERIFICATION_ASSERTIONS = 3


class PreflightEstimate(BaseModel):
    """P2-T3：写操作执行前的 dry-run 估计（只声明事实，不参与决策）。

    ``affected_rows`` 是"预计会被本次写影响的行数"；超过工具定义的
    ``max_affected_rows`` 时网关拒绝执行（且**不消耗审批**）。
    """

    model_config = ConfigDict(extra="forbid")

    affected_rows: int = Field(ge=0)
    detail: str = ""


class ToolDefinition(BaseModel):
    """对 Planner/模型暴露的工具元数据。

    input_schema/output_schema 描述 data 的载荷结构（JSON Schema）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str
    version: str = "1.0"
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"
    side_effect: Literal["none", "read", "write"] = "read"
    permissions: list[str]
    timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    idempotent: bool = True
    audit_policy: Literal["none", "metadata", "full"] = "metadata"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # V2.2 双引擎元数据；默认值保持旧工具可运行，Adapter 会覆盖真实值。
    db_engines: list[Literal["mysql", "postgresql"]] = Field(default_factory=lambda: ["mysql"])
    allowed_environments: list[str] = Field(default_factory=lambda: ["LAB"])
    required_role: str = "dg_ro"
    required_capabilities: list[str] = Field(default_factory=list)
    operation_class: Literal["observe", "diagnose", "experiment", "repair"] = "observe"
    risk_floor: Literal["R0", "R1", "R2", "R3", "R4"] | None = None
    row_limit: int = Field(default=500, ge=1, le=100_000)
    # W06：目标对象声明（写工具必须声明，否则审批创建 fail closed）。
    target_objects: list[TargetObjectSpec] = Field(default_factory=list)
    # W06：工具契约声明的前置条件文案（进入 prerequisites 摘要时为"仅声明、未核验"）。
    preconditions: list[str] = Field(default_factory=list)
    # W07：三节披露的唯一事实来源（必须有默认值，否则全仓 27 处构造点集体失败）。
    compensation: CompensationSpec | None = None
    verification_plan: list[VerificationAssertion] = Field(default_factory=list)
    # W07：True 时启用 9 类 D2 前置条件检查（探针缺失即 UNKNOWN 阻断）。
    prepare_required: bool = False
    # P2-T3：写操作的**爆炸半径**上限（影响行数估计超过即拒绝，且不消耗审批）。
    max_affected_rows: int = Field(default=1, ge=1, le=100_000)
    # P2-T3：True 时网关在执行前调用工具的 `preflight(context, params)`（dry-run/EXPLAIN）。
    preflight_required: bool = False

    @model_validator(mode="after")
    def _require_write_declarations(self) -> "ToolDefinition":
        """写操作的契约定义期 fail closed：目标对象 / 补偿 / 验证计划一次校验完。

        一次性把**全部**问题列出（而不是遇到第一个就抛），因为构造点遍布适配器与
        测试桩，逐条修会反复触发同一处失败；错误文案保留字段名以便定位。

        - 写工具必须声明 `target_objects`（P0-03b）：否则审批无从绑定具体操作；
        - 写工具必须声明 `compensation`（W07）：否则审批页无法披露"怎么补"；
        - 写工具必须声明 ≥3 条 `verification_plan`，`assertion_id` 不重复，
          且覆盖 `step_observed` 与 `target_readback`（W07）：否则无法披露"怎么验"。
        - `prepare_required=True` 只允许用于写操作（读工具没有可执行的准备动作）。
        """
        problems: list[str] = []
        if self.side_effect == "write" and not self.target_objects:
            problems.append("target_objects（目标对象抽取规则）")
        if self.prepare_required and self.side_effect != "write":
            problems.append("prepare_required（仅允许 side_effect=write）")
        if self.preflight_required and self.side_effect != "write":
            problems.append("preflight_required（仅允许 side_effect=write）")
        if self.side_effect == "write":
            if self.compensation is None:
                problems.append("compensation（补偿方案）")
            assertion_ids = [item.assertion_id for item in self.verification_plan]
            if len(self.verification_plan) < MIN_VERIFICATION_ASSERTIONS:
                problems.append(
                    f"verification_plan（至少 {MIN_VERIFICATION_ASSERTIONS} 条断言）"
                )
            if len(set(assertion_ids)) != len(assertion_ids):
                problems.append("verification_plan.assertion_id（不得重复）")
            covered = {item.source for item in self.verification_plan}
            missing_sources = [
                source for source in REQUIRED_VERIFICATION_SOURCES if source not in covered
            ]
            if missing_sources:
                problems.append(
                    f"verification_plan.source（缺少 {','.join(missing_sources)}）"
                )
        if problems:
            raise ValueError(
                f"side_effect={self.side_effect} 的契约声明不完整：" + "；".join(problems)
            )
        return self


@dataclass
class ToolContext:
    """单次工具调用的上下文；identity 在同一调用内复用。"""

    client: MysqlClient
    task_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    _identity: MysqlIdentity | None = field(default=None, init=False, repr=False)

    def identity(self) -> MysqlIdentity:
        if self._identity is None:
            self._identity = self.client.identity()
        return self._identity
