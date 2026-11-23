"""第三方插件 SDK 契约（P4-T2）与跨栈 SARIF v2.1.0 报告器。

两件事：

1. **插件发布契约**：外部团队按 ``PluginManifest`` 声明元数据并实现四大接口，
   :func:`build_third_party_plugin` 在装配期完成 manifest 校验 + 接口完整性校验，
   不合格直接 fail closed（不允许"半成品插件"进生产编排）。
2. **跨栈报告**：同一份 SARIF 里同时容纳 ``db://<asset>`` 与
   ``file://<path>#L<line>`` 两种 location，配合 ``crossStack`` 属性把
   "代码行 -> 数据库资产"的联动关系带给 SIEM / SOAR / CI-CD。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.plugins.contracts import (
    KNOWN_DOMAINS,
    DomainMasker,
    DomainPlugin,
    DomainProbe,
    DomainRollbackExecutor,
    DomainSandboxHarness,
    ProbeFinding,
    RemediationProposal,
    assert_plugin_complete,
)

CONTRACTS_VERSION = "3.0.0"

_MANIFEST_ID_RE = re.compile(r"^plugin\.[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_FILE_LOCATION_RE = re.compile(r"^file://(?P<path>[^#]+)(?:#L(?P<line>\d+))?$")


class PluginManifestError(ValueError):
    """插件 manifest 不合规（命名 / 版本 / 域 / 能力声明）。"""


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    domain_id: str
    name: str
    version: str
    vendor: str
    capabilities: tuple[str, ...]
    contracts_version: str = CONTRACTS_VERSION
    entrypoint: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "domain_id": self.domain_id,
            "name": self.name,
            "version": self.version,
            "vendor": self.vendor,
            "capabilities": list(self.capabilities),
            "contracts_version": self.contracts_version,
            "entrypoint": self.entrypoint,
            "description": self.description,
        }


def validate_manifest(manifest: PluginManifest) -> None:
    problems: list[str] = []
    if not _MANIFEST_ID_RE.match(manifest.plugin_id or ""):
        problems.append("plugin_id 需匹配 plugin.<lower_snake>")
    if manifest.domain_id not in KNOWN_DOMAINS:
        problems.append(f"domain_id 必须是 {list(KNOWN_DOMAINS)} 之一")
    if not _SEMVER_RE.match(manifest.version or ""):
        problems.append("version 需为 x.y.z")
    if not (manifest.vendor or "").strip():
        problems.append("vendor 不能为空")
    if not manifest.capabilities:
        problems.append("capabilities 不能为空")
    elif any(":" not in capability for capability in manifest.capabilities):
        problems.append("capabilities 每项需形如 probe:readonly")
    manifest_major = (manifest.contracts_version or "").split(".")[0]
    if manifest_major != CONTRACTS_VERSION.split(".")[0]:
        problems.append(f"contracts_version 主版本需为 {CONTRACTS_VERSION.split('.')[0]}.x")
    if problems:
        raise PluginManifestError("插件 manifest 不合规：" + "；".join(problems))


class ThirdPartyPlugin:
    """第三方四接口实现的 DomainPlugin 适配器（保留 manifest 溯源）。"""

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        probe: DomainProbe,
        masker: DomainMasker,
        harness: DomainSandboxHarness,
        executor: DomainRollbackExecutor,
    ) -> None:
        self._manifest = manifest
        self._probe = probe
        self._masker = masker
        self._harness = harness
        self._executor = executor

    def plugin_id(self) -> str:
        return self._manifest.plugin_id

    def domain_id(self) -> str:
        return self._manifest.domain_id

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self._manifest.capabilities)

    def probe(self) -> DomainProbe:
        return self._probe

    def masker(self) -> DomainMasker:
        return self._masker

    def harness(self) -> DomainSandboxHarness:
        return self._harness

    def executor(self) -> DomainRollbackExecutor:
        return self._executor

    def manifest(self) -> dict[str, Any]:
        return self._manifest.to_dict()


def build_third_party_plugin(
    manifest: PluginManifest,
    *,
    probe: DomainProbe,
    masker: DomainMasker,
    harness: DomainSandboxHarness,
    executor: DomainRollbackExecutor,
) -> ThirdPartyPlugin:
    """装配期双重校验：manifest 合规 + 四大接口完整。"""
    validate_manifest(manifest)
    plugin = ThirdPartyPlugin(
        manifest,
        probe=probe,
        masker=masker,
        harness=harness,
        executor=executor,
    )
    assert_plugin_complete(plugin)
    return plugin


def describe_plugin(plugin: DomainPlugin) -> dict[str, Any]:
    """插件溯源描述（供插件市场/审计展示，不含实现细节）。"""
    manifest_getter = getattr(plugin, "manifest", None)
    manifest = manifest_getter() if callable(manifest_getter) else {}
    return {
        "plugin_id": plugin.plugin_id(),
        "domain_id": plugin.domain_id(),
        "capabilities": list(plugin.capabilities()),
        "contracts_version": manifest.get("contracts_version", CONTRACTS_VERSION),
        "manifest": manifest,
    }


class CrossStackSarifReporter:
    """SARIF v2.1.0：同一报告内同时承载 DB 资产与源码行号。"""

    SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
    SARIF_VERSION = "2.1.0"

    def __init__(
        self,
        *,
        tool_name: str = "AegisForge SecOps",
        tool_version: str = "3.0.0",
        information_uri: str = "https://aegisforge.local/secops",
    ) -> None:
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.information_uri = information_uri

    def build(
        self,
        *,
        findings: Iterable[ProbeFinding],
        proposals: Mapping[str, RemediationProposal] | None = None,
        run_properties: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        proposals = proposals or {}
        rules: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []

        for finding in findings:
            if finding.suppressed_as_noise:
                continue
            proposal = proposals.get(finding.finding_id)
            rule = rules.setdefault(
                finding.rule_id,
                {
                    "id": finding.rule_id,
                    "name": finding.category,
                    "shortDescription": {"text": finding.title},
                    "properties": {"domain": finding.domain, "baseCvss": finding.base_cvss},
                },
            )
            rule.setdefault("properties", {})

            uri, line = self.split_location(finding.location)
            physical: dict[str, Any] = {"artifactLocation": {"uri": uri}}
            if line is not None:
                physical["region"] = {"startLine": line}

            properties: dict[str, Any] = {
                "domain": finding.domain,
                "assetId": finding.asset_id,
                "category": finding.category,
                "baseCvss": finding.base_cvss,
                "cveId": finding.cve_id,
                "tags": list(finding.tags),
            }
            if proposal is not None:
                properties.update(
                    {
                        "approvalDigest": proposal.approval_digest(),
                        "rollbackArmed": proposal.rollback.is_armed(),
                        "selfCorrectionRound": proposal.round,
                        "crossStack": [dict(item) for item in proposal.code_refs],
                    }
                )

            results.append(
                {
                    "ruleId": finding.rule_id,
                    "level": "error" if finding.base_cvss >= 7.0 else "warning",
                    "message": {"text": f"{finding.title} ({finding.asset_id})"},
                    "locations": [{"physicalLocation": physical}],
                    "properties": properties,
                }
            )

        return {
            "$schema": self.SCHEMA_URI,
            "version": self.SARIF_VERSION,
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": self.tool_name,
                            "informationUri": self.information_uri,
                            "version": self.tool_version,
                            "rules": list(rules.values()),
                        }
                    },
                    "results": results,
                    "properties": dict(run_properties or {}),
                }
            ],
        }

    def write(self, path: Path, **kwargs: Any) -> dict[str, Any]:
        sarif = self.build(**kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sarif, ensure_ascii=False, indent=2), encoding="utf-8")
        return sarif

    @staticmethod
    def split_location(location: str) -> tuple[str, int | None]:
        """把内部 location 拆成 SARIF 的 (uri, startLine)。"""
        if not location:
            return "unknown://", None
        match = _FILE_LOCATION_RE.match(location)
        if match:
            line = match.group("line")
            return match.group("path"), int(line) if line else None
        return location, None


__all__ = [
    "CONTRACTS_VERSION",
    "CrossStackSarifReporter",
    "PluginManifest",
    "PluginManifestError",
    "ThirdPartyPlugin",
    "build_third_party_plugin",
    "describe_plugin",
    "validate_manifest",
]
