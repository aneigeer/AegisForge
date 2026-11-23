from __future__ import annotations

import io
import json
import logging

from core.errors import (
    ConfigError,
    DatabasePermissionError,
    DatabaseUnavailableError,
    ErrorCode,
)
from core.logging import JsonFormatter, configure_logging, redact_text


def test_redact_dsn_password() -> None:
    text = "connect mysql://dg_ro:topsecret@127.0.0.1:3306/dg_lab failed"
    assert "topsecret" not in redact_text(text)
    assert "dg_ro:***@" in redact_text(text)


def test_redact_password_fields() -> None:
    for text in (
        "password=abc123",
        "password: 'abc123'",
        '{"password": "abc123", "user": "dg_ro"}',
        "DG_RW_PASSWORD=abc123",
    ):
        redacted = redact_text(text)
        assert "abc123" not in redacted
        assert "***" in redacted


def test_json_logging_is_structured_and_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", json_format=True, stream=stream)
    logging.getLogger("test").info(
        "connect using mysql://dg_ro:topsecret@127.0.0.1:3306/dg_lab",
        extra={"task_id": "task_1", "duration_ms": 12},
    )
    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "INFO"
    assert record["task_id"] == "task_1"
    assert record["duration_ms"] == 12
    assert "topsecret" not in record["message"]
    assert record["message"].find("mysql://dg_ro:***@") >= 0


def test_json_formatter_redacts_exception_message() -> None:
    try:
        raise RuntimeError("password=abc123")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="boom", args=(), exc_info=exc_info,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "abc123" not in payload["exception"]


def test_error_serialization_and_status_codes() -> None:
    permission = DatabasePermissionError("拒绝写入", details={"database_id": "lab"})
    assert permission.to_dict()["code"] == ErrorCode.DB_PERMISSION_DENIED.value
    assert permission.status_code == 403

    unavailable = DatabaseUnavailableError("无法连接", details={"database_id": "lab"})
    assert unavailable.status_code == 503

    config = ConfigError("缺少配置", details={"fields": ["ro_dsn"]})
    assert config.to_dict()["details"]["fields"] == ["ro_dsn"]
