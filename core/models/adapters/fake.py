"""离线模型适配器：脚本桩与确定性诊断桩。

这两个适配器不访问网络、不读取数据库，供单元测试与本地演示使用。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from typing import Any

from core.errors import ModelCallFailedError
from core.models.contracts import ModelRequest, ModelResponse, ModelUsage
from core.models.adapters.base import ModelAdapter

_PLAN_TOOL_PRIORITY = (
    "get_locks",
    "get_blocking_tree",
    "get_activity",
    "get_query_stats",
    "get_table_stats",
    "get_db_version",
)

_READ_ONLY_RISKS = {"R0", "R1"}


class FakeModelAdapter(ModelAdapter):
    """按脚本返回固定文本；用于测试超时、空输出、非法 JSON 等分支。"""

    provider = "fake"
    adapter_version = "0.1.0"

    def __init__(
        self,
        responses: Sequence[str] | Callable[[ModelRequest], str] | None = None,
        *,
        model_name: str = "fake-diagnostic-1",
        delay_s: float = 0.0,
        usage: ModelUsage | None = None,
    ) -> None:
        self.model_name = model_name
        self._callable: Callable[[ModelRequest], str] | None = (
            responses if callable(responses) else None
        )
        if self._callable is not None:
            self._script: list[str] = []
        elif isinstance(responses, str):
            self._script = [responses]
        else:
            self._script = list(responses or [])
        self._delay_s = max(delay_s, 0.0)
        self._usage = usage or ModelUsage()
        self._index = 0
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        self.requests.append(request)
        if self._delay_s > 0:
            time.sleep(self._delay_s)

        if self._callable is not None:
            text = self._callable(request)
        else:
            if self._index >= len(self._script):
                raise ModelCallFailedError(
                    "模型响应脚本已耗尽",
                    details={"provider": self.provider, "model_name": self.model_name},
                )
            text = self._script[self._index]
            self._index += 1

        return self.make_response(text, request, usage=self._usage, started=started)


class HeuristicDiagnosticAdapter(ModelAdapter):
    """确定性桩：从用户消息中的 JSON 读取 request_type，产出可校验的 JSON。"""

    provider = "fake"
    adapter_version = "0.1.0"

    def __init__(self, model_name: str = "fake-diagnostic-1") -> None:
        self.model_name = model_name

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        payload = self._read_payload(request)
        request_type = payload.get("request_type")
        if request_type == "plan":
            text = json.dumps(self._plan(payload), ensure_ascii=False)
        elif request_type == "assess":
            text = json.dumps(self._assess(payload), ensure_ascii=False)
        elif request_type == "tool_call":
            text = json.dumps(self._tool_call(payload), ensure_ascii=False)
        elif request_type == "classify_fault":
            text = json.dumps(self._classify(payload), ensure_ascii=False)
        else:
            raise ModelCallFailedError(
                "不支持的 request_type",
                details={"request_type": str(request_type)[:64]},
            )
        return self.make_response(text, request, started=started)

    @staticmethod
    def _classify(payload: dict[str, Any]) -> dict[str, Any]:
        """P2-T1：确定性故障分类桩（仅用于结构性验证，不代表模型能力）。

        只看证据摘要里的关键词，命中顺序固定；都不命中即 ``unknown``（如实弃权）。
        """
        text = json.dumps(payload.get("evidence") or [], ensure_ascii=False).lower()
        table = (
            ("lock_wait", ("lock", "blocking", "锁", "等待")),
            ("connection_pressure", ("connection", "session", "连接", "会话")),
            ("slow_query", ("query", "slow", "慢", "扫描")),
        )
        for scenario, keywords in table:
            if any(keyword.lower() in text for keyword in keywords):
                return {"scenario": scenario, "reason": f"keyword:{keywords[0]}"}
        return {"scenario": "unknown", "reason": "no_signal"}

    # ---- payload helpers ------------------------------------------------
    def _read_payload(self, request: ModelRequest) -> dict[str, Any]:
        for message in reversed(request.messages):
            if message.role != "user":
                continue
            try:
                payload = json.loads(message.content)
            except (json.JSONDecodeError, TypeError):
                raise ModelCallFailedError("用户消息不是合法 JSON") from None
            if not isinstance(payload, dict):
                raise ModelCallFailedError("用户消息必须是 JSON 对象")
            return payload
        raise ModelCallFailedError("请求缺少 user 消息")

    @staticmethod
    def _tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
        context = payload.get("context")
        if isinstance(context, dict) and isinstance(context.get("tools"), list):
            return [item for item in context["tools"] if isinstance(item, dict)]
        tools = payload.get("available_tools")
        if isinstance(tools, list):
            return [item for item in tools if isinstance(item, dict)]
        return []

    # ---- request types --------------------------------------------------
    def _plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("context")
        context = context if isinstance(context, dict) else {}
        goal = context.get("goal")
        goal = str(goal) if isinstance(goal, str) and goal.strip() else "诊断当前数据库状态"

        tools = self._tools(payload)
        by_name = {str(tool.get("name")): tool for tool in tools if tool.get("name")}
        selected: list[dict[str, Any]] = []
        for name in _PLAN_TOOL_PRIORITY:
            tool = by_name.get(name)
            if tool is not None:
                selected.append(tool)
            if len(selected) >= 3:
                break

        if not selected:
            for tool in tools:
                risk = tool.get("risk_level", "R0")
                side_effect = tool.get("side_effect", "read")
                if risk in _READ_ONLY_RISKS and side_effect == "read":
                    selected.append(tool)
                if len(selected) >= 2:
                    break

        steps = [self._step(tool) for tool in selected]
        return {
            "goal": goal,
            "assumptions": [],
            "steps": steps,
            "stop_conditions": ["证据足够即可停止", "工具失败时停止并报告"],
            "success_criteria": None,
            "budget": {"max_steps": len(steps), "max_tool_calls": len(steps)},
        }

    @staticmethod
    def _step(tool: dict[str, Any]) -> dict[str, Any]:
        name = str(tool["name"])
        description = str(tool.get("description") or name)
        return {
            "title": description[:60] or name,
            "tool_name": name,
            "params": {},
            "depends_on": [],
            "expected_evidence": f"{name} 返回的观测结果",
            "risk_level": tool.get("risk_level", "R0"),
            "verification": None,
            "stop_condition": None,
        }

    def _assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = payload.get("evidence")
        evidence = evidence if isinstance(evidence, list) else []
        plan = payload.get("plan")
        plan = plan if isinstance(plan, dict) else {}
        steps = plan.get("steps")
        steps = steps if isinstance(steps, list) else []

        if len(evidence) >= len(steps):
            return {
                "action": "stop",
                "reason": "计划步骤已执行完毕",
                "conclusion": "只读观察完成；根因判断需结合证据，不输出未经证实的结论。",
                "confidence": "low",
                "evidence_gaps": [],
                "next_step": None,
            }
        return {
            "action": "continue",
            "reason": "仍有计划步骤待执行",
            "conclusion": None,
            "confidence": None,
            "evidence_gaps": [],
            "next_step": None,
        }

    def _tool_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        tools = self._tools(payload)
        used: set[str] = set()
        plan = payload.get("plan")
        if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
            for step in plan["steps"]:
                if isinstance(step, dict) and step.get("tool_name"):
                    used.add(str(step["tool_name"]))
        if isinstance(payload.get("used_tools"), list):
            used.update(str(name) for name in payload["used_tools"])

        for tool in tools:
            name = tool.get("name")
            if not name or str(name) in used:
                continue
            if tool.get("side_effect", "read") != "read":
                continue
            return {"tool_name": str(name), "arguments": {}, "reason": "补充观测"}

        return {"tool_name": "", "arguments": {}, "reason": "无可用工具"}


__all__ = ["FakeModelAdapter", "HeuristicDiagnosticAdapter"]
