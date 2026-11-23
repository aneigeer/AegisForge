"""W04 集成测试：双引擎三场景的只读诊断流程、证据留存与 Oracle 隔离。

PostgreSQL 用例需要 PG LAB 在 55432 端口运行（测试命令会在同一进程组内临时启动）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from adapters.mysql.adapter import MySQLAdapter
from adapters.postgres_mcp.adapter import PostgresMcpAdapter
from adapters.postgres_mcp.client import PostgresMcpClient
from benchmarks.faults.injector import FaultSession
from core.config import Settings
from core.diagnosis.analyzers import APP_POOL_MISSING
from core.diagnosis.runner import DiagnosisRunner
from core.errors import DatabaseQueryError
from core.evidence.models import CollectionStatus
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService
from core.faults.store import load_manifest, load_oracle
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlClient
from database.postgres.connection import PgClient

pytestmark = pytest.mark.integration

SCENARIOS = [
    ("mysql_slow_query", "mysql"),
    ("mysql_connection_pressure", "mysql"),
    ("mysql_lock_wait", "mysql"),
    ("pg_slow_query", "postgresql"),
    ("pg_connection_pressure", "postgresql"),
    ("pg_lock_wait", "postgresql"),
]
SLOW_QUERY_IDS = {"mysql_slow_query", "pg_slow_query"}
LOCK_WAIT_IDS = {"mysql_lock_wait", "pg_lock_wait"}


@pytest.fixture(scope="module")
def pg_mcp(settings: Settings) -> PostgresMcpClient:
    if settings.pg_ro_dsn is None:
        pytest.skip("未配置 DG_PG_RO_DSN")
    client = PostgresMcpClient(settings.pg_ro_dsn, timeout_s=20.0)
    try:
        yield client
    finally:
        client.close()


def _services(
    control_client: MysqlClient,
) -> tuple[TaskService, EvidenceService, EvidenceRepository]:
    repository = TaskRepository(control_client)
    task_service = TaskService(repository, ToolRegistry())
    evidence_repository = EvidenceRepository(control_client)
    evidence_service = EvidenceService(evidence_repository, repository)
    return task_service, evidence_service, evidence_repository


@pytest.fixture
def mysql_runner(
    control_client: MysqlClient, ro_client: MysqlClient, settings: Settings
) -> DiagnosisRunner:
    task_service, evidence_service, _ = _services(control_client)
    return DiagnosisRunner(
        task_service,
        evidence_service,
        MySQLAdapter(ro_client),
        "tenant_local",
        settings.default_environment_id,
    )


@pytest.fixture
def pg_runner(
    control_client: MysqlClient,
    pg_mcp: PostgresMcpClient,
    settings: Settings,
) -> DiagnosisRunner:
    task_service, evidence_service, _ = _services(control_client)
    return DiagnosisRunner(
        task_service,
        evidence_service,
        PostgresMcpAdapter(pg_mcp),
        "tenant_local",
        settings.default_pg_environment_id,
    )


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(admin_client: MysqlClient, monkeypatch):
    created: list[str] = []
    original = TaskService.create_task

    def tracked(self, goal, **kwargs):
        task = original(self, goal, **kwargs)
        created.append(task.task_id)
        return task

    monkeypatch.setattr(TaskService, "create_task", tracked)
    yield
    for task_id in created:
        try:
            admin_client.execute(
                "DELETE FROM dg_system.evidence WHERE task_id = %s", (task_id,)
            )
        except DatabaseQueryError:
            pass
        for table in ("task_events", "tool_calls", "task_steps", "checkpoints", "tasks"):
            admin_client.execute(
                f"DELETE FROM dg_system.{table} WHERE task_id = %s", (task_id,)
            )


def _evidences(
    task_service: TaskService,
    evidence_service: EvidenceService,
    task_id: str,
) -> list[Any]:
    task = task_service.get_task(task_id)
    items, _ = evidence_service.list(task_id, task.tenant_id, limit=100)
    return items


def _payload_data(evidence: Any) -> dict[str, Any]:
    payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _mysql_fault_table_exists(client: MysqlClient, table: str) -> bool:
    rows = client.fetch_all(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        ("dg_lab", table),
    )
    return int(rows[0]["c"]) > 0


def _pg_table_exists(client: PgClient, qualified: str) -> bool:
    rows = client.fetch_all("SELECT to_regclass(%s) AS r", (qualified,))
    return bool(rows and rows[0].get("r"))


def _public_text(result: Any, task: Any, evidences: list[Any]) -> str:
    return (
        json.dumps(task.model_dump(mode="json"), ensure_ascii=False, default=str)
        + json.dumps(
            [item.payload for item in evidences], ensure_ascii=False, default=str
        )
        + result.model_dump_json()
    )


@pytest.mark.parametrize(("scenario_id", "engine"), SCENARIOS)
def test_diagnosis_flow_is_traceable_and_evidence_survives_reset(
    scenario_id: str,
    engine: str,
    request: pytest.FixtureRequest,
    ro_client: MysqlClient,
    rw_client: MysqlClient,
    pg_rw_client: PgClient,
    control_client: MysqlClient,
    settings: Settings,
) -> None:
    runner: DiagnosisRunner = request.getfixturevalue(
        "mysql_runner" if engine == "mysql" else "pg_runner"
    )
    task_service, evidence_service, _ = _services(control_client)
    manifest = load_manifest(scenario_id)
    truth = load_oracle(scenario_id)
    table = f"fault_{manifest.scenario.value}_{manifest.data_seed}"
    qualified = f"{manifest.workload_config.get('schema', 'fault_lab')}.{table}"

    with FaultSession(scenario_id):
        result = runner.run(scenario_id)

    assert result.status.value != "UNKNOWN"
    assert result.hypotheses
    assert result.evidence_ids
    assert result.has_traceable_evidence

    # 重置后任务与证据仍可查询（证据只追加，不随 reset 删除）。
    task = task_service.get_task(result.task_id)
    assert task.task_id == result.task_id
    assert task.environment_id == (
        settings.default_environment_id
        if engine == "mysql"
        else settings.default_pg_environment_id
    )
    evidences = _evidences(task_service, evidence_service, result.task_id)
    assert {item.evidence_id for item in evidences} == set(result.evidence_ids)
    assert all(item.tenant_id == "tenant_local" for item in evidences)
    assert all(item.environment_id == task.environment_id for item in evidences)

    data = [_payload_data(item) for item in evidences]
    tools = {item.source_tool for item in evidences}

    if manifest.scenario.value == "slow_query":
        explain = next(
            item for item in evidences if item.source_tool == "explain_query"
        )
        plan_text = json.dumps(_payload_data(explain).get("plan"), ensure_ascii=False)
        assert "ALL" in plan_text or "Seq Scan" in plan_text
        assert explain.evidence_id in result.hypotheses[0].supporting_evidence_ids

    if manifest.scenario.value == "connection_pressure":
        assert "get_connection_snapshot" in tools
        assert any(
            APP_POOL_MISSING in item for item in result.missing_evidence
        ), result.missing_evidence

    if manifest.scenario.value == "lock_wait":
        assert "get_lock_wait_edges" in tools
        edge_evidence = next(
            item for item in evidences if item.source_tool == "get_lock_wait_edges"
        )
        edge_data = _payload_data(edge_evidence)
        edges = edge_data.get("edges") or []
        assert edges
        assert any("等待" in item.explanation for item in result.hypotheses)
        assert any(
            "transaction_age_seconds" in item.payload
            or "transaction_age_seconds" in json.dumps(
                _payload_data(item), ensure_ascii=False
            )
            or "blocking_transaction_age_seconds" in json.dumps(
                _payload_data(item), ensure_ascii=False
            )
            for item in evidences
        )
        assert data

    # Oracle 真值不得进入任务目标、证据 payload 或诊断结果。
    public_text = _public_text(result, task, evidences)
    assert truth.expected_root_cause not in public_text
    assert "expected_root_cause" not in public_text
    assert "fault_seed" not in public_text

    # 故障沙箱表在 reset 后清理。
    if scenario_id in SLOW_QUERY_IDS or scenario_id in LOCK_WAIT_IDS:
        if engine == "mysql":
            assert not _mysql_fault_table_exists(rw_client, table)
        else:
            assert not _pg_table_exists(pg_rw_client, qualified)


def test_pg_query_stats_missing_is_not_fabricated(
    pg_runner: DiagnosisRunner,
    pg_mcp: PostgresMcpClient,
    control_client: MysqlClient,
) -> None:
    task_service, evidence_service, _ = _services(control_client)

    with FaultSession("pg_slow_query"):
        result = pg_runner.run("pg_slow_query")

    evidences = _evidences(task_service, evidence_service, result.task_id)
    stats = next(item for item in evidences if item.source_tool == "get_query_stats")
    assert stats.collection_status is CollectionStatus.NOT_SUPPORTED
    stats_data = _payload_data(stats)
    assert stats_data.get("available") is False
    assert stats_data.get("statements") == []
    assert any("query_stats" in item for item in result.missing_evidence)
    assert result.status.value in {"PARTIAL", "NOT_SUPPORTED"}
    for hypothesis in result.hypotheses:
        assert stats.evidence_id not in hypothesis.supporting_evidence_ids

    explain = next(item for item in evidences if item.source_tool == "explain_query")
    plan_text = json.dumps(_payload_data(explain).get("plan"), ensure_ascii=False)
    assert "Seq Scan" in plan_text


def test_pg_lock_wait_edge_and_transaction_age_are_observed(
    pg_runner: DiagnosisRunner,
    control_client: MysqlClient,
) -> None:
    task_service, evidence_service, _ = _services(control_client)

    with FaultSession("pg_lock_wait"):
        result = pg_runner.run("pg_lock_wait")

    evidences = _evidences(task_service, evidence_service, result.task_id)
    edges = next(
        item for item in evidences if item.source_tool == "get_lock_wait_edges"
    )
    edge_data = _payload_data(edges)
    assert edge_data.get("edges")
    ages = next(
        _payload_data(item).get("max_transaction_age_seconds")
        for item in evidences
        if item.source_tool == "get_transaction_metadata"
    )
    if ages is None:
        # PG 只读账号可能缺少 pg_read_all_stats，xact_start 被隐藏；必须显式保留未知项。
        assert any(
            "事务年龄" in item or "等待时长" in item
            for item in result.missing_evidence
        ), result.missing_evidence
        assert result.status.value == "PARTIAL"
    else:
        assert float(ages) > 0
    assert any("等待" in item.explanation for item in result.hypotheses)


def test_runner_writes_tool_calls_and_audit_events(
    mysql_runner: DiagnosisRunner,
    control_client: MysqlClient,
    admin_client: MysqlClient,
) -> None:
    with FaultSession("mysql_lock_wait"):
        result = mysql_runner.run("mysql_lock_wait")

    calls = control_client.fetch_all(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN tool_call_id IS NOT NULL THEN 1 ELSE 0 END) AS with_id "
        "FROM dg_system.tool_calls WHERE task_id = %s",
        (result.task_id,),
    )
    assert int(calls[0]["total"]) >= 1
    assert int(calls[0]["with_id"]) == int(calls[0]["total"])

    events = control_client.fetch_all(
        "SELECT DISTINCT event_type FROM dg_system.task_events WHERE task_id = %s",
        (result.task_id,),
    )
    event_types = {str(row["event_type"]) for row in events}
    assert "TOOL_CALL_STARTED" in event_types
    assert "TOOL_CALL_SUCCEEDED" in event_types

    evidence_rows = control_client.fetch_all(
        "SELECT COUNT(*) AS c FROM dg_system.evidence "
        "WHERE task_id = %s AND tool_call_id IS NOT NULL",
        (result.task_id,),
    )
    assert int(evidence_rows[0]["c"]) >= 1
