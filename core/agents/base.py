"""多 Agent 公共契约：执行上下文、角色标识与 WORM 审计快捷方法。

角色标识一律为**纯职能名**（总控调度 / 研判 / 领域专家 / 红队验证 / 进化蒸馏），
不含任何虚拟人名；桌面端只展示这些职能标签与 RBAC 角色（高级 DBA / 安全工程师 / 合规审计员）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.orchestrator.state_graph import MAX_SELF_CORRECTION_ROUNDS, SecOpsState
from core.orchestrator.tool_client import McpToolClient
from core.platform.ten_modules import AuditEcosystemExporter, RbacRole
from core.plugins.contracts import TargetAsset
from core.plugins.registry import DomainPluginRegistry
from core.privacy_gateway.multimodal import MultimodalPrivacyGateway

#: Agent ID -> 桌面端展示用纯角色标识。
AGENT_ROLE_LABELS: dict[str, str] = {
    "AGENT_SUPERVISOR": "总控调度 Agent",
    "AGENT_TRIAGE": "研判定级 Agent",
    "AGENT_DB_SPECIALIST": "DB 领域专家 Agent",
    "AGENT_K8S_SPECIALIST": "K8s 领域专家 Agent",
    "AGENT_HOST_SPECIALIST": "Host 领域专家 Agent",
    "AGENT_APPSEC_SPECIALIST": "AppSec 领域专家 Agent",
    "AGENT_CRITIC": "红队审查与沙箱验证 Agent",
    "AGENT_EVOLUTION": "自主进化蒸馏 Agent",
}

#: 业务域 -> 领域专家 Agent ID。
DOMAIN_AGENT_IDS: dict[str, str] = {
    "database": "AGENT_DB_SPECIALIST",
    "k8s": "AGENT_K8S_SPECIALIST",
    "host": "AGENT_HOST_SPECIALIST",
    "appsec": "AGENT_APPSEC_SPECIALIST",
}


@dataclass
class AgentContext:
    """一次工作流运行内所有 Agent 共享的依赖注入容器（不含凭据）。"""

    workflow_run_id: str
    tenant_id: str
    tools: McpToolClient
    gateway: MultimodalPrivacyGateway
    plugins: DomainPluginRegistry
    audit: AuditEcosystemExporter
    max_self_correction_rounds: int = MAX_SELF_CORRECTION_ROUNDS
    max_branches: int = 4

    def __post_init__(self) -> None:
        if self.max_self_correction_rounds > MAX_SELF_CORRECTION_ROUNDS:
            raise ValueError(f"max_self_correction_rounds 不得超过 {MAX_SELF_CORRECTION_ROUNDS}")


class BaseAgent:
    """Agent 基类：只提供审计与状态注记能力，不隐藏业务逻辑。"""

    agent_id: str = "AGENT_BASE"

    def __init__(self, context: AgentContext) -> None:
        self.ctx = context

    @property
    def role_label(self) -> str:
        return AGENT_ROLE_LABELS.get(self.agent_id, self.agent_id)

    def worm(self, event_type: str, payload: dict[str, Any], *, actor_role: str | None = None) -> dict[str, Any]:
        return self.ctx.audit.append_worm_event(
            event_type,
            actor_role or RbacRole.SEC.value,
            {"workflow_run_id": self.ctx.workflow_run_id, "agent_id": self.agent_id, **payload},
        )

    @staticmethod
    def note(state: SecOpsState, text: str) -> None:
        state.notes.append(f"[{state.self_correction_round}] {text}")


def asset_from_dict(data: dict[str, Any]) -> TargetAsset:
    """把状态里的资产字典还原为不可变 :class:`TargetAsset`（已是目标类型则原样返回）。"""
    if isinstance(data, TargetAsset):
        return data
    labels = data.get("labels") or {}
    if isinstance(labels, dict):
        label_items = tuple((str(k), str(v)) for k, v in labels.items())
    else:
        label_items = tuple((str(k), str(v)) for k, v in labels)
    return TargetAsset(
        asset_id=str(data["asset_id"]),
        domain=str(data.get("domain", "")),
        engine=str(data.get("engine", "")),
        version=str(data.get("version", "")),
        business_tier=str(data.get("business_tier", "INTERNAL_BI")),
        network_exposure=str(data.get("network_exposure", "INTERNAL_VPC")),
        pii_density=str(data.get("pii_density", "LOW")),
        endpoint=str(data.get("endpoint", "")),
        labels=label_items,
    )


__all__ = [
    "AGENT_ROLE_LABELS",
    "DOMAIN_AGENT_IDS",
    "AgentContext",
    "BaseAgent",
    "asset_from_dict",
]
