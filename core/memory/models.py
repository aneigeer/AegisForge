"""W09 T04 五类记忆模型（全部 ``extra="forbid"``）。

设计依据：``docs/w09/02-design.md`` §5.1/§5.2/§5.3/§5.5、``docs/w09/03-task-cards.md:413-502``。

要点：

- 五类记忆封闭枚举 ``MemoryKind`` 大小 == 5（AC-13）；``KnowledgeStatus`` 5 值、
  ``ValidationLevel`` 4 值。
- ``is_indexable`` 是**唯一**可索引判定属性；解析器一律调 ``indexable_at(now)``
  并注入 ``now``，解析路径不读系统时钟（可复现）。
- 只有 ``SEMANTIC`` 且经人工审核、未撤销、在有效期内的条目可索引；
  ``PROCEDURAL`` / ``ENVIRONMENT`` 永不进入 ``available`` 授权集。
- 记忆不授权执行：模型字段与形参里都没有 ``tool_registry`` / ``gateway`` / ``dsn``
  之类的字段（AC-15）；``core/memory/**`` 不导入工具注册表 / Gateway / 数据库驱动。

``WORKING`` / ``EPISODIC`` 只存在于进程内与只读投影（``working.py`` /
``episodic.py``），``MemoryService.record`` 对这两种 kind 抛 ``KNOWLEDGE_INVALID``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

import hashlib
import json
from typing import Literal

from core.errors import DBGuardianError, ErrorCode
from core.tasks.models import utc_now


class MemoryKind(str, Enum):
    """五类记忆（封闭）：工作态 / 时间线 / 已验证知识 / SOP / 环境能力。"""

    WORKING = "WORKING"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"
    ENVIRONMENT = "ENVIRONMENT"


class KnowledgeStatus(str, Enum):
    PROPOSED = "PROPOSED"
    INDEXED = "INDEXED"
    CONFLICTED = "CONFLICTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class ValidationLevel(str, Enum):
    """验证等级，顺序：UNVERIFIED < SINGLE_CASE < REGRESSION < FIELD。"""

    UNVERIFIED = "UNVERIFIED"
    SINGLE_CASE = "SINGLE_CASE"
    REGRESSION = "REGRESSION"
    FIELD = "FIELD"


VALIDATION_LEVEL_RANK: dict[ValidationLevel, int] = {
    ValidationLevel.UNVERIFIED: 0,
    ValidationLevel.SINGLE_CASE: 1,
    ValidationLevel.REGRESSION: 2,
    ValidationLevel.FIELD: 3,
}

# 落库只允许这三类；WORKING/EPISODIC 不进入 knowledge_entries。
PERSISTABLE_KINDS: frozenset[MemoryKind] = frozenset(
    {MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL, MemoryKind.ENVIRONMENT}
)

RiskLevel = Literal["R0", "R1", "R2", "R3", "R4"]

#: P3-T1：两级确认（3-Gate 的 N-of-M）。**同一人不得包办两级**。
CONFIRMATION_STAGES: tuple[int, ...] = (1, 2)
#: 至少要几个**不同的人**确认，才允许晋升 INDEXED。
REQUIRED_DISTINCT_CONFIRMERS = 2


def content_digest(content: dict[str, Any]) -> str:
    """内容摘要（确定性序列化），用于"第一级确认后内容改动即失效"的判定。"""
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Confirmation(BaseModel):
    """一次人工确认（只追加；不覆盖、不修改）。"""

    model_config = ConfigDict(extra="forbid")

    stage: Literal[1, 2]
    confirmed_by: str = Field(min_length=1, max_length=64)
    confirmed_at: str
    content_digest: str = Field(min_length=64, max_length=64)


class KnowledgeInvalidError(DBGuardianError):
    """知识条目非法（未通过脱敏 / 不可落库 / 状态不允许）。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.KNOWLEDGE_INVALID, message, details=details)


def _parse_iso(value: str) -> datetime:
    """把 ISO-8601 字符串解析为 UTC aware datetime；非法值抛 ``ValueError``。"""
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: str | datetime) -> str:
    """归一化为固定精度的 UTC ISO 字符串，供有效期区间比较（避免表示差异误判）。"""
    moment = _parse_iso(value) if isinstance(value, str) else _as_utc(value)
    return moment.isoformat(timespec="microseconds")


