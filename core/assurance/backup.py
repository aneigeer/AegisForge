"""W11 T05：备份恢复的**可控构造**（P0-W11-04 / AC-12 / AC-13）。

设计依据：``docs/w11/02-design.md`` §8（W11-Q2 落地）、§11 签名；裁定 W11-Q2
（不以「环境可清零」为前提）、W11-Q3（RPO/RTO 只作建议目标，不作门禁硬阈值）、
W11-Q6（无独立 Object Store，如实登记 ``NOT_SUPPORTED``）、Q-W11-B（逻辑影子回放
四处硬标注不得省）。

四步可控构造（不使用 ``DG_ADMIN_DSN``、不做物理恢复、不 DELETE/UPDATE/TRUNCATE/DROP）：

1. **backup**：对本卡 ``task_id`` 集合**只读** SELECT 四类对象 → :class:`BackupSnapshot`
   + ``backup_digest = canonical_digest(...)``；
2. **append**：经既有仓储**只 INSERT** 12 条（四类各 3 条，带本卡 ``run_id`` 后缀）；
3. **restore**：:meth:`ShadowStore.replay` 把快照**真实回放**成独立可查询存储
   （② 的行不在其中 → 证明是"点恢复"，不是"备份文件存在"）；
4. **verify**：四类计数逐值校验 + ``seq == list(range(n))`` +
   ``AuditService.verify``（``core/audit/service.py:153`` 复用）+ Evidence 引用可解析。

诚实边界（全部写进报告与模型字段）：``restore_mode="LOGICAL_SHADOW"``、
``physical_restore="NOT_SUPPORTED"``、``object_store="NOT_SUPPORTED"``、
``measured_rpo_minutes`` / ``measured_rto_hours`` **恒为 None**、``sla_claim=False``；
``logical_replay_duration_s`` 只是耗时，**不是 RTO**。

六类对象的物理落点（``BACKUP_OBJECT_CLASSES`` 是冻结的逻辑类名）：

| 逻辑类 | 表 | 说明 |
|---|---|---|
| ``task_events`` | ``dg_system.task_events`` | 追加式事件（哈希链本体） |
| ``approvals`` | ``dg_system.approvals`` | 审批行 |
| ``step_evidence`` | ``dg_system.evidence`` | 证据行（引用可解析性由此校验） |
| ``approval_consumptions`` | ``dg_system.approval_consumptions`` | 审批消费台账（单次使用证据） |
| ``step_prepared`` | ``dg_system.step_prepared`` | 执行前证据（崩溃后核对"是否可能已执行"） |
| ``audit_chain`` | ``dg_system.step_observed`` | 审计链的"执行后回执"段 |
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from core.approval.digest import environment_fingerprint_digest, environment_fingerprint_from_row
from core.approval.disclosure import build_disclosure, disclosure_digest
from core.approval.models import Approval, ApprovalStatus
from core.approval.repository import ApprovalRepository
from core.audit.service import AuditService
from core.audit.events import AuditEventType, make_event
from core.evidence.models import CollectionStatus, Evidence, EvidenceQuality
from core.evidence.repository import EvidenceRepository
from core.execution.step_records import StepObserved, StepRecordRepository
from core.security.canonical import canonical_digest
from core.tasks.repository import TaskRepository
from core.tasks.models import new_id, utc_now

#: 追加总行数 = 6 类 × 3 条（P2-T2：新增 approval_consumptions / step_prepared）。
BACKUP_APPEND_N = 18
BACKUP_ROWS_PER_CLASS = 3
BACKUP_SNAPSHOT_VERSION = "backup-snapshot-v1"

#: 六类对象的冻结逻辑类名（顺序即报告顺序）。
BACKUP_OBJECT_CLASSES: tuple[str, ...] = (
    "task_events",
    "approvals",
    "approval_consumptions",
    "step_prepared",
    "step_evidence",
    "audit_chain",
)

#: 逻辑类 → 物理表（见模块 docstring 的映射表）。
BACKUP_OBJECT_TABLES: dict[str, str] = {
    "task_events": "dg_system.task_events",
    "approvals": "dg_system.approvals",
    "approval_consumptions": "dg_system.approval_consumptions",
    "step_prepared": "dg_system.step_prepared",
    "step_evidence": "dg_system.evidence",
    "audit_chain": "dg_system.step_observed",
}

#: 每类的行主键列（用于"追加行不在影子存储里"的逐行核对）。
BACKUP_ROW_ID_COLUMNS: dict[str, str] = {
    "task_events": "event_id",
    "approvals": "approval_id",
    "approval_consumptions": "consumption_id",
    "step_prepared": "prepared_id",
    "step_evidence": "evidence_id",
    "audit_chain": "observed_id",
}

RESTORE_MODE_LOGICAL_SHADOW = "LOGICAL_SHADOW"
NOT_SUPPORTED = "NOT_SUPPORTED"
STORAGE_TOPOLOGY = "控制库 dg_system.* + evidence 表；无独立 Object Store"
SLA_NOTE = "建议目标，未验证 SLA"

#: **建议目标**（不是已验证 SLA、不是门禁硬阈值；W11-Q3）。
TARGET_RPO_MINUTES = 15.0
TARGET_RTO_HOURS = 4.0

REGISTERED_PHYSICAL_RESTORE = "PHYSICAL_RESTORE_NOT_SUPPORTED"
REGISTERED_OBJECT_STORE = "OBJECT_STORE_NOT_SUPPORTED"
REGISTERED_RPO_RTO = "RPO_RTO_NOT_MEASURED"


@dataclass(frozen=True)
class BackupSnapshot:
    """一次逻辑快照（只读 SELECT 的结果 + 规范化摘要）。"""

    version: str
    task_ids: tuple[str, ...]
    counts: dict[str, int]
    rows: dict[str, tuple[dict[str, Any], ...]]
    digest: str

    def row_ids(self, kind: str) -> tuple[str, ...]:
        column = BACKUP_ROW_ID_COLUMNS[kind]
        return tuple(str(row[column]) for row in self.rows.get(kind, ()))


def _environment_row(environment_id: str, tenant_id: str) -> dict[str, Any]:
    """环境事实最小行（与 ``EnvironmentContext`` 同源），供指纹计算复用。"""
    return {
        "environment_id": environment_id,
        "tenant_id": tenant_id,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "network_id": "lab_net",
        "image_digest": None,
        "config_version": "w11-backup",
        "status": "LAB",
    }


def _probe_disclosure() -> dict[str, Any]:
    """构造**结构合法**的披露快照（复用 ``build_disclosure``，不手写键集）。"""
    from adapters.mysql.lab_repair import LabOrderNoteUpdateTool  # noqa: PLC0415 - 惰性：仅供探针行使用
    from core.security.models import EnvironmentContext
    from core.security.policy import RiskResolver

    definition = LabOrderNoteUpdateTool().definition
    environment = EnvironmentContext(
        environment_id="env_w11_backup_probe",
        tenant_id="tenant_w11_backup_probe",
        db_engine="mysql",
        tier="LAB",
        db_version="8.0.46",
    )
    resolution = RiskResolver().resolve_risk(definition, environment)
    return build_disclosure(
        resolution=resolution,
        definition=definition,
        captured_at=utc_now(),
        policy_version="w05-v1",
    )


class ShadowStore:
    """独立可查询的**影子存储**：只持有回放出来的快照行。

    刻意**不持有任何 client**：它无法读实时库，因此"回放"不是"再查一次库"
    （W11-Q2 的硬要求：不以「环境可清零」为前提）。
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in BACKUP_OBJECT_CLASSES
        }
        self._replayed = False

    # ---- 回放 ----------------------------------------------------------
    def replay(self, snapshot: BackupSnapshot) -> dict[str, int]:
        """把快照行逐条写入影子存储，返回**回放后**的四类计数。"""
        self._rows = {kind: {} for kind in BACKUP_OBJECT_CLASSES}
        id_columns = BACKUP_ROW_ID_COLUMNS
        for kind, rows in snapshot.rows.items():
            column = id_columns[kind]
            for row in rows:
                self._rows[kind][str(row[column])] = dict(row)
        self._replayed = True
        return self.counts()

    # ---- 查询 ----------------------------------------------------------
    def count(self, kind: str, *, task_id: str) -> int:
        """按 ``task_id`` 统计影子存储里的行数（**不是**全表计数）。"""
        if kind not in self._rows:
            raise KeyError(f"未知对象类：{kind}")
        return sum(1 for row in self._rows[kind].values() if str(row.get("task_id")) == task_id)

    def counts(self, *, task_id: str | None = None) -> dict[str, int]:
        if task_id is None:
            return {kind: len(rows) for kind, rows in self._rows.items()}
        return {kind: self.count(kind, task_id=task_id) for kind in BACKUP_OBJECT_CLASSES}

    def contains(self, kind: str, row_id: str) -> bool:
        return row_id in self._rows.get(kind, {})

    def digest(self) -> str:
        """影子存储内容摘要（按类 + 主键排序，可比对"回放是否同源"）。"""
        payload: dict[str, Any] = {}
        for kind in BACKUP_OBJECT_CLASSES:
            payload[kind] = [
                row_id for row_id in sorted(self._rows[kind])
            ]
        return canonical_digest(payload)

    @property
    def replayed(self) -> bool:
        return self._replayed


