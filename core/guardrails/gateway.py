"""通用 Agent 护栏客户端（``core.guardrails.gateway``）。

三层职责，全部**委托**既有实现，不重复造轮子：

1. 出站脱敏 → :meth:`MultimodalPrivacyGateway.mask`（Fernet 信封加密持久化映射）；
2. 入站还原 → :meth:`MultimodalPrivacyGateway.restore`（PII 令牌保持不可逆掩码，
   结构校验失败即 fail closed）；
3. 高危动作门控 → :class:`DefaultAgentSafetyPolicy` + 风险等级 HITL 判定。

审计链：每个决策都写入 ``_audit_chain``，条目包含 ``prev_hash`` 与 ``sha256``，
形成可外部复核的哈希链（``export_audit_chain`` / ``verify_audit_chain``）。
链上只记录规则编号、动作与载荷摘要，**不记录载荷原文**。
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.guardrails.policies import (
    BLOCKING_CATEGORIES,
    DefaultAgentSafetyPolicy,
)
from core.platform.ten_modules import MappingExpiredError, MappingNotFoundError
from core.privacy_gateway.multimodal import ModalityStructureError, MultimodalPrivacyGateway
from core.redaction.models import RedactionChannel

#: 触发条件不满足时的默认门控动作名（稳定字符串，便于外部告警规则匹配）。
ACTION_ALLOWED = "ALLOW"
ACTION_BLOCKED = "BLOCK"
ACTION_HITL_REQUIRED = "REQUIRE_HITL"
ACTION_RESTORED = "RESTORE"


class GuardrailError(RuntimeError):
    """护栏基类异常。"""


class GuardrailBlockedError(GuardrailError):
    """动作被策略阻断（未审批的高危工具调用 / 命中阻断类规则）。"""

    def __init__(self, message: str, *, decision: "GuardrailDecision | None" = None) -> None:
        super().__init__(message)
        self.decision = decision


class GuardrailRestoreError(GuardrailError):
    """入站还原失败：映射缺失/过期或载荷结构损坏，按 fail closed 处理。"""


@dataclass(frozen=True)
class GuardrailDecision:
    """一次护栏决策的不可变结果。"""

    allowed: bool
    action: str
    reason: str
    masked_payload: str
    requires_hitl: bool
    workflow_run_id: str
    audit_digest: str
    findings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    modality: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reason": self.reason,
            # masked_payload 已是脱敏文本；外部若不需要正文可只读 digest 字段
            "masked_payload": self.masked_payload,
            "requires_hitl": self.requires_hitl,
            "workflow_run_id": self.workflow_run_id,
            "audit_digest": self.audit_digest,
            "findings": [dict(item) for item in self.findings],
            "modality": self.modality,
        }


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentGuardrailClient:
    """框架无关的 Agent 护栏客户端：出站脱敏 + 入站还原 + 工具门控 + 哈希链审计。"""

    def __init__(
        self,
        gateway: MultimodalPrivacyGateway | None = None,
        policy: DefaultAgentSafetyPolicy | None = None,
        *,
        hitl_tiers: Sequence[str] = ("P0", "P1"),
    ) -> None:
        self._gateway = gateway or MultimodalPrivacyGateway()
        self._policy = policy or DefaultAgentSafetyPolicy()
        self._hitl_tiers = tuple(hitl_tiers)
        self._audit_chain: list[dict[str, Any]] = []
        self._pii_engine: Any | None = None

    # -- 访问器 --------------------------------------------------------
    @property
    def gateway(self) -> MultimodalPrivacyGateway:
        return self._gateway

    @property
    def policy(self) -> DefaultAgentSafetyPolicy:
        return self._policy

    # -- 出站 ----------------------------------------------------------
    def guard_outbound(
        self,
        payload: str,
        *,
        workflow_run_id: str,
        modality: str | None = None,
        ttl_seconds: int = 3600,
    ) -> GuardrailDecision:
        """外发前脱敏 + 策略判定；命中阻断类规则时 ``allowed=False``。"""
        if not isinstance(payload, str):
            raise GuardrailError("payload 必须是字符串")
        if not workflow_run_id:
            raise GuardrailError("workflow_run_id 不能为空")

        hits = self._policy.evaluate(payload)
        blocking = [hit for hit in hits if hit.category in BLOCKING_CATEGORIES]
        masked_text, chosen = self._mask(payload, modality, workflow_run_id, ttl_seconds)
        masked_text = self._mask_irreversible_pii(masked_text)
        findings: list[dict[str, Any]] = [hit.to_dict() for hit in hits]
        if any(hit.category == "SECRET_EXFIL" for hit in hits):
            masked_text = self._scrub_secret_values(masked_text)
            findings = [
                {**item, "masked": True} if item.get("category") == "SECRET_EXFIL" else item
                for item in findings
            ]

        requires_hitl = any(hit.requires_hitl for hit in hits)
        allowed = not blocking
        if blocking:
            action = ACTION_BLOCKED
            reason = "POLICY_VIOLATION:" + ",".join(sorted({hit.rule_id for hit in blocking}))
        else:
            action = ACTION_ALLOWED
            reason = "NO_BLOCKING_POLICY_HIT"

        return self._decide(
            allowed=allowed,
            action=action,
            reason=reason,
            masked_payload=masked_text,
            requires_hitl=requires_hitl,
            workflow_run_id=workflow_run_id,
            findings=findings,
            modality=chosen,
            event_type="GUARDRAIL_OUTBOUND",
        )

    # -- 入站 ----------------------------------------------------------
    def guard_inbound_restore(
        self,
        llm_output: str,
        *,
        workflow_run_id: str,
        destroy: bool = False,
    ) -> str:
        """还原业务标识符；PII 令牌保持 ``[REDACTED_PII]``，结构不合法即 fail closed。"""
        if not isinstance(llm_output, str):
            raise GuardrailRestoreError("llm_output 必须是字符串")
        try:
            outcome = self._gateway.restore(
                llm_output,
                workflow_run_id=workflow_run_id,
                destroy=destroy,
            )
        except (MappingNotFoundError, MappingExpiredError) as exc:
            raise GuardrailRestoreError(f"映射不可用：{type(exc).__name__}") from exc
        except (ModalityStructureError, ValueError) as exc:
            raise GuardrailRestoreError(f"入站载荷结构校验失败：{type(exc).__name__}") from exc

        self._append_audit(
            "GUARDRAIL_INBOUND_RESTORED",
            workflow_run_id,
            {
                "action": ACTION_RESTORED,
                "modality": outcome.modality,
                "token_count": outcome.token_count,
                "destroyed": outcome.destroyed,
                "payload_sha256": _sha256(llm_output),
            },
        )
        return outcome.restored_text

    # -- 工具门控 ------------------------------------------------------
    def guard_tool_execution(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        risk_tier: str,
        hitl_approved: bool = False,
        workflow_run_id: str,
        require_hitl_for: Sequence[str] | None = None,
    ) -> GuardrailDecision:
        """高危工具调用门控：P0/P1 未审批一律阻断并标记 ``requires_hitl``。"""
        if not workflow_run_id:
            raise GuardrailError("workflow_run_id 不能为空")
        tier = (risk_tier or "P2").upper()
        tiers = tuple(item.upper() for item in (require_hitl_for or self._hitl_tiers))

        hits = self._policy.evaluate_tool(tool_name, arguments, risk_tier=tier)
        blocking = [hit for hit in hits if hit.category in BLOCKING_CATEGORIES]
        needs_hitl = tier in tiers or any(hit.requires_hitl for hit in hits)

        if blocking:
            allowed, action, reason = False, ACTION_BLOCKED, "TOOL_POLICY_VIOLATION:" + ",".join(
                sorted({hit.rule_id for hit in blocking})
            )
        elif needs_hitl and not hitl_approved:
            allowed, action, reason = False, ACTION_HITL_REQUIRED, f"RISK_TIER_{tier}_REQUIRES_HITL"
        else:
            allowed, action, reason = True, ACTION_ALLOWED, f"RISK_TIER_{tier}_APPROVED"

        return self._decide(
            allowed=allowed,
            action=action,
            reason=reason,
            masked_payload="",
            requires_hitl=needs_hitl,
            workflow_run_id=workflow_run_id,
            findings=[hit.to_dict() for hit in hits],
            modality="",
            event_type="GUARDRAIL_TOOL",
            extra={"tool_name": tool_name, "risk_tier": tier, "hitl_approved": bool(hitl_approved)},
        )

    def guard_tool_result(
        self,
        tool_name: str,
        result: Any,
        *,
        workflow_run_id: str,
    ) -> GuardrailDecision:
        """工具回传结果再脱敏一次（避免工具把凭据带回给模型）。"""
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        decision = self.guard_outbound(text, workflow_run_id=workflow_run_id)
        self._append_audit(
            "GUARDRAIL_TOOL_RESULT",
            workflow_run_id,
            {"tool_name": tool_name, "allowed": decision.allowed, "audit_digest": decision.audit_digest},
        )
        return decision

    # -- 审计链 --------------------------------------------------------
    def export_audit_chain(self) -> list[dict[str, Any]]:
        """导出完整哈希链（深拷贝，供外部 SIEM / 合规检查复核）。"""
        return [dict(entry) for entry in self._audit_chain]

    def verify_audit_chain(self) -> bool:
        """重算哈希链，任一条被篡改即返回 False。"""
        previous = ""
        for entry in self._audit_chain:
            body = {
                "seq": entry["seq"],
                "event_type": entry["event_type"],
                "workflow_run_id": entry["workflow_run_id"],
                "detail": entry["detail"],
                "prev_hash": previous,
            }
            if entry.get("prev_hash", "") != previous:
                return False
            if entry.get("sha256", "") != _sha256(_canonical(body)):
                return False
            previous = entry["sha256"]
        return True

    def _append_audit(self, event_type: str, workflow_run_id: str, detail: Mapping[str, Any]) -> str:
        previous = self._audit_chain[-1]["sha256"] if self._audit_chain else ""
        seq = len(self._audit_chain) + 1
        body = {
            "seq": seq,
            "event_type": event_type,
            "workflow_run_id": workflow_run_id,
            "detail": dict(detail),
            "prev_hash": previous,
        }
        digest = _sha256(_canonical(body))
        self._audit_chain.append({**body, "sha256": digest, "recorded_at": _now()})
        return digest

    # -- 内部 ----------------------------------------------------------
    def _mask(
        self,
        payload: str,
        modality: str | None,
        workflow_run_id: str,
        ttl_seconds: int,
    ) -> tuple[str, str]:
        chosen = modality if modality and modality in self._gateway.modalities() else None
        try:
            outcome = self._gateway.mask(
                payload,
                modality=chosen,
                workflow_run_id=workflow_run_id,
                ttl_seconds=ttl_seconds,
            )
        except (ModalityStructureError, KeyError, ValueError) as exc:
            # 首选模态不适用时降级到文本模态；仍然失败则不做"原样外发"这种危险兜底。
            try:
                outcome = self._gateway.mask(
                    payload,
                    modality="text",
                    workflow_run_id=workflow_run_id,
                    ttl_seconds=ttl_seconds,
                )
            except (ModalityStructureError, KeyError, ValueError) as inner:
                raise GuardrailError(f"出站脱敏失败：{type(inner).__name__}") from exc
        return outcome.masked_text, outcome.modality

    def _scrub_secret_values(self, text: str) -> str:
        """把策略命中的凭据片段替换为固定占位符（不依赖任何外部检测规则）。"""
        scrubbed = text
        for rule in self._policy.rules():
            if rule.category != "SECRET_EXFIL":
                continue
            for pattern in rule.patterns:
                scrubbed = pattern.sub("[REDACTED_SECRET]", scrubbed)
        return scrubbed

    def _mask_irreversible_pii(self, text: str) -> str:
        """PII 不可逆掩码：复用 :class:`DataRedactionEngine` 的 ``TRAINING_SAMPLE`` 通道。

        为什么需要第二道：SQL 模态的 AST 脱敏只处理标识符，手机号/邮箱会原样留下。
        本层用既有引擎把它们替换成 ``<PHONE:hash>`` / ``<EMAIL:hash>`` 这类**不可还原**
        令牌（``INV-FAIL-CLOSED-PRIVACY``）。仅当确实检出 PII 时才改写，避免无谓破坏载荷。
        """
        if not text:
            return text
        engine = self._redactor_engine()
        try:
            if not engine.assert_only_pii_categories({"text": text}):
                return text
            masked, _result = engine.redact_text(text, channel=RedactionChannel.TRAINING_SAMPLE)
        except Exception as exc:  # noqa: BLE001 - PII 复扫失败必须 fail closed
            raise GuardrailError(f"PII 复扫失败：{type(exc).__name__}") from exc
        return masked

    def _redactor_engine(self) -> Any:
        if self._pii_engine is None:
            from core.redaction.engine import DataRedactionEngine

            self._pii_engine = DataRedactionEngine()
        return self._pii_engine

    def _decide(
        self,
        *,
        allowed: bool,
        action: str,
        reason: str,
        masked_payload: str,
        requires_hitl: bool,
        workflow_run_id: str,
        findings: Sequence[Mapping[str, Any]],
        modality: str,
        event_type: str,
        extra: Mapping[str, Any] | None = None,
    ) -> GuardrailDecision:
        detail = {
            "action": action,
            "allowed": bool(allowed),
            "reason": reason,
            "requires_hitl": bool(requires_hitl),
            "rule_ids": [str(item.get("rule_id", "")) for item in findings],
            "masked_payload_sha256": _sha256(masked_payload) if masked_payload else "",
            "payload_length": len(masked_payload),
            **dict(extra or {}),
        }
        digest = self._append_audit(event_type, workflow_run_id, detail)
        return GuardrailDecision(
            allowed=allowed,
            action=action,
            reason=reason,
            masked_payload=masked_payload,
            requires_hitl=requires_hitl,
            workflow_run_id=workflow_run_id,
            audit_digest=digest,
            findings=tuple(dict(item) for item in findings),
            modality=modality,
        )


# ---------------------------------------------------------------------------
# 默认客户端与装饰器
# ---------------------------------------------------------------------------
_default_client: AgentGuardrailClient | None = None


def default_client() -> AgentGuardrailClient:
    """进程级默认客户端（懒加载；外部项目无需手动构造即可用装饰器）。"""
    global _default_client
    if _default_client is None:
        _default_client = AgentGuardrailClient()
    return _default_client


def configure_default_client(client: AgentGuardrailClient | None) -> None:
    """替换进程级默认客户端（传 ``None`` 表示下次调用时重建）。"""
    global _default_client
    _default_client = client


def guard_tool_call(
    risk_tier: str = "P2",
    require_hitl_for: tuple[str, ...] = ("P0", "P1"),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰任意 Agent Tool 函数：自动做策略判定 + HITL 拦截。

    被装饰函数可通过 ``hitl_approved`` / ``workflow_run_id`` / ``_guardrail_client``
    三个关键字参数与护栏交互（这三个参数不会传给业务函数本身）。

    Raises:
        GuardrailBlockedError: 命中阻断类策略，或高危等级未获人工审批。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            client = kwargs.pop("_guardrail_client", None) or default_client()
            hitl_approved = bool(kwargs.pop("hitl_approved", False))
            workflow_run_id = str(kwargs.pop("workflow_run_id", "") or f"tool:{func.__name__}")
            call_args = dict(kwargs)
            if args:
                call_args["__positional__"] = [repr(item) for item in args]
            decision = client.guard_tool_execution(
                func.__name__,
                call_args,
                risk_tier=risk_tier,
                hitl_approved=hitl_approved,
                workflow_run_id=workflow_run_id,
                require_hitl_for=require_hitl_for,
            )
            if not decision.allowed:
                raise GuardrailBlockedError(
                    f"工具 {func.__name__} 已被护栏阻断：{decision.reason}",
                    decision=decision,
                )
            return func(*args, **kwargs)

        wrapper.__guardrail_risk_tier__ = risk_tier  # type: ignore[attr-defined]
        wrapper.__guardrail_require_hitl_for__ = tuple(require_hitl_for)  # type: ignore[attr-defined]
        return wrapper

    return decorator


def export_audit_chain(client: AgentGuardrailClient | None = None) -> list[dict[str, Any]]:
    """导出默认（或指定）客户端的哈希链审计事件。"""
    return (client or default_client()).export_audit_chain()


def verify_audit_chain(client: AgentGuardrailClient | None = None) -> bool:
    """校验默认（或指定）客户端的哈希链完整性。"""
    return (client or default_client()).verify_audit_chain()


__all__ = [
    "ACTION_ALLOWED",
    "ACTION_BLOCKED",
    "ACTION_HITL_REQUIRED",
    "ACTION_RESTORED",
    "AgentGuardrailClient",
    "GuardrailBlockedError",
    "GuardrailDecision",
    "GuardrailError",
    "GuardrailRestoreError",
    "configure_default_client",
    "default_client",
    "export_audit_chain",
    "guard_tool_call",
    "verify_audit_chain",
]
