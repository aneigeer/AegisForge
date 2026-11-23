"""P5-T1：评测中台的持久化与**候选对比**（只信实测，不补零）。

设计取舍：

- 只存**实测**指标：缺失一律 `None`，不写 0、不做插值（INV-06）；
- 对比只做"逐指标差值与方向"，不产出综合分——综合分需要权重，权重不该由代码暗定；
- `verdict` 只有三种：`CANDIDATE_BETTER` / `BASELINE_BETTER` / `INCONCLUSIVE`，
  且要求**所有可比指标同向**才给结论；任一指标缺失或方向冲突即为 `INCONCLUSIVE`
  （避免"挑几个好看的指标宣布胜利"）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.errors import DBGuardianError, ErrorCode

#: 指标方向：数值越大越好 / 越小越好。
HIGHER_IS_BETTER: frozenset[str] = frozenset(
    {
        "diagnosis_accuracy",
        "root_cause_hit_rate",
        "json_validity_rate",
        "tool_precision",
        "outcome_pass_rate",
        "artifact_hash_match_rate",
        "degraded_path_correctness",
        "cost_efficiency",
    }
)
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {"latency_p95_ms", "tokens_per_case", "unsafe_action_rate", "false_positive_rate"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class ComparisonVerdict:
    verdict: str
    deltas: dict[str, float | None]
    comparable: tuple[str, ...]
    reason: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "deltas": self.deltas,
            "comparable": list(self.comparable),
            "reason": self.reason,
        }


def compare_metrics(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> ComparisonVerdict:
    """逐指标比较；只有**全部可比指标同向**才给出结论。"""
    deltas: dict[str, float | None] = {}
    comparable: list[str] = []
    directions: list[str] = []
    for metric in sorted(set(baseline) | set(candidate)):
        left = baseline.get(metric)
        right = candidate.get(metric)
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            deltas[metric] = None
            continue
        delta = float(right) - float(left)
        deltas[metric] = round(delta, 6)
        comparable.append(metric)
        if delta == 0:
            continue
        if metric in HIGHER_IS_BETTER:
            directions.append("better" if delta > 0 else "worse")
        elif metric in LOWER_IS_BETTER:
            directions.append("better" if delta < 0 else "worse")
    if not comparable:
        return ComparisonVerdict("INCONCLUSIVE", deltas, (), "NO_MEASURED_METRIC")
    if not directions:
        return ComparisonVerdict("INCONCLUSIVE", deltas, tuple(comparable), "NO_DIFFERENCE")
    if set(directions) == {"better"}:
        return ComparisonVerdict(
            "CANDIDATE_BETTER", deltas, tuple(comparable), "ALL_METRICS_IMPROVED"
        )
    if set(directions) == {"worse"}:
        return ComparisonVerdict(
            "BASELINE_BETTER", deltas, tuple(comparable), "ALL_METRICS_REGRESSED"
        )
    return ComparisonVerdict("INCONCLUSIVE", deltas, tuple(comparable), "MIXED_DIRECTION")


class EvalRepository:
    """控制库上的评测仓储（只 INSERT / SELECT）。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create_suite(
        self,
        tenant_id: str,
        *,
        suite_version: str,
        kind: str,
        case_count: int,
        digest: str,
    ) -> str:
        suite_id = f"suite_{uuid.uuid4().hex[:16]}"
        self._client.execute(
            "INSERT INTO dg_system.eval_suites (suite_id, tenant_id, suite_version, kind,"
            " case_count, digest, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (suite_id, tenant_id, suite_version, kind, int(case_count), digest, _now()),
        )
        return suite_id

    def create_run(
        self,
        tenant_id: str,
        *,
        suite_id: str,
        candidate_label: str,
        provider_type: str,
        model_name: str,
        wiki_snapshot: str | None,
        status: str,
        metrics: Mapping[str, Any],
        notes: str | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        self._client.execute(
            "INSERT INTO dg_system.eval_runs (run_id, tenant_id, suite_id, candidate_label,"
            " provider_type, model_name, wiki_snapshot, status, metrics_json, notes, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                tenant_id,
                suite_id,
                candidate_label,
                provider_type,
                model_name,
                wiki_snapshot,
                status,
                _json(dict(metrics)),
                notes,
                _now(),
            ),
        )
        return run_id

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        rows = self._client.fetch_all(
            "SELECT * FROM dg_system.eval_runs WHERE run_id = %s AND tenant_id = %s",
            (run_id, tenant_id),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
        return row

    def record_comparison(
        self,
        tenant_id: str,
        *,
        baseline_run_id: str,
        candidate_run_id: str,
        verdict: ComparisonVerdict,
    ) -> str:
        comparison_id = f"cmp_{uuid.uuid4().hex[:16]}"
        self._client.execute(
            "INSERT INTO dg_system.eval_comparisons (comparison_id, tenant_id, baseline_run_id,"
            " candidate_run_id, deltas_json, verdict, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                comparison_id,
                tenant_id,
                baseline_run_id,
                candidate_run_id,
                _json(verdict.as_payload()),
                verdict.verdict,
                _now(),
            ),
        )
        return comparison_id

    def get_comparison(self, tenant_id: str, comparison_id: str) -> dict[str, Any] | None:
        rows = self._client.fetch_all(
            "SELECT * FROM dg_system.eval_comparisons WHERE comparison_id = %s AND tenant_id = %s",
            (comparison_id, tenant_id),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["payload"] = json.loads(row.pop("deltas_json") or "{}")
        return row

    def list_runs(self, tenant_id: str, limit: int = 50) -> Sequence[dict[str, Any]]:
        rows = self._client.fetch_all(
            "SELECT run_id, candidate_label, model_name, wiki_snapshot, status, created_at"
            " FROM dg_system.eval_runs WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, max(1, min(int(limit), 200))),
        )
        return [dict(row) for row in rows]


__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "ComparisonVerdict",
    "EvalRepository",
    "compare_metrics",
]
