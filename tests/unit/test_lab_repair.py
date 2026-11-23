"""W07 D1/D3：MySQL LAB 唯一受限写工具的单元测试（不连接数据库）。

验证重点：服务端白名单、目标对象与主键同源、执行前基线、
幂等重放、只读连接拒绝，以及"另一引擎仍只有只读工具"。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from adapters.mysql.lab_repair import (
    ALLOWED_DATABASE,
    ALLOWED_TABLE,
    TOOL_NAME,
    LabOrderNoteUpdateTool,
)
from core.tools.contracts import ToolContext, ToolStatus
from database.observers import build_observer_registry


class _FakeClient:
    def __init__(
        self,
        note: str | None = None,
        *,
        affected: int = 1,
        read_only: bool = False,
        after_note: str | None = None,
    ) -> None:
        self.database_id = "lab_mysql_01"
        self.read_only = read_only
        self._note = note
        self._affected = affected
        # 模拟"两次读之间对象已被并发更新"：第二次 SELECT 返回 after_note。
        self._after_note = after_note
        self._select_count = 0
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def fetch_all(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        if "SELECT" in sql:
            self._select_count += 1
            if self._select_count > 1 and self._after_note is not None:
                return [{"note": self._after_note}]
            return [{"note": self._note}]
        return []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        self.executed.append((sql, params))
        if self._affected and params:
            self._note = params[0]
        return self._affected

    def identity(self) -> SimpleNamespace:
        return SimpleNamespace(server_version="8.0.46")


def _context(client: _FakeClient) -> ToolContext:
    return ToolContext(
        client=client,  # type: ignore[arg-type]
        task_id="task_1",
        run_id="run_1",
        tool_call_id="call_1",
    )


def _params(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "database": ALLOWED_DATABASE,
        "table": ALLOWED_TABLE,
        "order_id": 2001,
        "keys": "order_id=2001",
        "note": "repaired-by-w07",
        "expected_note": None,
    }
    base.update(overrides)
    return base


def test_definition_is_single_r2_write_with_declared_target() -> None:
    definition = LabOrderNoteUpdateTool().definition
    assert definition.name == TOOL_NAME
    assert definition.side_effect == "write"
    assert definition.risk_level == "R2"
    assert definition.db_engines == ["mysql"]
    assert definition.allowed_environments == ["LAB"]
    assert definition.permissions == ["mysql:write"]
    assert definition.required_role == "dg_rw"
    assert definition.target_objects, "写工具必须声明目标对象抽取规则"
    assert definition.target_objects[0].required is True


def test_default_registry_stays_read_only() -> None:
    """W07 验收：另一引擎保持只读；写工具不得混进默认只读注册表。"""
    for definition in build_observer_registry().definitions():
        assert definition.side_effect != "write", definition.name
    assert TOOL_NAME not in build_observer_registry().names()


def test_success_updates_single_row_with_before_after() -> None:
    client = _FakeClient(note=None)
    result = LabOrderNoteUpdateTool().run(_context(client), _params())

    assert result.status is ToolStatus.SUCCESS, result.error
    assert result.data["before"] is None
    assert result.data["after"] == "repaired-by-w07"
    assert result.data["affected_rows"] == 1
    assert result.data["idempotent_noop"] is False
    assert len(client.executed) == 1
    assert "LIMIT 1" in client.executed[0][0]


def test_prerequisite_change_is_denied_without_any_write() -> None:
    client = _FakeClient(note="changed-by-someone-else")
    result = LabOrderNoteUpdateTool().run(_context(client), _params())

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "PREREQUISITE_CHANGED"
    assert client.executed == [], "前置条件变化时必须一条 UPDATE 都不发"


def test_target_key_mismatch_is_denied() -> None:
    client = _FakeClient(note=None)
    result = LabOrderNoteUpdateTool().run(
        _context(client), _params(keys="order_id=2002")
    )

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "TARGET_KEY_MISMATCH"
    assert client.executed == []


def test_extra_field_or_out_of_range_key_is_rejected() -> None:
    tool = LabOrderNoteUpdateTool()
    extra = tool.run(_context(_FakeClient()), {**_params(), "sql": "DROP TABLE x"})
    assert extra.status is ToolStatus.DENIED
    assert extra.error is not None
    assert extra.error.code == "REQUEST_INVALID"

    out_of_range = tool.run(_context(_FakeClient()), _params(order_id=9999))
    assert out_of_range.status is ToolStatus.DENIED
    assert out_of_range.error is not None
    assert out_of_range.error.code == "REQUEST_INVALID"


def test_read_only_connection_is_denied() -> None:
    client = _FakeClient(note=None, read_only=True)
    result = LabOrderNoteUpdateTool().run(_context(client), _params())

    assert result.status is ToolStatus.DENIED
    assert result.error is not None
    assert result.error.code == "READ_ONLY_CONNECTION"
    assert client.executed == []


def test_idempotent_replay_is_success_noop() -> None:
    """并发窗口：两次读之间对象已被前一次相同操作更新，结果已达成。

    基线检查必须通过（before == expected_note），只是 UPDATE 因
    `WHERE note <=> expected_note` 不再匹配而返回 affected=0。
    """
    client = _FakeClient(
        note=None, affected=0, after_note="repaired-by-w07"
    )
    result = LabOrderNoteUpdateTool().run(_context(client), _params())

    assert result.status is ToolStatus.SUCCESS, result.error
    assert result.data["idempotent_noop"] is True
    assert result.data["affected_rows"] == 0
