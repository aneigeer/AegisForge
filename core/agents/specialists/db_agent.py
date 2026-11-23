"""DB 领域专家 Agent（DB-Agent）。

职责：把工单转成「三层修复方案 + 强制 Rollback + 影响评估」，并在被 Critic 打回后
按结构化失败码做 Self-Correction 重提交。

分工边界（避免"专家 Agent 变成万能脚本"）：

- 具体 SQL 生成、锁超时注入、沙箱验证全部由 ``database`` 插件提供（经 MCP 工具调用）；
- 本 Agent 只做：轮次管理、失败码透传、提交前自检、状态与审计写入；
- 提交前自检项与 Critic 的红队检查同源，减少"提交就知道会被打回"的空转。
"""

from __future__ import annotations

from core.agents.specialists.base_specialist import DomainSpecialistAgent


class DbSpecialistAgent(DomainSpecialistAgent):
    """database 域专家：生成修复提案并接受对抗验证。"""

    agent_id = "AGENT_DB_SPECIALIST"
    domain = "database"


__all__ = ["DbSpecialistAgent"]
