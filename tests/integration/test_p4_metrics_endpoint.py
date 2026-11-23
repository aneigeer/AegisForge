"""P4-T3 集成测试：`/metrics` 可抓取且出口干净。

断言：

- 端点返回 200 且是 Prometheus 文本格式（`# TYPE` 行存在）；
- 预声明指标齐全；
- 响应体不含 DSN、SQL 字面量片段或 BYOK key 形态。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app

pytestmark = pytest.mark.integration


def test_metrics_endpoint_is_scrapable_and_clean() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "# TYPE dg_tool_calls_total counter" in body
        assert "# TYPE dg_model_latency_seconds histogram" in body
        assert not re.search(r"[a-zA-Z][a-zA-Z0-9+.\-]*://", body), "不得出现连接串"
        assert not re.search(r"\bsk-[A-Za-z0-9_\-]{6,}", body), "不得出现 API key"
        assert "SELECT " not in body.upper()
