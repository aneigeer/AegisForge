"""W10 T02：独立 benchmark-runner（AC-03/04/05，只读、可重放、不编造）。

只读边界（AC-05 / 全局纪律 3）：构造签名**不含** ``ToolGateway`` /
``ApprovalService``；注册表在构造时用等价断言
``[d for d in registry.definitions() if d.side_effect == "write"] == []``
守卫（``write_tools()`` 不存在，设计 D-02）。观测面固定为**适配器 7 个只读
工具**（``ADAPTER_READ_TOOLS``，与 ``tool_schema.json`/``PUBLIC_TOOL_ALLOWLIST``
一致），**不用** ``build_observer_registry()`` 的 10 工具面（D-03）。

打包安全（R-13 / 任务卡语义 11）：``injector`` / ``resetter`` /
``verify_assertions`` 一律**函数体内惰性 import**。``benchmarks/`` 不在
``pyproject.toml:30`` 打包清单里，顶层 import 会让只装 wheel 的环境在
``import core.benchmark.runner`` 时直接 ImportError。本模块顶层只依赖
``core*`` / 标准库；``suite_manifest.json`` 是**只读数据文件**，不是包导入。

不编造（AC-08 的 T02 侧）：``RunResult.scores`` / ``metrics`` 在
``NOT_RUN`` / ``RUNNING`` / ``BLOCKED`` 恒 ``None``，由模型校验器强制；
状态迁移 ``NOT_RUN → RUNNING → {COMPLETED|CANCELLED|FAILED|BLOCKED}``
不可回退。``HARNESS`` 模式（``ENV-MODEL``，模型不可用）不读 Oracle，
``rca_match`` 恒 ``None``，只外泄位（设计 R-01 / D-08）。

隔离：``CaseResult`` / ``cases.jsonl`` 绝不出现 ``expected_root_cause`` /
``injection_params`` / ``truth_assertions``；``fault_seed`` 是公开 Manifest
的 ``data_seed`` 同值，按设计 §4.2 允许保留作定位字段。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from core.benchmark.manifest import RunManifest, RunStatus
from core.benchmark.oracle import OraclePort
from core.errors import BenchmarkInvalidError, DBGuardianError
from core.evidence.models import CollectionStatus
from core.security.canonical import canonical_digest
from core.tasks.models import TaskStatus
from core.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE_PATH: Path = ROOT / "benchmarks" / "w10" / "suite_manifest.json"

# 适配器 7 个固定只读工具（顺序固定；与 core/faults/store.py:32-42 及
# benchmarks/baseline_v0/tool_schema.json:9-17 逐字一致）。**不是** 10 工具 observer 面。
ADAPTER_READ_TOOLS: tuple[str, ...] = (
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
)

# 设计 §9.1 S6 / 任务卡禁止项 6：产物文本禁止出现的 Oracle 真值字段名。
FORBIDDEN_CASE_FIELDS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
)

# suite 中允许出现的环境阻塞原因（全部是主理人台账已登记项，不臆造）。
KNOWN_ENV_BLOCKERS: frozenset[str] = frozenset(
    {"ENV-PG5432", "ENV-MODEL", "ENV-RESIDUE", "ENV-1045", "ENV-NV1"}
)

# 运行期状态迁移：只允许前进，终态不可回退（任务卡语义 7）。
ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.NOT_RUN: frozenset({RunStatus.RUNNING, RunStatus.BLOCKED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.BLOCKED: frozenset(),
}


class InjectionState(str, Enum):
    INJECTED = "INJECTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PlannedCase:
    case_id: str
    engine: str
    scenario: str
    fault_seed: int
    repetition: int
    tool_plan: tuple[str, ...]


@dataclass(frozen=True)
class SuiteCase:
    """冻结测试集清单中的一条 case 定义（``benchmarks/w10/suite_manifest.json``）。"""

    case_id: str
    engine: str
    scenario: str
    scenario_id: str
    fault_seed: int
    repetitions: int
    tool_plan: tuple[str, ...]
    env_blocked_reason: str | None


@dataclass(frozen=True)
class SuiteSpec:
    suite_version: str
    testset_version: str
    repetitions_per_seed: int
    tool_plan: tuple[str, ...]
    cases: tuple[SuiteCase, ...]


class CancelToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...


class ObservationPort(Protocol):
    """按固定顺序「真实执行」7 个只读工具，返回 ``name@version`` 序列。"""

    def observe(self, tool_plan: tuple[str, ...]) -> Sequence[str]: ...


class RegistryToolObserver:
    """默认观测端口：按固定顺序从注册表取**真实** ToolDefinition 并执行只读校验。

    离线可判路径不连接数据库（``ENV-PG5432`` 未解、评测进程只读），因此「执行」
    落在工具契约层（注册表查找 + side_effect 校验 + 版本读取），而非发起 DB 调用；
    序列元素严格为 ``<definition.name>@<definition.version>``，可被 ``canonical_digest``
    复现。需要真实 DB 观测时由组合根注入另一个满足 ``ObservationPort`` 的对象。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def observe(self, tool_plan: tuple[str, ...]) -> tuple[str, ...]:
        sequence: list[str] = []
        for name in tool_plan:
            tool = self._registry.get(name)
            definition = getattr(tool, "definition", None)
            if definition is None:
                raise BenchmarkInvalidError(
                    "观测工具缺少 ToolDefinition", details={"tool_name": name}
                )
            if definition.side_effect != "read":
                raise BenchmarkInvalidError(
                    "评测观测面只允许只读工具", details={"tool_name": name}
                )
            sequence.append(f"{definition.name}@{definition.version}")
        return tuple(sequence)


