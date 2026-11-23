"""W04 Fault Manifest 与 Oracle 真值模型。

Oracle 只允许 benchmark/诊断验收进程读取；AgentLoop、ContextBuilder、普通日志
与训练样本不得引用本模块的 OracleTruth。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.tasks.models import utc_now


class FaultScenario(str, Enum):
    SLOW_QUERY = "slow_query"
    CONNECTION_PRESSURE = "connection_pressure"
    LOCK_WAIT = "lock_wait"


class DbEngineName(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


class FaultCaseManifest(BaseModel):
    """可重放故障场景的公开元数据；Oracle 真值不在此结构内。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    scenario: FaultScenario
    db_engine: DbEngineName
    db_image_digest: str | None = None
    db_version: str
    adapter_version: str
    migration_version: str
    data_seed: int
    workload_config: dict[str, Any] = Field(default_factory=dict)
    fault_script_hash: str = Field(min_length=16, max_length=128)
    allowed_tools: list[str] = Field(min_length=1)
    verification_assertions: list[str] = Field(min_length=1)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @field_validator("allowed_tools")
    @classmethod
    def _no_arbitrary_sql(cls, tools: list[str]) -> list[str]:
        forbidden = {"execute_sql", "run_sql", "shell", "arbitrary_sql"}
        leaked = forbidden.intersection(tools)
        if leaked:
            raise ValueError(f"allowed_tools 含越界工具：{sorted(leaked)}")
        return tools


class TruthAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    expected: Any
    tolerance: float | None = Field(default=None, ge=0)


class OracleTruth(BaseModel):
    """Ground Truth Oracle；不得进入 Agent Observation / 模型上下文 / 日志。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    expected_root_cause: str
    fault_seed: int
    injection_params: dict[str, Any] = Field(default_factory=dict)
    truth_assertions: list[TruthAssertion] = Field(min_length=1)
    isolation_version: str = "oracle-v1"
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class OracleBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: FaultCaseManifest
    truth: OracleTruth

    @field_validator("truth")
    @classmethod
    def _matching_scenario(cls, truth: OracleTruth, info) -> OracleTruth:
        manifest = info.data.get("manifest")
        if manifest is not None and truth.scenario_id != manifest.scenario_id:
            raise ValueError("Oracle scenario_id 与 Manifest 不一致")
        return truth


__all__ = [
    "DbEngineName",
    "FaultCaseManifest",
    "FaultScenario",
    "OracleBundle",
    "OracleTruth",
    "TruthAssertion",
]
