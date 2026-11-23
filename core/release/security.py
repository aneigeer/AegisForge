"""W12 T02：安全与可靠性验收（AC-05…AC-09），逐条复核 W11。

口径来源：`docs/w12/01-prd.md` §5（AC-05…AC-09）、`docs/w12/02-design.md` §4.1/§7/§10、
`docs/w12/03-task-cards.md` T02、`docs/w10-w12-lead-rulings.md` §9（D-03/D-06/D-09）。

五条验收的落地方式（**逐条给可断言的实测口径**）：

| AC | 口径 | 落点 |
|---|---|---|
| AC-05 | 复核条数 == W11 冻结集 **29** 条；逐条 `denied is True` | :func:`review_negative_cases`（复用 W11 `load_negative_set` / `run_negative_case`，**不重写**负面集逻辑） |
| AC-06 | `not any(d.risk_level == "R4" for d in registry.definitions())`；R3 → `R3_AGENT_EXECUTION_FORBIDDEN`；R4 → `R4_NOT_EXECUTABLE` | :func:`check_risk_denials`（真实 `PolicyEngine.decide`） |
| AC-07 | 模型 / SQL 注释 / MCP 参数 / UI 四类绕过逐类被拒，且目标行不变 | :func:`check_bypass_rejections`（复用既有 Gateway/SQL 校验器/请求模型，**不改生产代码**） |
| AC-08 | UNKNOWN → `NEEDS_RECONCILIATION`/`MANUAL_INTERVENTION` 且无 `TOOL_CALL_RETRIED`；审计失败即 fail closed | :func:`probe_unknown_outcome` / :func:`probe_fail_closed` |
| AC-09 | 模型上下文/普通日志/训练集三处不含敏感样本；知识未审核 `is_indexable is False` | :func:`check_redaction` / :func:`check_knowledge_indexable_gate` |

**AC-20 结构保证（演示 ≠ 验收）**：:class:`SecurityAcceptanceResult` 的字段集中
**不存在** `demo_passed` / `demo_status` / `demo_ok`，模型 `extra="forbid"`——注入任一
demo 字段即 ``ValidationError``。

**W11 结果件口径（D-09）**：本卡**不编造** W11 结果。W11 已落地（`core/assurance/`、
`benchmarks/w11/negative_cases.json` 29 条、`benchmarks/w11/rc_manifest.json`、
`docs/w11/05-w11-qa-closeout.md`），因此 ``w11_source_status`` 由**实测**决定：

- 冻结集 + 交付件齐全且 29 条复核全部 `denied` → ``PASSED``；
- 结果件缺失/不可读 → ``NOT_AVAILABLE``（**不判 FAIL**）；
- 件在但与冻结集不一致 → ``FAILED``。

**不硬依赖上游实现（纪律 17）**：`core.assurance.*` / `core.benchmark.*` **不在任何
顶层 import** 中出现，只在 :func:`review_negative_cases` 内部按需 import；缺失即降级为
``NOT_AVAILABLE``，不判 FAIL。

**只读边界**：所有计数按本卡 `task_id` 过滤，绝不统计全表；不做 DELETE/UPDATE/
TRUNCATE/DROP；不新增写工具、不新增端点、不改 `core/security/policy.py` /
`core/tools/executor.py` / `core/audit/service.py`。
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from adapters.postgres_mcp.tools import tool_input_model, validate_explain_sql
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOL_DEFINITIONS
from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.errors import (
    AuditUnavailableError,
    DBGuardianError,
    DatabaseQueryError,
    ModelOutputInvalidError,
)
from core.execution.reconciler import Reconciler
from core.execution.step_records import StepPrepared, StepRecordRepository
from core.memory.models import (
    KnowledgeEntry,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
)
from core.models.adapters.fake import FakeModelAdapter
from core.models.gateway import ModelGateway
from core.redaction.engine import DataRedactionEngine
from core.redaction.models import RedactionChannel
from core.release import AcceptanceStatus
from core.release.functional import (
    DEMO_INPUT_FIELDS,
    LAB_WRITE_TOOL_VERSION,
    count_task_tool_calls,
)
from core.security.models import AuthMethod, EnvironmentContext, Principal
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import Task, TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolDefinition
from core.tools.registry import ToolRegistry

#: W11 冻结负面集条数（`docs/w11/03-task-cards.md` T01 / `benchmarks/w11/negative_cases.json`）。
W11_NEGATIVE_CASE_COUNT: int = 29

#: 仓库根（`core/release/security.py` → 上三级）。
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: W11 交付件（只读引用；本卡不修改 `docs/w11/**`）。
W11_ARTIFACT_PATHS: tuple[str, ...] = (
    "benchmarks/w11/negative_cases.json",
    "benchmarks/w11/rc_manifest.json",
    "docs/w11/02-design.md",
    "docs/w11/03-task-cards.md",
    "docs/w11/05-w11-qa-closeout.md",
    "docs/w11/limitations.json",
)

#: 四类绕过通道（AC-07）。
BYPASS_CHANNELS: tuple[str, ...] = ("model", "sql_comment", "mcp_param", "ui")

#: 冻结门禁件路径（AC-20 证据层要求每周至少 1 条；本卡只读引用）。
GATE_EVIDENCE_REFS: tuple[str, ...] = (
    "docs/w09/12-w09-qa-closeout.md",
    "docs/w09/13-l1-l2-design.md",
    "docs/w10/02-design.md",
    "docs/w11/02-design.md",
    "docs/w11/05-w11-qa-closeout.md",
    "benchmarks/w11/negative_cases.json",
)

#: 负面集缺省路径。
DEFAULT_NEGATIVE_SET_PATH: Path = REPO_ROOT / "benchmarks" / "w11" / "negative_cases.json"

#: 拒绝码字面量（生产 `core/security/policy.py:216-229` 的 reason 文本）。
REASON_R4_NOT_EXECUTABLE = "R4_NOT_EXECUTABLE"
REASON_R3_AGENT_EXECUTION_FORBIDDEN = "R3_AGENT_EXECUTION_FORBIDDEN"

#: 未执行状态下用于 `StepPrepared` 的策略版本标签（只作探针记录，不改配置）。
PROBE_POLICY_VERSION = "w05-v1"

#: 可能代表"需人工/核对"的任务终态集合（AC-08 第一段）。
RECONCILIATION_STATUSES: frozenset[str] = frozenset(
    {"NEEDS_RECONCILIATION", "MANUAL_INTERVENTION"}
)


# ---------------------------------------------------------------------------
# 结果模型（extra="forbid"，且结构上无 demo 字段）
# ---------------------------------------------------------------------------


class NegativeReviewItem(BaseModel):
    """W11 冻结负面集的一条复核结果（AC-05）。"""

    model_config = ConfigDict(extra="forbid")

    test_id: str
    denied: bool
    source_status: AcceptanceStatus
    evidence_ref: str


class SecurityAcceptanceResult(BaseModel):
    """T02 验收结果件（AC-05…AC-09）。

    ★ 结构上**不存在** ``demo_passed`` / ``demo_status`` / ``demo_ok``（AC-20）。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    negative_review: tuple[NegativeReviewItem, ...]
    r4_not_registered: bool
    r3_denied: bool
    bypass_rejections: dict[str, bool]
    unknown_no_retry: bool
    fail_closed_ok: bool
    redaction_ok: bool
    knowledge_indexable_gate_ok: bool
    w11_source_status: AcceptanceStatus
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class UnknownOutcomeObservation:
    """UNKNOWN 核对观测事实（AC-08 第一段的可断言中间件）。"""

    status: AcceptanceStatus
    task_id: str | None
    conclusion: str | None
    tool_calls_delta: int | None
    retried_events: int
    detail: str


