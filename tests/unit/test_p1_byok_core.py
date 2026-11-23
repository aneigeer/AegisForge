"""P1-BYOK 单元测试：加密存储、掩码、SSRF、配额、协议降级与重试。

覆盖验收口径：

- T1：`api_key` 加密落库、响应只有掩码、租户作用域（跨租户 404）、单活切换；
- T2：`169.254.169.254` / `localhost` / 控制库主机 / 数据库端口被拒；配额触顶 fail closed；
- T3：`json_schema` 被 400 拒绝时自动降到 `json_object` / `prompt_json`；429 有界重试；
  非法 JSON 仍 `MODEL_OUTPUT_INVALID`（fail closed）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.errors import (
    ModelOutputInvalidError,
    ModelProviderNotFoundError,
    QuotaExceededError,
    SsrfBlockedError,
)
from core.models.adapters.openai_compat import OpenAICompatibleAdapter
from core.models.contracts import ModelMessage, ModelRequest
from core.models.gateway import ModelGateway
from core.models.provider_registry import (
    ModelProviderRepository,
    SecretBox,
    mask_api_key,
)
from core.models.quota import TenantQuota
from core.models.ssrf import validate_base_url

# ---------------------------------------------------------------------------
# 假控制库：只实现本仓库用到的 execute/fetch_all，行为贴近 MySQL client
# ---------------------------------------------------------------------------


_COLUMNS = (
    "provider_id",
    "root_id",
    "revision",
    "tenant_id",
    "name",
    "provider_type",
    "base_url",
    "api_key_cipher",
    "api_key_preview",
    "role_models_json",
    "timeout_s",
    "stream",
    "active",
    "status",
    "ssrf_checked_version",
    "created_at",
    "updated_at",
)


class _FakeControl:
    """只实现 INSERT / SELECT 的控制库桩（与 dg_ctl 的真实权限一致：无 UPDATE/DELETE）。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        if not sql.lstrip().upper().startswith("INSERT"):
            raise AssertionError(f"控制库账号只允许 INSERT，收到：{sql[:40]}")
        self.rows.append(dict(zip(_COLUMNS, params or ())))
        return 1

    def fetch_all(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict]:
        if "WHERE tenant_id = %s" not in sql:
            raise AssertionError(f"未预期的 SQL：{sql[:60]}")
        tenant_id = (params or ("",))[0]
        rows = [dict(row) for row in self.rows if row["tenant_id"] == tenant_id]
        rows.sort(key=lambda row: (-int(row["revision"]), str(row["created_at"])))
        return rows


def _repo(client: _FakeControl | None = None) -> tuple[ModelProviderRepository, _FakeControl]:
    control = client or _FakeControl()
    return (
        ModelProviderRepository(
            control,
            master_key="unit-test-master-key-32bytes!!",
            # 测试不触网：确定性解析器（生产用 socket.getaddrinfo）。
            resolver=lambda host, port: ("93.184.216.34",),
        ),
        control,
    )


# ---------------------------------------------------------------------------
# T1：加密 / 掩码 / 租户隔离 / 单活
# ---------------------------------------------------------------------------


def test_api_key_is_encrypted_and_masked() -> None:
    repo, control = _repo()
    record = repo.create(
        "tenant_a",
        name="prod",
        provider_type="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="sk-live-abcdef1234",
        role_models={"planner": "gpt-4o-mini"},
    )
    assert record.api_key_preview == "sk-***1234"
    stored = control.rows[-1]
    assert stored["root_id"] == record.provider_id
    assert "sk-live-abcdef1234" not in json.dumps(control.rows, ensure_ascii=False)
    assert repo.decrypt_key(stored["api_key_cipher"]) == "sk-live-abcdef1234"


def test_cross_tenant_access_is_404() -> None:
    repo, _ = _repo()
    record = repo.create(
        "tenant_a",
        name="a",
        provider_type="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="sk-aaaa1111",
    )
    with pytest.raises(ModelProviderNotFoundError):
        repo.get("tenant_b", record.provider_id)
    with pytest.raises(ModelProviderNotFoundError):
        repo.delete("tenant_b", record.provider_id)
    assert repo.list("tenant_b") == ()


def test_activate_is_single_active_per_tenant() -> None:
    repo, _ = _repo()
    first = repo.create(
        "t1",
        name="a",
        provider_type="openai_compat",
        base_url="https://a.example.com/v1",
        api_key="sk-1111aaaa",
        active=True,
    )
    second = repo.create(
        "t1",
        name="b",
        provider_type="openai_compat",
        base_url="https://b.example.com/v1",
        api_key="sk-2222bbbb",
    )
    repo.activate("t1", second.provider_id)
    active = repo.active_for("t1")
    assert active is not None and active[0].provider_id == second.provider_id
    assert [r.active for r in repo.list("t1")] == [False, True]


def test_mask_api_key_variants() -> None:
    assert mask_api_key("sk-1234567890") == "sk-***7890"
    assert mask_api_key("short") == "***"


# ---------------------------------------------------------------------------
# T2：SSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://169.254.169.254/latest/meta-data", "SCHEME_NOT_ALLOWED"),
        ("https://169.254.169.254/latest/meta-data", "BLOCKED_ADDRESS"),
        ("https://localhost/v1", "BLOCKED_HOSTNAME"),
        ("https://control_db:8000/v1", "BLOCKED_HOSTNAME"),
        ("https://api.example.com:3306/v1", "BLOCKED_PORT"),
        ("https://user:pass@api.example.com/v1", "USERINFO_NOT_ALLOWED"),
    ],
)
def test_ssrf_blocks_dangerous_targets(url: str, reason: str) -> None:
    def resolver(host: str, port: int) -> tuple[str, ...]:
        if host == "api.example.com":
            return ("93.184.216.34",)
        return ("127.0.0.1",)

    with pytest.raises(SsrfBlockedError) as excinfo:
        validate_base_url(url, resolver=resolver)
    details = excinfo.value.details
    assert details.get("reason") == reason or details.get("reason") == "BLOCKED_HOSTNAME"


