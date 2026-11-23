"""W09 T02 集成用例：全链路脱敏管道（证据持久化 → 审计 → 隔离区）。

风格与 `tests/integration/test_evidence_service.py` 一致：真实控制库、真实仓储。

**数据库不可用时必须 skipped 并带原因**（例如本机 `DG_ADMIN_DSN` 报 1045，
或控制库未配置）。本文件因此：

- 不依赖 `admin_client`（它需要管理员账号做清理），也不执行任何 DELETE；
- 只断言**本用例自己新建任务**的范围内事实（`count_for_task`），
  不断言"全表为空"（全局纪律）。

覆盖：AC-01（原串不残留）、AC-05（BLOCKED 零新增行）、AC-08（证据行版本与事件
版本相等）、R3（隔离区只存哈希/类别/路径，无原文）、A2（`source_payload_hash`
不覆盖 `payload_hash`）。
"""

from __future__ import annotations

import json

import pytest

from core.audit.service import AuditService
from core.diagnosis.evidence_mapping import to_evidence_create
from core.errors import (
    DBGuardianError,
    RedactionBlockedError,
    RedactionReviewRequiredError,
)
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService, canonical_payload_hash
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.quarantine import MysqlQuarantineRepository
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolResult, ToolSource, ToolStatus
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

_OBSERVED = "2026-03-01T00:00:00+00:00"
PHONE_RAW = "13812345678"
NAME_RAW = "张三"
CONNECTION_RAW = "mysql://user:pass@host.internal:3306/db"


