"""P1-T1：按 :class:`ModelRole` 路由到不同适配器（轻任务小模型 / 复杂 RCA 旗舰模型）。

设计边界（`docs/enterprise-upgrade-roadmap.json` P1-T1）：

- **只做路由**：不改 `ModelRequest` 契约、不绕过 `PolicyEngine`、不接触数据库凭据；
  适配器只拿到 `base_url` 与模型服务自己的 `api_key`（INV-02）。
- **未配置即回落**：只有显式映射的角色才走独立适配器，其余一律用 `default`；
  因此"不配置"与改造前行为逐字等价。
- **`embedding` 不支持**：当前只实现 `/chat/completions` 传输，`embedding` 角色若被映射
  会**装配期报错**（`ConfigError`），而不是拿 chat 适配器冒充向量模型。
- **可观测**：`mapped_roles()` / `model_for()` 供装配自检与报告引用；路由本身无状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.errors import ConfigError
from core.models.contracts import (
    ModelAdapterProtocol,
    ModelRequest,
    ModelResponse,
    ModelRole,
)

#: 允许路由的角色（`embedding` 缺 HTTP 传输实现，见模块文档）。
ROUTABLE_ROLES: tuple[ModelRole, ...] = (
    ModelRole.PLANNER,
    ModelRole.CLASSIFIER,
    ModelRole.EVALUATOR,
)

#: 明确不支持的映射角色（语义写进错误详情，便于装配期定位）。
UNSUPPORTED_ROLES: tuple[ModelRole, ...] = (ModelRole.EMBEDDING,)


class ModelRoleRouter:
    """按角色分派到适配器；本身满足 `ModelAdapterProtocol`（可直接交给 ModelGateway）。"""

    def __init__(
        self,
        default: ModelAdapterProtocol,
        *,
        role_adapters: Mapping[ModelRole | str, ModelAdapterProtocol] | None = None,
    ) -> None:
        self._default = default
        self._role_adapters: dict[ModelRole, ModelAdapterProtocol] = {}
        for raw_role, adapter in dict(role_adapters or {}).items():
            role = coerce_role(raw_role)
            if role in UNSUPPORTED_ROLES:
                raise ConfigError(
                    f"角色 {role.value} 暂不支持路由：当前只实现 /chat/completions 传输",
                    details={"role": role.value, "supported_roles": [r.value for r in ROUTABLE_ROLES]},
                )
            if role not in ROUTABLE_ROLES:  # pragma: no cover - 闭集兜底
                raise ConfigError(
                    f"未知模型角色：{role!r}",
                    details={"supported_roles": [r.value for r in ROUTABLE_ROLES]},
                )
            self._role_adapters[role] = adapter

    # ---- ModelAdapterProtocol -------------------------------------------
    @property
    def provider(self) -> str:
        return str(getattr(self._default, "provider", "unknown"))

    @property
    def adapter_version(self) -> str:
        return str(getattr(self._default, "adapter_version", "unknown"))

    @property
    def model_name(self) -> str:
        """默认（planner 未映射时）模型名；路由后由 :meth:`model_for` 决定。"""
        return str(getattr(self._default, "model_name", "unknown"))

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self.adapter_for(request.role).complete(request)

    # ---- 路由视图 --------------------------------------------------------
    def adapter_for(self, role: ModelRole | str) -> ModelAdapterProtocol:
        """返回该角色应使用的适配器（未映射 → 默认适配器）。"""
        return self._role_adapters.get(coerce_role(role), self._default)

    def model_for(self, role: ModelRole | str) -> str:
        """返回该角色实际使用的模型名（供 usage/报告标注，避免误记）。"""
        return str(getattr(self.adapter_for(role), "model_name", "unknown"))

    def mapped_roles(self) -> tuple[ModelRole, ...]:
        return tuple(sorted(self._role_adapters, key=lambda item: item.value))

    def describe(self) -> dict[str, Any]:
        """装配自检视图：角色 → 模型名（不含任何密钥）。"""
        view = {role.value: self.model_for(role) for role in self.mapped_roles()}
        view.setdefault(ModelRole.PLANNER.value, self.model_for(ModelRole.PLANNER))
        return view


def coerce_role(role: ModelRole | str) -> ModelRole:
    if isinstance(role, ModelRole):
        return role
    try:
        return ModelRole(str(role))
    except ValueError:
        raise ConfigError(
            f"未知模型角色：{role!r}",
            details={"supported_roles": [item.value for item in ModelRole]},
        ) from None


__all__ = ["ROUTABLE_ROLES", "UNSUPPORTED_ROLES", "ModelRoleRouter", "coerce_role"]
