"""W09 T01 脱敏引擎包（纯领域层：不连库、不持 DSN、不新增第三方依赖）。

公开 API 只暴露模型、规范化函数、规则常量与引擎；`RedactionGate` 接入点属 T02，
本包不导入 `core/tools/*` / `core/evidence/*`，避免反向依赖。

规则集版本：`RS_REDACTION_V2` 为当前默认（引擎 `ruleset_version` 缺省值）；
`RS_REDACTION_V1` 仅为历史数据库行与既有审计口径保留，值不得删改。
"""

from __future__ import annotations

from core.redaction.engine import DataRedactionEngine
from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
    RedactionSummary,
    SensitiveCategory,
    SensitiveSpan,
)
from core.redaction.payload_text import canonical_payload_hash, canonical_payload_text
from core.redaction.rules import (
    MAX_REDACTION_DEPTH,
    MAX_REDACTION_ITEMS,
    MAX_SCAN_CHARS,
    RS_REDACTION_V1,
    RS_REDACTION_V2,
    SENSITIVE_KEY_MARKERS,
)

__version__ = "0.1.0"

__all__ = [
    "MAX_REDACTION_DEPTH",
    "MAX_REDACTION_ITEMS",
    "MAX_SCAN_CHARS",
    "RS_REDACTION_V1",
    "RS_REDACTION_V2",
    "SENSITIVE_KEY_MARKERS",
    "DataRedactionEngine",
    "RedactionChannel",
    "RedactionResult",
    "RedactionStatus",
    "RedactionSummary",
    "SensitiveCategory",
    "SensitiveSpan",
    "canonical_payload_hash",
    "canonical_payload_text",
]