class CaseResult(BaseModel):
    """单条评测结果（设计 §4.2）；**不设**任何 Oracle 真值文本字段。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    engine: str
    scenario: str
    fault_seed: int
    repetition: int
    execution_mode: Literal["HARNESS", "AGENT"] = "HARNESS"
    injection_state: InjectionState = InjectionState.UNKNOWN
    collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    task_id: str | None = None
    task_status: TaskStatus | None = None
    tool_sequence: tuple[str, ...] = ()
    tool_sequence_digest: str = ""
    assertions_passed: bool | None = None
    schema_valid: bool | None = None
    abstained: bool = False
    abstention_expected: bool | None = None
    abstention_correct: bool | None = None
    ranked_root_causes: tuple[str, ...] = ()
    rca_match: bool | None = None
    unsafe_action_attempted: bool = False
    unsafe_action_blocked: bool | None = None
    repair_attempted: bool = False
    repair_target_ok: bool | None = None
    verification_state: Literal["NONE", "PENDING", "PASS", "FAIL", "UNKNOWN"] = "UNKNOWN"
    evidence_coverage: float | None = None
    latency_ms: float | None = None
    cost_units: float | None = None
    env_blocked: bool = False
    excluded_reason: str | None = None
    # Attribution 枚举在 T03 ``metrics.py`` 定义；此处只存字面值，
    # 避免 runner → metrics 的反向依赖（设计 §2.2 单向依赖规则）。
    attribution: str | None = None
    failure_reason: str | None = None
    cancel_reason: str | None = None


class RunResult(BaseModel):
    """一次运行的汇总；``scores`` / ``metrics`` 未到终态恒 ``None``。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = ""
    manifest_version: str = "w10-run-v1"
    status: RunStatus = RunStatus.NOT_RUN
    execution_mode: Literal["HARNESS", "AGENT"] = "HARNESS"
    cases: tuple[CaseResult, ...] = ()
    scores: dict[str, float] | None = None
    metrics: dict[str, float] | None = None
    cancel_reason: str | None = None
    failure_reason: str | None = None
    artifact_dir: str = ""

    @model_validator(mode="after")
    def _no_fabricated_scores(self) -> "RunResult":
        """AC-08：未真实测得/未到终态时填任何值（含 0 / {}）都算编造。"""

        if self.status in {RunStatus.NOT_RUN, RunStatus.RUNNING, RunStatus.BLOCKED}:
            if self.scores is not None or self.metrics is not None:
                raise ValueError("NOT_RUN/RUNNING/BLOCKED 下 scores 与 metrics 必须为 None")
        return self


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """校验运行期状态迁移；终态不可回退，非法迁移抛 ``BENCHMARK_INVALID``。"""

    if current == target:
        return
    if target not in ALLOWED_RUN_TRANSITIONS.get(current, frozenset()):
        raise BenchmarkInvalidError(
            "非法运行状态迁移（不可回退）",
            details={"from": current.value, "to": target.value},
        )


