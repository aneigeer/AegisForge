"""载荷规范化文本与哈希（W09 T01，全仓唯一实现）。

口径与 `core/evidence/service.py::canonical_payload_hash` 逐字一致：
`json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)`
后取 UTF-8 的 SHA-256。**不使用 `core/security/canonical.py::canonical_digest`**——
后者拒绝 float/Decimal/datetime/bytes/set，而工具结果必然含 `duration_ms`（float），
用它会把"脱敏"实现成"全量阻断"。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_payload_text(payload: Any) -> str:
    """任意可 JSON 化载荷 → 稳定文本（键按码点排序、保留非 ASCII）。"""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def canonical_payload_hash(payload: Any) -> str:
    """`sha256(UTF-8(canonical_payload_text(payload)))`，64 位小写 hex。"""
    return hashlib.sha256(canonical_payload_text(payload).encode("utf-8")).hexdigest()


__all__ = ["canonical_payload_hash", "canonical_payload_text"]
