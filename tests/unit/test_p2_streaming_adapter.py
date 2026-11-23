"""P2-T1 单元测试：OpenAI 兼容适配器的**流式读取**与超时中止。

为什么需要它（实测根因）：非流式请求在网关超时后仍会在服务端把输出生成完，
本地单 worker 端点被占住，后续调用级联超时。流式 + 读循环内的墙钟判定能在
超时时**关闭连接**，让端点中止生成。

覆盖：

1. SSE 分块拼接 content、`usage` 取自末尾块、`finish_reason` 透传；
2. 超过墙钟上限 → `MODEL_TIMEOUT`（而不是空内容/格式错误）；
3. 非 2xx → `MODEL_CALL_FAILED` 且带 status_code；
4. 非流式（默认）行为不变：请求体里不出现 `stream`。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.errors import ModelCallFailedError, ModelTimeoutError
from core.models.adapters.openai_compat import OpenAICompatibleAdapter
from core.models.contracts import ModelMessage, ModelRequest

_SSE = (
    b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"true}"},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    b"data: [DONE]\n\n"
)


def _request() -> ModelRequest:
    return ModelRequest(
        messages=[ModelMessage(role="user", content="ping")],
        response_format="json",
        max_tokens=64,
    )


def _adapter(handler: Any, **kwargs: Any) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        model_name="stub",
        base_url="http://stub.local/v1",
        api_key="k",
        http_timeout_s=kwargs.pop("http_timeout_s", 30.0),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_streaming_concatenates_deltas_and_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_SSE)

    response = _adapter(handler, stream=True).complete(_request())

    assert response.text == '{"ok":true}'
    assert response.usage.total_tokens == 10
    assert response.finish_reason == "stop"
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}


def test_non_streaming_body_has_no_stream_flag() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    response = _adapter(handler).complete(_request())

    assert response.text == "ok"
    assert "stream" not in seen["body"]


def test_streaming_deadline_raises_timeout() -> None:
    """墙钟上限极小 → 读循环第一行即判超时（证明 deadline 生效，而非静默读完）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SSE)

    adapter = _adapter(handler, stream=True, http_timeout_s=1e-9)
    with pytest.raises(ModelTimeoutError) as excinfo:
        adapter.complete(_request())
    assert excinfo.value.code.value == "MODEL_TIMEOUT"


def test_streaming_non_2xx_is_call_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    adapter = _adapter(handler, stream=True)
    with pytest.raises(ModelCallFailedError) as excinfo:
        adapter.complete(_request())
    assert excinfo.value.details["status_code"] == 500


def test_streaming_empty_content_is_failed_not_hallucinated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    adapter = _adapter(handler, stream=True)
    with pytest.raises(ModelCallFailedError):
        adapter.complete(_request())