def test_ssrf_allows_public_https() -> None:
    url = "https://api.example.com/v1"
    assert (
        validate_base_url(url, resolver=lambda host, port: ("93.184.216.34",)) == url
    )


def test_ssrf_allowlist_skips_private_network_but_keeps_https() -> None:
    with pytest.raises(SsrfBlockedError):
        validate_base_url(
            "http://gw.internal/v1", allowlist=("gw.internal",), allow_http=False
        )
    assert (
        validate_base_url(
            "http://gw.internal/v1", allowlist=("gw.internal",), allow_http=True
        )
        == "http://gw.internal/v1"
    )


def test_registration_rejects_metadata_endpoint() -> None:
    repo, control = _repo()
    with pytest.raises(SsrfBlockedError):
        repo.create(
            "t1",
            name="evil",
            provider_type="openai_compat",
            base_url="https://169.254.169.254/latest/meta-data",
            api_key="sk-x",
        )
    assert control.rows == []


# ---------------------------------------------------------------------------
# T2：配额
# ---------------------------------------------------------------------------


def test_quota_call_budget_fails_closed_and_notifies() -> None:
    seen: list[str] = []
    quota = TenantQuota(
        tenant_id="t1", max_calls=2, on_exceeded=lambda reason, state: seen.append(reason)
    )
    quota.before_call()
    quota.before_call()
    with pytest.raises(QuotaExceededError) as excinfo:
        quota.before_call()
    assert excinfo.value.details["reason"] == "CALL_BUDGET_EXCEEDED"
    assert seen == ["CALL_BUDGET_EXCEEDED"]


def test_quota_token_budget_marks_exhausted_after_measurement() -> None:
    quota = TenantQuota(tenant_id="t1", max_tokens=100)
    quota.before_call()
    quota.record_tokens(150)  # 本次调用已完成，不回滚
    with pytest.raises(QuotaExceededError) as excinfo:
        quota.before_call()
    assert excinfo.value.details["reason"] == "QUOTA_EXHAUSTED"
    assert quota.snapshot()["tokens"] == 150


def test_gateway_enforces_quota_before_http() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    adapter = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "k",
        transport=httpx.MockTransport(handler),
        http_timeout_s=5,
    )
    gateway = ModelGateway(adapter, timeout_s=5, quota=TenantQuota(tenant_id="t", max_calls=1))
    gateway.generate([{"role": "user", "content": "hi"}], purpose="probe")
    with pytest.raises(QuotaExceededError):
        gateway.generate([{"role": "user", "content": "hi"}], purpose="probe")
    assert len(calls) == 1, "配额触顶后不得再发 HTTP"


# ---------------------------------------------------------------------------
# T3：协议降级 / 重试 / 围栏 / fail-closed
# ---------------------------------------------------------------------------


class _OkModel(__import__("pydantic").BaseModel):
    ok: bool


def _json_ok() -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_protocol_downgrades_on_schema_rejection() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(
                400,
                json={"error": {"message": "response_format json_schema unsupported"}},
            )
        return httpx.Response(200, json=_json_ok())

    adapter = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "k",
        transport=httpx.MockTransport(handler),
        http_timeout_s=5,
    )
    gateway = ModelGateway(adapter, timeout_s=5)
    output = gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    assert output.parsed.ok is True
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[1]["response_format"]["type"] == "json_object"
    # 已协商到 json_object：下一次直接用它，不再先撞 400。
    gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel)
    assert seen[2]["response_format"]["type"] == "json_object"


def test_retry_on_429_then_success() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=_json_ok())

    adapter = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "k",
        transport=httpx.MockTransport(handler),
        http_timeout_s=5,
    )
    gateway = ModelGateway(adapter, timeout_s=5)
    assert gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel).parsed.ok
    assert len(attempts) == 2


def test_fenced_json_is_stripped_but_invalid_still_fails_closed() -> None:
    def fenced(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "```json\n{\"ok\": true}\n```"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    adapter = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "k",
        transport=httpx.MockTransport(fenced),
        http_timeout_s=5,
        protocol="prompt_json",
    )
    gateway = ModelGateway(adapter, timeout_s=5)
    assert gateway.generate_structured([{"role": "user", "content": "x"}], _OkModel).parsed.ok

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{ok: true"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    adapter2 = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "k",
        transport=httpx.MockTransport(bad),
        http_timeout_s=5,
        protocol="prompt_json",
    )
    with pytest.raises(ModelOutputInvalidError):
        ModelGateway(adapter2, timeout_s=5).generate_structured(
            [{"role": "user", "content": "x"}], _OkModel
        )


def test_custom_headers_and_api_key_auth_style() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=_json_ok())

    adapter = OpenAICompatibleAdapter(
        "stub",
        "https://api.example.com/v1",
        "secret-key",
        transport=httpx.MockTransport(handler),
        http_timeout_s=5,
        auth_header="api-key",
        extra_headers={"X-Api-Version": "2024-06-01"},
    )
    ModelGateway(adapter, timeout_s=5).generate([{"role": "user", "content": "x"}], purpose="p")
    assert seen["headers"]["api-key"] == "secret-key"
    assert seen["headers"]["x-api-version"] == "2024-06-01"
    assert "authorization" not in seen["headers"]
