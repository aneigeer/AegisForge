"""V3.0 Phase 4 API 单元测试：跨栈 SARIF 导出端点与插件能力矩阵。

验收点（对应 ``P4-T2`` 的 ``apps/api/routes/`` 部分）：

- ``POST /v1/secops/sarif`` 返回标准 SARIF v2.1.0（``application/sarif+json``）；
- 同一报告内同时包含 ``db://`` 与 ``file://#L`` 位置，并回填审批摘要与自愈轮次；
- 缺少 ``report:read`` 权限返回 403；
- 请求模型禁止携带 ``evidence``（防止未脱敏载荷经报告端点外带）；
- ``GET /v1/secops/plugins`` 返回四域插件能力矩阵。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.main import create_app
from apps.api.routes.secops import SarifExportRequest
from apps.api.security import get_principal
from core.security.models import Principal
from core.security.roles import Role

TENANT = "tenant_api"


def _principal(role: Role = Role.AUDITOR) -> Principal:
    return Principal(user_id="user_api", tenant_id=TENANT, roles=(role,))


def _client(role: Role = Role.AUDITOR) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_principal] = lambda: _principal(role)
    return TestClient(app)


def _payload() -> dict[str, Any]:
    return {
        "findings": [
            {
                "finding_id": "F-DB",
                "asset_id": "pg-prod-trade-01",
                "domain": "database",
                "category": "CVE_MATCH",
                "rule_id": "PG-CVE-2024-7348",
                "title": "PostgreSQL SECURITY DEFINER 提权",
                "base_cvss": 8.8,
                "cve_id": "CVE-2024-7348",
                "location": "db://pg-prod-trade-01",
            },
            {
                "finding_id": "F-APP",
                "asset_id": "app-api-01",
                "domain": "appsec",
                "category": "SQL_INJECTION",
                "rule_id": "APP-SQLI-01",
                "title": "SQL 语句拼接外部输入",
                "base_cvss": 9.1,
                "location": "file://src/api/orders.py#L7",
            },
            {
                "finding_id": "F-NOISE",
                "asset_id": "pg-bi-report-03",
                "domain": "database",
                "category": "CIS_BASELINE",
                "rule_id": "CIS-PG16-2.1",
                "title": "内网报表库 banner 轻微暴露",
                "base_cvss": 3.1,
                "location": "db://pg-bi-report-03",
                "suppressed_as_noise": True,
            },
        ],
        "proposals": [
            {
                "proposal_id": "prop-app-1",
                "finding_id": "F-APP",
                "asset_id": "app-api-01",
                "domain": "appsec",
                "plugin_id": "plugin.appsec",
                "mitigation": "WAF 临时拦截",
                "standard_fix": '--- a/src/api/orders.py\n+++ b/src/api/orders.py\n@@ -7,1 +7,1 @@\n-a\n+b',
                "rollback": {
                    "kind": "PATCH",
                    "payload": '--- a/src/api/orders.py\n+++ b/src/api/orders.py\n@@ -7,1 +7,1 @@\n-b\n+a',
                    "timeout_sec": 120,
                    "atomic": True,
                    "reversible": True,
                },
                "impact": {"lock_impact_level": "NONE", "requires_restart": True},
                "round": 2,
                "code_refs": [{"kind": "db_sql", "asset_id": "pg-prod-trade-01", "sql": "REVOKE ALL PRIVILEGES ON orders FROM app_rw;"}],
            }
        ],
        "run": {"workflowRunId": "wf-api-1", "sourceTrigger": "CI_PR_GATE"},
    }


def test_sarif_endpoint_returns_cross_stack_report() -> None:
    with _client() as client:
        response = client.post("/v1/secops/sarif", json=_payload())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sarif+json")
    assert "secops.sarif" in response.headers["content-disposition"]

    sarif = response.json()
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["properties"]["tenantId"] == TENANT
    assert run["properties"]["workflowRunId"] == "wf-api-1"
    assert run["properties"]["sourceTrigger"] == "CI_PR_GATE"

    # 噪音项被过滤，只保留 2 条结果
    assert len(run["results"]) == 2
    by_rule = {item["ruleId"]: item for item in run["results"]}
    assert set(by_rule) == {"PG-CVE-2024-7348", "APP-SQLI-01"}

    app_result = by_rule["APP-SQLI-01"]
    physical = app_result["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "src/api/orders.py"
    assert physical["region"]["startLine"] == 7
    assert len(app_result["properties"]["approvalDigest"]) == 64
    assert app_result["properties"]["rollbackArmed"] is True
    assert app_result["properties"]["selfCorrectionRound"] == 2
    assert app_result["properties"]["crossStack"][0]["kind"] == "db_sql"

    db_result = by_rule["PG-CVE-2024-7348"]
    assert db_result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "db://pg-prod-trade-01"
    assert "region" not in db_result["locations"][0]["physicalLocation"]


def test_sarif_endpoint_requires_report_permission() -> None:
    with _client(Role.SERVICE_WORKER) as client:
        response = client.post("/v1/secops/sarif", json=_payload())

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["error_code"] == "FORBIDDEN"


def test_plugins_endpoint_lists_four_domains() -> None:
    with _client() as client:
        response = client.get("/v1/secops/plugins")

    assert response.status_code == 200
    body = response.json()
    assert body["contracts_version"] == "3.0.0"
    assert len(body["sandbox_modes"]) == 5
    domains = {item["domain_id"] for item in body["domains"]}
    assert domains == {"database", "k8s", "host", "appsec"}
    for item in body["domains"]:
        assert item["plugin_id"].startswith("plugin.")
        assert item["capabilities"]


def test_sarif_request_rejects_raw_evidence_field() -> None:
    payload = _payload()
    payload["findings"][0]["evidence"] = "13812345678 明文手机号"
    with pytest.raises(ValidationError):
        SarifExportRequest.model_validate(payload)


def test_sarif_request_requires_at_least_one_finding() -> None:
    with pytest.raises(ValidationError):
        SarifExportRequest.model_validate({"findings": []})
