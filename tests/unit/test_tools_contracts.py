from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.errors import ToolNotFoundError
from core.tools.contracts import ToolContext, ToolResult, ToolStatus
from database.observers import build_observer_registry
from database.observers.base import ObserverTool


class _EmptyInput(BaseModel):
    pass


class _FakeClient:
    database_id = "lab_mysql_01"

    def identity(self):
        raise RuntimeError("no db")


def test_registry_has_exactly_the_ten_observer_tools() -> None:
    registry = build_observer_registry()
    assert len(registry) == 10
    assert registry.names() == [
        "explain_query",
        "get_activity",
        "get_blocking_tree",
        "get_database_size",
        "get_db_settings",
        "get_db_version",
        "get_index_stats",
        "get_locks",
        "get_query_stats",
        "get_table_stats",
    ]
    definitions = registry.definitions()
    assert all(definition.risk_level == "R0" for definition in definitions)
    assert all(definition.side_effect == "read" for definition in definitions)
    assert all(definition.input_schema["type"] == "object" for definition in definitions)


def test_registry_rejects_duplicate_and_unknown_names() -> None:
    registry = build_observer_registry()
    with pytest.raises(ValueError):
        registry.register(registry.get("get_db_version"))
    with pytest.raises(ToolNotFoundError) as excinfo:
        registry.get("drop_database")
    assert "drop_database" in excinfo.value.details["tool_name"]


def test_tool_input_validation_returns_structured_error() -> None:
    registry = build_observer_registry()
    tool = registry.get("get_db_version")
    result: ToolResult = tool.run(  # type: ignore[attr-defined]
        ToolContext(client=_FakeClient()),  # type: ignore[arg-type]
        {"unexpected": True},
    )
    assert result.status == ToolStatus.ERROR
    assert result.error is not None
    assert result.error.code == "REQUEST_INVALID"
    assert result.source.engine == "mysql"
    assert result.source.db_version == "unknown"


def test_tool_result_serializes_contract_fields() -> None:
    from core.tools.contracts import ToolSource

    result = ToolResult(
        tool_name="x",
        status=ToolStatus.SUCCESS,
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=1.5,
        data={"a": 1},
        warnings=[],
        source=ToolSource(database_id="lab", db_version="8.0.46", collected_at="2026-01-01T00:00:00+00:00"),
    )
    payload = result.model_dump()
    assert payload["status"] == "success"
    assert payload["source"]["engine"] == "mysql"


def test_observer_base_class_requires_definition_fields() -> None:
    class _BadTool(ObserverTool):
        name = "bad_tool"
        description = "x"
        input_model = _EmptyInput
        output_schema = {"type": "object"}

        def _execute(self, context, params):
            return {}, []

    tool = _BadTool()
    assert tool.definition.name == "bad_tool"
    assert tool.definition.timeout_ms == 30_000
