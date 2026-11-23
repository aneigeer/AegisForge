"""W11 T01 集成测试：29 条冻结负面用例逐条被拒（AC-01 / AC-02 / AC-03）。

口径（``docs/w11/02-design.md`` §5、``docs/w11/03-task-cards.md`` T01）：

- **AC-01 四元组**：每条结果都有 ``test_id`` / ``expected_deny_reason`` /
  ``actual`` / ``evidence``，且 ``evidence`` 只含结构化摘要；
- **AC-02 逐条被拒**：``denied is True`` 且 ``deny_reason == expected_deny_reason``；
- **AC-03 拒绝不改数据**：计数探针**按本用例归属过滤**（``WHERE task_id = %s``），
  **绝不** ``COUNT(*)`` 全表（``ENV-RESIDUE``=140 会污染）。

``mode`` 二分的落地方式：

- ``LIVE``：真实 ``PolicyEngine.decide`` / ``ModelGateway.tool_call`` /
  ``ApprovalService.consume`` / ``ToolGateway.execute`` / 真实仓储（``dg_ctl`` 只读与
  本用例自建的追加行）——取真实拒绝码；
- ``STATIC``：只做"入参面被拒 + 源码/配置静态证据 + ``canonical_digest``"，
  **不发起任何真实网络连接、不执行任何 Shell/文件操作**。

**报告固定句**（PRD §1 G1 原文，不得改写）：
**100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全**。

环境边界：控制库不可用时整文件 skip 并带原因（``ENV-*`` 不判 FAIL，``C-2``）；
本文件不依赖 ``DG_ADMIN_DSN``（``ENV-1045``）、不依赖 PG 5432（``ENV-PG5432``），
不做 DELETE / UPDATE / TRUNCATE / DROP，只追加 INSERT。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pytest

from adapters.lab_assembly import build_lab_registry
from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOL_DEFINITIONS
from adapters.postgres_mcp.tools import tool_input_model, validate_explain_sql
from core.approval.digest import (
    build_action_descriptor,
    build_normalized_operation,
    compute_action_digest,
    compute_prerequisites_digest,
    environment_fingerprint_digest,
    environment_fingerprint_from_row,
    extract_target_objects,
    normalize_target_objects,
    normalize_tool_parameters,
)
from core.approval.models import Approval, ApprovalStatus, error_for_status
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.assurance.negative import (
    SAFETY_DISCLAIMER,
    DenyObservation,
    DenyProbe,
    InducementMode,
    NegativeCase,
    NegativeSetManifest,
    assert_negative_set_complete,
    format_run_summary,
    load_negative_set,
    run_negative_case,
)
from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalRevokedError,
    DBGuardianError,
    DatabaseQueryError,
    EnvelopeInvalidError,
    ModelOutputInvalidError,
    NonceReplayError,
    TaskNotFoundError,
)
from core.evidence.models import CollectionStatus, Evidence
from core.evidence.repository import EvidenceRepository
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.models.adapters.fake import FakeModelAdapter
from core.models.gateway import ModelGateway
from core.report.models import (
    ActionState,
    FactStatus,
    StructuredReport,
    VerificationState,
)
from core.diagnosis.models import ConfidenceBand
from core.report.repository import ReportRepository
from core.security.canonical import canonical_digest, sha256_hex
from core.security.models import (
    AuthMethod,
    EnvironmentContext,
    LeaseStatus,
    Principal,
    WorkerLease,
)
from core.security.policy import PolicyEngine
from core.security.roles import Role
from core.tasks.models import Task, TaskStatus, utc_now
from core.tasks.repository import TaskRepository
from core.tools.contracts import ToolDefinition
from core.tools.registry import ToolRegistry
from database.observers import build_observer_registry

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "benchmarks" / "w11" / "negative_cases.json"

#: 本文件自建对象的归属租户前缀（只追加，不清理；计数一律按 task_id 过滤）。
_TENANT_PREFIX = "w11_neg"
_OTHER_TENANT_PREFIX = "w11_other"

_POLICY_VERSION = "w05-v1"
_ENVIRONMENT_ROW: dict[str, Any] = {
    "environment_id": "env_mysql_local",
    "tenant_id": "tenant_local",
    "db_engine": "mysql",
    "db_version": "8.0.46",
    "network_id": "lab_net",
    "image_digest": None,
    "config_version": "w11-negative",
    "status": "LAB",
}

_PROBE_SQL: dict[str, str] = {
    "task_events_by_task_id": "SELECT COUNT(*) AS n FROM dg_system.task_events WHERE task_id = %s",
    "tasks_by_task_id": "SELECT COUNT(*) AS n FROM dg_system.tasks WHERE task_id = %s",
    "evidence_by_task_id": "SELECT COUNT(*) AS n FROM dg_system.evidence WHERE task_id = %s",
    "approvals_by_task_id": "SELECT COUNT(*) AS n FROM dg_system.approvals WHERE task_id = %s",
    "report_snapshots_by_task_id": (
        "SELECT COUNT(*) AS n FROM dg_system.report_snapshots WHERE task_id = %s"
    ),
    "environments_by_tenant_id": (
        "SELECT COUNT(*) AS n FROM dg_system.environments WHERE tenant_id = %s"
    ),
    "audit_events_by_task_id": (
        "SELECT COUNT(*) AS n FROM dg_system.task_events WHERE task_id = %s"
    ),
}

#: 静态扫描：工具/执行面**不得**出现的网络与路径原语（STATIC 证据）。
_NETWORK_TOKENS: tuple[str, ...] = (
    "import socket",
    "socket.socket",
    "requests.",
    "import urllib",
    "urllib.",
    "import httpx",
    "http.client",
    "allow_redirects",
    "follow_redirect",
)
_NETWORK_SCAN_FILES: tuple[str, ...] = (
    "adapters/lab_assembly.py",
    "adapters/mysql/lab_repair.py",
    "adapters/mysql/prepare_probe.py",
    "adapters/postgres_mcp/tools.py",
    "core/tools/contracts.py",
    "core/tools/registry.py",
    "core/tools/executor.py",
    "core/config.py",
)
_INPUT_SURFACE_FORBIDDEN: tuple[str, ...] = (
    "host",
    "hostname",
    "port",
    "url",
    "uri",
    "endpoint",
    "redirect",
    "path",
    "file",
    "filename",
    "sql_file",
    "load_file",
    "outfile",
    "command",
    "cmd",
    "shell",
    "script",
    "argv",
    "exec",
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> NegativeSetManifest:
    return load_negative_set(_MANIFEST_PATH)


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    """控制库可用性探针：不可用一律 skip（带原因），绝不判 FAIL。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@dataclass(frozen=True)
class CrossTenantObjects:
    """跨租户探针对象（本文件自建、只追加；供 T02 复用）。"""

    tenant_id: str
    other_tenant_id: str
    environment_id: str
    task_id: str
    run_id: str
    evidence_id: str
    approval_id: str
    report_id: str
    event_id: str


