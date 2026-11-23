"""P4-T3：最小可用的 Prometheus 指标（零外部依赖，标签白名单）。

为什么自己写而不是引入 `prometheus_client`：本环境的依赖清单里没有它，为一个文本
exposition 端点新增运行期依赖不划算。实现严格限定在 Prometheus 文本格式的最小子集
（counter / gauge / histogram 的 `_bucket/_sum/_count`），足以被 Prometheus 抓取。

红线（对应 P4-T3 验收）：

- **标签值白名单 + 脱敏**：标签只允许事先声明的键；值一律过 `redact_text` 并截断，
  因此 SQL 字面量、DSN、BYOK key 即使被误传进标签也会被抹掉；
- **数值只接受数字**：拒绝把任意对象 stringify 成指标值（防止把 SQL 塞进 value）；
- **指标名不可动态拼接**：只有 `define_*` 注册过的名字能写入。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from core.telemetry.redact import scrub

#: 标签值最大长度（防止把整条 SQL 当标签）。
MAX_LABEL_VALUE = 64
#: histogram 默认桶（秒）；覆盖 5ms ~ 10s。
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _sanitize(value: Any) -> str:
    return scrub(value)[:MAX_LABEL_VALUE]


@dataclass
class _Series:
    labels: dict[str, str]
    value: float = 0.0
    #: 每个桶的**累计**计数（长度 = len(DEFAULT_BUCKETS) + 1，末位是 +Inf）。
    buckets: list[int] = field(default_factory=lambda: [0] * (len(DEFAULT_BUCKETS) + 1))
    total: float = 0.0
    count: int = 0


class MetricsRegistry:
    """线程安全的最小指标注册表；只允许预声明的指标与标签。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], _Series]] = {}
        self._gauges: dict[str, dict[tuple[tuple[str, str], ...], _Series]] = {}
        self._histograms: dict[str, dict[tuple[tuple[str, str], ...], _Series]] = {}
        self._label_sets: dict[str, frozenset[str]] = {}
        self._help: dict[str, str] = {}
        self._types: dict[str, str] = {}

    # ---- 声明 ------------------------------------------------------------
    def declare(
        self,
        name: str,
        *,
        kind: str,
        help_text: str = "",
        labels: tuple[str, ...] = (),
    ) -> None:
        if kind not in {"counter", "gauge", "histogram"}:
            raise ValueError(f"未知指标类型：{kind}")
        with self._lock:
            self._label_sets[name] = frozenset(labels)
            self._types[name] = kind
            self._help.setdefault(name, help_text)
            target = {
                "counter": self._counters,
                "gauge": self._gauges,
                "histogram": self._histograms,
            }[kind]
            target.setdefault(name, {})

    # ---- 写入 ------------------------------------------------------------
    def inc(self, name: str, labels: dict[str, Any] | None = None, value: float = 1.0) -> None:
        self._write(self._counters, name, labels, value, additive=True)

    def set(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        self._write(self._gauges, name, labels, value, additive=False)

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            # 非数字一律丢弃：防止把任意对象 stringify 成观测值。
            return
        series = self._series(self._histograms, name, labels)
        if series is None:
            return
        with self._lock:
            amount = float(value)
            series.total += amount
            series.count += 1
            # 累计语义：命中某桶后，其**后续**桶（含 +Inf）都要 +1，
            # 这样 `le` 才是"小于等于"的单调累计值。
            for index, bound in enumerate(DEFAULT_BUCKETS):
                if amount <= bound:
                    for position in range(index, len(series.buckets)):
                        series.buckets[position] += 1
                    break
            else:
                series.buckets[-1] += 1

    def _series(
        self,
        target: dict[str, dict[tuple[tuple[str, str], ...], _Series]],
        name: str,
        labels: dict[str, Any] | None,
    ) -> _Series | None:
        allowed = self._label_sets.get(name)
        if allowed is None or name not in target:
            # 未声明（或类型不匹配）的指标：静默丢弃，不做动态创建。
            return None
        provided = dict(labels or {})
        unknown = set(provided) - set(allowed)
        clean = {key: _sanitize(provided.get(key)) for key in allowed}
        if unknown:
            # 未知标签不写入（也不抛错：观测不该影响主流程），但也不静默并入。
            clean = {key: value for key, value in clean.items() if key in allowed}
        key = tuple(sorted(clean.items()))
        with self._lock:
            series = target[name].get(key)
            if series is None:
                series = _Series(labels=clean)
                target[name][key] = series
            return series

    def _write(
        self,
        target: dict[str, dict[tuple[tuple[str, str], ...], _Series]],
        name: str,
        labels: dict[str, Any] | None,
        value: float,
        *,
        additive: bool,
    ) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        if math.isnan(float(value)):
            return
        series = self._series(target, name, labels)
        if series is None:
            return
        with self._lock:
            series.value = series.value + float(value) if additive else float(value)

    # ---- 导出 ------------------------------------------------------------
    @staticmethod
    def _fmt_labels(labels: dict[str, str], extra: tuple[tuple[str, str], ...] = ()) -> str:
        pairs = list(labels.items()) + list(extra)
        if not pairs:
            return ""
        body = ",".join(f'{key}="{value}"' for key, value in pairs)
        return "{" + body + "}"

    def render(self) -> str:
        """Prometheus 文本格式（# HELP / # TYPE + 样本行）。"""
        lines: list[str] = []
        with self._lock:
            for name, series_map in self._counters.items():
                lines.append(f"# HELP {name} {self._help.get(name, '')}".rstrip())
                lines.append(f"# TYPE {name} counter")
                for series in series_map.values():
                    lines.append(f"{name}{self._fmt_labels(series.labels)} {series.value}")
            for name, series_map in self._gauges.items():
                lines.append(f"# HELP {name} {self._help.get(name, '')}".rstrip())
                lines.append(f"# TYPE {name} gauge")
                for series in series_map.values():
                    lines.append(f"{name}{self._fmt_labels(series.labels)} {series.value}")
            for name, series_map in self._histograms.items():
                lines.append(f"# HELP {name} {self._help.get(name, '')}".rstrip())
                lines.append(f"# TYPE {name} histogram")
                for series in series_map.values():
                    for index, bucket in enumerate(DEFAULT_BUCKETS):
                        lines.append(
                            f"{name}_bucket{self._fmt_labels(series.labels, (('le', str(bucket)),))} {series.buckets[index]}"
                        )
                    lines.append(
                        f"{name}_bucket{self._fmt_labels(series.labels, (('le', '+Inf'),))} {series.buckets[-1]}"
                    )
                    lines.append(f"{name}_sum{self._fmt_labels(series.labels)} {series.total}")
                    lines.append(f"{name}_count{self._fmt_labels(series.labels)} {series.count}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            for collection in (self._counters, self._gauges, self._histograms):
                for series_map in collection.values():
                    series_map.clear()


#: 进程级单例（FastAPI 与 Worker 共用一个注册表即可，标签里区分组件）。
METRICS = MetricsRegistry()

# ---- 预声明：名字与标签集合都是封闭的 -------------------------------------
METRICS.declare(
    "dg_tool_calls_total",
    kind="counter",
    help_text="工具调用总数（按工具/状态/风险等级）",
    labels=("tool", "status", "risk"),
)
METRICS.declare(
    "dg_tool_latency_seconds",
    kind="histogram",
    help_text="工具调用耗时（ToolGateway 观测）",
    labels=("tool",),
)
METRICS.declare(
    "dg_model_calls_total",
    kind="counter",
    help_text="模型调用总数（按角色/结果）",
    labels=("role", "outcome"),
)
METRICS.declare(
    "dg_model_latency_seconds",
    kind="histogram",
    help_text="模型调用耗时（ModelGateway 观测）",
    labels=("role",),
)
METRICS.declare(
    "dg_model_tokens_total",
    kind="counter",
    help_text="模型 token 用量（端点回报的真实值）",
    labels=("role",),
)
METRICS.declare(
    "dg_gateway_denials_total",
    kind="counter",
    help_text="网关拒绝次数（按拒绝码）",
    labels=("reason",),
)


__all__ = ["DEFAULT_BUCKETS", "METRICS", "MetricsRegistry"]