class BackupRestoreReport(BaseModel):
    """备份/恢复结论（目标值与实测值**分列**，实测缺席一律 ``None``）。"""

    model_config = ConfigDict(extra="forbid")

    restore_mode: Literal["LOGICAL_SHADOW"] = RESTORE_MODE_LOGICAL_SHADOW
    physical_restore: Literal["NOT_SUPPORTED"] = NOT_SUPPORTED
    object_store: Literal["NOT_SUPPORTED"] = NOT_SUPPORTED
    storage_topology: str = STORAGE_TOPOLOGY
    target_rpo_minutes: float = TARGET_RPO_MINUTES
    measured_rpo_minutes: float | None = None
    target_rto_hours: float = TARGET_RTO_HOURS
    measured_rto_hours: float | None = None
    sla_claim: bool = False
    sla_note: str = SLA_NOTE
    backup_digest: str
    appended_rows: int
    counts_before: dict[str, int]
    counts_after_restore: dict[str, int]
    integrity_ok: bool
    logical_replay_duration_s: float | None = None
    registered_issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _no_sla_without_measurement(self) -> "BackupRestoreReport":
        """``measured_*`` 任一缺席 ⇒ 不得声称 SLA（AC-13 的硬约束）。"""
        if (self.measured_rpo_minutes is None or self.measured_rto_hours is None) and (
            self.sla_claim
        ):
            raise ValueError(
                "measured_rpo_minutes / measured_rto_hours 缺席时禁止 sla_claim=True"
                "（建议目标不等于已验证 SLA）"
            )
        if self.sla_note != SLA_NOTE:
            raise ValueError(f"sla_note 必须逐字为「{SLA_NOTE}」")
        return self