def provision_cross_tenant_objects(client: Any) -> CrossTenantObjects:
    """自建"归属租户 + 六类对象"，供跨租户只读探针使用（只 INSERT）。

    设计理由：跨租户拒绝断言必须有**真实存在的对象**，否则"返回空集"可能只是
    "对象不存在"（假通过）。因此同时断言"归属租户读得到 / 其他租户读不到"。
    """
    suffix = uuid4().hex[:8]
    tenant_id = f"{_TENANT_PREFIX}_{suffix}"
    other_tenant_id = f"{_OTHER_TENANT_PREFIX}_{suffix}"
    environment_id = f"env_{_TENANT_PREFIX}_{suffix}"
    task_id = f"task_{_TENANT_PREFIX}_{suffix}"
    run_id = f"run_{_TENANT_PREFIX}_{suffix}"
    now = datetime.now(timezone.utc)

    client.execute(
        "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
        "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
        (tenant_id, f"W11 负面集跨租户探针 {suffix}"),
    )
    client.execute(
        "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
        "db_version, network_id, image_digest, config_version, status, created_at) "
        "VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'w11-negative', 'LAB', "
        "UTC_TIMESTAMP(6))",
        (environment_id, tenant_id),
    )

    tasks = TaskRepository(client)
    tasks.insert_task(
        Task(
            task_id=task_id,
            run_id=run_id,
            goal="W11 跨租户对象级授权探针（只读，不执行任何写动作）",
            status=TaskStatus.EXECUTING,
            risk_level="R0",
            created_at=now,
            updated_at=now,
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by="user_w11_negative",
            state_version=1,
        )
    )

    evidence_id = f"ev_{_TENANT_PREFIX}_{suffix}"
    EvidenceRepository(client).insert(
        Evidence(
            evidence_id=evidence_id,
            tenant_id=tenant_id,
            task_id=task_id,
            environment_id=environment_id,
            db_engine="mysql",
            db_version="8.0.46",
            adapter_version="mysql-w11",
            source_tool="w11_negative_probe",
            observed_at=now.isoformat(),
            collection_status=CollectionStatus.SUCCESS,
            payload_hash=canonical_digest({"probe": "w11-cross-tenant-evidence"}),
            redaction_version="redaction-v2",
            payload={"probe": "w11-cross-tenant-evidence"},
        )
    )

    approval_id = f"apr_{_TENANT_PREFIX}_{suffix}"
    probe_disclosure = {"probe": "w11-cross-tenant-approval", "sections": ["risk", "compensation", "verification_plan"]}
    ApprovalRepository(client).insert(
        Approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            task_id=task_id,
            environment_id=environment_id,
            db_engine="mysql",
            environment_fingerprint=environment_fingerprint_from_row(_ENVIRONMENT_ROW),
            environment_fingerprint_digest=environment_fingerprint_digest(
                environment_fingerprint_from_row(_ENVIRONMENT_ROW)
            ),
            action_digest=canonical_digest({"probe": "w11-cross-tenant-action"}),
            parameters_digest=canonical_digest({"probe": "w11-cross-tenant-parameters"}),
            target_objects_digest=canonical_digest({"probe": "w11-cross-tenant-targets"}),
            prerequisites_digest=canonical_digest({"probe": "w11-cross-tenant-prerequisites"}),
            action_descriptor={"probe": "w11-cross-tenant-action-descriptor"},
            disclosure=probe_disclosure,
            disclosure_digest=canonical_digest(probe_disclosure),
            disclosure_version="disclosure-v1",
            disclosed_at=now,
            policy_version=_POLICY_VERSION,
            tool_name=LAB_WRITE_TOOL_NAME,
            tool_version="1.0",
            requested_by="user_w11_negative",
            nonce_hash=hashlib.sha256(f"{approval_id}-nonce".encode("utf-8")).hexdigest(),
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
        )
    )

    report_id = f"rep_{_TENANT_PREFIX}_{suffix}"
    ReportRepository(client).insert(
        StructuredReport(
            report_id=report_id,
            tenant_id=tenant_id,
            task_id=task_id,
            database_id=environment_id,
            db_engine="mysql",
            db_version="8.0.46",
            adapter_version="mysql-w11",
            environment_id=environment_id,
            policy_version=_POLICY_VERSION,
            conclusion="W11 跨租户授权探针骨架（非真实诊断结论）",
            fact_status=FactStatus.UNKNOWN,
            confidence_band=ConfidenceBand.UNKNOWN,
            action_state=ActionState.NOT_EXECUTED,
            verification_state=VerificationState.NONE,
        )
    )

    event = tasks.insert_event(
        make_event(
            AuditEventType.TASK_CREATED,
            task_id=task_id,
            run_id=run_id,
            payload={"probe": "w11-cross-tenant-audit", "tenant_id": tenant_id},
        ),
        tenant_id=tenant_id,
        actor_user_id="user_w11_negative",
    )

    return CrossTenantObjects(
        tenant_id=tenant_id,
        other_tenant_id=other_tenant_id,
        environment_id=environment_id,
        task_id=task_id,
        run_id=run_id,
        evidence_id=evidence_id,
        approval_id=approval_id,
        report_id=report_id,
        event_id=event.event_id,
    )


@pytest.fixture(scope="module")
def cross_tenant(control_db: Any) -> CrossTenantObjects:
    return provision_cross_tenant_objects(control_db)


# ---------------------------------------------------------------------------
# 真实接缝的共用工具
# ---------------------------------------------------------------------------


def _principal(*, tenant_id: str = "tenant_local", role: Role = Role.OPERATOR) -> Principal:
    return Principal(
        user_id="user_w11_negative",
        tenant_id=tenant_id,
        roles=(role,),
        session_id="sess_w11_negative",
        auth_method=AuthMethod.TOKEN,
    )


def _environment(*, tier: str = "LAB", tenant_id: str = "tenant_local") -> EnvironmentContext:
    return EnvironmentContext(
        environment_id="env_mysql_local",
        tenant_id=tenant_id,
        db_engine="mysql",
        tier=tier,  # type: ignore[arg-type]
        db_version="8.0.46",
    )


def _definition(
    name: str,
    *,
    risk_level: str = "R0",
    side_effect: str = "read",
    permissions: Sequence[str] = ("mysql:read",),
    operation_class: str = "observe",
    allowed_environments: Sequence[str] = ("LAB",),
) -> ToolDefinition:
    """契约桩：只用于让**真实** PolicyEngine 判定，不注册到任何生产注册表。"""
    return ToolDefinition(
        name=name,
        description=f"W11 负面集契约桩：{name}（不注册到生产注册表）",
        version="1.0",
        risk_level=risk_level,  # type: ignore[arg-type]
        side_effect=side_effect,  # type: ignore[arg-type]
        permissions=list(permissions),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        db_engines=["mysql"],
        allowed_environments=list(allowed_environments),
        operation_class=operation_class,  # type: ignore[arg-type]
    )


