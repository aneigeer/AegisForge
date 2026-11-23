"""W10 T04：token 统计（AC-16：算术可测 + 真值缺席不臆造）。

口径来源：``docs/w10/02-design.md`` §8 / §4.6；任务卡 T04。

两条硬约束：

1. **tokenizer 缺席 → 全部统计字段 ``None``**：``tokenizer_version="UNKNOWN"``，
   ``p50`` / ``p95`` / ``truncation_ratio`` / ``assistant_loss_mask_tokens`` /
   ``packing_boundaries`` / ``tool_schema_coverage`` / ``sample_count`` 全 ``None``；
   **绝不填 0 / 0.0 / 估算值**（设计 §8、全局纪律 8）。
2. ``max_tokens is None``（现状 ``inference_config.json`` 的
   ``runtime.max_model_len=null``）→ ``truncation_ratio is None``。

P50/P95 **复用** ``core.verification.perf.percentile``（禁止重写分位数）。
``tool_schema_coverage`` 无来源（本函数只收到样本文本，没有 tool schema
清单）→ 恒 ``None``，不臆造。

统计量定义（全部为对真实 ``encode()`` 结果的确定性计数，非主观打分）：

- ``p50`` / ``p95``：``percentile(lengths, 50/95)``（排序索引法）；
- ``truncation_ratio``：``|{len(tokens) > max_tokens}| / sample_count``；
- ``assistant_loss_mask_tokens``：``Σ min(len_i, max_tokens)`` —— 离线样本集
  无 prompt/assistant 切分，整条样本视为 assistant 段，截断窗口外的 token
  不计 loss；``max_tokens is None`` 时不可判定 → ``None``；
- ``packing_boundaries``：贪心打包（顺序填充容量 ``max_tokens`` 的包）时
  「因放不下而另起新包」的次数；``max_tokens is None`` 时 → ``None``；
- ``tool_schema_coverage``：无 tool schema 来源 → ``None``。

依赖方向：只依赖 ``core*``；不 import ``apps/**``、``workers/**``、
``core/gateway/**``、``core/approval/**``、``core/agent/**``。
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict

from core.errors import BenchmarkInvalidError, RedactionBlockedError
from core.redaction.models import RedactionChannel, RedactionStatus
from core.verification.perf import percentile


@runtime_checkable
class TokenizerPort(Protocol):
    """冻结 manifest 中的目标 tokenizer；实现只能来自 ``tokenizer_commit``。"""

    version: str

    def encode(self, text: str) -> list[int]: ...


class TokenStats(BaseModel):
    """token 统计结果；``extra="forbid"``，未测得一律 ``None``。"""

    model_config = ConfigDict(extra="forbid")

    tokenizer_version: str
    sample_count: int | None
    p50: float | None
    p95: float | None
    max_tokens: int | None
    truncation_ratio: float | None
    assistant_loss_mask_tokens: int | None
    packing_boundaries: int | None
    tool_schema_coverage: float | None


def _packing_boundaries(lengths: Sequence[int], capacity: int) -> int:
    """贪心打包：返回「放不下而另起新包」的边界数。

    逐样本顺序填充 ``capacity`` 的包；样本长度先按 ``max_tokens`` 截断
    （截断窗口外的 token 不进入任何包）。当前包放不下时边界 +1 并起新包。
    """

    boundaries = 0
    current = 0
    for length in lengths:
        effective = min(length, capacity)
        if current == 0:
            current = effective
        elif current + effective <= capacity:
            current += effective
        else:
            boundaries += 1
            current = effective
    return boundaries


def _exit_rescan(samples: Sequence[str], gate: Any) -> None:
    """可选的出口复扫：复用 W09 ``for_document`` / ``TRAINING_SAMPLE`` 口径。

    ``gate=None`` 表示本函数只做纯统计（出口脱敏由 ``clean_samples`` 负责）。
    显式传入 gate 时对每条样本文本复扫，``BLOCKED`` / ``REVIEW_REQUIRED``
    **一律 fail closed**，不静默通过（AC-21 同向）。
    """

    for text in samples:
        try:
            document, result = gate.for_document(
                {"text": str(text)},
                channel=RedactionChannel.TRAINING_SAMPLE,
                task_id="w10-token-stats",
            )
        except RedactionBlockedError as exc:
            raise BenchmarkInvalidError(
                "token 统计出口复扫阻断（BLOCKED）",
                details={"reason": "REDACTION_BLOCKED"},
            ) from exc
        _ = document
        if result.status in (
            RedactionStatus.BLOCKED,
            RedactionStatus.REVIEW_REQUIRED,
        ):
            raise BenchmarkInvalidError(
                "token 统计出口复扫未放行，拒绝入集",
                details={"status": str(getattr(result.status, "value", result.status))},
            )


def compute_token_stats(
    samples: Sequence[str],
    *,
    tokenizer: TokenizerPort | None,
    max_tokens: int | None,
    gate: Any | None = None,
) -> TokenStats:
    """计算 token 统计；tokenizer 缺席时所有统计字段恒 ``None``（AC-16②）。

    Args:
        samples: 待统计的样本文本（通常来自 ``clean_samples`` 的出口）。
        tokenizer: 目标 tokenizer；``None`` 表示未冻结（``tokenizer_commit=null``）。
        max_tokens: 模型上下文上限；``None`` 表示现状未配置（``max_model_len=null``）。
        gate: 可选 W09 脱敏闸门；提供时额外做 ``TRAINING_SAMPLE`` 出口复扫。

    Returns:
        ``TokenStats``；真值缺席时不臆造任何数值。

    Raises:
        BenchmarkInvalidError: ``max_tokens`` 非法，或出口复扫未放行。
    """

    if max_tokens is not None and max_tokens < 1:
        raise BenchmarkInvalidError(
            "max_tokens 必须 >= 1 或 None", details={"max_tokens": max_tokens}
        )

    # AC-16②：tokenizer 未冻结 → 一个 token 数值都不填（含不得填 0）。
    if tokenizer is None:
        return TokenStats(
            tokenizer_version="UNKNOWN",
            sample_count=None,
            p50=None,
            p95=None,
            max_tokens=max_tokens,
            truncation_ratio=None,
            assistant_loss_mask_tokens=None,
            packing_boundaries=None,
            tool_schema_coverage=None,
        )

    texts = [str(item) for item in samples]
    lengths = [len(tokenizer.encode(text)) for text in texts]
    sample_count = len(lengths)

    if sample_count == 0:
        # 空样本：不调用 percentile（避免 REQUEST_INVALID），统计全部 None。
        return TokenStats(
            tokenizer_version=str(tokenizer.version),
            sample_count=0,
            p50=None,
            p95=None,
            max_tokens=max_tokens,
            truncation_ratio=None,
            assistant_loss_mask_tokens=None,
            packing_boundaries=None,
            tool_schema_coverage=None,
        )

    if gate is not None:
        _exit_rescan(texts, gate)

    # 唯一分位数入口：复用 W08 perf.py（禁止重写）。
    p50 = percentile(lengths, 50)
    p95 = percentile(lengths, 95)

    if max_tokens is None:
        truncation_ratio: float | None = None
        assistant_loss_mask_tokens: int | None = None
        packing_boundaries: int | None = None
    else:
        truncated = sum(1 for length in lengths if length > max_tokens)
        truncation_ratio = truncated / sample_count
        assistant_loss_mask_tokens = sum(
            min(length, max_tokens) for length in lengths
        )
        packing_boundaries = _packing_boundaries(lengths, max_tokens)

    return TokenStats(
        tokenizer_version=str(tokenizer.version),
        sample_count=sample_count,
        p50=p50,
        p95=p95,
        max_tokens=max_tokens,
        truncation_ratio=truncation_ratio,
        assistant_loss_mask_tokens=assistant_loss_mask_tokens,
        packing_boundaries=packing_boundaries,
        # 无 tool schema 来源 → 恒 None，不臆造覆盖率。
        tool_schema_coverage=None,
    )


__all__ = ["TokenStats", "TokenizerPort", "compute_token_stats"]
