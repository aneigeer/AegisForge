"""W04 最小真值断言执行器：Manifest 断言 → 可执行检查。

断言只消费注入/观测得到的公开观测值，不读取 Oracle expected_root_cause。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.faults.models import FaultCaseManifest

ObservationMap = dict[str, Any]


@dataclass(frozen=True)
class AssertionResult:
    name: str
    passed: bool
    detail: str


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _session_count_increase(obs: ObservationMap) -> tuple[bool, str]:
    baseline = _number(obs.get("connections_baseline"))
    after = _number(obs.get("connections_after"))
    configured = _number(obs.get("configured_sessions"))
    return after - baseline >= configured, f"baseline={baseline} after={after} configured={configured}"


def _session_count_restored(obs: ObservationMap) -> tuple[bool, str]:
    baseline = _number(obs.get("connections_baseline"))
    restored = _number(obs.get("connections_after_reset"))
    return restored <= baseline, f"baseline={baseline} after_reset={restored}"


def _wait_observed(obs: ObservationMap) -> tuple[bool, str]:
    wait_count = _number(obs.get("wait_count"))
    edges = obs.get("edges") or obs.get("blocking_edges") or []
    return wait_count >= 1 or bool(edges), f"wait_count={wait_count} edges={len(edges) if isinstance(edges, list) else 'n/a'}"


def _table_dropped(obs: ObservationMap) -> tuple[bool, str]:
    exists = obs.get("table_exists_after_reset")
    return exists is False, f"table_exists_after_reset={exists}"


def _sessions_closed(obs: ObservationMap) -> tuple[bool, str]:
    open_sessions = _number(obs.get("open_sessions_after_reset"), -1)
    return open_sessions == 0, f"open_sessions_after_reset={open_sessions}"


def _query_stats_or_plan(obs: ObservationMap) -> tuple[bool, str]:
    stats = obs.get("query_stats_observable")
    plan = obs.get("explain_plan")
    return bool(stats) or bool(plan), f"query_stats={bool(stats)} plan={bool(plan)}"


def _sequential_scan(obs: ObservationMap) -> tuple[bool, str]:
    plan = str(obs.get("explain_plan") or "")
    access = str(obs.get("explain_access_type") or "")
    return ("Seq Scan" in plan) or access.upper() == "ALL", f"access_type={access}"


_ASSERTIONS: dict[str, Callable[[ObservationMap], tuple[bool, str]]] = {
    "session_count_increases_by_configured_sessions": _session_count_increase,
    "session_count_restored_after_reset": _session_count_restored,
    "blocking_tree_observes_waiting_lock": _wait_observed,
    "lock_wait_edges_observes_waiting_lock": _wait_observed,
    "lock_target_table_dropped_after_reset": _table_dropped,
    "sandbox_table_dropped_after_reset": _table_dropped,
    "lock_sessions_closed_after_reset": _sessions_closed,
    "query_stats_or_explain_plan_observable": _query_stats_or_plan,
    "explain_plan_shows_sequential_scan": _sequential_scan,
}


def supported_assertions() -> set[str]:
    return set(_ASSERTIONS)


def verify_assertions(
    manifest: FaultCaseManifest, observations: ObservationMap
) -> list[AssertionResult]:
    """逐条执行 Manifest 断言；未知断言不默认通过，而是明确失败。"""

    results: list[AssertionResult] = []
    for name in manifest.verification_assertions:
        check = _ASSERTIONS.get(name)
        if check is None:
            results.append(AssertionResult(name=name, passed=False, detail="未注册断言"))
            continue
        passed, detail = check(observations)
        results.append(AssertionResult(name=name, passed=passed, detail=detail))
    return results


__all__ = ["AssertionResult", "supported_assertions", "verify_assertions"]
