r"""W04 演示：一次多轮只读锁等待诊断（默认使用确定性 fake 模型）。

运行：
    .\.venv\Scripts\python.exe scripts\demo_agent_run.py

注意：
- fake 适配器只产出可校验的计划/决策，不虚构根因或指标；
- 轨迹写入 DG_TRAJECTORY_DIR（默认 runs/）；
- 如需真实模型，设置 DG_MODEL_PROVIDER=openai_compat 及 BASE_URL/API_KEY。
"""

from __future__ import annotations

import json

from core.agent.budget import AgentBudget
from core.agent.loop import AgentLoop
from core.agent.planner import Planner
from core.agent.trajectory import TrajectoryStore
from core.config import get_settings
from core.context.builder import ContextBuilder
from core.models.factory import build_model_gateway
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from database.mysql.connection import MysqlClient
from database.observers import build_observer_registry


def main() -> int:
    settings = get_settings()
    registry = build_observer_registry()
    rw_client = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    ro_client = MysqlClient(
        settings.ro_dsn,
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )

    service = TaskService(TaskRepository(rw_client), registry)
    gateway = build_model_gateway(settings)
    budget = AgentBudget.from_settings(settings)
    planner = Planner(
        gateway,
        registry,
        max_steps=budget.max_steps,
        max_tool_calls=budget.max_tool_calls,
    )
    loop = AgentLoop(
        service,
        registry,
        gateway,
        planner,
        ContextBuilder(registry),
        context_factory=lambda task_id: ToolContext(client=ro_client, task_id=task_id),
        budget=budget,
        trajectory_store=TrajectoryStore(settings.trajectory_dir),
        max_duplicate_calls=settings.agent_max_duplicate_calls,
        no_progress_threshold=settings.agent_no_progress_threshold,
    )

    task = service.create_task("数据库出现锁等待：订单表更新被阻塞，请只读诊断当前锁与阻塞链")
    result = loop.run(task.task_id)
    summary = {
        "task_id": result.task.task_id,
        "status": result.task.status.value,
        "stop_reason": result.stop_reason,
        "model_version": result.model_version,
        "tool_calls": result.tool_calls,
        "model_calls": result.model_calls,
        "usage": result.usage,
        "conclusion": result.conclusion,
        "trajectory_path": result.trajectory_path,
        "evidence_count": len(result.evidence),
        "evidence": [
            {
                "tool": item.tool_name,
                "status": item.status,
                "summary": item.summary[:300],
            }
            for item in result.evidence
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if result.task.status.value == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
