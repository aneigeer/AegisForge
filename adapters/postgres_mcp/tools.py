"""PostgreSQL MCP 的 7 个固定只读工具。

约束：
- 工具输入由 Pydantic 生成 JSON Schema（extra=forbid → additionalProperties=false）；
- 表名/标识符只做白名单正则校验，所有 SQL 参数走 psycopg 绑定参数；
- 只允许固定 SQL，`explain_query` 仅接受单条 SELECT/WITH，禁止 ANALYZE 与多语句；
- 结果统一编码为 ToolResult，并且不伪造不可用能力的数据。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.errors import DBGuardianError, ErrorCode
from core.tools.contracts import ToolDefinition, ToolError, ToolResult, ToolSource, ToolStatus
from database.postgres.capabilities import detect_pg_capabilities
from database.postgres.connection import PgClient

ADAPTER_VERSION = "1.0"
INTERNAL_CAPABILITIES_TOOL = "get_capabilities"
MAX_PLAN_CHARS = 50_000
MAX_QUERY_SUMMARY_CHARS = 200

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_FORBIDDEN_STATEMENTS = re.compile(
    r"\b(analy[sz]e|explain|update|insert|delete|merge|drop|alter|create|truncate|"
    r"grant|revoke|copy|vacuum|reindex|cluster|refresh|call|do|execute|prepare|"
    r"into|commit|rollback|savepoint)\b",
    re.IGNORECASE,
)
_FORBIDDEN_LOCKING = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.IGNORECASE)
_LEADING_KEYWORD = re.compile(r"^(select|with)\b", re.IGNORECASE)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionSnapshotInput(ToolInput):
    limit: int = Field(default=50, ge=1, le=500, description="最多返回的会话数")
    state: str | None = Field(default=None, max_length=64, description="按 pg_stat_activity.state 过滤")


class QueryStatsInput(ToolInput):
    limit: int = Field(default=50, ge=1, le=500, description="最多返回的语句数")


class LockWaitEdgesInput(ToolInput):
    limit: int = Field(default=100, ge=1, le=1_000, description="最多扫描的等待锁行数")


class LockDetailsInput(ToolInput):
    limit: int = Field(default=100, ge=1, le=1_000, description="最多返回的锁行数")
    granted: bool | None = Field(default=None, description="true 只看已授予，false 只看等待中")


class TransactionMetadataInput(ToolInput):
    limit: int = Field(default=100, ge=1, le=1_000, description="最多返回的事务会话数")


class ExplainQueryInput(ToolInput):
    sql: str = Field(
        ...,
        min_length=1,
        max_length=20_000,
        description="单条 SELECT/WITH 语句；禁止 ANALYZE、分号多语句与写操作",
    )

    @field_validator("sql")
    @classmethod
    def _validate_sql(cls, value: str) -> str:
        return validate_explain_sql(value)


class TableStatsInput(ToolInput):
    table: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=IDENTIFIER_PATTERN.pattern,
        description="表名（仅字母/数字/下划线，^[A-Za-z_][A-Za-z0-9_]{0,62}$）",
    )


_INPUT_MODELS: dict[str, type[ToolInput]] = {
    "get_connection_snapshot": ConnectionSnapshotInput,
    "get_query_stats": QueryStatsInput,
    "get_lock_wait_edges": LockWaitEdgesInput,
    "get_lock_details": LockDetailsInput,
    "get_transaction_metadata": TransactionMetadataInput,
    "explain_query": ExplainQueryInput,
    "get_table_stats": TableStatsInput,
}

_DESCRIPTIONS: dict[str, str] = {
    "get_connection_snapshot": "PostgreSQL 连接快照：会话状态、等待事件与脱敏查询摘要。",
    "get_query_stats": "pg_stat_statements 查询统计；扩展不可用时显式 NOT_SUPPORTED/PARTIAL，不伪造数据。",
    "get_lock_wait_edges": "基于 pg_locks 与 pg_blocking_pids 的锁等待/阻塞边与根阻塞节点。",
    "get_lock_details": "pg_locks 关联 pg_stat_activity 的锁对象、模式与 granted 状态。",
    "get_transaction_metadata": "存在 xact_start 的会话事务年龄、状态与脱敏查询摘要。",
    "explain_query": "对单条 SELECT/WITH 执行 EXPLAIN (FORMAT JSON)，返回 JSON 计划（最多 50k）。",
    "get_table_stats": "pg_class / pg_stat_user_tables / pg_total_relation_size 的表行数估算、扫描与大小。",
}

TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    name: ToolDefinition(
        name=name,
        description=_DESCRIPTIONS[name],
        version=ADAPTER_VERSION,
        risk_level="R0",
        side_effect="read",
        permissions=["postgresql:read"],
        timeout_ms=30_000,
        idempotent=True,
        audit_policy="metadata",
        input_schema=model.model_json_schema(),
        output_schema={"type": "object"},
        db_engines=["postgresql"],
        allowed_environments=["LAB"],
        required_role="dg_ro",
        operation_class="observe",
        risk_floor="R0",
    )
    for name, model in _INPUT_MODELS.items()
}


def tool_list_payload() -> list[dict[str, Any]]:
    """MCP `tools/list` 的静态载荷；不连接数据库。"""

    return [
        {
            "name": definition.name,
            "description": definition.description,
            "inputSchema": definition.input_schema,
        }
        for definition in TOOL_DEFINITIONS.values()
    ]


def tool_input_model(name: str) -> type[ToolInput] | None:
    return _INPUT_MODELS.get(name)


def validate_identifier(name: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError("标识符非法：仅允许 ^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    return name


def _strip_leading_comments(sql: str) -> str:
    text = sql.strip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline + 1 :].lstrip()
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return text
            text = text[end + 2 :].lstrip()
            continue
        return text


def validate_explain_sql(sql: str) -> str:
    """仅允许单条 SELECT/WITH；拒绝多语句、ANALYZE 与写/locking 语句。"""

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql 不能为空")
    text = _strip_leading_comments(sql)
    if not text:
        raise ValueError("sql 不能只包含注释")
    if ";" in text:
        raise ValueError("禁止分号或多语句")
    if not _LEADING_KEYWORD.match(text):
        raise ValueError("仅允许单条 SELECT 或 WITH 语句")
    if _FORBIDDEN_STATEMENTS.search(text):
        raise ValueError("语句包含禁止关键字（ANALYZE/写操作等）")
    if _FORBIDDEN_LOCKING.search(text):
        raise ValueError("禁止 FOR UPDATE/SHARE 等锁定读")
    return text


def redact_query(sql: str | None, *, max_chars: int = MAX_QUERY_SUMMARY_CHARS) -> str:
    """压缩空白并替换字符串/数字字面量，避免把参数值带入诊断结果。"""

    if not sql:
        return ""
    text = re.sub(r"\s+", " ", str(sql)).strip()
    text = _STRING_LITERAL.sub("'?'", text)
    text = _NUMBER_LITERAL.sub("?", text)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _pretty_bytes(size: int | float | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


@dataclass
class _Outcome:
    status: ToolStatus = ToolStatus.SUCCESS
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: ToolError | None = None
    truncated: bool = False


def _db_status(code: ErrorCode) -> ToolStatus:
    if code == ErrorCode.DB_TIMEOUT:
        return ToolStatus.TIMEOUT
    if code == ErrorCode.DB_PERMISSION_DENIED:
        return ToolStatus.DENIED
    return ToolStatus.ERROR


def _internal_error(message: str, details: dict[str, Any] | None = None) -> _Outcome:
    return _Outcome(
        status=ToolStatus.ERROR,
        error=ToolError(code="INTERNAL", message=message, details=details or {}),
    )


def _connection_snapshot(client: PgClient, args: ConnectionSnapshotInput) -> _Outcome:
    rows = client.fetch_all(
        """
        SELECT pid, datname, usename, application_name, client_addr::text AS client_addr,
               state, wait_event_type, wait_event, backend_start, xact_start,
               query_start, state_change, query
        FROM pg_stat_activity
        WHERE (%s::text IS NULL OR state = %s::text)
        ORDER BY backend_start ASC NULLS LAST
        LIMIT %s
        """,
        (args.state, args.state, args.limit + 1),
        max_rows=args.limit + 1,
    )
    truncated = len(rows) > args.limit
    sessions: list[dict[str, Any]] = []
    for row in rows[: args.limit]:
        query = row.get("query")
        sessions.append(
            {
                "pid": row.get("pid"),
                "database": row.get("datname"),
                "user": row.get("usename"),
                "application_name": row.get("application_name"),
                "client_addr": row.get("client_addr"),
                "state": row.get("state"),
                "wait_event_type": row.get("wait_event_type"),
                "wait_event": row.get("wait_event"),
                "backend_start": row.get("backend_start"),
                "xact_start": row.get("xact_start"),
                "query_start": row.get("query_start"),
                "state_change": row.get("state_change"),
                "query_summary": redact_query(query),
                "query_length": len(str(query)) if query else 0,
            }
        )
    return _Outcome(
        data={"sessions": sessions, "session_count": len(sessions), "limit": args.limit},
        truncated=truncated,
    )


def _query_stats(client: PgClient, args: QueryStatsInput) -> _Outcome:
    installed = client.fetch_all(
        "SELECT count(*) AS installed FROM pg_extension WHERE extname = 'pg_stat_statements'"
    )
    if not installed or int(installed[0]["installed"] or 0) == 0:
        return _Outcome(
            status=ToolStatus.NOT_SUPPORTED,
            data={"available": False, "reason": "extension_not_installed", "statements": []},
            warnings=["pg_stat_statements 扩展未安装；查询统计能力不可用，未返回任何伪造数据"],
        )

    scan_limit = 1_001
    try:
        rows = client.fetch_all(
            "SELECT * FROM pg_stat_statements LIMIT %s",
            (scan_limit,),
            max_rows=scan_limit,
        )
    except DBGuardianError as exc:
        return _Outcome(
            status=ToolStatus.PARTIAL,
            data={"available": False, "reason": "query_failed", "statements": []},
            warnings=[f"pg_stat_statements 不可查询：{exc.code.value}"],
        )

    if not rows:
        return _Outcome(
            status=ToolStatus.PARTIAL,
            data={"available": True, "statements": [], "statement_count": 0},
            warnings=["pg_stat_statements 尚无可用样本"],
        )

    keys = set(rows[0].keys())
    sort_key = "total_exec_time" if "total_exec_time" in keys else "total_time"
    if sort_key not in keys:
        return _Outcome(
            status=ToolStatus.PARTIAL,
            data={"available": True, "statements": [], "statement_count": 0},
            warnings=["pg_stat_statements 缺少耗时字段，无法排序"],
        )

    def _time_value(row: dict[str, Any]) -> float:
        try:
            return float(row.get(sort_key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(rows, key=_time_value, reverse=True)
    statements: list[dict[str, Any]] = []
    for row in ordered[: args.limit]:
        calls = int(row.get("calls") or 0)
        total_ms = _time_value(row)
        mean_ms = row.get("mean_time")
        if mean_ms is None and calls:
            mean_ms = total_ms / calls
        statements.append(
            {
                "queryid": str(row.get("queryid")) if row.get("queryid") is not None else None,
                "query_summary": redact_query(row.get("query")),
                "calls": calls,
                "total_time_ms": round(total_ms, 3),
                "mean_time_ms": round(float(mean_ms), 3) if mean_ms is not None else None,
                "rows": int(row.get("rows") or 0),
                "shared_blks_hit": int(row.get("shared_blks_hit") or 0),
                "shared_blks_read": int(row.get("shared_blks_read") or 0),
                "temp_blks_read": int(row.get("temp_blks_read") or 0),
                "temp_blks_written": int(row.get("temp_blks_written") or 0),
            }
        )
    return _Outcome(
        data={
            "available": True,
            "statements": statements,
            "statement_count": len(statements),
            "sorted_by": sort_key,
        },
        truncated=len(rows) > args.limit or len(rows) >= scan_limit,
    )


def _lock_wait_edges(client: PgClient, args: LockWaitEdgesInput) -> _Outcome:
    rows = client.fetch_all(
        """
        SELECT l.pid, l.locktype, l.mode, l.relation,
               l.transactionid::text AS transactionid,
               n.nspname AS schema_name, c.relname AS relation_name,
               a.usename, a.state, a.query, a.query_start,
               pg_blocking_pids(l.pid) AS blocking_pids,
               EXTRACT(EPOCH FROM (now() - a.query_start)) AS wait_seconds
        FROM pg_locks l
        LEFT JOIN pg_stat_activity a ON a.pid = l.pid
        LEFT JOIN pg_class c ON c.oid = l.relation
        LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT l.granted
        ORDER BY l.pid
        LIMIT %s
        """,
        (args.limit + 1,),
        max_rows=args.limit + 1,
    )
    truncated = len(rows) > args.limit
    edges: list[dict[str, Any]] = []
    waiting_pids: set[int] = set()
    blocking_pids: set[int] = set()
    edge_keys: set[tuple[int, int]] = set()
    for row in rows[: args.limit]:
        pid = int(row["pid"])
        waiting_pids.add(pid)
        blockers = [int(item) for item in (row.get("blocking_pids") or [])]
        for blocker in blockers:
            blocking_pids.add(blocker)
            key = (pid, blocker)
            if key in edge_keys:
                continue
            edge_keys.add(key)
            wait_seconds = row.get("wait_seconds")
            edges.append(
                {
                    "blocked_pid": pid,
                    "blocking_pid": blocker,
                    "wait_seconds": round(float(wait_seconds), 3) if wait_seconds is not None else None,
                    "lock_type": row.get("locktype"),
                    "mode": row.get("mode"),
                    "schema": row.get("schema_name"),
                    "relation": row.get("relation_name"),
                    "transactionid": row.get("transactionid"),
                    "state": row.get("state"),
                    "query_summary": redact_query(row.get("query")),
                }
            )
    root_pids = sorted(blocking_pids - waiting_pids)
    return _Outcome(
        data={
            "edges": edges,
            "edge_count": len(edges),
            "waiting_pids": sorted(waiting_pids),
            "blocking_pids": sorted(blocking_pids),
            "root_pids": root_pids,
            "wait_count": len(waiting_pids),
        },
        truncated=truncated,
    )


def _lock_details(client: PgClient, args: LockDetailsInput) -> _Outcome:
    rows = client.fetch_all(
        """
        SELECT l.pid, l.locktype, l.mode, l.granted,
               l.database, l.relation, l.page, l.tuple,
               l.virtualxid, l.transactionid::text AS transactionid,
               l.classid, l.objid, l.objsubid, l.virtualtransaction,
               n.nspname AS schema_name, c.relname AS relation_name,
               a.usename, a.datname, a.state, a.query
        FROM pg_locks l
        LEFT JOIN pg_class c ON c.oid = l.relation
        LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_activity a ON a.pid = l.pid
        WHERE (%s::boolean IS NULL OR l.granted = %s::boolean)
        ORDER BY l.pid, l.locktype, l.mode
        LIMIT %s
        """,
        (args.granted, args.granted, args.limit + 1),
        max_rows=args.limit + 1,
    )
    truncated = len(rows) > args.limit
    locks: list[dict[str, Any]] = []
    granted_count = 0
    waiting_count = 0
    for row in rows[: args.limit]:
        is_granted = bool(row.get("granted"))
        granted_count += 1 if is_granted else 0
        waiting_count += 0 if is_granted else 1
        locks.append(
            {
                "pid": row.get("pid"),
                "lock_type": row.get("locktype"),
                "mode": row.get("mode"),
                "granted": is_granted,
                "database": row.get("database"),
                "relation": row.get("relation"),
                "schema": row.get("schema_name"),
                "relation_name": row.get("relation_name"),
                "page": row.get("page"),
                "tuple": row.get("tuple"),
                "virtualxid": row.get("virtualxid"),
                "transactionid": row.get("transactionid"),
                "classid": row.get("classid"),
                "objid": row.get("objid"),
                "objsubid": row.get("objsubid"),
                "virtualtransaction": row.get("virtualtransaction"),
                "user": row.get("usename"),
                "database_name": row.get("datname"),
                "state": row.get("state"),
                "query_summary": redact_query(row.get("query")),
            }
        )
    return _Outcome(
        data={
            "locks": locks,
            "lock_count": len(locks),
            "granted_count": granted_count,
            "waiting_count": waiting_count,
        },
        truncated=truncated,
    )


def _transaction_metadata(client: PgClient, args: TransactionMetadataInput) -> _Outcome:
    rows = client.fetch_all(
        """
        SELECT pid, datname, usename, state,
               xact_start, query_start, state_change,
               backend_xid::text AS backend_xid, backend_xmin::text AS backend_xmin,
               wait_event_type, wait_event, query,
               EXTRACT(EPOCH FROM (now() - xact_start)) AS xact_age_seconds,
               EXTRACT(EPOCH FROM (now() - query_start)) AS query_age_seconds
        FROM pg_stat_activity
        WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()
        ORDER BY xact_start ASC
        LIMIT %s
        """,
        (args.limit + 1,),
        max_rows=args.limit + 1,
    )
    truncated = len(rows) > args.limit
    transactions: list[dict[str, Any]] = []
    max_age = 0.0
    for row in rows[: args.limit]:
        age = row.get("xact_age_seconds")
        age_value = float(age) if age is not None else None
        if age_value is not None:
            max_age = max(max_age, age_value)
        state = row.get("state")
        transactions.append(
            {
                "pid": row.get("pid"),
                "database": row.get("datname"),
                "user": row.get("usename"),
                "state": state,
                "xact_start": row.get("xact_start"),
                "query_start": row.get("query_start"),
                "state_change": row.get("state_change"),
                "transaction_age_seconds": round(age_value, 3) if age_value is not None else None,
                "query_age_seconds": round(float(row["query_age_seconds"]), 3)
                if row.get("query_age_seconds") is not None
                else None,
                "backend_xid": row.get("backend_xid"),
                "backend_xmin": row.get("backend_xmin"),
                "wait_event_type": row.get("wait_event_type"),
                "wait_event": row.get("wait_event"),
                "idle_in_transaction": state in {"idle in transaction", "idle in transaction (aborted)"},
                "query_summary": redact_query(row.get("query")),
            }
        )
    return _Outcome(
        data={
            "transactions": transactions,
            "transaction_count": len(transactions),
            "max_transaction_age_seconds": round(max_age, 3) if transactions else None,
        },
        truncated=truncated,
    )


def _explain_query(client: PgClient, args: ExplainQueryInput) -> _Outcome:
    rows = client.fetch_all("EXPLAIN (FORMAT JSON) " + args.sql)
    if not rows:
        return _internal_error("EXPLAIN 未返回计划")
    plan = rows[0].get("QUERY PLAN")
    plan_text = json.dumps(_jsonable(plan), ensure_ascii=False)
    plan_bytes = len(plan_text.encode("utf-8"))
    if len(plan_text) > MAX_PLAN_CHARS:
        return _Outcome(
            status=ToolStatus.PARTIAL,
            data={
                "plan": plan_text[:MAX_PLAN_CHARS],
                "plan_truncated": True,
                "plan_is_complete_json": False,
                "plan_bytes": plan_bytes,
            },
            warnings=[f"执行计划超过 {MAX_PLAN_CHARS} 字符，已截断；plan 文本不是完整 JSON"],
            truncated=True,
        )
    return _Outcome(
        data={"plan": plan, "plan_truncated": False, "plan_bytes": plan_bytes},
    )


def _table_stats(client: PgClient, args: TableStatsInput) -> _Outcome:
    validate_identifier(args.table)
    rows = client.fetch_all(
        """
        SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind,
               c.reltuples AS estimated_rows, c.relpages AS pages,
               s.seq_scan, s.seq_tup_read, s.idx_scan, s.idx_tup_fetch,
               s.n_live_tup, s.n_dead_tup,
               s.last_vacuum, s.last_autovacuum, s.last_analyze,
               pg_total_relation_size(c.oid) AS total_bytes,
               pg_relation_size(c.oid) AS heap_bytes,
               pg_indexes_size(c.oid) AS index_bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE c.relname = %s AND c.relkind IN ('r', 'p', 'm')
        ORDER BY n.nspname
        LIMIT 10
        """,
        (args.table,),
    )
    if not rows:
        return _Outcome(
            status=ToolStatus.ERROR,
            error=ToolError(
                code="TABLE_NOT_FOUND",
                message="未找到指定表",
                details={"table": args.table},
            ),
        )
    tables: list[dict[str, Any]] = []
    for row in rows:
        total_bytes = row.get("total_bytes")
        tables.append(
            {
                "schema": row.get("schema_name"),
                "table": row.get("table_name"),
                "relkind": row.get("relkind"),
                "estimated_rows": int(float(row.get("estimated_rows") or 0)),
                "estimated_rows_is_estimate": True,
                "pages": row.get("pages"),
                "seq_scan": row.get("seq_scan"),
                "seq_tup_read": row.get("seq_tup_read"),
                "idx_scan": row.get("idx_scan"),
                "idx_tup_fetch": row.get("idx_tup_fetch"),
                "n_live_tup": row.get("n_live_tup"),
                "n_dead_tup": row.get("n_dead_tup"),
                "last_vacuum": row.get("last_vacuum"),
                "last_autovacuum": row.get("last_autovacuum"),
                "last_analyze": row.get("last_analyze"),
                "total_bytes": total_bytes,
                "total_size": _pretty_bytes(total_bytes),
                "heap_bytes": row.get("heap_bytes"),
                "index_bytes": row.get("index_bytes"),
            }
        )
    first = tables[0]
    data: dict[str, Any] = {
        "table": args.table,
        "tables": tables,
        "table_count": len(tables),
        "estimated_rows": first["estimated_rows"],
        "seq_scan": first["seq_scan"],
        "idx_scan": first["idx_scan"],
        "total_bytes": first["total_bytes"],
        "total_size": first["total_size"],
    }
    return _Outcome(data=data)


def _capabilities(client: PgClient) -> _Outcome:
    report = detect_pg_capabilities(client)
    capabilities = {
        "pg_stat_activity": report.pg_stat_activity,
        "pg_locks": report.pg_locks,
        "pg_blocking_pids": report.pg_blocking_pids,
        "pg_stat_statements": report.pg_stat_statements,
    }
    status = (
        ToolStatus.SUCCESS
        if all(value == "AVAILABLE" for value in capabilities.values()) and not report.warnings
        else ToolStatus.PARTIAL
    )
    return _Outcome(
        status=status,
        data={
            "database_id": report.database_id,
            "server_version": report.server_version,
            "capabilities": capabilities,
        },
        warnings=list(report.warnings),
    )


_HANDLERS: dict[str, Callable[[PgClient, Any], _Outcome]] = {
    "get_connection_snapshot": _connection_snapshot,
    "get_query_stats": _query_stats,
    "get_lock_wait_edges": _lock_wait_edges,
    "get_lock_details": _lock_details,
    "get_transaction_metadata": _transaction_metadata,
    "explain_query": _explain_query,
    "get_table_stats": _table_stats,
}


def _result(
    tool_name: str,
    outcome: _Outcome,
    *,
    started_at: str,
    started_monotonic: float,
    database_id: str,
    db_version: str,
    capability_ref: str | None,
) -> ToolResult:
    observed_at = _now_iso()
    status = outcome.status
    warnings = list(outcome.warnings)
    if outcome.truncated and status == ToolStatus.SUCCESS:
        status = ToolStatus.PARTIAL
        if "结果已截断，未返回完整数据" not in warnings:
            warnings.append("结果已截断，未返回完整数据")
    return ToolResult(
        tool_name=tool_name,
        status=status,
        started_at=started_at,
        duration_ms=round((time.perf_counter() - started_monotonic) * 1000, 2),
        data=_jsonable(outcome.data),
        warnings=warnings,
        error=outcome.error,
        source=ToolSource(
            database_id=database_id,
            engine="postgresql",
            db_version=db_version,
            collected_at=observed_at,
            adapter_version=ADAPTER_VERSION,
        ),
        observed_at=observed_at,
        truncated=outcome.truncated,
        capability_ref=capability_ref,
        source_metadata={"adapter": "postgres-mcp"},
    )


def execute_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    client: PgClient,
    database_id: str,
    db_version: str,
    capability_ref: str | None = None,
    include_internal: bool = False,
) -> ToolResult:
    """执行固定工具；未知工具立即失败，绝不拼接或执行调用方 SQL。"""

    started_at = _now_iso()
    started_monotonic = time.perf_counter()
    args = arguments or {}

    if not isinstance(name, str):
        outcome = _internal_error("工具名非法")
    elif name == INTERNAL_CAPABILITIES_TOOL:
        if include_internal:
            try:
                outcome = _capabilities(client)
            except DBGuardianError as exc:
                outcome = _Outcome(
                    status=_db_status(exc.code),
                    error=ToolError(
                        code=exc.code.value, message=exc.message, details=dict(exc.details)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 返回结构化错误而非崩溃
                outcome = _internal_error("内部能力探测失败", {"exception": type(exc).__name__})
        else:
            outcome = _Outcome(
                status=ToolStatus.ERROR,
                error=ToolError(
                    code="TOOL_NOT_FOUND",
                    message=f"未知工具：{name}",
                    details={"tool_name": name},
                ),
            )
    elif name not in _INPUT_MODELS:
        outcome = _Outcome(
            status=ToolStatus.ERROR,
            error=ToolError(
                code="TOOL_NOT_FOUND",
                message=f"未知工具：{name}",
                details={"tool_name": name},
            ),
        )
    elif not isinstance(args, dict):
        outcome = _Outcome(
            status=ToolStatus.ERROR,
            error=ToolError(code="REQUEST_INVALID", message="arguments 必须是对象"),
        )
    else:
        model = _INPUT_MODELS[name]
        try:
            parsed = model.model_validate(args)
        except ValidationError as exc:
            outcome = _Outcome(
                status=ToolStatus.ERROR,
                error=ToolError(
                    code="REQUEST_INVALID",
                    message="工具参数校验失败",
                    details={
                        "errors": [
                            {
                                "loc": list(item["loc"]),
                                "msg": item["msg"],
                                "type": item["type"],
                            }
                            for item in exc.errors(include_url=False, include_input=False)
                        ]
                    },
                ),
            )
        else:
            try:
                outcome = _HANDLERS[name](client, parsed)
            except DBGuardianError as exc:
                outcome = _Outcome(
                    status=_db_status(exc.code),
                    error=ToolError(
                        code=exc.code.value, message=exc.message, details=dict(exc.details)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 外部输入导致的所有异常都结构化
                outcome = _internal_error(
                    "工具执行失败", {"exception": type(exc).__name__}
                )

    return _result(
        name,
        outcome,
        started_at=started_at,
        started_monotonic=started_monotonic,
        database_id=database_id,
        db_version=db_version,
        capability_ref=capability_ref,
    )


__all__ = [
    "ADAPTER_VERSION",
    "IDENTIFIER_PATTERN",
    "INTERNAL_CAPABILITIES_TOOL",
    "MAX_PLAN_CHARS",
    "TOOL_DEFINITIONS",
    "ConnectionSnapshotInput",
    "ExplainQueryInput",
    "LockDetailsInput",
    "LockWaitEdgesInput",
    "QueryStatsInput",
    "TableStatsInput",
    "ToolInput",
    "TransactionMetadataInput",
    "execute_tool",
    "redact_query",
    "tool_input_model",
    "tool_list_payload",
    "validate_explain_sql",
    "validate_identifier",
]
