"""W10 T02：Benchmark Run Manifest（AC-01 / AC-02）。

所有版本字段**逐字取自** ``benchmarks/baseline_v0/`` 的 4 个冻结资产，
运行期不新造任何版本字符串（设计 §4.1、任务卡 :202）：

- ``baseline_manifest.json``：5 个版本字段 + model_name/model_commit/tokenizer_commit；
- ``inference_config.json``：``decoding.seed``（42）与 ``tool_use.max_tool_calls``（16）；
- ``tool_schema.json`` / ``testset_manifest.json``：交叉校验版本一致 + 计划 case 数。

``extra="forbid"``；``seed`` / ``env_image_digest`` / ``budget`` 三者**必填**，
缺任一即 ``ValidationError``。``env_image_digest`` 未知一律填 ``"UNKNOWN"``，
不臆造容器镜像 digest（``ENV-NV1`` / Q-W10-A 口径）。

本模块不 import ``benchmarks.*``（只读冻结 JSON 数据文件），因此随 ``core*``
打包后在只装 wheel 的环境里也不会因缺 ``benchmarks/`` 包而 ImportError。
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.errors import BenchmarkInvalidError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_DIR: Path = ROOT / "benchmarks" / "baseline_v0"

# 评测运行墙钟上限（**配置预算，不是实测指标**；与「禁止编造指标」口径不冲突）。
DEFAULT_MAX_WALL_S: float = 3600.0

# from_baseline() 必须逐字段相等的 5 个冻结版本字段（设计 §4.1 / 任务卡 :202）。
FROZEN_VERSION_FIELDS: tuple[str, ...] = (
    "prompt_version",
    "tool_schema_version",
    "harness_version",
    "inference_config_version",
    "testset_version",
)


class RunStatus(str, Enum):
    """评测运行状态；迁移顺序见 ``runner.ALLOWED_RUN_TRANSITIONS``（不可回退）。"""

    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class RunBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cases: int = Field(ge=1)
    max_wall_s: float = Field(gt=0)
    max_tool_calls_per_case: int = Field(ge=1)


class RunManifest(BaseModel):
    """一次评测运行的冻结清单；字段封闭、无隐式默认兜底。"""

    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["w10-run-v1"] = "w10-run-v1"
    suite_version: str = Field(pattern=r"^suite-v\d+$")
    model_name: str
    model_version: str | None
    tokenizer_version: str | None
    prompt_version: str
    tool_schema_version: str
    schema_version: str
    harness_version: str
    inference_config_version: str
    testset_version: str
    env_image_digest: str = Field(min_length=1)
    env_image_digest_source: Literal["DOCKER_INSPECT", "UNKNOWN"] = "UNKNOWN"
    seed: int
    budget: RunBudget
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.NOT_RUN
    notes: tuple[str, ...] = ()

    @classmethod
    def from_baseline(
        cls,
        base_dir: Path = DEFAULT_BASELINE_DIR,
        *,
        suite_version: str,
        env_image_digest: str = "UNKNOWN",
        **overrides: Any,
    ) -> "RunManifest":
        """从 4 个 W04 冻结资产构造 Manifest（**不新造版本字符串**）。

        ``overrides`` 只允许覆盖已有字段（``extra="forbid"`` 会拒绝新字段）；
        典型用途是显式传 ``budget`` / ``notes`` / ``env_image_digest_source``。
        """

        base = Path(base_dir)
        baseline = _read_json(base / "baseline_manifest.json")
        inference = _read_json(base / "inference_config.json")
        tool_schema = _read_json(base / "tool_schema.json")
        testset = _read_json(base / "testset_manifest.json")

        # 交叉校验：4 个冻结资产互相矛盾时 fail closed，不静默挑一个。
        if tool_schema.get("tool_schema_version") != baseline.get("tool_schema_version"):
            raise BenchmarkInvalidError(
                "冻结资产冲突：tool_schema.json.tool_schema_version 与 baseline_manifest 不一致",
                details={"asset": "tool_schema.json"},
            )
        if testset.get("testset_version") != baseline.get("testset_version"):
            raise BenchmarkInvalidError(
                "冻结资产冲突：testset_manifest.json.testset_version 与 baseline_manifest 不一致",
                details={"asset": "testset_manifest.json"},
            )
        if inference.get("inference_config_version") != baseline.get("inference_config_version"):
            raise BenchmarkInvalidError(
                "冻结资产冲突：inference_config.json 版本与 baseline_manifest 不一致",
                details={"asset": "inference_config.json"},
            )

        try:
            seed = int(inference["decoding"]["seed"])
            max_tool_calls = int(inference["tool_use"]["max_tool_calls"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkInvalidError(
                "inference_config.json 缺少 decoding.seed / tool_use.max_tool_calls",
                details={"asset": "inference_config.json"},
            ) from exc

        # max_cases 取自冻结测试集的真实计划 episode 数（6 组合 × 20 seed × 3 次 = 360），
        # 与 run_baseline.py 的 planned_episodes 断言同向，不臆造。
        max_cases = sum(
            len(case["seeds"]) * int(case["repetitions_per_seed"])
            for case in testset["cases"]
        )
        payload: dict[str, Any] = {
            "manifest_version": "w10-run-v1",
            "suite_version": suite_version,
            "model_name": baseline["model_name"],
            # model_commit / tokenizer_commit 当前为 null（ENV-MODEL）→ 保持 None。
            "model_version": baseline["model_commit"],
            "tokenizer_version": baseline["tokenizer_commit"],
            "prompt_version": baseline["prompt_version"],
            "tool_schema_version": baseline["tool_schema_version"],
            # schema_version 与 tool_schema_version 同源（设计 §4.1 显式冗余）。
            "schema_version": baseline["tool_schema_version"],
            "harness_version": baseline["harness_version"],
            "inference_config_version": baseline["inference_config_version"],
            "testset_version": baseline["testset_version"],
            "env_image_digest": env_image_digest,
            "env_image_digest_source": (
                "UNKNOWN" if env_image_digest == "UNKNOWN" else "DOCKER_INSPECT"
            ),
            "seed": seed,
            "budget": {
                "max_cases": max_cases,
                "max_wall_s": DEFAULT_MAX_WALL_S,
                "max_tool_calls_per_case": max_tool_calls,
            },
            "status": RunStatus.NOT_RUN,
        }
        payload.update(overrides)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _times_ordered(self) -> "RunManifest":
        """起止时间必须 tz-aware UTC 且 ``finished_at >= started_at``。"""

        for name, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} 必须是 tz-aware UTC 时间")
        if self.finished_at is not None:
            if self.started_at is None:
                raise ValueError("finished_at 存在时 started_at 不得为空")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at 不得早于 started_at")
        return self


def _read_json(path: Path) -> dict[str, Any]:
    """读取冻结资产 JSON（只读；不 import ``benchmarks.*``）。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkInvalidError(
            "冻结资产不可读或不是合法 JSON",
            details={"path": str(path)},
        ) from exc
    if not isinstance(payload, dict):
        raise BenchmarkInvalidError(
            "冻结资产顶层必须是对象", details={"path": str(path)}
        )
    return payload


__all__ = [
    "DEFAULT_BASELINE_DIR",
    "DEFAULT_MAX_WALL_S",
    "FROZEN_VERSION_FIELDS",
    "RunBudget",
    "RunManifest",
    "RunStatus",
]
