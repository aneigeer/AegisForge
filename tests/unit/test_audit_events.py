"""审计事件单元测试：事件构造、关联 ID、脱敏与截断。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.audit.events import (
    MAX_AUDIT_ITEMS,
    MAX_AUDIT_STRING,
    SENSITIVE_KEYS,
    AuditEventType,
    make_event,
    redact_payload,
)
from core.logging import redact_text


def test_make_event_with_enum_event_type() -> None:
    event = make_event(
        AuditEventType.TASK_STATE_CHANGED,
        task_id="task_1",
        run_id="run_1",
        tool_call_id="call_1",
        payload={"status": "PENDING"},
    )

    assert event.event_id.startswith("evt_")
    assert event.task_id == "task_1"
    assert event.run_id == "run_1"
    assert event.tool_call_id == "call_1"
    assert event.event_type == "TASK_STATE_CHANGED"
    assert event.payload == {"status": "PENDING"}
    assert event.created_at.tzinfo is not None
    assert event.created_at.utcoffset() == timedelta(0)


def test_make_event_with_string_event_type_and_defaults() -> None:
    event = make_event("CUSTOM_EVENT", task_id="task_2", run_id="run_2")

    assert event.event_id.startswith("evt_")
    assert event.task_id == "task_2"
    assert event.run_id == "run_2"
    assert event.tool_call_id is None
    assert event.event_type == "CUSTOM_EVENT"
    assert event.payload == {}
    assert event.created_at.tzinfo is not None


def test_make_event_generates_unique_event_ids() -> None:
    first = make_event(AuditEventType.TASK_CREATED, task_id="t", run_id="r")
    second = make_event(AuditEventType.TASK_CREATED, task_id="t", run_id="r")

    assert first.event_id != second.event_id
    assert first.event_id.startswith("evt_")
    assert second.event_id.startswith("evt_")


@pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert redact_payload({key: "s3cr3t-value"}) == {key: "<redacted>"}


@pytest.mark.parametrize(
    "key",
    ["Password", "PASSWORD", "TOKEN", "Api_Key", "DSN", "Authorization"],
)
def test_sensitive_key_matching_is_case_insensitive(key: str) -> None:
    result = redact_payload({key: "s3cr3t-value"})
    assert result == {key: "<redacted>"}


def test_nested_dict_and_list_are_recursively_redacted() -> None:
    payload = {
        "outer": {
            "Password": "p@ss",
            "nested": [{"TOKEN": "abc"}, {"keep": "visible"}],
        },
        "safe": "hello",
    }

    result = redact_payload(payload)

    assert result["outer"]["Password"] == "<redacted>"
    assert result["outer"]["nested"][0]["TOKEN"] == "<redacted>"
    assert result["outer"]["nested"][1]["keep"] == "visible"
    assert result["safe"] == "hello"


def test_scalar_values_are_preserved() -> None:
    payload = {"count": 42, "ratio": 1.5, "enabled": True, "missing": None}

    assert redact_payload(payload) == payload


def test_long_string_is_truncated() -> None:
    text = "a" * (MAX_AUDIT_STRING + 500)
    result = redact_payload(text)

    assert result[:MAX_AUDIT_STRING] == "a" * MAX_AUDIT_STRING
    assert result.endswith("...<truncated>")
    assert "<truncated>" in result
    assert len(result) == MAX_AUDIT_STRING + len("...<truncated>")


def test_long_list_is_truncated() -> None:
    payload = list(range(MAX_AUDIT_ITEMS + 10))
    result = redact_payload(payload)

    assert len(result) == MAX_AUDIT_ITEMS + 1
    assert result[:MAX_AUDIT_ITEMS] == payload[:MAX_AUDIT_ITEMS]
    assert result[-1] == "<truncated 10 items>"
    assert "items>" in result[-1]


def test_long_dict_is_truncated() -> None:
    payload = {f"key_{index}": index for index in range(MAX_AUDIT_ITEMS + 10)}
    result = redact_payload(payload)

    assert len(result) == MAX_AUDIT_ITEMS + 1
    assert result["<truncated>"] == "10 more keys"
    assert set(result) - {"<truncated>"} == {
        f"key_{index}" for index in range(MAX_AUDIT_ITEMS)
    }


def test_dsn_password_in_string_is_redacted() -> None:
    text = "connect mysql://dg_ro:topsecret@127.0.0.1:3306/dg_lab failed"

    assert redact_text(text) == (
        "connect mysql://dg_ro:***@127.0.0.1:3306/dg_lab failed"
    )
    result = redact_payload({"message": text})
    assert "topsecret" not in result["message"]
    assert "dg_ro:***@" in result["message"]


def test_make_event_redacts_payload_automatically() -> None:
    event = make_event(
        AuditEventType.TOOL_CALL_STARTED,
        task_id="task_3",
        run_id="run_3",
        tool_call_id="call_3",
        payload={
            "password": "hunter2",
            "nested": {"TOKEN": "abc", "ok": 1},
        },
    )

    assert event.payload["password"] == "<redacted>"
    assert event.payload["nested"]["TOKEN"] == "<redacted>"
    assert event.payload["nested"]["ok"] == 1
    assert "hunter2" not in str(event.payload)
