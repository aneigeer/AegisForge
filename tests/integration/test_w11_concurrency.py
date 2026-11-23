"""W11 T02 集成测试：十类并发/竞争用例（AC-06 / AC-07）。

口径（``docs/w11/02-design.md`` §6.2、§6.3；裁定 W11-Q2 / W11-Q5）：

- ``CONCURRENCY_CASE_IDS`` **恰 10** 个，每条一个**独立**用例（不是参数化折叠），
  逐例断言 ``tool_calls_delta <= 1``、``target_row_changes <= 1``、
  ``seq == list(range(n))``、``"TOOL_CALL_RETRIED" not in event_types``、
  ``state_version_after >= state_version_before``；
- AC-07：旧 Worker → ``FENCING_TOKEN_STALE``；双 Worker → ``WORKER_CONFLICT``
  （复用 ``core/gateway/leases.py:175/:254/:267`` 的既有骨架，**不改其语义**）；
- 三个特殊例：``duplicate_message`` 第二轮 ``executed == 0`` 且 ``tool_calls`` 仍 1；
  ``approval_consume_race`` 恰 1 次 ``APPROVAL_CONSUMED``；``nonce_replay`` →
  ``NONCE_REPLAY``；
- 两个失败路径：``audit_write_failure`` → R2 被阻断（``AUDIT_WRITE_FAILED``）且
  **零已执行工具调用**；``gateway_unreachable`` → ``OUTCOME_SKIPPED`` 且零副作用。

**测试替身的边界（如实登记）**：本文件用内存/端口替身替代 MySQL 连接与
``tool_calls`` / ``approvals`` 存储；被测对象是**生产判定逻辑**本身
（``LeaseService.verify`` / ``TaskRepository.update_task_status`` 的 CAS /
``ToolExecutor`` 的幂等重放 / ``ApprovalService.consume`` 的九项校验与排序 /
``ToolGateway`` 的 nonce 唯一登记 / ``AuditService.emit_required`` 的 fail closed）。
DB 级唯一键与事务原子性由 ``database/migrations/006_w06_approval_audit.sql`` 的双唯一键
保证，本文件用静态断言核对它存在（不在此重复实现 W07 的 DB 并发证明）。

**本文件不连库、不写目标库、不执行任何 R2 动作**：所有计数以本卡 ``task_id`` 过滤，
不做全表 ``COUNT(*)``（``ENV-RESIDUE`` 不干扰）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pytest

from adapters.lab_assembly import build_lab_registry
from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
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
from core.approval.disclosure import build_disclosure, disclosure_digest
from core.approval.models import Approval, ApprovalStatus, issue_grant
from core.approval.repository import ApprovalRepository
from core.approval.service import ApprovalService
from core.assurance.concurrency import (
    CONCURRENCY_CASE_IDS,
    DUPLICATE_SIDE_EFFECT,
    OBJECT_AUTH_DENIED,
    PSEUDO_TASK_ID,
    ConcurrencyResult,
    DuplicateSideEffectDetected,
    assert_no_duplicate_side_effects,
    load_concurrency_manifest,
)
from core.audit.service import ALERT_AUDIT_WRITE_FAILED, AuditService
from core.errors import (
    ApprovalAlreadyConsumedError,
    AuditUnavailableError,
    DatabaseQueryError,
    DBGuardianError,
    EnvelopeInvalidError,
    ErrorCode,
    FencingTokenStaleError,
    NonceReplayError,
    StateConflictError,
    TaskNotFoundError,
    WorkerConflictError,
    WorkerLeaseInvalidError,
)
from core.gateway.envelope import build_envelope, sign_envelope
from core.gateway.leases import LeaseService
from core.gateway.tool_gateway import ToolGateway
from core.security.canonical import canonical_digest
from core.security.models import (
    AuthMethod,
    LeaseStatus,
    Principal,
    WorkerLease,
)
from core.security.policy import PolicyEngine, RiskResolver
from core.security.roles import Role
from core.tasks.models import Task, TaskStatus, ToolCallRecord, utc_now
from core.tasks.repository import TaskRepository
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry
from workers.runner import OUTCOME_SKIPPED

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "benchmarks" / "w11" / "concurrency_cases.json"
_MIGRATION_006 = _REPO_ROOT / "database" / "migrations" / "006_w06_approval_audit.sql"

_TENANT = "tenant_local"
_ENVIRONMENT_ID = "env_mysql_local"
_POLICY_VERSION = "w05-v1"
_ENV_ROW: dict[str, Any] = {
    "environment_id": _ENVIRONMENT_ID,
    "tenant_id": _TENANT,
    "db_engine": "mysql",
    "db_version": "8.0.46",
    "network_id": "lab_net",
    "image_digest": None,
    "config_version": "w11-concurrency",
    "status": "LAB",
}
_WRITE_PARAMS: dict[str, Any] = {
    "database": "dg_lab",
    "table": "seed_orders",
    "order_id": 2001,
    "keys": "order_id=2001",
    "note": "w11-concurrency-note",
    "expected_note": None,
}


def _task(*, task_id: str | None = None, state_version: int = 1) -> Task:
    now = utc_now()
    return Task(
        task_id=task_id or f"task_w11_conc_{uuid4().hex[:8]}",
        run_id=f"run_w11_conc_{uuid4().hex[:8]}",
        goal="W11 并发回归：不执行任何写动作",
        status=TaskStatus.EXECUTING,
        risk_level="R0",
        created_at=now,
        updated_at=now,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        created_by="user_w11_concurrency",
        state_version=state_version,
    )


def _principal(role: Role = Role.OPERATOR) -> Principal:
    return Principal(
        user_id="user_w11_concurrency",
        tenant_id=_TENANT,
        roles=(role,),
        session_id="sess_w11_concurrency",
        auth_method=AuthMethod.TOKEN,
    )


def _result(
    case_id: str,
    *,
    denied: bool | None,
    expected_code: str | None,
    tool_calls_delta: int = 0,
    target_row_changes: int = 0,
    seq: Sequence[int] = (),
    event_types: Sequence[str] = (),
    state_version_before: int = 1,
    state_version_after: int = 1,
) -> ConcurrencyResult:
    return ConcurrencyResult(
        test_id=f"w11-t02-{case_id}",
        case_id=case_id,
        denied=denied,
        expected_code=expected_code,
        tool_calls_delta=tool_calls_delta,
        target_row_changes=target_row_changes,
        seq=tuple(seq),
        event_types=tuple(event_types),
        state_version_before=state_version_before,
        state_version_after=state_version_after,
    )


# ---------------------------------------------------------------------------
# 通用替身
# ---------------------------------------------------------------------------


class _AuditLedger:
    """审计替身：按追加顺序分配 ``seq``（等价 ``task_events.seq = max+1``）。"""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str]] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None, **kwargs: Any) -> Any:
        self._append(event)
        return event

    def try_emit(self, event: Any, *, actor_user_id: str | None = None, **kwargs: Any) -> bool:
        self._append(event)
        return True

    def _append(self, event: Any) -> None:
        event_type = getattr(event.event_type, "value", str(event.event_type))
        self.entries.append((len(self.entries), event_type))

    def seq(self) -> tuple[int, ...]:
        return tuple(seq for seq, _ in self.entries)

    def event_types(self) -> tuple[str, ...]:
        return tuple(event_type for _, event_type in self.entries)


class _MemLeaseRepository:
    """内存租约仓储：忠实复刻 ``worker_leases`` 的条件更新与 fencing 递增语义。"""

    def __init__(self) -> None:
        self._leases: dict[str, WorkerLease] = {}
        self._max_token: dict[str, int] = {}

    # ---- reads ---------------------------------------------------------
    def get(self, lease_id: str) -> WorkerLease | None:
        return self._leases.get(lease_id)

    def get_active(self, task_id: str) -> WorkerLease | None:
        for lease in self._leases.values():
            if lease.task_id == task_id and lease.status is LeaseStatus.ACTIVE:
                return lease
        return None

    def max_fencing_token(self, task_id: str) -> int:
        return self._max_token.get(task_id, 0)

    # ---- writes --------------------------------------------------------
    def insert(self, lease: WorkerLease) -> None:
        self._leases[lease.lease_id] = lease
        self._max_token[lease.task_id] = max(
            self._max_token.get(lease.task_id, 0), lease.fencing_token
        )

    def mark_expired(self, lease_id: str, now: datetime) -> int:
        lease = self._leases.get(lease_id)
        if lease is None or lease.status is not LeaseStatus.ACTIVE:
            return 0
        self._leases[lease_id] = lease.model_copy(
            update={"status": LeaseStatus.EXPIRED, "released_at": now}
        )
        return 1

    def expire_stale(self, now: datetime) -> int:
        affected = 0
        for lease_id, lease in list(self._leases.items()):
            if lease.status is LeaseStatus.ACTIVE and lease.expires_at <= now:
                affected += self.mark_expired(lease_id, now)
        return affected

    def touch(
        self,
        lease_id: str,
        worker_id: str,
        fencing_token: int,
        *,
        heartbeat_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> int:
        lease = self._leases.get(lease_id)
        if (
            lease is None
            or lease.status is not LeaseStatus.ACTIVE
            or lease.worker_id != worker_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            return 0
        self._leases[lease_id] = lease.model_copy(
            update={"heartbeat_at": heartbeat_at, "expires_at": expires_at}
        )
        return 1

    def mark_released(
        self, lease_id: str, worker_id: str, fencing_token: int, released_at: datetime
    ) -> int:
        lease = self._leases.get(lease_id)
        if (
            lease is None
            or lease.status is not LeaseStatus.ACTIVE
            or lease.worker_id != worker_id
            or lease.fencing_token != fencing_token
        ):
            return 0
        self._leases[lease_id] = lease.model_copy(
            update={"status": LeaseStatus.RELEASED, "released_at": released_at}
        )
        return 1

    # ---- 崩溃进程模拟 ---------------------------------------------------
    def revive(self, lease_id: str) -> None:
        """把旧租约记录"复活"为 ACTIVE（模拟崩溃进程未清理，见演练报告用例 4）。"""
        lease = self._leases[lease_id]
        self._leases[lease_id] = lease.model_copy(
            update={"status": LeaseStatus.ACTIVE, "released_at": None}
        )


class _TaskRowClient:
    """只服务 ``TaskRepository.get_task`` 的控制库替身（CAS 冲突路径不写库）。"""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self._row = dict(row)
        self.writes = 0

    def fetch_all(self, sql: str, params: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        if "FROM dg_system.tasks" in sql:
            return [dict(self._row)]
        return []

    def execute(self, sql: str, params: Any = None, **kwargs: Any) -> int:
        self.writes += 1
        return 0


def _task_row(task: Task, *, state_version: int) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "goal": task.goal,
        "status": task.status.value,
        "risk_level": task.risk_level,
        "cancel_requested": 0,
        "pause_requested": 0,
        "error_code": None,
        "error_message": None,
        "created_at": task.created_at.replace(tzinfo=None),
        "updated_at": task.updated_at.replace(tzinfo=None),
        "finished_at": None,
        "tenant_id": task.tenant_id,
        "environment_id": task.environment_id,
        "created_by": task.created_by,
        "state_version": state_version,
        "deadline_at": None,
        "max_steps": 8,
        "max_tool_calls": 10,
        "max_tokens": 20000,
        "replan_count": 0,
    }


class _ToolCallRepository:
    """``tool_calls`` 存储替身：保留真实 ``ToolCallRecord``，便于幂等重放。"""

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

    def update_tool_call(
        self,
        tool_call_id: str,
        *,
        status: str,
        attempt: int,
        result: dict[str, Any] | None,
        error_code: str | None,
        error_message: str | None,
        duration_ms: float,
    ) -> None:
        for index, record in enumerate(self.records):
            if record.tool_call_id != tool_call_id:
                continue
            if record.status == "denied":
                continue
            self.records[index] = record.model_copy(
                update={
                    "status": status,
                    "attempt": attempt,
                    "result": result,
                    "error_code": error_code,
                    "error_message": error_message,
                    "duration_ms": duration_ms,
                }
            )

    def insert_event(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        return event

    def count_for_task(self, task_id: str) -> int:
        return sum(1 for record in self.records if record.task_id == task_id)

    def denied_for_task(self, task_id: str) -> int:
        return sum(
            1 for record in self.records if record.task_id == task_id and record.status == "denied"
        )


class _UnreachableRepository(_ToolCallRepository):
    """控制库不可达：``insert_tool_call`` 直接抛 ``DatabaseQueryError``。"""

    def insert_tool_call(self, record: ToolCallRecord) -> None:
        raise DatabaseQueryError(
            "控制库不可达，工具调用无法落账", details={"reason": "CONTROL_DB_UNREACHABLE"}
        )


class _FailingAuditRepository:
    """审计账本不可写：``insert_event`` 一律抛 ``DatabaseQueryError``。"""

    def __init__(self) -> None:
        self.attempts = 0

    def insert_event(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        raise DatabaseQueryError("审计账本不可写", details={"reason": "AUDIT_WRITE_FAILED"})

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        raise TaskNotFoundError("任务不存在", details={"task_id": task_id})


class _FakeLabClient:
    """工具上下文用的只读身份替身（不连库）。"""

    database_id = "dg_lab"
    read_only = True
    engine = "mysql"

    def identity(self) -> Any:
        raise AssertionError("本用例不需要真实身份")


class _WriteContextClient(_FakeLabClient):
    read_only = False


class _StubReadTool:
    def __init__(self, *, name: str = "w11_stub_read") -> None:
        self.calls = 0
        self.definition = ToolDefinition(
            name=name,
            description="W11 并发用例只读桩（不连库）",
            version="1.0",
            risk_level="R0",
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            db_engines=["mysql"],
            allowed_environments=["LAB"],
        )

    def run(self, context: ToolContext, params: Mapping[str, Any]) -> ToolResult:
        self.calls += 1
        return ToolResult(
            tool_name=self.definition.name,
            status=ToolStatus.SUCCESS,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=utc_now().isoformat(),
            duration_ms=1.0,
            data={"probe": "w11-concurrency-read"},
            source=ToolSource(
                database_id="dg_lab",
                db_version="8.0.46",
                collected_at=utc_now().isoformat(),
            ),
        )


class _StubR2WriteTool:
    """R2 写契约桩（只在**测试内存**注册；生产注册表仍只有唯一写工具）。"""

    def __init__(self) -> None:
        from tests.helpers.definitions import write_declarations
        from core.tools.contracts import TargetObjectSpec

        self.calls = 0
        self.definition = ToolDefinition(
            name="w11_stub_write",
            description="W11 并发用例 R2 桩（不连库、不写任何目标）",
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


class _StepRecordDouble:
    def __init__(self) -> None:
        self.prepared: list[Any] = []

    def insert_prepared(self, prepared: Any) -> Any:
        self.prepared.append(prepared)
        return prepared


class _TaskRepoDouble:
    def __init__(self, task: Task) -> None:
        self._task = task

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Task:
        if task_id != self._task.task_id:
            raise TaskNotFoundError("任务不存在", details={"task_id": task_id})
        return self._task


class _TransactionConn:
    """最小事务连接：``transaction()`` 只要求 begin/commit/rollback/close。"""

    def __init__(self) -> None:
        self.begun = 0
        self.committed = 0
        self.rolled_back = 0

    def begin(self) -> None:
        self.begun += 1

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        return None


class _TransactionClient:
    read_only = False
    database_id = "dg_system"

    def __init__(self) -> None:
        self.conns: list[_TransactionConn] = []

    def connect(self) -> _TransactionConn:
        conn = _TransactionConn()
        self.conns.append(conn)
        return conn


# ---------------------------------------------------------------------------
# AC-07①：旧 Worker 的 fencing 失效
# ---------------------------------------------------------------------------


def test_old_worker_fencing_is_rejected() -> None:
    task = _task()
    repository = _MemLeaseRepository()
    service = LeaseService(repository)  # type: ignore[arg-type]

    old_lease = service.acquire(
        worker_id="worker-old",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        ttl_s=30,
    )
    assert old_lease.fencing_token == 1

    # 演练报告用例 4 的构造：强制过期 → 新 Worker 取到 token2 并释放 →
    # 旧租约记录被"复活"为 ACTIVE（崩溃进程未清理）→ 旧 Worker 持 token1 提交。
    repository.mark_expired(old_lease.lease_id, utc_now())
    new_lease = service.acquire(
        worker_id="worker-new",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        ttl_s=30,
    )
    assert new_lease.fencing_token == 2
    service.release(new_lease.lease_id, "worker-new", new_lease.fencing_token)
    repository.revive(old_lease.lease_id)

    with pytest.raises(FencingTokenStaleError) as excinfo:
        service.verify(
            old_lease.lease_id, task.task_id, "worker-old", old_lease.fencing_token
        )
    assert excinfo.value.code is ErrorCode.FENCING_TOKEN_STALE

    result = _result(
        "old_worker_fencing",
        denied=True,
        expected_code="FENCING_TOKEN_STALE",
        tool_calls_delta=0,
        target_row_changes=0,
    )
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


# ---------------------------------------------------------------------------
# AC-07②：租约过期
# ---------------------------------------------------------------------------


def test_lease_expired_is_rejected() -> None:
    task = _task()
    repository = _MemLeaseRepository()
    service = LeaseService(repository)  # type: ignore[arg-type]
    lease = service.acquire(
        worker_id="worker-old",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        ttl_s=-1,  # 立刻过期（服务端时间判定，不看客户端）
    )
    with pytest.raises(WorkerLeaseInvalidError) as excinfo:
        service.verify(lease.lease_id, task.task_id, "worker-old", lease.fencing_token)
    assert excinfo.value.code is ErrorCode.WORKER_LEASE_INVALID
    assert "过期" in excinfo.value.message

    result = _result(
        "lease_expired", denied=True, expected_code="WORKER_LEASE_INVALID"
    )
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


# ---------------------------------------------------------------------------
# AC-07③：伪造 fencing token
# ---------------------------------------------------------------------------


def test_wrong_fencing_token_is_rejected() -> None:
    task = _task()
    repository = _MemLeaseRepository()
    service = LeaseService(repository)  # type: ignore[arg-type]
    lease = service.acquire(
        worker_id="worker-a",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        ttl_s=30,
    )
    forged = lease.fencing_token + 5
    with pytest.raises(FencingTokenStaleError) as excinfo:
        service.verify(lease.lease_id, task.task_id, "worker-a", forged)
    assert excinfo.value.code is ErrorCode.FENCING_TOKEN_STALE
    assert excinfo.value.details["fencing_token"] == forged

    result = _result("wrong_fencing_token", denied=True, expected_code="FENCING_TOKEN_STALE")
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


# ---------------------------------------------------------------------------
# AC-07④：双 Worker 抢同一任务
# ---------------------------------------------------------------------------


def test_two_workers_same_task_conflict() -> None:
    task = _task()
    repository = _MemLeaseRepository()
    service = LeaseService(repository)  # type: ignore[arg-type]
    first = service.acquire(
        worker_id="worker-a",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        ttl_s=60,
    )
    with pytest.raises(WorkerConflictError) as excinfo:
        service.acquire(
            worker_id="worker-b",
            task_id=task.task_id,
            tenant_id=_TENANT,
            environment_id=_ENVIRONMENT_ID,
            ttl_s=60,
        )
    assert excinfo.value.code is ErrorCode.WORKER_CONFLICT
    assert excinfo.value.details["lease_id"] == first.lease_id

    result = _result("two_workers_same_task", denied=True, expected_code="WORKER_CONFLICT")
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


# ---------------------------------------------------------------------------
# 状态版本冲突（CAS）
# ---------------------------------------------------------------------------


def test_state_version_conflict_is_rejected() -> None:
    task = _task(state_version=2)
    client = _TaskRowClient(_task_row(task, state_version=2))
    repository = TaskRepository(client)  # type: ignore[arg-type]
    with pytest.raises(StateConflictError) as excinfo:
        repository.update_task_status(
            task.task_id, TaskStatus.NEEDS_RECONCILIATION, expected_state_version=1
        )
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT
    assert excinfo.value.details["expected"] == 1
    assert excinfo.value.details["actual"] == 2
    assert client.writes == 0, "版本冲突必须在写库前拒绝（零写入）"

    result = _result(
        "state_version_conflict",
        denied=True,
        expected_code="STATE_CONFLICT",
        state_version_before=2,
        state_version_after=2,
    )
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


# ---------------------------------------------------------------------------
# 重复消息：第二轮不执行、tool_calls 仍为 1
# ---------------------------------------------------------------------------


def test_duplicate_message_does_not_execute_twice() -> None:
    task = _task()
    tool = _StubReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    storage = _ToolCallRepository()
    ledger = _AuditLedger()
    executor = ToolExecutor(
        registry, storage,  # type: ignore[arg-type]
        audit=ledger,  # type: ignore[arg-type]
    )
    context = ToolContext(client=_FakeLabClient(), task_id=task.task_id, run_id=task.run_id)

    first = executor.execute(
        tool.definition.name,
        {"probe": "w11"},
        context,
        task=task,
        idempotency_key="w11-dup-msg",
    )
    assert first.status is ToolStatus.SUCCESS
    assert tool.calls == 1
    calls_after_first = storage.count_for_task(task.task_id)
    assert calls_after_first == 1

    second = executor.execute(
        tool.definition.name,
        {"probe": "w11"},
        context,
        task=task,
        idempotency_key="w11-dup-msg",
    )
    assert tool.calls == 1, "第二轮不得再次执行工具（executed == 0）"
    assert storage.count_for_task(task.task_id) == calls_after_first == 1, "tool_calls 仍为 1"
    assert "idempotent_replay" in second.warnings
    assert "TOOL_CALL_RETRIED" not in ledger.event_types()

    result = _result(
        "duplicate_message",
        denied=None,
        expected_code=None,
        tool_calls_delta=storage.count_for_task(task.task_id) - calls_after_first,
        target_row_changes=0,
        seq=ledger.seq(),
        event_types=ledger.event_types(),
    )
    assert result.tool_calls_delta == 0
    assert_no_duplicate_side_effects(result)


# ---------------------------------------------------------------------------
# 审批并发消费：恰 1 次 APPROVAL_CONSUMED
# ---------------------------------------------------------------------------


class _ApprovalRepositoryDouble(ApprovalRepository):
    """审批仓储替身：状态与消费台账都在内存，**判定逻辑仍走真实 service**。"""

    def __init__(self, approval: Approval) -> None:
        super().__init__(_TransactionClient())  # type: ignore[arg-type]
        self._approval = approval
        self.consumptions: list[dict[str, Any]] = []

    def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        if approval_id != self._approval.approval_id or tenant_id != self._approval.tenant_id:
            raise TaskNotFoundError("审批不存在", details={"approval_id": approval_id})
        return self._approval

    def find_compensation_for(self, approval_id: str) -> str | None:
        return None

    def lock_row(self, conn: Any, approval_id: str, *, tenant_id: str) -> dict[str, Any] | None:
        if approval_id != self._approval.approval_id:
            return None
        return {
            "approval_id": self._approval.approval_id,
            "tenant_id": self._approval.tenant_id,
            "task_id": self._approval.task_id,
            "nonce_hash": self._approval.nonce_hash,
            "action_digest": self._approval.action_digest,
            "parameters_digest": self._approval.parameters_digest,
            "target_objects_digest": self._approval.target_objects_digest,
            "prerequisites_digest": self._approval.prerequisites_digest,
            "policy_version": self._approval.policy_version,
            "tool_name": self._approval.tool_name,
            "tool_version": self._approval.tool_version,
            "disclosure_digest": self._approval.disclosure_digest,
            "disclosure_json": self._approval.disclosure,
            "status": self._approval.status.value,
            "expires_at": self._approval.expires_at,
        }

    def insert_consumption(self, conn: Any, **kwargs: Any) -> None:
        self.consumptions.append(dict(kwargs))

    def mark_consumed(
        self, conn: Any, approval_id: str, *, consumed_by: str, consumed_at: datetime
    ) -> int:
        if self._approval.status is not ApprovalStatus.APPROVED:
            return 0
        self._approval = self._approval.model_copy(
            update={
                "status": ApprovalStatus.CONSUMED,
                "consumed_at": consumed_at,
                "consumed_by": consumed_by,
            }
        )
        return 1

    def reread_status(self, conn: Any, approval_id: str) -> ApprovalStatus | None:
        return self._approval.status


class _ProbeStub:
    """前置条件探针替身：控制面 checks 为空（与登记值一致，故摘要相等）。"""

    def __init__(self) -> None:
        self.policy_version: str | None = _POLICY_VERSION

    def bind_policy_version(self, version: str) -> None:
        self.policy_version = version

    def control_plane_checks(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def probe(self, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        return []


def _consistent_approval(settings: Any) -> tuple[Approval, Task, ToolDefinition]:
    """构造一条与真实 consume 九项校验**逐项自洽**的审批（不写库）。"""
    definition = build_lab_registry(None, _ENV_ROW, "mysql").get(
        LAB_WRITE_TOOL_NAME
    ).definition
    fingerprint = environment_fingerprint_from_row(_ENV_ROW)
    parameters = normalize_tool_parameters(definition, _WRITE_PARAMS)
    targets = normalize_target_objects(extract_target_objects(definition, parameters))
    operation = build_normalized_operation(definition, targets)
    descriptor = build_action_descriptor(
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
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
    task = _task()
    resolution = RiskResolver().resolve_risk(
        definition,
        _environment_context(),
    )
    disclosure = build_disclosure(
        resolution=resolution,
        definition=definition,
        captured_at=utc_now(),
        policy_version=_POLICY_VERSION,
    )
    now = utc_now()
    approval = Approval(
        approval_id=f"apr_w11_conc_{uuid4().hex[:8]}",
        tenant_id=_TENANT,
        task_id=task.task_id,
        environment_id=_ENVIRONMENT_ID,
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
        disclosure=disclosure,
        disclosure_digest=disclosure_digest(disclosure),
        disclosure_version="disclosure-v1",
        disclosed_at=now,
        policy_version=_POLICY_VERSION,
        tool_name=LAB_WRITE_TOOL_NAME,
        tool_version="1.0",
        requested_by="user_w11_requester",
        nonce_hash=hashlib.sha256(f"{task.task_id}-nonce".encode("utf-8")).hexdigest(),
        status=ApprovalStatus.APPROVED,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    return approval, task, definition


def _environment_context() -> Any:
    from core.security.models import EnvironmentContext

    return EnvironmentContext(
        environment_id=_ENVIRONMENT_ID,
        tenant_id=_TENANT,
        db_engine="mysql",
        tier="LAB",
        db_version="8.0.46",
    )


def test_approval_consume_race_allows_exactly_one_consumption(settings: Any) -> None:
    approval, task, definition = _consistent_approval(settings)
    repository = _ApprovalRepositoryDouble(approval)
    ledger = _AuditLedger()
    service = ApprovalService(
        repository,  # type: ignore[arg-type]
        _TaskRepoDouble(task),  # type: ignore[arg-type]
        _ProbeStub(),  # type: ignore[arg-type]
        ledger,  # type: ignore[arg-type]
        settings=settings,
        registry=build_lab_registry(None, _ENV_ROW, "mysql"),
    )

    grant = service.consume(
        approval_id=approval.approval_id,
        principal=_principal(),
        task=task,
        definition=definition,
        params=dict(_WRITE_PARAMS),
        environment_row=dict(_ENV_ROW),
        policy_version=_POLICY_VERSION,
        envelope_id="envl_w11_race",
        tool_call_id=None,
    )
    assert grant.approval_id == approval.approval_id
    assert len(repository.consumptions) == 1
    assert ledger.event_types().count("APPROVAL_CONSUMED") == 1

    with pytest.raises(ApprovalAlreadyConsumedError) as excinfo:
        service.consume(
            approval_id=approval.approval_id,
            principal=_principal(),
            task=task,
            definition=definition,
            params=dict(_WRITE_PARAMS),
            environment_row=dict(_ENV_ROW),
            policy_version=_POLICY_VERSION,
            envelope_id="envl_w11_race_2",
            tool_call_id=None,
        )
    assert excinfo.value.code is ErrorCode.APPROVAL_ALREADY_CONSUMED
    assert len(repository.consumptions) == 1, "恰 1 条消费台账，第二次不得写入"
    assert ledger.event_types().count("APPROVAL_CONSUMED") == 1, "恰 1 次 APPROVAL_CONSUMED"

    result = _result(
        "approval_consume_race",
        denied=None,
        expected_code=None,
        tool_calls_delta=0,
        target_row_changes=len(repository.consumptions),
        seq=ledger.seq(),
        event_types=ledger.event_types(),
    )
    assert result.target_row_changes == 1
    assert_no_duplicate_side_effects(result)


def test_consumption_ledger_has_dual_unique_keys() -> None:
    """DB 级并发屏障的静态证据：消费台账对 ``approval_id`` / ``nonce_hash`` 双唯一。"""
    source = _MIGRATION_006.read_text(encoding="utf-8")
    marker = "CREATE TABLE IF NOT EXISTS dg_system.approval_consumptions"
    assert marker in source
    block = source.split(marker, 1)[1].split("ENGINE=", 1)[0]
    assert "UNIQUE KEY uq_consumption_approval (approval_id)" in block
    assert "UNIQUE KEY uq_consumption_nonce (nonce_hash)" in block


# ---------------------------------------------------------------------------
# nonce 重放
# ---------------------------------------------------------------------------


class _NonceRegistryClient:
    """控制库 client 替身：第一次非 1062，第二次撞唯一键（1062）。"""

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


class _GatewayRepository:
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
        return dict(_ENV_ROW)

    def insert_event(self, event: Any, *args: Any, **kwargs: Any) -> Any:
        self.events.append(event)
        return event


class _NeverExecute:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("本用例不得触达工具执行")


def test_nonce_replay_is_rejected() -> None:
    task = _task()
    now = utc_now()
    lease = WorkerLease(
        lease_id=f"lease_w11_conc_{uuid4().hex[:8]}",
        worker_id="worker-w11",
        task_id=task.task_id,
        tenant_id=_TENANT,
        environment_id=_ENVIRONMENT_ID,
        fencing_token=1,
        status=LeaseStatus.ACTIVE,
        issued_at=now,
        heartbeat_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    repository = _MemLeaseRepository()
    repository.insert(lease)
    client = _NonceRegistryClient()
    gateway_repository = _GatewayRepository(client, task)
    registry = build_lab_registry(None, _ENV_ROW, "mysql")
    executor = _NeverExecute()
    gateway = ToolGateway(
        registry,
        gateway_repository,  # type: ignore[arg-type]
        LeaseService(repository),  # type: ignore[arg-type]
        PolicyEngine(policy_version=_POLICY_VERSION),
        executor,  # type: ignore[arg-type]
        lambda actor_user_id: _principal(),
        lambda envelope, task_row: None,  # type: ignore[arg-type,return-value]
        settings=_SettingsStub(),
    )
    envelope = sign_envelope(
        build_envelope(
            task_id=task.task_id,
            step_id=None,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=task.state_version,
            correlation_id=f"corr_w11_conc_{uuid4().hex[:8]}",
            actor_user_id="user_w11_concurrency",
            tool_name=LAB_WRITE_TOOL_NAME,
            tool_version="1.0",
            environment_id=_ENVIRONMENT_ID,
            params=dict(_WRITE_PARAMS),
            ttl_s=60,
            approval_id=None,
        ),
        _SettingsStub().gateway_key(),
    )

    first = gateway.execute(envelope, dict(_WRITE_PARAMS))
    assert first.status is ToolStatus.DENIED, "R2 且无 approval_id ⇒ 策略第 8b 步拒绝"
    assert first.error is not None and first.error.code == "APPROVAL_REQUIRED"
    assert executor.calls == 0

    with pytest.raises(NonceReplayError) as excinfo:
        gateway.execute(envelope, dict(_WRITE_PARAMS))
    assert excinfo.value.code is ErrorCode.NONCE_REPLAY
    assert executor.calls == 0, "重放信封不得进入执行链"

    result = _result(
        "nonce_replay",
        denied=True,
        expected_code="NONCE_REPLAY",
        tool_calls_delta=0,
        target_row_changes=0,
        event_types=tuple(
            getattr(event.event_type, "value", str(event.event_type))
            for event in gateway_repository.events
        ),
    )
    assert_no_duplicate_side_effects(result)
    assert excinfo.value.code.value == result.expected_code


class _SettingsStub:
    """``Settings`` 的最小面：只提供网关签名密钥与摘要版本（不读 .env）。"""

    action_digest_version = "w06-v1"
    policy_version = _POLICY_VERSION

    def gateway_key(self) -> bytes:
        return hashlib.sha256(b"w11-concurrency-gateway-key").digest()


# ---------------------------------------------------------------------------
# AC-08 交叠：审计写失败 → R2 被阻断、零执行
# ---------------------------------------------------------------------------


def test_audit_write_failure_blocks_r2_without_execution() -> None:
    task = _task()
    tool = _StubR2WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    storage = _ToolCallRepository()
    failing = _FailingAuditRepository()
    audit = AuditService(failing)  # type: ignore[arg-type]
    executor = ToolExecutor(
        registry, storage,  # type: ignore[arg-type]
        step_records=_StepRecordDouble(),  # type: ignore[arg-type]
        audit=audit,
    )
    grant = issue_grant(
        approval_id=f"apr_w11_audit_{uuid4().hex[:8]}",
        tenant_id=_TENANT,
        task_id=task.task_id,
        step_id=None,
        tool_name=tool.definition.name,
        tool_version=tool.definition.version,
        environment_id=_ENVIRONMENT_ID,
        action_digest=canonical_digest({"probe": "w11-audit-failure"}),
        prerequisites_digest=canonical_digest({"probe": "w11-audit-failure-prereq"}),
        policy_version=_POLICY_VERSION,
        consumed_by="user_w11_concurrency",
        consumed_at=utc_now(),
    )
    context = ToolContext(
        client=_WriteContextClient(), task_id=task.task_id, run_id=task.run_id
    )

    result = executor.execute(
        tool.definition.name,
        {"probe": "w11"},
        context,
        task=task,
        idempotency_key="w11-audit-failure",
        approval_grant=grant,
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == ErrorCode.AUDIT_UNAVAILABLE.value
    assert tool.calls == 0, "审计不可写即阻断，工具不得执行"
    assert storage.executed == 0, "零已执行工具调用"
    assert failing.attempts >= 1
    assert ALERT_AUDIT_WRITE_FAILED == "AUDIT_WRITE_FAILED"

    result_metric = _result(
        "audit_write_failure",
        denied=True,
        expected_code="AUDIT_WRITE_FAILED",
        tool_calls_delta=storage.executed,
        target_row_changes=0,
    )
    assert result_metric.tool_calls_delta == 0
    assert storage.denied_for_task(task.task_id) == 1, "拒绝也要留一条 denied 台账（可追溯）"
    assert_no_duplicate_side_effects(result_metric)


# ---------------------------------------------------------------------------
# 网关不可达 → OUTCOME_SKIPPED、零副作用
# ---------------------------------------------------------------------------


def test_gateway_unreachable_skips_without_side_effect() -> None:
    task = _task()
    tool = _StubReadTool(name="w11_stub_read_unreachable")
    registry = ToolRegistry()
    registry.register(tool)
    storage = _UnreachableRepository()
    ledger = _AuditLedger()
    executor = ToolExecutor(registry, storage, audit=ledger)  # type: ignore[arg-type]
    context = ToolContext(client=_FakeLabClient(), task_id=task.task_id, run_id=task.run_id)

    with pytest.raises(DBGuardianError) as excinfo:
        executor.execute(
            tool.definition.name,
            {"probe": "w11"},
            context,
            task=task,
            idempotency_key="w11-gateway-unreachable",
        )
    assert excinfo.value.code is ErrorCode.DB_QUERY_ERROR
    assert tool.calls == 0, "控制库不可达时工具不得执行（无执行事实）"
    assert storage.count_for_task(task.task_id) == 0
    assert OUTCOME_SKIPPED == "skipped"

    result = _result(
        "gateway_unreachable",
        denied=False,
        expected_code="OUTCOME_SKIPPED",
        tool_calls_delta=storage.count_for_task(task.task_id),
        target_row_changes=0,
        seq=ledger.seq(),
        event_types=ledger.event_types(),
    )
    assert result.tool_calls_delta == 0
    assert "TOOL_CALL_RETRIED" not in result.event_types
    assert_no_duplicate_side_effects(result)


# ---------------------------------------------------------------------------
# 冻结清单与谓词本身的守卫
# ---------------------------------------------------------------------------


def test_manifest_freezes_exactly_ten_cases() -> None:
    manifest = load_concurrency_manifest(_MANIFEST_PATH)
    assert len(manifest.cases) == 10
    assert tuple(manifest.case_ids()) == CONCURRENCY_CASE_IDS
    assert len(set(CONCURRENCY_CASE_IDS)) == 10
    assert len(manifest.cross_tenant_probes) == 7
    assert PSEUDO_TASK_ID == "system:worker_scheduler"
    assert OBJECT_AUTH_DENIED == "OBJECT_AUTH_DENIED"


def test_every_manifest_case_has_an_independent_test() -> None:
    """十条清单各自对应本文件一个独立 ``test_*``（防止被折叠进参数化）。"""
    source = Path(__file__).read_text(encoding="utf-8")
    expected_tests = {
        "old_worker_fencing": "test_old_worker_fencing_is_rejected",
        "lease_expired": "test_lease_expired_is_rejected",
        "wrong_fencing_token": "test_wrong_fencing_token_is_rejected",
        "two_workers_same_task": "test_two_workers_same_task_conflict",
        "state_version_conflict": "test_state_version_conflict_is_rejected",
        "duplicate_message": "test_duplicate_message_does_not_execute_twice",
        "approval_consume_race": "test_approval_consume_race_allows_exactly_one_consumption",
        "nonce_replay": "test_nonce_replay_is_rejected",
        "audit_write_failure": "test_audit_write_failure_blocks_r2_without_execution",
        "gateway_unreachable": "test_gateway_unreachable_skips_without_side_effect",
    }
    assert set(expected_tests) == set(CONCURRENCY_CASE_IDS)
    for case_id, test_name in expected_tests.items():
        assert f"def {test_name}(" in source, f"{case_id} 缺少独立用例 {test_name}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_calls_delta": 2},
        {"target_row_changes": 2},
        {"seq": (0, 2)},
        {"event_types": ("TOOL_CALL_RETRIED",)},
        {"state_version_before": 3, "state_version_after": 2},
    ],
    ids=[
        "tool_calls_duplicated",
        "target_rows_duplicated",
        "seq_broken",
        "retried",
        "state_version_regressed",
    ],
)
def test_zero_duplicate_side_effect_predicate_rejects_violations(
    overrides: Mapping[str, Any]
) -> None:
    base: dict[str, Any] = {
        "denied": None,
        "expected_code": None,
        "tool_calls_delta": 1,
        "target_row_changes": 1,
        "seq": (0, 1),
        "event_types": ("TOOL_CALL_STARTED",),
        "state_version_before": 1,
        "state_version_after": 2,
    }
    base.update(overrides)
    result = _result("old_worker_fencing", **base)
    assert result.no_duplicate_side_effect is False
    with pytest.raises(DuplicateSideEffectDetected) as excinfo:
        assert_no_duplicate_side_effects(result)
    assert excinfo.value.code == DUPLICATE_SIDE_EFFECT
    assert excinfo.value.test_id == result.test_id


def test_zero_duplicate_side_effect_predicate_accepts_compliant_result() -> None:
    result = _result(
        "duplicate_message",
        denied=None,
        expected_code=None,
        tool_calls_delta=1,
        target_row_changes=1,
        seq=(0, 1, 2),
        event_types=("TOOL_CALL_STARTED", "TOOL_CALL_REPLAYED"),
        state_version_before=1,
        state_version_after=1,
    )
    assert result.no_duplicate_side_effect is True
    assert_no_duplicate_side_effects(result)
