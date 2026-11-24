"""通用 Agent 安全策略规则集（``core.guardrails.policies``）。

四类默认规则，覆盖"外发即泄密"与"越权即破坏"两条主线：

1. ``SECRET_EXFIL``       明文密钥/口令/令牌出现在出站载荷里；
2. ``PROMPT_INJECTION``   载荷里夹带"忽略既有指令 / 套取系统提示"这类提示注入；
3. ``DESTRUCTIVE_WRITE``  无 WHERE 的 DELETE、DROP/TRUNCATE TABLE 等不可逆写操作；
4. ``PATH_ESCAPE``        越界文件路径（路径穿越、系统敏感目录）。

规则是**保守**的：宁可漏报也不误报（误报会让 Agent 正常任务无法执行）。
因此这里只匹配高置信形态，命中后由调用方决定"阻断"还是"转人工审批"。

注意：本模块刻意不收录任何真实凭据格式常量（例如云厂商访问密钥前缀），
避免自身成为 Secret Scan 的命中源；密钥识别走"键名 + 足够长的取值"这一形态特征。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: 命中即阻断（不需要人工审批就有明确危害）。
BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    {"SECRET_EXFIL", "PROMPT_INJECTION", "DESTRUCTIVE_WRITE", "PATH_ESCAPE"}
)

#: 风险等级由高到低；``P0`` / ``P1`` 默认需要人工审批才能执行。
RISK_TIERS: tuple[str, ...] = ("P0", "P1", "P2", "P3")


@dataclass(frozen=True)
class PolicyRule:
    """一条安全策略：编号 + 分类 + 说明 + 检测形态 + 是否需要人工审批。"""

    rule_id: str
    category: str
    description: str
    patterns: tuple[re.Pattern[str], ...] = ()
    requires_hitl: bool = False

    def matches(self, payload: str) -> bool:
        if not payload:
            return False
        return any(pattern.search(payload) for pattern in self.patterns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "description": self.description,
            "requires_hitl": self.requires_hitl,
        }


@dataclass(frozen=True)
class PolicyFinding:
    """一次策略命中。"""

    rule_id: str
    category: str
    description: str
    requires_hitl: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "description": self.description,
            "requires_hitl": self.requires_hitl,
        }


#: 键名形态的凭据赋值：``api_key = "....."`` / ``password: ....`` 等。
_SECRET_ASSIGN = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
    r"\s*[=:]\s*[\"']?[A-Za-z0-9_./+=\-]{8,}"
)
#: DSN 形态：``scheme://user:password@host``（密码段不是占位符时命中）。
_DSN_CREDENTIAL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:(?!CHANGE_ME|\*{3})[^\s@]{4,}@")

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions?|rules?|prompts?)"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:the\s+)?(?:system|previous|above|prior)"),
    re.compile(r"(?i)reveal\s+(?:the\s+)?system\s+prompt"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|jailbroken|developer\s+mode)"),
    re.compile(r"忽略(?:以上|之前|前面|上面|此前).{0,8}(?:指令|规则|提示|设定)"),
    re.compile(r"(?:泄露|输出|打印).{0,4}(?:系统提示|system\s*prompt)", re.IGNORECASE),
)

_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bdrop\s+(?:table|database|schema)\b"),
    re.compile(r"(?i)\btruncate\s+table\b"),
    re.compile(r"(?i)\bdelete\s+from\s+[\w.\"`\[\]]+\s*(?:;|$)"),
    re.compile(r"(?i)\bgrant\s+all\s+privileges\b"),
)

_PATH_ESCAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[\s\"'=:(])(?:\.\./){2,}"),
    re.compile(r"(?i)(?:^|[\s\"'=:(])(?:/etc/(?:passwd|shadow|sudoers)|/root/\.ssh)"),
    re.compile(r"(?i)[a-z]:\\(?:windows\\system32|users\\[^\\\s]+\\.ssh)"),
)


def _default_rules() -> tuple[PolicyRule, ...]:
    return (
        PolicyRule(
            rule_id="GR-SEC-01",
            category="SECRET_EXFIL",
            description="出站载荷包含明文口令/令牌/密钥赋值",
            patterns=(_SECRET_ASSIGN, _DSN_CREDENTIAL),
        ),
        PolicyRule(
            rule_id="GR-INJ-01",
            category="PROMPT_INJECTION",
            description="载荷夹带提示注入或系统提示套取指令",
            patterns=_PROMPT_INJECTION_PATTERNS,
        ),
        PolicyRule(
            rule_id="GR-WRITE-01",
            category="DESTRUCTIVE_WRITE",
            description="不可逆写操作（DROP/TRUNCATE/无条件 DELETE/GRANT ALL）",
            patterns=_DESTRUCTIVE_PATTERNS,
            requires_hitl=True,
        ),
        PolicyRule(
            rule_id="GR-PATH-01",
            category="PATH_ESCAPE",
            description="越界文件路径（路径穿越或系统敏感目录）",
            patterns=_PATH_ESCAPE_PATTERNS,
            requires_hitl=True,
        ),
    )


class DefaultAgentSafetyPolicy:
    """默认策略集合：四类规则 + 工具调用风险分级。"""

    def __init__(self, rules: Sequence[PolicyRule] | None = None) -> None:
        self._rules: tuple[PolicyRule, ...] = tuple(rules) if rules is not None else _default_rules()

    def rules(self) -> tuple[PolicyRule, ...]:
        return self._rules

    def describe(self) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in self._rules]

    def evaluate(self, payload: str) -> tuple[PolicyFinding, ...]:
        hits: list[PolicyFinding] = []
        for rule in self._rules:
            if rule.matches(payload):
                hits.append(
                    PolicyFinding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        description=rule.description,
                        requires_hitl=rule.requires_hitl,
                    )
                )
        return tuple(hits)

    def evaluate_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        risk_tier: str,
    ) -> tuple[PolicyFinding, ...]:
        """工具调用的策略评估：工具名 + 参数序列化后一起送检。"""
        try:
            rendered = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 不可序列化参数
            rendered = repr(arguments)
        payload = f"{tool_name} {rendered}"
        hits = list(self.evaluate(payload))
        if risk_tier in ("P0", "P1") and not hits:
            hits.append(
                PolicyFinding(
                    rule_id="GR-RISK-01",
                    category="HIGH_RISK_ACTION",
                    description=f"风险等级 {risk_tier} 的工具调用需要人工审批",
                    requires_hitl=True,
                )
            )
        return tuple(hits)


__all__ = [
    "BLOCKING_CATEGORIES",
    "DefaultAgentSafetyPolicy",
    "PolicyFinding",
    "PolicyRule",
    "RISK_TIERS",
]
