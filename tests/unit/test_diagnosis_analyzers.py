"""W04 单元测试：Evidence 映射与三类诊断分析器（不连接数据库）。"""

from __future__ import annotations

import json

import pytest

from core.diagnosis.analyzers import (
    APP_POOL_MISSING,
    THRESHOLD_DISCLAIMER,
    analyze_connection_pressure,
    analyze_lock_wait,
    analyze_scenario,
    analyze_slow_query,
)
from core.diagnosis.evidence_mapping import REDACTION_VERSION, to_evidence_create
from core.diagnosis.models import ConfidenceBand, HypothesisStatus
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceQuality,
)
from core.faults.models import FaultScenario
from core.tools.contracts import ToolResult, ToolSource, ToolStatus

_TENANT = "tenant_unit"
_TASK = "task_unit"
_ENV = "env_unit"
_OBSERVED = "2026-11-08T00:00:00+00:00"
_WATCH_START = "2026-11-07T23:59:59+00:00"


def _evidence(
    tool: str,
    data: dict,
    *,
    status: CollectionStatus = CollectionStatus.SUCCESS,
    quality: EvidenceQuality = EvidenceQuality.HIGH,
    completeness: EvidenceCompleteness = EvidenceCompleteness.COMPLETE,
    engine: str = "mysql",
    warnings: list[str] | None = None,
    evidence_id: str | None = None,
) -> Evidence:
    payload = {
        "tool_status": status.value,
        "data": data,
        "warnings": warnings or [],
        "capability_ref": f"{engine}:1.0:unit",
        "source_metadata": {"adapter": "unit"},
        "truncated": completeness is EvidenceCompleteness.PARTIAL,
        "error": None,
    }
    return Evidence(
        evidence_id=evidence_id or f"ev_{tool}",
        tenant_id=_TENANT,
        task_id=_TASK,
        environment_id=_ENV,
        db_engine=engine,
        db_version="8.0.46" if engine == "mysql" else "12.18",
        adapter_version="1.0",
        source_tool=tool,
        observed_at=_OBSERVED,
        time_window_start=_WATCH_START,
        time_window_end=_OBSERVED,
        collection_status=status,
        quality=quality,
        completeness=completeness,
        payload_hash="a" * 64,
        redaction_version=REDACTION_VERSION,
        reproducible=True,
        payload=payload,
    )


def _tool_result(
    tool: str,
    *,
    status: ToolStatus = ToolStatus.SUCCESS,
    data: dict | None = None,
    observed_at: str | None = _OBSERVED,
    truncated: bool = False,
    warnings: list[str] | None = None,
    engine: str = "mysql",
) -> ToolResult:
    return ToolResult(
        tool_name=tool,
        status=status,
        started_at=_WATCH_START,
        duration_ms=12.5,
        data=data or {},
        warnings=warnings or [],
        source=ToolSource(
            database_id="lab_unit",
            engine=engine,  # type: ignore[arg-type]
            db_version="8.0.46" if engine == "mysql" else "12.18",
            collected_at=_OBSERVED,
            adapter_version="1.0",
        ),
        observed_at=observed_at,
        truncated=truncated,
        capability_ref=f"{engine}:1.0:unit",
        source_metadata={"adapter": "unit"},
    )


# --------------------------------------------------------------------------- #
# Evidence 映射
# --------------------------------------------------------------------------- #