class ControlledRestoreProcedure:
    """四步可控构造的唯一入口（只读 SELECT + 只 INSERT 追加；无 DDL、无清理）。"""

    def __init__(
        self,
        *,
        client: Any,
        task_ids: Sequence[str],
        tenant_id: str,
        environment_id: str,
        run_id: str,
        append_n: int = BACKUP_APPEND_N,
    ) -> None:
        if not task_ids:
            raise ValueError("task_ids 不能为空（计数必须按本卡任务过滤）")
        if append_n != BACKUP_APPEND_N:
            raise ValueError(f"append 条数冻结为 {BACKUP_APPEND_N}（六类各 3 条）")
        self._client = client
        self._task_ids = tuple(task_ids)
        self._tenant_id = tenant_id
        self._environment_id = environment_id
        self._run_id = run_id
        self._tasks = TaskRepository(client)
        self._evidence = EvidenceRepository(client)
        self._approvals = ApprovalRepository(client)
        self._step_records = StepRecordRepository(client)
        self._snapshot: BackupSnapshot | None = None
        self._shadow = ShadowStore()
        self._appended_rows = 0
        self._appended_ids: dict[str, list[str]] = {kind: [] for kind in BACKUP_OBJECT_CLASSES}
        self._replay_duration_s: float | None = None
        self._integrity_ok = False

    # ---- ① backup ------------------------------------------------------
    def backup(self) -> BackupSnapshot:
        """对本卡 ``task_id`` 集合只读 SELECT 六类对象并取摘要（不写入任何行）。"""
        rows: dict[str, tuple[dict[str, Any], ...]] = {}
        for kind in BACKUP_OBJECT_CLASSES:
            rows[kind] = tuple(self._select(kind))
        counts = {kind: len(rows[kind]) for kind in BACKUP_OBJECT_CLASSES}
        payload = {
            "version": BACKUP_SNAPSHOT_VERSION,
            "task_ids": list(self._task_ids),
            "counts": counts,
            "rows": {
                kind: [
                    {
                        "id": str(row[BACKUP_ROW_ID_COLUMNS[kind]]),
                        "task_id": str(row.get("task_id")),
                        "created_at": str(row.get("created_at") or row.get("observed_at") or ""),
                    }
                    for row in rows[kind]
                ]
                for kind in BACKUP_OBJECT_CLASSES
            },
        }
        snapshot = BackupSnapshot(
            version=BACKUP_SNAPSHOT_VERSION,
            task_ids=self._task_ids,
            counts=counts,
            rows=rows,
            digest=canonical_digest(payload),
        )
        self._snapshot = snapshot
        return snapshot

    # ---- ② append ------------------------------------------------------
    def append(self, n: int = BACKUP_APPEND_N) -> int:
        """只 INSERT 追加 ``n``（默认 18）条：六类各 3 条，带本卡 ``run_id`` 后缀。"""
        if n != BACKUP_APPEND_N:
            raise ValueError(f"append 条数冻结为 {BACKUP_APPEND_N}（六类各 3 条）")
        task_id = self._task_ids[0]
        appended = 0
        for index in range(BACKUP_ROWS_PER_CLASS):
            event = self._tasks.insert_event(
                make_event(
                    AuditEventType.CHECKPOINT_SAVED,
                    task_id=task_id,
                    run_id=self._run_id,
                    payload={
                        "probe": "w11-backup-append",
                        "run_id": self._run_id,
                        "index": index,
                    },
                ),
                tenant_id=self._tenant_id,
                actor_user_id="user_w11_backup",
            )
            self._appended_ids["task_events"].append(event.event_id)
            appended += 1

            approval_id = f"apr_{self._run_id}_{index}".replace(" ", "")[:64]
            disclosure = _probe_disclosure()
            self._approvals.insert(
                Approval(
                    approval_id=approval_id,
                    tenant_id=self._tenant_id,
                    task_id=task_id,
                    environment_id=self._environment_id,
                    db_engine="mysql",
                    environment_fingerprint=environment_fingerprint_from_row(
                        _environment_row(self._environment_id, self._tenant_id)
                    ),
                    environment_fingerprint_digest=environment_fingerprint_digest(
                        environment_fingerprint_from_row(
                            _environment_row(self._environment_id, self._tenant_id)
                        )
                    ),
                    action_digest=canonical_digest({"probe": "w11-backup-append", "index": index}),
                    parameters_digest=canonical_digest({"probe": "w11-backup-append-params"}),
                    target_objects_digest=canonical_digest({"probe": "w11-backup-append-targets"}),
                    prerequisites_digest=canonical_digest(
                        {"probe": "w11-backup-append-prerequisites"}
                    ),
                    action_descriptor={"probe": "w11-backup-append"},
                    disclosure=disclosure,
                    disclosure_digest=disclosure_digest(disclosure),
                    policy_version="w05-v1",
                    tool_name="w11_backup_probe",
                    tool_version="1.0",
                    requested_by="user_w11_backup",
                    nonce_hash=canonical_digest({"nonce": approval_id}),
                    status=ApprovalStatus.PENDING,
                    created_at=utc_now(),
                    expires_at=utc_now(),
                )
            )
            self._appended_ids["approvals"].append(approval_id)
            appended += 1

            consumption_id = f"cons_{self._run_id}_{index}"[:64]
            self._client.execute(
                "INSERT INTO dg_system.approval_consumptions (consumption_id, approval_id,"
                " tenant_id, task_id, tool_call_id, envelope_id, nonce_hash, action_digest,"
                " policy_version, consumed_by, consumed_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))",
                (
                    consumption_id,
                    approval_id,
                    self._tenant_id,
                    task_id,
                    f"call_{self._run_id}_{index}"[:64],
                    None,
                    canonical_digest({"nonce": consumption_id}),
                    canonical_digest({"probe": "w11-backup-append", "index": index}),
                    "w05-v1",
                    "user_w11_backup",
                ),
            )
            self._appended_ids["approval_consumptions"].append(consumption_id)
            appended += 1

            evidence_id = new_id("ev")
            self._evidence.insert(
                Evidence(
                    evidence_id=evidence_id,
                    tenant_id=self._tenant_id,
                    task_id=task_id,
                    environment_id=self._environment_id,
                    db_engine="mysql",
                    db_version="8.0.46",
                    adapter_version="mysql-w11",
                    source_tool="w11_backup_probe",
                    observed_at=utc_now().isoformat(),
                    collection_status=CollectionStatus.SUCCESS,
                    quality=EvidenceQuality.MEDIUM,
                    payload_hash=canonical_digest({"probe": "w11-backup-append", "index": index}),
                    redaction_version="redaction-v2",
                    payload={"probe": "w11-backup-append", "index": index},
                )
            )
            self._appended_ids["step_evidence"].append(evidence_id)
            appended += 1

            observed_id = new_id("obs")
            self._step_records.insert_observed(
                StepObserved(
                    observed_id=observed_id,
                    tenant_id=self._tenant_id,
                    task_id=task_id,
                    run_id=self._run_id,
                    tool_call_id=f"call_{self._run_id}_{index}"[:64],
                    tool_name="w11_backup_probe",
                    outcome="success",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    result={"probe": "w11-backup-append", "index": index},
                )
            )
            self._appended_ids["audit_chain"].append(observed_id)
            appended += 1

            prepared_id = f"prep_{self._run_id}_{index}"[:64]
            self._client.execute(
                "INSERT INTO dg_system.step_prepared (prepared_id, tenant_id, task_id, run_id,"
                " step_id, tool_call_id, envelope_id, approval_id, tool_name, tool_version,"
                " action_digest, idempotency_key, policy_version, environment_id, request_json,"
                " prepared_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))",
                (
                    prepared_id,
                    self._tenant_id,
                    task_id,
                    self._run_id,
                    None,
                    f"call_{self._run_id}_p{index}"[:64],
                    None,
                    approval_id,
                    "w11_backup_probe",
                    "1.0",
                    canonical_digest({"probe": "w11-backup-append", "index": index}),
                    f"idem_{self._run_id}_{index}"[:128],
                    "w05-v1",
                    self._environment_id,
                    json.dumps({"probe": "w11-backup-append", "index": index}),
                ),
            )
            self._appended_ids["step_prepared"].append(prepared_id)
            appended += 1

        self._appended_rows = appended
        return appended

    # ---- ③ restore -----------------------------------------------------
    def restore(self, snapshot: BackupSnapshot) -> dict[str, int]:
        """把快照**真实回放**进影子存储，返回回放后的四类计数。"""
        started = time.perf_counter()
        counts = self._shadow.replay(snapshot)
        self._replay_duration_s = round(time.perf_counter() - started, 6)
        self._integrity_ok = self._verify_integrity(snapshot, counts)
        return counts

    # ---- ④ report ------------------------------------------------------
    def report(self) -> BackupRestoreReport:
        if self._snapshot is None:
            raise RuntimeError("必须先 backup() 再取报告")
        return BackupRestoreReport(
            backup_digest=self._snapshot.digest,
            appended_rows=self._appended_rows,
            counts_before=dict(self._snapshot.counts),
            counts_after_restore=self._shadow.counts(),
            integrity_ok=self._integrity_ok,
            logical_replay_duration_s=self._replay_duration_s,
            registered_issues=(
                REGISTERED_PHYSICAL_RESTORE,
                REGISTERED_OBJECT_STORE,
                REGISTERED_RPO_RTO,
            ),
        )

    # ---- 属性 ----------------------------------------------------------
    @property
    def shadow(self) -> ShadowStore:
        return self._shadow

    @property
    def snapshot(self) -> BackupSnapshot | None:
        return self._snapshot

    def appended_ids(self, kind: str) -> tuple[str, ...]:
        return tuple(self._appended_ids[kind])

    # ---- internals -----------------------------------------------------
    def _select(self, kind: str) -> list[dict[str, Any]]:
        """按 ``task_id`` 只读读取一类对象（IN 列表参数化，绝不整表扫描断言）。"""
        table = BACKUP_OBJECT_TABLES[kind]
        placeholders = ",".join(["%s"] * len(self._task_ids))
        order = BACKUP_ROW_ID_COLUMNS[kind]
        return list(
            self._client.fetch_all(
                f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY {order}",
                self._task_ids,
            )
        )

    def _verify_integrity(self, snapshot: BackupSnapshot, counts: Mapping[str, int]) -> bool:
        """AC-12：六类计数逐值相等 + seq 连续 + 链通过 + Evidence 引用可解析。"""
        if any(counts.get(kind) != snapshot.counts.get(kind) for kind in BACKUP_OBJECT_CLASSES):
            return False
        task_id = self._task_ids[0]
        events = self._tasks.list_events(task_id, limit=500)
        if [event.seq for event in events] != list(range(len(events))):
            return False
        try:
            verification = AuditService(self._tasks).verify(
                tenant_id=self._tenant_id, task_id=task_id
            )
        except Exception:  # noqa: BLE001 - 校验不可用即判"完整性未通过"，不得静默报绿
            return False
        if not verification.ok:
            return False
        evidence_ids = [
            *snapshot.row_ids("step_evidence"),
            *self._appended_ids["step_evidence"],
        ]
        if evidence_ids:
            missing = self._evidence.validate_task_references(
                task_id, self._tenant_id, evidence_ids
            )
            if missing:
                return False
        return True