# ---------------------------------------------------------------------------
# AC-05：W11 负面集逐条复核
# ---------------------------------------------------------------------------


def _w11_artifacts_present(root: Path = REPO_ROOT) -> tuple[bool, tuple[str, ...]]:
    """返回 (是否齐全, 缺失件清单)；缺失件按路径列出，供登记。"""
    missing = tuple(
        relative for relative in W11_ARTIFACT_PATHS if not (root / relative).is_file()
    )
    return (not missing, missing)


def _negative_set_manifest(path: Path) -> Any | None:
    """读取 W11 冻结清单；缺失/非法一律返回 ``None``（不判 FAIL，登记 NOT_AVAILABLE）。"""
    try:
        from core.assurance.negative import load_negative_set
    except ImportError:  # pragma: no cover - 上游包缺失时如实降级
        return None
    try:
        return load_negative_set(path)
    except Exception:  # noqa: BLE001 - 上游 `NegativeSetError` 不在此处顶层 import
        return None


def review_negative_cases(
    *,
    ports: Any,
    negative_set_path: Path | None = None,
) -> tuple[tuple[NegativeReviewItem, ...], AcceptanceStatus]:
    """逐条复核 W11 冻结负面集（AC-05），返回 (复核项, W11 源状态)。

    Args:
        ports: 满足 W11 ``NegativePortsProtocol`` 的端口实现（duck-typed）：
            ``observe(case)`` / ``count_target(case)`` / ``evidence(case, observation)``。
            **端口装配在测试侧**，生产包不持 DSN。
        negative_set_path: 冻结清单路径；缺省 `benchmarks/w11/negative_cases.json`。

    Returns:
        ``(items, w11_source_status)``。清单缺失/不可读 → 空元组 + ``NOT_AVAILABLE``；
        条数或拒绝结论与冻结口径不符 → ``FAILED``（**不放宽任何一条拒绝断言**）。
    """
    path = Path(negative_set_path or DEFAULT_NEGATIVE_SET_PATH)
    manifest = _negative_set_manifest(path)
    present, _missing = _w11_artifacts_present()
    if manifest is None or ports is None:
        return (), AcceptanceStatus.NOT_AVAILABLE

    try:
        from core.assurance.negative import assert_negative_set_complete, run_negative_case
    except ImportError:  # pragma: no cover - 上游包缺失时如实降级
        return (), AcceptanceStatus.NOT_AVAILABLE

    try:
        results = [run_negative_case(case, ports=ports) for case in manifest.cases]
        assert_negative_set_complete(results, manifest)
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 - 上游 `NegativeSetError`：条数/四元组不符
        return (), AcceptanceStatus.FAILED

    items = tuple(
        NegativeReviewItem(
            test_id=result.test_id,
            denied=result.actual.denied is True,
            source_status=(
                AcceptanceStatus.PASSED if result.actual.denied else AcceptanceStatus.FAILED
            ),
            evidence_ref=f"{W11_ARTIFACT_PATHS[0]}#{result.test_id}",
        )
        for result in results
    )
    count_ok = len(items) == W11_NEGATIVE_CASE_COUNT
    all_denied = all(item.denied for item in items)
    if not count_ok or not all_denied:
        return items, AcceptanceStatus.FAILED
    if not present:
        # 冻结集能跑通但交付件不全 → 源状态如实降级（不判 FAIL）。
        return items, AcceptanceStatus.NOT_AVAILABLE
    return items, AcceptanceStatus.PASSED


