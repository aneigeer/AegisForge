"""Host 领域专家 Agent（Host-Agent）：主机基线 / SSH / 内核 CVE 加固。"""

from __future__ import annotations

from core.agents.specialists.base_specialist import DomainSpecialistAgent


class HostSpecialistAgent(DomainSpecialistAgent):
    agent_id = "AGENT_HOST_SPECIALIST"
    domain = "host"


__all__ = ["HostSpecialistAgent"]
