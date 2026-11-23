"""W10 T04 单测：token 统计（AC-16：算术可测 + 真值缺席不臆造）。

零 DB、零网络；不放宽断言、不加 skip/xfail。

覆盖：

- AC-16① 固定样本 + ``_FixedTokenizer``（``version="stub-v1"``）+ ``max_tokens=16``
  → P50 / P95 / ``truncation_ratio`` / ``assistant_loss_mask_tokens`` /
  ``packing_boundaries`` 逐值相等（写死期望值）；
- AC-16② ``tokenizer is None`` → ``tokenizer_version == "UNKNOWN"`` 且全部
  统计字段 ``None``（**不得填 0 / 0.0 / 估算值**）；``max_tokens is None`` →
  ``truncation_ratio is None``；
- 复用 ``core.verification.perf.percentile``（不重写分位数）；
- 出口复扫（可选 gate）对 ``BLOCKED`` / ``REVIEW_REQUIRED`` fail closed；
- 静态守护：只依赖 ``core*``、无第三方统计实现。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.tokens import (
    TokenStats,
    TokenizerPort,
    compute_token_stats,
)
from core.errors import BenchmarkInvalidError, ErrorCode, RedactionBlockedError
from core.redaction.models import RedactionChannel, RedactionStatus
from core.verification.perf import percentile

ROOT = Path(__file__).resolve().parents[2]
TOKENS_MODULE = ROOT / "core" / "benchmark" / "tokens.py"

# 4 条样本，encode 长度分别为 2 / 4 / 6 / 20（见 _FixedTokenizer）。
FIXED_SAMPLES: tuple[str, ...] = (
    "ab",
    "abcd",
    "abcdef",
    "abcdefghijklmnopqrst",
)
FIXED_LENGTHS: tuple[int, ...] = (2, 4, 6, 20)
MAX_TOKENS = 16


class _FixedTokenizer:
    """测试桩：固定 tokenizer（``version="stub-v1"``），长度 == 文本长度。"""

    version: str = "stub-v1"

    def encode(self, text: str) -> list[int]:
        return [ord(char) % 256 for char in text]


class _ConstantTokenizer:
    """测试桩：encode 恒返回 3 个 token，用于覆盖「等长且全部被截断」的分支。"""

    version: str = "constant-v1"

    def encode(self, text: str) -> list[int]:
        _ = text
        return [1, 2, 3]


class _StubResult:
    """测试桩：只暴露 ``status``。"""

    def __init__(self, status: RedactionStatus) -> None:
        self.status = status


class _StubGate:
    """测试桩：可控 ``for_document`` 判定。"""

    def __init__(self, status: RedactionStatus) -> None:
        self._status = status
        self.channels: list[RedactionChannel] = []

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], _StubResult]:
        self.channels.append(channel)
        return dict(payload), _StubResult(self._status)


class _RaisingGate:
    """测试桩：``BLOCKED`` 抛 W09 ``RedactionBlockedError``。"""

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], _StubResult]:
        raise RedactionBlockedError("结构性阻断", details={"channel": channel.value})


STATISTICAL_FIELDS: tuple[str, ...] = (
    "sample_count",
    "p50",
    "p95",
    "truncation_ratio",
    "assistant_loss_mask_tokens",
    "packing_boundaries",
    "tool_schema_coverage",
)


# --------------------------------------------------------------------------- #
# 协议 / 模型契约
# --------------------------------------------------------------------------- #


def test_tokenizer_port_is_runtime_checkable_and_stub_conforms() -> None:
    assert isinstance(_FixedTokenizer(), TokenizerPort)
    assert isinstance(_ConstantTokenizer(), TokenizerPort)
    assert _FixedTokenizer.version == "stub-v1"


def test_token_stats_is_closed_model() -> None:
    stats = compute_token_stats(FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=MAX_TOKENS)
    payload = stats.model_dump()
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        TokenStats.model_validate(payload)


def test_fixed_tokenizer_lengths_match_expectation() -> None:
    tokenizer = _FixedTokenizer()
    assert tuple(len(tokenizer.encode(text)) for text in FIXED_SAMPLES) == FIXED_LENGTHS


# --------------------------------------------------------------------------- #
# AC-16① 算术可测（逐值相等）
# --------------------------------------------------------------------------- #


def test_ac16_fixed_samples_stats_match_written_down_values() -> None:
    stats = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=MAX_TOKENS
    )
    assert stats.tokenizer_version == "stub-v1"
    assert stats.sample_count == 4
    assert stats.max_tokens == 16
    # P50 = percentile([2,4,6,20], 50) = 4.0；P95 = 20.0（排序索引法）。
    assert stats.p50 == 4.0
    assert stats.p95 == 20.0
    # 1 条长度 20 > 16 → 1/4 = 0.25。
    assert stats.truncation_ratio == 0.25
    # Σ min(len, 16) = 2 + 4 + 6 + 16 = 28。
    assert stats.assistant_loss_mask_tokens == 28
    # 贪心打包：2+4+6=12，再放 16 放不下 → 1 个边界。
    assert stats.packing_boundaries == 1
    # 无 tool schema 来源 → 不臆造覆盖率。
    assert stats.tool_schema_coverage is None
    # 与直接复用 percentile 的结果一致（禁止重写分位数）。
    assert stats.p50 == percentile(list(FIXED_LENGTHS), 50)
    assert stats.p95 == percentile(list(FIXED_LENGTHS), 95)


def test_ac16_stats_are_deterministic() -> None:
    first = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=MAX_TOKENS
    )
    second = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=MAX_TOKENS
    )
    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_ac16_version_is_taken_from_tokenizer_and_all_truncated_branch() -> None:
    # 4 条等长 3、max_tokens=2 → 全部被截断（实测值均为非 0，不触碰「0 冒充」歧义）。
    stats = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_ConstantTokenizer(), max_tokens=2
    )
    assert stats.tokenizer_version == "constant-v1"
    assert stats.sample_count == 4
    assert stats.p50 == 3.0
    assert stats.p95 == 3.0
    assert stats.truncation_ratio == 1.0  # 4/4 实测
    assert stats.assistant_loss_mask_tokens == 8  # Σ min(3, 2) = 8
    assert stats.packing_boundaries == 3  # 容量 2：每条(截断为2)都需另起新包
    assert stats.tool_schema_coverage is None


# --------------------------------------------------------------------------- #
# AC-16② 真值缺席不臆造
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("max_tokens", [None, 16])
def test_ac16_no_tokenizer_means_all_statistical_fields_are_none(
    max_tokens: int | None,
) -> None:
    stats = compute_token_stats(
        FIXED_SAMPLES, tokenizer=None, max_tokens=max_tokens
    )
    assert stats.tokenizer_version == "UNKNOWN"
    assert stats.max_tokens == max_tokens
    for field in STATISTICAL_FIELDS:
        value = getattr(stats, field)
        assert value is None, f"{field} 必须为 None，实际 {value!r}"
        # 明确排除「0 / 0.0 冒充真值缺席」：None 既不是 0 也不是 0.0。
        assert value != 0 and value != 0.0


def test_ac16_no_tokenizer_never_reports_fabricated_numbers() -> None:
    """逐字段扫描序列化结果：除 UNKNOWN 与 max_tokens 外没有任何数值。"""

    stats = compute_token_stats(FIXED_SAMPLES, tokenizer=None, max_tokens=None)
    payload = stats.model_dump(mode="json")
    numeric = {
        key: value
        for key, value in payload.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    assert numeric == {}, numeric
    assert payload["tokenizer_version"] == "UNKNOWN"


def test_ac16_max_tokens_none_means_no_truncation_ratio() -> None:
    stats = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=None
    )
    assert stats.tokenizer_version == "stub-v1"
    assert stats.sample_count == 4
    assert stats.p50 == 4.0
    assert stats.p95 == 20.0
    assert stats.max_tokens is None
    assert stats.truncation_ratio is None
    assert stats.assistant_loss_mask_tokens is None
    assert stats.packing_boundaries is None
    assert stats.tool_schema_coverage is None


def test_ac16_empty_samples_do_not_call_percentile_or_fabricate() -> None:
    stats = compute_token_stats([], tokenizer=_FixedTokenizer(), max_tokens=16)
    assert stats.tokenizer_version == "stub-v1"
    assert stats.sample_count == 0
    assert stats.p50 is None
    assert stats.p95 is None
    assert stats.truncation_ratio is None
    assert stats.assistant_loss_mask_tokens is None
    assert stats.packing_boundaries is None
    assert stats.tool_schema_coverage is None


def test_invalid_max_tokens_is_rejected() -> None:
    with pytest.raises(BenchmarkInvalidError) as caught:
        compute_token_stats(FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=0)
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID
    with pytest.raises(BenchmarkInvalidError):
        compute_token_stats(FIXED_SAMPLES, tokenizer=None, max_tokens=-1)


# --------------------------------------------------------------------------- #
# 出口复扫（AC-21 同向，可选 gate）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [RedactionStatus.BLOCKED, RedactionStatus.REVIEW_REQUIRED]
)
def test_exit_rescan_rejects_not_passable_statuses(status: RedactionStatus) -> None:
    gate = _StubGate(status)
    with pytest.raises(BenchmarkInvalidError) as caught:
        compute_token_stats(
            FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=16, gate=gate
        )
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID
    assert gate.channels and all(
        channel is RedactionChannel.TRAINING_SAMPLE for channel in gate.channels
    )


def test_exit_rescan_blocked_exception_is_fail_closed() -> None:
    with pytest.raises(BenchmarkInvalidError):
        compute_token_stats(
            FIXED_SAMPLES,
            tokenizer=_FixedTokenizer(),
            max_tokens=16,
            gate=_RaisingGate(),
        )


def test_exit_rescan_clean_status_is_passable() -> None:
    gate = _StubGate(RedactionStatus.CLEAN)
    stats = compute_token_stats(
        FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=16, gate=gate
    )
    assert stats.p50 == 4.0
    assert len(gate.channels) == len(FIXED_SAMPLES)


def test_token_stats_without_gate_is_pure_statistics() -> None:
    """``gate=None`` 表示出口脱敏由 clean_samples 负责，本函数只做纯统计。"""

    stats = compute_token_stats(FIXED_SAMPLES, tokenizer=_FixedTokenizer(), max_tokens=16)
    assert stats.truncation_ratio == 0.25


# --------------------------------------------------------------------------- #
# 静态守护：复用点 / 依赖方向 / 无第三方
# --------------------------------------------------------------------------- #


def test_tokens_module_reuses_percentile_and_adds_no_third_party() -> None:
    source = TOKENS_MODULE.read_text(encoding="utf-8")
    assert "from core.verification.perf import percentile" in source
    assert "def percentile(" not in source
    assert "def canonical_digest(" not in source
    banned = {"numpy", "scipy", "statistics", "apps", "workers"}
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & banned), modules & banned
    for forbidden in ("core.gateway", "core.approval", "core.agent"):
        assert f"from {forbidden}" not in source
    assert "from core.redaction.models import" in source