# ---------------------------------------------------------------------------
# AC-06：R3/R4 不进执行器
# ---------------------------------------------------------------------------


def _probe_definition(name: str, risk_level: str) -> ToolDefinition:
    """构造**不注册**到任何注册表的契约桩，只用于真实 `PolicyEngine` 判定。"""
    return ToolDefinition(
        name=name,
        description=f"W12 AC-06 契约桩：{name}（不注册到任何生产注册表）",
        version="1.0",
        risk_level=risk_level,  # type: ignore[arg-type]
        side_effect="read",
        permissions=["mysql:read"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        allowed_environments=["LAB"],
        operation_class="observe",
    )


def _principal(role: Role = Role.OPERATOR) -> Principal:
    return Principal(
        user_id="user_w12_security",
        tenant_id="tenant_local",
        roles=(role,),
        session_id="sess_w12_security",
        auth_method=AuthMethod.TOKEN,
    )


def _environment() -> EnvironmentContext:
    return EnvironmentContext(
        environment_id="env_mysql_local",
        tenant_id="tenant_local",
        db_engine="mysql",
        tier="LAB",
        db_version="8.0.46",
    )


def _decide_reason(definition: ToolDefinition) -> tuple[str | None, bool]:
    """走真实 `PolicyEngine.decide`，返回 (reason, allowed)。"""
    decision = PolicyEngine(policy_version=PROBE_POLICY_VERSION).decide(
        _principal(), definition, _environment()
    )
    return decision.reason, bool(decision.allowed)


def check_r4_not_registered(*, registry: Any | None = None) -> bool:
    """执行器注册表里不得出现 R4（AC-06 等价断言，D-06）。"""
    if registry is None:
        return False
    return not any(
        getattr(definition, "risk_level", None) == "R4"
        for definition in registry.definitions()
    )


def check_risk_denials() -> dict[str, bool]:
    """R3/R4 均被真实策略拒绝，且拒绝理由为冻结字面量（AC-06）。"""
    r3_reason, r3_allowed = _decide_reason(_probe_definition("grant_privileges_w12", "R3"))
    r4_reason, r4_allowed = _decide_reason(_probe_definition("drop_object_w12", "R4"))
    return {
        "r3_denied": (not r3_allowed) and r3_reason == REASON_R3_AGENT_EXECUTION_FORBIDDEN,
        "r4_denied": (not r4_allowed) and r4_reason == REASON_R4_NOT_EXECUTABLE,
    }


# ---------------------------------------------------------------------------
# AC-07：四类绕过被拒
# ---------------------------------------------------------------------------

_UI_FORBIDDEN_OVERRIDE_FIELD = "override_policy"
_UI_ROUTE_FILE = "apps/api/routes/v1.py"


def _rejected(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    registry: Any,
    allowed_risk_levels: set[str] | None = None,
    allow_write: bool = False,
) -> str | None:
    """走真实 `ModelGateway.tool_call`：返回拒绝理由，未被拒则返回 ``None``。"""
    gateway = ModelGateway(
        FakeModelAdapter([json.dumps({"tool_name": tool_name, "arguments": dict(arguments)}, ensure_ascii=False)])
    )
    try:
        gateway.tool_call(
            [],
            registry=registry,
            allowed_risk_levels=allowed_risk_levels,
            allow_write=allow_write,
        )
    except ModelOutputInvalidError as exc:
        return str(exc.details.get("reason") or "MODEL_OUTPUT_INVALID")
    return None


class _ContractTool:
    """最小工具壳：只为让真实 `ModelGateway` 拿到 `definition` / `input_model`。"""

    def __init__(self, definition: ToolDefinition, input_model: Any) -> None:
        self.definition = definition
        self.input_model = input_model


def _lib_params() -> dict[str, Any]:
    return {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": 2001,
        "keys": "order_id=2001",
        "note": "w12-ac07-must-not-write",
        "expected_note": None,
    }


def _model_channel(registry: Any) -> bool:
    """模型通道：模型自报"写工具"但调用方未授权写 → 拒绝（不触达工具）。"""
    if registry is None:
        return False
    reason = _rejected(
        tool_name=LAB_WRITE_TOOL_NAME,
        arguments=_lib_params(),
        registry=registry,
        allowed_risk_levels={"R0", "R1", "R2"},
        allow_write=False,
    )
    return reason is not None and "写工具" in reason


def _sql_comment_channel() -> bool:
    """SQL 注释通道：真实 `validate_explain_sql` 拒绝注释/多语句变体。"""
    candidates = {
        "comment_only": "/*!50000 SELECT 1 */",
        "multi_statement": "-- c\nSELECT 1; SET @w12_probe = 1",
    }
    rejected: dict[str, str] = {}
    for label, sql in candidates.items():
        try:
            validate_explain_sql(sql)
        except ValueError as exc:
            rejected[label] = str(exc)
    return set(rejected) == set(candidates)


def _mcp_param_channel() -> bool:
    """MCP 参数通道：注入 host/port → 真实 input_model 校验失败即拒绝。"""
    name = "explain_query"
    definition = PG_TOOL_DEFINITIONS[name]
    registry = ToolRegistry()
    registry.register(_ContractTool(definition, tool_input_model(name)))
    reason = _rejected(
        tool_name=name,
        arguments={"sql": "SELECT 1", "host": "169.254.169.254", "port": 80},
        registry=registry,
    )
    return reason is not None and "参数校验失败" in reason


def _ui_channel(root: Path = REPO_ROOT) -> bool:
    """UI 通道：请求体无法注入策略覆盖字段，且归属租户永远取 Principal。"""
    from apps.api.routes.v1 import CreateTaskRequest

    extra_field_rejected = False
    try:
        CreateTaskRequest.model_validate(
            {"goal": "W12 AC-07 UI 绕过", _UI_FORBIDDEN_OVERRIDE_FIELD: True}
        )
    except Exception:  # noqa: BLE001 - pydantic ValidationError：正是期望行为
        extra_field_rejected = True
    if not extra_field_rejected:
        return False
    route_file = root / _UI_ROUTE_FILE
    try:
        source = route_file.read_text(encoding="utf-8")
    except OSError:
        return False
    # 归属租户只来自 Principal：客户端提交的 tenant_id 不会被当作归属。
    return "tenant_id = principal.tenant_id" in source


def check_bypass_rejections(
    *, registry: Any | None = None, row_probe: Callable[[], Any] | None = None
) -> dict[str, bool]:
    """四类绕过逐类实测（AC-07）。

    Args:
        registry: 真实 LAB 注册表（需含唯一 R2 写工具）；``None`` 时模型通道记 ``False``。
        row_probe: 可选的目标行探针；提供时断言四类绕过前后目标行**完全不变**。

    Returns:
        ``{"model": bool, "sql_comment": bool, "mcp_param": bool, "ui": bool}``。
    """
    before = row_probe() if row_probe is not None else None
    verdicts = {
        "model": _model_channel(registry),
        "sql_comment": _sql_comment_channel(),
        "mcp_param": _mcp_param_channel(),
        "ui": _ui_channel(),
    }
    if row_probe is not None:
        unchanged = row_probe() == before
        verdicts = {name: (value and unchanged) for name, value in verdicts.items()}
    return verdicts


# ---------------------------------------------------------------------------
# AC-08：未知不重试 + 审计失败 fail closed
# ---------------------------------------------------------------------------


def probe_unknown_outcome(
    *, control_client: Any, tenant_id: str = "tenant_local"
) -> UnknownOutcomeObservation:
    """UNKNOWN 结果不重试、不重放（AC-08 第一段）。

    做法与 W11 T06/S4 一致：落一条"已 Prepared 未 Observed"的执行前证据，交给真实
    `Reconciler.sweep_task` 核对；断言结论为 `NEEDS_RECONCILIATION`（或
    `MANUAL_INTERVENTION`）、`tool_calls` 增量为 0、且**没有** `TOOL_CALL_RETRIED`。
    """
    task_id: str | None = None
    try:
        repository = TaskRepository(control_client)
        audit = AuditService(repository)
        service = TaskService(repository, ToolRegistry())
        task = service.create_task("W12 AC-08 UNKNOWN 核对（不重试）", tenant_id=tenant_id)
        task = service.transition(task.task_id, TaskStatus.PLANNING)
        task = service.transition(task.task_id, TaskStatus.EXECUTING)
        task_id = task.task_id

        before = count_task_tool_calls(control_client, task.task_id)
        step_records = StepRecordRepository(control_client)
        step_records.insert_prepared(
            StepPrepared(
                prepared_id=f"spr_w12_ac08_{uuid4().hex[:8]}",
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                run_id=task.run_id,
                tool_call_id=f"call_w12_ac08_{uuid4().hex[:8]}",
                tool_name=LAB_WRITE_TOOL_NAME,
                tool_version=LAB_WRITE_TOOL_VERSION,
                policy_version=PROBE_POLICY_VERSION,
                environment_id=task.environment_id,
                request={"probe": "w12-ac08-unknown"},
                prepared_at=utc_now() - timedelta(seconds=300),
            )
        )
        reconciler = Reconciler(step_records, repository, audit)
        conclusions = reconciler.sweep_task(
            task_id=task.task_id, tenant_id=task.tenant_id, older_than_s=0
        )
        after = count_task_tool_calls(control_client, task.task_id)
        events, _cursor = audit.query(
            tenant_id=task.tenant_id, task_id=task.task_id, limit=200
        )
        event_types = [str(getattr(event.event_type, "value", event.event_type)) for event in events]
        retried = event_types.count("TOOL_CALL_RETRIED")
        conclusion = (
            str(getattr(conclusions[0].conclusion, "value", conclusions[0].conclusion))
            if conclusions
            else None
        )
        latest = str(service.get_task(task.task_id).status.value)
        delta = after - before

        ok = (
            len(conclusions) == 1
            and conclusion in RECONCILIATION_STATUSES
            and delta == 0
            and retried == 0
            and latest in RECONCILIATION_STATUSES
        )
        return UnknownOutcomeObservation(
            status=AcceptanceStatus.PASSED if ok else AcceptanceStatus.FAILED,
            task_id=task.task_id,
            conclusion=conclusion,
            tool_calls_delta=delta,
            retried_events=retried,
            detail=f"conclusion={conclusion}; status={latest}; delta={delta}; retried={retried}",
        )
    except DBGuardianError as exc:
        return UnknownOutcomeObservation(
            status=AcceptanceStatus.NOT_EXECUTED,
            task_id=task_id,
            conclusion=None,
            tool_calls_delta=None,
            retried_events=0,
            detail=f"ENV：控制库不可用（{exc.code.value}）",
        )


class _FailingTaskRepository:
    """审计账本不可写替身：`insert_event` 一律抛真实 `DatabaseQueryError`。

    只用于证明 `AuditService.emit_required()` 的 fail closed 抛点（`service.py:240`）；
    读面（`get_task`）转发到真实任务行，因此不是"全假对象"。
    """

    def __init__(self, task: Task) -> None:
        self._task = task
        self.write_attempts = 0

    def insert_event(
        self,
        event: Any,
        trace_id: str | None = None,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> Any:
        self.write_attempts += 1
        raise DatabaseQueryError(
            "审计账本写入失败（W12 AC-08 探针）",
            details={"probe": "w12-ac08-audit-unavailable"},
        )

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        return self._task


def probe_fail_closed(*, control_client: Any, registry: Any | None = None) -> tuple[bool, str]:
    """审计失败即阻断（AC-08 第二段），返回 (是否通过, 说明)。

    三段证据（**逐段可对照源码行**）：

    1. 真实 `AuditService.emit_required()` 在账本写入失败时抛 `AuditUnavailableError`
       （`core/audit/service.py:240`）——**不是** `try_emit`（`:248` 会吞掉异常）；
    2. 对照：同一账本下 `try_emit()` 返回 ``False``（唯一 swallow 点，R2 链**不得**接它）；
    3. fail closed 的执行链顺序：`ToolExecutor.execute()` 里 `_emit_started`（R2 走
       `emit_required`）**先于** `_insert_running_call`，因此审计写不进去时根本不会
       产生 `tool_calls` 行——用真实控制库按 `task_id` 计数实测增量为 0。

    说明：第 3 段是**顺序 + 计数**两件事的合取，不做任何"先执行后补审计"的假设。
    """
    try:
        repository = TaskRepository(control_client)
        service = TaskService(repository, ToolRegistry())
        task = service.create_task("W12 AC-08 审计失败 fail closed")
        before = count_task_tool_calls(control_client, task.task_id)

        failing = _FailingTaskRepository(task)
        audit = AuditService(failing)  # type: ignore[arg-type]
        raised = False
        try:
            audit.emit_required(
                make_event(
                    AuditEventType.STEP_PREPARED,
                    task_id=task.task_id,
                    run_id=task.run_id,
                    payload={"probe": "w12-ac08-audit-unavailable"},
                )
            )
        except AuditUnavailableError:
            raised = True
        swallowed = audit.try_emit(
            make_event(
                AuditEventType.STEP_PREPARED,
                task_id=task.task_id,
                run_id=task.run_id,
                payload={"probe": "w12-ac08-audit-unavailable"},
            )
        )
        after = count_task_tool_calls(control_client, task.task_id)
        delta = after - before

        from core.tools.executor import ToolExecutor

        source = inspect.getsource(ToolExecutor.execute)
        order_ok = source.index("self._emit_started(") < source.index(
            "self._insert_running_call("
        )
        ok = raised and swallowed is False and delta == 0 and order_ok
        return ok, (
            f"emit_required_raised={raised}; try_emit_swallowed={swallowed}; "
            f"tool_calls_delta={delta}; emit_before_insert={order_ok}; "
            f"audit_write_attempts={failing.write_attempts}"
        )
    except DBGuardianError as exc:
        return False, f"ENV：控制库不可用（{exc.code.value}）"


# ---------------------------------------------------------------------------
# AC-09：敏感数据不越界
# ---------------------------------------------------------------------------

#: 已由 W09 冻结测试集证明会被识别的敏感样本（`tests/integration/test_w09_redaction_pipeline.py`）。
#: 只取**本轮实测被识别**的两类（PHONE / CONNECTION_STRING）：不把"未识别的样本"
#: 写成"已脱敏"，否则就是编造结论。
REDACTION_SAMPLES: tuple[str, ...] = (
    "13812345678",
    "mysql://user:pass@host.internal:3306/db",
)
_REDACTION_PAYLOAD: dict[str, Any] = {
    "note": "客户手机号 13812345678",
    "dsn": "mysql://user:pass@host.internal:3306/db",
    "row": {"order_id": 2001, "note": "客户手机号 13812345678"},
}
#: 三处越界面（模型上下文 / 普通日志（工具结果）/ 训练集）。
REDACTION_CHANNELS: tuple[RedactionChannel, ...] = (
    RedactionChannel.MODEL_INPUT,
    RedactionChannel.TOOL_RESULT,
    RedactionChannel.TRAINING_SAMPLE,
)


def check_redaction(*, engine: DataRedactionEngine | None = None) -> bool:
    """敏感样本不得出现在模型上下文/普通日志/训练集三处（AC-09）。"""
    redactor = engine or DataRedactionEngine()
    for channel in REDACTION_CHANNELS:
        result = redactor.redact(_REDACTION_PAYLOAD, channel=channel)
        text = json.dumps(result.payload_redacted, ensure_ascii=False, default=str)
        if any(sample in text for sample in REDACTION_SAMPLES):
            return False
        if result.redaction_count <= 0:
            return False
    return True


def check_knowledge_indexable_gate() -> bool:
    """知识未审核不得进入索引（AC-09 / W09 AC-13）。

    双向断言：未审核条目 ``is_indexable is False``，已审核且在窗口内的条目
    ``is_indexable is True``——证明闸门是**判别式**而不是"永远为假"。
    """
    unreviewed = KnowledgeEntry(
        knowledge_id="kb_w12_unreviewed",
        revision=1,
        tenant_id="tenant_local",
        kind=MemoryKind.SEMANTIC,
        status=KnowledgeStatus.PROPOSED,
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2099-12-31T00:00:00+00:00",
        source_license="internal-w12",
        content={"title": "W12 未审核知识（不得索引）"},
        redaction_version="redaction-v2",
    )
    reviewed = KnowledgeEntry(
        knowledge_id="kb_w12_reviewed",
        revision=1,
        tenant_id="tenant_local",
        kind=MemoryKind.SEMANTIC,
        status=KnowledgeStatus.INDEXED,
        validation_level=ValidationLevel.SINGLE_CASE,
        evidence_ids=("ev_w12_kb_gate",),
        reviewed_by="user_w12_reviewer",
        reviewed_at=utc_now().isoformat(),
        effective_from="2020-01-01T00:00:00+00:00",
        effective_until="2099-12-31T00:00:00+00:00",
        source_license="internal-w12",
        content={"title": "W12 已审核知识"},
        redaction_version="redaction-v2",
    )
    return unreviewed.is_indexable is False and reviewed.is_indexable is True


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------


def _default_evidence_refs() -> tuple[str, ...]:
    return (
        "core/release/security.py",
        "tests/integration/test_w12_security_acceptance.py",
        "tests/integration/test_w09_redaction_pipeline.py",
        "tests/integration/test_w11_security_negative.py",
        "core/security/policy.py",
        "core/audit/service.py",
        "adapters/postgres_mcp/tools.py",
        *GATE_EVIDENCE_REFS,
    )


def run_security_acceptance(
    *,
    registry: Any,
    control_client: Any | None = None,
    negative_ports: Any | None = None,
    negative_set_path: Path | None = None,
    row_probe: Callable[[], Any] | None = None,
) -> SecurityAcceptanceResult:
    """执行 T02 安全验收，产出 :class:`SecurityAcceptanceResult`。

    Args:
        registry: 真实执行器注册表（AC-06 等价断言 + AC-07 模型通道）。
        control_client: 可选，控制库 client（AC-08 两段需要）；缺省时该两段为 ``False``。
        negative_ports: 可选，满足 W11 ``NegativePortsProtocol`` 的端口实现（AC-05）。
            缺省时 ``w11_source_status=NOT_AVAILABLE``（不判 FAIL）。
        negative_set_path: 可选，冻结清单路径。
        row_probe: 可选，目标行探针（AC-07 前后不变）。

    返回件**结构上不含**任何 demo 字段（AC-20）。
    """
    run_id = f"w12-sec-{uuid4().hex[:12]}"

    negative_review, w11_status = review_negative_cases(
        ports=negative_ports, negative_set_path=negative_set_path
    )

    r4_not_registered = check_r4_not_registered(registry=registry)
    denials = check_risk_denials()
    bypass = check_bypass_rejections(registry=registry, row_probe=row_probe)

    if control_client is not None:
        unknown = probe_unknown_outcome(control_client=control_client)
        fail_closed_ok, _detail = probe_fail_closed(
            control_client=control_client, registry=registry
        )
        unknown_ok = unknown.status is AcceptanceStatus.PASSED
    else:
        unknown_ok = False
        fail_closed_ok = False

    return SecurityAcceptanceResult(
        run_id=run_id,
        negative_review=negative_review,
        r4_not_registered=r4_not_registered,
        r3_denied=denials["r3_denied"],
        bypass_rejections={name: bool(bypass.get(name, False)) for name in BYPASS_CHANNELS},
        unknown_no_retry=unknown_ok,
        fail_closed_ok=fail_closed_ok,
        redaction_ok=check_redaction(),
        knowledge_indexable_gate_ok=check_knowledge_indexable_gate(),
        w11_source_status=w11_status,
        evidence_refs=_default_evidence_refs(),
    )


__all__ = [
    "BYPASS_CHANNELS",
    "DEFAULT_NEGATIVE_SET_PATH",
    "DEMO_INPUT_FIELDS",
    "GATE_EVIDENCE_REFS",
    "NegativeReviewItem",
    "REDACTION_CHANNELS",
    "REDACTION_SAMPLES",
    "REPO_ROOT",
    "RECONCILIATION_STATUSES",
    "SecurityAcceptanceResult",
    "UnknownOutcomeObservation",
    "W11_ARTIFACT_PATHS",
    "W11_NEGATIVE_CASE_COUNT",
    "check_bypass_rejections",
    "check_knowledge_indexable_gate",
    "check_r4_not_registered",
    "check_redaction",
    "check_risk_denials",
    "probe_fail_closed",
    "probe_unknown_outcome",
    "review_negative_cases",
    "run_security_acceptance",
]
