"""W10 T05 集成用例：AC-13 遥测 Evidence 真实落库 + W09 脱敏闸门②。

纪律（任务卡全局纪律 4、主理人裁定 C-2 / W10-Q2、设计 R-09）：

- 只依赖 ``control_client``（dg_ctl 控制账号）；**不依赖** ``DG_ADMIN_DSN``、
  **不依赖** PG 5432；控制库不可用时 skip 并写明原因，不判 FAIL、不冒充通过；
- 只做 ``INSERT`` 追加（新建 task / checkpoint / event / evidence 行），
  **不做清理、不 DELETE、不 UPDATE、不 TRUNCATE**；
- 只按**本任务 id / 本 evidence_id** 断言，不统计全表（``ENV-RESIDUE`` 140 行
  不重测、不清理）；``ENV-PG5432`` / ``ENV-1045`` / ``ENV-MODEL`` /
  ``ENV-NV1`` 如实登记为环境阻塞，本用例不修环境；
- Evidence 落库**必须经** ``EvidenceService.create``（内部 ``for_evidence``
  闸门②），禁止直接 INSERT evidence。

覆盖：AC-13 字段非空、``redaction_version`` 非空、``payload`` 只含白名单键、
禁用字段原文不入库、读回一致、W09 HIGH 质量拒绝口径未被绕过。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.benchmark.telemetry import (
    TELEMETRY_FIELD_ALLOWLIST,
    TELEMETRY_FORBIDDEN_FIELDS,
    AppTelemetryAdapter,
)
from core.errors import DBGuardianError, EvidenceInvalidError
from core.evidence.models import CollectionStatus, EvidenceQuality
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration

EXAMPLE_PATH = "benchmarks/w10/snapshot_manifest_example.json"
_RAW_SECRET = "13812345678"
TENANT = "tenant_local"


@pytest.fixture()
def control_db(control_client: Any) -> Any:
    """控制库可用性探针：不可达一律 skip（带原因），绝不判 FAIL。"""

    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(
            f"控制库不可用（{exc.code.value}）：{exc.message}；"
            "登记为 NOT_EXECUTED（ENV-1045 / ENV-RESIDUE），不冒充通过、不修环境"
        )
    return control_client


def _real_raw() -> dict[str, Any]:
    """从冻结快照 manifest 的字段派生合法 raw，并混入禁用字段。"""

    with open(EXAMPLE_PATH, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    raw: dict[str, Any] = {
        key: manifest[key]
        for key in (
            "source",
            "source_kind",
            "sampled_at",
            "window_start",
            "window_end",
            "exporter_version",
        )
    }
    raw.update(
        {
            "pool_connections": {"active": 8, "idle": 2},
            "pending_threads": 3,
            "acquire_timeout_total": 0,
            "acquire_latency_p95_ms": 12.5,
            "pool_max_size": 20,
            "application_error_rate": 0.01,
            # 禁用字段：必须被剔除，绝不落库。
            "request_body": f"SELECT * FROM users WHERE phone={_RAW_SECRET}",
            "user_id": f"u-{_RAW_SECRET}",
            "sql": "SELECT 1",
            "password": "pw-secret",
        }
    )
    return raw


def _payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, (str, bytes)) else dict(value)


# --------------------------------------------------------------------------- #
# AC-13 真实落库
# --------------------------------------------------------------------------- #


def test_ac13_telemetry_evidence_is_persisted_via_evidence_service(
    control_db: Any,
) -> None:
    """真实控制库：建 task → to_evidence_request → EvidenceService.create → 断言行。"""

    task_repository = TaskRepository(control_db)
    evidence_repository = EvidenceRepository(control_db)
    task_service = TaskService(task_repository, ToolRegistry())
    evidence_service = EvidenceService(evidence_repository, task_repository)

    # 只 INSERT：create_task 追加 task / event / checkpoint 行，不清理历史残留。
    task = task_service.create_task("W10 T05 遥测 Evidence 落库", tenant_id=TENANT)
    environment_id = task.environment_id

    adapter = AppTelemetryAdapter()
    snapshot = adapter.parse(_real_raw())
    assert set(snapshot.dropped_fields) == {
        "password", "request_body", "sql", "user_id"
    }
    request = adapter.to_evidence_request(
        snapshot, task=task, environment_id=environment_id
    )

    # 落库必经 W09 闸门②（EvidenceService.create 内部 for_evidence）。
    stored = evidence_service.create(request)

    # AC-13：字段均非空。
    assert stored.source_tool.startswith("app_telemetry:")
    assert stored.source_tool == "app_telemetry:hikaricp"
    assert stored.observed_at
    assert stored.time_window_start
    assert stored.time_window_end
    assert stored.adapter_version
    assert stored.db_version == "n/a"
    # 脱敏版本非空（W09 闸门②产物），不是 "none"。
    assert stored.redaction_version
    assert stored.redaction_version != "none"
    assert stored.redaction_version == "redaction-v2"
    assert stored.source_payload_hash
    assert stored.payload_hash

    # payload 只含白名单键（6 逻辑键 + 设计 §10.3 的 source/source_kind 身份键）。
    allowed_keys = set(TELEMETRY_FIELD_ALLOWLIST) | {"source", "source_kind"}
    assert set(stored.payload) == allowed_keys
    assert not (set(stored.payload) & set(TELEMETRY_FORBIDDEN_FIELDS))
    payload_text = json.dumps(stored.payload, ensure_ascii=False)
    redacted_text = json.dumps(stored.redacted_payload, ensure_ascii=False)
    for forbidden in TELEMETRY_FORBIDDEN_FIELDS:
        assert forbidden not in payload_text
        assert forbidden not in redacted_text
    assert _RAW_SECRET not in payload_text
    assert _RAW_SECRET not in redacted_text
    assert "SELECT * FROM users" not in payload_text

    # 真实读回（不是只改内存对象）。
    read_back = evidence_service.get(stored.evidence_id, task.tenant_id)
    assert read_back.source_tool == stored.source_tool
    assert read_back.redaction_version == stored.redaction_version
    assert read_back.payload == stored.payload

    # 直接按本 evidence_id 查真实数据库行（只按本任务 id 断言，不统计全表）。
    rows = control_db.fetch_all(
        "SELECT evidence_id, task_id, tenant_id, source_tool, adapter_version, "
        "db_version, observed_at, time_window_start, time_window_end, "
        "collection_status, quality, redaction_version, payload_json, "
        "redacted_payload_json FROM dg_system.evidence "
        "WHERE evidence_id = %s AND tenant_id = %s",
        (stored.evidence_id, task.tenant_id),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == task.task_id
    assert row["tenant_id"] == task.tenant_id
    assert row["source_tool"] == "app_telemetry:hikaricp"
    assert row["adapter_version"]
    assert row["db_version"] == "n/a"
    assert row["observed_at"] is not None
    assert row["time_window_start"] is not None
    assert row["time_window_end"] is not None
    assert row["redaction_version"] == "redaction-v2"
    assert row["quality"] != "HIGH"
    row_payload = _payload(row["payload_json"])
    assert set(row_payload) == allowed_keys
    assert not (set(row_payload) & set(TELEMETRY_FORBIDDEN_FIELDS))
    row_text = json.dumps(row_payload, ensure_ascii=False)
    assert _RAW_SECRET not in row_text
    if row["redacted_payload_json"] is not None:
        assert _RAW_SECRET not in json.dumps(
            _payload(row["redacted_payload_json"]), ensure_ascii=False
        )

    # 只按本任务 id 断言新增行数（R-09：不统计全表、不清理历史残留）。
    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 1


def test_w09_high_quality_guard_is_not_bypassed(
    control_db: Any,
) -> None:
    """弱采集状态 + HIGH 质量 → EvidenceService.create 必须拒绝、零新增行。

    ``EvidenceService._DEGRADED_STATUSES`` 的实测闭集是
    ``{DENIED, TIMEOUT, NOT_SUPPORTED}``（design §10.3 的「UNKNOWN 会拒」是
    口径性表述，实测集合不含 UNKNOWN）；本用例按**实测集合**取 DENIED 构造，
    同时单测侧已断言本适配器对 UNKNOWN 恒不产出 HIGH（AC-13）。
    """

    task_repository = TaskRepository(control_db)
    evidence_repository = EvidenceRepository(control_db)
    task_service = TaskService(task_repository, ToolRegistry())
    evidence_service = EvidenceService(evidence_repository, task_repository)

    task = task_service.create_task("W10 T05 HIGH 质量口径复核", tenant_id=TENANT)
    snapshot = AppTelemetryAdapter().parse(_real_raw())
    request = AppTelemetryAdapter().to_evidence_request(
        snapshot, task=task, environment_id=task.environment_id
    )
    # 正常产物绝不会是 HIGH（AC-13 的 quality 不得 HIGH）。
    assert request.quality is not EvidenceQuality.HIGH
    forged = request.model_copy(
        update={
            "collection_status": CollectionStatus.DENIED,
            "quality": EvidenceQuality.HIGH,
        }
    )
    with pytest.raises(EvidenceInvalidError):
        evidence_service.create(forged)
    # 拒绝后本任务下零新增 evidence 行（闸门在哈希/落库之前断言）。
    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 0
