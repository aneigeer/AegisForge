"""结构化日志与敏感信息脱敏。"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

_DSN_PATTERN = re.compile(
    r"(?i)(mysql(?:\+pymysql)?://)([^:/@\s]+):([^@\s/]*)@"
)
_PASSWORD_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token)(['\"]?)(\s*[=:]\s*)(['\"]?)([^'\"\s,;}&\\]+)"
)

_STANDARD_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


def redact_text(text: str) -> str:
    """屏蔽 DSN 密码与常见凭据字段，供日志/错误输出使用。"""
    text = _DSN_PATTERN.sub(r"\1\2:***@", text)
    text = _PASSWORD_PATTERN.sub(r"\1\2\3\4***", text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(_redact_value(payload), ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    json_format: bool = True,
    stream: TextIO | None = None,
) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
