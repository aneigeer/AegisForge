"""根据配置构造 Model Gateway；应用进程不持有数据库凭据给模型。"""

from __future__ import annotations

from core.config import Settings
from core.errors import ConfigError
from core.models.adapters.fake import HeuristicDiagnosticAdapter
from core.models.adapters.openai_compat import OpenAICompatibleAdapter
from core.models.contracts import ModelAdapterProtocol, ModelRole
from core.models.degradation import DisabledModelGateway
from core.models.gateway import ModelGateway
from core.models.router import ModelRoleRouter, coerce_role


def build_model_gateway(settings: Settings) -> ModelGateway | DisabledModelGateway:
    provider = settings.model_provider
    role_models = dict(getattr(settings, "model_role_models", None) or {})
    role_models = dict(getattr(settings, "model_role_models", None) or {})
    if provider == "disabled":
        # A9（设计 §6.5）：provider=disabled 必须可构造，返回占位网关而不是
        # 装配期抛 ConfigError；所有调用抛 ModelUnavailableError，由降级路径兜住。
        return DisabledModelGateway()
    if provider == "fake":
        if role_models:
            raise ConfigError(
                "角色路由仅支持 openai_compat：fake 为单桩适配器，无法按角色区分模型",
                details={"model_provider": provider, "roles": sorted(role_models)},
            )
        adapter = HeuristicDiagnosticAdapter(settings.model_name)
    elif provider == "openai_compat":
        if not settings.model_base_url or settings.model_api_key is None:
            raise ConfigError(
                "openai_compat 模式需要 DG_MODEL_BASE_URL 与 DG_MODEL_API_KEY",
                details={"model_provider": provider, "fields": ["model_base_url", "model_api_key"]},
            )
        api_key = settings.model_api_key.get_secret_value()

        def _adapter(model_name: str) -> OpenAICompatibleAdapter:
            return OpenAICompatibleAdapter(
                model_name=model_name,
                base_url=settings.model_base_url or "",
                api_key=api_key,
                http_timeout_s=settings.model_timeout_s,
                stream=bool(getattr(settings, "model_stream", False)),
            )

        default_adapter = _adapter(settings.model_name)
        role_adapters: dict[ModelRole, ModelAdapterProtocol] = {}
        for raw_role, model_name in role_models.items():
            role = coerce_role(raw_role)
            if not str(model_name).strip():
                raise ConfigError(
                    f"角色 {role.value} 的模型名不得为空",
                    details={"role": role.value},
                )
            role_adapters[role] = _adapter(str(model_name))
        adapter = (
            ModelRoleRouter(default_adapter, role_adapters=role_adapters)
            if role_adapters
            else default_adapter
        )
    else:
        raise ConfigError(
            "未配置模型服务；请设置 DG_MODEL_PROVIDER=fake 或 openai_compat",
            details={"model_provider": provider},
        )

    return ModelGateway(
        adapter,
        timeout_s=settings.model_timeout_s,
        default_max_tokens=settings.model_max_tokens,
        input_price_per_1k=getattr(settings, "model_input_price_per_1k", None),
        output_price_per_1k=getattr(settings, "model_output_price_per_1k", None),
        use_json_schema=bool(getattr(settings, "model_use_json_schema", True)),
    )


__all__ = ["build_model_gateway"]
