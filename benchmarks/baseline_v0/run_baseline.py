"""DBGuardian V2.2 W04 Baseline v0 资产校验与 dry-run 规划。

范围约束：
- 只读取本地 JSON/TXT 资产，校验字段完整性与版本一致性；
- 不下载模型、不加载权重、不训练、不联网、不调用外部 API；
- dry-run 只输出“上机后如何执行”的规划，不产生任何指标；
- 任何缺失或伪造都通过非零退出码报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

REQUIRED_MANIFEST_FIELDS = (
    "manifest_version",
    "wbs",
    "model_name",
    "model_commit",
    "tokenizer_commit",
    "prompt_version",
    "tool_schema_version",
    "harness_version",
    "inference_config_version",
    "testset_version",
    "created_at",
    "status",
    "notes",
)

FIXED_TOOL_NAMES = (
    "get_connection_snapshot",
    "get_query_stats",
    "get_lock_wait_edges",
    "get_lock_details",
    "get_transaction_metadata",
    "explain_query",
    "get_table_stats",
)

ENGINES = ("mysql", "postgresql")
SCENARIOS = ("slow_query", "connection_pressure", "lock_wait")
EXPECTED_CASES = {(engine, scenario) for engine in ENGINES for scenario in SCENARIOS}

TOOL_REQUIRED_FIELDS = (
    "name",
    "version",
    "engine",
    "input_schema",
    "output_schema",
    "risk_level",
)

METRIC_KEYS = {
    "accuracy",
    "f1",
    "f1_score",
    "precision",
    "recall",
    "latency",
    "latency_ms",
    "score",
    "scores",
}

ORACLE_PROMPT_FORBIDDEN = ("expected_root_cause", "fault_seed")

PROMPT_REQUIRED_TERMS = (
    "JSON",
    "evidence_id",
    "supporting_evidence_ids",
    "contradicting_evidence_ids",
    "missing_evidence",
    "confidence_band",
    "HIGH",
    "MEDIUM",
    "LOW",
    "UNKNOWN",
    "不可信",
    "禁止执行 SQL",
)


def load_assets(base_dir: Path = BASE_DIR) -> dict[str, Any]:
    """加载并解析全部冻结资产；任一文件缺失/非法 JSON 都会抛出 ValueError。"""

    def _json(name: str) -> dict[str, Any]:
        path = base_dir / name
        if not path.is_file():
            raise ValueError(f"缺少资产文件：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} 不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{name} 顶层必须是 JSON 对象")
        return payload

    prompt_path = base_dir / "prompt.txt"
    if not prompt_path.is_file():
        raise ValueError(f"缺少资产文件：{prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("prompt.txt 不能为空")

    return {
        "manifest": _json("baseline_manifest.json"),
        "prompt": prompt,
        "tool_schema": _json("tool_schema.json"),
        "testset": _json("testset_manifest.json"),
        "inference_config": _json("inference_config.json"),
        "results": _json("results.json"),
    }


def _metric_keys_in(payload: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in METRIC_KEYS and value is not None:
                found.append(path)
            found.extend(_metric_keys_in(value, path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_metric_keys_in(value, f"{prefix}[{index}]"))
    return found


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"manifest 缺少字段：{field}")
    if manifest.get("status") != "BLOCKED_MODEL_ENV_MISSING":
        errors.append("manifest.status 必须为 BLOCKED_MODEL_ENV_MISSING")
    if manifest.get("model_commit") is not None:
        errors.append("manifest.model_commit 必须为 null（模型尚未冻结）")
    if manifest.get("tokenizer_commit") is not None:
        errors.append("manifest.tokenizer_commit 必须为 null（tokenizer 尚未冻结）")
    if manifest.get("harness_version") != "44e4f9d":
        errors.append("manifest.harness_version 必须为 44e4f9d")
    if not manifest.get("notes"):
        errors.append("manifest.notes 不能为空")
    metrics = _metric_keys_in(manifest)
    if metrics:
        errors.append(f"manifest 不得包含任何指标字段：{metrics}")
    return errors


def validate_prompt(prompt: str) -> list[str]:
    errors: list[str] = []
    lowered = prompt.lower()
    for term in PROMPT_REQUIRED_TERMS:
        if term.lower() not in lowered:
            errors.append(f"prompt 缺少必需约束/字段：{term}")
    for forbidden in ORACLE_PROMPT_FORBIDDEN:
        if forbidden.lower() in lowered:
            errors.append(f"prompt 不得包含 Oracle 真值字段：{forbidden}")
    if "hypotheses" not in lowered:
        errors.append("prompt 必须要求输出结构化 hypotheses JSON")
    return errors


def validate_tool_schema(
    tool_schema: dict[str, Any], expected_version: str | None = None
) -> list[str]:
    errors: list[str] = []
    version = tool_schema.get("tool_schema_version")
    if not version:
        errors.append("tool_schema 缺少 tool_schema_version")
    elif expected_version is not None and version != expected_version:
        errors.append(
            f"tool_schema_version 不一致：manifest={expected_version}, schema={version}"
        )

    engines = tool_schema.get("engines")
    if not isinstance(engines, dict):
        return errors + ["tool_schema.engines 必须是对象"]
    if set(engines.keys()) != set(ENGINES):
        errors.append(f"tool_schema.engines 必须且仅包含 {sorted(ENGINES)}")

    total = 0
    for engine in ENGINES:
        entry = engines.get(engine)
        if not isinstance(entry, dict):
            errors.append(f"tool_schema.engines.{engine} 必须是对象")
            continue
        tools = entry.get("tools")
        if not isinstance(tools, list):
            errors.append(f"tool_schema.engines.{engine}.tools 必须是数组")
            continue
        total += len(tools)
        if len(tools) != 7:
            errors.append(f"{engine} 工具数必须为 7，实际 {len(tools)}")
        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
        if set(names) != set(FIXED_TOOL_NAMES) or len(names) != len(FIXED_TOOL_NAMES):
            errors.append(f"{engine} 工具名必须覆盖固定 7 个只读工具")
        for tool in tools:
            if not isinstance(tool, dict):
                errors.append(f"{engine} 存在非对象工具条目")
                continue
            for field in TOOL_REQUIRED_FIELDS:
                if field not in tool:
                    errors.append(f"{engine}.{tool.get('name', '?')} 缺少字段 {field}")
            if tool.get("engine") != engine:
                errors.append(f"{engine}.{tool.get('name', '?')} engine 字段不一致")
            if tool.get("risk_level") != "R0":
                errors.append(f"{engine}.{tool.get('name', '?')} risk_level 必须为 R0")
            if not isinstance(tool.get("input_schema"), dict):
                errors.append(f"{engine}.{tool.get('name', '?')} input_schema 必须是对象")
            if not isinstance(tool.get("output_schema"), dict):
                errors.append(f"{engine}.{tool.get('name', '?')} output_schema 必须是对象")
    if total != 14:
        errors.append(f"两引擎工具总数必须为 14，实际 {total}")
    if tool_schema.get("harness_version") != "44e4f9d":
        errors.append("tool_schema.harness_version 必须为 44e4f9d")
    return errors


def validate_testset(testset: dict[str, Any], expected_version: str | None = None) -> list[str]:
    errors: list[str] = []
    version = testset.get("testset_version")
    if not version:
        errors.append("testset 缺少 testset_version")
    elif expected_version is not None and version != expected_version:
        errors.append(
            f"testset_version 不一致：manifest={expected_version}, testset={version}"
        )

    if testset.get("preview") is not True:
        errors.append("testset.preview 必须为 true")
    if testset.get("executed") is not False:
        errors.append("testset.executed 必须为 false")
    if testset.get("generated") is not False:
        errors.append("testset.generated 必须为 false")

    cases = testset.get("cases")
    if not isinstance(cases, list):
        return errors + ["testset.cases 必须是数组"]
    combos = {(case.get("engine"), case.get("scenario_id")) for case in cases if isinstance(case, dict)}
    if combos != EXPECTED_CASES:
        errors.append(f"testset 必须覆盖 6 个引擎×场景组合，实际 {sorted(map(str, combos))}")
    if len(cases) != len(EXPECTED_CASES):
        errors.append(f"testset.cases 必须为 6 条，实际 {len(cases)}")
    for case in cases:
        if not isinstance(case, dict):
            errors.append("testset 存在非对象 case")
            continue
        case_id = case.get("case_id", "?")
        seeds = case.get("seeds")
        if not isinstance(seeds, list) or not seeds:
            errors.append(f"{case_id} seeds 必须为非空数组")
        if case.get("repetitions_per_seed") != 3:
            errors.append(f"{case_id} repetitions_per_seed 必须为 3")
        if case.get("status") != "NOT_GENERATED":
            errors.append(f"{case_id} status 必须为 NOT_GENERATED")
        workload_groups = case.get("workload_groups")
        if not isinstance(workload_groups, list) or not workload_groups:
            errors.append(f"{case_id} workload_groups 必须为非空数组")

    split = testset.get("split")
    if not isinstance(split, dict):
        errors.append("testset.split 必须是对象")
    else:
        if split.get("strategy") != "group_split":
            errors.append("testset.split.strategy 必须为 group_split")
        keys = split.get("group_keys")
        required_keys = {"engine", "fault_family", "seed", "workload_group"}
        if not isinstance(keys, list) or not required_keys.issubset(set(keys)):
            errors.append("testset.split.group_keys 必须包含 engine/fault_family/seed/workload_group")
        oracle = split.get("oracle_isolation")
        if not isinstance(oracle, dict) or oracle.get("enabled") is not True:
            errors.append("testset.split.oracle_isolation.enabled 必须为 true")
        else:
            for flag in (
                "oracle_visible_to_agent",
                "oracle_visible_to_model",
                "oracle_in_logs",
                "oracle_in_training_samples",
            ):
                if oracle.get(flag) is not False:
                    errors.append(f"testset.split.oracle_isolation.{flag} 必须为 false")
    return errors


def validate_inference_config(
    config: dict[str, Any], expected_version: str | None = None
) -> list[str]:
    errors: list[str] = []
    version = config.get("inference_config_version")
    if not version:
        errors.append("inference_config 缺少 inference_config_version")
    elif expected_version is not None and version != expected_version:
        errors.append(
            f"inference_config_version 不一致：manifest={expected_version}, config={version}"
        )
    if config.get("status") != "BLOCKED_MODEL_ENV_MISSING":
        errors.append("inference_config.status 必须为 BLOCKED_MODEL_ENV_MISSING")

    model = config.get("model")
    if not isinstance(model, dict):
        errors.append("inference_config.model 必须是对象")
    else:
        if model.get("model_commit") is not None:
            errors.append("inference_config.model.model_commit 必须为 null")
        if model.get("tokenizer_commit") is not None:
            errors.append("inference_config.model.tokenizer_commit 必须为 null")

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        errors.append("inference_config.runtime 必须是对象")
    else:
        gpu = runtime.get("gpu_memory")
        if not isinstance(gpu, dict) or gpu.get("estimated_gb") is not None:
            errors.append("显存预估必须待实测（estimated_gb 为 null）")

    tool_use = config.get("tool_use")
    if not isinstance(tool_use, dict):
        errors.append("inference_config.tool_use 必须是对象")
    else:
        if tool_use.get("allow_write_tools") is not False:
            errors.append("禁止启用写工具")
        if tool_use.get("allow_arbitrary_sql") is not False:
            errors.append("禁止允许任意 SQL")

    observation = config.get("observation")
    if not isinstance(observation, dict) or observation.get("oracle_visible_to_model") is not False:
        errors.append("Oracle 不得对模型可见")

    metrics = _metric_keys_in(config)
    if metrics:
        errors.append(f"inference_config 不得包含任何指标字段：{metrics}")
    return errors


def validate_results(results: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if results.get("status") != "NOT_RUN":
        errors.append("results.status 必须为 NOT_RUN")
    if results.get("scores") is not None:
        errors.append("results.scores 必须为 null")
    if results.get("metrics") is not None:
        errors.append("results.metrics 必须为 null（不得编造指标）")
    if not results.get("blocked_reason"):
        errors.append("results.blocked_reason 不能为空")
    if results.get("run_id") is not None:
        errors.append("尚未执行时 results.run_id 必须为 null")
    return errors


def validate_assets(assets: dict[str, Any]) -> list[str]:
    manifest = assets["manifest"]
    errors: list[str] = []
    errors.extend(validate_manifest(manifest))
    errors.extend(validate_prompt(assets["prompt"]))
    errors.extend(validate_tool_schema(assets["tool_schema"], manifest.get("tool_schema_version")))
    errors.extend(validate_testset(assets["testset"], manifest.get("testset_version")))
    errors.extend(
        validate_inference_config(
            assets["inference_config"], manifest.get("inference_config_version")
        )
    )
    errors.extend(validate_results(assets["results"]))

    config = assets["inference_config"]
    observation = config.get("observation", {}) if isinstance(config.get("observation"), dict) else {}
    if observation.get("prompt_version") != manifest.get("prompt_version"):
        errors.append("inference_config.observation.prompt_version 与 manifest 不一致")
    tool_use = config.get("tool_use", {}) if isinstance(config.get("tool_use"), dict) else {}
    if tool_use.get("tool_schema_version") != manifest.get("tool_schema_version"):
        errors.append("inference_config.tool_use.tool_schema_version 与 manifest 不一致")
    if observation.get("testset_version") != manifest.get("testset_version"):
        errors.append("inference_config.observation.testset_version 与 manifest 不一致")
    return errors


def build_dry_run_plan(assets: dict[str, Any]) -> dict[str, Any]:
    manifest = assets["manifest"]
    testset = assets["testset"]
    tool_schema = assets["tool_schema"]

    cases = testset.get("cases", [])
    episodes = sum(
        len(case.get("seeds", [])) * int(case.get("repetitions_per_seed", 0))
        for case in cases
        if isinstance(case, dict)
    )
    tool_count = sum(
        len(entry.get("tools", []))
        for entry in tool_schema.get("engines", {}).values()
        if isinstance(entry, dict)
    )

    return {
        "mode": "dry-run",
        "network_access": False,
        "model_downloaded": False,
        "model_loaded": False,
        "training": False,
        "external_api_calls": False,
        "status": manifest.get("status"),
        "blocked_reason": assets["results"].get("blocked_reason"),
        "model": {
            "name": manifest.get("model_name"),
            "model_status": manifest.get("model_status"),
            "model_commit": manifest.get("model_commit"),
            "tokenizer_commit": manifest.get("tokenizer_commit"),
        },
        "frozen_versions": {
            "harness_version": manifest.get("harness_version"),
            "prompt_version": manifest.get("prompt_version"),
            "tool_schema_version": manifest.get("tool_schema_version"),
            "inference_config_version": manifest.get("inference_config_version"),
            "testset_version": manifest.get("testset_version"),
        },
        "assets": {
            "prompt_lines": len(assets["prompt"].splitlines()),
            "tool_schema_tools": tool_count,
            "testset_cases": len(cases),
            "planned_episodes": episodes,
            "preview": testset.get("preview"),
            "executed": testset.get("executed"),
        },
        "planned_steps_after_unblock": [
            "1. 用户确认 Qwen3-8B（或经 ADR 固定的替代基线），填写 model_commit 与 tokenizer_commit。",
            "2. 租用云端 GPU；实测框架版本、max_model_len 与显存占用后回填 inference_config.json。",
            "3. 按冻结的 prompt/tool_schema 加载权重并发起只读推理，不进行任何训练或微调。",
            "4. 生成并校验 PG/MySQL × 三场景测试集；保持 preview/executed 标记诚实。",
            f"5. 按计划执行 {episodes} 个 episodes（20 seeds × 3 次 × 6 组合）。",
            "6. 仅将真实运行结果写入 results.json；未跑完前保持 NOT_RUN / scores=null。",
            "7. 若任何评测指标未实际测得，不得填写，也不得据此宣称基线成绩。",
        ],
    }


def _print_human_summary(plan: dict[str, Any]) -> None:
    frozen = plan["frozen_versions"]
    assets = plan["assets"]
    print("[OK] Baseline v0 资产校验通过")
    print(f"status={plan['status']}")
    print(
        "model={name} model_commit={commit} tokenizer_commit={tok}".format(
            name=plan["model"]["name"],
            commit=plan["model"]["model_commit"],
            tok=plan["model"]["tokenizer_commit"],
        )
    )
    print(
        "harness={harness} prompt={prompt} tool_schema={tools} "
        "inference_config={inf} testset={testset}".format(
            harness=frozen["harness_version"],
            prompt=frozen["prompt_version"],
            tools=frozen["tool_schema_version"],
            inf=frozen["inference_config_version"],
            testset=frozen["testset_version"],
        )
    )
    print(
        f"assets: tools={assets['tool_schema_tools']} cases={assets['testset_cases']} "
        f"planned_episodes={assets['planned_episodes']} "
        f"preview={assets['preview']} executed={assets['executed']}"
    )
    print("dry-run: 不下载模型、不加载权重、不训练、不联网、不调用外部 API")
    print(f"blocked_reason: {plan['blocked_reason']}")
    print("next steps after unblock:")
    for step in plan["planned_steps_after_unblock"]:
        print(f"  {step}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Baseline v0 资产校验与 dry-run 规划（不加载权重、不联网、不训练）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="输出上机执行规划（默认行为；不会执行推理）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅校验资产完整性，与 --dry-run 共用同一安全路径",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出规划")
    args = parser.parse_args(argv)

    try:
        assets = load_assets()
    except ValueError as exc:
        print(f"[FAIL] 资产加载失败：{exc}", file=sys.stderr)
        return 2

    errors = validate_assets(assets)
    if errors:
        print("[FAIL] 资产校验未通过：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    plan = build_dry_run_plan(assets)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_human_summary(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