def test_evidence_mapping_success_has_explicit_window_and_high_quality() -> None:
    result = _tool_result(
        "explain_query",
        data={"plan": {"query_block": {"table": {"access_type": "ALL"}}}},
        warnings=["execution plan available"],
    )

    request = to_evidence_create(
        result, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.tenant_id == _TENANT
    assert request.task_id == _TASK
    assert request.environment_id == _ENV
    assert request.source_tool == "explain_query"
    assert request.db_engine == "mysql"
    assert request.db_version == "8.0.46"
    assert request.adapter_version == "1.0"
    assert request.observed_at == _OBSERVED
    assert request.time_window_start == _WATCH_START
    assert request.time_window_end == _OBSERVED
    assert request.collection_status is CollectionStatus.SUCCESS
    assert request.quality is EvidenceQuality.HIGH
    assert request.completeness is EvidenceCompleteness.COMPLETE
    assert request.payload["data"] == result.data
    assert request.payload["warnings"] == ["execution plan available"]
    assert request.payload["capability_ref"] == "mysql:1.0:unit"
    assert request.payload["source_metadata"] == {"adapter": "unit"}
    assert request.redacted_payload == request.payload
    assert request.redaction_version == REDACTION_VERSION
    assert request.reproducible is True
    assert request.tool_call_id is None


def test_evidence_mapping_truncated_success_is_degraded() -> None:
    result = _tool_result("get_table_stats", data={"tables": []}, truncated=True)

    request = to_evidence_create(
        result, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.collection_status is CollectionStatus.SUCCESS
    assert request.quality is EvidenceQuality.MEDIUM
    assert request.completeness is EvidenceCompleteness.PARTIAL


def test_evidence_mapping_not_supported_is_unknown_and_missing() -> None:
    result = _tool_result(
        "get_query_stats",
        status=ToolStatus.NOT_SUPPORTED,
        data={"available": False, "statements": []},
        warnings=["extension not installed"],
    )

    request = to_evidence_create(
        result, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.collection_status is CollectionStatus.NOT_SUPPORTED
    assert request.quality is EvidenceQuality.UNKNOWN
    assert request.completeness is EvidenceCompleteness.MISSING


def test_evidence_mapping_partial_is_low() -> None:
    result = _tool_result(
        "get_connection_snapshot",
        status=ToolStatus.PARTIAL,
        data={"sessions": [{"pid": 1}]},
        warnings=["truncated"],
    )

    request = to_evidence_create(
        result, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.collection_status is CollectionStatus.PARTIAL
    assert request.quality is EvidenceQuality.LOW
    assert request.completeness is EvidenceCompleteness.PARTIAL


# --------------------------------------------------------------------------- #
# 慢查询
# --------------------------------------------------------------------------- #


def _slow_query_support_evidence() -> list[Evidence]:
    explain = _evidence(
        "explain_query",
        {
            "plan": {
                "query_block": {
                    "table": {
                        "table_name": "fault_slow_query_4101",
                        "access_type": "ALL",
                        "rows_examined_per_scan": 20000,
                    }
                }
            }
        },
        evidence_id="ev_explain_support",
    )
    stats = _evidence(
        "get_query_stats",
        {
            "statements": [
                {
                    "digest": "digest-1",
                    "digest_text": "SELECT COUNT(*) FROM fault_slow_query_4101",
                    "count_star": 3,
                    "total_time_ms": 12000,
                    "rows_examined": 60000,
                    "no_index_used": 3,
                }
            ]
        },
        evidence_id="ev_stats_support",
    )
    tables = _evidence(
        "get_table_stats",
        {
            "tables": [
                {
                    "table_name": "fault_slow_query_4101",
                    "estimated_rows": 20000,
                    "index_bytes": 0,
                }
            ]
        },
        evidence_id="ev_table_support",
    )
    return [explain, stats, tables]


def test_slow_query_support_produces_actionable_candidate() -> None:
    outcome = analyze_slow_query(
        _slow_query_support_evidence(), scenario_id="mysql_slow_query"
    )

    assert outcome.status.value in {"SUCCESS", "PARTIAL"}
    assert outcome.hypotheses
    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status in {
        HypothesisStatus.CANDIDATE,
        HypothesisStatus.CONFIRMED,
    }
    assert hypothesis.confidence_band in {ConfidenceBand.HIGH, ConfidenceBand.MEDIUM}
    assert "ev_explain_support" in hypothesis.supporting_evidence_ids
    assert "ev_stats_support" in hypothesis.supporting_evidence_ids
    assert "全表扫描" in hypothesis.explanation
    assert any("索引" in item for item in (hypothesis.explanation, *hypothesis.assumptions))


def test_slow_query_index_plan_is_contradicting_and_rejected() -> None:
    explain = _evidence(
        "explain_query",
        {
            "plan": {
                "query_block": {
                    "table": {
                        "table_name": "fault_slow_query_4101",
                        "access_type": "ref",
                        "key": "idx_k",
                    }
                }
            }
        },
        evidence_id="ev_explain_index",
    )
    stats = _evidence(
        "get_query_stats",
        {
            "statements": [
                {
                    "digest": "digest-2",
                    "digest_text": "SELECT COUNT(*) FROM fault_slow_query_4101",
                    "rows_examined": 10,
                    "no_index_used": 0,
                }
            ]
        },
        evidence_id="ev_stats_index",
    )

    outcome = analyze_slow_query(
        [explain, stats], scenario_id="mysql_slow_query"
    )

    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.REJECTED
    assert hypothesis.confidence_band is ConfidenceBand.LOW
    assert "ev_explain_index" in hypothesis.contradicting_evidence_ids
    assert outcome.status.value in {"SUCCESS", "PARTIAL"}


def test_slow_query_not_supported_and_truncated_is_partial_or_unknown() -> None:
    stats = _evidence(
        "get_query_stats",
        {"available": False, "statements": []},
        status=CollectionStatus.NOT_SUPPORTED,
        quality=EvidenceQuality.UNKNOWN,
        completeness=EvidenceCompleteness.MISSING,
        engine="postgresql",
        evidence_id="ev_stats_missing",
    )
    explain = _evidence(
        "explain_query",
        {"plan": "[{\"Plan\": {\"Node Type\": \"Seq Sc", "plan_truncated": True},
        status=CollectionStatus.PARTIAL,
        quality=EvidenceQuality.LOW,
        completeness=EvidenceCompleteness.PARTIAL,
        engine="postgresql",
        evidence_id="ev_explain_truncated",
    )

    outcome = analyze_slow_query(
        [stats, explain], scenario_id="pg_slow_query"
    )

    assert outcome.status.value in {"PARTIAL", "UNKNOWN"}
    assert any("query_stats" in item for item in outcome.missing_evidence)
    assert any("截断" in item for item in outcome.missing_evidence)
    assert outcome.hypotheses[0].confidence_band in {
        ConfidenceBand.LOW,
        ConfidenceBand.MEDIUM,
        ConfidenceBand.UNKNOWN,
    }
    assert outcome.hypotheses[0].status in {
        HypothesisStatus.CANDIDATE,
        HypothesisStatus.UNKNOWN,
    }


def test_slow_query_cumulative_stats_are_not_treated_as_current_window() -> None:
    stats = _evidence(
        "get_query_stats",
        {
            "statements": [
                {
                    "digest_text": "SELECT COUNT(*) FROM fault_slow_query_4101",
                    "count_star": 3,
                    "rows_examined": 60000,
                }
            ]
        },
    )

    outcome = analyze_slow_query([stats], scenario_id="mysql_slow_query")

    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.CANDIDATE
    assert hypothesis.confidence_band is ConfidenceBand.LOW
    assert any("累计" in item for item in hypothesis.assumptions)
    assert any("执行计划" in item for item in outcome.missing_evidence)


# --------------------------------------------------------------------------- #
# KD-W12-02 回归：全表扫描 + 无 table_stats 证据不得抛 UnboundLocalError
# --------------------------------------------------------------------------- #


def _full_scan_without_table_stats() -> list[Evidence]:
    """EXPLAIN 全表扫描 + query_stats 高扫描量，但**不含** get_table_stats 证据。"""
    explain = _evidence(
        "explain_query",
        {
            "plan": {
                "query_block": {
                    "table": {
                        "table_name": "fault_slow_query_4101",
                        "access_type": "ALL",
                        "rows_examined_per_scan": 20000,
                    }
                }
            }
        },
        evidence_id="ev_kd_w12_02_explain",
    )
    stats = _evidence(
        "get_query_stats",
        {
            "statements": [
                {
                    "digest": "digest-kd-w12-02",
                    "digest_text": "SELECT COUNT(*) FROM fault_slow_query_4101",
                    "count_star": 3,
                    "rows_examined": 60000,
                    "no_index_used": 3,
                }
            ]
        },
        evidence_id="ev_kd_w12_02_stats",
    )
    return [explain, stats]


def test_kd_w12_02_full_scan_without_table_stats_is_degraded_not_exception() -> None:
    """无 table_stats 证据 → 不抛异常且返回明确降级结论（UNKNOWN / 证据不足）。"""
    outcome = analyze_slow_query(
        _full_scan_without_table_stats(), scenario_id="mysql_slow_query"
    )

    assert outcome.hypotheses
    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.UNKNOWN
    assert hypothesis.confidence_band is ConfidenceBand.UNKNOWN
    assert "证据不足" in hypothesis.explanation
    assert "table_stats" in hypothesis.explanation
    assert any("table_stats" in item for item in outcome.missing_evidence)
    assert outcome.status.value in {"PARTIAL", "UNKNOWN"}
    # 不得把「缺少可用索引」当成已确认结论。
    assert hypothesis.status is not HypothesisStatus.CONFIRMED


def test_kd_w12_02_with_table_stats_evidence_normal_path_is_unchanged() -> None:
    """对照：补齐 table_stats 证据后既有正常路径逐值不变（SUCCESS/CONFIRMED/HIGH）。"""
    outcome = analyze_slow_query(
        _slow_query_support_evidence(), scenario_id="mysql_slow_query"
    )

    assert outcome.status.value == "SUCCESS"
    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.CONFIRMED
    assert hypothesis.confidence_band is ConfidenceBand.HIGH
    assert "ev_table_support" in hypothesis.supporting_evidence_ids
    assert "全表扫描" in hypothesis.explanation
    assert "证据不足" not in hypothesis.explanation
    assert outcome.missing_evidence == []


# --------------------------------------------------------------------------- #
# 连接压力
# --------------------------------------------------------------------------- #


def test_connection_pressure_requires_app_pool_and_threshold_disclaimer() -> None:
    snapshot = _evidence(
        "get_connection_snapshot",
        {
            "sessions": [
                {"processlist_id": index, "command": "Sleep", "state": None}
                for index in range(1, 11)
            ],
            "row_count": 10,
        },
        evidence_id="ev_snapshot",
    )
    transactions = _evidence(
        "get_transaction_metadata",
        {
            "transactions": [
                {
                    "trx_mysql_thread_id": 3,
                    "trx_state": "RUNNING",
                    "transaction_age_seconds": 120,
                }
            ]
        },
        evidence_id="ev_trx",
    )
    stats = _evidence(
        "get_query_stats",
        {"statements": [{"digest_text": "SELECT 1", "errors": 5}]},
        evidence_id="ev_conn_stats",
    )

    outcome = analyze_connection_pressure(
        [snapshot, transactions, stats], scenario_id="mysql_connection_pressure"
    )

    hypothesis = outcome.hypotheses[0]
    assert outcome.status.value == "PARTIAL"
    assert APP_POOL_MISSING in outcome.missing_evidence
    assert any("max_connections" in item for item in outcome.missing_evidence)
    assert THRESHOLD_DISCLAIMER in hypothesis.assumptions
    assert hypothesis.status is HypothesisStatus.CANDIDATE
    assert hypothesis.confidence_band is ConfidenceBand.MEDIUM
    assert "ev_snapshot" in hypothesis.supporting_evidence_ids
    assert "连接池" in hypothesis.explanation
    assert "建议值/未实测" in hypothesis.assumptions[0]


def test_connection_pressure_low_usage_is_contradicting_and_degraded() -> None:
    snapshot = _evidence(
        "get_connection_snapshot",
        {
            "sessions": [],
            "session_count": 2,
            "max_connections": 200,
        },
        evidence_id="ev_snapshot_low",
    )
    transactions = _evidence(
        "get_transaction_metadata",
        {"transactions": []},
        evidence_id="ev_trx_empty",
    )
    stats = _evidence(
        "get_query_stats",
        {"statements": [{"digest_text": "SELECT 1", "errors": 0}]},
        evidence_id="ev_stats_ok",
    )

    outcome = analyze_connection_pressure(
        [snapshot, transactions, stats], scenario_id="mysql_connection_pressure"
    )

    hypothesis = outcome.hypotheses[0]
    assert hypothesis.status is HypothesisStatus.REJECTED
    assert hypothesis.confidence_band is ConfidenceBand.LOW
    assert "ev_snapshot_low" in hypothesis.contradicting_evidence_ids
    assert outcome.status.value == "PARTIAL"
    assert APP_POOL_MISSING in outcome.missing_evidence


# --------------------------------------------------------------------------- #
# 锁等待
# --------------------------------------------------------------------------- #


def _lock_evidence() -> list[Evidence]:
    edges = _evidence(
        "get_lock_wait_edges",
        {
            "edges": [
                {
                    "waiting_processlist_id": 7,
                    "blocking_processlist_id": 5,
                    "object_name": "fault_lock_wait_4103",
                    "waiting_time_seconds": 5,
                    "blocking_transaction_age_seconds": 8,
                    "blocking_lock_mode": "X,REC_NOT_GAP",
                }
            ],
            "roots": [{"blocking_processlist_id": 5, "waiting_count": 1}],
            "wait_count": 1,
        },
        evidence_id="ev_edges",
    )
    details = _evidence(
        "get_lock_details",
        {"locks": [{"pid": 7, "granted": False}], "waiting_count": 1},
        evidence_id="ev_locks",
    )
    transactions = _evidence(
        "get_transaction_metadata",
        {
            "transactions": [
                {"trx_mysql_thread_id": 5, "transaction_age_seconds": 8}
            ]
        },
        evidence_id="ev_lock_trx",
    )
    return [edges, details, transactions]


def test_lock_wait_builds_blocking_tree_with_transaction_age() -> None:
    outcome = analyze_lock_wait(
        _lock_evidence(), scenario_id="mysql_lock_wait"
    )

    hypothesis = outcome.hypotheses[0]
    assert outcome.status.value in {"SUCCESS", "PARTIAL"}
    assert hypothesis.status in {
        HypothesisStatus.CANDIDATE,
        HypothesisStatus.CONFIRMED,
    }
    assert "ev_edges" in hypothesis.supporting_evidence_ids
    assert "持有" in hypothesis.explanation
    assert "等待" in hypothesis.explanation
    assert "阻塞事务年龄 8.0s" in hypothesis.explanation
    assert hypothesis.confidence_band in {ConfidenceBand.HIGH, ConfidenceBand.MEDIUM}


def test_lock_wait_cross_view_inconsistency_is_partial() -> None:
    edges = _evidence(
        "get_lock_wait_edges",
        {
            "edges": [
                {
                    "waiting_processlist_id": 7,
                    "blocking_processlist_id": 5,
                    "object_name": "fault_lock_wait_4103",
                }
            ],
            "roots": [],
            "wait_count": 1,
        },
        completeness=EvidenceCompleteness.PARTIAL,
        quality=EvidenceQuality.LOW,
        evidence_id="ev_edges_truncated",
    )
    details = _evidence(
        "get_lock_details",
        {"locks": [], "waiting_count": 3},
        evidence_id="ev_locks_inconsistent",
    )

    outcome = analyze_lock_wait(
        [edges, details], scenario_id="mysql_lock_wait"
    )

    assert outcome.status.value == "PARTIAL"
    assert any("不一致" in item for item in outcome.missing_evidence)
    assert any("截断" in item for item in outcome.missing_evidence)
    assert outcome.hypotheses[0].confidence_band in {
        ConfidenceBand.LOW,
        ConfidenceBand.MEDIUM,
    }


def test_lock_wait_not_supported_is_unknown() -> None:
    edges = _evidence(
        "get_lock_wait_edges",
        {},
        status=CollectionStatus.NOT_SUPPORTED,
        quality=EvidenceQuality.UNKNOWN,
        completeness=EvidenceCompleteness.MISSING,
        evidence_id="ev_edges_missing",
    )
    details = _evidence(
        "get_lock_details",
        {},
        status=CollectionStatus.DENIED,
        quality=EvidenceQuality.UNKNOWN,
        completeness=EvidenceCompleteness.UNKNOWN,
        evidence_id="ev_locks_denied",
    )

    outcome = analyze_lock_wait(
        [edges, details], scenario_id="pg_lock_wait"
    )

    assert outcome.status.value in {"UNKNOWN", "PARTIAL"}
    assert outcome.hypotheses[0].status is HypothesisStatus.UNKNOWN
    assert outcome.hypotheses[0].confidence_band is ConfidenceBand.UNKNOWN
    assert outcome.hypotheses[0].supporting_evidence_ids == []


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("scenario", "analyser"),
    [
        (FaultScenario.SLOW_QUERY, analyze_slow_query),
        (FaultScenario.CONNECTION_PRESSURE, analyze_connection_pressure),
        (FaultScenario.LOCK_WAIT, analyze_lock_wait),
    ],
)
def test_analyze_scenario_dispatches_and_exports_stop_conditions(
    scenario: FaultScenario, analyser
) -> None:
    outcome = analyze_scenario(scenario, [], scenario_id=f"unit_{scenario.value}")

    assert outcome.stop_conditions
    assert outcome.hypotheses
    assert json.dumps(outcome.observed_window) == "{}"
    assert outcome.status.value in {"UNKNOWN", "PARTIAL"}