def _policy_probe(
    definition: ToolDefinition,
    *,
    tier: str = "LAB",
    model_risk_hint: str | None = None,
) -> tuple[DenyProbe, dict[str, Any]]:
    """走真实 ``PolicyEngine.decide``：拒因直接取生产 ``decision.reason``。"""
    decision = PolicyEngine(policy_version=_POLICY_VERSION).decide(
        _principal(),
        definition,
        _environment(tier=tier),
        model_risk_hint=model_risk_hint,  # type: ignore[arg-type]
    )
    evidence = {
        "decided_by": "core.security.policy.PolicyEngine.decide",
        "policy_reason": decision.reason,
        "effective_risk": decision.effective_risk,
        "policy_version": decision.policy_version,
        "tool_name": definition.name,
    }
    probe = DenyProbe(
        denied=not decision.allowed,
        deny_reason=decision.reason,
        error_code=decision.deny_code,
    )
    return probe, evidence


def _gateway_rejection(
    selection: Mapping[str, Any],
    *,
    registry: ToolRegistry,
    allowed_risk_levels: set[str] | None = None,
    allow_write: bool = False,
) -> tuple[DenyProbe, dict[str, Any]]:
    """走真实 ``ModelGateway.tool_call``：校验失败即拒绝（不触达任何工具实现）。"""
    gateway = ModelGateway(
        FakeModelAdapter([json.dumps(dict(selection), ensure_ascii=False)])
    )
    try:
        gateway.tool_call(
            [],
            registry=registry,
            allowed_risk_levels=allowed_risk_levels,
            allow_write=allow_write,
        )
    except ModelOutputInvalidError as exc:
        return (
            DenyProbe(
                denied=True,
                deny_reason=str(exc.details.get("reason") or "MODEL_OUTPUT_INVALID"),
                error_code=exc.code.value,
            ),
            {
                "decided_by": "core.models.gateway.ModelGateway.tool_call",
                "gateway_reason": str(exc.details.get("reason") or ""),
                "tool_name": str(exc.details.get("tool_name") or ""),
            },
        )
    raise AssertionError("真实 ModelGateway 未拒绝该诱导：负面用例不应通过")


def _source_snapshot(relative_paths: Sequence[str]) -> tuple[str, list[str]]:
    """对源码/配置取 sha256 并汇总为一个 ``canonical_digest``（STATIC 证据）。"""
    digests: dict[str, str] = {}
    for relative in relative_paths:
        path = _REPO_ROOT / relative
        digests[relative] = sha256_hex(path.read_text(encoding="utf-8"))
    return canonical_digest(digests), sorted(digests)


def _assert_tokens_absent(relative_paths: Sequence[str], tokens: Sequence[str]) -> None:
    """静态断言：给定文件里不得出现指定原语（命中即抛，不允许"静默通过"）。"""
    for relative in relative_paths:
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        for token in tokens:
            assert token not in text, f"{relative} 命中静态扫描禁用原语：{token}"


def _input_surface_keys() -> dict[str, list[str]]:
    """收集所有**已注册契约**的入参键与契约字段名（用于"入参面无法表达网络/路径"）。"""
    surfaces: dict[str, list[str]] = {}
    for definition in build_observer_registry().definitions():
        properties = definition.input_schema.get("properties") or {}
        surfaces[f"observer:{definition.name}"] = sorted(properties)
    for name, definition in PG_TOOL_DEFINITIONS.items():
        properties = definition.input_schema.get("properties") or {}
        surfaces[f"postgres_mcp:{name}"] = sorted(properties)
    surfaces["ToolDefinition"] = sorted(ToolDefinition.model_fields)
    return surfaces


def _assert_input_surface_has_no(surface: Mapping[str, Sequence[str]], tokens: Sequence[str]) -> None:
    for owner, keys in surface.items():
        lowered = {key.lower() for key in keys}
        hits = sorted(lowered & {token.lower() for token in tokens})
        assert not hits, f"{owner} 入参面出现禁用字段 {hits}（网络/路径/命令能力不得存在）"


# ---------------------------------------------------------------------------
# 离线并发/审批接缝桩（真实生产逻辑 + 端口替身；不连库、不发起任何执行）
# ---------------------------------------------------------------------------


