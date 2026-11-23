"""W04 故障实验 CLI：list / inject / reset / verify。

输出只含公开字段；本脚本不在任何输出、日志或异常中展示 Oracle 真值。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 直接以脚本方式运行时（python scripts/fault_lab.py），确保 benchmarks/ 可导入。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.faults.injector import FaultInjectionError, FaultSession, inject, reset
from core.faults.store import (
    FaultStoreError,
    compute_fault_script_hash,
    list_manifests,
    load_manifest,
    load_oracle,
    manifest_summary,
)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_list(_args: argparse.Namespace) -> int:
    _print([manifest_summary(manifest.scenario_id) for manifest in list_manifests()])
    return 0


def _cmd_inject(args: argparse.Namespace) -> int:
    result = inject(
        args.scenario_id,
        hold_seconds=args.hold_seconds,
        connection_count=args.sessions,
    )
    _print(result.to_public_dict())
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    _print(reset(args.scenario_id))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.scenario_id)
    truth = load_oracle(args.scenario_id)
    hash_ok = compute_fault_script_hash() == manifest.fault_script_hash
    manifest_text = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)
    leak = "expected_root_cause" in manifest_text
    payload = {
        "scenario_id": args.scenario_id,
        "manifest_ok": True,
        "oracle_present": True,
        "scenario_id_match": truth.scenario_id == manifest.scenario_id,
        "fault_seed_match": truth.fault_seed == manifest.data_seed,
        "script_hash_ok": hash_ok,
        "oracle_leak_in_manifest": leak,
    }
    _print(payload)
    return 0 if all(
        value is True
        for key, value in payload.items()
        if key != "scenario_id" and key != "oracle_leak_in_manifest"
    ) and not leak else 1


def _cmd_session(args: argparse.Namespace) -> int:
    with FaultSession(
        args.scenario_id,
        hold_seconds=args.hold_seconds,
        connection_count=args.sessions,
    ) as result:
        _print(result.to_public_dict())
    _print({"scenario_id": args.scenario_id, "reset": True})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fault_lab", description="DBGuardian W04 故障实验 CLIs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出公开 Manifest（不含 Oracle 真值）")
    p_list.set_defaults(func=_cmd_list)

    p_inject = sub.add_parser("inject", help="注入故障并保持（进程退出前由 reset 清理）")
    p_inject.add_argument("scenario_id")
    p_inject.add_argument("--hold-seconds", type=float, default=0.0)
    p_inject.add_argument("--sessions", type=int, default=10)
    p_inject.set_defaults(func=_cmd_inject)

    p_reset = sub.add_parser("reset", help="重置并清理故障")
    p_reset.add_argument("scenario_id")
    p_reset.set_defaults(func=_cmd_reset)

    p_verify = sub.add_parser("verify", help="显式校验 Manifest 与 Oracle（仅输出布尔结果）")
    p_verify.add_argument("scenario_id")
    p_verify.set_defaults(func=_cmd_verify)

    p_session = sub.add_parser("session", help="注入并在退出时自动重置（进程内保持）")
    p_session.add_argument("scenario_id")
    p_session.add_argument("--hold-seconds", type=float, default=0.0)
    p_session.add_argument("--sessions", type=int, default=10)
    p_session.set_defaults(func=_cmd_session)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (FaultStoreError, FaultInjectionError) as exc:
        _print(
            {
                "error": {
                    "code": getattr(exc, "code", "FAULT_ERROR"),
                    "message": str(exc),
                }
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
