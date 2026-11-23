"""P1-T1 集成测试：真实 OpenAI 兼容端点（自托管 Ollama / vLLM）上的只读诊断。

**默认跳过**：真实模型调用既慢又不确定，不属于 PR 门禁。启用方式：

```powershell
$env:DG_TEST_REAL_LLM = "1"
$env:DG_TEST_REAL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"   # 可选
$env:DG_TEST_REAL_LLM_MODEL = "qwen2.5-7b-instruct-baseline:latest"  # 可选
.\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_p1_real_llm.py -o addopts="" -q
```

验收口径（`docs/enterprise-upgrade-roadmap.json` P1-T1）：

1. `OpenAICompatibleAdapter` 对真实端点返回**非空文本 + 非零 usage + 真实延迟**
   （P1-T1 修掉了延迟恒 0 的缺陷：适配器此前没把 `started` 传给 `make_response`）；
2. `AgentLoop` 用 `openai_compat` 在 MySQL LAB 上完成**多步只读诊断**：执行 ≥2 次工具调用、
   产出证据、以 `planned_stop` 正常收口；全部工具 `side_effect=read`，且不产生审批/写事件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from core.agent.budget import AgentBudget
from core.agent.loop import AgentLoop
from core.agent.planner import Planner
from core.agent.trajectory import TrajectoryStore
from core.context.builder import ContextBuilder
from core.models.adapters.openai_compat import OpenAICompatibleAdapter
from core.models.contracts import ModelMessage, ModelRequest
from core.models.gateway import ModelGateway
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from database.observers import build_observer_registry

_ENABLED = os.environ.get("DG_TEST_REAL_LLM") == "1"
_BASE_URL = os.environ.get("DG_TEST_REAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
_MODEL = os.environ.get("DG_TEST_REAL_LLM_MODEL", "qwen2.5-7b-instruct-baseline:latest")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ENABLED, reason="需显式开启：DG_TEST_REAL_LLM=1"),
]


def _adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        model_name=_MODEL,
        base_url=_BASE_URL,
        api_key="local-noauth",  # 自托管端点不校验；真实云端用 DG_MODEL_API_KEY 注入
        http_timeout_s=180,
    )


def test_adapter_returns_real_usage_and_latency() -> None:
    """真实端点的 usage 与延迟必须非零（延迟=0 曾是真实缺陷）。"""
    adapter = _adapter()
    try:
        response = adapter.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content='只返回 JSON：{"ok":true}')],
                response_format="json",
                max_tokens=64,
                temperature=0.0,
            )
        )
    finally:
        adapter.close()

    assert response.text.strip()
    assert response.model_version.startswith("openai_compat/")
    assert response.usage.total_tokens > 0, "端点应回报 token 用量"
    assert response.latency_ms > 50, f"延迟应来自真实计时，实测 {response.latency_ms}ms"


def test_agent_loop_completes_multistep_readonly_diagnosis(
    control_client, settings, ro_client, tmp_path
) -> None:
    """真实模型驱动 AgentLoop 在 LAB 上完成多步只读诊断。"""
    registry = build_observer_registry()
    service = TaskService(TaskRepository(control_client), registry)
    gateway = ModelGateway(_adapter(), timeout_s=180, default_max_tokens=1024)
    planner = Planner(gateway, registry, max_steps=4, max_tool_calls=4)
    loop = AgentLoop(
        service,
        registry,
        gateway,
        planner,
        # 本机 7B（8GB 显存）上下文有限：收紧证据条数与 prompt 预算，
        # 避免端点侧上下文溢出（表现为非 2xx）。生产旗舰模型可用默认值。
        ContextBuilder(
            registry, max_evidence_items=5, max_summary_chars=600, max_prompt_chars=8000
        ),
        context_factory=lambda task_id: ToolContext(client=ro_client, task_id=task_id),
        budget=AgentBudget(
            max_steps=4, max_tool_calls=4, max_model_calls=6, max_tokens=40_000, max_seconds=300
        ),
        trajectory_store=TrajectoryStore(tmp_path),
    )

    task = service.create_task("订单表出现锁等待，请只读诊断并给出证据")
    result = loop.run(task.task_id)

    assert result.stop_reason == "planned_stop", result.stop_reason
    assert result.tool_calls >= 2, f"多步诊断至少 2 次工具调用，实测 {result.tool_calls}"
    assert result.evidence, "应产出证据"
    assert result.model_version and _MODEL in result.model_version

    rows: list[dict[str, Any]] = control_client.fetch_all(
        "SELECT event_type, payload_json FROM dg_system.task_events "
        "WHERE task_id = %s ORDER BY seq",
        (task.task_id,),
    )
    started = [
        row
        for row in rows
        if row["event_type"] == "TOOL_CALL_STARTED"
    ]
    assert started, "应记录 TOOL_CALL_STARTED"
    for row in started:
        payload = _payload(row)
        assert payload["side_effect"] == "read", payload
    forbidden = {
        "APPROVAL_REQUESTED",
        "APPROVAL_CONSUMED",
        "COMPENSATION_REQUESTED",
    }
    assert not (forbidden & {row["event_type"] for row in rows})


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(row["payload_json"])
