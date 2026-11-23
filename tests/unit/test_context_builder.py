"""W04 单元测试：Context Builder 的工具目录、证据摘要与 prompt 渲染。"""

from __future__ import annotations

import json

from core.context.builder import ContextBuilder
from core.context.contracts import EvidenceItem
from core.models.contracts import ModelMessage
from core.tools.contracts import ToolError, ToolResult, ToolSource, ToolStatus
from database.mysql.connection import MysqlIdentity
from database.observers import build_observer_registry
from database.observers.utils import utc_now_iso


def _identity() -> MysqlIdentity:
    return MysqlIdentity(
        database_id="lab_mysql_01",
        user="dg_ro@%",
        database="dg_lab",
        server_version="8.0.46",
        version_comment="MySQL Community Server",
        transaction_read_only=1,
        collected_at="2026-11-06T00:00:00+00:00",
    )


def _result(data: dict, *, status: ToolStatus = ToolStatus.SUCCESS) -> ToolResult:
    return ToolResult(
        tool_name="get_locks",
        status=status,
        task_id="task_1",
        run_id="run_1",
        tool_call_id="call_1",
        started_at=utc_now_iso(),
        duration_ms=12.5,
        data=data,
        warnings=["sample warning"],
        error=None if status == ToolStatus.SUCCESS else ToolError(code="X", message="m"),
        source=ToolSource(
            database_id="lab_mysql_01",
            db_version="8.0.46",
            collected_at="2026-11-06T00:00:00+00:00",
        ),
    )


def test_build_exposes_tool_catalog_and_permissions() -> None:
    registry = build_observer_registry()
    builder = ContextBuilder(registry)
    bundle = builder.build(
        goal="诊断锁等待",
        identity=_identity(),
        read_only=True,
        budget={"max_steps": 8},
    )
    assert len(bundle.tools) == len(registry.definitions())
    assert bundle.permissions == ["mysql:read"]
    assert bundle.database_id == "lab_mysql_01"
    assert bundle.db_version == "8.0.46"
    assert bundle.budget["max_steps"] == 8
    assert "不可信" in bundle.untrusted_notice

    writable = builder.build(
        goal="g", identity=_identity(), read_only=False, budget={}
    )
    assert set(writable.permissions) == {"mysql:read", "mysql:write"}


def test_evidence_is_truncated_to_recent_items() -> None:
    registry = build_observer_registry()
    builder = ContextBuilder(registry, max_evidence_items=3)
    evidence = [
        EvidenceItem(
            evidence_id=f"e{i}", tool_name="get_locks", status="success", summary=str(i)
        )
        for i in range(6)
    ]
    bundle = builder.build(
        goal="g", identity=_identity(), read_only=True, budget={}, evidence=evidence
    )
    assert [item.evidence_id for item in bundle.evidence] == ["e3", "e4", "e5"]
    assert bundle.truncated_evidence == 3


def test_summarize_result_digest_is_stable_and_source_kept() -> None:
    builder = ContextBuilder(build_observer_registry())
    first = builder.summarize_result(_result({"b": 2, "a": 1}))
    second = builder.summarize_result(_result({"a": 1, "b": 2}))
    assert first.data_digest == second.data_digest
    other = builder.summarize_result(_result({"a": 2}))
    assert first.data_digest != other.data_digest
    assert first.database_id == "lab_mysql_01"
    assert first.db_version == "8.0.46"
    assert first.status == "success"
    assert first.warnings == ["sample warning"]


def test_summary_is_truncated() -> None:
    builder = ContextBuilder(build_observer_registry(), max_summary_chars=40)
    summary = builder.summarize_result(_result({"payload": "x" * 100}))
    assert summary.summary.endswith("...<truncated>")
    assert len(summary.summary) <= 40 + len("...<truncated>")


def test_render_messages_has_security_notice_and_no_dsn() -> None:
    builder = ContextBuilder(build_observer_registry())
    bundle = builder.build(
        goal="诊断慢查询", identity=_identity(), read_only=True, budget={"max_steps": 2}
    )
    messages = builder.render_messages(bundle, "系统安全提示")
    assert [message.role for message in messages] == ["system", "user"]
    assert messages[0].content == "系统安全提示"
    payload = json.loads(messages[1].content)
    assert payload["goal"] == "诊断慢查询"
    assert payload["untrusted_data_notice"]
    text = messages[1].content
    assert "mysql://" not in text
    assert "CHANGE_ME" not in text
