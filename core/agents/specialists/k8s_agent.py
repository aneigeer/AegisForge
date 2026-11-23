"""K8s 领域专家 Agent（K8s-Agent）：Pod 特权逃逸 / RBAC / NetworkPolicy 加固。"""

from __future__ import annotations

from core.agents.specialists.base_specialist import DomainSpecialistAgent


class K8sSpecialistAgent(DomainSpecialistAgent):
    agent_id = "AGENT_K8S_SPECIALIST"
    domain = "k8s"


__all__ = ["K8sSpecialistAgent"]
