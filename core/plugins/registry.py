"""业务域插件注册表：装配期校验 + MCP 工具注册表装配 + 工具命名约定。

命名约定统一为 ``<domain>_<action>``，让 Supervisor / Triage / Critic 可以用
``f"{domain}_probe_readonly"`` 这类确定性名字调用任意业务域，无需为每个域写分支。
"""

from __future__ import annotations

from typing import Any, Iterable

from core.orchestrator.tool_client import McpToolClient, McpToolRegistry, ToolSpec
from core.plugins.contracts import (
    DomainPlugin,
    PluginContractError,
    TargetAsset,
    assert_plugin_complete,
)


def probe_tool_name(domain: str) -> str:
    return f"{domain}_probe_readonly"


def fix_tool_name(domain: str) -> str:
    return f"{domain}_fix_generate"


def sandbox_tool_name(domain: str) -> str:
    return f"{domain}_sandbox_verify"


def evidence_tool_name(domain: str) -> str:
    return f"{domain}_evidence_masked"


def preflight_tool_name(domain: str) -> str:
    return f"{domain}_preflight_readonly"


class DomainPluginRegistry:
    """按域注册插件；装配期即校验四大接口完整性（缺一即 fail closed）。"""

    def __init__(self) -> None:
        self._plugins: dict[str, DomainPlugin] = {}

    def register(self, plugin: DomainPlugin) -> "DomainPluginRegistry":
        assert_plugin_complete(plugin)
        domain = plugin.domain_id()
        if domain in self._plugins:
            raise PluginContractError(f"业务域插件重复注册: {domain}")
        self._plugins[domain] = plugin
        return self

    def get(self, domain: str) -> DomainPlugin | None:
        return self._plugins.get(domain)

    def require(self, domain: str) -> DomainPlugin:
        plugin = self._plugins.get(domain)
        if plugin is None:
            raise PluginContractError(f"未装配业务域插件: {domain}")
        return plugin

    def domains(self) -> tuple[str, ...]:
        return tuple(self._plugins)

    def plugins(self) -> tuple[DomainPlugin, ...]:
        return tuple(self._plugins.values())

    def register_assets(self, assets: Iterable[TargetAsset]) -> None:
        materialized = list(assets)
        for plugin in self._plugins.values():
            hook = getattr(plugin, "register_assets", None)
            if callable(hook):
                hook(materialized)

    def tool_specs(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for plugin in self._plugins.values():
            provider = getattr(plugin, "tool_specs", None)
            if not callable(provider):
                raise PluginContractError(f"插件 {plugin.plugin_id()} 未暴露 tool_specs()")
            specs.extend(provider())
        return specs

    def build_tool_registry(self) -> McpToolRegistry:
        registry = McpToolRegistry()
        for plugin in self._plugins.values():
            provider = getattr(plugin, "tool_specs", None)
            handlers = getattr(plugin, "tool_handlers", None)
            if not callable(provider) or not callable(handlers):
                raise PluginContractError(f"插件 {plugin.plugin_id()} 未暴露工具集")
            handler_map = handlers()
            for spec in provider():
                handler = handler_map.get(spec.name)
                if handler is None:
                    raise PluginContractError(f"工具 {spec.name} 缺少 handler")
                registry.register(spec, handler)
        return registry

    def build_tool_client(self, *, audit: Any | None = None, actor_role: str = "SEC") -> McpToolClient:
        return McpToolClient(self.build_tool_registry(), audit=audit, actor_role=actor_role)

    def describe(self) -> dict[str, Any]:
        """瘦桌面端可见的插件矩阵描述（不含任何实现细节或凭据）。"""
        return {
            domain: {
                "plugin_id": plugin.plugin_id(),
                "capabilities": list(plugin.capabilities()),
                "probe": type(plugin.probe()).__name__,
                "masker": type(plugin.masker()).__name__,
                "harness": type(plugin.harness()).__name__,
                "executor": type(plugin.executor()).__name__,
            }
            for domain, plugin in self._plugins.items()
        }


__all__ = [
    "DomainPluginRegistry",
    "evidence_tool_name",
    "fix_tool_name",
    "preflight_tool_name",
    "probe_tool_name",
    "sandbox_tool_name",
]
