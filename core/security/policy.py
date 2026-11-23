"""W05 Risk Resolver 与 Policy Engine。

约束：
- 风险由服务端工具契约、环境事实与操作类别共同解析；
- 模型风险提示只能抬高风险，不能降低，非法提示忽略并记录；
- 缺策略/未知操作 fail closed；W05 不开放写操作执行；
- 只读 Principal.roles / Principal.permissions 与 ToolDefinition，不接受客户端覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.errors import ErrorCode
from core.security.models import (
    EnvironmentContext,
    PolicyDecision,
    Principal,
    RiskEvaluation,
    RiskLevel,
    max_risk,
    risk_rank,
)
from core.security.roles import Permission, Role
from core.tools.contracts import ToolDefinition

_RISK_LEVELS: tuple[RiskLevel, ...] = ("R0", "R1", "R2", "R3", "R4")

_OPERATION_MIN_RISK: dict[str, RiskLevel] = {
    "observe": "R0",
    "diagnose": "R0",
    "experiment": "R1",
    "repair": "R2",
}

_SIDE_EFFECT_MIN_RISK: dict[str, RiskLevel] = {
    "none": "R0",
    "read": "R0",
    "write": "R2",
}


@dataclass(frozen=True)
class RiskResolution:
    """风险解析的可解释中间结果，供 PolicyEngine 判定与审计使用。"""

    effective_risk: RiskLevel
    factors: tuple[str, ...]
    notes: tuple[str, ...]
    model_hint: RiskLevel | None
    model_hint_accepted: bool
    operation_known: bool
    side_effect_known: bool

    def to_evaluation(self) -> RiskEvaluation:
        return RiskEvaluation(
            effective_risk=self.effective_risk,
            factors=self.factors,
            model_hint=self.model_hint,
            model_hint_accepted=self.model_hint_accepted,
            notes=self.notes,
        )


class RiskResolver:
    """服务端风险解析器；模型提示仅可作为风险下限的抬升信号。"""

    def resolve_risk(
        self,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        *,
        model_risk_hint: RiskLevel | None = None,
    ) -> RiskResolution:
        factors: list[str] = []
        notes: list[str] = []

        effective: RiskLevel = max_risk(definition.risk_level, definition.risk_floor)
        factors.append(f"definition.risk_level={definition.risk_level}")
        if definition.risk_floor is not None:
            factors.append(f"risk_floor={definition.risk_floor}")

        operation_floor = _OPERATION_MIN_RISK.get(definition.operation_class)
        operation_known = operation_floor is not None
        if operation_floor is None:
            notes.append(
                f"未知 operation_class：{definition.operation_class}，按 fail closed 处理"
            )
        else:
            factors.append(f"operation_class={definition.operation_class}")
            effective = max_risk(effective, operation_floor)

        side_effect_floor = _SIDE_EFFECT_MIN_RISK.get(definition.side_effect)
        side_effect_known = side_effect_floor is not None
        if side_effect_floor is None:
            notes.append(
                f"未知 side_effect：{definition.side_effect}，按 fail closed 处理"
            )
        else:
            factors.append(f"side_effect={definition.side_effect}")
            effective = max_risk(effective, side_effect_floor)

        if environment.tier == "PROD":
            factors.append("environment_tier=PROD")
            effective = max_risk(effective, "R2")

        hint: RiskLevel | None = None
        hint_accepted = False
        if model_risk_hint is not None:
            if model_risk_hint not in _RISK_LEVELS:
                notes.append(f"模型风险提示非法，已忽略：{model_risk_hint!r}")
            else:
                hint = model_risk_hint
                if risk_rank(hint) > risk_rank(effective):
                    effective = hint
                    hint_accepted = True
                    factors.append(f"model_risk_hint={hint}")
                elif risk_rank(hint) == risk_rank(effective):
                    hint_accepted = True
                    factors.append(f"model_risk_hint={hint}")
                else:
                    notes.append(
                        f"模型风险提示 {hint} 低于解析风险 {effective}，"
                        "已忽略（只升不降）"
                    )

        return RiskResolution(
            effective_risk=effective,
            factors=tuple(factors),
            notes=tuple(notes),
            model_hint=hint,
            model_hint_accepted=hint_accepted,
            operation_known=operation_known,
            side_effect_known=side_effect_known,
        )


class PolicyEngine:
    """唯一策略出口：风险解析 + RBAC 授权，缺策略默认拒绝。"""

    def __init__(self, policy_version: str = "w05-v1") -> None:
        self.policy_version = policy_version
        self.risk_resolver = RiskResolver()

    def decide(
        self,
        principal: Principal,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        *,
        model_risk_hint: RiskLevel | None = None,
        target_objects: list[str] | None = None,
    ) -> PolicyDecision:
        details: dict[str, Any] = {
            "tool_name": definition.name if definition is not None else None,
            "tool_version": definition.version if definition is not None else None,
            "environment_id": environment.environment_id,
            "tenant_id": principal.tenant_id,
        }
        if target_objects is not None:
            details["target_objects"] = list(target_objects)

        if definition is None:
            risk = RiskEvaluation(
                effective_risk="R4",
                factors=("definition_missing",),
                notes=("缺少工具策略定义，fail closed",),
            )
            return self._deny(
                reason="POLICY_MISSING",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )

        resolution = self.risk_resolver.resolve_risk(
            definition,
            environment,
            model_risk_hint=model_risk_hint,
        )
        risk = resolution.to_evaluation()

        if not resolution.operation_known:
            return self._deny(
                reason="UNKNOWN_OPERATION_CLASS",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )
        if not resolution.side_effect_known:
            return self._deny(
                reason="UNKNOWN_SIDE_EFFECT",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )

        if environment.tier == "PROD":
            return self._deny(
                reason="PROD_TARGET_NOT_ALLOWED",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )

        allowed_environments = definition.allowed_environments
        if "*" not in allowed_environments and environment.tier not in allowed_environments:
            return self._deny(
                reason="ENVIRONMENT_NOT_ALLOWED",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )

        if risk.effective_risk == "R4":
            return self._deny(
                reason="R4_NOT_EXECUTABLE",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )
        if risk.effective_risk == "R3":
            return self._deny(
                reason="R3_AGENT_EXECUTION_FORBIDDEN",
                risk=risk,
                details=details,
                deny_code=ErrorCode.POLICY_DENIED,
            )
        if risk.effective_risk == "R2":
            # W06：策略层只声明"需要审批"，**绝不**因审批存在而放行。
            # 审批在执行链唯一入口（ToolGateway 第 8b 步）以一次性 grant 消费，
            # 因此这里仍然 allowed=False，且 deny_code 改为稳定的 APPROVAL_REQUIRED。
            return self._deny(
                reason="R2_REQUIRES_APPROVAL",
                risk=risk,
                details=details,
                deny_code=ErrorCode.APPROVAL_REQUIRED,
                required_approval=True,
            )

        if not principal.has_permission(Permission.ADAPTER_INVOKE):
            return self._deny(
                reason="MISSING_ADAPTER_INVOKE",
                risk=risk,
                details=details,
                deny_code=ErrorCode.FORBIDDEN,
            )

        if risk.effective_risk == "R1":
            if environment.tier != "LAB":
                return self._deny(
                    reason="R1_LAB_ONLY",
                    risk=risk,
                    details=details,
                    deny_code=ErrorCode.FORBIDDEN,
                )
            if not (
                principal.has_role(Role.OPERATOR)
                or principal.has_role(Role.ADMINISTRATOR)
            ):
                return self._deny(
                    reason="R1_REQUIRES_OPERATOR_ROLE",
                    risk=risk,
                    details=details,
                    deny_code=ErrorCode.FORBIDDEN,
                )

        return PolicyDecision(
            allowed=True,
            effective_risk=risk.effective_risk,
            reason="ALLOWED",
            policy_version=self.policy_version,
            required_approval=False,
            deny_code=None,
            risk=risk,
            details=details,
        )

    def _deny(
        self,
        *,
        reason: str,
        risk: RiskEvaluation,
        details: dict[str, Any],
        deny_code: ErrorCode,
        required_approval: bool = False,
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            effective_risk=risk.effective_risk,
            reason=reason,
            policy_version=self.policy_version,
            required_approval=required_approval,
            deny_code=deny_code.value,
            risk=risk,
            details=details,
        )


__all__ = ["PolicyEngine", "RiskResolver", "RiskResolution"]
