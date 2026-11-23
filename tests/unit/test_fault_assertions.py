"""W04 断言注册与可执行性测试。"""

from __future__ import annotations

import pytest

from benchmarks.faults.assertions import supported_assertions, verify_assertions
from core.faults.store import list_manifests, load_manifest

_SAMPLE_OBSERVATIONS = {
    "connections_baseline": 5,
    "connections_after": 15,
    "configured_sessions": 10,
    "connections_after_reset": 5,
    "wait_count": 1,
    "edges": [{"blocking_pid": 1, "waiting_pid": 2}],
    "table_exists_after_reset": False,
    "open_sessions_after_reset": 0,
    "query_stats_observable": True,
    "explain_plan": "Seq Scan on fault_slow_query_1",
    "explain_access_type": "ALL",
}


def test_all_manifest_assertions_have_executable_checks() -> None:
    manifests = list_manifests()
    assert len(manifests) == 6
    known = supported_assertions()
    for manifest in manifests:
        unknown = set(manifest.verification_assertions) - known
        assert not unknown, f"{manifest.scenario_id} 未注册断言：{unknown}"


def test_manifest_assertions_pass_with_real_observation_shape() -> None:
    for manifest in list_manifests():
        results = verify_assertions(manifest, _SAMPLE_OBSERVATIONS)
        assert all(result.passed for result in results), [
            (result.name, result.detail) for result in results if not result.passed
        ]


def test_assertions_fail_closed_without_observations() -> None:
    manifest = load_manifest("mysql_lock_wait")
    results = verify_assertions(manifest, {})
    assert any(not result.passed for result in results)
