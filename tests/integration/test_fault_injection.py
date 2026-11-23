"""W04 集成测试：双引擎三类故障 inject/reset 与 W02 工具可观测性。

PostgreSQL 用例需要 PG LAB 在 55432 端口运行；未启动时显式 skip 并标注环境缺失。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import pytest

from adapters.postgres_mcp.tools import execute_tool as execute_pg_tool
from benchmarks.faults.injector import FaultSession, reset
from core.errors import DBGuardianError
from core.faults.store import load_manifest, load_oracle
from core.tools.contracts import ToolContext, ToolResult, ToolStatus
from database.observers import build_observer_registry

pytestmark = pytest.mark.integration


def _run_observer(
    registry, client, name: str, params: dict[str, Any] | None = None
) -> ToolResult:
    tool = registry.get(name)
    context = ToolContext(client=client, task_id="task_w04", tool_call_id="call_w04")
    return tool.run(context, params or {})


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return False


def _mysql_table_exists(client, table: str) -> bool:
    rows = client.fetch_all(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        ("dg_lab", table),
    )
    return int(rows[0]["c"]) > 0


def _mysql_rw_connection_count(client) -> int:
    identity = client.identity()
    user = str(identity.user).split("@", 1)[0]
    rows = client.fetch_all(
        "SELECT COUNT(*) AS c FROM information_schema.processlist "
        "WHERE USER = %s AND DB = %s",
        (user, "dg_lab"),
    )
    return int(rows[0]["c"])


def _poll_query_stats(registry, client, table: str) -> dict[str, Any] | None:
    stats = _run_observer(
        registry, client, "get_query_stats", {"database_name": "dg_lab", "limit": 200}
    )
    if stats.status != ToolStatus.SUCCESS:
        return None
    for statement in stats.data.get("statements", []):
        text = str(statement.get("digest_text") or "").lower()
        if "select" in text and table.lower() in text:
            return statement
    return None


def _pg_tool(
    client, name: str, params: dict[str, Any] | None = None
) -> ToolResult:
    identity = client.identity()
    return execute_pg_tool(
        name,
        params,
        client=client,
        database_id=identity.database_id,
        db_version=identity.server_version,
    )


def _pg_table_exists(client, qualified: str) -> bool:
    rows = client.fetch_all("SELECT to_regclass(%s) AS r", (qualified,))
    return bool(rows and rows[0].get("r"))


def _pg_connection_count(client) -> int:
    identity = client.identity()
    rows = client.fetch_all(
        "SELECT count(*) AS c FROM pg_stat_activity WHERE datname = %s AND usename = %s",
        ("dg_lab_pg", str(identity.user)),
    )
    return int(rows[0]["c"])


def _require_pg(client) -> None:
    try:
        client.ping()
    except DBGuardianError as exc:
        pytest.skip(
            f"PostgreSQL LAB 环境缺失（需 127.0.0.1:55432 运行 PG 12.18）：{exc.code.value}"
        )


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #


def test_mysql_slow_query_inject_observe_reset_repeatably(rw_client, ro_client, caplog) -> None:
    caplog.set_level("INFO")
    manifest = load_manifest("mysql_slow_query")
    truth = load_oracle("mysql_slow_query")
    table = f"fault_slow_query_{manifest.data_seed}"
    registry = build_observer_registry()

    for _cycle in range(2):
        with FaultSession("mysql_slow_query", rw_client=rw_client) as result:
            assert result.injected is True
            assert result.observed["rows_inserted"] == manifest.workload_config["rows"]
            assert _mysql_table_exists(rw_client, table)

            explain = _run_observer(
                registry,
                ro_client,
                "explain_query",
                {"sql": f"SELECT COUNT(*) AS c, SUM(k) AS s FROM dg_lab.{table} WHERE k > 0"},
            )
            assert explain.status == ToolStatus.SUCCESS, explain.error
            plan_text = json.dumps(explain.data["plan"], ensure_ascii=False)
            assert table in plan_text
            assert '"access_type": "ALL"' in plan_text

            found = _wait_until(
                lambda: _poll_query_stats(registry, ro_client, table) is not None
            )
            assert found, "慢查询未在 get_query_stats 中留下可观测 digest"

            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert not _mysql_table_exists(rw_client, table)


def test_mysql_connection_pressure_observable_and_restored(rw_client, caplog) -> None:
    caplog.set_level("INFO")
    manifest = load_manifest("mysql_connection_pressure")
    truth = load_oracle("mysql_connection_pressure")
    baseline = _mysql_rw_connection_count(rw_client)

    for _cycle in range(2):
        with FaultSession(
            "mysql_connection_pressure", rw_client=rw_client, connection_count=10
        ) as result:
            after = _mysql_rw_connection_count(rw_client)
            assert after >= baseline + 10
            assert result.observed["connections_opened"] == 10
            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert _wait_until(lambda: _mysql_rw_connection_count(rw_client) <= baseline, timeout=3)


def test_mysql_lock_wait_visible_to_blocking_tree(rw_client, ro_client, caplog) -> None:
    caplog.set_level("INFO")
    manifest = load_manifest("mysql_lock_wait")
    truth = load_oracle("mysql_lock_wait")
    table = f"fault_lock_wait_{manifest.data_seed}"
    registry = build_observer_registry()
    baseline = _mysql_rw_connection_count(rw_client)

    for _cycle in range(2):
        with FaultSession("mysql_lock_wait", rw_client=rw_client) as result:
            assert result.observed["wait_count"] >= 1
            assert _mysql_table_exists(rw_client, table)

            tree = _run_observer(registry, ro_client, "get_blocking_tree", {"limit": 50})
            assert tree.status == ToolStatus.SUCCESS, tree.error
            assert tree.data["wait_count"] >= 1
            assert tree.data["roots"]
            assert any(
                edge.get("object_name") == table for edge in tree.data["edges"]
            ), tree.data["edges"]

            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert not _mysql_table_exists(rw_client, table)
        assert _wait_until(lambda: _mysql_rw_connection_count(rw_client) <= baseline, timeout=3)


def test_mysql_reset_without_active_session_drops_sandbox_tables(rw_client) -> None:
    """模拟 CLI 跨进程 reset：无活动会话时仍按 Manifest 清理沙箱表。"""

    manifest = load_manifest("mysql_lock_wait")
    table = f"fault_lock_wait_{manifest.data_seed}"
    rw_client.execute(
        f"CREATE TABLE IF NOT EXISTS `{table}` (id INT NOT NULL PRIMARY KEY, k INT NOT NULL)"
    )
    assert _mysql_table_exists(rw_client, table)
    summary = reset("mysql_lock_wait", rw_client=rw_client)
    assert summary["reset"] is True
    assert summary["session_cleanup"] is False
    assert table in summary["tables_cleaned"]
    assert not _mysql_table_exists(rw_client, table)


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def test_pg_slow_query_inject_observe_reset_repeatably(pg_rw_client, pg_ro_client, caplog) -> None:
    _require_pg(pg_rw_client)
    caplog.set_level("INFO")
    manifest = load_manifest("pg_slow_query")
    truth = load_oracle("pg_slow_query")
    table = f"{manifest.workload_config['schema']}.fault_slow_query_{manifest.data_seed}"

    for _cycle in range(2):
        with FaultSession("pg_slow_query", pg_rw_client=pg_rw_client) as result:
            assert result.injected is True
            assert result.observed["rows_inserted"] == manifest.workload_config["rows"]
            assert _pg_table_exists(pg_rw_client, table)

            explain = _pg_tool(
                pg_ro_client,
                "explain_query",
                {"sql": f"SELECT count(*) AS c, sum(k) AS s FROM {table} WHERE k > 0"},
            )
            assert explain.status == ToolStatus.SUCCESS, explain.error
            plan_text = json.dumps(explain.data["plan"], ensure_ascii=False)
            assert "fault_slow_query_" in plan_text
            assert "Seq Scan" in plan_text

            stats = _pg_tool(pg_ro_client, "get_query_stats", {"limit": 50})
            # PG12 LAB 未安装 pg_stat_statements；能力缺失必须显式 NOT_SUPPORTED。
            assert stats.status in {ToolStatus.SUCCESS, ToolStatus.NOT_SUPPORTED}
            if stats.status == ToolStatus.NOT_SUPPORTED:
                assert stats.data.get("available") is False

            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert not _pg_table_exists(pg_rw_client, table)


def test_pg_connection_pressure_observable_and_restored(pg_rw_client, caplog) -> None:
    _require_pg(pg_rw_client)
    caplog.set_level("INFO")
    truth = load_oracle("pg_connection_pressure")
    baseline = _pg_connection_count(pg_rw_client)

    for _cycle in range(2):
        with FaultSession(
            "pg_connection_pressure", pg_rw_client=pg_rw_client, connection_count=10
        ) as result:
            after = _pg_connection_count(pg_rw_client)
            assert after >= baseline + 10
            assert result.observed["connections_opened"] == 10
            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert _wait_until(lambda: _pg_connection_count(pg_rw_client) <= baseline, timeout=3)


def test_pg_lock_wait_visible_to_lock_wait_edges(pg_rw_client, pg_ro_client, caplog) -> None:
    _require_pg(pg_rw_client)
    caplog.set_level("INFO")
    manifest = load_manifest("pg_lock_wait")
    truth = load_oracle("pg_lock_wait")
    table = f"fault_lock_wait_{manifest.data_seed}"
    qualified = f"{manifest.workload_config['schema']}.{table}"

    for _cycle in range(2):
        with FaultSession("pg_lock_wait", pg_rw_client=pg_rw_client) as result:
            assert result.observed["wait_count"] >= 1
            assert _pg_table_exists(pg_rw_client, qualified)

            edges = _pg_tool(pg_ro_client, "get_lock_wait_edges", {"limit": 100})
            assert edges.status == ToolStatus.SUCCESS, edges.error
            assert edges.data["edge_count"] >= 1
            assert edges.data["waiting_pids"]
            assert edges.data["blocking_pids"]
            assert edges.data["root_pids"]

            public_text = json.dumps(result.to_public_dict(), ensure_ascii=False) + caplog.text
            assert truth.expected_root_cause not in public_text

        assert not _pg_table_exists(pg_rw_client, qualified)


def test_pg_reset_without_active_session_drops_sandbox_table(pg_rw_client) -> None:
    _require_pg(pg_rw_client)
    manifest = load_manifest("pg_lock_wait")
    table = f"fault_lock_wait_{manifest.data_seed}"
    qualified = f"{manifest.workload_config['schema']}.{table}"
    pg_rw_client.execute(f"CREATE SCHEMA IF NOT EXISTS {manifest.workload_config['schema']}")
    pg_rw_client.execute(
        f"CREATE TABLE IF NOT EXISTS {qualified} (id INTEGER NOT NULL PRIMARY KEY, k INTEGER NOT NULL)"
    )
    assert _pg_table_exists(pg_rw_client, qualified)
    summary = reset("pg_lock_wait", pg_rw_client=pg_rw_client)
    assert summary["reset"] is True
    assert summary["session_cleanup"] is False
    assert table in summary["tables_cleaned"]
    assert not _pg_table_exists(pg_rw_client, qualified)
