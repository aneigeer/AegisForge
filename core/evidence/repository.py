"""Evidence 持久化仓储（MySQL 8，dg_system 库，控制账号）。

约束：
- 所有读取强制携带 tenant_id，跨租户与不存在返回同一个 EvidenceInvalidError，
  不泄露其他租户的证据是否存在；
- 列表使用 (created_at, evidence_id) keyset 游标，避免 offset 分页重复。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from core.errors import EvidenceInvalidError
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceQuality,
)
from database.mysql.connection import MysqlClient

EVIDENCE = "dg_system.evidence"

_CURSOR_SEPARATOR = "|"


def _to_db(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _from_db(value: Any) -> str | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def encode_cursor(evidence: Evidence) -> str:
    return f"{evidence.created_at}{_CURSOR_SEPARATOR}{evidence.evidence_id}"


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        created_at, evidence_id = cursor.rsplit(_CURSOR_SEPARATOR, 1)
        return _to_db(created_at), evidence_id  # type: ignore[return-value]
    except (AttributeError, TypeError, ValueError):
        raise EvidenceInvalidError(
            "无效的 evidence 游标", details={"reason": "cursor_invalid"}
        ) from None


class EvidenceRepository:
    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    def insert(self, evidence: Evidence) -> Evidence:
        self._client.execute(
            f"INSERT INTO {EVIDENCE} (evidence_id, tenant_id, task_id, step_id, "
            "tool_call_id, environment_id, db_engine, db_version, adapter_version, "
            "source_tool, observed_at, time_window_start, time_window_end, "
            "collection_status, quality, completeness, payload_hash, "
            "source_payload_hash, redacted_payload_hash, redaction_version, reproducible, "
            "payload_json, redacted_payload_json, "
            "created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                evidence.evidence_id,
                evidence.tenant_id,
                evidence.task_id,
                evidence.step_id,
                evidence.tool_call_id,
                evidence.environment_id,
                evidence.db_engine,
                evidence.db_version,
                evidence.adapter_version,
                evidence.source_tool,
                _to_db(evidence.observed_at),
                _to_db(evidence.time_window_start),
                _to_db(evidence.time_window_end),
                evidence.collection_status.value,
                evidence.quality.value,
                evidence.completeness.value,
                evidence.payload_hash,
                evidence.source_payload_hash,
                evidence.redacted_payload_hash,
                evidence.redaction_version,
                int(evidence.reproducible),
                _json_dump(evidence.payload),
                _json_dump(evidence.redacted_payload)
                if evidence.redacted_payload is not None
                else None,
                _to_db(evidence.created_at),
            ),
        )
        return evidence

    def get(self, evidence_id: str, tenant_id: str) -> Evidence:
        rows = self._client.fetch_all(
            f"SELECT * FROM {EVIDENCE} WHERE evidence_id = %s AND tenant_id = %s LIMIT 1",
            (evidence_id, tenant_id),
        )
        if not rows:
            raise EvidenceInvalidError(
                "证据不存在或不属于当前租户",
                details={"evidence_id": evidence_id},
            )
        return self._to_evidence(rows[0])

    def list_by_task(
        self,
        task_id: str,
        tenant_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Evidence], str | None]:
        conditions = ["tenant_id = %s", "task_id = %s"]
        params: list[Any] = [tenant_id, task_id]
        if cursor is not None:
            created_at, evidence_id = decode_cursor(cursor)
            conditions.append(
                "(created_at > %s OR (created_at = %s AND evidence_id > %s))"
            )
            params.extend([created_at, created_at, evidence_id])
        params.append(limit + 1)
        rows = self._client.fetch_all(
            f"SELECT * FROM {EVIDENCE} WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at ASC, evidence_id ASC LIMIT %s",
            tuple(params),
        )
        items = [self._to_evidence(row) for row in rows[:limit]]
        next_cursor = encode_cursor(items[-1]) if len(rows) > limit and items else None
        return items, next_cursor

    def validate_references(
        self,
        task_id: str,
        tenant_id: str,
        environment_id: str,
        evidence_ids: list[str],
    ) -> list[str]:
        if not evidence_ids:
            return []
        placeholders = ",".join(["%s"] * len(evidence_ids))
        rows = self._client.fetch_all(
            f"SELECT evidence_id FROM {EVIDENCE} WHERE tenant_id = %s AND task_id = %s "
            f"AND environment_id = %s AND evidence_id IN ({placeholders})",
            (tenant_id, task_id, environment_id, *evidence_ids),
        )
        found = {str(row["evidence_id"]) for row in rows}
        return _invalid_ids(evidence_ids, found)

    def validate_task_references(
        self, task_id: str, tenant_id: str, evidence_ids: list[str]
    ) -> list[str]:
        """报告事实校验：只要求同一 tenant/task，环境由引用证据自身携带。"""
        if not evidence_ids:
            return []
        placeholders = ",".join(["%s"] * len(evidence_ids))
        rows = self._client.fetch_all(
            f"SELECT evidence_id FROM {EVIDENCE} WHERE tenant_id = %s AND task_id = %s "
            f"AND evidence_id IN ({placeholders})",
            (tenant_id, task_id, *evidence_ids),
        )
        found = {str(row["evidence_id"]) for row in rows}
        return _invalid_ids(evidence_ids, found)

    def count_for_task(self, task_id: str, tenant_id: str) -> int:
        rows = self._client.fetch_all(
            f"SELECT COUNT(*) AS c FROM {EVIDENCE} WHERE task_id = %s AND tenant_id = %s",
            (task_id, tenant_id),
        )
        return int(rows[0]["c"]) if rows else 0

    def fetch_by_ids(self, tenant_id: str, evidence_ids: Sequence[str]) -> list[Evidence]:
        """按 `(tenant_id, evidence_id)` 批量取回证据行（W09 `ReportEvidencePort`）。

        契约：

        - **强制租户过滤**：跨租户取不到与不存在同码（`EvidenceInvalidError`），
          不泄露"别人的证据是否存在"（沿用 `get()` 的口径）；
        - **不保证顺序、不保证全量**：返回只含命中的行，调用方自行比对缺失
          （报告侧要把缺失 id 落进 `missing_evidence`，不能静默补齐）；
        - 空入参直接返回 `[]`，不构造 `IN ()` 这种非法 SQL。
        """
        unique_ids = list(dict.fromkeys(str(item) for item in evidence_ids if item))
        if not unique_ids:
            return []
        placeholders = ",".join(["%s"] * len(unique_ids))
        rows = self._client.fetch_all(
            f"SELECT * FROM {EVIDENCE} WHERE tenant_id = %s "
            f"AND evidence_id IN ({placeholders})",
            (tenant_id, *unique_ids),
        )
        return [self._to_evidence(row) for row in rows]

    def latest_observed_at(self, task_id: str, tenant_id: str) -> str | None:
        rows = self._client.fetch_all(
            f"SELECT MAX(observed_at) AS latest FROM {EVIDENCE} "
            "WHERE task_id = %s AND tenant_id = %s",
            (task_id, tenant_id),
        )
        if not rows:
            return None
        return _from_db(rows[0].get("latest"))

    @staticmethod
    def _to_evidence(row: dict[str, Any]) -> Evidence:
        return Evidence(
            evidence_id=str(row["evidence_id"]),
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            step_id=row.get("step_id"),
            tool_call_id=row.get("tool_call_id"),
            environment_id=str(row["environment_id"]),
            db_engine=str(row["db_engine"]),
            db_version=str(row["db_version"]),
            adapter_version=str(row["adapter_version"]),
            source_tool=str(row["source_tool"]),
            observed_at=_from_db(row["observed_at"]),  # type: ignore[arg-type]
            time_window_start=_from_db(row.get("time_window_start")),
            time_window_end=_from_db(row.get("time_window_end")),
            collection_status=CollectionStatus(str(row["collection_status"])),
            quality=EvidenceQuality(str(row["quality"])),
            completeness=EvidenceCompleteness(str(row["completeness"])),
            payload_hash=str(row["payload_hash"]),
            redacted_payload_hash=row.get("redacted_payload_hash"),
            source_payload_hash=row.get("source_payload_hash"),
            redaction_version=str(row["redaction_version"]),
            reproducible=bool(row["reproducible"]),
            payload=_json_load(row.get("payload_json"), {}),
            redacted_payload=_json_load(row.get("redacted_payload_json"), None),
            created_at=_from_db(row["created_at"]),  # type: ignore[arg-type]
        )


def _invalid_ids(evidence_ids: list[str], found: set[str]) -> list[str]:
    invalid: list[str] = []
    seen: set[str] = set()
    for evidence_id in evidence_ids:
        if evidence_id in found or evidence_id in seen:
            continue
        seen.add(evidence_id)
        invalid.append(evidence_id)
    return invalid
