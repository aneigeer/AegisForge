"""披露三节的**唯一**构建 / 校验入口（W07 §5.1）。

三节 = 风险等级（`risk`）/ 补偿方案（`compensation`）/ 验证计划（`verification_plan`）。

为什么必须"创建时刻快照"而不是读取时现算：

- W06 冻结的 `action_digest` 字段集不含三节，纳入三节会要求升版并重算全部冻结向量；
  因此三节只能落在 `approvals.disclosure_json`，用**独立摘要** `disclosure_digest`
  保证"审批人看到的 == 落库的那份"。
- 读取时现算会让"批准时看到的内容"随契约/环境漂移，"批准的到底是什么"无法取证。

完整性三层（本模块负责第 2、3 层的公共实现）：

1. 写入路径：`ApprovalRepository._MUTABLE_COLUMNS` 不含披露列（`transition()` 直接拒绝）；
2. 读取路径：`ApprovalRepository._to_approval()` 用 `verify_disclosure()` 重算比对；
3. 消费路径：`ApprovalService.consume()` 在行锁内二次复核。

摘要唯一入口固定为 `core.security.canonical.canonical_digest`（ADR-0005 明令禁止
`json.dumps(sort_keys=True)` 或 `compute_args_digest` 等旁路）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from core.approval.models import DISCLOSURE_SECTION_KEYS
from core.errors import DisclosureIncompleteError, StateConflictError
from core.security.canonical import (
    CanonicalizationError,
    canonical_digest,
    normalize_timestamp,
)
from core.security.policy import RiskResolution
from core.tools.contracts import MIN_VERIFICATION_ASSERTIONS, ToolDefinition

DISCLOSURE_VERSION = "w07-v1"
# 三节小节名（顺序即展示顺序）。
DISCLOSURE_SECTIONS: tuple[str, ...] = DISCLOSURE_SECTION_KEYS
# `disclosure_json` 的**封闭**键集；新增键必须同时升 `DISCLOSURE_VERSION`。
DISCLOSURE_KEYS: tuple[str, ...] = (
    "disclosure_version",
    "captured_at",
    "risk",
    "compensation",
    "verification_plan",
    "impact",
    "sources",
)
# 三节的事实来源标签（供审批人判断"这条结论是谁给的"）。
DISCLOSURE_SOURCES: dict[str, str] = {
    "risk": "risk_resolver",
    "compensation": "tool_definition",
    "verification_plan": "tool_definition",
}
# 篡改检测的稳定原因码（`STATE_CONFLICT` 的 `details.reason`）。
DISCLOSURE_TAMPERED = "DISCLOSURE_TAMPERED"


def _risk_section(
    resolution: RiskResolution, *, policy_version: str
) -> dict[str, Any]:
    """风险等级小节：内容全部来自服务端 `RiskResolver`，不接受客户端提示。"""
    model_hint = resolution.model_hint
    return {
        "effective_risk": resolution.effective_risk,
        # tuple → list：`canonical_digest` 只接受 JSON 值域（无 float / 无 datetime）。
        "factors": list(resolution.factors),
        "notes": list(resolution.notes),
        "model_hint": model_hint,
        "model_hint_accepted": bool(resolution.model_hint_accepted),
        "operation_known": bool(resolution.operation_known),
        "side_effect_known": bool(resolution.side_effect_known),
        "source": DISCLOSURE_SOURCES["risk"],
        "policy_version": policy_version,
    }


def _compensation_section(definition: ToolDefinition) -> dict[str, Any]:
    """补偿方案小节：直接来自契约声明，不做文案二次解释。

    非写操作（`side_effect` 为 `none`/`read`）没有可补偿的副作用：此处如实声明
    `applicable=false`，而不是伪造一段补偿。这样"三节永远存在"（审批页不折叠字段）
    与"只写操作必须有补偿声明"（构造期 fail closed）两条约束可以同时成立。
    """
    if definition.side_effect != "write":
        return {
            "action": "none",
            "description": "该操作无副作用（side_effect=%s），无需补偿" % definition.side_effect,
            "is_rollback": False,
            "requires_own_approval": False,
            "tool_name": definition.name,
            "tool_version": definition.version,
            "applicable": False,
        }
    compensation = definition.compensation
    if compensation is None:
        raise DisclosureIncompleteError(
            "写工具缺少补偿方案声明，无法生成披露快照",
            details={
                "tool_name": definition.name,
                "missing_fields": ["disclosure.compensation"],
                "sections": ["compensation"],
                "reason": "DISCLOSURE_INCOMPLETE",
            },
        )
    return {
        "action": compensation.action,
        "description": compensation.description,
        "is_rollback": bool(compensation.is_rollback),
        "requires_own_approval": bool(compensation.requires_own_approval),
        "tool_name": definition.name,
        "tool_version": definition.version,
        "applicable": True,
    }


def _verification_section(definition: ToolDefinition) -> dict[str, Any]:
    """验证计划小节：写操作 ≥3 条结构化断言，覆盖 `step_observed` 与 `target_readback`。"""
    plan = list(definition.verification_plan)
    if definition.side_effect != "write" and not plan:
        return {
            "assertions": [],
            "min_assertions": 0,
            "applicable": False,
            "reason": "NO_SIDE_EFFECT_TO_VERIFY",
        }
    if len(plan) < MIN_VERIFICATION_ASSERTIONS:
        raise DisclosureIncompleteError(
            "写工具的验证计划不足，无法生成披露快照",
            details={
                "tool_name": definition.name,
                "missing_fields": ["disclosure.verification_plan"],
                "sections": ["verification_plan"],
                "reason": "DISCLOSURE_INCOMPLETE",
                "assertion_count": len(plan),
                "min_assertions": MIN_VERIFICATION_ASSERTIONS,
            },
        )
    return {
        "assertions": [
            {
                "assertion_id": item.assertion_id,
                "metric": item.metric,
                "expected": item.expected,
                "tolerance": item.tolerance,
                "source": item.source,
            }
            for item in plan
        ],
        "min_assertions": MIN_VERIFICATION_ASSERTIONS,
        "applicable": True,
    }


def _impact_section(definition: ToolDefinition) -> dict[str, Any]:
    """影响面小节：`reversible` / `affects_other_tasks` 的服务端事实来源（P1-W07-01）。

    判定口径（不猜测、不留给客户端）：

    - `reversible`：契约声明了补偿方案且补偿本身需要独立审批 → 可逆（通过一次
      **新的** R2 动作恢复）。没有补偿声明即判不可逆。
    - `affects_other_tasks`：目标对象全部是 `row`（按主键单行）→ 不影响其他任务；
      只要出现表/库级目标就保守判"可能影响其他任务"。W07 的唯一动作是行级更新，
      因此恒为 False，但判定逻辑不写死结论。
    """
    compensation = definition.compensation
    reversible = compensation is not None
    if compensation is None:
        reversible_reason = "契约未声明补偿方案，按不可逆处理"
    else:
        reversible_reason = (
            f"把目标恢复为执行前的值（{compensation.action} 是独立 R2 动作，需新审批）"
        )
    object_types = [str(item.object_type) for item in definition.target_objects]
    affects_other = any(kind != "row" for kind in object_types)
    if affects_other:
        affects_other_reason = "目标包含表/库级对象，可能影响其他任务"
    else:
        affects_other_reason = "目标为按主键定位的单行，不涉及其他任务的行"
    return {
        "reversible": reversible,
        "affects_other_tasks": affects_other,
        "reversible_reason": reversible_reason,
        "affects_other_tasks_reason": affects_other_reason,
    }


def build_disclosure(
    *,
    resolution: RiskResolution,
    definition: ToolDefinition,
    captured_at: datetime,
    policy_version: str = "w06-v1",
) -> dict[str, Any]:
    """生成披露三节快照（确定性、无 float、无 datetime 对象）。

    这是运行期的第二道防线（第一道在 `ToolDefinition` 构造期）：即使有工具绕过了
    构造期校验，缺补偿/缺验证计划也会在这里抛 `DISCLOSURE_INCOMPLETE`，
    不会写出"半份披露"的审批。
    """
    disclosure: dict[str, Any] = {
        "disclosure_version": DISCLOSURE_VERSION,
        "captured_at": normalize_timestamp(captured_at),
        "risk": _risk_section(resolution, policy_version=policy_version),
        "compensation": _compensation_section(definition),
        "verification_plan": _verification_section(definition),
        "impact": _impact_section(definition),
        "sources": dict(DISCLOSURE_SOURCES),
    }
    # 立即做一次规范化：不可规范化的值（float / datetime）必须在此暴露，
    # 不能等到写库或读取时才发现（那样会把坏数据留在 approvals 表里）。
    canonical_digest(disclosure)
    return disclosure


def disclosure_digest(disclosure: Mapping[str, Any]) -> str:
    """对披露快照取 `canonical_digest`；不合法输入抛 `CanonicalizationError`。"""
    if not isinstance(disclosure, Mapping):
        raise CanonicalizationError(
            "披露快照必须是对象", details={"value_type": type(disclosure).__name__}
        )
    return canonical_digest(dict(disclosure))


def missing_disclosure_sections(disclosure: Mapping[str, Any] | None) -> list[str]:
    """返回缺失的披露小节名（`risk` / `compensation` / `verification_plan`）。"""
    if not isinstance(disclosure, Mapping):
        return list(DISCLOSURE_SECTIONS)
    return [section for section in DISCLOSURE_SECTIONS if not disclosure.get(section)]


def has_complete_disclosure(disclosure: Mapping[str, Any] | None) -> bool:
    """三节是否齐全（仅判"非空"，完整性由 `verify_disclosure` 判）。"""
    return not missing_disclosure_sections(disclosure)


def verify_disclosure(
    disclosure: Mapping[str, Any] | None, expected_digest: str | None
) -> None:
    """用库内 JSON 重算摘要并与 `disclosure_digest` 列比对；不符即拒绝。

    失败一律 `STATE_CONFLICT` + `details.reason=DISCLOSURE_TAMPERED`（不改既有
    9 个审批错误码语义）：披露内容与摘要不再同源，必须由人工介入，不得继续展示
    或继续批准/消费。
    """
    if not expected_digest:
        raise StateConflictError(
            "审批披露快照缺少完整性摘要，拒绝使用",
            details={
                "reason": DISCLOSURE_TAMPERED,
                "cause": "DISCLOSURE_DIGEST_MISSING",
            },
        )
    if not isinstance(disclosure, Mapping) or set(disclosure.keys()) != set(DISCLOSURE_KEYS):
        raise StateConflictError(
            "审批披露快照的键集被改写，拒绝使用",
            details={
                "reason": DISCLOSURE_TAMPERED,
                "cause": "DISCLOSURE_KEYS_MISMATCH",
                "expected_keys": list(DISCLOSURE_KEYS),
                "actual_keys": (
                    sorted(str(key) for key in disclosure.keys())
                    if isinstance(disclosure, Mapping)
                    else []
                ),
            },
        )
    try:
        actual = canonical_digest(dict(disclosure))
    except CanonicalizationError:
        raise StateConflictError(
            "审批披露快照无法规范化，拒绝使用",
            details={
                "reason": DISCLOSURE_TAMPERED,
                "cause": "DISCLOSURE_NOT_CANONICAL",
            },
        ) from None
    if actual != expected_digest:
        raise StateConflictError(
            "审批披露快照与完整性摘要不一致，拒绝使用",
            details={
                "reason": DISCLOSURE_TAMPERED,
                "cause": "DISCLOSURE_DIGEST_MISMATCH",
                "expected": expected_digest,
                "actual": actual,
            },
        )


__all__ = [
    "DISCLOSURE_KEYS",
    "DISCLOSURE_SECTIONS",
    "DISCLOSURE_SOURCES",
    "DISCLOSURE_TAMPERED",
    "DISCLOSURE_VERSION",
    "build_disclosure",
    "disclosure_digest",
    "has_complete_disclosure",
    "missing_disclosure_sections",
    "verify_disclosure",
]
