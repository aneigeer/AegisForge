"""V5.0 有界递归自我改进（Eval-Gated Bounded RSI）的进化基础设施。

模块分层（刻意与不可变安全内核物理隔离）：

- :mod:`core.evolution.strategy_store`  可版本化策略包 + 宪法安全锁（唯一可自我改写面）
- :mod:`core.evolution.proposer`        失败归因与元经验感知的改进提案器
- :mod:`core.evolution.runtime_adapter` 策略包 -> 运行时策略/评测运行器热绑定
- :mod:`core.evolution.shadow_evaluator` Train/Holdout 影子门控
- :mod:`core.evolution.rsi_loop`        有界多代闭环调度器

**不在此包内**：``core/guardrails/``、``core/security/``、``core/audit/``、
``core/orchestrator/state_graph.py`` 属于安全宪法，任何自我改写路径都不得触碰。
"""

from core.evolution.strategy_store import (
    AgentStrategyBundle,
    ConstitutionalViolationError,
    StrategyStore,
    assert_constitutional_safe,
    default_seed_bundle,
)

__all__ = [
    "AgentStrategyBundle",
    "ConstitutionalViolationError",
    "StrategyStore",
    "assert_constitutional_safe",
    "default_seed_bundle",
]