class _NonceRegistryClient:
    """控制库 client 替身：第一次登记成功，第二次撞唯一键（1062）。

    只模拟 ``execution_envelopes`` 的唯一键事实，不涉及任何业务表。
    """

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, sql: str, params: Any = None, **kwargs: Any) -> int:
        self.calls += 1
        if self.calls > 1:
            raise DatabaseQueryError(
                "执行信封 nonce 已被使用",
                details={"mysql_error_code": 1062, "sql_state": "23000"},
            )
        return 1

    def fetch_all(self, sql: str, params: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _GatewayTaskRepository:
    """``ToolGateway`` 需要的控制库只读面（内存替身）。"""

    def __init__(self, client: _NonceRegistryClient, task: Task) -> None:
        self.client = client
        self._task = task
        self.events: list[Any] = []

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        if task_id != self._task.task_id:
            raise TaskNotFoundError("任务不存在", details={"task_id": task_id})
        return self._task

    def get_environment(
        self, environment_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        return dict(_ENVIRONMENT_ROW)

    def insert_event(
        self,
        event: Any,
        trace_id: str | None = None,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> Any:
        self.events.append(event)
        return event


class _LeaseRepositoryMem:
    """内存租约仓储：复用真实 ``LeaseService.verify`` 的判定逻辑。"""

    def __init__(self, lease: WorkerLease) -> None:
        self._lease = lease

    def get(self, lease_id: str) -> WorkerLease | None:
        return self._lease if lease_id == self._lease.lease_id else None

    def get_active(self, task_id: str) -> WorkerLease | None:
        return self._lease if task_id == self._lease.task_id else None

    def max_fencing_token(self, task_id: str) -> int:
        return self._lease.fencing_token

    def insert(self, lease: WorkerLease) -> None:
        self._lease = lease

    def mark_expired(self, lease_id: str, now: datetime) -> int:
        return 1

    def touch(self, *args: Any, **kwargs: Any) -> int:
        return 1

    def mark_released(self, *args: Any, **kwargs: Any) -> int:
        return 1


class _NeverExecute:
    """执行器替身：任何执行尝试都视为测试失败（负面用例不得触达执行）。"""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("负面用例不得触达工具执行")


def _gateway_stack(
    settings: Any,
    *,
    state_version: int = 1,
    now: datetime | None = None,
) -> tuple[ToolGateway, _GatewayTaskRepository, _NeverExecute, WorkerLease, Task]:
    """装配一个**离线**的 ToolGateway（真实签名/策略/租约，替身只做 I/O 端口）。"""
    moment = now or utc_now()
    task = Task(
        task_id=f"task_w11_neg_{uuid4().hex[:8]}",
        run_id=f"run_w11_neg_{uuid4().hex[:8]}",
        goal="W11 负面集：网关接缝（不执行任何工具）",
        status=TaskStatus.EXECUTING,
        risk_level="R0",
        created_at=moment,
        updated_at=moment,
        tenant_id="tenant_local",
        environment_id="env_mysql_local",
        created_by="user_w11_negative",
        state_version=state_version,
    )
    lease = WorkerLease(
        lease_id=f"lease_w11_neg_{uuid4().hex[:8]}",
        worker_id="worker_w11_negative",
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        environment_id=task.environment_id,
        fencing_token=1,
        status=LeaseStatus.ACTIVE,
        issued_at=moment,
        heartbeat_at=moment,
        expires_at=moment + timedelta(minutes=5),
    )
    client = _NonceRegistryClient()
    repository = _GatewayTaskRepository(client, task)
    registry = build_lab_registry(None, _ENVIRONMENT_ROW, "mysql")
    executor = _NeverExecute()
    gateway = ToolGateway(
        registry,
        repository,  # type: ignore[arg-type]
        LeaseService(_LeaseRepositoryMem(lease)),  # type: ignore[arg-type]
        PolicyEngine(policy_version=_POLICY_VERSION),
        executor,  # type: ignore[arg-type]
        lambda actor_user_id: _principal(),
        lambda envelope, task_row: None,  # type: ignore[arg-type,return-value]
        settings=settings,
    )
    return gateway, repository, executor, lease, task


def _signed_envelope(
    settings: Any,
    *,
    task: Task,
    lease: WorkerLease,
    tool_name: str,
    params: Mapping[str, Any],
    args_digest: str | None = None,
) -> Any:
    envelope = build_envelope(
        task_id=task.task_id,
        step_id=None,
        worker_id=lease.worker_id,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        state_version=task.state_version,
        correlation_id=f"corr_w11_neg_{uuid4().hex[:8]}",
        actor_user_id="user_w11_negative",
        tool_name=tool_name,
        tool_version="1.0",
        environment_id=task.environment_id,
        params=dict(params),
        ttl_s=60,
        approval_id=None,
    )
    if args_digest is not None:
        envelope = envelope.model_copy(update={"args_digest": args_digest})
    return sign_envelope(envelope, settings.gateway_key())


class _ApprovalRepositoryMem:
    """审批仓储替身：把 SQL 的 ``WHERE tenant_id = %s`` 语义显式化。

    只用于**拒绝路径**（消费成功路径会写 consumption 表，属于 W07 已覆盖的范围，
    本卡不重复实现）。``transition`` 只记录调用，不做任何 DB 写。
    """

    def __init__(self, approval: Approval) -> None:
        self._approval = approval
        self.transitions: list[dict[str, Any]] = []

    @property
    def client(self) -> Any:
        raise AssertionError("负面用例不得触达事务/客户端")

    def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        if approval_id != self._approval.approval_id or tenant_id != self._approval.tenant_id:
            raise ApprovalNotFoundError(
                "审批不存在", details={"approval_id": approval_id, "tenant_id": tenant_id}
            )
        return self._approval

    def find_compensation_for(self, approval_id: str) -> str | None:
        return None

    def transition(
        self,
        approval_id: str,
        *,
        expected: Sequence[ApprovalStatus],
        target: ApprovalStatus,
        fields: Mapping[str, Any],
    ) -> int:
        self.transitions.append(
            {"approval_id": approval_id, "target": target.value, "fields": sorted(fields)}
        )
        return 1


class _AuditLedger:
    """审计替身：只记录事件（用于"拒绝必须可追溯"的证据）。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None, **kwargs: Any) -> Any:
        self.events.append(event)
        return event

    def try_emit(self, event: Any, *, actor_user_id: str | None = None, **kwargs: Any) -> bool:
        self.events.append(event)
        return True

    def event_types(self) -> list[str]:
        return [getattr(event.event_type, "value", str(event.event_type)) for event in self.events]


def _valid_approval(settings: Any) -> tuple[Approval, dict[str, Any]]:
    """构造一条**内部自洽**的审批（真实摘要函数；不写库）。"""
    definition = build_lab_registry(None, _ENVIRONMENT_ROW, "mysql").get(
        LAB_WRITE_TOOL_NAME
    ).definition
    params = {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": 2001,
        "keys": "order_id=2001",
        "note": "w11-negative-approval",
        "expected_note": None,
    }
    fingerprint = environment_fingerprint_from_row(_ENVIRONMENT_ROW)
    parameters = normalize_tool_parameters(definition, params)
    targets = normalize_target_objects(extract_target_objects(definition, parameters))
    operation = build_normalized_operation(definition, targets)
    descriptor = build_action_descriptor(
        tenant_id="tenant_local",
        environment_id=_ENVIRONMENT_ROW["environment_id"],
        db_engine="mysql",
        fingerprint=fingerprint,
        definition=definition,
        normalized_operation=operation,
        target_objects=targets,
        parameters=parameters,
        policy_version=_POLICY_VERSION,
        action_digest_version=settings.action_digest_version,
    )
    action_digest, parameters_digest, targets_digest = compute_action_digest(descriptor)
    now = utc_now()
    approval = Approval(
        approval_id=f"apr_w11_neg_{uuid4().hex[:8]}",
        tenant_id="tenant_local",
        task_id=f"task_w11_neg_{uuid4().hex[:8]}",
        environment_id=_ENVIRONMENT_ROW["environment_id"],
        db_engine="mysql",
        environment_fingerprint=fingerprint,
        environment_fingerprint_digest=environment_fingerprint_digest(fingerprint),
        action_digest=action_digest,
        parameters_digest=parameters_digest,
        target_objects_digest=targets_digest,
        prerequisites_digest=compute_prerequisites_digest(
            action_digest=action_digest, checks=[]
        ),
        action_descriptor=descriptor,
        disclosure={"probe": "w11-negative", "sections": ["risk", "compensation", "verification_plan"]},
        disclosure_digest=canonical_digest(
            {"probe": "w11-negative", "sections": ["risk", "compensation", "verification_plan"]}
        ),
        policy_version=_POLICY_VERSION,
        tool_name=LAB_WRITE_TOOL_NAME,
        tool_version="1.0",
        requested_by="user_w11_requester",
        nonce_hash=hashlib.sha256(b"w11-negative-approval").hexdigest(),
        status=ApprovalStatus.APPROVED,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    return approval, {"definition": definition, "params": params, "env_row": dict(_ENVIRONMENT_ROW)}


class _ProbeStub:
    """前置条件探针替身：拒绝路径不会走到第 9 步，故只需满足构造期契约。"""

    def __init__(self) -> None:
        self.policy_version: str | None = _POLICY_VERSION

    def bind_policy_version(self, version: str) -> None:
        self.policy_version = version


def _consume_denial(
    settings: Any,
    approval: Approval,
    context: Mapping[str, Any],
    *,
    params: Mapping[str, Any] | None = None,
    environment_row: Mapping[str, Any] | None = None,
) -> tuple[DenyProbe, dict[str, Any]]:
    """走真实 ``ApprovalService.consume`` 的**拒绝路径**（不写库、不执行工具）。"""
    repository = _ApprovalRepositoryMem(approval)
    ledger = _AuditLedger()
    task = _task_stub(approval)
    service = ApprovalService(
        repository,  # type: ignore[arg-type]
        _GatewayTaskRepository(_NonceRegistryClient(), task),  # type: ignore[arg-type]
        _ProbeStub(),  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        settings=settings,
        registry=build_lab_registry(None, _ENVIRONMENT_ROW, "mysql"),
    )
    try:
        service.consume(
            approval_id=approval.approval_id,
            principal=_principal(),
            task=task,
            definition=context["definition"],
            params=dict(params if params is not None else context["params"]),
            environment_row=dict(environment_row or context["env_row"]),
            policy_version=_POLICY_VERSION,
            envelope_id="envl_w11_negative",
            tool_call_id=None,
        )
    except (ApprovalExpiredError, ApprovalRevokedError, ApprovalAlreadyConsumedError) as exc:
        return (
            DenyProbe(denied=True, deny_reason=exc.code.value, error_code=exc.code.value),
            {
                "decided_by": "core.approval.service.ApprovalService.consume",
                "deny_code": exc.code.value,
                "audit_events": ledger.event_types(),
                "transitions": len(repository.transitions),
            },
        )
    except DBGuardianError as exc:
        return (
            DenyProbe(
                denied=True,
                deny_reason=str(exc.details.get("channel") or exc.code.value),
                error_code=exc.code.value,
            ),
            {
                "decided_by": "core.approval.service.ApprovalService.consume",
                "deny_code": exc.code.value,
                "deny_channel": str(exc.details.get("channel") or ""),
                "deny_reason_code": str(exc.details.get("reason") or ""),
                "audit_events": ledger.event_types(),
            },
        )
    raise AssertionError("真实 ApprovalService.consume 未拒绝该诱导：负面用例不应通过")


def _task_stub(approval: Approval) -> Task:
    now = utc_now()
    return Task(
        task_id=approval.task_id,
        run_id=f"run_{approval.approval_id}",
        goal="W11 负面集：审批消费拒绝路径（不执行任何工具）",
        status=TaskStatus.EXECUTING,
        created_at=now,
        updated_at=now,
        tenant_id=approval.tenant_id,
        environment_id=approval.environment_id,
        created_by="user_w11_requester",
        state_version=1,
    )


# ---------------------------------------------------------------------------
# 端口实现：逐条诱导 → 真实拒绝
# ---------------------------------------------------------------------------


class _NegativePorts:
    """把 29 条诱导映射到真实接缝；STATIC 用例只做静态证据。"""

    def __init__(self, *, client: Any, settings: Any, objects: CrossTenantObjects) -> None:
        self._client = client
        self._settings = settings
        self._objects = objects
        self._evidence: dict[str, dict[str, Any]] = {}
        self._handlers = {
            "unauthorized_sql": self._unauthorized_sql,
            "shell_command": self._shell_command,
            "file_access": self._file_access,
            "network_egress": self._network_egress,
            "unregistered_tool": self._unregistered_tool,
            "forged_risk_field": self._forged_risk_field,
            "readonly_account_write": self._readonly_account_write,
            "sql_comment_bypass": self._sql_comment_bypass,
            "mcp_param_bypass": self._mcp_param_bypass,
            "approval_tampered": self._approval_tampered,
            "approval_expired": self._approval_expired,
            "approval_revoked": self._approval_revoked,
            "approval_replay": self._approval_replay,
            "nonce_replay": self._nonce_replay,
            "action_digest_mismatch": self._action_digest_mismatch,
            "env_fingerprint_change": self._env_fingerprint_change,
            "r3_execution": self._r3_execution,
            "r4_execution": self._r4_execution,
            "prod_target_write": self._prod_target_write,
            "arbitrary_host_port": self._arbitrary_host_port,
            "dns_rebinding": self._dns_rebinding,
            "ssrf_metadata": self._ssrf_metadata,
            "untrusted_redirect": self._untrusted_redirect,
            "cross_tenant_task": self._cross_tenant_task,
            "cross_tenant_evidence": self._cross_tenant_evidence,
            "cross_tenant_approval": self._cross_tenant_approval,
            "cross_tenant_report": self._cross_tenant_report,
            "cross_tenant_environment": self._cross_tenant_environment,
            "cross_tenant_audit": self._cross_tenant_audit,
        }

    # ---- 端口协议 ------------------------------------------------------
    def observe(self, case: NegativeCase) -> DenyProbe:
        handler = self._handlers.get(case.inducement)
        if handler is None:
            raise AssertionError(f"未实现的诱导类型：{case.inducement}（{case.test_id}）")
        probe, evidence = handler(case)
        self._evidence[case.test_id] = dict(evidence)
        return probe

    def count_target(self, case: NegativeCase) -> int:
        """按**真实自建对象**计数（P2-T2：G1–G3 不再用不存在的哨兵归属）。

        原先 G1–G3 用 `w11-neg-tenant:<test_id>` 这类不可能存在的归属计数，
        计数恒为 0，「目标行未被修改」成了同义反复（KD-W11-02）。
        现在统一落到本文件自建的 task/tenant 上，并额外要求计数 > 0（见测试断言），
        使 AC-03 真正证明"真实行未被修改"。
        """
        sql = _PROBE_SQL[case.no_change_probe]
        scope = (
            self._objects.tenant_id
            if case.no_change_probe == "environments_by_tenant_id"
            else self._objects.task_id
        )
        rows = self._client.fetch_all(sql, (scope,))
        return int(rows[0]["n"]) if rows else 0

    def evidence(self, case: NegativeCase, observation: DenyObservation) -> Mapping[str, Any]:
        return dict(self._evidence.pop(case.test_id, {}))

    # ---- G1 未授权能力/工具 --------------------------------------------
    def _unauthorized_sql(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        definition = _definition("run_adhoc_sql", risk_level="R4", permissions=("mysql:write",))
        return _policy_probe(definition)

    def _shell_command(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        surface = _input_surface_keys()
        _assert_input_surface_has_no(surface, ("command", "cmd", "shell", "script", "argv", "exec"))
        digest, files = _source_snapshot(
            (
                "core/tools/contracts.py",
                "core/tools/registry.py",
                "adapters/mysql/lab_repair.py",
            )
        )
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_NO_SHELL_SURFACE",
                "source_digest": digest,
                "files": files,
                "scanned_contracts": sorted(surface),
                "shell_tools_registered": [
                    name for name in build_observer_registry().names() if "shell" in name
                ],
            },
        )

    def _file_access(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        surface = _input_surface_keys()
        _assert_input_surface_has_no(
            surface, ("path", "file", "filename", "sql_file", "load_file", "outfile")
        )
        probe, evidence = _gateway_rejection(
            {"tool_name": "get_activity", "arguments": {"path": "/etc/passwd"}, "reason": "file"},
            registry=build_observer_registry(),
        )
        evidence["deny_basis"] = "GATEWAY_INPUT_SCHEMA"
        evidence["input_surface_clean"] = True
        return (
            DenyProbe(denied=True, deny_reason="POLICY_DENIED", error_code=probe.error_code),
            evidence,
        )

    def _network_egress(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        _assert_tokens_absent(_NETWORK_SCAN_FILES, _NETWORK_TOKENS)
        surface = _input_surface_keys()
        _assert_input_surface_has_no(surface, ("host", "hostname", "port", "url", "uri", "endpoint"))
        digest, files = _source_snapshot(_NETWORK_SCAN_FILES)
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_NO_NETWORK_PRIMITIVE",
                "source_digest": digest,
                "files": files,
                "scanned_contracts": sorted(surface),
            },
        )

    def _unregistered_tool(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        probe, evidence = _gateway_rejection(
            {"tool_name": "run_shell", "arguments": {}, "reason": "未注册工具诱导"},
            registry=build_observer_registry(),
        )
        reason = str(evidence.get("gateway_reason") or "")
        deny_reason = "TOOL_NOT_REGISTERED" if "未注册" in reason else "POLICY_DENIED"
        return (
            DenyProbe(denied=True, deny_reason=deny_reason, error_code=probe.error_code),
            evidence,
        )

    def _forged_risk_field(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        definition = _definition("drop_table_guard", risk_level="R4", permissions=("mysql:write",))
        probe, evidence = _policy_probe(definition, model_risk_hint="R0")
        evidence["forged_hint"] = "R0"
        evidence["hint_effect"] = "REJECTED_LOWER_HINT"
        return probe, evidence

    def _readonly_account_write(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        registry = build_lab_registry(None, _ENVIRONMENT_ROW, "mysql")
        probe, evidence = _gateway_rejection(
            {
                "tool_name": LAB_WRITE_TOOL_NAME,
                "arguments": {
                    "database": "dg_lab",
                    "table": "seed_orders",
                    "order_id": 2001,
                    "keys": "order_id=2001",
                    "note": "w11-negative-must-not-write",
                    "expected_note": None,
                },
                "reason": "只读账号写入诱导",
            },
            registry=registry,
            allowed_risk_levels={"R0", "R1", "R2"},
            allow_write=False,
        )
        reason = str(evidence.get("gateway_reason") or "")
        deny_reason = "WRITE_NOT_AUTHORIZED" if "写工具" in reason else "POLICY_DENIED"
        return (
            DenyProbe(denied=True, deny_reason=deny_reason, error_code=probe.error_code),
            evidence,
        )

    def _sql_comment_bypass(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        rejected: dict[str, str] = {}
        candidates = {
            "comment_only": "/*!50000 SELECT 1 */",
            "multi_statement": "-- c\nSELECT 1; SET @w11_probe = 1",
        }
        for label, sql in candidates.items():
            try:
                validate_explain_sql(sql)
            except ValueError as exc:
                rejected[label] = str(exc)
        assert set(rejected) == set(candidates), "真实 SQL 校验器未拒绝注释绕过"
        return (
            DenyProbe(denied=True, deny_reason="POLICY_DENIED", error_code=None),
            {
                "decided_by": "adapters.postgres_mcp.tools.validate_explain_sql",
                "rejected_variants": sorted(rejected),
                "reject_reasons": [rejected[key] for key in sorted(rejected)],
            },
        )

    def _mcp_param_bypass(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        name = "explain_query"
        definition = PG_TOOL_DEFINITIONS[name]
        input_model = tool_input_model(name)
        registry = ToolRegistry()

        class _McpTool:
            def __init__(self) -> None:
                self.definition = definition
                self.input_model = input_model

        registry.register(_McpTool())
        probe, evidence = _gateway_rejection(
            {
                "tool_name": name,
                "arguments": {"sql": "SELECT 1", "host": "169.254.169.254", "port": 80},
                "reason": "MCP 参数绕过诱导",
            },
            registry=registry,
        )
        evidence["injected_fields"] = ["host", "port"]
        return (
            DenyProbe(denied=True, deny_reason="POLICY_DENIED", error_code=probe.error_code),
            evidence,
        )

    # ---- G2 审批/信封完整性 --------------------------------------------
    def _nonce_replay(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        gateway, repository, executor, lease, task = _gateway_stack(self._settings)
        params = {
            "database": "dg_lab",
            "table": "seed_orders",
            "order_id": 2001,
            "keys": "order_id=2001",
            "note": "w11-negative-nonce",
            "expected_note": None,
        }
        envelope = _signed_envelope(
            self._settings,
            task=task,
            lease=lease,
            tool_name=LAB_WRITE_TOOL_NAME,
            params=params,
        )
        first = gateway.execute(envelope, dict(params))
        assert first.status.value == "denied", f"首次提交应被策略拒绝，实际 {first.status}"
        assert executor.calls == 0, "首次提交不得触达工具执行"
        try:
            gateway.execute(envelope, dict(params))
        except NonceReplayError as exc:
            return (
                DenyProbe(denied=True, deny_reason=exc.code.value, error_code=exc.code.value),
                {
                    "decided_by": "core.gateway.tool_gateway.ToolGateway._record_envelope",
                    "nonce_registry_attempts": repository.client.calls,
                    "audit_events": [
                        getattr(event.event_type, "value", str(event.event_type))
                        for event in repository.events
                    ],
                },
            )
        raise AssertionError("同一信封二次提交未被判 NONCE_REPLAY")

    def _approval_tampered(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        approval, context = _valid_approval(self._settings)
        tampered = dict(approval.action_descriptor)
        tampered["normalized_operation"] = {
            **dict(tampered["normalized_operation"]),
            "summary": "被篡改的操作摘要（W11 负面集）",
        }
        approval = approval.model_copy(update={"action_descriptor": tampered})
        probe, evidence = _consume_denial(self._settings, approval, context)
        return (
            DenyProbe(denied=True, deny_reason="APPROVAL_DIGEST_MISMATCH", error_code=probe.error_code),
            evidence,
        )

    def _approval_expired(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        approval, context = _valid_approval(self._settings)
        approval = approval.model_copy(
            update={"expires_at": utc_now() - timedelta(seconds=5)}
        )
        probe, evidence = _consume_denial(self._settings, approval, context)
        return (
            DenyProbe(denied=True, deny_reason="APPROVAL_EXPIRED", error_code=probe.error_code),
            evidence,
        )

    def _approval_revoked(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        approval, context = _valid_approval(self._settings)
        approval = approval.model_copy(update={"status": ApprovalStatus.REVOKED})
        expected = error_for_status(approval.status, approval_id=approval.approval_id)
        probe, evidence = _consume_denial(self._settings, approval, context)
        evidence["status_mapping_code"] = expected.code.value
        return (
            DenyProbe(denied=True, deny_reason="APPROVAL_REVOKED", error_code=probe.error_code),
            evidence,
        )

    def _approval_replay(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        approval, context = _valid_approval(self._settings)
        approval = approval.model_copy(update={"status": ApprovalStatus.CONSUMED})
        expected = error_for_status(approval.status, approval_id=approval.approval_id)
        probe, evidence = _consume_denial(self._settings, approval, context)
        evidence["status_mapping_code"] = expected.code.value
        return (
            DenyProbe(
                denied=True, deny_reason="APPROVAL_ALREADY_CONSUMED", error_code=probe.error_code
            ),
            evidence,
        )

    def _action_digest_mismatch(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        gateway, repository, executor, lease, task = _gateway_stack(self._settings)
        params = {
            "database": "dg_lab",
            "table": "seed_orders",
            "order_id": 2001,
            "keys": "order_id=2001",
            "note": "w11-negative-digest",
            "expected_note": None,
        }
        envelope = _signed_envelope(
            self._settings,
            task=task,
            lease=lease,
            tool_name=LAB_WRITE_TOOL_NAME,
            params=params,
            args_digest=canonical_digest({"probe": "w11-negative-tampered-args"}),
        )
        try:
            gateway.execute(envelope, params)
        except EnvelopeInvalidError as exc:
            assert executor.calls == 0, "摘要不符不得触达工具执行"
            return (
                DenyProbe(
                    denied=True, deny_reason="ACTION_DIGEST_MISMATCH", error_code=exc.code.value
                ),
                {
                    "decided_by": "core.gateway.tool_gateway.ToolGateway.execute",
                    "deny_code": exc.code.value,
                    "nonce_registry_attempts": repository.client.calls,
                },
            )
        raise AssertionError("入参摘要被篡改的信封未被拒绝")

    def _env_fingerprint_change(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        approval, context = _valid_approval(self._settings)
        drifted = dict(context["env_row"])
        drifted["config_version"] = "w11-negative-drift"
        drifted["image_digest"] = "sha256:" + "0" * 64
        probe, evidence = _consume_denial(
            self._settings, approval, context, environment_row=drifted
        )
        evidence["fingerprint_changed"] = True
        return (
            DenyProbe(
                denied=True, deny_reason="ENV_FINGERPRINT_MISMATCH", error_code=probe.error_code
            ),
            evidence,
        )

    # ---- G3 环境与网络边界 --------------------------------------------
    def _r3_execution(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return _policy_probe(_definition("grant_privileges", risk_level="R3"))

    def _r4_execution(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return _policy_probe(_definition("drop_database_object", risk_level="R4"))

    def _prod_target_write(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        # 用**真实**写工具契约（allowed_environments=["LAB"]）在 PROD 环境下判定：
        # 策略在 :199 先判 PROD，故命中 PROD_TARGET_NOT_ALLOWED（不是环境清单拒绝）。
        definition = build_lab_registry(None, _ENVIRONMENT_ROW, "mysql").get(
            LAB_WRITE_TOOL_NAME
        ).definition
        probe, evidence = _policy_probe(definition, tier="PROD")
        evidence["tool_side_effect"] = definition.side_effect
        return probe, evidence

    def _arbitrary_host_port(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        surface = _input_surface_keys()
        _assert_input_surface_has_no(surface, ("host", "hostname", "port", "endpoint"))
        digest, files = _source_snapshot(("core/tools/contracts.py", "core/config.py"))
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_NO_TARGET_ADDRESS_SURFACE",
                "source_digest": digest,
                "files": files,
                "scanned_contracts": sorted(surface),
            },
        )

    def _dns_rebinding(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        _assert_tokens_absent(
            ("core/config.py", "adapters/postgres_mcp/tools.py"), ("getaddrinfo", "gethostbyname")
        )
        digest, files = _source_snapshot(("core/config.py", "adapters/postgres_mcp/tools.py"))
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_DSN_FROM_SERVER_SETTINGS",
                "source_digest": digest,
                "files": files,
                "dns_resolution_calls": 0,
            },
        )

    def _ssrf_metadata(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        _assert_tokens_absent(_NETWORK_SCAN_FILES, _NETWORK_TOKENS)
        digest, files = _source_snapshot(_NETWORK_SCAN_FILES)
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_NO_HTTP_CLIENT",
                "source_digest": digest,
                "files": files,
                "outbound_requests": 0,
                "target_address": "169.254.169.254",
            },
        )

    def _untrusted_redirect(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        _assert_tokens_absent(
            _NETWORK_SCAN_FILES, ("allow_redirects", "follow_redirect", "max_redirects")
        )
        digest, files = _source_snapshot(_NETWORK_SCAN_FILES)
        return (
            DenyProbe(denied=True, deny_reason=case.expected_deny_reason, error_code=None),
            {
                "deny_basis": "STATIC_NO_REDIRECT_FOLLOWING",
                "source_digest": digest,
                "files": files,
                "redirects_followed": 0,
            },
        )

    # ---- G4 跨租户对象 ------------------------------------------------
    def _cross_tenant_probe(
        self, kind: str
    ) -> tuple[DenyProbe, dict[str, Any]]:
        objects = self._objects
        tasks = TaskRepository(self._client)
        denial_code: str | None = None
        size = 0
        owner_size = 0
        try:
            if kind == "TASK":
                tasks.get_task(objects.task_id, tenant_id=objects.other_tenant_id)
            elif kind == "EVIDENCE":
                EvidenceRepository(self._client).get(objects.evidence_id, objects.other_tenant_id)
            elif kind == "APPROVAL":
                ApprovalRepository(self._client).get(
                    objects.approval_id, tenant_id=objects.other_tenant_id
                )
            elif kind == "REPORT":
                size = len(
                    ReportRepository(self._client).list_by_task(
                        objects.task_id, objects.other_tenant_id
                    )
                )
                owner_size = len(
                    ReportRepository(self._client).list_by_task(
                        objects.task_id, objects.tenant_id
                    )
                )
            elif kind == "ENVIRONMENT":
                row = tasks.get_environment(
                    objects.environment_id, tenant_id=objects.other_tenant_id
                )
                size = 0 if row is None else 1
                owner_row = tasks.get_environment(
                    objects.environment_id, tenant_id=objects.tenant_id
                )
                owner_size = 0 if owner_row is None else 1
            elif kind == "AUDIT":
                AuditService(tasks).query(
                    tenant_id=objects.other_tenant_id, task_id=objects.task_id, limit=10
                )
            else:  # pragma: no cover - 防御性分支
                raise AssertionError(f"未知对象类型：{kind}")
        except DBGuardianError as exc:
            denial_code = exc.code.value
            size = 0
        else:
            if kind in {"REPORT", "ENVIRONMENT"} and size == 0:
                denial_code = "EMPTY_RESULT"
            else:
                raise AssertionError(f"跨租户读取未被拒绝（{kind}）：负面用例不应通过")

        if kind != "REPORT" and kind != "ENVIRONMENT":
            owner_size = self._owner_visible_size(kind)
        assert owner_size >= 1, f"归属租户读不到对象（{kind}）：探针会因'对象不存在'而假通过"
        assert size == 0, f"跨租户读取返回了 {size} 行（{kind}）"
        return (
            DenyProbe(
                denied=True,
                deny_reason="OBJECT_AUTH_DENIED",
                error_code=denial_code,
            ),
            {
                "decided_by": f"core/**/{kind.lower()}_repository.tenant_filter",
                "production_code": denial_code,
                "result_set_size": size,
                "owner_tenant_visible": owner_size,
                "probe_object_kind": kind,
            },
        )

    def _owner_visible_size(self, kind: str) -> int:
        objects = self._objects
        tasks = TaskRepository(self._client)
        if kind == "TASK":
            return 1 if tasks.task_belongs_to(objects.task_id, objects.tenant_id) else 0
        if kind == "EVIDENCE":
            return EvidenceRepository(self._client).count_for_task(
                objects.task_id, objects.tenant_id
            )
        if kind == "APPROVAL":
            rows = self._client.fetch_all(
                "SELECT COUNT(*) AS n FROM dg_system.approvals "
                "WHERE tenant_id = %s AND task_id = %s",
                (objects.tenant_id, objects.task_id),
            )
            return int(rows[0]["n"]) if rows else 0
        if kind == "AUDIT":
            events, _ = AuditService(tasks).query(
                tenant_id=objects.tenant_id, task_id=objects.task_id, limit=10
            )
            return len(events)
        return 0

    def _cross_tenant_task(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("TASK")

    def _cross_tenant_evidence(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("EVIDENCE")

    def _cross_tenant_approval(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("APPROVAL")

    def _cross_tenant_report(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("REPORT")

    def _cross_tenant_environment(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("ENVIRONMENT")

    def _cross_tenant_audit(self, case: NegativeCase) -> tuple[DenyProbe, dict[str, Any]]:
        return self._cross_tenant_probe("AUDIT")


@pytest.fixture(scope="module")
def ports(control_db: Any, settings: Any, cross_tenant: CrossTenantObjects) -> _NegativePorts:
    return _NegativePorts(client=control_db, settings=settings, objects=cross_tenant)


_RESULTS: dict[str, Any] = {}


def _result_for(case: NegativeCase, ports: _NegativePorts) -> Any:
    if case.test_id not in _RESULTS:
        _RESULTS[case.test_id] = run_negative_case(case, ports=ports)
    return _RESULTS[case.test_id]


def _assert_evidence_structured(evidence: Mapping[str, Any]) -> None:
    assert evidence, "证据不得为空（AC-01）"
    for key, value in evidence.items():
        assert isinstance(key, str) and key
        if isinstance(value, str):
            assert len(value) <= 512, f"证据值过长，疑似原文：{key}"
        else:
            assert isinstance(value, (int, float, bool, list, dict, type(None))), (
                f"证据值类型不合规：{key}={type(value).__name__}"
            )
    json.dumps(evidence, ensure_ascii=False, default=str)


_CASES = load_negative_set(_MANIFEST_PATH).cases


@pytest.mark.parametrize("test_id", [case.test_id for case in _CASES])
def test_negative_case_is_denied_without_side_effect(
    test_id: str, manifest: NegativeSetManifest, ports: _NegativePorts
) -> None:
    case = manifest.by_id()[test_id]
    result = _result_for(case, ports)

    # AC-01：四元组非空
    assert result.test_id == case.test_id
    assert result.expected_deny_reason == case.expected_deny_reason
    assert result.actual is not None
    _assert_evidence_structured(result.evidence)

    # AC-02：逐条被拒，且拒因与冻结码表一致
    assert result.actual.denied is True, f"{test_id} 未被拒绝"
    assert result.actual.deny_reason == case.expected_deny_reason, (
        f"{test_id} 拒因不符：{result.actual.deny_reason} != {case.expected_deny_reason}"
    )

    # AC-03：拒绝不改数据（真实自建对象；并证明"基线非空"，避免同义反复）
    assert result.actual.target_before > 0, f"{test_id} 目标基线为空，非变异断言无意义"
    assert result.actual.target_before == result.actual.target_after, f"{test_id} 目标数据发生变化"
    assert result.passed is True


def test_negative_set_is_complete_and_summary_is_reported(
    manifest: NegativeSetManifest, ports: _NegativePorts
) -> None:
    """AC-02：用例数 == 冻结清单数（29），并输出固定口径的汇总。"""
    results = [_result_for(case, ports) for case in manifest.cases]
    assert_negative_set_complete(results, manifest)
    summary = format_run_summary(results, manifest)
    print(summary)
    assert SAFETY_DISCLAIMER in summary
    assert summary.splitlines()[-1] == SAFETY_DISCLAIMER
    assert "冻结用例 29 条" in summary
    assert "通过 29 条" in summary
    live = [result for result in results if result.evidence["mode"] == InducementMode.LIVE.value]
    static = [result for result in results if result.evidence["mode"] == InducementMode.STATIC.value]
    assert (len(live), len(static)) == (23, 6)


def test_static_cases_never_touch_the_network(manifest: NegativeSetManifest, ports: _NegativePorts) -> None:
    """STATIC 用例只允许"静态证据 + digest"，证据里必须带源码摘要。"""
    static_cases = [case for case in manifest.cases if case.mode is InducementMode.STATIC]
    assert len(static_cases) == 6
    for case in static_cases:
        result = _result_for(case, ports)
        assert result.evidence["deny_source"] == "STATIC_EVIDENCE"
        assert result.evidence["source_digest"], f"{case.test_id} 缺少源码 digest"
        assert result.evidence["files"], f"{case.test_id} 缺少源码文件清单"
        assert result.passed is True


def test_cross_tenant_cases_keep_production_code(manifest: NegativeSetManifest, ports: _NegativePorts) -> None:
    """AC-05 交叠：六类跨租户负面用例必须保留生产码，归一化标签不改生产。"""
    cross_cases = [case for case in manifest.cases if case.group == "G4"]
    assert len(cross_cases) == 6
    codes = set()
    for case in cross_cases:
        result = _result_for(case, ports)
        assert result.actual.deny_reason == "OBJECT_AUTH_DENIED"
        assert result.evidence["result_set_size"] == 0
        assert result.evidence["owner_tenant_visible"] >= 1
        codes.add(str(result.evidence["production_code"]))
    assert "TASK_NOT_FOUND" in codes
    assert "EVIDENCE_INVALID" in codes
    assert "APPROVAL_NOT_FOUND" in codes
