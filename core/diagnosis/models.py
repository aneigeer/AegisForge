"""W04 Hypothesis 与诊断结果模型。

Hypothesis 只表示候选根因；必须携带支持证据、反证、缺失信息、适用范围与
置信度带（HIGH/MEDIUM/LOW/UNKNOWN），不得输出未校准的精确概率。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from core.faults.models import DbEngineName, FaultScenario
from core.tasks.models import utc_now


class ConfidenceBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class HypothesisStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class DiagnosisStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    DENIED = "DENIED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str
    scenario_id: str
    explanation: str = Field(min_length=1, max_length=2000)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence_band: ConfidenceBand = ConfidenceBand.UNKNOWN
    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    scope: str = "LAB"
    assumptions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @property
    def is_actionable(self) -> bool:
        """无支持证据或置信度为 UNKNOWN 的假设不能作为根因结论。"""
        return (
            self.status in {HypothesisStatus.CONFIRMED, HypothesisStatus.CANDIDATE}
            and bool(self.supporting_evidence_ids)
            and self.confidence_band != ConfidenceBand.UNKNOWN
        )


class DiagnosisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    scenario_id: str
    scenario: FaultScenario
    db_engine: DbEngineName
    status: DiagnosisStatus
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    scope: str = "LAB"
    generated_by: str = "rules-v0"
    observed_window: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @property
    def has_traceable_evidence(self) -> bool:
        return bool(self.evidence_ids) and all(
            evidence in self.evidence_ids
            for hypothesis in self.hypotheses
            for evidence in hypothesis.supporting_evidence_ids
        )


class DiagnosisReportTemplate(BaseModel):
    """最小诊断报告模板：结论必须绑定证据与适用范围。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    scenario_id: str
    db_engine: DbEngineName
    conclusion: str
    confidence_band: ConfidenceBand
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    scope: str = "LAB"
    limitations: list[str] = Field(default_factory=list)


__all__ = [
    "ConfidenceBand",
    "DiagnosisReportTemplate",
    "DiagnosisResult",
    "DiagnosisStatus",
    "Hypothesis",
    "HypothesisStatus",
]