@pytest.fixture
def control_db(control_client) -> TaskRepository:
    """控制库可用性探针：不可用一律 skip（带原因），绝不 failed。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return TaskRepository(control_client)


@pytest.fixture
def task_service(control_db: TaskRepository) -> TaskService:
    return TaskService(control_db, ToolRegistry())


@pytest.fixture
def evidence_repository(control_client) -> EvidenceRepository:
    return EvidenceRepository(control_client)


@pytest.fixture
def evidence_service(
    control_client,
    control_db: TaskRepository,
    evidence_repository: EvidenceRepository,
) -> EvidenceService:
    """组合根形态：闸门带审计与隔离区落点（生产装配同款）。"""
    gate = RedactionGate(
        DataRedactionEngine(),
        audit=AuditService(control_db),
        quarantine=MysqlQuarantineRepository(control_client),
    )
    return EvidenceService(
        evidence_repository, control_db, redaction_gate=gate
    )


def _tool_result(data: dict, *, tool: str = "get_locks") -> ToolResult:
    return ToolResult(
        tool_name=tool,
        status=ToolStatus.SUCCESS,
        started_at=_OBSERVED,
        duration_ms=3.0,
        data=data,
        warnings=[],
        source=ToolSource(
            database_id="lab_mysql_01", db_version="8.0.46", collected_at=_OBSERVED
        ),
        observed_at=_OBSERVED,
        capability_ref="mysql:1.0:integration",
        source_metadata={"adapter": "w09-integration"},
    )


def _events_of(control_db: TaskRepository, task_id: str, event_type: str) -> list:
    return [
        event
        for event in control_db.list_events(task_id, limit=200)
        if event.event_type == event_type
    ]


# --------------------------------------------------------------------------- #
# AC-01 / AC-08：真脱敏落库 + 证据行版本与事件版本相等
# --------------------------------------------------------------------------- #


def test_redacted_evidence_is_persisted_with_matching_audit_version(
    evidence_service: EvidenceService,
    evidence_repository: EvidenceRepository,
    task_service: TaskService,
    control_db: TaskRepository,
) -> None:
    task = task_service.create_task("W09 脱敏管道-证据落库")
    request = to_evidence_create(
        _tool_result({"phone": PHONE_RAW, "rows": [{"id": 1}]}),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        environment_id=task.environment_id,
        gate=evidence_service.redaction_gate,
    )

    stored = evidence_service.create(request)

    # AC-01：原串不落库
    assert PHONE_RAW not in json.dumps(stored.payload, ensure_ascii=False)
    assert PHONE_RAW not in json.dumps(stored.redacted_payload, ensure_ascii=False)
    # A2：payload_hash 仍是"写入载荷哈希"，原文锚点单独一列，二者不互相覆盖
    assert stored.payload_hash == canonical_payload_hash(stored.payload)
    assert stored.source_payload_hash == request.source_payload_hash
    assert stored.redaction_version == "redaction-v2"
    # 读回一致（真落库，不是只改了内存对象）
    read_back = evidence_service.get(stored.evidence_id, task.tenant_id)
    assert read_back.source_payload_hash == stored.source_payload_hash
    assert read_back.redaction_version == stored.redaction_version

    # AC-08：证据行 redaction_version 与 REDACTION_APPLIED 事件相等
    applied = _events_of(control_db, task.task_id, "REDACTION_APPLIED")
    assert applied, "缺少 REDACTION_APPLIED 事件"
    payload = applied[-1].payload
    assert payload["redaction_version"] == stored.redaction_version
    assert payload["source_payload_hash"] == stored.source_payload_hash
    assert payload["status"] in ("CLEAN", "REDACTED")
    # 事件里不得带原文
    assert PHONE_RAW not in json.dumps(payload, ensure_ascii=False)
    # 本用例只新增 1 行（不依赖"全表为空"的全局断言）
    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 1
    # A2：原文锚点是另一个值，不能与写入载荷哈希同格
    assert stored.source_payload_hash != stored.payload_hash


# --------------------------------------------------------------------------- #
# AC-05：BLOCKED 零新增行
# --------------------------------------------------------------------------- #


def test_blocked_payload_raises_and_writes_zero_rows(
    evidence_service: EvidenceService,
    evidence_repository: EvidenceRepository,
    task_service: TaskService,
    control_db: TaskRepository,
) -> None:
    task = task_service.create_task("W09 脱敏管道-BLOCKED")

    with pytest.raises(RedactionBlockedError):
        evidence_service.create(
            to_evidence_create(
                _tool_result({"target": CONNECTION_RAW}),
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                environment_id=task.environment_id,
                gate=evidence_service.redaction_gate,
            )
        )

    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 0
    blocked = _events_of(control_db, task.task_id, "REDACTION_BLOCKED")
    assert blocked, "缺少 REDACTION_BLOCKED 事件"
    assert CONNECTION_RAW not in json.dumps(blocked[-1].payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# R3：REVIEW_REQUIRED → 隔离区只存哈希/类别/路径，零新增证据行
# --------------------------------------------------------------------------- #


def test_review_required_writes_quarantine_without_raw_content(
    evidence_service: EvidenceService,
    evidence_repository: EvidenceRepository,
    task_service: TaskService,
    control_client,
    control_db: TaskRepository,
) -> None:
    task = task_service.create_task("W09 脱敏管道-隔离区")

    with pytest.raises(RedactionReviewRequiredError):
        evidence_service.create(
            to_evidence_create(
                _tool_result({"name": NAME_RAW}),
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                environment_id=task.environment_id,
                gate=evidence_service.redaction_gate,
            )
        )

    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 0

    rows = control_client.fetch_all(
        "SELECT quarantine_id, tenant_id, task_id, channel, status, payload_hash, "
        "detected_categories, unresolved_spans_json, blockers, redaction_version "
        "FROM dg_system.redaction_quarantine WHERE task_id = %s",
        (task.task_id,),
    )
    assert rows, "REVIEW_REQUIRED 必须写隔离区（不得静默跳过）"
    row = rows[0]
    assert row["status"] == "REVIEW_REQUIRED"
    assert row["channel"] == "EVIDENCE"
    assert row["redaction_version"] == "redaction-v2"
    assert len(str(row["payload_hash"])) == 64
    # 隔离区只存定位信息：任何字段都不得出现原串
    serialized = json.dumps(
        {key: str(value) for key, value in row.items()}, ensure_ascii=False
    )
    assert NAME_RAW not in serialized
    spans = json.loads(str(row["unresolved_spans_json"]))
    assert spans and all(
        set(span) == {"category", "json_path", "start", "end", "reason"}
        for span in spans
    )
    # 事件侧同样不得静默：REVIEW_REQUIRED 有事件
    review = _events_of(control_db, task.task_id, "REDACTION_REVIEW_REQUIRED")
    assert review, "缺少 REDACTION_REVIEW_REQUIRED 事件"
    assert "quarantine_skipped" not in review[-1].payload


# --------------------------------------------------------------------------- #
# W08 冻结口径：CLEAN 载荷哈希不变量
# --------------------------------------------------------------------------- #


def test_clean_payload_hash_invariant_is_unchanged(
    evidence_service: EvidenceService, task_service: TaskService
) -> None:
    task = task_service.create_task("W09 脱敏管道-哈希不变量")
    request = to_evidence_create(
        _tool_result({"rows": [{"id": 1}], "nested": {"b": 2, "a": 1}}),
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        environment_id=task.environment_id,
    )

    stored = evidence_service.create(request)

    assert stored.payload == request.payload
    assert stored.payload_hash == canonical_payload_hash(request.payload)
    # 无敏感命中时原文哈希与写入哈希同值（同一份内容），但仍各占一格。
    assert stored.source_payload_hash == canonical_payload_hash(request.payload)
    assert stored.redaction_version == "redaction-v2"
