"""W09 T04 知识仓储（``dg_system.knowledge_entries``，**只追加**）。

约束：

- 只发 ``INSERT`` 与只读 ``SELECT``；不发 ``UPDATE`` / ``DELETE`` / ``TRUNCATE``
  （``dg_ctl`` 也没有这些授权）。
- ``WORKING`` / ``EPISODIC`` 不进入落库路径：``insert`` 直接拒绝。
- 本模块**不导入**数据库驱动（``database.mysql`` / ``database.postgres``）：
  客户端能力以本地 ``Protocol`` 声明，避免 ``core/memory/**`` 反向依赖连接层。
- 版本链以 ``(knowledge_id, revision)`` 主键追加表达；读取一律强制租户过滤。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from core.memory.models import (
    PERSISTABLE_KINDS,
    KnowledgeEntry,
    KnowledgeInvalidError,
    KnowledgeStatus,
    MemoryKind,
    ValidationLevel,
)

KNOWLEDGE_ENTRIES = "dg_system.knowledge_entries"


class SqlClient(Protocol):
    """最小 SQL 客户端契约（与 ``database.mysql.connection.MysqlClient`` 同形）。"""

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]: ...


def _to_db(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _from_db(value: Any) -> str | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value)
    )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, default=str, separators=(",", ":")
    )


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


class KnowledgeRepository:
    """知识条目只追加仓储；``client`` 由组合根注入（本类不认识账号）。"""

    def __init__(self, client: SqlClient) -> None:
        self._client = client

    @property
    def client(self) -> SqlClient:
        return self._client

    def insert(self, entry: KnowledgeEntry) -> KnowledgeEntry:
        """写一条 revision；``WORKING`` / ``EPISODIC`` 拒绝落库。"""
        if entry.kind not in PERSISTABLE_KINDS:
            raise KnowledgeInvalidError(
                "WORKING/EPISODIC 不进入 knowledge_entries 落库路径",
                details={
                    "knowledge_id": entry.knowledge_id,
                    "kind": entry.kind.value,
                    "reason": "KIND_NOT_PERSISTABLE",
                },
            )
        self._client.execute(
            f"INSERT INTO {KNOWLEDGE_ENTRIES} (knowledge_id, revision, tenant_id, "
            "kind, status, db_engine, version_min, version_max, adapter_version, "
            "capability_scope_json, environment_fingerprint_digest, "
            "risk_scope_json, evidence_ids_json, test_case_ids_json, "
            "validation_level, reviewed_by, reviewed_at, confirmations_json, "
            "content_digest, effective_from, "
            "effective_until, revoked_at, revoked_reason, source_license, "
            "supersedes, content_json, redaction_version, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entry.knowledge_id,
                entry.revision,
                entry.tenant_id,
                entry.kind.value,
                entry.status.value,
                entry.db_engine,
                entry.version_min,
                entry.version_max,
                entry.adapter_version,
                _json_dump(list(entry.capability_scope)),
                entry.environment_fingerprint_digest,
                _json_dump(list(entry.risk_scope)),
                _json_dump(list(entry.evidence_ids)),
                _json_dump(list(entry.test_case_ids)),
                entry.validation_level.value,
                entry.reviewed_by,
                _to_db(entry.reviewed_at),
                _json_dump([item.model_dump(mode="json") for item in entry.confirmations]),
                entry.content_digest,
                _to_db(entry.effective_from),
                _to_db(entry.effective_until),
                _to_db(entry.revoked_at),
                entry.revoked_reason,
                entry.source_license,
                entry.supersedes,
                _json_dump(entry.content),
                entry.redaction_version,
                datetime.now(timezone.utc).replace(tzinfo=None),
            ),
        )
        return entry

    def get(self, knowledge_id: str, revision: int) -> KnowledgeEntry | None:
        """按主键读取单个 revision（供 review/revoke 追加新版本）。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {KNOWLEDGE_ENTRIES} "
            "WHERE knowledge_id = %s AND revision = %s LIMIT 1",
            (knowledge_id, int(revision)),
        )
        return self._to_entry(rows[0]) if rows else None

    def latest(self, knowledge_id: str, *, tenant_id: str) -> KnowledgeEntry | None:
        """读取某知识的最新 revision（强制租户过滤）。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {KNOWLEDGE_ENTRIES} "
            "WHERE knowledge_id = %s AND tenant_id = %s "
            "ORDER BY revision DESC LIMIT 1",
            (knowledge_id, tenant_id),
        )
        return self._to_entry(rows[0]) if rows else None

    def list_versions(
        self, knowledge_id: str, *, tenant_id: str
    ) -> tuple[KnowledgeEntry, ...]:
        """返回全部 revision（升序）；撤销后仍可追溯历史。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {KNOWLEDGE_ENTRIES} "
            "WHERE knowledge_id = %s AND tenant_id = %s ORDER BY revision ASC",
            (knowledge_id, tenant_id),
        )
        return tuple(self._to_entry(row) for row in rows)

    def list_by_tenant(
        self, tenant_id: str, limit: int = 500
    ) -> tuple[KnowledgeEntry, ...]:
        """只读列出某租户的知识（供解析器候选集）。"""
        safe_limit = max(1, min(int(limit), 2000))
        rows = self._client.fetch_all(
            f"SELECT * FROM {KNOWLEDGE_ENTRIES} WHERE tenant_id = %s "
            "ORDER BY knowledge_id ASC, revision ASC LIMIT %s",
            (tenant_id, safe_limit),
        )
        return tuple(self._to_entry(row) for row in rows)

    def list_expired(self, now: datetime) -> tuple[str, ...]:
        """只读给出**最新 revision 已过期**的 knowledge_id 清单（不改历史行）。"""
        reference = (
            now
            if now.tzinfo is None
            else now.astimezone(timezone.utc).replace(tzinfo=None)
        )
        rows = self._client.fetch_all(
            f"SELECT knowledge_id, revision, effective_until FROM {KNOWLEDGE_ENTRIES} "
            "WHERE status NOT IN (%s, %s) ORDER BY knowledge_id ASC, revision ASC",
            (KnowledgeStatus.REVOKED.value, KnowledgeStatus.EXPIRED.value),
        )
        latest: dict[str, Any] = {}
        for row in rows:
            latest[str(row["knowledge_id"])] = row
        expired: list[str] = []
        for knowledge_id, row in latest.items():
            until = row.get("effective_until")
            moment = until if isinstance(until, datetime) else datetime.fromisoformat(
                str(until)
            )
            if moment.tzinfo is not None:
                moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
            if moment < reference:
                expired.append(knowledge_id)
        return tuple(sorted(expired))

    # ---- mapping ---------------------------------------------------------
    @staticmethod
    def _to_entry(row: dict[str, Any]) -> KnowledgeEntry:
        return KnowledgeEntry(
            knowledge_id=str(row["knowledge_id"]),
            revision=int(row["revision"]),
            tenant_id=str(row["tenant_id"]),
            kind=MemoryKind(str(row["kind"])),
            status=KnowledgeStatus(str(row["status"])),
            db_engine=(
                str(row["db_engine"]) if row.get("db_engine") is not None else None
            ),
            version_min=(
                str(row["version_min"]) if row.get("version_min") is not None else None
            ),
            version_max=(
                str(row["version_max"]) if row.get("version_max") is not None else None
            ),
            adapter_version=(
                str(row["adapter_version"])
                if row.get("adapter_version") is not None
                else None
            ),
            capability_scope=tuple(
                _json_load(row.get("capability_scope_json"), [])
            ),
            environment_fingerprint_digest=(
                str(row["environment_fingerprint_digest"])
                if row.get("environment_fingerprint_digest") is not None
                else None
            ),
            risk_scope=tuple(_json_load(row.get("risk_scope_json"), [])),
            evidence_ids=tuple(_json_load(row.get("evidence_ids_json"), [])),
            test_case_ids=tuple(_json_load(row.get("test_case_ids_json"), [])),
            validation_level=ValidationLevel(str(row["validation_level"])),
            reviewed_by=(
                str(row["reviewed_by"]) if row.get("reviewed_by") is not None else None
            ),
            reviewed_at=_from_db(row.get("reviewed_at")),
            confirmations=tuple(
                _json_load(row.get("confirmations_json"), []) or []
            ),
            content_digest=(
                str(row["content_digest"]) if row.get("content_digest") is not None else None
            ),
            effective_from=_from_db(row.get("effective_from")) or "",
            effective_until=_from_db(row.get("effective_until")) or "",
            revoked_at=_from_db(row.get("revoked_at")),
            revoked_reason=(
                str(row["revoked_reason"])
                if row.get("revoked_reason") is not None
                else None
            ),
            source_license=str(row["source_license"]),
            supersedes=(
                str(row["supersedes"]) if row.get("supersedes") is not None else None
            ),
            content=dict(_json_load(row.get("content_json"), {})),
            redaction_version=str(row["redaction_version"]),
        )


__all__ = ["KNOWLEDGE_ENTRIES", "KnowledgeRepository", "SqlClient"]
