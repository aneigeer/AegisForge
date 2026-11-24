"""P5-T1：评测中台（持久化 + 候选对比，只信实测）。

V4.1 起新增 :mod:`core.eval.agent_benchmark`：通用 Agent 护栏/防泄漏评测引擎，
指标口径与本模块的 :func:`compare_metrics` 完全对齐。
"""

from core.eval.agent_benchmark import (
    AgentEvalCase,
    AgentEvalMetrics,
    build_reference_runner,
    compare_agent_variants,
    load_agent_eval_suite,
    run_agent_eval_suite,
)
from core.eval.repository import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    ComparisonVerdict,
    EvalRepository,
    compare_metrics,
)

__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "AgentEvalCase",
    "AgentEvalMetrics",
    "ComparisonVerdict",
    "EvalRepository",
    "build_reference_runner",
    "compare_agent_variants",
    "compare_metrics",
    "load_agent_eval_suite",
    "run_agent_eval_suite",
]
