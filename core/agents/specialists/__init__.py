"""领域专家 Agent 群（可水平扩展）。"""

from __future__ import annotations

from core.agents.specialists.appsec_agent import AppSecSpecialistAgent
from core.agents.specialists.base_specialist import DomainSpecialistAgent
from core.agents.specialists.db_agent import DbSpecialistAgent
from core.agents.specialists.host_agent import HostSpecialistAgent
from core.agents.specialists.k8s_agent import K8sSpecialistAgent

__all__ = [
    "AppSecSpecialistAgent",
    "DbSpecialistAgent",
    "DomainSpecialistAgent",
    "HostSpecialistAgent",
    "K8sSpecialistAgent",
]
