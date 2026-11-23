"""P2-T1B：**suite-v2** 生成器（扩张套件，冻结 suite-v1 逐字节不动）。

扩张策略（在"不编造数据"前提下能做的最诚实事）：

- 复用 6 个**已有**故障清单（`benchmarks/faults/manifests/*.json`）的引擎/场景/种子；
- 每个场景展开 `repetitions` 次（默认 6）→ 6 × 6 = **36 个 case 实例**；
  重复实验**不是独立样本**（同一 fault_seed），报告与台账按此行文，不冒充多 seed；
- 外部案例（DiagDBench / D-Bot / BIRD-CRITIC / BenchBase）只有在本项目存在
  **同引擎同场景**的故障清单时才可挂载；否则进入 `unmapped_external`，
  **不进入 cases**（避免把别人的真值当成本项目 Oracle）。

`suitte-v1` 保持逐字节不变：本模块只新增文件，不读写 `suite_manifest.json`。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from benchmarks.external.convert import SUPPORTED_SOURCES
from benchmarks.external.schema import ExternalCase

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR: Path = ROOT / "benchmarks" / "faults" / "manifests"
SUITE_V1_PATH: Path = ROOT / "benchmarks" / "w10" / "suite_manifest.json"
SUITE_V2_VERSION = "suite-v2"

#: 与 `core.benchmark.runner.ADAPTER_READ_TOOLS` 逐字一致（避免 core ↔ benchmarks 反向依赖）。
_TOOL_PLAN: tuple[str, ...] = (
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
)

_ENGINE_ALIASES = {"postgres": "postgresql", "pg": "postgresql", "mysql": "mysql"}


def load_local_manifests(directory: Path = MANIFEST_DIR) -> list[dict[str, Any]]:
    """读取全部故障清单（只读 JSON，不 import ``core.faults``）。"""
    manifests: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "scenario_id" not in payload:
            continue
        manifests.append(payload)
    return manifests


def _normalize_engine(value: str) -> str:
    engine = _ENGINE_ALIASES.get(str(value).strip().lower(), str(value).strip().lower())
    return engine


def split_external(
    external: Iterable[ExternalCase], manifests: Sequence[Mapping[str, Any]]
) -> tuple[list[ExternalCase], list[dict[str, Any]]]:
    """可挂载 / 不可挂载（无同引擎同场景清单）的外部案例，逐一给出原因。"""
    known = {
        (_normalize_engine(str(item["db_engine"])), str(item["scenario"]))
        for item in manifests
    }
    attached: list[ExternalCase] = []
    unmapped: list[dict[str, Any]] = []
    for case in external:
        key = (_normalize_engine(case.engine), case.scenario)
        if key in known:
            attached.append(case)
        else:
            unmapped.append(
                {
                    "source": case.source,
                    "source_case_id": case.source_case_id,
                    "engine": case.engine,
                    "scenario": case.scenario,
                    "reason": "NO_LOCAL_MANIFEST",
                }
            )
    return attached, unmapped


def build_suite_v2(
    *,
    repetitions: int = 6,
    external: Iterable[ExternalCase] = (),
    directory: Path = MANIFEST_DIR,
) -> dict[str, Any]:
    """构造 suite-v2 字典（可直接 `json.dumps` 落盘并被 `load_suite` 读取）。"""
    if repetitions < 1:
        raise ValueError("repetitions 必须 >= 1")
    manifests = load_local_manifests(directory)
    if not manifests:
        raise ValueError(f"未找到任何故障清单：{directory}")
    attached, unmapped = split_external(external, manifests)

    cases: list[dict[str, Any]] = []
    for manifest in sorted(manifests, key=lambda item: str(item["scenario_id"])):
        engine = _normalize_engine(str(manifest["db_engine"]))
        for block in range(1, repetitions + 1):
            cases.append(
                {
                    "case_id": f"{manifest['scenario_id']}-b{block}",
                    "engine": engine,
                    "scenario": str(manifest["scenario"]),
                    "scenario_id": str(manifest["scenario_id"]),
                    "fault_seed": int(manifest["data_seed"]),
                    "repetitions": 1,
                }
            )

    suite_v1 = json.loads(SUITE_V1_PATH.read_text(encoding="utf-8"))
    return {
        "manifest_version": "w10-suite-v2",
        "suite_version": SUITE_V2_VERSION,
        # 与 v1 同 testset（Oracle/清单未变），避免伪造新测试集版本。
        "testset_version": suite_v1["testset_version"],
        "harness_version": suite_v1["harness_version"],
        "repetitions_per_seed": repetitions,
        "tool_plan": list(_TOOL_PLAN),
        "provenance": {
            "base_suite": suite_v1["suite_version"],
            "cases_source": "benchmarks/faults/manifests（本地冻结故障清单）",
            "instances": len(cases),
            "distinct_fault_seeds": len({case["fault_seed"] for case in cases}),
            "independence_note": "同一 fault_seed 的重复实验不是独立样本，CI 不得据此收窄",
            "external_sources_supported": list(SUPPORTED_SOURCES),
            "external_attached": [
                {
                    "source": case.source,
                    "source_case_id": case.source_case_id,
                    "engine": case.engine,
                    "scenario": case.scenario,
                    "fault_seed": case.fault_seed,
                }
                for case in attached
            ],
            "external_unmapped": unmapped,
        },
        "cases": cases,
    }


def write_suite_v2(target: Path, **kwargs: Any) -> Path:
    """生成并写入 suite-v2（排序键固定，保证可复现）。"""
    payload = build_suite_v2(**kwargs)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "MANIFEST_DIR",
    "SUITE_V1_PATH",
    "SUITE_V2_VERSION",
    "build_suite_v2",
    "load_local_manifests",
    "split_external",
    "write_suite_v2",
]
