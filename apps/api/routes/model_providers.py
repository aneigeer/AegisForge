"""P1-BYOK-T1/T2：租户级模型供应商管理 API（`/v1/model-providers`）。

安全边界：

- **租户隔离**：`tenant_id` 只取自服务端 `Principal`；跨租户访问统一 404；
- **密钥**：响应只含 `api_key_preview`（`sk-***1234`），明文/密文都不出仓库层；
- **SSRF**：`base_url` 写入前过 `core.models.ssrf.validate_base_url`（allowlist 来自部署配置）；
- **RBAC**：读需 `model_provider:read`，写/测/激活需 `model_provider:manage`。

`/test` 会发起一次真实调用（管理员显式触发），失败以结构化 `ok=false` 返回，
**不回显**上游错误正文中的任何密钥片段。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_control_client, get_principal
from core.config import get_settings
from core.errors import DBGuardianError, SecretKeyMissingError
from core.models.provider_registry import (
    PROVIDER_TYPES,
    ModelProviderRecord,
    ModelProviderRepository,
    build_gateway_from_provider,
)
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission

router = APIRouter(prefix="/v1/model-providers", tags=["model-providers"])


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    provider_type: Literal[
        "openai_compat", "azure_openai", "anthropic_compat", "disabled"
    ] = "openai_compat"
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    role_models: dict[str, str] = Field(default_factory=dict)
    timeout_s: int = Field(default=60, ge=1, le=600)
    stream: bool = False
    active: bool = False


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    provider_type: Literal[
        "openai_compat", "azure_openai", "anthropic_compat", "disabled"
    ] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    role_models: dict[str, str] | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=600)
    stream: bool | None = None


def _repository(principal: Principal, control_client: Any) -> ModelProviderRepository:
    settings = get_settings()
    master = getattr(settings, "secret_master_key", None)
    if master is None:
        raise SecretKeyMissingError(
            "未配置 DG_SECRET_MASTER_KEY，无法启用 BYOK 供应商管理",
            details={"field": "secret_master_key"},
        )
    return ModelProviderRepository(
        control_client,
        master_key=master.get_secret_value(),
        allowlist=getattr(settings, "byok_allowed_hosts", ()) or (),
        allow_http=bool(getattr(settings, "byok_allow_http", False)),
    )


def _ok(record: ModelProviderRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    # 双保险：即使 record 结构变化，响应里也不允许出现密文/明文键名。
    payload.pop("api_key_cipher", None)
    payload.pop("api_key", None)
    return payload


@router.get("", response_model=list[dict])
def list_providers(
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> list[dict]:
    ensure_permission(principal, Permission.MODEL_PROVIDER_READ)
    repository = _repository(principal, control_client)
    return [_ok(record) for record in repository.list(principal.tenant_id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=dict)
def create_provider(
    body: ProviderCreate,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.MODEL_PROVIDER_MANAGE)
    repository = _repository(principal, control_client)
    record = repository.create(
        principal.tenant_id,
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=body.api_key,
        role_models=body.role_models,
        timeout_s=body.timeout_s,
        stream=body.stream,
        active=body.active,
    )
    return _ok(record)


@router.get("/{provider_id}", response_model=dict)
def get_provider(
    provider_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.MODEL_PROVIDER_READ)
    repository = _repository(principal, control_client)
    return _ok(repository.get(principal.tenant_id, provider_id))


@router.patch("/{provider_id}", response_model=dict)
def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.MODEL_PROVIDER_MANAGE)
    repository = _repository(principal, control_client)
    record = repository.update(
        principal.tenant_id,
        provider_id,
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        api_key=body.api_key,
        role_models=body.role_models,
        timeout_s=body.timeout_s,
        stream=body.stream,
    )
    return _ok(record)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> None:
    ensure_permission(principal, Permission.MODEL_PROVIDER_MANAGE)
    repository = _repository(principal, control_client)
    repository.delete(principal.tenant_id, provider_id)


@router.post("/{provider_id}/activate", response_model=dict)
def activate_provider(
    provider_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.MODEL_PROVIDER_MANAGE)
    repository = _repository(principal, control_client)
    return _ok(repository.activate(principal.tenant_id, provider_id))


@router.post("/{provider_id}/test", response_model=dict)
def test_provider(
    provider_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    """连通性 + 结构化输出探针（管理员显式触发一次真实调用）。"""
    ensure_permission(principal, Permission.MODEL_PROVIDER_MANAGE)
    settings = get_settings()
    repository = _repository(principal, control_client)
    record, cipher = repository.get(principal.tenant_id, provider_id, with_secret=True)
    api_key = repository.decrypt_key(cipher) if cipher else ""

    from pydantic import BaseModel as _BaseModel

    class _Probe(_BaseModel):
        ok: bool

    try:
        gateway = build_gateway_from_provider(
            record,
            api_key,
            settings=settings,
            allowlist=getattr(settings, "byok_allowed_hosts", ()) or (),
            allow_http=bool(getattr(settings, "byok_allow_http", False)),
        )
        output = gateway.generate_structured(
            [
                {"role": "system", "content": "只返回 JSON：{\"ok\": true}"},
                {"role": "user", "content": "ping"},
            ],
            _Probe,
            temperature=0.0,
            max_tokens=16,
            purpose="byok.provider_probe",
        )
    except DBGuardianError as exc:
        # 只回错误码与定位字段，不回上游正文（可能含网关自己的诊断信息）。
        return {
            "ok": False,
            "code": exc.code.value,
            "message": exc.message,
            "details": {
                key: value
                for key, value in (exc.details or {}).items()
                if key in {"status_code", "reason", "field", "timeout_s", "provider_id"}
            },
        }
    return {
        "ok": bool(output.parsed.ok),
        "model_version": output.response.model_version,
        "latency_ms": output.response.latency_ms,
        "usage": output.response.usage.model_dump(),
    }
