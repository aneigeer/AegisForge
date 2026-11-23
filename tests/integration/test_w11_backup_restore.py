"""W11 T05 集成测试：备份恢复的**可控构造**（AC-12 / AC-13）。

口径（`docs/w11/02-design.md` §8；裁定 W11-Q2 / W11-Q3 / W11-Q6 / Q-W11-B）：

- **四步**：① 只读 SELECT 六类对象 → `BackupSnapshot` + `canonical_digest`；
  ② 只 INSERT 18 条（六类各 3）；③ `ShadowStore.replay` 真实回放；
  ④ 六类计数逐值校验 + `seq` 连续 + `AuditService.verify` + Evidence 引用可解析；
- **AC-12**：`counts_after_restore[k] == counts_before[k]` 六类逐值；`appended_rows == 18`
  且这 12 行**不在** `ShadowStore`（证明是点恢复，不是「备份文件存在」）；
- **AC-13**：目标值（15 分钟 / 4 小时）与实测值分列，`measured_*` 全 `null`、
  `sla_claim is False`；校验器强制「实测缺席 ⇒ 不得声称 SLA」。

**替身/真实边界**：全部落点都是**真实控制库**（只 INSERT 追加）；本文件没有替身。
`ShadowStore` 刻意不持有 client，因此无法读实时库（"回放"不是"再查一次库"）。

**不做**：不 `DELETE`/`UPDATE`/`TRUNCATE`/`DROP`、不建影子表、不清理、不用 `DG_ADMIN_DSN`、
不实例化 `ObjectStorageArchiveSink`、不填任何 `measured_*`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.approval.digest import (
    environment_fingerprint_digest,
    environment_fingerprint_from_row,
)
from core.approval.disclosure import build_disclosure, disclosure_digest
from core.approval.models import Approval, ApprovalStatus
from core.approval.repository import ApprovalRepository
from core.assurance.backup import (
    _environment_row,
    _probe_disclosure,
    BACKUP_APPEND_N,
    BACKUP_OBJECT_CLASSES,
    BACKUP_OBJECT_TABLES,
    BACKUP_ROWS_PER_CLASS,
    NOT_SUPPORTED,
    RESTORE_MODE_LOGICAL_SHADOW,
    SLA_NOTE,
    STORAGE_TOPOLOGY,
    TARGET_RPO_MINUTES,
    TARGET_RTO_HOURS,
    BackupRestoreReport,
    BackupSnapshot,
    ControlledRestoreProcedure,
    ShadowStore,
    storage_topology_note,
)
from core.audit.events import AuditEventType, make_event
from core.audit.service import AuditService
from core.errors import DBGuardianError
from core.evidence.models import CollectionStatus, Evidence, EvidenceQuality
from core.evidence.repository import EvidenceRepository
from core.execution.step_records import StepObserved, StepRecordRepository
from core.security.canonical import canonical_digest
from core.tasks.models import new_id
from core.tasks.models import Task, TaskStatus, utc_now
from core.tasks.repository import TaskRepository

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKUP_SOURCE = _REPO_ROOT / "core" / "assurance" / "backup.py"
_TEST_SOURCE = Path(__file__)

_TENANT_PREFIX = "w11_bkp"
# 只匹配**真正的破坏性语句**（要求后接标识符），避免把文档里的
# 「不 DELETE/UPDATE/TRUNCATE/DROP」这类禁令字样当成命中（自指假阳性）。
_FORBIDDEN_SQL_PATTERNS = (
    r"DELETE\s+FROM\s+[`\w]",
    r"UPDATE\s+dg_system\.",
    r"TRUNCATE\s+TABLE\s+",
    r"DROP\s+(TABLE|DATABASE|SCHEMA)\s+",
    r"CREATE\s+(TABLE|DATABASE|SCHEMA)\s+",
)


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
        (tenant_id, f"W11 备份恢复 {suffix}"),
    )
    client.execute(
        "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
        "db_version, network_id, image_digest, config_version, status, created_at) "
        "VALUES (%s, %s, 'mysql', '8.0.46', 'lab_net', NULL, 'w11-backup', 'LAB', "
        "UTC_TIMESTAMP(6))",
        (environment_id, tenant_id),
    )
    tasks = TaskRepository(client)
    tasks.insert_task(
        Task(
            task_id=task_id,
            run_id=run_id,
            goal="W11 备份恢复可控构造（逻辑影子回放，不执行任何写动作）",
            status=TaskStatus.COMPLETED,
            risk_level="R0",
            created_at=now,
            updated_at=now,
            tenant_id=tenant_id,
            environment_id=environment_id,
            created_by="user_w11_backup",
            state_version=1,
        )
    )
    tasks.insert_event(
        make_event(
            AuditEventType.TASK_CREATED,
            task_id=task_id,
            run_id=run_id,
            payload={"probe": "w11-backup-provision", "tenant_id": tenant_id},
        ),
        tenant_id=tenant_id,
        actor_user_id="user_w11_backup",
    )
    # P2-T2：为**全部六类**各播一条真实行，保证 backup 前计数非空
    # （KD-W11-01 的原症状：approvals/evidence/step_observed 恒为 0，
    #  "restore 前后均非空"无法被证明）。
    approval_id = f"apr_{run_id}_seed"[:64]
    disclosure = _probe_disclosure()
    ApprovalRepository(client).insert(
        Approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            task_id=task_id,
            environment_id=environment_id,
            db_engine="mysql",
            environment_fingerprint=environment_fingerprint_from_row(
                _environment_row(environment_id, tenant_id)
            ),
            environment_fingerprint_digest=environment_fingerprint_digest(
                environment_fingerprint_from_row(_environment_row(environment_id, tenant_id))
            ),
            action_digest=canonical_digest({"probe": "w11-backup-seed"}),
            parameters_digest=canonical_digest({"probe": "w11-backup-seed-params"}),
            target_objects_digest=canonical_digest({"probe": "w11-backup-seed-targets"}),
            prerequisites_digest=canonical_digest({"probe": "w11-backup-seed-prereq"}),
            action_descriptor={"probe": "w11-backup-seed"},
            disclosure=disclosure,
            disclosure_digest=disclosure_digest(disclosure),
            policy_version="w05-v1",
            tool_name="w11_backup_probe",
            tool_version="1.0",
            requested_by="user_w11_backup",
            nonce_hash=canonical_digest({"nonce": approval_id}),
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now,
        )
    )
    client.execute(
        "INSERT INTO dg_system.approval_consumptions (consumption_id, approval_id, tenant_id,"
        " task_id, tool_call_id, envelope_id, nonce_hash, action_digest, policy_version,"
        " consumed_by, consumed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))",
        (
            f"cons_{run_id}_seed"[:64],
            approval_id,
            tenant_id,
            task_id,
            f"call_{run_id}_seed"[:64],
            None,
            canonical_digest({"nonce": f"cons_{run_id}_seed"}),
            canonical_digest({"probe": "w11-backup-seed"}),
            "w05-v1",
            "user_w11_backup",
        ),
    )
    client.execute(
        "INSERT INTO dg_system.step_prepared (prepared_id, tenant_id, task_id, run_id, step_id,"
        " tool_call_id, envelope_id, approval_id, tool_name, tool_version, action_digest,"
        " idempotency_key, policy_version, environment_id, request_json, prepared_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))",
        (
            f"prep_{run_id}_seed"[:64],
            tenant_id,
            task_id,
            run_id,
            None,
            f"call_{run_id}_prep"[:64],
            None,
            approval_id,
            "w11_backup_probe",
            "1.0",
            canonical_digest({"probe": "w11-backup-seed"}),
            f"idem_{run_id}_seed"[:128],
            "w05-v1",
            environment_id,
            json.dumps({"probe": "w11-backup-seed"}),
        ),
    )
    evidence_id = new_id("ev")
    EvidenceRepository(client).insert(
        Evidence(
            evidence_id=evidence_id,
            tenant_id=tenant_id,
            task_id=task_id,
            environment_id=environment_id,
            db_engine="mysql",
            db_version="8.0.46",
            adapter_version="mysql-w11",
            source_tool="w11_backup_probe",
            observed_at=now.isoformat(),
            collection_status=CollectionStatus.SUCCESS,
            quality=EvidenceQuality.MEDIUM,
            payload_hash=canonical_digest({"probe": "w11-backup-seed"}),
            redaction_version="redaction-v2",
            payload={"probe": "w11-backup-seed"},
        )
    )
    StepRecordRepository(client).insert_observed(
        StepObserved(
            observed_id=new_id("obs"),
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=f"call_{run_id}_obs"[:64],
            tool_name="w11_backup_probe",
            outcome="success",
            started_at=now,
            finished_at=now,
            result={"probe": "w11-backup-seed"},
        )
    )
    return _Fixture(
        tenant_id=tenant_id,
        environment_id=environment_id,
        task_id=task_id,
        run_id=run_id,
    )


def _count(client: Any, table: str, task_id: str) -> int:
    rows = client.fetch_all(
        f"SELECT COUNT(*) AS n FROM {table} WHERE task_id = %s", (task_id,)
    )
    return int(rows[0]["n"]) if rows else 0


@dataclass(frozen=True)
class _Run:
    fixture: _Fixture
    procedure: ControlledRestoreProcedure
    snapshot: BackupSnapshot
    counts_before: dict[str, int]
    counts_after_append: dict[str, int]
    restore_counts: dict[str, int]
    appended: int
    report: BackupRestoreReport


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@pytest.fixture(scope="module")
def run(control_db: Any) -> _Run:
    """四步各执行一次：backup → append → restore → report（模块内共享结果）。"""
    fixture = _provision(control_db)
    procedure = ControlledRestoreProcedure(
        client=control_db,
        task_ids=[fixture.task_id],
        tenant_id=fixture.tenant_id,
        environment_id=fixture.environment_id,
        run_id=fixture.run_id,
    )
    snapshot = procedure.backup()
    counts_before = {kind: _count(control_db, BACKUP_OBJECT_TABLES[kind], fixture.task_id) for kind in BACKUP_OBJECT_CLASSES}
    appended = procedure.append()
    counts_after_append = {
        kind: _count(control_db, BACKUP_OBJECT_TABLES[kind], fixture.task_id)
        for kind in BACKUP_OBJECT_CLASSES
    }
    restore_counts = procedure.restore(snapshot)
    report = procedure.report()
    return _Run(
        fixture=fixture,
        procedure=procedure,
        snapshot=snapshot,
        counts_before=counts_before,
        counts_after_append=counts_after_append,
        restore_counts=restore_counts,
        appended=appended,
        report=report,
    )


# ---------------------------------------------------------------------------
# ① backup：只读快照
# ---------------------------------------------------------------------------


def test_backup_snapshot_is_read_only_and_digested(run: _Run) -> None:
    snapshot = run.snapshot
    assert snapshot.version == "backup-snapshot-v1"
    assert snapshot.task_ids == (run.fixture.task_id,)
    assert set(snapshot.counts) == set(BACKUP_OBJECT_CLASSES)
    assert snapshot.counts == run.counts_before, "backup 不得写入任何行"
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot.digest)
    # 四类各自的物理表都能读到（task_events 至少有 TASK_CREATED 一条）
    assert snapshot.counts["task_events"] >= 1


# ---------------------------------------------------------------------------
# ② append：只 INSERT 12 条，四类各 3
# ---------------------------------------------------------------------------


def test_append_inserts_exactly_eighteen_rows_three_per_class(run: _Run) -> None:
    assert BACKUP_APPEND_N == 18
    assert run.appended == 18
    for kind in BACKUP_OBJECT_CLASSES:
        delta = run.counts_after_append[kind] - run.counts_before[kind]
        assert delta == BACKUP_ROWS_PER_CLASS == 3, f"{kind} 追加 {delta} 行（应为 3）"


# ---------------------------------------------------------------------------
# ③ restore：真实回放，四类计数逐值相等
# ---------------------------------------------------------------------------


def test_restore_reproduces_counts_for_all_four_classes(run: _Run) -> None:
    assert run.restore_counts == run.snapshot.counts
    for kind in BACKUP_OBJECT_CLASSES:
        assert run.restore_counts[kind] == run.counts_before[kind], kind
    assert all(
        run.counts_after_append[kind] > run.counts_before[kind] for kind in BACKUP_OBJECT_CLASSES
    ), "追加必须真的落库（否则'点恢复'无对照意义）"


def test_appended_rows_are_not_in_shadow_store(run: _Run) -> None:
    """点恢复的硬证据：追加的 12 条**不在**影子存储里（不是「备份文件存在」）。"""
    shadow = run.procedure.shadow
    assert shadow.replayed is True
    for kind in BACKUP_OBJECT_CLASSES:
        appended_ids = run.procedure.appended_ids(kind)
        assert len(appended_ids) == BACKUP_ROWS_PER_CLASS
        for row_id in appended_ids:
            assert shadow.contains(kind, row_id) is False, (kind, row_id)
    # 而快照里的行都应在影子存储里
    for kind in BACKUP_OBJECT_CLASSES:
        for row_id in run.snapshot.row_ids(kind):
            assert shadow.contains(kind, row_id) is True, (kind, row_id)


def test_shadow_store_counts_are_task_scoped(run: _Run) -> None:
    shadow = run.procedure.shadow
    for kind in BACKUP_OBJECT_CLASSES:
        assert shadow.count(kind, task_id=run.fixture.task_id) == run.counts_before[kind]
        # 其它 task_id 不得混入（计数按本卡任务过滤，不是全表）
        assert shadow.count(kind, task_id="task_not_ours") == 0
    assert shadow.digest() == run.procedure.shadow.digest()


# ---------------------------------------------------------------------------
# ④ verify：seq 连续 / 链通过 / Evidence 引用可解析
# ---------------------------------------------------------------------------


def test_recovery_integrity_after_append(run: _Run) -> None:
    tasks = TaskRepository(run.procedure._client)  # noqa: SLF001 - 只读校验用
    events = tasks.list_events(run.fixture.task_id, limit=500)
    assert [event.seq for event in events] == list(range(len(events))), "seq 必须连续"
    verification = AuditService(tasks).verify(
        tenant_id=run.fixture.tenant_id, task_id=run.fixture.task_id
    )
    assert verification.ok is True, verification
    evidence_ids = [*run.snapshot.row_ids("step_evidence"), *run.procedure.appended_ids("step_evidence")]
    missing = EvidenceRepository(run.procedure._client).validate_task_references(  # noqa: SLF001
        run.fixture.task_id, run.fixture.tenant_id, evidence_ids
    )
    assert missing == [], "Evidence 引用必须全部可解析"
    assert run.report.integrity_ok is True


# ---------------------------------------------------------------------------
# AC-13：目标/实测分列 + 诚实边界
# ---------------------------------------------------------------------------


def test_report_splits_targets_and_measurements(run: _Run) -> None:
    report = run.report
    assert report.target_rpo_minutes == TARGET_RPO_MINUTES == 15.0
    assert report.target_rto_hours == TARGET_RTO_HOURS == 4.0
    assert report.measured_rpo_minutes is None
    assert report.measured_rto_hours is None
    assert report.sla_claim is False
    assert report.sla_note == SLA_NOTE == "建议目标，未验证 SLA"
    assert report.appended_rows == 18
    assert report.counts_before == run.counts_before
    assert report.counts_after_restore == run.counts_before
    assert report.logical_replay_duration_s is not None
    assert report.logical_replay_duration_s >= 0.0


def test_report_keeps_four_honest_markers(run: _Run) -> None:
    report = run.report
    assert report.restore_mode == RESTORE_MODE_LOGICAL_SHADOW == "LOGICAL_SHADOW"
    assert report.physical_restore == NOT_SUPPORTED == "NOT_SUPPORTED"
    assert report.object_store == NOT_SUPPORTED == "NOT_SUPPORTED"
    assert report.storage_topology == STORAGE_TOPOLOGY
    assert report.registered_issues == (
        "PHYSICAL_RESTORE_NOT_SUPPORTED",
        "OBJECT_STORE_NOT_SUPPORTED",
        "RPO_RTO_NOT_MEASURED",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"sla_claim": True},
        {"measured_rpo_minutes": 15.0, "sla_claim": True},
        {"measured_rto_hours": 4.0, "sla_claim": True},
        {"sla_note": "已验证 SLA"},
        {"physical_restore": "SUPPORTED"},
        {"object_store": "SUPPORTED"},
        {"restore_mode": "PHYSICAL"},
    ],
    ids=[
        "sla_without_measurement",
        "rpo_only_claim",
        "rto_only_claim",
        "bad_sla_note",
        "physical_supported",
        "object_store_supported",
        "physical_mode",
    ],
)
def test_report_model_rejects_overclaim(payload: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "backup_digest": "0" * 64,
        "appended_rows": 12,
        "counts_before": {kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        "counts_after_restore": {kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        "integrity_ok": True,
    }
    base.update(payload)
    with pytest.raises(ValidationError):
        BackupRestoreReport(**base)


def test_report_model_accepts_measured_pair_with_claim() -> None:
    """反向对照：**两个**实测值都在时校验器不拦（但 W11 从不这样填）。"""
    report = BackupRestoreReport(
        backup_digest="0" * 64,
        appended_rows=12,
        counts_before={kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        counts_after_restore={kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        integrity_ok=True,
        measured_rpo_minutes=1.0,
        measured_rto_hours=1.0,
        sla_claim=True,
    )
    assert report.sla_claim is True
    # 本卡实际报告不得出现该组合
    assert BackupRestoreReport(
        backup_digest="0" * 64,
        appended_rows=12,
        counts_before={kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        counts_after_restore={kind: 0 for kind in BACKUP_OBJECT_CLASSES},
        integrity_ok=True,
    ).sla_claim is False


# ---------------------------------------------------------------------------
# W11-Q6：对象存储边界（不实例化占位类、不编造路径）
# ---------------------------------------------------------------------------


def test_object_store_boundary_is_registered_not_faked() -> None:
    note = storage_topology_note()
    assert STORAGE_TOPOLOGY in note
    assert "ObjectStorageArchiveSink" in note
    assert "NOT_SUPPORTED" in note
    worm_source = (_REPO_ROOT / "core" / "audit" / "worm.py").read_text(encoding="utf-8")
    assert "class ObjectStorageArchiveSink" in worm_source
    assert "NotImplementedError" in worm_source
    # 占位类在全仓（除定义处与测试断言文本外）没有任何装配点
    for relative in (
        "workers/stack.py",
        "adapters/lab_assembly.py",
        "core/assurance/backup.py",
    ):
        source = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "ObjectStorageArchiveSink(" not in source, relative


def test_backup_module_has_no_destructive_sql() -> None:
    source = _BACKUP_SOURCE.read_text(encoding="utf-8")
    test_source = _TEST_SOURCE.read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_SQL_PATTERNS:
        assert re.search(pattern, source, flags=re.IGNORECASE) is None, pattern
        assert re.search(pattern, test_source, flags=re.IGNORECASE) is None, pattern
    # 只读 SELECT + 只 INSERT
    assert "SELECT * FROM" in source
    for method in ("insert_event", "insert(", "insert_observed"):
        assert method in source, method


def test_shadow_store_cannot_read_live_database() -> None:
    """影子存储不持 client：结构上无法"回放=再查一次库"。"""
    shadow = ShadowStore()
    assert not hasattr(shadow, "client")
    assert shadow.counts() == {kind: 0 for kind in BACKUP_OBJECT_CLASSES}
    assert shadow.replayed is False


def test_procedure_rejects_wrong_append_count(control_db: Any) -> None:
    fixture = _provision(control_db)
    with pytest.raises(ValueError):
        ControlledRestoreProcedure(
            client=control_db,
            task_ids=[fixture.task_id],
            tenant_id=fixture.tenant_id,
            environment_id=fixture.environment_id,
            run_id=fixture.run_id,
            append_n=11,
        )
    procedure = ControlledRestoreProcedure(
        client=control_db,
        task_ids=[fixture.task_id],
        tenant_id=fixture.tenant_id,
        environment_id=fixture.environment_id,
        run_id=fixture.run_id,
    )
    with pytest.raises(ValueError):
        procedure.append(11)
    with pytest.raises(ValueError):
        ControlledRestoreProcedure(
            client=control_db,
            task_ids=[],
            tenant_id=fixture.tenant_id,
            environment_id=fixture.environment_id,
            run_id=fixture.run_id,
        )


def test_report_requires_backup_first(control_db: Any) -> None:
    fixture = _provision(control_db)
    procedure = ControlledRestoreProcedure(
        client=control_db,
        task_ids=[fixture.task_id],
        tenant_id=fixture.tenant_id,
        environment_id=fixture.environment_id,
        run_id=fixture.run_id,
    )
    with pytest.raises(RuntimeError):
        procedure.report()


def test_approvals_appended_belong_to_this_task(run: _Run) -> None:
    """四类追加行都带本卡 `task_id`（便于按任务过滤，不污染他人数据）。"""
    repository = ApprovalRepository(run.procedure._client)  # noqa: SLF001
    for approval_id in run.procedure.appended_ids("approvals"):
        approval = repository.get(approval_id, tenant_id=run.fixture.tenant_id)
        assert approval.task_id == run.fixture.task_id
