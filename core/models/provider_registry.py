"""P1-BYOK-T1：租户级模型供应商注册表（加密存储 + 掩码回显 + 按租户装配网关）。

设计要点（INV-02 / INV-09）：

- **密钥加密**：`api_key` 用 Fernet（密钥由 `DG_SECRET_MASTER_KEY` 经 SHA-256 派生）
  加密后落库；仓库层只在 `build_gateway` 内部解密，**不解密给 API 层**；
- **掩码**：列表/详情只返回 `sk-***1234` 形态的 `api_key_preview`；
- **SSRF**：`base_url` 在写入前过 `core.models.ssrf.validate_base_url`，
  allowlist / allow_http 来自部署配置（运维显式声明，不自动放行私网）；
- **租户隔离**：所有查询强制带 `tenant_id`，跨租户一律 404（不泄露存在性）；
- **单活**：`activate` 在同一事务语义下先把该租户其它供应商置 0，再置目标为 1。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.errors import (
    ModelProviderInvalidError,
    ModelProviderNotFoundError,
    SecretKeyMissingError,
)
from core.models.ssrf import validate_base_url

ProviderType = Literal["openai_compat", "azure_openai", "anthropic_compat", "disabled"]
PROVIDER_TYPES: tuple[str, ...] = (
    "openai_compat",
    "azure_openai",
    "anthropic_compat",
    "disabled",
)
#: 允许配置的角色。`embedding` 目前没有向量传输实现，**注册期直接拒绝**，
#: 避免出现"配了 embedding 却静默走 chat 适配器"的假能力。
ROLE_KEYS: tuple[str, ...] = ("planner", "classifier", "evaluator")

TABLE = "dg_system.tenant_model_providers"


def derive_key(master_key: str) -> bytes:
    """由主密钥派生 Fernet key（SHA-256 → urlsafe base64）。"""
    secret = (master_key or "").strip()
    if len(secret) < 16:
        raise SecretKeyMissingError(
            "DG_SECRET_MASTER_KEY 缺失或过短（至少 16 字符）",
            details={"field": "secret_master_key", "min_length": 16},
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def mask_api_key(raw: str) -> str:
    """`sk-abcdef1234` → `sk-***1234`；过短则整段掩码。"""
    value = (raw or "").strip()
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-4:]}"


class SecretBox:
    """对称加密包装；只暴露 `encrypt` / `decrypt` / `mask`。"""

    def __init__(self, master_key: str) -> None:
        self._fernet = Fernet(derive_key(master_key))

    def encrypt(self, raw: str) -> str:
        return self._fernet.encrypt(raw.encode("utf-8")).decode("ascii")

    def decrypt(self, cipher: str) -> str:
        try:
            return self._fernet.decrypt(cipher.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            raise ModelProviderInvalidError(
                "供应商密钥无法解密（主密钥已轮换或密文损坏）",
                details={"reason": "DECRYPT_FAILED"},
            ) from None

    @staticmethod
    def mask(raw: str) -> str:
        return mask_api_key(raw)


class ModelProviderRecord(BaseModel):
    """对外可见的供应商视图（**不含密文与明文**）。"""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    tenant_id: str
    name: str
    provider_type: ProviderType
    base_url: str
    api_key_preview: str
    role_models: dict[str, str] = Field(default_factory=dict)
    timeout_s: int = 60
    stream: bool = False
    active: bool = False
    ssrf_checked_version: str = "ssrf-v1"
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("role_models")
    @classmethod
    def _roles_allowed(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_roles(value)

    @field_validator("timeout_s")
    @classmethod
    def _timeout_range(cls, value: int) -> int:
        return validate_timeout(value)


def validate_roles(value: Mapping[str, str]) -> dict[str, str]:
    """角色映射校验（注册期与更新期共用同一份规则）。"""
    roles = dict(value or {})
    unknown = sorted(set(roles) - set(ROLE_KEYS))
    if unknown:
        raise ValueError(f"不支持的角色：{unknown}（允许 {list(ROLE_KEYS)}）")
    for role, model in roles.items():
        if not str(model).strip():
            raise ValueError(f"角色 {role} 的模型名不得为空")
    return {role: str(model) for role, model in roles.items()}


def validate_timeout(value: int) -> int:
    if not 1 <= int(value) <= 600:
        raise ValueError("timeout_s 必须在 1..600")
    return int(value)


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _row_to_record(row: Mapping[str, Any]) -> ModelProviderRecord:
    role_models = row.get("role_models_json")
    if isinstance(role_models, str):
        role_models = json.loads(role_models)
    return ModelProviderRecord(
        provider_id=str(row.get("root_id") or row["provider_id"]),
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"]),
        provider_type=str(row["provider_type"]),  # type: ignore[arg-type]
        base_url=str(row["base_url"]),
        api_key_preview=str(row["api_key_preview"]),
        role_models=dict(role_models or {}),
        timeout_s=int(row["timeout_s"]),
        stream=bool(row["stream"]),
        active=str(row.get("status") or ("ACTIVE" if row.get("active") else "INACTIVE"))
        == "ACTIVE",
        ssrf_checked_version=str(row.get("ssrf_checked_version") or "ssrf-v1"),
        created_at=str(row.get("created_at")) if row.get("created_at") else None,
        updated_at=str(row.get("updated_at")) if row.get("updated_at") else None,
    )


class ModelProviderRepository:
    """控制库上的供应商仓库；所有方法强制租户作用域。

    **只追加版本**：控制库账号 `dg_ctl` 只有 SELECT/INSERT（无 UPDATE/DELETE），
    因此改/删/激活都写一条 `revision+1` 的新行，读路径取每个 `root_id` 的最新
    revision。对外 `provider_id` 恒等于 `root_id`，调用方感知不到版本细节。
    """

    def __init__(
        self,
        client: Any,
        *,
        box: SecretBox | None = None,
        master_key: str | None = None,
        allowlist: Iterable[str] = (),
        allow_http: bool = False,
        resolver: Any | None = None,
    ) -> None:
        if box is None:
            if master_key is None:
                raise SecretKeyMissingError(
                    "未提供 SecretBox 或主密钥，无法管理 BYOK 供应商",
                    details={"field": "secret_master_key"},
                )
            box = SecretBox(master_key)
        self._client = client
        self._box = box
        self._allowlist = tuple(str(item) for item in allowlist)
        self._allow_http = bool(allow_http)
        # 解析器可注入：测试用确定性桩，生产走 socket.getaddrinfo（连接前判定，防 rebinding）。
        self._resolver = resolver

    # ---- 写入（全部 INSERT） --------------------------------------------
    def create(
        self,
        tenant_id: str,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        api_key: str | None,
        role_models: Mapping[str, str] | None = None,
        timeout_s: int = 60,
        stream: bool = False,
        active: bool = False,
    ) -> ModelProviderRecord:
        self._validate_common(name, provider_type, role_models, timeout_s)
        safe_url = validate_base_url(
            base_url,
            allowlist=self._allowlist,
            allow_http=self._allow_http,
            resolver=self._resolver,
        )
        key_value = (api_key or "").strip()
        if provider_type != "disabled" and not key_value:
            raise ModelProviderInvalidError(
                "启用中的供应商必须提供 api_key",
                details={"field": "api_key", "provider_type": provider_type},
            )
        root_id = str(uuid.uuid4())
        self._insert(
            root_id=root_id,
            revision=1,
            tenant_id=tenant_id,
            name=name,
            provider_type=provider_type,
            base_url=safe_url,
            cipher=self._box.encrypt(key_value) if key_value else "",
            preview=mask_api_key(key_value) if key_value else "***",
            role_models=dict(role_models or {}),
            timeout_s=int(timeout_s),
            stream=bool(stream),
            status="ACTIVE" if active else "INACTIVE",
        )
        return self.get(tenant_id, root_id)

    def update(
        self,
        tenant_id: str,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        role_models: Mapping[str, str] | None = None,
        timeout_s: int | None = None,
        stream: bool | None = None,
        provider_type: str | None = None,
    ) -> ModelProviderRecord:
        record, cipher = self.get(tenant_id, provider_id, with_secret=True)
        next_type = str(provider_type or record.provider_type)
        if next_type not in PROVIDER_TYPES:
            raise ModelProviderInvalidError(
                "provider_type 非法", details={"provider_type": next_type}
            )
        next_name = str(name).strip() if name is not None else record.name
        if not next_name:
            raise ModelProviderInvalidError("name 不能为空", details={"field": "name"})
        next_url = record.base_url
        if base_url is not None:
            next_url = validate_base_url(
                base_url,
                allowlist=self._allowlist,
                allow_http=self._allow_http,
                resolver=self._resolver,
            )
        next_roles = (
            validate_roles(dict(role_models)) if role_models is not None else dict(record.role_models)
        )
        next_timeout = (
            validate_timeout(int(timeout_s)) if timeout_s is not None else record.timeout_s
        )
        next_stream = bool(stream) if stream is not None else record.stream
        next_cipher = cipher
        next_preview = record.api_key_preview
        if api_key is not None:
            raw = api_key.strip()
            next_cipher = self._box.encrypt(raw) if raw else ""
            next_preview = mask_api_key(raw) if raw else "***"

        self._insert(
            root_id=provider_id,
            revision=self._next_revision(tenant_id, provider_id),
            tenant_id=tenant_id,
            name=next_name,
            provider_type=next_type,
            base_url=next_url,
            cipher=next_cipher,
            preview=next_preview,
            role_models=next_roles,
            timeout_s=next_timeout,
            stream=next_stream,
            status="ACTIVE" if record.active else "INACTIVE",
        )
        return self.get(tenant_id, provider_id)

    def delete(self, tenant_id: str, provider_id: str) -> None:
        record, cipher = self.get(tenant_id, provider_id, with_secret=True)
        # 只追加墓碑：不物理删除，保留历史配置（含密文）供审计追溯。
        self._insert(
            root_id=provider_id,
            revision=self._next_revision(tenant_id, provider_id),
            tenant_id=tenant_id,
            name=record.name,
            provider_type=record.provider_type,
            base_url=record.base_url,
            cipher=cipher,
            preview=record.api_key_preview,
            role_models=dict(record.role_models),
            timeout_s=record.timeout_s,
            stream=record.stream,
            status="DELETED",
        )

    def activate(self, tenant_id: str, provider_id: str) -> ModelProviderRecord:
        record, cipher = self.get(tenant_id, provider_id, with_secret=True)
        if record.provider_type == "disabled":
            raise ModelProviderInvalidError(
                "disabled 供应商不可激活", details={"provider_id": provider_id}
            )
        self._insert(
            root_id=provider_id,
            revision=self._next_revision(tenant_id, provider_id),
            tenant_id=tenant_id,
            name=record.name,
            provider_type=record.provider_type,
            base_url=record.base_url,
            cipher=cipher,
            preview=record.api_key_preview,
            role_models=dict(record.role_models),
            timeout_s=record.timeout_s,
            stream=record.stream,
            status="ACTIVE",
        )
        return self.get(tenant_id, provider_id)

    # ---- 读取 ------------------------------------------------------------
    def list(self, tenant_id: str) -> tuple[ModelProviderRecord, ...]:
        latest = self._latest(tenant_id)
        alive = [row for row in latest.values() if str(row.get("status")) != "DELETED"]
        alive.sort(key=lambda row: (str(row.get("created_at")), str(row.get("root_id"))))
        # 只追加语义下，历史 ACTIVE 标记不会被物理改写；"当前生效"由最近一次
        # 激活决定，因此列表里只把胜出者标为 active，避免出现"两个都 active"的误导。
        winner = self.active_for(tenant_id)
        winner_id = winner[0].provider_id if winner is not None else None
        records = []
        for row in alive:
            record = _row_to_record(row)
            records.append(
                record.model_copy(update={"active": record.provider_id == winner_id})
            )
        return tuple(records)

    def get(
        self, tenant_id: str, provider_id: str, *, with_secret: bool = False
    ) -> Any:
        row = self._latest(tenant_id).get(provider_id)
        if row is None or str(row.get("status")) == "DELETED":
            raise ModelProviderNotFoundError(
                "供应商不存在", details={"provider_id": provider_id}
            )
        record = _row_to_record(row)
        if with_secret:
            return record, row.get("api_key_cipher") or ""
        return record

    def active_for(self, tenant_id: str) -> Any:
        latest = self._latest(tenant_id)
        actives = [
            row
            for row in latest.values()
            if str(row.get("status")) == "ACTIVE"
        ]
        if not actives:
            return None
        actives.sort(key=lambda row: str(row.get("created_at") or ""))
        row = actives[-1]
        return _row_to_record(row), row.get("api_key_cipher") or ""

    def decrypt_key(self, cipher: str) -> str:
        return self._box.decrypt(str(cipher))

    # ---- 内部 ------------------------------------------------------------
    def _insert(
        self,
        *,
        root_id: str,
        revision: int,
        tenant_id: str,
        name: str,
        provider_type: str,
        base_url: str,
        cipher: str,
        preview: str,
        role_models: Mapping[str, str],
        timeout_s: int,
        stream: bool,
        status: str,
    ) -> None:
        now = _now()
        self._client.execute(
            f"INSERT INTO {TABLE} (provider_id, root_id, revision, tenant_id, name,"
            " provider_type, base_url, api_key_cipher, api_key_preview, role_models_json,"
            " timeout_s, stream, active, status, ssrf_checked_version, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                str(uuid.uuid4()),
                root_id,
                int(revision),
                tenant_id,
                name.strip(),
                provider_type,
                base_url,
                cipher,
                preview,
                json.dumps(dict(role_models), ensure_ascii=False, sort_keys=True),
                int(timeout_s),
                1 if stream else 0,
                1 if status == "ACTIVE" else 0,
                status,
                "ssrf-v1",
                now,
                now,
            ),
        )

    def _rows(self, tenant_id: str) -> list[dict[str, Any]]:
        return list(
            self._client.fetch_all(
                f"SELECT * FROM {TABLE} WHERE tenant_id = %s"
                " ORDER BY revision DESC, created_at DESC",
                (tenant_id,),
            )
        )

    def _latest(self, tenant_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self._rows(tenant_id):
            key = str(row.get("root_id") or row.get("provider_id"))
            if key not in latest:
                latest[key] = dict(row)
        return latest

    def _next_revision(self, tenant_id: str, provider_id: str) -> int:
        row = self._latest(tenant_id).get(provider_id)
        if row is None:
            raise ModelProviderNotFoundError(
                "供应商不存在", details={"provider_id": provider_id}
            )
        return int(row.get("revision") or 1) + 1

    # ---- 校验 ------------------------------------------------------------
    @staticmethod
    def _validate_common(
        name: str,
        provider_type: str,
        role_models: Mapping[str, str] | None,
        timeout_s: int,
    ) -> None:
        if provider_type not in PROVIDER_TYPES:
            raise ModelProviderInvalidError(
                "provider_type 非法",
                details={"provider_type": provider_type, "allowed": list(PROVIDER_TYPES)},
            )
        if not str(name or "").strip():
            raise ModelProviderInvalidError("name 不能为空", details={"field": "name"})
        validate_roles(dict(role_models or {}))
        validate_timeout(int(timeout_s))


# ---------------------------------------------------------------------------
# 从供应商记录装配 ModelGateway（唯一解密点，API 层拿不到明文）
# ---------------------------------------------------------------------------


def build_gateway_from_provider(
    record: ModelProviderRecord,
    api_key: str,
    *,
    settings: Any,
    quota: Any | None = None,
    allowlist: Iterable[str] = (),
    allow_http: bool = False,
) -> Any:
    """按供应商记录构造 `ModelGateway`（角色映射为空时回落 `DG_MODEL_NAME`）。

    - 适配器：`openai_compat` / `anthropic_compat` 走 Bearer；`azure_openai` 走 `api-key` 头；
    - SSRF：连接前再用同一个 `validate_base_url` 复校验（防 DNS rebinding）；
    - 配额：由调用方注入 `TenantQuota`，网关在发请求前拦截。
    """
    from core.models.adapters.openai_compat import OpenAICompatibleAdapter
    from core.models.gateway import ModelGateway
    from core.models.router import ModelRoleRouter

    if record.provider_type == "disabled":
        raise ModelProviderInvalidError(
            "disabled 供应商不可用于推理", details={"provider_id": record.provider_id}
        )

    roles = record.role_models or {}
    default_model = (
        roles.get("planner") or roles.get("classifier") or str(settings.model_name)
    )
    auth_header = "api-key" if record.provider_type == "azure_openai" else "bearer"

    def _guard(url: str) -> str:
        return validate_base_url(url, allowlist=allowlist, allow_http=allow_http)

    def _adapter(model_name: str) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            model_name=model_name,
            base_url=record.base_url,
            api_key=api_key,
            http_timeout_s=float(record.timeout_s),
            stream=bool(record.stream),
            auth_header=auth_header,
            protocol="auto",
            ssrf_guard=_guard,
        )

    default_adapter = _adapter(default_model)
    role_adapters = {
        role: _adapter(model) for role, model in roles.items() if model != default_model
    }
    router = ModelRoleRouter(default_adapter, role_adapters=role_adapters)
    return ModelGateway(
        router,
        timeout_s=float(record.timeout_s),
        default_max_tokens=int(getattr(settings, "model_max_tokens", 1024) or 1024),
        use_json_schema=bool(getattr(settings, "model_use_json_schema", True)),
        quota=quota,
    )


def resolve_tenant_gateway(
    settings: Any,
    control_client: Any,
    tenant_id: str,
    *,
    quota: Any | None = None,
) -> tuple[Any, ModelProviderRecord | None]:
    """返回 `(gateway, active_record)`；无 active 配置或未配主密钥时回落全局网关。

    回落是**显式**行为：调用方可用返回的 `active_record is None` 判断走的是全局配置，
    不需要也不允许从日志里推断密钥。
    """
    from core.models.factory import build_model_gateway

    master = getattr(settings, "secret_master_key", None)
    if master is None:
        return build_model_gateway(settings), None
    repository = ModelProviderRepository(
        control_client,
        master_key=master.get_secret_value(),
        allowlist=getattr(settings, "byok_allowed_hosts", ()) or (),
        allow_http=bool(getattr(settings, "byok_allow_http", False)),
    )
    active = repository.active_for(tenant_id)
    if active is None:
        return build_model_gateway(settings), None
    record, cipher = active
    api_key = repository.decrypt_key(cipher) if cipher else ""
    gateway = build_gateway_from_provider(
        record,
        api_key,
        settings=settings,
        quota=quota,
        allowlist=getattr(settings, "byok_allowed_hosts", ()) or (),
        allow_http=bool(getattr(settings, "byok_allow_http", False)),
    )
    return gateway, record