class MemoryQuery(BaseModel):
    """解析请求；只含租户/引擎/版本/Adapter/能力/环境/风险，无 DSN、无 registry。"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    db_engine: str
    db_version: str
    adapter_version: str | None = None
    capabilities: tuple[str, ...] = ()
    environment_fingerprint_digest: str | None = None
    risk_level: str = "R0"
    # P3-T2：检索用的自然语言线索（症状/错误码/表名等）。为空时按"可信度"排序。
    text: str = ""


class KnowledgeEntry(BaseModel):
    """一条知识（不可变语义：撤销/过期/冲突都以**追加新 revision** 表达）。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_id: str
    revision: int = Field(ge=1)
    tenant_id: str
    kind: MemoryKind
    status: KnowledgeStatus = KnowledgeStatus.PROPOSED
    db_engine: str | None = None
    version_min: str | None = None
    version_max: str | None = None
    adapter_version: str | None = None
    capability_scope: tuple[str, ...] = ()
    environment_fingerprint_digest: str | None = None
    risk_scope: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    test_case_ids: tuple[str, ...] = ()
    validation_level: ValidationLevel = ValidationLevel.UNVERIFIED
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    # P3-T1：两级确认记录（阶段 1/2，各自记录确认人与当时的内容摘要）。
    confirmations: tuple[Confirmation, ...] = ()
    content_digest: str | None = None
    effective_from: str
    effective_until: str
    revoked_at: str | None = None
    revoked_reason: str | None = None
    source_license: str = Field(min_length=1)
    supersedes: str | None = None
    content: dict[str, Any]
    redaction_version: str

    @model_validator(mode="after")
    def _check_consistency(self) -> "KnowledgeEntry":
        if _parse_iso(self.effective_until) <= _parse_iso(self.effective_from):
            raise ValueError("effective_until 必须晚于 effective_from")
        if (self.revoked_at is None) != (self.revoked_reason is None):
            raise ValueError("revoked_at 与 revoked_reason 必须同时有或同时无")
        if (
            self.status is KnowledgeStatus.REVOKED
            and self.revoked_at is None
        ):
            raise ValueError("REVOKED 条目必须带 revoked_at")
        rank = VALIDATION_LEVEL_RANK[self.validation_level]
        if rank >= VALIDATION_LEVEL_RANK[ValidationLevel.SINGLE_CASE] and not self.evidence_ids:
            raise ValueError("validation_level >= SINGLE_CASE 时 evidence_ids 不能为空")
        return self

    @property
    def confirmed_stages(self) -> frozenset[int]:
        return frozenset(item.stage for item in self.confirmations)

    @property
    def confirmer_ids(self) -> frozenset[str]:
        return frozenset(item.confirmed_by for item in self.confirmations)

    @property
    def is_multi_confirmed(self) -> bool:
        """两级都确认、且由**至少两名不同的人**完成，且摘要与当前内容一致。"""
        if self.confirmed_stages != frozenset(CONFIRMATION_STAGES):
            return False
        if len(self.confirmer_ids) < REQUIRED_DISTINCT_CONFIRMERS:
            return False
        digest = content_digest(self.content)
        return all(item.content_digest == digest for item in self.confirmations)

    @property
    def is_indexable(self) -> bool:
        """唯一可索引判定（供人读与 AC-13 断言）；解析器请调 ``indexable_at(now)``。"""
        return self.indexable_at(utc_now())

    def indexable_at(self, now: datetime) -> bool:
        """在指定时刻是否可索引；条件与设计 §5.3 逐条一致。"""
        return (
            self.kind is MemoryKind.SEMANTIC
            and self.status is KnowledgeStatus.INDEXED
            and bool(self.reviewed_by and self.reviewed_by.strip())
            and self.reviewed_at is not None
            and self.validation_level is not ValidationLevel.UNVERIFIED
            # P3-T1：未确认或仅单级确认的知识**永不**进入可检索面。
            and self.is_multi_confirmed
            and self.revoked_at is None
            and _iso_utc(self.effective_from)
            <= _iso_utc(now)
            <= _iso_utc(self.effective_until)
        )


__all__ = [
    "KnowledgeEntry",
    "KnowledgeInvalidError",
    "KnowledgeStatus",
    "MemoryKind",
    "MemoryQuery",
    "PERSISTABLE_KINDS",
    "RiskLevel",
    "VALIDATION_LEVEL_RANK",
    "ValidationLevel",
]
