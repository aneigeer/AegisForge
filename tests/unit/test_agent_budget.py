"""W04 单元测试：Agent 预算与超限原因。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agent.budget import AgentBudget, BudgetTracker
from core.errors import BudgetExceededError, ErrorCode
from core.models.contracts import ModelResponse, ModelUsage


def _response(prompt: int = 0, completion: int = 0) -> ModelResponse:
    return ModelResponse(
        text="ok",
        model_version="fake/fake/0.1",
        provider="fake",
        adapter_version="0.1",
        usage=ModelUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
        latency_ms=1.0,
    )


def test_from_settings_maps_limits() -> None:
    settings = SimpleNamespace(
        agent_max_steps=3,
        agent_max_tool_calls=4,
        agent_max_model_calls=5,
        agent_max_tokens=6,
        agent_max_seconds=7.0,
    )
    budget = AgentBudget.from_settings(settings)
    assert budget == AgentBudget(
        max_steps=3, max_tool_calls=4, max_model_calls=5, max_tokens=6, max_seconds=7.0
    )


def test_step_and_tool_call_limits() -> None:
    tracker = BudgetTracker(AgentBudget(max_steps=1, max_tool_calls=1))
    tracker.start()
    tracker.check_step()
    tracker.check_tool_call()
    assert tracker.usage().steps == 1
    assert tracker.usage().tool_calls == 1

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.check_step()
    assert exc_info.value.code is ErrorCode.BUDGET_EXCEEDED
    assert exc_info.value.details == {"dimension": "max_steps", "limit": 1, "used": 2}

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.check_tool_call()
    assert exc_info.value.details == {"dimension": "max_tool_calls", "limit": 1, "used": 2}


def test_model_call_and_token_limits() -> None:
    tracker = BudgetTracker(AgentBudget(max_model_calls=1, max_tokens=10))
    tracker.start()
    tracker.check_model_call()
    tracker.record_model(_response(6, 2))
    assert tracker.usage().model_calls == 1
    assert tracker.usage().total_tokens == 8

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.check_model_call()
    assert exc_info.value.details["dimension"] == "max_model_calls"

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.record_model(_response(2, 1))
    assert exc_info.value.details["dimension"] == "max_tokens"
    assert exc_info.value.details["used"] == 11


def test_elapsed_limit_with_clock() -> None:
    now = [100.0]
    tracker = BudgetTracker(AgentBudget(max_seconds=5.0), clock=lambda: now[0])
    tracker.start()
    now[0] = 104.0
    tracker.ensure_time()
    now[0] = 106.0
    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.ensure_time()
    assert exc_info.value.details["dimension"] == "max_seconds"
    assert exc_info.value.details["used"] == 6.0


def test_remaining_is_never_negative() -> None:
    tracker = BudgetTracker(AgentBudget(max_steps=2, max_tool_calls=2, max_tokens=100))
    tracker.start()
    tracker.check_step()
    tracker.check_model_call()
    tracker.record_model(_response(10, 10))
    remaining = tracker.remaining()
    assert remaining["max_steps"] == 1
    assert remaining["max_tool_calls"] == 2
    assert remaining["max_model_calls"] == tracker._budget.max_model_calls - 1
    assert remaining["max_tokens"] == 80
