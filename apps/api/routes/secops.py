"""SecOps 多 Agent 报告 API（V3.0 / P4-T2）：跨栈 SARIF v2.1.0 导出与插件矩阵。

两个只读端点，统一要求 `report:read` 权限：

- ``POST /v1/secops/sarif``：把扫描结果渲染为标准 SARIF v2.1.0，
  同一份报告内可同时出现 ``db://<asset>`` 与 ``file://<path>#L<line>`` 位置，
  供 SIEM / SOAR / CI-CD 直接消费；
- ``GET /v1/secops/plugins``：返回已装配业务域插件的能力矩阵（SDK 契约版本 + 沙箱模式）。

安全边界：

- **不接受原始证据**：请求模型显式禁止 ``evidence`` 等字段（``extra="forbid"``），
  避免把未脱敏载荷经报告端点带出控制面；
- **不落盘**：以响应体返回，不写服务器文件；
- 报告属性只放租户 ID、工作流 ID 等标识，不含凭据。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_principal
from core.plugins.appsec.plugin import AppSecPlugin
from core.plugins.contracts import ProbeFinding, RemediationProposal, proposal_from_dict
from core.plugins.database.plugin import DatabasePlugin
from core.plugins.host.plugin import HostPlugin
from core.plugins.k8s.plugin import K8sPlugin
from core.plugins.registry import DomainPluginRegistry
from core.plugins.sandbox_manager import ALL_MODES
from core.plugins.sdk import CONTRACTS_VERSION, CrossStackSarifReporter, describe_plugin
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission

router = APIRouter(prefix="/v1/secops", tags=["secops"])

MAX_FINDINGS_PER_REPORT = 500


class FindingPayload(BaseModel):
    """报告用发现项：刻意**不含** evidence，避免未脱敏载荷外带。"""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    domain: str = Field(min_length=1, max_length=32)
    category: str = Field(min_length=1, max_length=64)
    rule_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    base_cvss: float = Field(ge=0.0, le=10.0)
    cve_id: str | None = Field(default=None, max_length=32)
    location: str = Field(default="", max_length=512)
    tags: tuple[str, ...] = ()
    suppressed_as_noise: bool = False

    def to_finding(self) -> ProbeFinding:
        return ProbeFinding(
            finding_id=self.finding_id,
            asset_id=self.asset_id,
            domain=self.domain,
            category=self.category,
            rule_id=self.rule_id,
            title=self.title,
            evidence="",
            base_cvss=self.base_cvss,
            cve_id=self.cve_id,
            location=self.location,
            tags=self.tags,
            suppressed_as_noise=self.suppressed_as_noise,
        )


class ProposalPayload(BaseModel):
    """提案摘要：只用于回填审批摘要、回滚状态与跨栈引用。"""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=128)
    finding_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    domain: str = Field(min_length=1, max_length=32)
    plugin_id: str = Field(default="", max_length=128)
    mitigation: str = Field(default="", max_length=4096)
    standard_fix: str = Field(default="", max_length=65536)
    upgrade_path: str = Field(default="", max_length=4096)
    rollback: dict[str, Any] = Field(default_factory=dict)
    impact: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    round: int = Field(default=1, ge=1, le=3)
    code_refs: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    def to_proposal(self) -> RemediationProposal:
        return proposal_from_dict(self.model_dump())


class SarifExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[FindingPayload] = Field(min_length=1, max_length=MAX_FINDINGS_PER_REPORT)
    proposals: list[ProposalPayload] = Field(default_factory=list, max_length=MAX_FINDINGS_PER_REPORT)
    run: dict[str, Any] = Field(default_factory=dict)


def build_default_plugin_registry() -> DomainPluginRegistry:
    """构造内置业务域插件矩阵（无 I/O、无凭据，仅用于能力发现）。

    说明：这里不装配多模态网关，因此这些实例的 ``*_evidence_masked`` 工具不可用；
    报告端点只读取插件能力声明，不需要网关。
    """
    return (
        DomainPluginRegistry()
        .register(DatabasePlugin())
        .register(K8sPlugin())
        .register(HostPlugin())
        .register(AppSecPlugin())
    )


@router.post("/sarif")
def export_sarif(
    request: SarifExportRequest,
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    """渲染跨栈 SARIF v2.1.0（同步返回，不落盘）。"""
    ensure_permission(principal, Permission.REPORT_READ)

    findings = [item.to_finding() for item in request.findings]
    proposals = {item.finding_id: item.to_proposal() for item in request.proposals}
    run_properties: dict[str, Any] = {
        "tenantId": principal.tenant_id,
        "requestedBy": principal.user_id,
        **{str(key): value for key, value in request.run.items()},
    }

    sarif = CrossStackSarifReporter().build(
        findings=findings,
        proposals=proposals,
        run_properties=run_properties,
    )
    return JSONResponse(
        content=sarif,
        media_type="application/sarif+json",
        headers={"Content-Disposition": 'attachment; filename="secops.sarif"'},
    )


@router.get("/plugins")
def list_plugins(
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """返回业务域插件能力矩阵（对接插件市场 / SOAR 资产发现）。"""
    ensure_permission(principal, Permission.REPORT_READ)
    registry = build_default_plugin_registry()
    return {
        "contracts_version": CONTRACTS_VERSION,
        "sandbox_modes": list(ALL_MODES),
        "domains": [describe_plugin(plugin) for plugin in registry.plugins()],
    }


__all__ = [
    "FindingPayload",
    "ProposalPayload",
    "SarifExportRequest",
    "build_default_plugin_registry",
    "export_sarif",
    "list_plugins",
    "router",
]
