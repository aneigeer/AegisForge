"""V3.0 多 Agent 协作层：五类核心 Agent + 领域专家 Agent 群。"""

from __future__ import annotations

from core.agents.base import AGENT_ROLE_LABELS, DOMAIN_AGENT_IDS, AgentContext, BaseAgent, asset_from_dict
from core.agents.critic import CriticAgent
from core.agents.evolution import EvolutionAgent
from core.agents.supervisor import SupervisorAgent
from core.agents.triage import TriageAgent

__all__ = [
    "AGENT_ROLE_LABELS",
    "AgentContext",
    "BaseAgent",
    "CriticAgent",
    "DOMAIN_AGENT_IDS",
    "EvolutionAgent",
    "SupervisorAgent",
    "TriageAgent",
    "asset_from_dict",
]
