"""W11 T03 集成测试：六类故障注入与恢复核对（AC-08…AC-11）。

口径（``docs/w11/02-design.md`` §7.1–§7.3；裁定 W11-Q5 / D-03）：

- **严格六类**：``EVIDENCE_STORE`` / ``AUDIT_WRITER`` / ``DB_DISCONNECT`` /
  ``MODEL_SERVICE`` / ``WORKER_CRASH`` / ``CONTROL_DB_TRANSIENT``，六条独立用例；
  网络分区、磁盘满**不纳入**（第 7 类会被 ``FaultTypeError`` 拒绝）。
- **注入只用既有接缝**（``docs/w08/02-design.md:517``「不为测试加生产分支」）：
  ``monkeypatch`` 仓储 ``insert``/``insert_event``、客户端 ``execute``/``fetch_all``、
  真实子进程 ``Popen.kill()``；W04 ``FaultSession`` 只经 ``FaultSessionAdapter``
  **函数内惰性 import** 使用。
- **AC-08（D-03）**：Audit Writer 失败 + R2 请求必须走
  ``core/audit/service.py:209 emit_required``（``:240`` 抛 ``AuditUnavailableError``），
  断言 ``AUDIT_WRITE_FAILED`` 告警、``tool_calls_delta == 0``、目标行不变；
  **不接** ``:248 try_emit``。
- **AC-09**：R0 读取走 ``:248 try_emit`` 降级（返回 ``False``、不抛错），
  ``alert_emitted is True``（账本不可写时告警退化为结构化日志，ADR-0006:129）。
- **AC-10/AC-11**：``seq == list(range(n))``、``state_version`` 单调、
  Evidence 引用可解析、``audit_chain_ok`` 由 ``AuditService.verify`` 给出；
  恢复期 ``tool_calls_delta == 0`` 且无 ``TOOL_CALL_RETRIED``（先核对后行动，绝不重放）。

**替身边界（如实登记，续 T02 透明度要求）**

- **真实**：控制库（``dg_ctl``，只 INSERT 追加）、``TaskRepository.insert_event`` 的哈希链、
  ``AuditService.emit_required`` / ``try_emit`` / ``verify``、``EvidenceService.create``、
  真实观察器工具的错误映射、``ModelGateway`` 归一化、``Reconciler`` 判定逻辑、
  真实子进程 ``Popen.kill()``。
- **替身**：``ToolExecutor`` 的 ``tool_calls`` 存储与审计账本在 AC-08 用例里用内存替身
  （因为真实仓储被注入成"不可写"正是被测故障本身，无法同时充当落点）；
  ``Reconciler`` 的 ``step_records``/``task_repository`` 用内存替身以**避免 UPDATE**
  （纪律：只 INSERT，不 UPDATE 历史行）。
- **不做**：不 DELETE/UPDATE/TRUNCATE/DROP，不清理，不碰 ``DG_ADMIN_DSN``、不连 PG 5432、
  不新增第 7 类、不改 ``benchmarks/faults/injector.py``。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pytest

from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from core.approval.models import issue_grant
from core.assurance.faults import (
    FAULT_BOUNDARIES,
    FAULT_BOUNDARY_REASONS,
    FAULT_CASE_COUNT,
    FAULT_CLASSES,
    FaultCaseManifest,
    FaultClass,
    FaultInjectionResult,
    FaultProbeOutcome,
    FaultSessionAdapter,
    FaultTypeError,
    RecoveryBoundary,
    RecoveryCheck,
    RecoveryIntegrityError,
    assert_fault_manifest_frozen,
    assert_recovery_integrity,
    load_fault_cases,
    summarize_fault_results,
)
from core.audit.events import AuditEventType, make_event
from core.audit.service import ALERT_AUDIT_WRITE_FAILED, AuditService
from core.errors import (
    AuditUnavailableError,
    DBGuardianError,
    DatabaseQueryError,
    DatabaseUnavailableError,
    ErrorCode,
    ModelCallFailedError,
    ModelUnavailableError,
)
from core.evidence.models import CollectionStatus, EvidenceCreate, EvidenceQuality
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.execution.reconciler import CONCLUSION_NEEDS_RECONCILIATION, Reconciler
from core.execution.step_records import StepPrepared
from core.models.adapters.fake import FakeModelAdapter
from core.models.degradation import DisabledModelGateway
from core.models.gateway import ModelGateway
from core.security.canonical import canonical_digest
from core.tasks.models import Task, TaskStatus, ToolCallRecord, utc_now
from core.tasks.repository import TaskRepository
from core.tools.contracts import (
    TargetObjectSpec,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolStatus,
)
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from database.observers import build_observer_registry
from tests.helpers.definitions import write_declarations

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAULT_CASES_PATH = _REPO_ROOT / "benchmarks" / "w11" / "fault_cases.json"
_POLICY_VERSION = "w05-v1"
_TENANT_PREFIX = "w11_fault"


# ---------------------------------------------------------------------------
# 现场对象（只 INSERT 追加）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Fixture:
    tenant_id: str
    environment_id: str
    task_id: str
    run_id: str


def _provision(client: Any) -> _Fixture:
    suffix = uuid4().hex[:8]
    tenant_id = f"{_TENANT_PREFIX}_{suffix}"
    environment_id = f"env_{_TENANT_PREFIX}_{suffix}"
    task_id = f"task_{_TENANT_PREFIX}_{suffix}"
    run_id = f"run_{_TENANT_PREFIX}_{suffix}"
    now = utc_now()
    client.execute(
        "INSERT INTO dg_system.tenants (tenant_id, name, status, created_at) "
        "VALUES (%s, %s, 'ACTIVE', UTC_TIMESTAMP(6))",
        (tenant_id, f"W11 故障注入 {suffix}"),
    )
    client.execute(
        "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
        "db_version, network_id, image_digest, config_version, status, created_at) "
        "VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'w11-fault', 'LAB', "
        "UTC_TIMESTAMP(6))",
        (environment_id, tenant_id),
    )
    tasks = TaskRepository(client)
    tasks.insert_task(
        Task(
            task_id=task_id,
            run_id=run_id,
            goal="W11 故障注入与恢复核对（不执行任何写动作）",
            status=TaskStatus.EXECUTING,
            risk_level="R0",
            created_at=now,
            updated_at=now,
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by="user_w11_fault",
            state_version=1,
        )
    )
    tasks.insert_event(
        make_event(
            AuditEventType.TASK_CREATED,
            task_id=task_id,
            run_id=run_id,
            payload={"probe": "w11-fault-provision", "tenant_id": tenant_id},
        ),
        tenant_id=tenant_id,
        actor_user_id="user_w11_fault",
    )
    return _Fixture(
        tenant_id=tenant_id,
        environment_id=environment_id,
        task_id=task_id,
        run_id=run_id,
    )


class _Chain:
    """真实控制库上的事件链事实读取器（只读 + 追加）。"""

    def __init__(self, client: Any, fixture: _Fixture) -> None:
        self.client = client
        self.fixture = fixture
        self.tasks = TaskRepository(client)

    def append(self, event_type: AuditEventType, payload: Mapping[str, Any] | None = None) -> Any:
        return self.tasks.insert_event(
            make_event(
                event_type,
                task_id=self.fixture.task_id,
                run_id=self.fixture.run_id,
                payload=dict(payload or {}),
            ),
            tenant_id=self.fixture.tenant_id,
            actor_user_id="user_w11_fault",
        )

    def events(self) -> list[Any]:
        return self.tasks.list_events(self.fixture.task_id, limit=200)

    def seqs(self) -> list[int]:
        return [event.seq for event in self.events()]

    def seq_continuous(self) -> bool:
        seqs = self.seqs()
        return seqs == list(range(len(seqs)))

    def audit_chain_ok(self) -> bool:
        try:
            verification = AuditService(self.tasks).verify(
                tenant_id=self.fixture.tenant_id, task_id=self.fixture.task_id
            )
        except DBGuardianError:
            return False
        return bool(verification.ok)

    def event_types(self) -> list[str]:
        return [str(event.event_type) for event in self.events()]

    def state_version(self) -> int:
        return self.tasks.get_task(
            self.fixture.task_id, tenant_id=self.fixture.tenant_id
        ).state_version

    def executed_tool_calls(self) -> int:
        rows = self.client.fetch_all(
            "SELECT COUNT(*) AS n FROM dg_system.tool_calls "
            "WHERE task_id = %s AND status <> 'denied'",
            (self.fixture.task_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def tool_calls(self) -> int:
        rows = self.client.fetch_all(
            "SELECT COUNT(*) AS n FROM dg_system.tool_calls WHERE task_id = %s",
            (self.fixture.task_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def evidence_count(self) -> int:
        rows = self.client.fetch_all(
            "SELECT COUNT(*) AS n FROM dg_system.evidence WHERE task_id = %s",
            (self.fixture.task_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    def evidence_refs_resolvable(self, evidence_ids: Sequence[str]) -> bool:
        if not evidence_ids:
            return True
        missing = EvidenceRepository(self.client).validate_task_references(
            self.fixture.task_id, self.fixture.tenant_id, list(evidence_ids)
        )
        return not missing


def _recovery(
    chain: _Chain,
    *,
    state_before: int,
    tool_calls_before: int,
    evidence_ids: Sequence[str] = (),
) -> RecoveryCheck:
    """用**真实**事实构造六项恢复检查（audit_chain_ok 来自 AuditService.verify）。"""
    return RecoveryCheck(
        seq_continuous=chain.seq_continuous(),
        audit_chain_ok=chain.audit_chain_ok(),
        evidence_refs_resolvable=chain.evidence_refs_resolvable(evidence_ids),
        state_version_monotonic=chain.state_version() >= state_before,
        tool_calls_delta=chain.tool_calls() - tool_calls_before,
        replay_occurred="TOOL_CALL_RETRIED" in chain.event_types(),
    )


def _alerts(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        str(getattr(record, "alert", "")) for record in caplog.records if getattr(record, "alert", None)
    ]


# ---------------------------------------------------------------------------
# AC-08 用的执行器接缝（R2 写契约桩 + 内存落点；生产注册表不变）
# ---------------------------------------------------------------------------


class _StubR2WriteTool:
    def __init__(self) -> None:
        self.calls = 0
        self.definition = ToolDefinition(
            name="w11_fault_stub_write",
            description="W11 故障用例 R2 桩（不连库、不写任何目标）",
            version="1.0",
            risk_level="R2",
            side_effect="write",
            permissions=["mysql:write"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            db_engines=["mysql"],
            allowed_environments=["LAB"],
            operation_class="repair",
            target_objects=[
                TargetObjectSpec(
                    object_type="row",
                    database_field="database",
                    name_field="table",
                )
            ],
            **write_declarations(),
        )

    def run(self, context: ToolContext, params: Mapping[str, Any]) -> ToolResult:  # pragma: no cover
        self.calls += 1
        raise AssertionError("R2 桩不得被真正执行")


class _ToolCallMemory:
    """``tool_calls`` 落点替身（故障期真实仓储不可写，故此处用内存落点）。"""

    def __init__(self) -> None:
        self.records: list[ToolCallRecord] = []
        self.executed = 0

    def find_tool_call_by_idempotency(self, task_id: str, key: str) -> ToolCallRecord | None:
        for record in self.records:
            if record.task_id == task_id and record.idempotency_key == key:
                return record
        return None

    def insert_tool_call(self, record: ToolCallRecord) -> None:
        self.records.append(record)
        if record.status != "denied":
            self.executed += 1

    def update_tool_call(self, tool_call_id: str, **kwargs: Any) -> None:
        return None

    def insert_event(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        return event


class _FailingLedger:
    """审计仓储替身：``insert_event`` 一律抛 ``DatabaseQueryError``（账本不可写）。"""

    def __init__(self) -> None:
        self.attempts = 0

    def insert_event(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        raise DatabaseQueryError("审计账本不可写", details={"reason": ALERT_AUDIT_WRITE_FAILED})

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        raise DatabaseQueryError("任务不可读", details={"task_id": task_id})


class _StepRecordMemory:
    def __init__(self) -> None:
        self.prepared: list[Any] = []

    def insert_prepared(self, prepared: Any) -> Any:
        self.prepared.append(prepared)
        return prepared


class _WriteContextClient:
    database_id = "dg_lab"
    read_only = False
    engine = "mysql"

    def identity(self) -> Any:
        raise AssertionError("本用例不需要真实身份")


class _ReadContextClient:
    database_id = "dg_lab"
    read_only = True
    engine = "mysql"

    def __init__(self, *, fail: bool = False, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self._fail = fail
        self._rows = [dict(row) for row in rows]

    def identity(self) -> Any:
        raise AssertionError("本用例不需要真实身份")

    def fetch_all(self, sql: str, params: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        if self._fail:
            raise DatabaseUnavailableError(
                "目标库连接中断", details={"reason": "DB_DISCONNECT"}
            )
        return list(self._rows)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@pytest.fixture(scope="module")
def fault_cases() -> FaultCaseManifest:
    return load_fault_cases(_FAULT_CASES_PATH)


# ---------------------------------------------------------------------------
# 1) EVIDENCE_STORE：fail closed + 自动恢复
# ---------------------------------------------------------------------------


def test_evidence_store_failure_is_fail_closed_and_recoverable(
    control_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    repository = EvidenceRepository(control_db)
    service = EvidenceService(repository, TaskRepository(control_db))
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    evidence_before = chain.evidence_count()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        def boom(evidence: Any) -> Any:
            raise DatabaseUnavailableError(
                "证据存储不可用", details={"reason": "EVIDENCE_STORE_DOWN"}
            )

        raised_code: str | None = None
        with monkeypatch.context() as patch:
            patch.setattr(repository, "insert", boom)
            try:
                service.create(_evidence_request(fixture))
            except DBGuardianError as exc:
                raised_code = exc.code.value
        observed["raised_code"] = raised_code
        observed["rows_after_failure"] = chain.evidence_count()

        # 恢复：故障消失后重试成功，引用可解析（自动恢复，无目标库副作用）
        created = service.create(_evidence_request(fixture))
        observed["evidence_id"] = created.evidence_id
        chain.append(AuditEventType.STEP_OBSERVED, {"probe": "evidence-store-recovered"})
        return FaultProbeOutcome(
            injected=raised_code is not None,
            fail_closed=raised_code == ErrorCode.DB_UNAVAILABLE.value
            and observed["rows_after_failure"] == evidence_before,
            alert_emitted=False,
            recovery=_recovery(
                chain,
                state_before=state_before,
                tool_calls_before=tool_calls_before,
                evidence_ids=[created.evidence_id],
            ),
            notes="证据存储不可写时零新增行（不伪造证据）",
        )

    result = FaultSessionAdapter().run(FaultClass.EVIDENCE_STORE, probe=probe)

    assert result.injected is True
    assert result.fail_closed_ok is True
    assert result.boundary is RecoveryBoundary.AUTO
    assert observed["raised_code"] == ErrorCode.DB_UNAVAILABLE.value
    assert observed["rows_after_failure"] == evidence_before
    assert_recovery_integrity(result.recovery)
    assert result.recovery.tool_calls_delta == 0
    assert result.recovery.replay_occurred is False
    assert chain.evidence_count() == evidence_before + 1


def _evidence_request(fixture: _Fixture) -> EvidenceCreate:
    return EvidenceCreate(
        tenant_id=fixture.tenant_id,
        task_id=fixture.task_id,
        environment_id=fixture.environment_id,
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="mysql-w11",
        source_tool="w11_fault_probe",
        observed_at=utc_now().isoformat(),
        collection_status=CollectionStatus.SUCCESS,
        quality=EvidenceQuality.MEDIUM,
        payload={"probe": "w11-fault-evidence", "note": "w11 fault injection"},
        redaction_version="redaction-v2",
    )


# ---------------------------------------------------------------------------
# 2) AUDIT_WRITER：AC-08（R2 阻断）+ AC-09（R0 降级）
# ---------------------------------------------------------------------------


def test_audit_writer_failure_blocks_r2_and_degrades_r0(
    control_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        failing = _FailingLedger()
        audit = AuditService(failing)  # type: ignore[arg-type]
        tool = _StubR2WriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        storage = _ToolCallMemory()
        executor = ToolExecutor(
            registry, storage,  # type: ignore[arg-type]
            step_records=_StepRecordMemory(),  # type: ignore[arg-type]
            audit=audit,
        )
        grant = issue_grant(
            approval_id=f"apr_w11_fault_{uuid4().hex[:8]}",
            tenant_id=fixture.tenant_id,
            task_id=fixture.task_id,
            step_id=None,
            tool_name=tool.definition.name,
            tool_version=tool.definition.version,
            environment_id=fixture.environment_id,
            action_digest=canonical_digest({"probe": "w11-audit-writer"}),
            prerequisites_digest=canonical_digest({"probe": "w11-audit-writer-prereq"}),
            policy_version=_POLICY_VERSION,
            consumed_by="user_w11_fault",
            consumed_at=utc_now(),
        )
        context = ToolContext(
            client=_WriteContextClient(), task_id=fixture.task_id, run_id=fixture.run_id
        )
        r2_result = executor.execute(
            tool.definition.name,
            {"probe": "w11"},
            context,
            task=chain.tasks.get_task(fixture.task_id, tenant_id=fixture.tenant_id),
            idempotency_key="w11-audit-writer",
            approval_grant=grant,
        )
        # AC-09：R0 读取路径必须走 try_emit 降级（不抛错）
        r0_event = make_event(
            AuditEventType.TOOL_CALL_FAILED,
            task_id=fixture.task_id,
            run_id=fixture.run_id,
            payload={"probe": "w11-r0-degrade", "tenant_id": fixture.tenant_id},
        )
        r0_degraded = audit.try_emit(r0_event) is False

        observed["r2_result"] = r2_result
        observed["r0_degraded"] = r0_degraded
        observed["executed"] = storage.executed
        observed["tool_calls_after"] = chain.tool_calls()
        return FaultProbeOutcome(
            injected=failing.attempts >= 1,
            fail_closed=(
                r2_result.status is ToolStatus.DENIED
                and r2_result.error is not None
                and r2_result.error.code == ErrorCode.AUDIT_UNAVAILABLE.value
                and tool.calls == 0
                and storage.executed == 0
            ),
            alert_emitted=ALERT_AUDIT_WRITE_FAILED in _alerts(caplog),
            recovery=_recovery(
                chain, state_before=state_before, tool_calls_before=tool_calls_before
            ),
            notes="R2 在 emit_required 处阻断；R0 走 try_emit 降级为 False",
        )

    with caplog.at_level(logging.ERROR):
        result = FaultSessionAdapter().run(FaultClass.AUDIT_WRITER, probe=probe)

    assert result.injected is True
    assert result.fail_closed_ok is True
    assert result.alert_emitted is True
    assert result.boundary is RecoveryBoundary.MANUAL
    r2_result = observed["r2_result"]
    assert r2_result.status is ToolStatus.DENIED
    assert r2_result.error is not None
    assert r2_result.error.code == ErrorCode.AUDIT_UNAVAILABLE.value
    assert observed["r0_degraded"] is True
    assert observed["executed"] == 0
    assert observed["tool_calls_after"] == tool_calls_before, "R2 阻断不得留下 tool_calls 行"
    assert_recovery_integrity(result.recovery)
    assert result.recovery.tool_calls_delta == 0
    # 账本不可写期间的告警退化为结构化日志（ADR-0006:129），如实登记
    assert "AUDIT_WRITE_FAILED" in _alerts(caplog)


# ---------------------------------------------------------------------------
# 3) DB_DISCONNECT：目标库断连 → 不伪造采集结果
# ---------------------------------------------------------------------------


def test_db_disconnect_does_not_fabricate_observation(
    control_db: Any,
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        tool = build_observer_registry().get("get_db_version")
        broken = _ReadContextClient(fail=True)
        context = ToolContext(
            client=broken, task_id=fixture.task_id, run_id=fixture.run_id  # type: ignore[arg-type]
        )
        failed = tool.run(context, {})  # type: ignore[attr-defined]
        observed["failed_status"] = failed.status.value
        observed["failed_code"] = failed.error.code if failed.error else None
        observed["failed_data_empty"] = failed.data == {}

        # 恢复：连接可用后同一工具返回非 ERROR（数据来自替身，用于验证状态映射）
        # 恢复：连接可用后同一工具返回 SUCCESS（行集是 LAB 库替身，只用于验证状态映射）
        healthy = _ReadContextClient(
            rows=[
                {
                    "version": "8.0.46",
                    "version_comment": "w11 probe",
                    "compile_os": "Linux",
                    "compile_machine": "x86_64",
                    "performance_schema_enabled": 1,
                    "global_read_only": 0,
                    "super_read_only": 0,
                    "current_user_name": "dg_ro@localhost",
                    "current_database": "dg_lab",
                    "server_time": utc_now().isoformat(),
                }
            ]
        )
        context_ok = ToolContext(
            client=healthy, task_id=fixture.task_id, run_id=fixture.run_id  # type: ignore[arg-type]
        )
        recovered = tool.run(context_ok, {})  # type: ignore[attr-defined]
        observed["recovered_status"] = recovered.status.value
        chain.append(AuditEventType.TOOL_CALL_FAILED, {"probe": "db-disconnect-observed"})
        return FaultProbeOutcome(
            injected=failed.status is ToolStatus.ERROR,
            fail_closed=(
                failed.status is ToolStatus.ERROR
                and failed.error is not None
                and failed.error.code == ErrorCode.DB_UNAVAILABLE.value
                and failed.data == {}
            ),
            alert_emitted=False,
            recovery=_recovery(
                chain, state_before=state_before, tool_calls_before=tool_calls_before
            ),
            notes="断连时返回 ERROR/DB_UNAVAILABLE 且 data 为空；写结果未知须人工核对",
        )

    result = FaultSessionAdapter().run(FaultClass.DB_DISCONNECT, probe=probe)

    assert result.injected is True
    assert result.fail_closed_ok is True
    assert result.boundary is RecoveryBoundary.MANUAL
    assert observed["failed_status"] == ToolStatus.ERROR.value
    assert observed["failed_code"] == ErrorCode.DB_UNAVAILABLE.value
    assert observed["failed_data_empty"] is True
    assert observed["recovered_status"] == ToolStatus.SUCCESS.value
    assert_recovery_integrity(result.recovery)
    assert result.recovery.tool_calls_delta == 0


# ---------------------------------------------------------------------------
# 4) MODEL_SERVICE：fail closed + 确定性降级
# ---------------------------------------------------------------------------


def test_model_service_failure_is_fail_closed(
    control_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        adapter = FakeModelAdapter(["w11 恢复后的模型输出"])

        def boom(request: Any) -> Any:
            raise ModelUnavailableError(
                "模型服务不可用", details={"reason": "MODEL_SERVICE_DOWN"}
            )

        def explode(request: Any) -> Any:
            raise RuntimeError("适配器连接被重置")

        raised_code: str | None = None
        with monkeypatch.context() as patch:
            patch.setattr(adapter, "complete", boom)
            gateway = ModelGateway(adapter)
            try:
                gateway.generate([{"role": "user", "content": "w11"}], purpose="w11-fault")
            except DBGuardianError as exc:
                raised_code = exc.code.value
        observed["raised_code"] = raised_code

        # 非领域异常由 ModelGateway 归一化为 MODEL_CALL_FAILED（两条路径都必须 fail closed）
        normalized_code: str | None = None
        with monkeypatch.context() as patch:
            patch.setattr(adapter, "complete", explode)
            try:
                ModelGateway(adapter).generate(
                    [{"role": "user", "content": "w11"}], purpose="w11-fault"
                )
            except ModelCallFailedError as exc:
                normalized_code = exc.code.value
        observed["normalized_code"] = normalized_code

        # 降级路径必须显式（DisabledModelGateway 抛 MODEL_UNAVAILABLE，不编造结论）
        degraded_code: str | None = None
        try:
            DisabledModelGateway().generate([{"role": "user", "content": "w11"}])
        except ModelUnavailableError as exc:
            degraded_code = exc.code.value
        observed["degraded_code"] = degraded_code

        # 恢复：模型可用后同一网关可返回文本
        recovered = ModelGateway(adapter).generate(
            [{"role": "user", "content": "w11"}], purpose="w11-fault"
        )
        observed["recovered_text"] = recovered.text
        chain.append(AuditEventType.MODEL_CALL_FAILED, {"probe": "model-service-failed"})
        return FaultProbeOutcome(
            injected=raised_code is not None,
            fail_closed=raised_code == ErrorCode.MODEL_UNAVAILABLE.value
            and normalized_code == ErrorCode.MODEL_CALL_FAILED.value
            and degraded_code == ErrorCode.MODEL_UNAVAILABLE.value,
            alert_emitted=False,
            recovery=_recovery(
                chain, state_before=state_before, tool_calls_before=tool_calls_before
            ),
            notes="模型不可用归一化为 MODEL_CALL_FAILED；降级路径显式抛 MODEL_UNAVAILABLE",
        )

    result = FaultSessionAdapter().run(FaultClass.MODEL_SERVICE, probe=probe)

    assert result.injected is True
    assert result.fail_closed_ok is True
    assert result.boundary is RecoveryBoundary.AUTO
    assert observed["raised_code"] == ErrorCode.MODEL_UNAVAILABLE.value
    assert observed["normalized_code"] == ErrorCode.MODEL_CALL_FAILED.value
    assert observed["degraded_code"] == ErrorCode.MODEL_UNAVAILABLE.value
    assert observed["recovered_text"] == "w11 恢复后的模型输出"
    assert_recovery_integrity(result.recovery)


# ---------------------------------------------------------------------------
# 5) WORKER_CRASH：Popen.kill() 可观测 + 崩溃等价构造后先核对不重放
# ---------------------------------------------------------------------------


class _StepRecordsMemory:
    """``step_records`` 替身：只读返回"已 Prepared 未 Observed"的候选。"""

    def __init__(self, prepared: StepPrepared) -> None:
        self._prepared = prepared
        self.calls: list[dict[str, Any]] = []

    @property
    def client(self) -> Any:
        raise AssertionError("本用例按任务范围核对，不应触达租户级扫描")

    def find_unobserved(
        self,
        *,
        tenant_id: str,
        older_than_s: float,
        limit: int,
        offset: int,
        task_id: str | None = None,
    ) -> list[StepPrepared]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return [self._prepared] if offset == 0 else []

    def get_observed(self, tool_call_id: str) -> None:
        return None


class _TaskRepoMemory:
    """``task_repository`` 替身：状态在内存里推进（**不 UPDATE 控制库**）。"""

    def __init__(self, task: Task) -> None:
        self._task = task
        self.transitions: list[tuple[str, int]] = []

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        return self._task

    def update_task_status(self, task_id: str, target: Any, **kwargs: Any) -> Task:
        self.transitions.append((target.value, int(kwargs.get("expected_state_version", 0))))
        self._task = self._task.model_copy(
            update={"status": target, "state_version": self._task.state_version + 1}
        )
        return self._task


class _LedgerMemory:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None, **kwargs: Any) -> Any:
        self.events.append(event)
        return event

    def event_types(self) -> list[str]:
        return [str(getattr(event.event_type, "value", event.event_type)) for event in self.events]


def test_worker_crash_is_detectable_and_recovered_by_reconciliation(
    control_db: Any,
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        # (a) 真实崩溃接缝可观测：启动子进程后立即 kill，并确认已终止
        child = subprocess.Popen(  # noqa: S603 - 固定 argv，不经 shell，不写任何文件
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child.kill()
        returncode = child.wait(timeout=30)
        observed["child_returncode"] = returncode

        # (b) 崩溃等价构造：已 Prepared 未 Observed → 由 Reconciler 收敛为待核对
        task = chain.tasks.get_task(fixture.task_id, tenant_id=fixture.tenant_id)
        prepared = StepPrepared(
            prepared_id=f"spr_w11_fault_{uuid4().hex[:8]}",
            tenant_id=fixture.tenant_id,
            task_id=fixture.task_id,
            run_id=fixture.run_id,
            tool_call_id=f"call_w11_fault_{uuid4().hex[:8]}",
            tool_name=LAB_WRITE_TOOL_NAME,
            tool_version="1.0",
            policy_version=_POLICY_VERSION,
            environment_id=fixture.environment_id,
            request={"probe": "w11-worker-crash"},
            prepared_at=utc_now() - timedelta(seconds=300),
        )
        ledger = _LedgerMemory()
        tasks = _TaskRepoMemory(task)
        reconciler = Reconciler(
            _StepRecordsMemory(prepared), tasks, ledger  # type: ignore[arg-type]
        )
        conclusions = reconciler.sweep_task(task_id=fixture.task_id, tenant_id=fixture.tenant_id)
        observed["conclusions"] = [item.conclusion for item in conclusions]
        observed["transitioned"] = [item.transitioned for item in conclusions]
        observed["reconciler_event_types"] = ledger.event_types()
        chain.append(
            AuditEventType.RECONCILIATION_REQUIRED, {"probe": "worker-crash-reconciled"}
        )
        return FaultProbeOutcome(
            injected=returncode is not None and returncode != 0,
            fail_closed=(
                len(conclusions) == 1
                and conclusions[0].conclusion == CONCLUSION_NEEDS_RECONCILIATION
                and "TOOL_CALL_RETRIED" not in ledger.event_types()
            ),
            alert_emitted=False,
            recovery=_recovery(
                chain, state_before=state_before, tool_calls_before=tool_calls_before
            ),
            notes="崩溃后只收敛为待核对（不重放、不判成功/失败），是否执行须人工确认",
        )

    result = FaultSessionAdapter().run(FaultClass.WORKER_CRASH, probe=probe)

    assert result.injected is True, "子进程未被 kill 成功"
    assert observed["child_returncode"] is not None
    assert result.fail_closed_ok is True
    assert result.boundary is RecoveryBoundary.MANUAL
    assert observed["conclusions"] == [CONCLUSION_NEEDS_RECONCILIATION]
    assert observed["transitioned"] == [True]
    assert "TOOL_CALL_RETRIED" not in observed["reconciler_event_types"]
    assert "RECONCILIATION_REQUIRED" in observed["reconciler_event_types"]
    assert_recovery_integrity(result.recovery)
    assert result.recovery.tool_calls_delta == 0
    assert result.recovery.replay_occurred is False


# ---------------------------------------------------------------------------
# 6) CONTROL_DB_TRANSIENT：瞬时故障 fail closed，恢复后不产生序号空洞
# ---------------------------------------------------------------------------


def test_control_db_transient_failure_keeps_seq_continuous(
    control_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _provision(control_db)
    chain = _Chain(control_db, fixture)
    state_before = chain.state_version()
    tool_calls_before = chain.tool_calls()
    seq_before = chain.seqs()
    observed: dict[str, Any] = {}

    def probe() -> FaultProbeOutcome:
        real_execute = control_db.execute

        def flaky_execute(sql: str, params: Any = None, **kwargs: Any) -> Any:
            raise DatabaseQueryError(
                "控制库短暂故障", details={"reason": "CONTROL_DB_TRANSIENT"}
            )

        raised_code: str | None = None
        with monkeypatch.context() as patch:
            patch.setattr(control_db, "execute", flaky_execute)
            try:
                AuditService(TaskRepository(control_db)).emit_required(
                    make_event(
                        AuditEventType.TASK_STATE_CHANGED,
                        task_id=fixture.task_id,
                        run_id=fixture.run_id,
                        payload={"probe": "control-db-transient", "tenant_id": fixture.tenant_id},
                    )
                )
            except AuditUnavailableError as exc:
                raised_code = exc.code.value
            observed["seq_during_failure"] = chain.seqs()

        observed["raised_code"] = raised_code
        # 恢复：故障消失后重试成功，且序号从故障前继续连续
        chain.append(AuditEventType.TASK_RECOVERED, {"probe": "control-db-recovered"})
        observed["seq_after_recovery"] = chain.seqs()
        return FaultProbeOutcome(
            injected=raised_code is not None,
            fail_closed=(
                raised_code == ErrorCode.AUDIT_UNAVAILABLE.value
                and observed["seq_during_failure"] == seq_before
            ),
            alert_emitted=False,
            recovery=_recovery(
                chain, state_before=state_before, tool_calls_before=tool_calls_before
            ),
            notes="瞬时故障在 emit_required 处 fail closed，重试后 seq 从故障前继续",
        )

    result = FaultSessionAdapter().run(FaultClass.CONTROL_DB_TRANSIENT, probe=probe)

    assert result.injected is True
    assert result.fail_closed_ok is True
    assert result.boundary is RecoveryBoundary.AUTO
    assert observed["raised_code"] == ErrorCode.AUDIT_UNAVAILABLE.value
    assert observed["seq_during_failure"] == seq_before, "故障期不得留下半截事件"
    assert observed["seq_after_recovery"] == list(range(len(observed["seq_after_recovery"])))
    assert_recovery_integrity(result.recovery)
    assert result.recovery.tool_calls_delta == 0


# ---------------------------------------------------------------------------
# 六类清单 / 冻结口径 / 适配器契约
# ---------------------------------------------------------------------------


def test_fault_manifest_is_strictly_six_classes(fault_cases: FaultCaseManifest) -> None:
    assert fault_cases.version == "fault-set-v1"
    assert len(fault_cases.cases) == FAULT_CASE_COUNT == 6
    assert tuple(item.fault_class for item in fault_cases.cases) == FAULT_CLASSES
    assert {item.boundary for item in fault_cases.cases} == {
        RecoveryBoundary.AUTO,
        RecoveryBoundary.MANUAL,
    }
    for item in fault_cases.cases:
        assert item.boundary is FAULT_BOUNDARIES[item.fault_class]
        assert item.recovery_scope.strip()
    # 网络分区 / 磁盘满不得出现在枚举里（W11-Q5）
    values = {item.value for item in FAULT_CLASSES}
    for forbidden in ("NETWORK_PARTITION", "DISK_FULL"):
        assert forbidden not in values


def test_fault_boundaries_are_not_uniformly_auto() -> None:
    """不得写成「全部可自动恢复」：六类里必须同时存在 AUTO 与 MANUAL。"""
    boundaries = {FAULT_BOUNDARIES[item] for item in FAULT_CLASSES}
    assert boundaries == {RecoveryBoundary.AUTO, RecoveryBoundary.MANUAL}
    manual = [item.value for item in FAULT_CLASSES if FAULT_BOUNDARIES[item] is RecoveryBoundary.MANUAL]
    assert sorted(manual) == ["AUDIT_WRITER", "DB_DISCONNECT", "WORKER_CRASH"]
    assert set(FAULT_BOUNDARY_REASONS) == set(FAULT_CLASSES)


def test_fault_counts_are_frozen(tmp_path: Path) -> None:
    payload = load_fault_cases(_FAULT_CASES_PATH).model_dump(mode="json")
    payload["cases"].pop()
    path = tmp_path / "fault_cases.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(FaultTypeError):
        load_fault_cases(path)


def test_seventh_fault_class_is_rejected(tmp_path: Path) -> None:
    payload = load_fault_cases(_FAULT_CASES_PATH).model_dump(mode="json")
    extra = dict(payload["cases"][0])
    extra["case_id"] = "NETWORK_PARTITION"
    extra["fault_class"] = "NETWORK_PARTITION"
    payload["cases"].append(extra)
    path = tmp_path / "fault_cases.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Exception):
        load_fault_cases(path)
    with pytest.raises(FaultTypeError):
        FaultSessionAdapter().run("NETWORK_PARTITION", probe=lambda: None)  # type: ignore[arg-type]


def test_adapter_rejects_non_outcome_probe() -> None:
    with pytest.raises(FaultTypeError):
        FaultSessionAdapter().run(FaultClass.MODEL_SERVICE, probe=lambda: {"injected": True})


def test_adapter_imports_benchmarks_lazily() -> None:
    """``benchmarks.*`` 只在方法体内惰性 import（顶层 import 会在只装 wheel 时失败）。"""
    source = (_REPO_ROOT / "core" / "assurance" / "faults.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith((" ", "	")):
            continue  # 缩进行 = 函数/方法体内，惰性 import 允许
        if line.startswith(("from benchmarks", "import benchmarks")):
            raise AssertionError(f"顶层 import benchmarks.* 被禁止：{line.strip()}")
    completed = subprocess.run(  # noqa: S603 - 固定 argv，只做 import 检查
        [
            sys.executable,
            "-c",
            "import sys; import core.assurance.faults as f; "
            "print(any(m.startswith('benchmarks') for m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_adapter_uses_fault_session_when_factory_given() -> None:
    """给出 ``session_factory`` 时走 W04 ``FaultSession`` 并采用其 injection 事实。"""
    from benchmarks.faults.injector import InjectionResult

    injected_result = InjectionResult(
        scenario_id="w11-probe-scenario",
        scenario="slow_query",
        db_engine="mysql",
        database_id="dg_lab",
        database="dg_lab",
        injected=True,
        parameters={},
        observed={},
        injected_at=utc_now().isoformat(),
    )

    class _Session:
        def __enter__(self) -> Any:
            return injected_result

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    recovery = RecoveryCheck(
        seq_continuous=True,
        audit_chain_ok=True,
        evidence_refs_resolvable=True,
        state_version_monotonic=True,
        tool_calls_delta=0,
        replay_occurred=False,
    )
    adapter = FaultSessionAdapter(session_factory=lambda: _Session())
    result = adapter.run(
        FaultClass.DB_DISCONNECT,
        probe=lambda: FaultProbeOutcome(
            injected=True, fail_closed=True, alert_emitted=True, recovery=recovery
        ),
    )
    assert result.injected is True
    assert "FAULT_SESSION" in result.notes
    assert result.boundary is RecoveryBoundary.MANUAL


def test_recovery_integrity_rejects_replay_and_delta() -> None:
    base = dict(
        seq_continuous=True,
        audit_chain_ok=True,
        evidence_refs_resolvable=True,
        state_version_monotonic=True,
        tool_calls_delta=0,
        replay_occurred=False,
    )
    assert_recovery_integrity(RecoveryCheck(**base))
    for overrides in (
        {"replay_occurred": True},
        {"tool_calls_delta": 1},
        {"seq_continuous": False},
        {"audit_chain_ok": False},
        {"evidence_refs_resolvable": False},
        {"state_version_monotonic": False},
    ):
        with pytest.raises(RecoveryIntegrityError):
            assert_recovery_integrity(RecoveryCheck(**{**base, **overrides}))


def test_summary_reports_six_cases_and_boundaries() -> None:
    """汇总口径：六类各一条、AUTO/MANUAL 各 3、无重放（结构化，不含请求正文）。"""
    recovery = RecoveryCheck(
        seq_continuous=True,
        audit_chain_ok=True,
        evidence_refs_resolvable=True,
        state_version_monotonic=True,
        tool_calls_delta=0,
        replay_occurred=False,
    )
    results = [
        FaultInjectionResult(
            test_id=f"fault-{item.value.lower().replace('_', '-')}",
            fault_class=item,
            injected=True,
            fail_closed_ok=True,
            alert_emitted=item is FaultClass.AUDIT_WRITER,
            recovery=recovery,
            boundary=FAULT_BOUNDARIES[item],
        )
        for item in FAULT_CLASSES
    ]
    summary = summarize_fault_results(results)
    assert summary["case_count"] == 6
    assert summary["fault_classes"] == [item.value for item in FAULT_CLASSES]
    assert summary["injected"] == 6
    assert summary["auto"] == 3
    assert summary["manual"] == 3
    assert summary["replay_occurred"] == 0
    assert summary["tool_calls_delta_total"] == 0


def test_manifest_assertion_accepts_frozen_manifest(fault_cases: FaultCaseManifest) -> None:
    assert_fault_manifest_frozen(fault_cases)
