"""P1-T1 集成测试：脱敏闸门在 Agent Loop 内**拦截于 HTTP 之前**（关闭 W09 AC-06 缺口）。

口径：`docs/enterprise-upgrade-roadmap.json` P1-T1 验收②——
「Unredacted PII in tool output triggers REDACTION_BLOCKED before leaving to the model HTTP transport」。

本测试的做法与边界：

1. 用一个**故意泄漏**的上下文构建器（模拟闸门③漏检）把未脱敏手机号放进 planner 的
   prompt payload，让请求走到 `ModelGateway.generate_structured`；
2. 断言 `ModelGateway` 内的闸门④（`RedactionGate.for_model_messages`）在**适配器调用之前**
   抛出 `REDACTION_BLOCKED`：spy 适配器的 `calls` 恒空（=没有任何 HTTP 传输发生）；
3. **正对照**：同一装配下把手机号替换为占位符 `<PHONE:…>`，请求正常通过并抵达适配器，
   证明"阻断"确实由未脱敏内容触发，而不是装配故障导致的假阳性。

不依赖真实模型服务（spy 适配器返回确定性计划）；控制库不可用时整文件 skip。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.agent.budget import AgentBudget
from core.agent.loop import AgentLoop
from core.agent.planner import Planner
from core.agent.trajectory import TrajectoryStore
from core.context.builder import ContextBuilder
from core.errors import DBGuardianError
from core.models.contracts import ModelRequest, ModelResponse, ModelUsage
from core.models.gateway import ModelGateway
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from database.observers import build_observer_registry

pytestmark = pytest.mark.integration

RAW_PHONE = "13812345678"
MASKED_PHONE = "<PHONE:deadbeef>"


class _SpyAdapter:
    """记录每次被调用时收到的消息；不发起任何网络请求。"""

    provider = "spy"
    adapter_version = "0.0.1"
    model_name = "spy-planner"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = [json.dumps(item, ensure_ascii=False) for item in responses]
        self._index = 0
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(
            [{"role": message.role, "content": message.content} for message in request.messages]
        )
        text = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ModelResponse(
            text=text,
            model_version=f"{self.provider}/{self.model_name}",
            provider=self.provider,
            adapter_version=self.adapter_version,
            usage=ModelUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=1.0,
        )


class _LeakyBundle:
    """模拟闸门③漏检：prompt 上下文里带着**未脱敏**的原文。"""

    def __init__(self, leaked_text: str) -> None:
        self._leaked_text = leaked_text

    def model_dump_for_prompt(self) -> dict[str, Any]:
        return {
            "goal": "投诉工单里出现锁等待",
            "evidence": [{"evidence_id": "ev_leak", "summary": self._leaked_text}],
        }


class _LeakyContextBuilder(ContextBuilder):
    """把调用方的证据替换成一条"漏检"证据，其余装配保持真实。"""

    def __init__(self, registry: Any, leaked_text: str) -> None:
        super().__init__(registry)
        self._leaked_text = leaked_text

    def build(self, **kwargs: Any) -> Any:  # type: ignore[override]
        return _LeakyBundle(self._leaked_text)


def _stop() -> dict[str, Any]:
    return {"action": "stop", "reason": "证据已足够，停止追加工具调用"}


def _plan() -> dict[str, Any]:
    return {
        "goal": "只读诊断锁等待",
        "assumptions": [],
        "steps": [
            {
                "title": "读取当前活动",
                "tool_name": "get_activity",
                "params": {},
                "depends_on": [],
                "expected_evidence": "活动会话",
                "risk_level": "R0",
            }
        ],
        "stop_conditions": ["已获取活动会话"],
        "budget": {"max_steps": 1, "max_tool_calls": 1},
    }


def _loop(
    service: TaskService,
    registry: Any,
    ro_client: Any,
    tmp_path: Path,
    adapter: _SpyAdapter,
    leaked_text: str,
) -> AgentLoop:
    gateway = ModelGateway(adapter, timeout_s=10)
    planner = Planner(gateway, registry, max_steps=2, max_tool_calls=2)
    return AgentLoop(
        service,
        registry,
        gateway,
        planner,
        _LeakyContextBuilder(registry, leaked_text),
        context_factory=lambda task_id: ToolContext(client=ro_client, task_id=task_id),
        budget=AgentBudget(max_steps=2, max_tool_calls=2, max_model_calls=4),
        trajectory_store=TrajectoryStore(tmp_path),
    )


def _stop_event(control_client: Any, task_id: str) -> dict[str, Any]:
    rows = control_client.fetch_all(
        "SELECT payload_json FROM dg_system.task_events "
        "WHERE task_id = %s AND event_type = 'AGENT_STOPPED' ORDER BY seq DESC LIMIT 1",
        (task_id,),
    )
    assert rows, "缺少 AGENT_STOPPED 事件"
    return json.loads(rows[0]["payload_json"])


def test_unredacted_pii_is_blocked_before_http_transport(
    control_client, settings, ro_client, tmp_path
) -> None:
    """未脱敏手机号 → REDACTION_BLOCKED，且适配器**一次都没有**被调用。"""
    registry = build_observer_registry()
    service = TaskService(TaskRepository(control_client), registry)
    adapter = _SpyAdapter([_plan(), _stop()])
    loop = _loop(service, registry, ro_client, tmp_path, adapter, f"客户 {RAW_PHONE} 投诉锁等待")

    task = service.create_task("订单表出现锁等待，请只读诊断")
    result = loop.run(task.task_id)

    assert result.stop_reason == "error"
    assert adapter.calls == [], "未脱敏载荷不得抵达模型传输层"
    event = _stop_event(control_client, task.task_id)
    assert event["error_code"] == "REDACTION_BLOCKED"


def test_masked_pii_passes_through_as_control(
    control_client, settings, ro_client, tmp_path
) -> None:
    """正对照：占位符形态的同一段文本可正常抵达适配器（阻断由未脱敏触发，非装配故障）。"""
    registry = build_observer_registry()
    service = TaskService(TaskRepository(control_client), registry)
    adapter = _SpyAdapter([_plan(), _stop()])
    loop = _loop(service, registry, ro_client, tmp_path, adapter, f"客户 {MASKED_PHONE} 投诉锁等待")

    task = service.create_task("订单表出现锁等待，请只读诊断")
    result = loop.run(task.task_id)

    assert adapter.calls, "占位符形态应当通过闸门并抵达适配器"
    assert result.stop_reason in {"planned_stop", "completed", "no_further_steps"}
    payload = json.dumps(adapter.calls, ensure_ascii=False)
    assert RAW_PHONE not in payload
