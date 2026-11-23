"""V3.0 业务域插件标准契约（Domain Plugin SDK）。

设计依据：``docs/v3-multi-agent-secops-spec.json`` →
``domain_plugin_sdk_standard.four_mandatory_interfaces``。

四大强制接口各只做一件事，且全部为**只读或受控**语义：

1. :class:`DomainProbe`            低侵入只读限流探测（``token_bucket_qps`` + ``cpu_breaker``）
2. :class:`DomainMasker`           格式感知双向可逆脱敏器（统一注册至多模态脱敏网关）
3. :class:`DomainSandboxHarness`   隔离沙箱 PoC 复现 + 修复 Dry-Run + 回滚可逆性验证
4. :class:`DomainRollbackExecutor` 带超时熔断与原子/声明式回退的生产执行器

工程红线（与 ``engineering_invariants`` 对齐）：

- 契约只描述能力，**不持有任何凭据**；:class:`TargetAsset` 仅承载已脱敏的定位描述。
- 插件装配期即做完整性校验（:func:`assert_plugin_complete`），缺任一接口 fail closed。
- 跨 Agent 传递的数据模型一律为不可变 dataclass，禁止共享可变状态。
- 任何修复方案都必须携带可逆回滚计划与超时熔断参数（模块 5/6 的 V2.0 约束在 V3 继续生效）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from core.platform.ten_modules import AssetCredentialRecord, TokenBucketRateLimiter

# ---------------------------------------------------------------------------
# 领域标识
# ---------------------------------------------------------------------------
DOMAIN_DATABASE = "database"
DOMAIN_K8S = "k8s"
DOMAIN_HOST = "host"
DOMAIN_APPSEC = "appsec"

KNOWN_DOMAINS: tuple[str, ...] = (DOMAIN_DATABASE, DOMAIN_K8S, DOMAIN_HOST, DOMAIN_APPSEC)

#: 回滚计划类型：SQL 语句 / K8s 声明式清单 / Ansible Playbook / 代码 Patch。
ROLLBACK_KIND_SQL = "SQL"
ROLLBACK_KIND_MANIFEST = "MANIFEST"
ROLLBACK_KIND_PLAYBOOK = "PLAYBOOK"
ROLLBACK_KIND_PATCH = "PATCH"

#: 生产只读预检拦截线：超过该毫秒数的阻塞一律视为会引发锁等待放大。
PROD_BLOCK_BUDGET_MS = 30_000

#: 沙箱业务兼容性阈值：低于该分数 Critic 直接打回。
COMPATIBILITY_THRESHOLD = 0.95


class PluginContractError(ValueError):
    """插件未满足四大接口契约，装配期直接失败。"""


# ---------------------------------------------------------------------------
# 基础数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TargetAsset:
    """跨域统一资产视图。

    ``endpoint`` 必须是**已脱敏**的定位描述（如 ``10.x.x.19:3306`` / ``ns://prod/api``），
    真实连接串与凭据永远由 M02 的 KMS/Vault 租约在控制面内部持有。
    """

    asset_id: str
    domain: str
    engine: str = ""
    version: str = ""
    business_tier: str = "INTERNAL_BI"      # CORE_TRADE / HIGH_FINANCE / INTERNAL_BI
    network_exposure: str = "INTERNAL_VPC"  # PUBLIC_DMZ / INTERNAL_VPC
    pii_density: str = "LOW"                # HIGH / MEDIUM / LOW
    endpoint: str = ""
    labels: tuple[tuple[str, str], ...] = ()

    def label(self, key: str, default: str = "") -> str:
        for k, v in self.labels:
            if k == key:
                return v
        return default

    def public_view(self) -> dict[str, Any]:
        """瘦桌面端可见视图：不含任何凭据字段。"""
        return {
            "asset_id": self.asset_id,
            "domain": self.domain,
            "engine": self.engine,
            "version": self.version,
            "business_tier": self.business_tier,
            "network_exposure": self.network_exposure,
            "pii_density": self.pii_density,
            "endpoint": self.endpoint,
            "labels": dict(self.labels),
        }


def proposal_from_dict(data: Mapping[str, Any]) -> RemediationProposal:
    """从状态快照还原不可变提案（执行器与桌面端预览共用同一口径）。"""
    rollback_raw = dict(data.get("rollback") or {})
    impact_raw = dict(data.get("impact") or {})
    return RemediationProposal(
        proposal_id=str(data["proposal_id"]),
        finding_id=str(data["finding_id"]),
        asset_id=str(data["asset_id"]),
        domain=str(data["domain"]),
        plugin_id=str(data.get("plugin_id", "")),
        mitigation=str(data.get("mitigation", "")),
        standard_fix=str(data.get("standard_fix", "")),
        upgrade_path=str(data.get("upgrade_path", "")),
        rollback=RollbackPlan(
            kind=str(rollback_raw.get("kind", ROLLBACK_KIND_SQL)),
            payload=str(rollback_raw.get("payload", "")),
            timeout_sec=int(rollback_raw.get("timeout_sec", 0)),
            atomic=bool(rollback_raw.get("atomic", False)),
            reversible=bool(rollback_raw.get("reversible", False)),
            note=str(rollback_raw.get("note", "")),
        ),
        impact=ImpactAssessment(
            lock_impact_level=str(impact_raw.get("lock_impact_level", "NONE")),
            requires_restart=bool(impact_raw.get("requires_restart", False)),
            estimated_block_ms=int(impact_raw.get("estimated_block_ms", 0)),
            blast_radius_rows=int(impact_raw.get("blast_radius_rows", 0)),
            change_window=str(impact_raw.get("change_window", "")),
            deferred=bool(impact_raw.get("deferred", False)),
        ),
        confidence=float(data.get("confidence", 0.0)),
        round=int(data.get("round", 1)),
        feedback_applied=tuple(str(item) for item in data.get("feedback_applied", ())),
        code_refs=tuple(dict(item) for item in data.get("code_refs", ())),
        notes=tuple(str(item) for item in data.get("notes", ())),
    )


def scoring_record(asset: TargetAsset) -> AssetCredentialRecord:
    """把域资产投影为 M04 风险定级所需的凭据记录。

    **零凭据**：``encrypted_dsn_token`` 恒为空串，``kms_key_id`` 只是逻辑标识，
    仅用于复用 V2.0 ``ContextualRiskScorer`` 的入参签名，不携带任何密钥材料。
    """
    return AssetCredentialRecord(
        asset_id=asset.asset_id,
        db_engine=(asset.engine or "postgresql").lower(),
        engine_version=asset.version or "16.2",
        business_tier=asset.business_tier,
        network_exposure=asset.network_exposure,
        pii_density=asset.pii_density,
        kms_key_id=f"kms://vault/secops/{asset.asset_id}",
        encrypted_dsn_token="",
    )


@dataclass
class ProbeBudget:
    """低侵入探测预算：令牌桶限流 + CPU 熔断降速。

    与 M03 ``MultiDimVulnScanner`` 使用同一个 :class:`TokenBucketRateLimiter` 实现，
    保证"插件化"不改变既有探测节流口径（向后兼容）。
    """

    token_bucket_qps: int = 20
    burst: int | None = None
    cpu_breaker_pct: float = 1.8
    throttle_state: str = "NORMAL"
    breaker_tripped: bool = False
    consumed: int = 0
    denied: int = 0
    _limiter: TokenBucketRateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.token_bucket_qps <= 0:
            raise ValueError("token_bucket_qps 必须为正整数")
        self._limiter = TokenBucketRateLimiter(max_qps=self.token_bucket_qps, burst=self.burst)

    def allow(self, cost: int = 1, *, now: float | None = None) -> bool:
        allowed = self._limiter.consume(cost, now=now)
        if allowed:
            self.consumed += cost
        else:
            self.denied += 1
        return allowed

    def observe_cpu_delta(self, delta_pct: float) -> str:
        """目标实例 CPU 增量越线即降速一半，避免影响业务。"""
        if delta_pct >= self.cpu_breaker_pct:
            self.breaker_tripped = True
            self.throttle_state = "DEGRADED"
            self._limiter.max_qps = max(1, int(self.token_bucket_qps * 0.5))
        else:
            self.breaker_tripped = False
            self.throttle_state = "NORMAL"
            self._limiter.max_qps = self.token_bucket_qps
        return self.throttle_state

    def snapshot(self) -> dict[str, Any]:
        return {
            "token_bucket_qps": self.token_bucket_qps,
            "effective_qps": self._limiter.max_qps,
            "cpu_breaker_pct": self.cpu_breaker_pct,
            "throttle_state": self.throttle_state,
            "breaker_tripped": self.breaker_tripped,
            "consumed": self.consumed,
            "denied": self.denied,
            "read_only": True,
        }


@dataclass(frozen=True)
class ProbeFinding:
    """领域探测发现项（脱敏前的原始证据仅存在于控制面内存）。"""

    finding_id: str
    asset_id: str
    domain: str
    category: str
    rule_id: str
    title: str
    evidence: str
    base_cvss: float
    cve_id: str | None = None
    location: str = ""
    tags: tuple[str, ...] = ()
    suppressed_as_noise: bool = False

    def public_view(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "asset_id": self.asset_id,
            "domain": self.domain,
            "category": self.category,
            "rule_id": self.rule_id,
            "title": self.title,
            "base_cvss": self.base_cvss,
            "cve_id": self.cve_id,
            "location": self.location,
            "tags": list(self.tags),
            "suppressed_as_noise": self.suppressed_as_noise,
        }


@dataclass(frozen=True)
class ProbeReport:
    asset_id: str
    domain: str
    findings: tuple[ProbeFinding, ...]
    budget: dict[str, Any]
    probe_tool: str = ""
    read_only: bool = True


@dataclass(frozen=True)
class RollbackPlan:
    """强制配套的回滚计划：无有效 payload 或无限时熔断即视为不可逆。"""

    kind: str
    payload: str
    timeout_sec: int = 3
    atomic: bool = True
    reversible: bool = True
    note: str = ""

    def is_armed(self) -> bool:
        return bool(self.payload.strip()) and self.timeout_sec > 0 and self.reversible

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": self.payload,
            "timeout_sec": self.timeout_sec,
            "atomic": self.atomic,
            "reversible": self.reversible,
            "armed": self.is_armed(),
            "note": self.note,
        }


@dataclass(frozen=True)
class ImpactAssessment:
    """变更影响评估：锁级别 / 是否重启 / 预估阻塞 / 爆炸半径 / 变更窗口。"""

    lock_impact_level: str = "NONE"     # NONE / METADATA_SHARE / TABLE_EXCLUSIVE
    requires_restart: bool = False
    estimated_block_ms: int = 0
    blast_radius_rows: int = 0
    change_window: str = ""             # "" / LOW_PEAK / MAINTENANCE
    deferred: bool = False

    def exceeds_block_budget(self) -> bool:
        return self.estimated_block_ms > PROD_BLOCK_BUDGET_MS


@dataclass(frozen=True)
class RemediationProposal:
    """领域专家 Agent 产出的「三层修复方案 + 强制 Rollback + 影响评估」。"""

    proposal_id: str
    finding_id: str
    asset_id: str
    domain: str
    plugin_id: str
    mitigation: str
    standard_fix: str
    upgrade_path: str
    rollback: RollbackPlan
    impact: ImpactAssessment
    confidence: float = 0.0
    round: int = 1
    feedback_applied: tuple[str, ...] = ()
    code_refs: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    def approval_digest(self) -> str:
        """HITL 审批绑定摘要：只覆盖"改什么 + 怎么退"，与轮次无关。"""
        raw = json.dumps(
            {
                "proposal_id": self.proposal_id,
                "finding_id": self.finding_id,
                "asset_id": self.asset_id,
                "standard_fix": self.standard_fix,
                "rollback": self.rollback.to_dict(),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "finding_id": self.finding_id,
            "asset_id": self.asset_id,
            "domain": self.domain,
            "plugin_id": self.plugin_id,
            "mitigation": self.mitigation,
            "standard_fix": self.standard_fix,
            "upgrade_path": self.upgrade_path,
            "rollback": self.rollback.to_dict(),
            "impact": {
                "lock_impact_level": self.impact.lock_impact_level,
                "requires_restart": self.impact.requires_restart,
                "estimated_block_ms": self.impact.estimated_block_ms,
                "blast_radius_rows": self.impact.blast_radius_rows,
                "change_window": self.impact.change_window,
                "deferred": self.impact.deferred,
            },
            "confidence": self.confidence,
            "round": self.round,
            "feedback_applied": list(self.feedback_applied),
            "code_refs": list(self.code_refs),
            "notes": list(self.notes),
            "approval_digest": self.approval_digest(),
        }


@dataclass(frozen=True)
class HarnessOutcome:
    """沙箱验证聚合结果：所有失败原因都以结构化码返回，供 Self-Correction 使用。"""

    proposal_id: str
    poc_reproduced: bool
    poc_blocked_after_fix: bool
    dry_run_passed: bool
    rollback_verified: bool
    compatibility_score: float
    sandbox_pool: str = ""
    logs: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    prod_preflight: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (
            self.poc_reproduced
            and self.poc_blocked_after_fix
            and self.dry_run_passed
            and self.rollback_verified
            and self.compatibility_score >= COMPATIBILITY_THRESHOLD
            and not self.failures
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "poc_reproduced": self.poc_reproduced,
            "poc_blocked_after_fix": self.poc_blocked_after_fix,
            "dry_run_passed": self.dry_run_passed,
            "rollback_verified": self.rollback_verified,
            "compatibility_score": self.compatibility_score,
            "sandbox_pool": self.sandbox_pool,
            "logs": list(self.logs),
            "failures": list(self.failures),
            "prod_preflight": dict(self.prod_preflight),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ExecutionReceipt:
    """生产执行回执：未拿到 HITL 审批摘要时一律 blocked，绝不带病执行。"""

    proposal_id: str
    executed: bool
    blocked: bool
    rollback_armed: bool
    timeout_sec: int
    detail: str
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "executed": self.executed,
            "blocked": self.blocked,
            "rollback_armed": self.rollback_armed,
            "timeout_sec": self.timeout_sec,
            "detail": self.detail,
            "kind": self.kind,
        }


# ---------------------------------------------------------------------------
# 四大强制接口（结构化类型，运行时仅校验方法存在）
# ---------------------------------------------------------------------------
@runtime_checkable
class DomainProbe(Protocol):
    """低侵入只读限流探测接口。"""

    def domain_id(self) -> str: ...

    def collect(self, asset: TargetAsset, *, budget: ProbeBudget | None = None) -> ProbeReport: ...


@runtime_checkable
class DomainMasker(Protocol):
    """格式感知双向可逆脱敏器接口。"""

    def domain_id(self) -> str: ...

    def modality(self) -> str: ...

    def detect(self, payload: str) -> float: ...

    def mask(self, payload: str) -> tuple[str, dict[str, str]]: ...

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str: ...

    def validate(self, payload: str) -> None: ...


@runtime_checkable
class DomainSandboxHarness(Protocol):
    """隔离沙箱 PoC 复现 + 修复 Dry-Run + 回滚验证接口。"""

    def domain_id(self) -> str: ...

    def evaluate(
        self,
        asset: TargetAsset,
        finding: ProbeFinding,
        proposal: RemediationProposal,
        *,
        probe: dict[str, Any] | None = None,
    ) -> HarnessOutcome: ...


@runtime_checkable
class DomainRollbackExecutor(Protocol):
    """带超时熔断与原子/声明式回退的生产执行器接口。"""

    def domain_id(self) -> str: ...

    def execute(
        self,
        asset: TargetAsset,
        proposal: RemediationProposal,
        *,
        approval_token: str = "",
    ) -> ExecutionReceipt: ...

    def rollback(self, asset: TargetAsset, proposal: RemediationProposal, *, reason: str = "") -> ExecutionReceipt: ...


@runtime_checkable
class DomainPlugin(Protocol):
    """业务域插件聚合契约：四大接口 + 能力声明。"""

    def plugin_id(self) -> str: ...

    def domain_id(self) -> str: ...

    def capabilities(self) -> tuple[str, ...]: ...

    def probe(self) -> DomainProbe: ...

    def masker(self) -> DomainMasker: ...

    def harness(self) -> DomainSandboxHarness: ...

    def executor(self) -> DomainRollbackExecutor: ...


_INTERFACE_GETTERS: tuple[tuple[str, str], ...] = (
    ("probe", "DomainProbe"),
    ("masker", "DomainMasker"),
    ("harness", "DomainSandboxHarness"),
    ("executor", "DomainRollbackExecutor"),
)


def assert_plugin_complete(plugin: DomainPlugin) -> None:
    """装配期完整性校验：缺任一接口、域标识不一致或能力为空即 fail closed。"""
    problems: list[str] = []
    plugin_domain = getattr(plugin, "domain_id", None)
    if not callable(plugin_domain):
        raise PluginContractError("插件缺少 domain_id() 实现")
    domain = plugin_domain()
    if not domain:
        problems.append("domain_id() 返回空")

    for getter, iface in _INTERFACE_GETTERS:
        accessor = getattr(plugin, getter, None)
        if not callable(accessor):
            problems.append(f"缺少 {getter}() 工厂方法")
            continue
        instance = accessor()
        if instance is None:
            problems.append(f"{getter}() 返回 None（接口 {iface} 未装配）")
            continue
        sub_domain = getattr(instance, "domain_id", None)
        if not callable(sub_domain):
            problems.append(f"{getter}() 未实现 domain_id()")
            continue
        if sub_domain() != domain:
            problems.append(f"{getter}().domain_id()={sub_domain()!r} 与插件域 {domain!r} 不一致")

    capabilities = getattr(plugin, "capabilities", None)
    if not callable(capabilities) or not capabilities():
        problems.append("capabilities() 为空")

    if problems:
        raise PluginContractError("插件契约不完整：" + "；".join(problems))


__all__ = [
    "COMPATIBILITY_THRESHOLD",
    "DOMAIN_APPSEC",
    "DOMAIN_DATABASE",
    "DOMAIN_HOST",
    "DOMAIN_K8S",
    "DomainMasker",
    "DomainPlugin",
    "DomainProbe",
    "DomainRollbackExecutor",
    "DomainSandboxHarness",
    "ExecutionReceipt",
    "HarnessOutcome",
    "ImpactAssessment",
    "KNOWN_DOMAINS",
    "PROD_BLOCK_BUDGET_MS",
    "PluginContractError",
    "ProbeBudget",
    "ProbeFinding",
    "ProbeReport",
    "ROLLBACK_KIND_MANIFEST",
    "ROLLBACK_KIND_PATCH",
    "ROLLBACK_KIND_PLAYBOOK",
    "ROLLBACK_KIND_SQL",
    "RemediationProposal",
    "RollbackPlan",
    "TargetAsset",
    "assert_plugin_complete",
    "proposal_from_dict",
    "scoring_record",
]
