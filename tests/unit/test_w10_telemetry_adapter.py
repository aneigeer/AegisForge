"""W10 T05 单测：最小 AppTelemetryAdapter（AC-11 / AC-12 / AC-13 + P1-W10-02）。

零 DB、零网络；不放宽断言、不加 skip/xfail。

覆盖：

- AC-11① 采集字段集合 == 恰好 6 个逻辑键；``pool_connections={active,idle}``；
- AC-11② 禁用字段被剔除并记入 ``dropped_fields``，不进 payload / Evidence；
- AC-12① ``raw is None`` → ``UNKNOWN`` + "应用池状态未确认" + 六项全 ``None``；
- AC-12② ``source`` 闭集 ``{hikaricp, pgbouncer}``，DB 连接数冒充 →
  ``ValidationError``；
- AC-13 ``to_evidence_request()`` 产出 ``EvidenceCreate``，字段非空、
  窗口时序、``UNKNOWN`` 时 ``quality`` 不得 ``HIGH``；
- 脱敏：直调 W09 ``gate.for_evidence``，断言 ``redaction_version`` 非空且
  行内无原文；
- P1-W10-02 ``SnapshotManifest`` 与 example JSON 字段一致、缺必填即
  ``ValidationError``、``snapshot_digest`` 由 ``canonical_digest`` 复核；
- 静态守护：无 SDK / exporter / 后台线程 / 直接 INSERT / 越界 import。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.telemetry import (
    APPLICATION_POOL_KIND,
    AppTelemetryAdapter,
    MISSING_POOL_MESSAGE,
    POOL_SOURCES,
    PoolConnections,
    SNAPSHOT_VERSION,
    SnapshotManifest,
    TELEMETRY_FIELD_ALLOWLIST,
    TELEMETRY_FORBIDDEN_FIELDS,
    TelemetrySnapshot,
    assert_window_order,
    compute_snapshot_digest,
)
from core.evidence.models import (
    CollectionStatus,
    EvidenceCreate,
    EvidenceQuality,
)
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.security.canonical import canonical_digest
from core.tasks.models import Task, new_id, utc_now

ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_MODULE = ROOT / "core" / "benchmark" / "telemetry.py"
EXAMPLE_PATH = ROOT / "benchmarks" / "w10" / "snapshot_manifest_example.json"

_RAW_SECRET = "13812345678"
SAMPLED_AT = "2026-03-01T00:01:00+00:00"
WINDOW_START = "2026-03-01T00:00:00+00:00"
WINDOW_END = "2026-03-01T00:01:00+00:00"
EXPORTER_VERSION = "hikaricp-exporter/1.4.2"


def _real_raw() -> dict[str, Any]:
    """完整合法快照 + 禁用字段（用于验证剔除与不越界）。"""

    return {
        "source": "hikaricp",
        "source_kind": APPLICATION_POOL_KIND,
        "sampled_at": SAMPLED_AT,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "exporter_version": EXPORTER_VERSION,
        "pool_connections": {"active": 8, "idle": 2},
        "pending_threads": 3,
        "acquire_timeout_total": 0,
        "acquire_latency_p95_ms": 12.5,
        "pool_max_size": 20,
        "application_error_rate": 0.01,
        # 禁用字段（含原文片段），必须被剔除且不进入任何 payload。
        "request_body": f"SELECT * FROM users WHERE phone={_RAW_SECRET}",
        "user_id": f"u-{_RAW_SECRET}",
        "sql": "SELECT 1",
        "password": "pw-secret",
        "dsn": "mysql://user:pass@host/db",
    }


def _task() -> Task:
    now = utc_now()
    return Task(
        task_id=new_id("task"),
        run_id=new_id("run"),
        goal="W10 T05 遥测 Evidence 单测",
        created_at=now,
        updated_at=now,
    )


def _example_payload() -> dict[str, Any]:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 常量 / 模型契约
# --------------------------------------------------------------------------- #


def test_allowlist_is_exactly_six_logical_keys() -> None:
    assert TELEMETRY_FIELD_ALLOWLIST == (
        "pool_connections",
        "pending_threads",
        "acquire_timeout_total",
        "acquire_latency_p95_ms",
        "pool_max_size",
        "application_error_rate",
    )
    assert len(TELEMETRY_FIELD_ALLOWLIST) == 6
    assert len(set(TELEMETRY_FIELD_ALLOWLIST)) == 6


def test_forbidden_fields_and_pool_sources_are_frozen() -> None:
    assert len(TELEMETRY_FORBIDDEN_FIELDS) == 10
    assert set(TELEMETRY_FORBIDDEN_FIELDS) == {
        "request_body", "user_id", "sql", "query_text", "session_user",
        "authorization", "cookie", "payload", "dsn", "password",
    }
    assert POOL_SOURCES == frozenset({"hikaricp", "pgbouncer"})
    assert MISSING_POOL_MESSAGE == "应用池状态未确认"
    assert SNAPSHOT_VERSION == "w10-snapshot-v1"


def test_telemetry_models_are_closed() -> None:
    with pytest.raises(ValidationError):
        PoolConnections.model_validate({"active": 1, "idle": 2, "total": 3})
    with pytest.raises(ValidationError):
        TelemetrySnapshot.model_validate(
            {
                "source": "hikaricp",
                "sampled_at": SAMPLED_AT,
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "exporter_version": EXPORTER_VERSION,
                "unexpected": 1,
            }
        )
    payload = _example_payload()
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(payload)


# --------------------------------------------------------------------------- #
# AC-11 白名单 / 禁用字段剔除
# --------------------------------------------------------------------------- #


def test_ac11_parse_output_has_exactly_six_metric_fields() -> None:
    snapshot = AppTelemetryAdapter().parse(_real_raw())
    assert snapshot.metric_fields == TELEMETRY_FIELD_ALLOWLIST
    assert set(snapshot.metric_payload()) == set(TELEMETRY_FIELD_ALLOWLIST)
    assert len(snapshot.metric_payload()) == 6
    assert snapshot.source == "hikaricp"
    assert snapshot.source_kind == "application_pool"
    assert snapshot.collection_status is CollectionStatus.SUCCESS
    assert snapshot.pool_connections == PoolConnections(active=8, idle=2)
    assert snapshot.metric_payload()["pool_connections"] == {"active": 8, "idle": 2}
    assert snapshot.pending_threads == 3
    assert snapshot.acquire_timeout_total == 0
    assert snapshot.acquire_latency_p95_ms == 12.5
    assert snapshot.pool_max_size == 20
    assert snapshot.application_error_rate == 0.01


def test_ac11_pool_connections_composite_key_is_validated() -> None:
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse(
            {**_real_raw(), "pool_connections": {"active": 8, "idle": 2, "waiting": 1}}
        )
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse(
            {**_real_raw(), "pool_connections": {"active": "eight", "idle": 2}}
        )


def test_ac11_forbidden_fields_are_dropped_and_recorded() -> None:
    raw = _real_raw()
    snapshot = AppTelemetryAdapter().parse(raw)
    present_forbidden = sorted(
        field for field in TELEMETRY_FORBIDDEN_FIELDS if field in raw
    )
    assert present_forbidden == ["dsn", "password", "request_body", "sql", "user_id"]
    assert set(snapshot.dropped_fields) == set(present_forbidden)
    # 禁用字段既不进采集字段集合，也不进 Evidence payload。
    assert not (set(snapshot.metric_payload()) & set(TELEMETRY_FORBIDDEN_FIELDS))
    request = AppTelemetryAdapter().to_evidence_request(
        snapshot, task=_task(), environment_id="env_mysql_local"
    )
    assert not (set(request.payload) & set(TELEMETRY_FORBIDDEN_FIELDS))
    serialized = json.dumps(request.payload, ensure_ascii=False)
    for forbidden in TELEMETRY_FORBIDDEN_FIELDS:
        assert forbidden not in serialized
    assert _RAW_SECRET not in serialized
    assert "SELECT * FROM users" not in serialized


def test_ac11_dropped_fields_are_sorted_and_deterministic() -> None:
    first = AppTelemetryAdapter().parse(_real_raw())
    second = AppTelemetryAdapter().parse(_real_raw())
    assert first.dropped_fields == second.dropped_fields
    assert list(first.dropped_fields) == sorted(first.dropped_fields)


def test_partial_snapshot_is_not_success_and_stays_honest() -> None:
    raw = _real_raw()
    raw.pop("pending_threads")
    raw.pop("pool_max_size")
    snapshot = AppTelemetryAdapter().parse(raw)
    assert snapshot.collection_status is CollectionStatus.PARTIAL
    assert snapshot.pending_threads is None
    assert snapshot.pool_max_size is None
    assert snapshot.message is None  # 有快照（虽不完整），不冒充「状态未确认」


# --------------------------------------------------------------------------- #
# AC-12 池状态缺失不冒充 / source 闭集
# --------------------------------------------------------------------------- #


def test_ac12_none_raw_is_unknown_with_six_none_metrics() -> None:
    snapshot = AppTelemetryAdapter().parse(None)
    assert snapshot.collection_status is CollectionStatus.UNKNOWN
    assert snapshot.message == MISSING_POOL_MESSAGE
    assert snapshot.metric_payload() == {
        "pool_connections": None,
        "pending_threads": None,
        "acquire_timeout_total": None,
        "acquire_latency_p95_ms": None,
        "pool_max_size": None,
        "application_error_rate": None,
    }
    for value in snapshot.metric_payload().values():
        assert value is None
        assert value != 0 and value != 0.0  # 不填 0 / 0.0 冒充「池空闲」
    assert snapshot.dropped_fields == ()
    assert snapshot.source in POOL_SOURCES


@pytest.mark.parametrize(
    "bad_source",
    ["mysql_server", "pg_stat_activity", "database_server", "Threads_connected", ""],
)
def test_ac12_database_connection_count_impersonation_is_rejected(
    bad_source: str,
) -> None:
    raw = {**_real_raw(), "source": bad_source}
    raw["threads_connected"] = 500  # 典型 DB 连接数冒充键
    with pytest.raises(ValidationError) as caught:
        AppTelemetryAdapter().parse(raw)
    assert "source" in str(caught.value)


def test_ac12_source_kind_outside_application_pool_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse({**_real_raw(), "source_kind": "database_server"})


def test_ac12_pgbouncer_is_an_allowed_source() -> None:
    snapshot = AppTelemetryAdapter().parse({**_real_raw(), "source": "pgbouncer"})
    assert snapshot.source == "pgbouncer"
    assert snapshot.collection_status is CollectionStatus.SUCCESS


def test_missing_required_metadata_is_fail_closed() -> None:
    raw = _real_raw()
    raw.pop("sampled_at")
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse(raw)
    raw2 = _real_raw()
    raw2.pop("source")
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse(raw2)


def test_window_order_helper_is_strict() -> None:
    assert_window_order(WINDOW_START, WINDOW_END, SAMPLED_AT)
    with pytest.raises(ValueError):
        assert_window_order(WINDOW_END, WINDOW_START, SAMPLED_AT)  # start >= end
    with pytest.raises(ValueError):
        assert_window_order(WINDOW_START, "2026-03-01T00:02:00+00:00", SAMPLED_AT)
    with pytest.raises(ValueError):
        assert_window_order("not-a-time", WINDOW_END, SAMPLED_AT)


def test_real_snapshot_with_bad_window_order_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppTelemetryAdapter().parse(
            {**_real_raw(), "window_start": WINDOW_END, "window_end": WINDOW_START}
        )


# --------------------------------------------------------------------------- #
# AC-13 → EvidenceCreate 字段
# --------------------------------------------------------------------------- #


def test_ac13_to_evidence_request_produces_well_formed_evidence_create() -> None:
    snapshot = AppTelemetryAdapter().parse(_real_raw())
    task = _task()
    request = AppTelemetryAdapter().to_evidence_request(
        snapshot, task=task, environment_id="env_mysql_local"
    )
    assert isinstance(request, EvidenceCreate)
    assert request.tenant_id == task.tenant_id
    assert request.task_id == task.task_id
    assert request.environment_id == "env_mysql_local"
    assert request.source_tool == "app_telemetry:hikaricp"
    assert request.source_tool.startswith("app_telemetry:")
    assert request.adapter_version == snapshot.exporter_version == EXPORTER_VERSION
    assert request.db_version == "n/a"  # 应用池不是数据库，不填版本冒充
    assert request.db_engine == "n/a"
    assert request.observed_at == snapshot.sampled_at
    assert request.time_window_start == snapshot.window_start
    assert request.time_window_end == snapshot.window_end
    assert request.collection_status is snapshot.collection_status
    assert request.quality is EvidenceQuality.MEDIUM
    assert request.quality is not EvidenceQuality.HIGH
    assert request.observed_at and request.time_window_start and request.time_window_end
    assert_window_order(
        request.time_window_start, request.time_window_end, request.observed_at
    )
    # payload 只有 6 个逻辑键 + source/source_kind（设计 §10.3）。
    assert set(request.payload) == set(TELEMETRY_FIELD_ALLOWLIST) | {
        "source",
        "source_kind",
    }
    assert request.payload["source"] == "hikaricp"
    assert request.payload["source_kind"] == "application_pool"


def test_ac13_unknown_collection_status_never_uses_high_quality() -> None:
    snapshot = AppTelemetryAdapter().parse(None)
    request = AppTelemetryAdapter().to_evidence_request(
        snapshot, task=_task(), environment_id="env_mysql_local"
    )
    assert request.collection_status is CollectionStatus.UNKNOWN
    assert request.quality is not EvidenceQuality.HIGH
    assert request.quality is EvidenceQuality.UNKNOWN
    assert request.completeness.value == "MISSING"
    assert request.source_tool.startswith("app_telemetry:")


def test_ac13_w09_for_evidence_sets_redaction_version_and_contains_no_raw() -> None:
    """AC-13 脱敏：单测可直调 ``gate.for_evidence``（W09 闸门②）。"""

    snapshot = AppTelemetryAdapter().parse(_real_raw())
    task = _task()
    request = AppTelemetryAdapter(gate=RedactionGate(DataRedactionEngine())).to_evidence_request(
        snapshot, task=task, environment_id="env_mysql_local"
    )
    gate = RedactionGate(DataRedactionEngine())
    redacted = gate.for_evidence(
        request, tenant_id=task.tenant_id, task_id=task.task_id
    )
    assert redacted.redaction_version  # 非空
    assert redacted.redaction_version != "none"
    assert redacted.redaction_version == "redaction-v2"
    assert redacted.source_payload_hash
    payload_text = json.dumps(redacted.payload, ensure_ascii=False)
    redacted_text = json.dumps(redacted.redacted_payload, ensure_ascii=False)
    # 禁用字段的原文片段绝不出现在任何载荷里。
    for forbidden in TELEMETRY_FORBIDDEN_FIELDS:
        assert forbidden not in payload_text
        assert forbidden not in redacted_text
    assert _RAW_SECRET not in payload_text
    assert _RAW_SECRET not in redacted_text
    assert "SELECT * FROM users" not in payload_text


# --------------------------------------------------------------------------- #
# P1-W10-02 SnapshotManifest 冻结字段
# --------------------------------------------------------------------------- #


def test_snapshot_manifest_example_matches_model_fields() -> None:
    payload = _example_payload()
    manifest = SnapshotManifest.model_validate(payload)
    assert set(payload) == set(SnapshotManifest.model_fields)
    assert manifest.snapshot_version == SNAPSHOT_VERSION
    assert manifest.source in POOL_SOURCES
    assert manifest.source_kind == "application_pool"
    assert manifest.snapshot_digest == compute_snapshot_digest(payload)
    assert manifest.snapshot_digest == canonical_digest(manifest.digest_payload())
    assert manifest.notes
    assert_window_order(
        manifest.window_start, manifest.window_end, manifest.sampled_at
    )


@pytest.mark.parametrize("missing", sorted(SnapshotManifest.model_fields))
def test_snapshot_manifest_missing_any_required_field_raises(missing: str) -> None:
    payload = _example_payload()
    payload.pop(missing)
    with pytest.raises(ValidationError) as caught:
        SnapshotManifest.model_validate(payload)
    assert missing in str(caught.value)


def test_snapshot_manifest_digest_tamper_is_detected() -> None:
    payload = _example_payload()
    payload["exporter_version"] = "hikaricp-exporter/9.9.9"  # 不重算 digest
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(payload)
    payload = _example_payload()
    payload["snapshot_digest"] = "0" * 64
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(payload)


def test_snapshot_manifest_window_and_source_are_validated() -> None:
    payload = _example_payload()
    payload["window_end"] = payload["window_start"]
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(payload)
    payload = _example_payload()
    payload["source"] = "pg_stat_activity"
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(payload)


def test_compute_snapshot_digest_is_content_addressed() -> None:
    payload = _example_payload()
    first = compute_snapshot_digest(payload)
    assert first == compute_snapshot_digest(dict(payload))
    changed = dict(payload)
    changed["notes"] = [*payload["notes"], "追加一条备注"]
    assert compute_snapshot_digest(changed) != first


# --------------------------------------------------------------------------- #
# 静态守护：无 SDK / 无 exporter / 无后台线程 / 无直接 INSERT / 依赖方向
# --------------------------------------------------------------------------- #


def test_telemetry_module_has_no_sdk_exporter_thread_or_direct_insert() -> None:
    source = TELEMETRY_MODULE.read_text(encoding="utf-8")
    # 与 T03 同款：禁用实现检查落在**真实 import 节点**上，
    # docstring 里「禁止引入 X」的措辞不算依赖。
    banned = {
        "prometheus_client",
        "psutil",
        "apscheduler",
        "threading",
        "socket",
        "subprocess",
        "numpy",
        "scipy",
        "apps",
        "workers",
    }
    modules: set[str] = set()
    forbidden_defs: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            forbidden_defs.add(node.name)
    assert not (modules & banned), modules & banned
    for forbidden in ("core.gateway", "core.approval", "core.agent"):
        assert not any(
            module == forbidden or module.startswith(forbidden + ".")
            for module in modules
        ), forbidden
    # 不重写复用点、不自建闸门、不直接写库。
    assert not (
        forbidden_defs & {"percentile", "canonical_digest", "RedactionGate", "for_evidence"}
    )
    assert "INSERT INTO" not in source
    assert "def percentile(" not in source
    assert "from core.evidence.models import" in source
    assert "from core.tasks.models import Task" in source
    assert "from core.security.canonical import canonical_digest" in source
