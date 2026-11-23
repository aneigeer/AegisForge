"""W04 Fault Manifest / Ground Truth Oracle 加载与校验。

隔离约束：
- Manifest 是公开元数据，可被诊断、Benchmark 与 CLI 读取；
- Oracle 真值只允许 benchmark / 诊断验收进程**显式**调用 ``load_oracle``；
  AgentLoop、ContextBuilder、普通 API、日志与训练样本不得引用本模块的 Oracle 读取函数；
- ``load_oracle`` 的返回值、错误与日志一律不得进入 Agent Observation 或模型上下文。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.faults.models import FaultCaseManifest, OracleTruth

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = ROOT / "benchmarks" / "faults" / "manifests"
ORACLE_DIR = ROOT / "benchmarks" / "faults" / "oracle"
INJECTOR_PATH = ROOT / "benchmarks" / "faults" / "injector.py"
_HASH_DEPENDENCIES = [
    INJECTOR_PATH,
    ROOT / "database" / "mysql" / "connection.py",
    ROOT / "database" / "postgres" / "connection.py",
]

# W02 双引擎固定只读工具；Manifest 只能从中选择，不得出现任意 SQL 工具。
PUBLIC_TOOL_ALLOWLIST = frozenset(
    {
        "get_connection_snapshot",
        "get_query_stats",
        "get_lock_wait_edges",
        "get_lock_details",
        "get_transaction_metadata",
        "explain_query",
        "get_table_stats",
    }
)

PUBLIC_MANIFEST_FIELDS = (
    "scenario_id",
    "scenario",
    "db_engine",
    "db_image_digest",
    "db_version",
    "adapter_version",
    "migration_version",
    "data_seed",
    "workload_config",
    "fault_script_hash",
    "allowed_tools",
    "verification_assertions",
    "created_at",
)


class FaultStoreError(ValueError):
    """Fault Store 的结构化错误；message 不含 Oracle 真值。"""

    def __init__(self, message: str, *, code: str = "FAULT_STORE_INVALID") -> None:
        super().__init__(message)
        self.code = code


class ManifestNotFoundError(FaultStoreError):
    def __init__(self, scenario_id: str) -> None:
        super().__init__(f"Manifest 不存在：{scenario_id}", code="MANIFEST_NOT_FOUND")
        self.scenario_id = scenario_id


class ManifestValidationError(FaultStoreError):
    def __init__(self, message: str, *, scenario_id: str) -> None:
        super().__init__(message, code="MANIFEST_INVALID")
        self.scenario_id = scenario_id


class OracleLoadError(FaultStoreError):
    def __init__(self, message: str, *, scenario_id: str) -> None:
        super().__init__(message, code="ORACLE_INVALID")
        self.scenario_id = scenario_id


def _validated_scenario_id(scenario_id: str) -> str:
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ManifestValidationError("scenario_id 必须为非空字符串", scenario_id=str(scenario_id))
    if "/" in scenario_id or "\\" in scenario_id or ".." in scenario_id:
        raise ManifestValidationError("scenario_id 含非法路径字符", scenario_id=scenario_id)
    return scenario_id


def _read_json(path: Path, *, scenario_id: str, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestNotFoundError(scenario_id) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise FaultStoreError(
            f"无法读取 {path.name}（{type(exc).__name__}）", code=code
        ) from None


def validate_allowed_tools(tools: list[str], *, scenario_id: str) -> None:
    unknown = sorted(set(tools) - PUBLIC_TOOL_ALLOWLIST)
    if unknown:
        raise ManifestValidationError(
            f"allowed_tools 含 W02 固定只读工具之外的工具：{unknown}",
            scenario_id=scenario_id,
        )


def load_manifest(scenario_id: str) -> FaultCaseManifest:
    """读取并校验单个公开 Manifest；不读取任何 Oracle 真值。"""

    scenario_id = _validated_scenario_id(scenario_id)
    path = MANIFEST_DIR / f"{scenario_id}.json"
    payload = _read_json(path, scenario_id=scenario_id, code="MANIFEST_INVALID")
    try:
        manifest = FaultCaseManifest.model_validate(payload)
    except ValidationError as exc:
        details = [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
            for item in exc.errors(include_input=False, include_url=False)
        ]
        raise ManifestValidationError(
            f"Manifest 字段校验失败：{details}", scenario_id=scenario_id
        ) from None
    if manifest.scenario_id != scenario_id:
        raise ManifestValidationError(
            "Manifest 文件名与 scenario_id 不一致", scenario_id=scenario_id
        )
    validate_allowed_tools(manifest.allowed_tools, scenario_id=scenario_id)
    expected_hash = compute_fault_script_hash(
        workload_config=manifest.workload_config,
        data_seed=manifest.data_seed,
        db_engine=manifest.db_engine.value,
        allowed_tools=manifest.allowed_tools,
    )
    if manifest.fault_script_hash != expected_hash:
        raise ManifestValidationError(
            "fault_script_hash 与注入脚本/运行参数不一致，Manifest 已过期",
            scenario_id=scenario_id,
        )
    return manifest


def manifest_summary(scenario_id: str) -> dict[str, Any]:
    """Manifest 的公开摘要；明确排除 Oracle 字段。"""

    manifest = load_manifest(scenario_id)
    data = manifest.model_dump(mode="json")
    return {field: data[field] for field in PUBLIC_MANIFEST_FIELDS if field in data}


def list_manifests() -> list[FaultCaseManifest]:
    if not MANIFEST_DIR.exists():
        return []
    manifests: list[FaultCaseManifest] = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        manifests.append(load_manifest(path.stem))
    return manifests


def load_oracle(scenario_id: str) -> OracleTruth:
    """显式读取 Ground Truth Oracle（仅 benchmark / 验收路径可调用）。"""

    scenario_id = _validated_scenario_id(scenario_id)
    path = ORACLE_DIR / f"{scenario_id}.json"
    if not path.exists():
        raise OracleLoadError(f"Oracle 不存在：{scenario_id}", scenario_id=scenario_id)
    payload = _read_json(path, scenario_id=scenario_id, code="ORACLE_INVALID")
    try:
        truth = OracleTruth.model_validate(payload)
    except ValidationError as exc:
        details = [
            {"loc": list(item["loc"]), "msg": item["msg"]}
            for item in exc.errors(include_input=False, include_url=False)
        ]
        raise OracleLoadError(f"Oracle 字段校验失败：{details}", scenario_id=scenario_id) from None

    manifest = load_manifest(scenario_id)
    if truth.scenario_id != scenario_id:
        raise OracleLoadError("Oracle scenario_id 与文件名不一致", scenario_id=scenario_id)
    if truth.scenario_id != manifest.scenario_id:
        raise OracleLoadError("Oracle scenario_id 与 Manifest 不一致", scenario_id=scenario_id)
    if truth.fault_seed != manifest.data_seed:
        raise OracleLoadError("Oracle fault_seed 与 Manifest data_seed 不一致", scenario_id=scenario_id)
    return truth


def compute_fault_script_hash(
    path: Path | None = None,
    *,
    workload_config: dict[str, Any] | None = None,
    data_seed: int | None = None,
    db_engine: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """计算故障资产哈希：注入脚本 + DB 连接层 + Manifest 运行参数。

    仅哈希 injector.py 无法发现 workload 参数或连接层变化，因此这里把
    运行参数与依赖文件一起纳入规范化 SHA-256。
    """

    script = Path(path) if path is not None else INJECTOR_PATH
    parts = [script.read_bytes()]
    for dependency in _HASH_DEPENDENCIES:
        if dependency.exists() and dependency != script:
            parts.append(dependency.read_bytes())
    runtime = {
        "workload_config": workload_config or {},
        "data_seed": data_seed,
        "db_engine": db_engine,
        "allowed_tools": sorted(allowed_tools or []),
    }
    parts.append(
        json.dumps(runtime, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return hashlib.sha256(b"\n".join(parts)).hexdigest()


__all__ = [
    "FaultStoreError",
    "INJECTOR_PATH",
    "MANIFEST_DIR",
    "ManifestNotFoundError",
    "ManifestValidationError",
    "ORACLE_DIR",
    "OracleLoadError",
    "PUBLIC_MANIFEST_FIELDS",
    "PUBLIC_TOOL_ALLOWLIST",
    "compute_fault_script_hash",
    "list_manifests",
    "load_manifest",
    "load_oracle",
    "manifest_summary",
    "validate_allowed_tools",
]