def storage_topology_note() -> str:
    """对象存储边界的固定文案（W11-Q6：不编造路径、不实例化占位类）。"""
    return (
        f"{STORAGE_TOPOLOGY}；physical_restore={NOT_SUPPORTED}、"
        f"object_store={NOT_SUPPORTED}；"
        "证据：core/audit/worm.py 的 ObjectStorageArchiveSink 为占位类（构造即抛 "
        "NotImplementedError），LocalArchiveSink 全仓无装配点"
    )


__all__ = [
    "BACKUP_APPEND_N",
    "BACKUP_OBJECT_CLASSES",
    "BACKUP_OBJECT_TABLES",
    "BACKUP_ROWS_PER_CLASS",
    "BACKUP_ROW_ID_COLUMNS",
    "BACKUP_SNAPSHOT_VERSION",
    "BackupRestoreReport",
    "BackupSnapshot",
    "ControlledRestoreProcedure",
    "NOT_SUPPORTED",
    "RESTORE_MODE_LOGICAL_SHADOW",
    "REGISTERED_OBJECT_STORE",
    "REGISTERED_PHYSICAL_RESTORE",
    "REGISTERED_RPO_RTO",
    "SLA_NOTE",
    "STORAGE_TOPOLOGY",
    "ShadowStore",
    "TARGET_RPO_MINUTES",
    "TARGET_RTO_HOURS",
    "storage_topology_note",
]
