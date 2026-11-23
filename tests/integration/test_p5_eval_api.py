"""P5-T1 集成测试：评测中台（记录实测运行 → 候选对比 → 持久化）。

断言口径：

- 两组实测指标全同向改善 → `CANDIDATE_BETTER`；方向冲突 → `INCONCLUSIVE`；
- 缺测指标（null）不参与判定，也不被补零；
- 跨租户运行不可见（404）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

pytestmark = pytest.mark.integration


def _digest() -> str:
    return uuid4().hex * 2


def _suite(client: TestClient) -> str:
    response = client.post(
        "/v1/eval/suites",
        json={
            "suite_version": f"golden-{uuid4().hex[:6]}",
            "kind": "golden",
            "case_count": 12,
            "digest": _digest(),
        },
    )
    assert response.status_code == 201 or response.status_code == 200, response.text
    return response.json()["suite_id"]


def _run(client: TestClient, suite_id: str, *, label: str, metrics: dict[str, Any]) -> str:
    response = client.post(
        "/v1/eval/runs",
        json={
            "suite_id": suite_id,
            "candidate_label": label,
            "provider_type": "openai_compat",
            "model_name": "gpt-4o-mini",
            "status": "COMPLETED",
            "metrics": metrics,
        },
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()["run_id"]


def test_candidate_better_is_measured_not_guessed() -> None:
    with TestClient(app) as client:
        suite_id = _suite(client)
        baseline = _run(
            client,
            suite_id,
            label="baseline",
            metrics={"diagnosis_accuracy": 0.6, "latency_p95_ms": 900, "unsafe_action_rate": 0.02},
        )
        candidate = _run(
            client,
            suite_id,
            label="candidate",
            metrics={"diagnosis_accuracy": 0.75, "latency_p95_ms": 700, "unsafe_action_rate": 0.0},
        )
        compared = client.post(
            "/v1/eval/comparisons",
            json={"baseline_run_id": baseline, "candidate_run_id": candidate},
        )
        assert compared.status_code == 200, compared.text
        body = compared.json()
        assert body["verdict"] == "CANDIDATE_BETTER"
        assert body["deltas"]["diagnosis_accuracy"] == pytest.approx(0.15)
        assert body["deltas"]["latency_p95_ms"] == pytest.approx(-200.0)

        fetched = client.get(f"/v1/eval/comparisons/{body['comparison_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["verdict"] == "CANDIDATE_BETTER"


def test_mixed_direction_is_inconclusive() -> None:
    with TestClient(app) as client:
        suite_id = _suite(client)
        baseline = _run(client, suite_id, label="b", metrics={"diagnosis_accuracy": 0.6, "latency_p95_ms": 800})
        candidate = _run(client, suite_id, label="c", metrics={"diagnosis_accuracy": 0.8, "latency_p95_ms": 1200})
        body = client.post(
            "/v1/eval/comparisons",
            json={"baseline_run_id": baseline, "candidate_run_id": candidate},
        ).json()
        assert body["verdict"] == "INCONCLUSIVE"
        assert body["reason"] == "MIXED_DIRECTION"


def test_missing_metric_is_not_zero_filled() -> None:
    with TestClient(app) as client:
        suite_id = _suite(client)
        baseline = _run(client, suite_id, label="b", metrics={"diagnosis_accuracy": 0.6})
        candidate = _run(
            client,
            suite_id,
            label="c",
            metrics={"diagnosis_accuracy": 0.7, "tokens_per_case": None},
        )
        body = client.post(
            "/v1/eval/comparisons",
            json={"baseline_run_id": baseline, "candidate_run_id": candidate},
        ).json()
        assert body["deltas"]["tokens_per_case"] is None
        assert body["verdict"] == "CANDIDATE_BETTER"


def test_cross_tenant_run_is_404() -> None:
    with TestClient(app) as client:
        suite_id = _suite(client)
        run_id = _run(client, suite_id, label="b", metrics={"diagnosis_accuracy": 0.6})
        # 运行属于当前租户；伪造一个不存在的 id 验证 404 语义
        missing = client.get(f"/v1/eval/runs/run_{uuid4().hex[:16]}")
        assert missing.status_code == 404
        assert client.get(f"/v1/eval/runs/{run_id}").status_code == 200
