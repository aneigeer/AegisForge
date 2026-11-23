"""P1-T2：BenchmarkRunner 的 **AGENT 执行模式**（真实 AgentLoop + 真实模型）。

与 ``HARNESS`` 的边界（`docs/enterprise-upgrade-roadmap.json` P1-T2）：

| 模式 | 做什么 | 不做什么 |
|---|---|---|
| HARNESS | 只跑固定工具序列，`rca_match` / `schema_valid` 恒 ``None`` | 不读 Oracle、不调模型 |
| **AGENT** | 注入故障 → 真实 ``AgentLoop`` 只读诊断 → **任务终态后**经 ``OracleGate`` 读 1 次真值 → 比对 | 不绕过 ToolGateway/PolicyEngine、不写库、不把真值放进 prompt |

**Oracle 隔离**：本模块**不 import** ``core.faults.store`` 的加载器，真值只在
``BenchmarkRunner`` 通过注入的 ``OraclePort``（生产实现为 ``OracleGate``）读取；
``AgentCaseOutcome`` 结构上没有任何真值文本字段。

**根因判定口径**（可复核、不依赖自由文本相似度）：

1. Agent 先用自己的证据做一次 **分类**（``ModelRole.CLASSIFIER``），在冻结的三类
   ``slow_query`` / ``connection_pressure`` / ``lock_wait`` 中选择（或 ``unknown`` 表示证据不足）；
2. ``rca_match = (分类结果 == Oracle 真值的场景类别)``——Agent 全程**没有被告知**
   注入的是哪一类故障，因此该比值是真实诊断能力；
3. ``abstained = 分类结果是 unknown``；``abstention_correct = abstained 且未注入``。

分类用**结构化输出**（``ClassifierVerdict``）；输出不合 schema 时 ``schema_valid=False``
并如实计入 ``CAPABILITY_FAILURE``，不做兜底猜测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.errors import DBGuardianError
from core.models.contracts import ModelMessage, ModelRole
from core.tasks.models import TaskStatus

#: 冻结的三类故障（与 ``benchmarks/w10/suite_manifest.json`` 的 ``scenario`` 一致）。
FAULT_SCENARIOS: tuple[str, ...] = ("slow_query", "connection_pressure", "lock_wait")

#: 分类允许的取值；``unknown`` = 证据不足（abstain），不是第五类故障。
ScenarioGuess = Literal["slow_query", "connection_pressure", "lock_wait", "unknown"]

#: 只读诊断目标：**不泄露**注入的是哪一类故障（Agent 必须自己从证据判断）。
AGENT_GOAL: str = (
    "数据库实例出现异常，请只读诊断并定位根因。"
    "可能属于慢查询、连接压力或锁等待，也可能证据不足；"
    "请按需调用只读工具采集证据，不要臆测。"
)

_CLASSIFIER_SYSTEM_PROMPT = (
    "你是数据库故障分类器。只依据给定的证据摘要判断根因类别，"
    "只能从 slow_query / connection_pressure / lock_wait / unknown 中选一个；"
    "证据不足时选 unknown，不要猜测。只输出 JSON。"
)


class ClassifierVerdict(BaseModel):
    """分类器输出（结构化校验的落地形状）。"""

    model_config = ConfigDict(extra="forbid")

    scenario: ScenarioGuess
    reason: str = ""


@dataclass(frozen=True)
class AgentCaseOutcome:
    """单个 AGENT case 的实测结果（**无任何 Oracle 真值字段**）。"""

    task_id: str | None = None
    task_status: TaskStatus | None = None
    tool_sequence: tuple[str, ...] = ()
    latency_ms: float | None = None
    total_tokens: int = 0
    model_calls: int = 0
    schema_valid: bool | None = None
    ranked_root_causes: tuple[str, ...] = ()
    abstained: bool = False
    evidence_coverage: float | None = None
    loop_stop_reason: str | None = None
    failure_reason: str | None = None
    model_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentLoopCaseExecutor:
    """把一次 case 跑成 ``AgentCaseOutcome``；依赖全部由组合根注入。

    Args:
        service: ``TaskService``（控制库）。
        loop_factory: ``(task_id) -> AgentLoop``；由组合根绑定上下文客户端与预算。
        registry: 只读工具注册表（写工具在装配期即被 runner 拒绝）。
        gateway: ``ModelGateway``（分类调用走 ``ModelRole.CLASSIFIER``）。
        classifier_role: 分类使用的角色（默认 ``CLASSIFIER``，便于路由到小模型）。
    """

    def __init__(
        self,
        *,
        service: Any,
        loop_factory: Any,
        registry: Any,
        gateway: Any,
        classifier_role: ModelRole = ModelRole.CLASSIFIER,
        task_goal: str = AGENT_GOAL,
        engine_loop_factories: dict[str, Any] | None = None,
    ) -> None:
        self._service = service
        self._loop_factory = loop_factory
        # P2-T1：按引擎切换组合根（MySQL / PostgreSQL 的工具面与权限不同）。
        self._engine_loop_factories = dict(engine_loop_factories or {})
        self._registry = registry
        self._gateway = gateway
        self._classifier_role = classifier_role
        self._task_goal = task_goal

    def run_case(
        self, *, case_id: str, repetition: int, run_id: str = "", engine: str = "mysql"
    ) -> AgentCaseOutcome:
        """执行一个 case：创建任务 → 只读诊断 → 分类；失败如实记录、不抛给 runner。"""
        started = time.perf_counter()
        try:
            task = self._service.create_task(self._task_goal)
        except DBGuardianError as exc:
            return AgentCaseOutcome(
                failure_reason=f"{exc.code.value}: {exc.message}",
            )

        try:
            factory = self._engine_loop_factories.get(engine, self._loop_factory)
            loop = factory(task.task_id)
            result = loop.run(task.task_id)
        except DBGuardianError as exc:
            return AgentCaseOutcome(
                task_id=task.task_id,
                task_status=_safe_status(self._service, task.task_id),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                schema_valid=False,
                failure_reason=f"{exc.code.value}: {exc.message}",
            )

        tool_sequence = tuple(
            str(getattr(item, "tool_name", "")) for item in getattr(result, "evidence", ())
        )
        usage = dict(getattr(result, "usage", {}) or {})
        total_tokens = int(usage.get("total_tokens") or 0)
        model_calls = int(getattr(result, "model_calls", 0) or 0)

        verdict, schema_valid = self._classify(
            result, task_id=task.task_id, run_id=run_id or task.task_id
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        task_status = _safe_status(self._service, task.task_id)
        stop_reason = getattr(result, "stop_reason", None)
        # 任务未正常收口时把 loop 停止原因带出来：报告里的失败归因需要它，
        # 否则 FailureCase.reason 只能是空的"未知"。
        failure_reason: str | None = None
        if task_status is not None and task_status.value == "FAILED":
            failure_reason = f"LOOP_{stop_reason or 'UNKNOWN'}"
        return AgentCaseOutcome(
            task_id=task.task_id,
            task_status=task_status,
            tool_sequence=tool_sequence,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            model_calls=model_calls,
            schema_valid=schema_valid,
            ranked_root_causes=(verdict.scenario,) if verdict is not None else (),
            abstained=verdict is not None and verdict.scenario == "unknown",
            evidence_coverage=_coverage(result, self._registry),
            loop_stop_reason=stop_reason,
            failure_reason=failure_reason,
            model_version=getattr(result, "model_version", None),
            extra={"repetition": repetition, "case_id": case_id},
        )

    # ---- 分类 ------------------------------------------------------------
    def _classify(
        self, result: Any, *, task_id: str, run_id: str
    ) -> tuple[ClassifierVerdict | None, bool | None]:
        """用 Agent 自己的证据做一次结构化分类；输出不合 schema 即 ``schema_valid=False``。"""
        evidence = list(getattr(result, "evidence", ()) or ())
        if not evidence:
            return None, False
        payload = {
            "request_type": "classify_fault",
            "evidence": [self._evidence_summary(item) for item in evidence[-8:]],
        }
        messages = [
            ModelMessage(role="system", content=_CLASSIFIER_SYSTEM_PROMPT),
            ModelMessage(role="user", content=_json(payload)),
        ]
        try:
            output = self._gateway.generate_structured(
                messages,
                ClassifierVerdict,
                temperature=0.0,
                max_tokens=256,
                role=self._classifier_role,
                purpose="benchmark.classify_fault",
                metadata={"task_id": task_id, "run_id": run_id},
            )
        except DBGuardianError:
            return None, False
        verdict: ClassifierVerdict = output.parsed
        return verdict, True

    @staticmethod
    def _evidence_summary(item: Any) -> dict[str, Any]:
        """证据摘要（只保留工具名/状态/关键计数，绝不复制原始行内容）。"""
        return {
            "tool_name": str(getattr(item, "tool_name", "")),
            "status": str(getattr(item, "status", "")),
            "summary": str(getattr(item, "summary", ""))[:400],
        }


def _coverage(result: Any, registry: Any) -> float | None:
    """证据覆盖率：已采集的不同工具数 / 只读工具目录大小（不猜、不补）。"""
    names = {str(getattr(item, "tool_name", "")) for item in getattr(result, "evidence", ()) or ()}
    definitions = list(getattr(registry, "definitions", lambda: ())())
    if not definitions:
        return None
    available = {str(getattr(item, "name", "")) for item in definitions if getattr(item, "side_effect", None) == "read"}
    if not available:
        return None
    return round(len(names & available) / len(available), 4)


def _safe_status(service: Any, task_id: str) -> TaskStatus | None:
    try:
        return service.get_task(task_id).status
    except DBGuardianError:
        return None


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "AGENT_GOAL",
    "FAULT_SCENARIOS",
    "AgentCaseOutcome",
    "AgentLoopCaseExecutor",
    "ClassifierVerdict",
]
