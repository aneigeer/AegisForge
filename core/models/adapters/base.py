"""模型适配器抽象基类：统一 model_version、延迟与响应构造。

适配器只负责“把一次 ModelRequest 变成 ModelResponse”，不做权限判定、
不接触数据库凭据，也不对 Token 用量做估算（缺失即保持 0）。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from core.models.contracts import ModelRequest, ModelResponse, ModelUsage


class ModelAdapter(ABC):
    """所有模型适配器的基类；Gateway 依赖这些属性与 ``complete``。"""

    provider: str = "unknown"
    adapter_version: str = "0.1.0"
    model_name: str = "unknown"

    @property
    def model_version(self) -> str:
        return f"{self.provider}/{self.model_name}/{self.adapter_version}"

    @abstractmethod
    def complete(self, request: ModelRequest) -> ModelResponse:
        """执行一次模型调用，返回标准化响应。"""

    def make_response(
        self,
        text: str,
        request: ModelRequest,
        *,
        usage: ModelUsage | None = None,
        finish_reason: str = "stop",
        started: float | None = None,
    ) -> ModelResponse:
        """统一填充 model_version/provider/adapter_version/latency_ms。"""
        started_at = started if started is not None else time.perf_counter()
        return ModelResponse(
            text=text,
            model_version=self.model_version,
            provider=self.provider,
            adapter_version=self.adapter_version,
            usage=usage or ModelUsage(),
            latency_ms=round(max(time.perf_counter() - started_at, 0.0) * 1000, 2),
            finish_reason=finish_reason,
        )


__all__ = ["ModelAdapter"]
