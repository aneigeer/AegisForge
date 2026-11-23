"""P4-T3 单元测试：遥测出口不含 SQL 字面量 / DSN / BYOK key。

白盒假设：调用方**仍然可能**把脏值塞进标签或 span 属性（误用），因此实现里必须
有结构性清洗；本测试刻意用最脏的输入验证出口干净。
"""

from __future__ import annotations

import pytest

from core.telemetry.metrics import METRICS, MetricsRegistry
from core.telemetry.tracing import TRACER, SpanRecorder, export_otlp


def _fresh() -> MetricsRegistry:
    registry = MetricsRegistry()
    registry.declare(
        "t_calls_total", kind="counter", help_text="test", labels=("tool", "status")
    )
    registry.declare("t_latency_seconds", kind="histogram", labels=("tool",))
    registry.declare("t_gauge", kind="gauge", labels=("role",))
    return registry


def test_unknown_labels_are_dropped_not_written() -> None:
    registry = _fresh()
    registry.inc("t_calls_total", {"tool": "x", "status": "ok", "sql": "SELECT 1"})
    text = registry.render()
    assert 'tool="x"' in text
    assert "SELECT 1" not in text
    assert "sql=" not in text


def test_sql_dsn_and_keys_are_scrubbed_from_label_values() -> None:
    registry = _fresh()
    registry.inc(
        "t_calls_total",
        {"tool": "SELECT * FROM users WHERE phone=13800138000", "status": "ok"},
    )
    registry.set(
        "t_gauge",
        "mysql://dg_ro:supersecret@control_db:3306/dg_lab",  # type: ignore[arg-type]
    )
    registry.set("t_gauge", 1.0, {"role": "sk-live-abcdef123456"})
    text = registry.render()
    assert "<sql>" in text
    assert "13800138000" not in text
    assert "supersecret" not in text
    assert "<key>" in text


def test_non_numeric_values_are_ignored() -> None:
    registry = _fresh()
    registry.set("t_gauge", "not-a-number", {"role": "planner"})  # type: ignore[arg-type]
    registry.observe("t_latency_seconds", "0.3", {"tool": "x"})  # type: ignore[arg-type]
    text = registry.render()
    assert "t_gauge{" not in text, "非数字不得写入 gauge"
    assert "_count{" not in text, "非数字不得写入 histogram"


def test_histogram_buckets_are_cumulative() -> None:
    registry = _fresh()
    for value in (0.004, 0.02, 0.3):
        registry.observe("t_latency_seconds", value, {"tool": "t"})
    lines = [line for line in registry.render().splitlines() if 'tool="t"' in line]
    buckets = {line.rsplit("le=", 1)[1].split("}")[0].strip('"'): float(line.rsplit(" ", 1)[1])
               for line in lines if "_bucket" in line}
    assert buckets["0.005"] == 1
    assert buckets["0.025"] == 2
    assert buckets["0.5"] == 3
    assert buckets["+Inf"] == 3


def test_span_attributes_are_allowlisted_and_scrubbed() -> None:
    recorder = SpanRecorder()
    with recorder.span(
        "tool.execute",
        tool="get_query_stats",
        provider="sk-live-abcdef123456",
        sql="SELECT * FROM orders WHERE id=2001",
        duration_ms=12.5,
    ):
        pass
    payload = recorder.snapshot()[0]
    assert payload["attributes"]["tool"] == "get_query_stats"
    assert payload["attributes"]["provider"] == "<key>"
    assert "sql" not in payload["attributes"], "未声明的属性必须丢弃"
    assert payload["attributes"]["duration_ms"] == 12.5


def test_span_records_error_status() -> None:
    recorder = SpanRecorder()
    with pytest.raises(RuntimeError):
        with recorder.span("tool.execute", tool="x"):
            raise RuntimeError("boom")
    assert recorder.snapshot()[0]["status"] == "ERROR"


def test_otlp_export_is_explicitly_unavailable() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        export_otlp()
    assert "ENV-OTEL" in str(excinfo.value)


def test_process_singleton_metrics_have_declared_shape() -> None:
    text = METRICS.render()
    for name in (
        "dg_tool_calls_total",
        "dg_model_calls_total",
        "dg_model_tokens_total",
        "dg_gateway_denials_total",
        "dg_tool_latency_seconds",
        "dg_model_latency_seconds",
    ):
        assert f"# TYPE {name}" in text


def test_tracer_singleton_is_separate_from_test_recorder() -> None:
    # 单例存在且可清空，测试之间不互相污染。
    TRACER.clear()
    with TRACER.span("api.request", component="api", operation="/metrics"):
        pass
    assert TRACER.snapshot()[-1]["attributes"]["component"] == "api"
    TRACER.clear()
    assert TRACER.snapshot() == []
