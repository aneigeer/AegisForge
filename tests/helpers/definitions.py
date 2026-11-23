"""写工具契约声明的共享夹具工厂（W07 T1）。

背景：W07 给 `ToolDefinition` 增加了**构造期**强约束——
`side_effect="write"` 的工具必须同时声明：

1. `target_objects`（W06 P0-03b，审批与执行对象同源的前提）；
2. `compensation`（W07，补偿方案：审批页必须披露"怎么补"）；
3. `verification_plan`（W07，验证计划：≥3 条断言，`assertion_id` 不重复，
   且必须覆盖 `step_observed` 与 `target_readback` 两个证据来源）。

全仓有十余处写工具测试桩，若各自手抄三节声明，必然与契约漂移。本模块提供
`write_declarations()` 一次返回三节声明，夹具只需：

    ToolDefinition(**{**base, **write_declarations()})

注意：本模块**只供测试使用**，生产写工具必须在自己的适配器里显式声明三节
（`adapters/mysql/lab_repair.py`），不得从测试代码导入。
"""

from __future__ import annotations

from typing import Any

# 与 `adapters/mysql/lab_repair.py` 的声明保持一致，便于夹具与真实工具同形。
DEFAULT_COMPENSATION_ACTION = "mysql_lab_update_order_note"


def compensation_declaration(
    *,
    action: str = DEFAULT_COMPENSATION_ACTION,
    description: str = (
        "把目标行恢复为执行前的值；补偿是独立 R2 动作，"
        "需新的 Prepare、新的 approval_id 与新审批，不称为回滚"
    ),
) -> dict[str, Any]:
    """返回一条合法的 `CompensationSpec` 载荷（dict，由 pydantic 校验）。"""
    return {
        "action": action,
        "description": description,
        "is_rollback": False,
        "requires_own_approval": True,
    }


def verification_declarations() -> list[dict[str, Any]]:
    """返回 3 条合法断言：affected 行数 / 目标终值 / 前后快照可比对。

    覆盖 `step_observed` 与 `target_readback` 两个来源，满足构造期约束。
    """
    return [
        {
            "assertion_id": "affected_rows",
            "metric": "affected_rows",
            "expected": "1",
            "tolerance": "exact",
            "source": "step_observed",
        },
        {
            "assertion_id": "target_note_final",
            "metric": "target_value",
            "expected": "$params.note",
            "tolerance": "exact",
            "source": "target_readback",
        },
        {
            "assertion_id": "snapshot_comparable",
            "metric": "before_after_snapshot",
            "expected": "before==$params.expected_note && after==$params.note",
            "tolerance": "exact",
            "source": "step_observed",
        },
    ]


def write_declarations() -> dict[str, Any]:
    """返回写工具必需的 `compensation` + `verification_plan` 声明载荷。"""
    return {
        "compensation": compensation_declaration(),
        "verification_plan": verification_declarations(),
    }


__all__ = [
    "DEFAULT_COMPENSATION_ACTION",
    "compensation_declaration",
    "verification_declarations",
    "write_declarations",
]
