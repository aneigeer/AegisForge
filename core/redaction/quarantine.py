"""隔离区落库实现（W09 `redaction_quarantine`，`database/migrations/009`）。

**硬约束：本表不得出现任何原文片段。**

- 校验不过的载荷由闸门拦下，不写 `evidence` 行、不送模型；隔离区只保留
  「哪类数据、在哪条路径、原文哈希是什么、什么时候发生」，供人工复核定位；
- `json_path` / `start` / `end` 是**偏移量**，不是值本身；
- `payload_hash` 是单向 SHA-256（引擎给出的原文哈希），不可逆推；
- 表由 `dg_ctl` 以 SELECT/INSERT 追加写入，无 UPDATE/DELETE 权限。

写入行由 `RedactionResult` 的**元数据**直接构造，`payload_redacted`（仍可能含
未脱敏结构）不参与、不落库。
"""

from __future__ import annotations

import json
from datetime import timezone
from typing import Any

from core.redaction.models import RedactionResult, SensitiveSpan
from core.tasks.models import new_id, utc_now
from database.mysql.connection import MysqlClient

REDACTION_QUARANTINE = "dg_system.redaction_quarantine"


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _span_located(span: SensitiveSpan) -> dict[str, Any]:
    """只保留定位信息：类别 + 路径 + 偏移 + 原因，**不含命中原文**。"""
    return {
        "category": span.category.value,
        "json_path": span.json_path,
        "start": span.start,
        "end": span.end,
        "reason": span.reason,
    }


class MysqlQuarantineRepository:
    """`QuarantineRepository` 协议的控制库实现（只追加）。"""

    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    def insert(
        self,
        *,
        tenant_id: str,
        task_id: str | None,
        channel: str,
        result: RedactionResult,
    ) -> str:
        quarantine_id = new_id("rq")
        self._client.execute(
            f"INSERT INTO {REDACTION_QUARANTINE} (quarantine_id, tenant_id, task_id, "
            "channel, status, payload_hash, detected_categories, "
            "unresolved_spans_json, blockers, redaction_version, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                quarantine_id,
                tenant_id,
                task_id,
                channel,
                result.status.value,
                result.payload_hash,
                _json_dump([item.value for item in result.detected_categories]),
                _json_dump(
                    [_span_located(span) for span in result.unresolved_sensitive_spans]
                ),
                _json_dump(list(result.blockers)),
                result.redaction_version,
                utc_now().astimezone(timezone.utc).replace(tzinfo=None),
            ),
        )
        return quarantine_id


__all__ = ["MysqlQuarantineRepository", "REDACTION_QUARANTINE"]
