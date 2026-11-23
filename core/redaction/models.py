"""W09 T01 脱敏领域模型（全部 `extra="forbid"`，禁止隐式字段旁路）。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RedactionStatus(str, Enum):
    CLEAN = "CLEAN"
    REDACTED = "REDACTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class SensitiveCategory(str, Enum):
    """12 类敏感类别；`detected_categories` 按本声明序去重输出。"""

    SQL_LITERAL = "SQL_LITERAL"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ID_CARD = "ID_CARD"
    PERSON_OR_BUSINESS_ID = "PERSON_OR_BUSINESS_ID"
    CONNECTION_STRING = "CONNECTION_STRING"
    USERNAME_PASSWORD = "USERNAME_PASSWORD"
    TOKEN = "TOKEN"
    API_KEY = "API_KEY"
    PRIVATE_KEY = "PRIVATE_KEY"
    CERTIFICATE = "CERTIFICATE"
    LOG_FIELD = "LOG_FIELD"


class RedactionChannel(str, Enum):
    TOOL_RESULT = "TOOL_RESULT"
    EVIDENCE = "EVIDENCE"
    MODEL_INPUT = "MODEL_INPUT"
    MODEL_MESSAGES = "MODEL_MESSAGES"
    REPORT = "REPORT"
    EXPORT = "EXPORT"
    TRAINING_SAMPLE = "TRAINING_SAMPLE"


class SensitiveSpan(BaseModel):
    """一条**未能可靠替换**的命中；叶子内偏移，供人工核对定位。"""

    model_config = ConfigDict(extra="forbid")

    category: SensitiveCategory
    json_path: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    reason: str


class RedactionResult(BaseModel):
    """脱敏结果；`payload_hash` 是**脱敏前**原文哈希（不是写入载荷哈希）。"""

    model_config = ConfigDict(extra="forbid")

    payload_redacted: dict[str, Any]
    redaction_version: str
    detected_categories: tuple[SensitiveCategory, ...]
    redaction_count: int = Field(ge=0)
    unresolved_sensitive_spans: tuple[SensitiveSpan, ...]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RedactionStatus
    blockers: tuple[str, ...] = ()

    @property
    def is_passable(self) -> bool:
        """只有 CLEAN / REDACTED 才允许继续流向证据/模型/报告。"""
        return self.status in (RedactionStatus.CLEAN, RedactionStatus.REDACTED)


class RedactionSummary(BaseModel):
    """落审计/证据/报告的小载荷摘要，**不含 payload 本体**。"""

    model_config = ConfigDict(extra="forbid")

    redaction_version: str
    status: RedactionStatus
    detected_categories: tuple[SensitiveCategory, ...]
    redaction_count: int = Field(ge=0)
    source_payload_hash: str
    redacted_payload_hash: str | None = None


__all__ = [
    "RedactionChannel",
    "RedactionResult",
    "RedactionStatus",
    "RedactionSummary",
    "SensitiveCategory",
    "SensitiveSpan",
]
