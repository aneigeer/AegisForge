"""AppSec 领域专家 Agent（AppSec-Agent）：源码审计 + 代码 Patch + 联合 DB 加固。"""

from __future__ import annotations

from core.agents.specialists.base_specialist import DomainSpecialistAgent


class AppSecSpecialistAgent(DomainSpecialistAgent):
    agent_id = "AGENT_APPSEC_SPECIALIST"
    domain = "appsec"


__all__ = ["AppSecSpecialistAgent"]
