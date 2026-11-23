"""P4-T3：最小 span 记录器（属性白名单 + 脱敏，不依赖 OTel SDK）。

本环境只有 `opentelemetry-api`、没有 SDK/Exporter，直接声称"已接入 OTLP"是假的。
因此这里的定位是：**把 span 生成到可导出的内存缓冲**，并强制属性白名单与脱敏；
真正导出到 collector 需要安装 SDK 后由 `export_otlp()` 接管（当前明确 NOT_VERIFIED）。

红线：span 属性只允许白名单键；字符串值一律过 `redact_text` 并截断，
因此 SQL 字面量、DSN、BYOK key 即使被误传也不会落进 span。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from core.telemetry.redact import scrub

#: span 属性白名单：只能出现这些键（组件、动作、结果、耗时、规模）。
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "component",
        "operation",
        "tool",
        "tool_version",
        "role",
        "model",
        "provider",
        "status",
        "risk",
        "deny_code",
        "task_id",
        "run_id",
        "step_index",
        "rows",
        "tokens",
        "duration_ms",
        "retry_count",
        "protocol_tier",
    }
)
MAX_ATTRIBUTE_CHARS = 96
#: 内存缓冲上限（避免长时间运行时无界增长）。
MAX_SPANS = 2048


def _clean_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES:
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            clean[key] = value
        else:
            clean[key] = scrub(value)[:MAX_ATTRIBUTE_CHARS]
    return clean


@dataclass
class Span:
    name: str
    started_at: float
    attributes: dict[str, Any] = field(default_factory=dict)
    ended_at: float | None = None
    status: str = "OK"

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return round((end - self.started_at) * 1000, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": dict(self.attributes),
        }


class SpanRecorder:
    """进程内 span 缓冲；线程安全，容量有界。"""

    def __init__(self, max_spans: int = MAX_SPANS) -> None:
        self._lock = threading.Lock()
        self._spans: deque[Span] = deque(maxlen=max_spans)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(name=name, started_at=time.perf_counter(), attributes=_clean_attributes(attributes))
        try:
            yield span
        except Exception:
            span.status = "ERROR"
            raise
        finally:
            span.ended_at = time.perf_counter()
            with self._lock:
                self._spans.append(span)

    def record(self, span: Span) -> None:
        with self._lock:
            self._spans.append(span)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [span.as_dict() for span in self._spans]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


#: 进程级单例。
TRACER = SpanRecorder()


def export_otlp(*_args: Any, **_kwargs: Any) -> None:
    """显式占位：未安装 OTel SDK，不假装已导出。"""
    raise RuntimeError(
        "OTLP 导出未启用：当前环境缺少 opentelemetry-sdk（ENV-OTEL），"
        "已记录的 span 仅供进程内检查"
    )


__all__ = ["ALLOWED_ATTRIBUTES", "MAX_SPANS", "Span", "SpanRecorder", "TRACER", "export_otlp"]
