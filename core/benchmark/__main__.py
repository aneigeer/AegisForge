"""``python -m core.benchmark``：W10 **离线评测 CLI**（不进生产镜像、不新增 API 端点）。

运行前提（设计 R-13）：在**仓库根目录**执行；``benchmarks/`` 是数据资产目录，
不在 ``pyproject.toml`` 的打包清单里，因此本模块与 ``runner.py`` 顶层
**不 import** ``benchmarks.*``（注入器/断言器一律函数体内惰性解析）。

子命令：

- ``plan --suite <path> --json``：只读冻结清单 + 冻结 Baseline 资产，输出
  **确定性 JSON**（无时间戳、无随机、两次运行逐字节相等），不连库、不写库；
- ``run --suite <path> --artifact-dir <dir>``：执行只读评测，产物落
  ``<artifact-dir>/<run_id>/``（``runs/benchmark/<run_id>/`` 已被 ``.gitignore``
  覆盖）。``--artifact-dir`` **必填**，避免隐式写盘。
- ``run --mode agent``：P1-T2 真实模型评测——注入故障 → 真实 ``AgentLoop`` 只读诊断 →
  任务**终态后**经 ``OracleGate`` 读 1 次真值 → 计算 14 项指标与四类归因，
  ``--report-out`` 可写出含 bootstrap 置信区间的 ``BaselineReport``。

``scan`` 属 T06 交付范围，本卡不提前实现。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from core.benchmark.manifest import RunManifest
from core.benchmark.runner import (
    ADAPTER_READ_TOOLS,
    BenchmarkRunner,
    DEFAULT_SUITE_PATH,
    RegistryToolObserver,
    derive_run_id,
    load_suite,
    plan_cases,
)
from core.errors import BenchmarkInvalidError
from core.security.canonical import canonical_digest
from core.tools.registry import ToolRegistry

DEFAULT_SUITE_ARG = str(Path("benchmarks") / "w10" / "suite_manifest.json")


class _DefinitionBinding:
    """把静态 ``ToolDefinition`` 包装成 ``ToolRegistry`` 可接受的工具对象。"""

    def __init__(self, definition: Any) -> None:
        self.definition = definition


class _HarnessOracle:
    """HARNESS 模式的 Oracle 端口：模型不可用时不读真值（设计 R-01 / D-08）。

    保留 ``OraclePort`` 形状，供 AGENT 模式组合根替换为真实 ``OracleGate``；
    这里连真值模型都不引用，避免顶层 import 击穿打包边界。
    """

    @property
    def records(self) -> tuple[Any, ...]:
        return ()

    def read(self, scenario_id: str, *, task_id: str, run_id: str) -> Any:
        raise BenchmarkInvalidError(
            "HARNESS 模式不读取 Oracle 真值（模型侧指标未测得）",
            details={"scenario_id": scenario_id, "task_id": task_id, "run_id": run_id},
        )


def _build_registry() -> ToolRegistry:
    """函数体内解析适配器 7 个固定只读工具（**不是** 10 工具 observer 面）。"""

    from adapters.postgres_mcp.tools import TOOL_DEFINITIONS  # noqa: PLC0415

    registry = ToolRegistry()
    for name in ADAPTER_READ_TOOLS:
        definition = TOOL_DEFINITIONS[name]
        if definition.side_effect != "read":
            raise BenchmarkInvalidError(
                "适配器工具面出现非只读定义",
                details={"tool_name": name, "side_effect": definition.side_effect},
            )
        registry.register(_DefinitionBinding(definition))
    return registry


def _build_agent_registry() -> ToolRegistry:
    """AGENT 模式使用真实 observer 工具面（全部 ``side_effect=read``）。"""

    from database.observers import build_observer_registry  # noqa: PLC0415

    registry = build_observer_registry()
    writes = [d.name for d in registry.definitions() if d.side_effect == "write"]
    if writes:
        raise BenchmarkInvalidError("AGENT 模式不得装配写工具", details={"tools": writes})
    return registry


def _agent_composition() -> tuple[Any, Any, dict[str, Any]]:
    """AGENT 组合根：控制库 + LAB 只读客户端 + 真实模型网关 + Oracle 闸门。

    只在 ``--mode agent`` 时装配；HARNESS 路径完全不碰数据库。
    """
    from core.agent.budget import AgentBudget  # noqa: PLC0415
    from core.agent.loop import AgentLoop  # noqa: PLC0415
    from core.agent.planner import Planner  # noqa: PLC0415
    from core.agent.trajectory import TrajectoryStore  # noqa: PLC0415
    from core.benchmark.agent_mode import AgentLoopCaseExecutor  # noqa: PLC0415
    from core.benchmark.oracle import OracleGate  # noqa: PLC0415
    from core.config import Settings  # noqa: PLC0415
    from core.context.builder import ContextBuilder  # noqa: PLC0415
    from core.models.factory import build_model_gateway  # noqa: PLC0415
    from core.tasks.repository import TaskRepository  # noqa: PLC0415
    from core.tasks.service import TaskService  # noqa: PLC0415
    from core.tools.contracts import ToolContext  # noqa: PLC0415
    from database.mysql.connection import MysqlClient  # noqa: PLC0415
    from database.observers import build_observer_registry  # noqa: PLC0415

    settings = Settings()
    control = MysqlClient(
        settings.control_mysql_dsn(),
        database_id="dg_system",
        read_only=False,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    ro_client = MysqlClient(
        settings.ro_dsn.get_secret_value(),
        database_id=settings.database_id,
        read_only=True,
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_ms=settings.statement_timeout_ms,
        max_result_rows=settings.max_result_rows,
    )
    registry = build_observer_registry()
    service = TaskService(TaskRepository(control), registry)
    # P1-T2-CLOSE：本地 7B 单次计划调用实测 14–28s，`DG_MODEL_TIMEOUT_S=30`
    # 在抖动下会误判超时。评测组合根给一个**下界**（不改生产默认值），
    # 超时仍强制生效，只把"正常但慢"的调用与真正的挂死区分开。
    effective = settings.model_copy(
        # 300s：7B 在 2048-token 计划上实测可达 100s+，120s 仍会误判；仍为硬上限。
        update={
            "model_timeout_s": max(float(settings.model_timeout_s), 300.0),
            # 流式：超时即断连，避免单 worker 端点被"已超时但仍在生成"的请求堵住。
            "model_stream": True,
        }
    )
    gateway = build_model_gateway(effective)

    def _build_loop(
        task_id: str,
        *,
        registry_for_engine: Any,
        client: Any,
        permissions: list[str],
        service_for_loop: Any = None,
    ) -> Any:
        # 工具执行走 `TaskService.run_tool`，而 service 绑定的是**构造期**的注册表：
        # 只换 loop 的 registry 不够（会拿 MySQL 工具去打 PG → 42P01）。
        loop_service = service_for_loop if service_for_loop is not None else service
        planner = Planner(
            gateway,
            registry_for_engine,
            max_steps=6,
            max_tool_calls=6,
            plan_max_tokens=1024,  # 见 §23：本地 7B 长输出会阻塞单 worker 队列
        )
        return AgentLoop(
            loop_service,
            registry_for_engine,
            gateway,
            planner,
            ContextBuilder(
                registry_for_engine,
                max_evidence_items=4,
                max_summary_chars=500,
                max_prompt_chars=7000,
                permissions=permissions,
            ),
            context_factory=lambda tid: ToolContext(client=client, task_id=tid),
            budget=AgentBudget(
                max_steps=6,
                max_tool_calls=6,
                max_model_calls=8,
                max_tokens=60_000,
                max_seconds=300,
            ),
            trajectory_store=TrajectoryStore(Path(settings.trajectory_dir)),
        )

    def _loop_factory(task_id: str) -> Any:
        # P1-T2 实测：7B 在 4 步预算内常无法收口（budget_exceeded / 上下文溢出），
        # 故按"多一步余量 + 更紧的 prompt 预算"配置；生产旗舰模型可放宽。
        return _build_loop(
            task_id,
            registry_for_engine=registry,
            client=ro_client,
            permissions=["mysql:read"],
        )

    # P2-T1：PG 引擎的独立组合根（工具面/权限/客户端全部换，其余预算一致）。
    pg_registry = None
    pg_client = None
    pg_factory = None
    if settings.pg_ro_dsn is not None:
        from adapters.postgres_mcp.registry import build_pg_read_registry  # noqa: PLC0415
        from database.postgres.connection import PgClient  # noqa: PLC0415

        try:
            pg_registry = build_pg_read_registry()
            pg_service = TaskService(TaskRepository(control), pg_registry)
            pg_client = PgClient(
                settings.pg_ro_dsn.get_secret_value(),
                database_id=settings.pg_database_id,
                read_only=True,
                connect_timeout_s=settings.connect_timeout_s,
                statement_timeout_ms=settings.statement_timeout_ms,
                # `get_query_stats` 需要扫 1001 行来判断是否截断；行数上限 500 会让它
                # 恒定返回 PARTIAL，进而被 Loop 当作失败（实测 LOOP_tool_partial）。
                max_result_rows=max(int(settings.max_result_rows), 2048),
            )
            pg_client.fetch_all("SELECT 1 AS ok")
        except Exception:  # noqa: BLE001 - PG 不可达时保持 ENV-PG5432 口径（不降级猜测）
            pg_registry = None
            pg_client = None
        else:
            pg_factory = lambda tid: _build_loop(  # noqa: E731
                tid,
                registry_for_engine=pg_registry,
                client=pg_client,
                permissions=["postgresql:read"],
                service_for_loop=pg_service,
            )

    executor = AgentLoopCaseExecutor(
        service=service,
        loop_factory=_loop_factory,
        registry=registry,
        gateway=gateway,
        engine_loop_factories={"postgresql": pg_factory} if pg_factory else None,
    )
    oracle = OracleGate(status_reader=lambda task_id: service.get_task(task_id).status)

    adapter = getattr(gateway, "adapter", None)
    default_adapter = getattr(adapter, "_default", adapter)
    model_version = str(getattr(default_adapter, "model_version", "") or "")
    def _resolve_blocker(case: Any) -> str | None:
        """实测解除：PG 可达时 `ENV-PG5432` 不再成立；其余阻塞照原样保留。"""
        declared = getattr(case, "env_blocked_reason", None)
        if declared == "ENV-PG5432" and pg_client is not None:
            if not _engine_alive(pg_client):
                return declared
            return None
        return declared

    def _engine_alive(client: Any) -> bool:
        try:
            return bool(client.fetch_all("SELECT 1 AS ok"))
        except Exception:  # noqa: BLE001 - 探活失败即视为不可用
            return False

    config: dict[str, Any] = {
        "model_name": settings.model_name,
        "model_version": model_version or None,
        "provider": settings.model_provider,
        "blocker_resolver": _resolve_blocker,
    }
    return executor, oracle, config


def _plan_payload(suite_path: Path) -> dict[str, Any]:
    """纯函数：构造可复现的 plan JSON（无时间戳 / 无随机 / 不连库）。"""

    suite = load_suite(suite_path)
    manifest = RunManifest.from_baseline(suite_version=suite.suite_version)
    cases = plan_cases(suite)
    case_rows = [asdict(item) for item in cases]
    digest_input: list[list[Any]] = [
        [
            item.case_id,
            item.engine,
            item.scenario,
            item.fault_seed,
            item.repetition,
            list(item.tool_plan),
        ]
        for item in cases
    ]
    return {
        "command": "plan",
        "manifest_version": manifest.manifest_version,
        "suite_version": manifest.suite_version,
        "testset_version": manifest.testset_version,
        "harness_version": manifest.harness_version,
        "model_name": manifest.model_name,
        "model_version": manifest.model_version,
        "env_image_digest": manifest.env_image_digest,
        "env_image_digest_source": manifest.env_image_digest_source,
        "seed": manifest.seed,
        "budget": manifest.budget.model_dump(mode="json"),
        "execution_mode": "HARNESS",
        "tool_plan": list(suite.tool_plan),
        "case_count": len(cases),
        "cases": case_rows,
        "plan_digest": canonical_digest(digest_input),
    }


def _cmd_plan(args: argparse.Namespace) -> int:
    payload = _plan_payload(Path(args.suite))
    if args.json:
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
    else:
        sys.stdout.write(
            f"plan suite={payload['suite_version']} seed={payload['seed']} "
            f"cases={payload['case_count']} digest={payload['plan_digest']}\n"
        )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite)
    suite = load_suite(suite_path)
    mode = str(getattr(args, "mode", "harness"))
    case_executor: Any | None = None
    oracle: Any = _HarnessOracle()
    overrides: dict[str, Any] = {}
    model_config: dict[str, Any] = {}
    if mode == "agent":
        case_executor, oracle, model_config = _agent_composition()
        overrides = {
            "model_name": model_config["model_name"],
            "model_version": model_config["model_version"],
            "notes": (
                f"P1-T2 AGENT 模式；provider={model_config['provider']}；"
                "Oracle 仅在任务终态后按 (run_id, scenario) 读取一次。",
            ),
        }
    manifest = RunManifest.from_baseline(suite_version=suite.suite_version, **overrides)
    registry = _build_registry() if mode == "harness" else _build_agent_registry()
    artifact_dir = Path(args.artifact_dir) / derive_run_id(manifest)
    runner = BenchmarkRunner(
        manifest=manifest,
        registry=registry,
        observer=RegistryToolObserver(registry),
        oracle=oracle,
        artifact_dir=artifact_dir,
        case_executor=case_executor,
        execution_mode="AGENT" if mode == "agent" else "HARNESS",
        blocker_resolver=(
            case_executor is not None and mode == "agent"
        ) and model_config.get("blocker_resolver") or None,
    )
    total = len(plan_cases(suite))
    counter = {"done": 0}

    def _progress(case: Any) -> None:
        counter["done"] += 1
        sys.stderr.write(
            f"[case {counter['done']}/{total}] {case.case_id} r{case.repetition} "
            f"mode={case.execution_mode} task={case.task_status.value if case.task_status else 'NONE'} "
            f"rca={case.rca_match} abstain={case.abstained} "
            f"latency={case.latency_ms}ms reason={case.failure_reason or '-'}"
        )
        sys.stderr.write("\n")
        sys.stderr.flush()

    result = runner.run(progress=_progress)

    report_path: str | None = None
    if mode == "agent":
        from core.benchmark.metrics import MetricsEngine  # noqa: PLC0415
        from core.benchmark.report import build_baseline_report  # noqa: PLC0415

        metrics = MetricsEngine.from_file().compute(result.cases)
        report = build_baseline_report(result=result, metrics=metrics, manifest=manifest)
        target = (
            Path(args.report_out)
            if getattr(args, "report_out", None)
            else artifact_dir / "baseline_report.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        report_path = str(target)

    payload = {
        "command": "run",
        "run_id": result.run_id,
        "status": result.status.value,
        "execution_mode": result.execution_mode,
        "case_count": len(result.cases),
        "scores": result.scores,
        "metrics": result.metrics,
        "cancel_reason": result.cancel_reason,
        "failure_reason": result.failure_reason,
        "artifact_dir": str(artifact_dir),
        "report": report_path,
    }
    if args.json:
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
    else:
        sys.stdout.write(
            f"run status={payload['status']} mode={payload['execution_mode']} "
            f"cases={payload['case_count']} artifacts={payload['artifact_dir']}"
            + (f" report={report_path}" if report_path else "")
            + "\n"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.benchmark",
        description="DBGuardian W10 离线评测工具（不进生产镜像、不新增 API 端点）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="输出确定性评测计划（不连库、不写库）")
    plan.add_argument("--suite", default=DEFAULT_SUITE_ARG, help="冻结测试集清单路径")
    plan.add_argument("--json", action="store_true", help="输出确定性 JSON")
    plan.set_defaults(handler=_cmd_plan)

    run = subparsers.add_parser("run", help="执行只读评测并落盘产物")
    run.add_argument("--suite", default=DEFAULT_SUITE_ARG, help="冻结测试集清单路径")
    # 显式 --artifact-dir：不允许隐式写盘（任务卡 :210）。
    run.add_argument("--artifact-dir", required=True, help="产物父目录")
    run.add_argument(
        "--mode",
        choices=("harness", "agent"),
        default="harness",
        help="harness=固定工具序列；agent=真实 AgentLoop + 真实模型（P1-T2）",
    )
    run.add_argument(
        "--report-out",
        default=None,
        help="AGENT 模式 BaselineReport 的落位（默认 <artifact-dir>/baseline_report.json）",
    )
    run.add_argument("--json", action="store_true", help="输出结构化 JSON")
    run.set_defaults(handler=_cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
