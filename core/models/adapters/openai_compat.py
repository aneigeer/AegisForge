"""OpenAI 兼容（/chat/completions）HTTP 适配器。

只发送 messages/model/temperature/max_tokens 与必要参数；不发送任何数据库
凭据。错误细节只保留 status_code 与脱敏后的响应摘要。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.errors import (
    ModelCallFailedError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from core.logging import redact_text
from core.models.adapters.base import ModelAdapter
from core.models.contracts import ModelRequest, ModelResponse, ModelUsage

#: P1-BYOK-T3：协议档位（严格 → 宽松），仅"端点明确拒绝"时逐级下降。
_PROTOCOL_TIERS: tuple[str, ...] = ("json_schema", "json_object", "prompt_json")

#: 端点拒绝 response_format 时常见的状态码与文本线索（用于判定是否降级）。
_SCHEMA_REJECT_STATUS: frozenset[int] = frozenset({400, 404, 415, 422})
_SCHEMA_REJECT_HINTS: tuple[str, ...] = (
    "response_format",
    "json_schema",
    "json_schema",
    "schema",
    "json mode",
    "unsupported",
    "not support",
)

#: 可重试状态码（429 限流 / 503 不可用），退避受墙钟预算约束。
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 503})
_RETRY_BASE_S: float = 0.5

_PREVIEW_LIMIT = 200
_UNAUTHORIZED_STATUS = {401, 403}


class OpenAICompatibleAdapter(ModelAdapter):
    provider = "openai_compat"
    adapter_version = "0.1.0"

    def __init__(
        self,
        model_name: str,
        base_url: str,
        api_key: str,
        *,
        http_timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        stream: bool = False,
        extra_headers: dict[str, str] | None = None,
        verify: bool | str = True,
        auth_header: str = "bearer",
        protocol: str = "auto",
        max_retries: int = 2,
        ssrf_guard: "object | None" = None,
    ) -> None:
        self.model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http_timeout_s = http_timeout_s
        # P2-T1：流式读取（opt-in）。非流式请求在网关超时后仍会在服务端把输出生成完，
        # 单 worker 端点会被拖住；流式 + 显式 deadline 才能在超时时**真正断开**连接。
        self._stream = bool(stream)
        # P1-BYOK-T3：企业网关常需要自定义头 / 私有 CA；协议档位用于自动降级。
        self._extra_headers = dict(extra_headers or {})
        self._auth_header = "api-key" if auth_header == "api-key" else "bearer"
        self._protocol = protocol if protocol in _PROTOCOL_TIERS + ("auto",) else "auto"
        self._protocol_index = 0
        self._max_retries = max(int(max_retries), 0)
        # P1-BYOK-T2：连接前再校验一次（防 DNS rebinding）；注册期已校验，属纵深防御。
        self._ssrf_guard = ssrf_guard
        self._client = httpx.Client(
            timeout=http_timeout_s, transport=transport, verify=verify
        )

    def close(self) -> None:
        self._client.close()

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        if self._ssrf_guard is not None:
            self._ssrf_guard(self._base_url)
        tiers = self._tiers()
        last_error: ModelCallFailedError | None = None
        for index in range(self._protocol_index, len(tiers)):
            tier = tiers[index]
            body = self._body_for(request, tier)
            try:
                if self._stream:
                    response = self._complete_streaming(request, body, started)
                else:
                    response = self._complete_plain(request, body, started)
            except ModelCallFailedError as exc:
                if not self._should_downgrade(exc, tier, index, tiers):
                    raise
                last_error = exc
                # 记住降级位置：后续同类调用直接从可用档位开始，避免每次先撞一次 400。
                self._protocol_index = index + 1
                continue
            self._protocol_index = index
            return response
        if last_error is not None:
            raise last_error
        raise ModelCallFailedError(
            "协议协商失败：没有任何可用档位",
            details={"provider": self.provider, "model_version": self.model_version},
        )

    def _tiers(self) -> tuple[str, ...]:
        if self._protocol != "auto":
            return (self._protocol,)
        return _PROTOCOL_TIERS

    def _should_downgrade(
        self,
        exc: ModelCallFailedError,
        tier: str,
        index: int,
        tiers: tuple[str, ...],
    ) -> bool:
        """仅在"端点明确拒绝该 response_format"时降级；其余错误原样失败。"""
        if tier == "prompt_json" or index + 1 >= len(tiers):
            return False
        status = exc.details.get("status_code")
        if status not in _SCHEMA_REJECT_STATUS:
            return False
        preview = str(exc.details.get("response_preview") or "").lower()
        return any(token in preview for token in _SCHEMA_REJECT_HINTS)

    def _body_for(self, request: ModelRequest, tier: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self.model_name,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.response_schema and tier == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": str(request.response_schema.get("title") or "output"),
                    "schema": request.response_schema,
                    "strict": True,
                },
            }
        elif request.response_format == "json" and tier in {"json_schema", "json_object"}:
            body["response_format"] = {"type": "json_object"}
        # prompt_json 档：不带 response_format，靠提示词约束 + 网关侧围栏剥离。
        return body

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth_header == "api-key":
            headers["api-key"] = self._api_key
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """POST 并在 429/503 上做有界指数退避（不超出墙钟预算）。"""
        deadline = time.perf_counter() + float(self._http_timeout_s)
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
            except httpx.TimeoutException:
                raise ModelTimeoutError(
                    "模型服务响应超时",
                    details={
                        "provider": self.provider,
                        "model_version": self.model_version,
                        "timeout_s": self._http_timeout_s,
                    },
                ) from None
            except httpx.TransportError:
                raise ModelUnavailableError(
                    "模型服务不可达",
                    details={"provider": self.provider, "model_version": self.model_version},
                ) from None
            if response.status_code not in _RETRYABLE_STATUS or attempt >= self._max_retries:
                return response
            remaining = deadline - time.perf_counter()
            delay = min(_RETRY_BASE_S * (2**attempt), max(remaining, 0.0))
            if delay <= 0:
                return response
            attempt += 1
            time.sleep(delay)

    def _complete_plain(
        self, request: ModelRequest, body: dict[str, Any], started: float
    ) -> ModelResponse:
        response = self._post(body)
        preview = redact_text(response.text[:_PREVIEW_LIMIT])

        if response.status_code in _UNAUTHORIZED_STATUS:
            raise ModelUnavailableError(
                "模型服务鉴权失败",
                details={
                    "provider": self.provider,
                    "model_version": self.model_version,
                    "status_code": response.status_code,
                },
            )
        if not 200 <= response.status_code < 300:
            raise ModelCallFailedError(
                "模型服务返回非 2xx 状态",
                details={
                    "provider": self.provider,
                    "model_version": self.model_version,
                    "status_code": response.status_code,
                    "response_preview": preview,
                },
            )

        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content 不是字符串")
            usage_payload = payload.get("usage") or {}
            usage = ModelUsage(
                prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
                completion_tokens=int(usage_payload.get("completion_tokens") or 0),
                total_tokens=int(usage_payload.get("total_tokens") or 0),
            )
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelCallFailedError(
                "模型服务响应格式非法",
                details={
                    "provider": self.provider,
                    "model_version": self.model_version,
                    "status_code": response.status_code,
                    "response_preview": preview,
                },
            ) from None

        return self.make_response(
            content,
            request,
            usage=usage,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            started=started,
        )

    def _complete_streaming(
        self, request: ModelRequest, body: dict[str, Any], started: float
    ) -> ModelResponse:
        """流式读取 SSE：逐块拼接 content，超 deadline 立即抛错以关闭连接。

        与 OpenAI 兼容约定一致：`data: {json}` 行、`data: [DONE]` 结束；
        `stream_options.include_usage=true` 时末尾块带 `usage`。
        """
        payload_body = dict(body)
        payload_body["stream"] = True
        payload_body["stream_options"] = {"include_usage": True}
        deadline = started + float(self._http_timeout_s)

        chunks: list[str] = []
        usage_payload: dict[str, Any] = {}
        finish_reason: str | None = None
        try:
            with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers(),
                json=payload_body,
            ) as response:
                if response.status_code in _UNAUTHORIZED_STATUS:
                    response.read()
                    raise ModelUnavailableError(
                        "模型服务鉴权失败",
                        details={
                            "provider": self.provider,
                            "model_version": self.model_version,
                            "status_code": response.status_code,
                        },
                    )
                if not 200 <= response.status_code < 300:
                    raw = response.read()
                    preview = redact_text(raw[: _PREVIEW_LIMIT].decode("utf-8", "replace"))
                    raise ModelCallFailedError(
                        "模型服务返回非 2xx 状态",
                        details={
                            "provider": self.provider,
                            "model_version": self.model_version,
                            "status_code": response.status_code,
                            "response_preview": preview,
                        },
                    )
                for line in response.iter_lines():
                    # 关键：墙钟判定放在**读循环内**；抛错即退出 with，httpx 关闭 socket，
                    # 端点侧随之中止生成（不再占用单 worker 队列）。
                    if time.perf_counter() > deadline:
                        raise ModelTimeoutError(
                            "模型服务响应超时（流式读取超过墙钟上限）",
                            details={
                                "provider": self.provider,
                                "model_version": self.model_version,
                                "timeout_s": self._http_timeout_s,
                                "partial_chars": sum(len(item) for item in chunks),
                            },
                        )
                    if not line:
                        continue
                    text_line = line.strip()
                    if not text_line.startswith("data:"):
                        continue
                    data = text_line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload.get("usage"), dict):
                        usage_payload = payload["usage"]
                    for choice in payload.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
        except httpx.TimeoutException:
            raise ModelTimeoutError(
                "模型服务响应超时",
                details={
                    "provider": self.provider,
                    "model_version": self.model_version,
                    "timeout_s": self._http_timeout_s,
                },
            ) from None
        except httpx.TransportError:
            raise ModelUnavailableError(
                "模型服务不可达",
                details={"provider": self.provider, "model_version": self.model_version},
            ) from None

        text = "".join(chunks)
        if not text.strip():
            raise ModelCallFailedError(
                "模型服务未返回任何内容块",
                details={
                    "provider": self.provider,
                    "model_version": self.model_version,
                    "finish_reason": finish_reason,
                },
            )
        usage = ModelUsage(
            prompt_tokens=int(usage_payload.get("prompt_tokens") or 0),
            completion_tokens=int(usage_payload.get("completion_tokens") or 0),
            total_tokens=int(usage_payload.get("total_tokens") or 0),
        )
        return self.make_response(
            text,
            request,
            usage=usage,
            finish_reason=finish_reason or "stop",
            started=started,
        )


__all__ = ["OpenAICompatibleAdapter"]
