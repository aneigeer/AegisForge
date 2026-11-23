"""Agent 预算：限制步数、工具调用、模型调用、Token 与总时长。

预算超限统一抛出 BudgetExceededError，details 只包含安全的计数字段。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from core.errors import BudgetExceededError
from core.models.contracts import ModelResponse

_DIMENSION_LABELS = {
    "max_steps": "步骤数",
    "max_tool_calls": "工具调用次数",
    "max_model_calls": "模型调用次数",
    "max_tokens": "Token 用量",
    "max_seconds": "执行时长（秒）",
}


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 8
    max_tool_calls: int = 10
    max_model_calls: int = 20
    max_tokens: int = 20_000
    max_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings: object) -> "AgentBudget":
        return cls(
            max_steps=settings.agent_max_steps,
            max_tool_calls=settings.agent_max_tool_calls,
            max_model_calls=settings.agent_max_model_calls,
            max_tokens=settings.agent_max_tokens,
            max_seconds=settings.agent_max_seconds,
        )


class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_calls: int = 0
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0.0


class BudgetTracker:
    def __init__(
        self,
        budget: AgentBudget,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._started_at: float | None = None
        self._steps = 0
        self._tool_calls = 0
        self._model_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    def start(self) -> None:
        if self._started_at is None:
            self._started_at = self._clock()

    def check_step(self) -> None:
        self._ensure_within("max_steps", self._steps + 1, self._budget.max_steps)
        self._steps += 1

    def check_tool_call(self) -> None:
        self._ensure_within(
            "max_tool_calls", self._tool_calls + 1, self._budget.max_tool_calls
        )
        self._tool_calls += 1

    def check_model_call(self) -> None:
        self._ensure_within(
            "max_model_calls", self._model_calls + 1, self._budget.max_model_calls
        )
        self._model_calls += 1

    def record_model(self, response: ModelResponse) -> None:
        usage = response.usage
        self._prompt_tokens += usage.prompt_tokens
        self._completion_tokens += usage.completion_tokens
        self._total_tokens += usage.total_tokens
        self._ensure_within("max_tokens", self._total_tokens, self._budget.max_tokens)

    def record_tool(self, duration_ms: float = 0.0) -> None:
        """工具耗时已由 ToolResult 记录，这里保留接口以便调用方统一记账。"""

    def usage(self) -> BudgetUsage:
        return BudgetUsage(
            model_calls=self._model_calls,
            steps=self._steps,
            tool_calls=self._tool_calls,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            total_tokens=self._total_tokens,
            elapsed_seconds=self.elapsed_seconds(),
        )

    def remaining(self) -> dict[str, int | float]:
        elapsed = self.elapsed_seconds()
        return {
            "max_steps": max(0, self._budget.max_steps - self._steps),
            "max_tool_calls": max(0, self._budget.max_tool_calls - self._tool_calls),
            "max_model_calls": max(0, self._budget.max_model_calls - self._model_calls),
            "max_tokens": max(0, self._budget.max_tokens - self._total_tokens),
            "max_seconds": max(0.0, self._budget.max_seconds - elapsed),
        }

    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    def ensure_time(self) -> None:
        self._ensure_within(
            "max_seconds", self.elapsed_seconds(), self._budget.max_seconds
        )

    def _ensure_within(self, dimension: str, used: float, limit: float) -> None:
        if used > limit:
            label = _DIMENSION_LABELS[dimension]
            raise BudgetExceededError(
                f"{label}超限：已使用 {used}，上限 {limit}",
                details={"dimension": dimension, "limit": limit, "used": used},
            )


__all__ = ["AgentBudget", "BudgetTracker", "BudgetUsage"]
