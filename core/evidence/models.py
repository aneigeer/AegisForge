"""Evidence 证据模型（V2.2 W03）。

证据必须带来源、时间窗、质量、payload_hash、脱敏版本与租户/任务/环境归属；
报告中的事实结论只能引用经过归属校验的 evidence_id。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.tasks.models import TaskStep, utc_now


class CollectionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    DENIED = "DENIED"
    TIMEOUT = "TIMEOUT"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class EvidenceQuality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


# W09：**弱采集状态**闭集——这些状态下证据不得升格为 HIGH 质量
# （`EvidenceService.create` 既有的 HIGH 质量拒绝口径，改用常量表达，取值不变）。
WEAK_COLLECTION_STATUSES: frozenset[CollectionStatus] = frozenset(
    {
        CollectionStatus.PARTIAL,
        CollectionStatus.DENIED,
        CollectionStatus.TIMEOUT,
        CollectionStatus.NOT_SUPPORTED,
        CollectionStatus.UNKNOWN,
    }
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    tenant_id: str
    task_id: str
    step_id: str | None = None
    tool_call_id: str | None = None
    environment_id: str
    db_engine: str
    db_version: str
    adapter_version: str
    source_tool: str
    observed_at: str
    time_window_start: str | None = None
    time_window_end: str | None = None
    collection_status: CollectionStatus
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    completeness: EvidenceCompleteness = EvidenceCompleteness.UNKNOWN
    payload_hash: str
    redacted_payload_hash: str | None = None
    # W09 闸门②：**脱敏前原文哈希**（引擎 `RedactionResult.payload_hash` 落库锚点）。
    # 与 `payload_hash`（写入载荷哈希）同名不同义，禁止互相覆盖
    # （`docs/w09/02-design.md` §0 A2）。`None` = 未经闸门的旧路径/历史行。
    source_payload_hash: str | None = None
    redaction_version: str = "none"
    reproducible: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    redacted_payload: dict[str, Any] | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class EvidenceCreate(BaseModel):
    """EvidenceService 的写入请求；tenant 与 environment 由服务端校验。"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    task_id: str
    environment_id: str
    step_id: str | None = None
    tool_call_id: str | None = None
    db_engine: str
    db_version: str
    adapter_version: str
    source_tool: str
    observed_at: str
    time_window_start: str | None = None
    time_window_end: str | None = None
    collection_status: CollectionStatus
    quality: EvidenceQuality = EvidenceQuality.UNKNOWN
    completeness: EvidenceCompleteness = EvidenceCompleteness.UNKNOWN
    payload: dict[str, Any]
    redacted_payload: dict[str, Any] | None = None
    # W09 闸门②：原文哈希锚点（仅落库，**不参与** payload_hash 计算）。
    source_payload_hash: str | None = None
    redaction_version: str = "none"
    reproducible: bool = False


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)


class ReportFacts(BaseModel):
    """报告事实必须绑定 evidence_id；缺失证据不得进入有效报告。"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    task_id: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


__all__ = [
    "CollectionStatus",
    "Evidence",
    "EvidenceCompleteness",
    "EvidenceCreate",
    "EvidenceQuality",
    "EvidenceRef",
    "ReportFacts",
    "TaskStep",
    "WEAK_COLLECTION_STATUSES",
]
