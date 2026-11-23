"""P2-T1B：外部数据库诊断基准的**归一化契约**（只做格式归一，不搬运真值）。

支持的四类来源（`docs/enterprise-upgrade-roadmap.json` P2-T1B）：

| source | 说明 | 归一化所需字段 |
|---|---|---|
| `diagdb` | DiagDBench 风格工单 | `id` / `db` / `fault` / `seed` |
| `dbot` | D-Bot 风格案例 | `case_id` / `database` / `issue_type` / `data_seed` |
| `bird_critic` | BIRD-CRITIC-1 风格题目 | `question_id` / `db_engine` / `category` / `seed` |
| `benchbase` | BenchBase 工作负载画像 | `workload_id` / `engine` / `scenario` / `seed` |

**红线**：本模块只产出 `ExternalCase`（来源、引擎、场景、种子、备注），
**不读取、不复制**任何 `expected_root_cause` / `injection_params` / `truth_assertions`；
真值仍由本项目自己的 `benchmarks/faults/oracle/*.json` 提供，且只经 `OracleGate` 读取。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 允许归一化到的三类故障（与冻结测试集一致；不做第五类扩展）。
ALLOWED_SCENARIOS: tuple[str, ...] = ("slow_query", "connection_pressure", "lock_wait")

#: 允许的引擎标识。
ALLOWED_ENGINES: tuple[str, ...] = ("mysql", "postgresql")


class CorpusMissingError(RuntimeError):
    """外部语料缺失/不可读：**明确失败**，不允许静默生成占位案例。"""

    code = "EXTERNAL_CORPUS_MISSING"

    def __init__(self, source: str, path: str, detail: str = "") -> None:
        self.source = source
        self.path = path
        self.detail = detail
        super().__init__(f"外部语料不可用：source={source} path={path} {detail}".strip())


@dataclass(frozen=True)
class ExternalCase:
    """一条归一化后的外部案例（**不含任何真值字段**）。"""

    source: str
    source_case_id: str
    engine: str
    scenario: str
    fault_seed: int
    notes: str = ""

    def __post_init__(self) -> None:
        if self.engine not in ALLOWED_ENGINES:
            raise ValueError(f"engine 必须是 {ALLOWED_ENGINES}，收到 {self.engine!r}")
        if self.scenario not in ALLOWED_SCENARIOS:
            raise ValueError(f"scenario 必须是 {ALLOWED_SCENARIOS}，收到 {self.scenario!r}")
        if self.fault_seed < 0:
            raise ValueError("fault_seed 必须 >= 0")


__all__ = [
    "ALLOWED_ENGINES",
    "ALLOWED_SCENARIOS",
    "CorpusMissingError",
    "ExternalCase",
]
