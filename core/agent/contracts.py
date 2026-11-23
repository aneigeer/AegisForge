"""Planner / Agent Loop 的数据契约：计划、步骤、决策与工具调用提议。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RiskLevel = Literal["R0", "R1", "R2", "R3", "R4"]


class PlanStep(BaseModel):
    """计划中的单个步骤；依赖用步骤下标表达，只能依赖更早的步骤。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    expected_evidence: str = ""
    risk_level: RiskLevel = "R0"
    verification: str | None = None
    stop_condition: str | None = None


class PlanBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=8, ge=1, le=50)
    max_tool_calls: int = Field(default=10, ge=1, le=50)


class Plan(BaseModel):
    """结构化诊断计划。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(min_length=1, max_length=50)
    stop_conditions: list[str] = Field(default_factory=list)
    success_criteria: str | None = None
    budget: PlanBudget = Field(default_factory=PlanBudget)


class PlannerDecision(BaseModel):
    """执行若干步骤后的决策：继续、调整或停止。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["continue", "stop"]
    reason: str = Field(min_length=1, max_length=1000)
    conclusion: str | None = Field(default=None, max_length=4000)
    confidence: Literal["low", "medium", "high"] | None = None
    evidence_gaps: list[str] = Field(default_factory=list)
    next_step: PlanStep | None = None


class ToolCallProposal(BaseModel):
    """模型额外提议的一次工具调用（仅在计划步骤耗尽且未停止时使用）。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: str = ""


__all__ = [
    "Plan",
    "PlanBudget",
    "PlanStep",
    "PlannerDecision",
    "RiskLevel",
    "ToolCallProposal",
]
