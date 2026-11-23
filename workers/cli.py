"""`python -m workers` 的命令行入口。

退出码约定（供 systemd / docker / 运维脚本判断，不用于业务逻辑）：

- `0`：干净退出（没有在途未收敛的候选，且本进程没遇到跳过/失败/未知结果）；
- `1`：有在途未收敛的候选（被他人持锁、执行被拒、失败或结果未知）；
- `2`：配置或自检失败（缺 DSN、控制库不可达、环境事实缺失等）。

安全：本模块的所有输出都经 `core.logging.redact_text` 脱敏，且**从不**打印参数值
（只打印参数键名与数量），也不打印 DSN / 签名密钥。
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from typing import Any, Sequence

from core.config import Settings, get_settings
from core.errors import ConfigError, DBGuardianError
from core.logging import configure_logging, get_logger, redact_text
from workers.discovery import discover_candidates
from workers.runner import RunReport, WorkerRunner
from workers.stack import WORKERS_VERSION, WorkerStack, build_worker_stack

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_OUTSTANDING = 1
EXIT_CONFIG_ERROR = 2

_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m workers",
        description=(
            "DBGuardian 独立 Worker：轮询已批准的执行候选，"
            "经唯一 Tool Gateway 执行并收敛结果。"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只处理一轮候选后退出（用于验收与定时任务）",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="装配执行栈并自检连接/环境/候选数量，打印 JSON 后退出",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只发现并列出候选（不领租约、不提交工具、不写审计）",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        metavar="ID",
        help="覆盖 DG_WORKER_ID（租约归属与审计标注）",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        metavar="TENANT",
        help="覆盖 DG_DEFAULT_TENANT_ID（只处理该租户的候选）",
    )
    return parser


def _print(payload: dict[str, Any]) -> None:
    """统一输出：JSON + 脱敏（不会出现参数值 / DSN / token）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    sys.stdout.write(redact_text(text) + "\n")
    sys.stdout.flush()


def _install_signal_handlers(runner: WorkerRunner) -> bool:
    """注册 SIGINT/SIGTERM；仅在主线程可用（测试里会返回 False）。"""
    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for signum in _SIGNALS:
        try:
            signal.signal(signum, lambda received, _frame: runner.request_stop(received))
        except (ValueError, OSError):  # pragma: no cover - 平台差异/受限环境
            continue
        installed = True
    return installed


def _error_payload(stage: str, exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": "worker_error",
        "stage": stage,
        "ok": False,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = getattr(code, "value", str(code))
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        # details 是结构化事实（字段名/原因码），不含凭据；仍统一脱敏。
        payload["details"] = details
    return payload


def _report_payload(report: RunReport, *, outstanding: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": "worker_report", "ok": report.clean}
    payload.update(report.to_public_dict())
    payload["outstanding"] = outstanding
    return payload


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    """候选的脱敏摘要：只暴露标识与参数**键名**，绝不暴露参数值。"""
    return {
        "task_id": candidate.task_id,
        "approval_id": candidate.approval_id,
        "tool_name": candidate.tool_name,
        "step_id": candidate.step_id,
        "task_status": candidate.task.status.value,
        "param_keys": sorted(str(key) for key in candidate.params),
        "param_count": len(candidate.params),
        "requested_by": candidate.requested_by,
    }


def _self_check(stack: WorkerStack) -> int:
    checks: dict[str, Any] = {}
    ok = True
    for name, client in (
        ("control", stack.control_client),
        ("lab_ro", stack.ro_client),
        ("lab_rw", stack.rw_client),
    ):
        try:
            latency_ms = client.ping()
            checks[name] = {"ok": True, "latency_ms": latency_ms}
        except DBGuardianError as exc:
            ok = False
            checks[name] = {
                "ok": False,
                "code": exc.code.value,
                "error_type": type(exc).__name__,
            }
    try:
        candidates = discover_candidates(stack, limit=1)
        checks["discovery"] = {"ok": True, "candidates": len(candidates)}
    except DBGuardianError as exc:
        ok = False
        checks["discovery"] = {"ok": False, "code": exc.code.value}
    checks["lab_write_enabled"] = stack.lab_write_enabled
    if not stack.lab_write_enabled:
        # 非 LAB 环境装配出的栈不会注册写工具：这本身不是错误，但与"能执行修复"不符。
        checks["warning"] = "环境事实不是 LAB+mysql，写工具未注册"
    _print(
        {
            "event": "worker_self_check",
            "ok": ok,
            "version": WORKERS_VERSION,
            "worker_id": stack.worker_id,
            "tenant_id": stack.tenant_id,
            "environment_id": stack.environment_id,
            "environment_status": str(stack.environment_row.get("status") or ""),
            "db_engine": str(stack.environment_row.get("db_engine") or ""),
            "checks": checks,
        }
    )
    return EXIT_OK if ok else EXIT_CONFIG_ERROR


def _dry_run(stack: WorkerStack, runner: WorkerRunner) -> int:
    candidates = discover_candidates(stack, limit=10)
    _print(
        {
            "event": "worker_dry_run",
            "ok": True,
            "worker_id": stack.worker_id,
            "tenant_id": stack.tenant_id,
            "environment_id": stack.environment_id,
            "candidate_count": len(candidates),
            "candidates": [_candidate_payload(item) for item in candidates],
            "shutdown_grace_s": runner.shutdown_grace_s,
        }
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings: Settings = get_settings()
    except ConfigError as exc:
        _print(_error_payload("settings", exc))
        return EXIT_CONFIG_ERROR

    configure_logging(settings.log_level, json_format=settings.log_json)

    try:
        stack = build_worker_stack(
            settings, worker_id=args.worker_id, tenant_id=args.tenant
        )
    except (ConfigError, DBGuardianError, OSError, ValueError) as exc:
        # 装配失败一律 fail closed：不进入轮询（绝不"半可用"地跑起来）。
        _print(_error_payload("assembly", exc))
        return EXIT_CONFIG_ERROR

    if args.self_check:
        return _self_check(stack)

    runner = WorkerRunner(
        stack,
        poll_interval_s=settings.worker_poll_interval_s,
        shutdown_grace_s=settings.worker_shutdown_grace_s,
    )

    if args.dry_run:
        try:
            return _dry_run(stack, runner)
        except DBGuardianError as exc:
            _print(_error_payload("dry_run", exc))
            return EXIT_CONFIG_ERROR

    if args.once:
        try:
            report = runner.run_once()
        except DBGuardianError as exc:
            _print(_error_payload("once", exc))
            return EXIT_CONFIG_ERROR
        outstanding = runner.outstanding_candidates()
        _print(_report_payload(report, outstanding=outstanding))
        return EXIT_OK if report.clean and outstanding == 0 else EXIT_OUTSTANDING

    _install_signal_handlers(runner)
    try:
        report = runner.run_forever()
    except DBGuardianError as exc:
        _print(_error_payload("forever", exc))
        return EXIT_CONFIG_ERROR
    outstanding = runner.outstanding_candidates()
    _print(_report_payload(report, outstanding=outstanding))
    return EXIT_OK if report.clean and outstanding == 0 else EXIT_OUTSTANDING


__all__ = ["EXIT_CONFIG_ERROR", "EXIT_OK", "EXIT_OUTSTANDING", "build_parser", "main"]