def load_suite(path: Path = DEFAULT_SUITE_PATH) -> SuiteSpec:
    """读取冻结测试集清单（**只读 JSON，不 import** ``benchmarks.*``）。"""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkInvalidError(
            "测试集清单不可读或不是合法 JSON", details={"path": str(source)}
        ) from exc
    if not isinstance(payload, dict):
        raise BenchmarkInvalidError("测试集清单顶层必须是对象")

    tool_plan = tuple(str(name) for name in payload.get("tool_plan", ()))
    if tool_plan != ADAPTER_READ_TOOLS:
        raise BenchmarkInvalidError(
            "测试集清单 tool_plan 必须恰为适配器 7 个只读工具且顺序固定",
            details={"expected": list(ADAPTER_READ_TOOLS), "actual": list(tool_plan)},
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise BenchmarkInvalidError("测试集清单 cases 必须是非空数组")

    cases: list[SuiteCase] = []
    seen: set[str] = set()
    for item in raw_cases:
        if not isinstance(item, dict):
            raise BenchmarkInvalidError("测试集清单 case 必须是对象")
        case_id = str(item.get("case_id", ""))
        engine = str(item.get("engine", ""))
        if not case_id or case_id in seen:
            raise BenchmarkInvalidError(
                "测试集清单 case_id 必须非空且唯一", details={"case_id": case_id}
            )
        seen.add(case_id)
        if engine not in {"mysql", "postgresql"}:
            raise BenchmarkInvalidError(
                "测试集清单 engine 必须是 mysql/postgresql",
                details={"case_id": case_id, "engine": engine},
            )
        blocker = item.get("env_blocked_reason")
        if blocker is not None and blocker not in KNOWN_ENV_BLOCKERS:
            raise BenchmarkInvalidError(
                "测试集清单 env_blocked_reason 必须是已登记环境项",
                details={"case_id": case_id, "reason": str(blocker)},
            )
        case_tool_plan = tuple(str(name) for name in item.get("tool_plan", tool_plan))
        if case_tool_plan != ADAPTER_READ_TOOLS:
            raise BenchmarkInvalidError(
                "case.tool_plan 必须与清单固定 7 工具一致",
                details={"case_id": case_id},
            )
        try:
            fault_seed = int(item["fault_seed"])
            repetitions = int(item.get("repetitions", payload["repetitions_per_seed"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkInvalidError(
                "case 缺少合法的 fault_seed/repetitions", details={"case_id": case_id}
            ) from exc
        if fault_seed < 0 or repetitions < 1:
            raise BenchmarkInvalidError(
                "case fault_seed 必须 >=0 且 repetitions >=1",
                details={"case_id": case_id},
            )
        cases.append(
            SuiteCase(
                case_id=case_id,
                engine=engine,
                scenario=str(item["scenario"]),
                scenario_id=str(item["scenario_id"]),
                fault_seed=fault_seed,
                repetitions=repetitions,
                tool_plan=case_tool_plan,
                env_blocked_reason=blocker,
            )
        )
    return SuiteSpec(
        suite_version=str(payload.get("suite_version", "")),
        testset_version=str(payload.get("testset_version", "")),
        repetitions_per_seed=int(payload.get("repetitions_per_seed", 3)),
        tool_plan=tool_plan,
        cases=tuple(cases),
    )


def plan_cases(suite: SuiteSpec) -> tuple[PlannedCase, ...]:
    """纯函数：按清单顺序 + repetition 升序展开（**不用 random**）。"""

    planned: list[PlannedCase] = []
    for case in suite.cases:
        for repetition in range(1, case.repetitions + 1):
            planned.append(
                PlannedCase(
                    case_id=case.case_id,
                    engine=case.engine,
                    scenario=case.scenario,
                    fault_seed=case.fault_seed,
                    repetition=repetition,
                    tool_plan=case.tool_plan,
                )
            )
    return tuple(planned)


def _canonicalizable(value: Any) -> Any:
    """把 float 转成十进制字符串，满足 ``canonical_json`` 的输入契约。

    ``core/security/canonical.py:108`` 明确拒绝浮点数并要求改用十进制字符串；
    这里只做输入规范化，**摘要仍由** ``canonical_digest`` 计算（禁止重写）。
    """

    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {key: _canonicalizable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalizable(item) for item in value]
    return value


def derive_run_id(manifest: RunManifest) -> str:
    """由 Manifest 内容确定性派生 run_id（同一 Manifest → 同一目录，AC-03）。"""

    payload = _canonicalizable(manifest.model_dump(mode="json"))
    return "run_" + canonical_digest(payload)[:24]


def _default_injector() -> Callable[..., Any]:
    """函数体内惰性解析默认注入器（R-13：顶层不得 import ``benchmarks.*``）。"""

    from benchmarks.faults.injector import inject  # noqa: PLC0415 - 惰性 import 是硬约束

    return inject


def _default_resetter() -> Callable[..., Any]:
    """函数体内惰性解析默认重置器。"""

    from benchmarks.faults.injector import reset  # noqa: PLC0415 - 惰性 import 是硬约束

    return reset


def _resolve_assertion_verifier() -> Callable[..., Any] | None:
    """函数体内惰性解析场景断言执行器；不可用时返回 ``None``（不编造结果）。"""

    try:
        from benchmarks.faults.assertions import (  # noqa: PLC0415
            verify_assertions,
        )
    except ImportError:
        return None
    return verify_assertions


class BenchmarkRunner:
    """离线只读评测执行器；产物落 ``artifact_dir``，不连控制库、不写库。"""

    def __init__(
        self,
        *,
        manifest: RunManifest,
        registry: ToolRegistry,
        observer: Any,
        oracle: OraclePort,
        artifact_dir: Path,
        injector: Callable[..., Any] | None = None,
        resetter: Callable[..., Any] | None = None,
        audit: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        case_executor: Any | None = None,
        execution_mode: Literal["HARNESS", "AGENT"] = "HARNESS",
        blocker_resolver: Callable[[SuiteCase], str | None] | None = None,
    ) -> None:
        # AC-05 / D-02：注册表等价断言（write_tools() 方法不存在）。
        write_definitions = [
            item for item in registry.definitions() if item.side_effect == "write"
        ]
        if write_definitions:
            raise BenchmarkInvalidError(
                "benchmark-runner 不得装配写工具",
                details={"tools": [item.name for item in write_definitions]},
            )
        self._manifest = manifest
        self._registry = registry
        self._observer = observer
        self._oracle = oracle
        self._artifact_dir = Path(artifact_dir)
        self._injector = injector
        self._resetter = resetter
        self._audit = audit
        self._clock = clock
        # P1-T2：AGENT 模式必须注入执行器（真实 AgentLoop + 真实模型）。
        # 未注入时**不允许**声明 AGENT，避免出现"AGENT 但没跑模型"的假结果。
        if execution_mode == "AGENT" and case_executor is None:
            raise BenchmarkInvalidError(
                "AGENT 模式必须注入 case_executor（否则只能声明 HARNESS）",
                details={"execution_mode": execution_mode},
            )
        self._case_executor = case_executor
        # P2-T1：清单里声明的环境阻塞可以**被实测解除**（如 PG16 已就绪）；
        # 解析器返回 None 表示"该阻塞已不成立"，仍由 runner 决定是否真执行。
        self._blocker_resolver = blocker_resolver
        self._execution_mode: Literal["HARNESS", "AGENT"] = execution_mode
        self._run_id = ""
        self._suite_path = DEFAULT_SUITE_PATH
        self._case_meta: dict[str, SuiteCase] = {}
        self._blocked_reasons: dict[str, str] = {}
        self._status = manifest.status
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._run_failure_reason: str | None = None

    @property
    def status(self) -> RunStatus:
        return self._status

    def plan(self) -> tuple[PlannedCase, ...]:
        """加载冻结清单并展开确定性 case 序列。"""

        suite = load_suite(self._suite_path)
        if suite.suite_version != self._manifest.suite_version:
            raise BenchmarkInvalidError(
                "RunManifest.suite_version 与冻结测试集清单不一致",
                details={
                    "manifest": self._manifest.suite_version,
                    "suite": suite.suite_version,
                },
            )
        if suite.testset_version != self._manifest.testset_version:
            raise BenchmarkInvalidError(
                "RunManifest.testset_version 与冻结测试集清单不一致",
                details={
                    "manifest": self._manifest.testset_version,
                    "suite": suite.testset_version,
                },
            )
        self._case_meta = {case.case_id: case for case in suite.cases}
        self._blocked_reasons = {
            case.case_id: str(case.env_blocked_reason)
            for case in suite.cases
            if case.env_blocked_reason is not None
        }
        return plan_cases(suite)

    def cancel(self, *, reason: str) -> None:
        """AC-04：请求取消；终态后不可再取消（状态不可回退）。"""

        cleaned = reason.strip()
        if not cleaned:
            raise BenchmarkInvalidError("取消原因不得为空")
        if self._status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
        }:
            raise BenchmarkInvalidError("运行已到终态，不可取消")
        self._cancelled = True
        self._cancel_reason = cleaned

    def run(
        self,
        *,
        cancel: CancelToken | None = None,
        progress: Callable[[CaseResult], None] | None = None,
    ) -> RunResult:
        """执行评测；返回 ``RunResult``（scores/metrics 由 T03 之后计算，此处恒 None）。"""

        ensure_run_transition(self._status, RunStatus.RUNNING)
        self._status = RunStatus.RUNNING
        self._run_id = derive_run_id(self._manifest)
        planned = self.plan()
        started = self._clock()
        cases: list[CaseResult] = []
        cancelled = False
        run_failed = False
        cancel_reason = self._cancel_reason

        if cancel is not None and cancel.cancelled:
            cancelled = True
            cancel_reason = cancel.reason or cancel_reason or "CANCEL_REQUESTED"
        if self._cancelled:
            cancelled = True
            cancel_reason = cancel_reason or "CANCEL_REQUESTED"

        for item in planned[: self._manifest.budget.max_cases]:
            if cancelled:
                break
            if self._clock() - started > self._manifest.budget.max_wall_s:
                cancelled = True
                cancel_reason = cancel_reason or "WALL_TIME_BUDGET"
                break
            try:
                completed = self._execute_case(item)
                cases.append(completed)
                if progress is not None:
                    progress(completed)
            except Exception as exc:  # noqa: BLE001 - 单 case 异常须留痕并转 run 级 FAILED
                run_failed = True
                self._run_failure_reason = f"{type(exc).__name__}: {exc}"
                cases.append(self._failed_case(item, exc))

        if cancelled:
            target = RunStatus.CANCELLED
        elif run_failed:
            target = RunStatus.FAILED
        else:
            target = RunStatus.COMPLETED
        ensure_run_transition(self._status, target)
        self._status = target

        result = RunResult(
            run_id=self._run_id,
            manifest_version=self._manifest.manifest_version,
            status=target,
            execution_mode=self._execution_mode,
            cases=tuple(cases),
            # 未到终态/无真实指标计算 → 恒 None（AC-08 / 禁止编造指标）。
            scores=None,
            metrics=None,
            cancel_reason=cancel_reason if cancelled else None,
            failure_reason=self._run_failure_reason if run_failed else None,
            artifact_dir=str(self._artifact_dir),
        )
        self._write_artifacts(result)
        return result

    def _resolved_blocker(self, case_id: str) -> str | None:
        declared = self._blocked_reasons.get(case_id)
        if declared is None or self._blocker_resolver is None:
            return declared
        return self._blocker_resolver(self._case_meta[case_id])

    def _execute_case(self, item: PlannedCase) -> CaseResult:
        blocker = self._resolved_blocker(item.case_id)
        if blocker is not None:
            # 引擎不可达（ENV-PG5432）：如实标 NOT_EXECUTED 归因，不判 FAIL、不冒充 PASS。
            return self._base_case(
                item,
                injection_state=InjectionState.UNKNOWN,
                collection_status=CollectionStatus.UNKNOWN,
                tool_sequence=(),
                env_blocked=True,
                excluded_reason=blocker,
            )

        if self._execution_mode == "AGENT":
            return self._execute_agent_case(item)

        meta = self._case_meta[item.case_id]
        injector = self._injector if self._injector is not None else _default_injector()
        try:
            injection = injector(meta.scenario_id)
        except Exception:  # noqa: BLE001 - 注入失败进 D_env，不判 run 级 FAIL（设计 §5.2 规则 2）
            return self._base_case(
                item,
                injection_state=InjectionState.FAILED,
                collection_status=CollectionStatus.UNKNOWN,
                tool_sequence=(),
                env_blocked=True,
                excluded_reason="INJECTION_FAILED",
            )
        injected = bool(getattr(injection, "injected", False))
        injection_state = (
            InjectionState.INJECTED if injected else InjectionState.NOT_SUPPORTED
        )

        sequence = self._observe(item.tool_plan)
        assertions = self._verify(meta.scenario_id)
        resetter = self._resetter if self._resetter is not None else _default_resetter()
        try:
            resetter(meta.scenario_id)
        except Exception as exc:  # noqa: BLE001 - 重置失败是 case 级失败原因，不影响其它 case
            return self._base_case(
                item,
                injection_state=injection_state,
                collection_status=CollectionStatus.SUCCESS,
                tool_sequence=sequence,
                assertions_passed=assertions,
                failure_reason=f"RESET_FAILED: {type(exc).__name__}",
            )
        return self._base_case(
            item,
            injection_state=injection_state,
            collection_status=CollectionStatus.SUCCESS,
            tool_sequence=sequence,
            assertions_passed=assertions,
        )

    def _execute_agent_case(self, item: PlannedCase) -> CaseResult:
        """P1-T2：注入故障 → 真实 Agent 诊断 → **终态后**读 1 次 Oracle → 比对。

        隔离口径：Oracle 只经 ``self._oracle``（生产实现 ``OracleGate``）读取，
        终态判定与单次读取由闸门自己完成；本方法不接触真值文本字段。
        """
        meta = self._case_meta[item.case_id]
        injector = self._injector if self._injector is not None else _default_injector()
        try:
            injection = injector(meta.scenario_id)
        except Exception:  # noqa: BLE001 - 注入失败进 D_env（与 HARNESS 同口径）
            return self._base_case(
                item,
                injection_state=InjectionState.FAILED,
                collection_status=CollectionStatus.UNKNOWN,
                tool_sequence=(),
                env_blocked=True,
                excluded_reason="INJECTION_FAILED",
            )
        injected = bool(getattr(injection, "injected", False))
        injection_state = (
            InjectionState.INJECTED if injected else InjectionState.NOT_SUPPORTED
        )
        oracle_run_id = f"{self._run_id}:{item.case_id}:r{item.repetition}"
        reset_failure: str | None = None
        try:
            outcome = self._case_executor.run_case(
                case_id=item.case_id,
                repetition=item.repetition,
                run_id=oracle_run_id,
                engine=meta.engine,
            )
        finally:
            resetter = self._resetter if self._resetter is not None else _default_resetter()
            try:
                resetter(meta.scenario_id)
            except Exception as exc:  # noqa: BLE001 - 重置失败留痕，不吞掉已采证据
                reset_failure = f"RESET_FAILED: {type(exc).__name__}"

        rca_match: bool | None = None
        oracle_note: str | None = None
        if outcome.task_id:
            try:
                truth = self._oracle.read(
                    meta.scenario_id, task_id=outcome.task_id, run_id=oracle_run_id
                )
            except DBGuardianError as exc:
                # 未终态/已读过/不可用：不猜结论，如实留拒绝原因码
                truth = None
                oracle_note = f"ORACLE_REFUSED: {exc.code.value}"
            if truth is not None:
                truth_scenario = str(getattr(truth, "scenario_id", "")).split("_", 1)[-1]
                top = outcome.ranked_root_causes[0] if outcome.ranked_root_causes else None
                rca_match = None if top is None else top == truth_scenario
        else:
            oracle_note = "NO_TASK_ID"

        abstention_correct: bool | None = None
        if outcome.abstained:
            abstention_correct = not injected

        failure_reason = outcome.failure_reason or reset_failure or oracle_note
        return self._base_case(
            item,
            injection_state=injection_state,
            collection_status=(
                CollectionStatus.SUCCESS if outcome.task_id else CollectionStatus.UNKNOWN
            ),
            tool_sequence=outcome.tool_sequence,
            task_id=outcome.task_id,
            task_status=outcome.task_status,
            schema_valid=outcome.schema_valid,
            rca_match=rca_match,
            abstained=outcome.abstained,
            abstention_expected=injected,
            abstention_correct=abstention_correct,
            ranked_root_causes=outcome.ranked_root_causes,
            evidence_coverage=outcome.evidence_coverage,
            latency_ms=outcome.latency_ms,
            cost_units=float(outcome.total_tokens) if outcome.total_tokens else None,
            failure_reason=failure_reason,
        )

    def _observe(self, tool_plan: tuple[str, ...]) -> tuple[str, ...]:
        observer = self._observer
        if hasattr(observer, "observe"):
            raw = observer.observe(tool_plan)
        else:
            raw = observer(tool_plan)
        return tuple(str(item) for item in raw)

    @staticmethod
    def _verify(scenario_id: str) -> bool | None:
        """惰性解析场景断言器；离线路径无真实观测 → ``None``（不编造通过/失败）。"""

        verifier = _resolve_assertion_verifier()
        if verifier is None:
            return None
        # 当前 harness 观测面只记录 name@version 序列，未采集 ObservationMap，
        # 因此不调用断言器、不产出「假失败」；真实观测由后续组合根注入。
        _ = (scenario_id, verifier)
        return None

    def _base_case(self, item: PlannedCase, **fields: Any) -> CaseResult:
        tool_sequence = tuple(fields.pop("tool_sequence", ()))
        payload: dict[str, Any] = {
            "case_id": item.case_id,
            "engine": item.engine,
            "scenario": item.scenario,
            "fault_seed": item.fault_seed,
            "repetition": item.repetition,
            "execution_mode": self._execution_mode,
            "tool_sequence": tool_sequence,
            "tool_sequence_digest": canonical_digest(list(tool_sequence)),
            # HARNESS 模式：模型不可用 → schema_valid / rca_match 恒 None（R-01）。
            "schema_valid": None,
            "rca_match": None,
        }
        payload.update(fields)
        return CaseResult.model_validate(payload)

    def _failed_case(self, item: PlannedCase, exc: Exception) -> CaseResult:
        return self._base_case(
            item,
            injection_state=InjectionState.UNKNOWN,
            collection_status=CollectionStatus.UNKNOWN,
            tool_sequence=(),
            failure_reason=f"{type(exc).__name__}: {exc}",
        )

    def _write_artifacts(self, result: RunResult) -> None:
        """产物落盘：run_manifest.json / cases.jsonl / oracle_access.jsonl。"""

        directory = self._artifact_dir
        directory.mkdir(parents=True, exist_ok=True)
        manifest_payload = self._manifest.model_dump(mode="json")
        manifest_payload.update(
            {
                "run_id": result.run_id,
                "execution_mode": result.execution_mode,
                "run_status": result.status.value,
            }
        )
        (directory / "run_manifest.json").write_text(
            json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        case_lines = [
            json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for case in result.cases
        ]
        cases_text = "\n".join(case_lines) + ("\n" if case_lines else "")
        _assert_no_truth_leak(cases_text)
        (directory / "cases.jsonl").write_text(cases_text, encoding="utf-8")

        oracle_lines = [
            json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
            for record in getattr(self._oracle, "records", ())
        ]
        oracle_text = "\n".join(oracle_lines) + ("\n" if oracle_lines else "")
        _assert_no_truth_leak(oracle_text)
        (directory / "oracle_access.jsonl").write_text(oracle_text, encoding="utf-8")


def _assert_no_truth_leak(text: str) -> None:
    """产物文本硬闸门：出现任一 Oracle 真值字段名即拒绝写盘。"""

    for forbidden in FORBIDDEN_CASE_FIELDS:
        if forbidden in text:
            raise BenchmarkInvalidError(
                "产物文本命中 Oracle 真值字段名",
                details={"field": forbidden},
            )


__all__ = [
    "ADAPTER_READ_TOOLS",
    "ALLOWED_RUN_TRANSITIONS",
    "BenchmarkRunner",
    "CancelToken",
    "CaseResult",
    "DEFAULT_SUITE_PATH",
    "FORBIDDEN_CASE_FIELDS",
    "InjectionState",
    "KNOWN_ENV_BLOCKERS",
    "ObservationPort",
    "PlannedCase",
    "RegistryToolObserver",
    "RunResult",
    "SuiteCase",
    "SuiteSpec",
    "derive_run_id",
    "ensure_run_transition",
    "load_suite",
    "plan_cases",
]
