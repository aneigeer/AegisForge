"""P1-BYOK-T2：租户级调用/token 配额（fail-closed，INV-06 不估算）。

口径：

- **调用数**：每次真正发起模型调用前 +1；超过上限立即 `QUOTA_EXCEEDED`，不发请求；
- **token**：只在端点回报 usage 后累加（不估算、不预扣）；累加后若已超额，
  **本次调用仍然成功返回**，但把配额标记为耗尽，下一次调用前置检查直接拒绝；
- 触顶时调用注入的 `on_exceeded`（组合根接审计），审计失败不改变判定。

配额对象**有状态**、线程安全（`Lock`）；每个租户一个实例，由工厂按需构造。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from core.errors import QuotaExceededError


@dataclass
class QuotaState:
    calls: int = 0
    tokens: int = 0
    exhausted: bool = False
    last_reason: str | None = None


@dataclass
class TenantQuota:
    """租户配额守卫；`max_calls`/`max_tokens` 为 ``None`` 表示该维度不限。"""

    tenant_id: str
    max_calls: int | None = None
    max_tokens: int | None = None
    on_exceeded: Callable[[str, QuotaState], None] | None = None
    _state: QuotaState = field(default_factory=QuotaState)
    # RLock：`_reject`/`_notify` 会在持锁路径里取快照（`state`），普通 Lock 会自死锁。
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        for name, value in (("max_calls", self.max_calls), ("max_tokens", self.max_tokens)):
            if value is not None and int(value) < 0:
                raise ValueError(f"{name} 不得为负")

    @property
    def state(self) -> QuotaState:
        with self._lock:
            return QuotaState(
                calls=self._state.calls,
                tokens=self._state.tokens,
                exhausted=self._state.exhausted,
                last_reason=self._state.last_reason,
            )

    def before_call(self) -> None:
        """调用前置检查：已耗尽或调用数将超限则 fail closed。"""
        with self._lock:
            if self._state.exhausted:
                self._reject("QUOTA_EXHAUSTED")
            if self.max_calls is not None and self._state.calls + 1 > self.max_calls:
                self._state.exhausted = True
                self._reject("CALL_BUDGET_EXCEEDED")
            self._state.calls += 1

    def record_tokens(self, tokens: int) -> None:
        """调用后累加实测 token；超额只标记耗尽，不回滚已完成的调用。"""
        amount = max(int(tokens), 0)
        with self._lock:
            self._state.tokens += amount
            if self.max_tokens is not None and self._state.tokens > self.max_tokens:
                self._state.exhausted = True
                self._state.last_reason = "TOKEN_BUDGET_EXCEEDED"
                self._notify("TOKEN_BUDGET_EXCEEDED")

    def snapshot(self) -> dict[str, object]:
        state = self.state
        return {
            "tenant_id": self.tenant_id,
            "calls": state.calls,
            "tokens": state.tokens,
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "exhausted": state.exhausted,
            "last_reason": state.last_reason,
        }

    # ---- internals -------------------------------------------------------
    def _reject(self, reason: str) -> None:
        self._state.last_reason = reason
        self._notify(reason)
        raise QuotaExceededError(
            "租户模型配额已耗尽，已按 fail-closed 中止",
            details={
                "tenant_id": self.tenant_id,
                "reason": reason,
                "calls": self._state.calls,
                "tokens": self._state.tokens,
                "max_calls": self.max_calls,
                "max_tokens": self.max_tokens,
            },
        )

    def _notify(self, reason: str) -> None:
        if self.on_exceeded is None:
            return
        try:
            self.on_exceeded(reason, self.state)
        except Exception:  # noqa: BLE001 - 审计失败不改变配额判定
            return


__all__ = ["QuotaState", "TenantQuota"]
