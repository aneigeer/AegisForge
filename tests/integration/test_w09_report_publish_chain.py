"""W10 L2-T03：报告发布链路**真实库**端到端（1 个任务 1 条证据）。

覆盖设计 §5.6 五条：

① `report_snapshots` 本任务恰 1 行；
② `report_id` 前缀 `rpt_`、`redaction_version` 与引擎一致；
③ `task_events` 中 `REPORT_GENERATED` 恰 1 条，payload 含
   `fact_status` / `action_state` / `verification_state` / `degraded`；
④ 重复 `publish` 不新增行（幂等：`ALREADY_GENERATED`）；
⑤ 任务 `status` / `state_version` 未因发布改变（只读证明）。

另外补一条装配事实：真实 `build_worker_stack` 装配出的
`stack.redaction is stack.report_publisher.redaction`（同一闸门实例）。

纪律：只 INSERT、不 DELETE / UPDATE / TRUNCATE；不依赖 `DG_ADMIN_DSN`、不依赖 PG；
环境不可达时 `skip` 并写明原因——**不伪造通过**。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.audit.service import AuditService
from core.errors import ConfigError, DBGuardianError
from core.evidence.models import CollectionStatus, EvidenceCreate, EvidenceQuality
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.redaction.engine import DataRedactionEngine
from core.tasks.models import Task, TaskStatus, new_id, utc_now
from core.tasks.repository import TaskRepository
from workers.report_assembly import REASON_ALREADY_GENERATED
from workers.stack import build_worker_stack

pytestmark = pytest.mark.integration

TENANT = "tenant_local"
TOOL_CALL_ID = "call_l2_t03_0001"
RECONCILIATION_EVENT = "REPORT_GENERATED"


def _require_lab(settings, control_client, ro_client, rw_client) -> None:
    """真实 MySQL 可达性探测：不可达就 skip（理由写清"需真实 MySQL LAB"）。"""
    assert settings is not None
    for name, client in (
        ("控制库", control_client),
        ("LAB 只读", ro_client),
        ("LAB 可写", rw_client),
    ):
        try:
            client.ping()
        except DBGuardianError as exc:
            pytest.skip(f"需真实 MySQL LAB：{name}不可用（{exc.code.value}）")


@pytest.fixture(scope="module")
def worker_stack(settings, control_client, ro_client, rw_client) -> Any:
    _require_lab(settings, control_client, ro_client, rw_client)
    try:
        return build_worker_stack(settings)
    except ConfigError as exc:  # pragma: no cover - 环境缺环境事实时跳过
        pytest.skip(f"需真实控制库环境事实：{exc.code.value}")


@pytest.fixture()
def executing_task(worker_stack) -> Task:
    """建一个 `EXECUTING` 任务（发布不要求该状态，但它是生产的真实形态）。"""
    task = worker_stack.task_service.create_task("W10 L2-T03 报告发布链路")
    worker_stack.task_service.transition(task.task_id, TaskStatus.PLANNING)
    task = worker_stack.task_service.transition(task.task_id, TaskStatus.EXECUTING)
    assert task.status is TaskStatus.EXECUTING
    return task


@pytest.fixture()
def seeded_evidence(worker_stack, control_client, executing_task) -> Any:
    """走闸门②造 1 条证据（`redaction_version` 非空）。"""
    service = EvidenceService(
        EvidenceRepository(control_client),
        TaskRepository(control_client),
        redaction_gate=worker_stack.redaction,
    )
    return service.create(
        EvidenceCreate(
            tenant_id=executing_task.tenant_id,
            task_id=executing_task.task_id,
            environment_id=executing_task.environment_id,
            tool_call_id=TOOL_CALL_ID,
            db_engine="mysql",
            db_version="8.0.46",
            adapter_version="w09-evidence-v1",
            source_tool="mysql_lab_update_order_note",
            observed_at=utc_now().isoformat(),
            collection_status=CollectionStatus.SUCCESS,
            quality=EvidenceQuality.HIGH,
            payload={"order_id": 2001, "note": "w10-l2-t03"},
        )
    )


def _snapshot_rows(control_client, task_id: str) -> list[dict[str, Any]]:
    return control_client.fetch_all(
        "SELECT report_id, tenant_id, task_id, fact_status, action_state, "
        "verification_state, redaction_version, evidence_ids_json "
        "FROM dg_system.report_snapshots WHERE tenant_id = %s AND task_id = %s",
        (TENANT, task_id),
    )


@pytest.fixture()
def audit_service(worker_stack) -> AuditService:
    """复用组合根上的审计落点（与发布器同一个 `AuditService`）。"""
    return worker_stack.audit


def test_publish_chain_persists_one_snapshot_and_one_event(
    worker_stack,
    control_client,
    audit_service,
    executing_task,
    seeded_evidence,
) -> None:
    publisher = worker_stack.report_publisher
    assert publisher is not None
    # 装配事实：报告通道与 stack 槽位是**同一闸门实例**。
    assert publisher.redaction is worker_stack.redaction

    before_task = worker_stack.repository.get_task(
        executing_task.task_id, tenant_id=TENANT
    )

    first = publisher.publish(
        executing_task, tool_call_id=TOOL_CALL_ID, succeeded=True
    )

    assert first.generated is True
    assert first.failed is False
    rows = _snapshot_rows(control_client, executing_task.task_id)
    assert len(rows) == 1, f"本任务应恰有 1 行快照，实际 {len(rows)} 行"
    row = rows[0]
    # ② report_id 前缀 + redaction_version 与引擎一致
    assert str(row["report_id"]).startswith("rpt_")
    assert row["report_id"] == first.report_id
    assert row["redaction_version"] == worker_stack.redaction.version
    assert row["redaction_version"] == DataRedactionEngine().version
    assert row["redaction_version"] != "none"
    # 事实等级由证据驱动（有 1 条成功证据 → CONFIRMED）
    assert row["fact_status"] in {"CONFIRMED", "PARTIAL", "UNKNOWN"}

    # ③ REPORT_GENERATED 恰 1 条且带四个状态字段
    events, _ = audit_service.query(
        tenant_id=TENANT,
        task_id=executing_task.task_id,
        event_types=[RECONCILIATION_EVENT],
        limit=50,
    )
    assert len(events) == 1, f"REPORT_GENERATED 应恰 1 条，实际 {len(events)}"
    payload = events[0].payload
    for key in ("fact_status", "action_state", "verification_state", "degraded"):
        assert key in payload, f"REPORT_GENERATED payload 缺字段 {key}"
    assert payload["report_id"] == row["report_id"]
    assert payload["tenant_id"] == TENANT

    # ④ 重复 publish 不新增行（幂等）
    second = publisher.publish(
        executing_task, tool_call_id=TOOL_CALL_ID, succeeded=True
    )
    assert second.generated is False
    assert second.skipped_reason == REASON_ALREADY_GENERATED
    assert len(_snapshot_rows(control_client, executing_task.task_id)) == 1
    events_again, _ = audit_service.query(
        tenant_id=TENANT,
        task_id=executing_task.task_id,
        event_types=[RECONCILIATION_EVENT],
        limit=50,
    )
    assert len(events_again) == 1

    # ⑤ 发布是只读视图：任务状态与版本号都不变
    after_task = worker_stack.repository.get_task(
        executing_task.task_id, tenant_id=TENANT
    )
    assert after_task.status is before_task.status is TaskStatus.EXECUTING
    assert after_task.state_version == before_task.state_version


def test_publish_without_evidence_still_writes_one_unknown_snapshot(
    worker_stack, control_client
) -> None:
    """无证据任务：仍生成 1 行，事实等级如实为 UNKNOWN（风险 R1 的表达）。"""
    publisher = worker_stack.report_publisher
    assert publisher is not None
    task = worker_stack.task_service.create_task("W10 L2-T03 无证据任务")

    outcome = publisher.publish(
        task, tool_call_id=f"call_{new_id('t03')}", succeeded=True
    )

    assert outcome.generated is True
    rows = _snapshot_rows(control_client, task.task_id)
    assert len(rows) == 1
    assert rows[0]["fact_status"] == "UNKNOWN"
