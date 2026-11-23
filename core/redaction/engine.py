"""W09 T01 脱敏引擎：确定性、不可逆、不可靠不冒充。

设计要点：
- 载荷遍历按 dict 键 `sorted()`、list 下标序；同一叶子内规则按 R01→R12 固定顺序；
  `detected_categories` 按枚举声明序输出，span 按 `(json_path, start, category)` 排序。
- `status` 用优先级法一次 `max()` 判定，**不依赖规则命中顺序**。
- 超限（深度/叶子数/扫描字符）只加 blocker，不截断、不丢弃任何键。
- `BLOCKED` 命中（连接串/私钥/证书）不做"可用化改写"，但会用占位符抹掉原串
  （见 `workers`/报告里的口径说明：不让 BLOCKED 载荷里残留可用敏感信息）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
import re

from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
    RedactionSummary,
    SensitiveCategory,
    SensitiveSpan,
)
from core.redaction.payload_text import canonical_payload_hash
from core.redaction.rules import (
    AKIA_RE,
    API_KEY_ASSIGN_RE,
    BEARER_TOKEN_RE,
    BIZ_ID_RE,
    BLOCKER_BINARY_LEAF,
    BLOCKER_DEPTH_EXCEEDED,
    BLOCKER_SCAN_SIZE_EXCEEDED,
    BLOCKER_UNKNOWN_ENCODING,
    CERTIFICATE_RE,
    CONNECTION_STRING_RE,
    EMAIL_RE,
    ID_CARD_RE,
    JWT_RE,
    MAX_REDACTION_DEPTH,
    MAX_REDACTION_ITEMS,
    MAX_SCAN_CHARS,
    PASSWORD_ASSIGN_RE,
    PASSWORD_PRESENT_RE,
    PHONE_PLAIN_RE,
    PHONE_SEPARATED_RE,
    PRIVATE_KEY_RE,
    REASON_BINARY_LEAF,
    REASON_ID_CHECKSUM_INVALID,
    REASON_SEMANTIC_CONFIRMATION,
    REASON_UNREPLACEABLE_CERTIFICATE,
    REASON_UNREPLACEABLE_CONNECTION_STRING,
    REASON_UNREPLACEABLE_KEY_MATERIAL,
    RS_REDACTION_V2,
    SQL_DOUBLE_RE,
    SQL_SINGLE_RE,
    TOKEN_ASSIGN_RE,
    USER_ASSIGN_RE,
    id_card_checksum_valid,
    is_binary_leaf,
    is_name_key,
    is_sensitive_key,
    is_sql_excluded,
    sha8,
)

_STATUS_PRIORITY: dict[RedactionStatus, int] = {
    RedactionStatus.CLEAN: 0,
    RedactionStatus.REDACTED: 1,
    RedactionStatus.REVIEW_REQUIRED: 2,
    RedactionStatus.BLOCKED: 3,
}
_BLOCKING_CATEGORIES = frozenset(
    {
        SensitiveCategory.CONNECTION_STRING,
        SensitiveCategory.PRIVATE_KEY,
        SensitiveCategory.CERTIFICATE,
    }
)
_BLOCKING_BLOCKERS = frozenset({BLOCKER_BINARY_LEAF, BLOCKER_UNKNOWN_ENCODING})


@dataclass
class _ScanContext:
    max_depth: int
    max_items: int
    max_scan_chars: int
    detected: set[SensitiveCategory] = field(default_factory=set)
    spans: list[SensitiveSpan] = field(default_factory=list)
    blockers: set[str] = field(default_factory=set)
    count: int = 0
    items: int = 0
    scanned_chars: int = 0

    def ordered_categories(self) -> tuple[SensitiveCategory, ...]:
        return tuple(item for item in SensitiveCategory if item in self.detected)

    def ordered_spans(self) -> tuple[SensitiveSpan, ...]:
        return tuple(
            sorted(
                self.spans,
                key=lambda span: (span.json_path, span.start, span.category.value),
            )
        )

    def ordered_blockers(self) -> tuple[str, ...]:
        return tuple(sorted(self.blockers))

    def resolve_status(self) -> RedactionStatus:
        candidates: list[RedactionStatus] = [RedactionStatus.CLEAN]
        if self.count > 0:
            candidates.append(RedactionStatus.REDACTED)
        if (
            SensitiveCategory.PERSON_OR_BUSINESS_ID in self.detected
            or self.spans
            or self.blockers
        ):
            candidates.append(RedactionStatus.REVIEW_REQUIRED)
        if self.detected & _BLOCKING_CATEGORIES or self.blockers & _BLOCKING_BLOCKERS:
            candidates.append(RedactionStatus.BLOCKED)
        return max(candidates, key=lambda status: _STATUS_PRIORITY[status])


def _replace_group(match: re.Match[str], group_index: int, token: str) -> str:
    """保留前缀（如 `user=` / `Bearer `），只替换捕获组里的敏感值。"""
    offset = match.start(group_index) - match.start(0)
    return match.group(0)[:offset] + token


class DataRedactionEngine:
    """无状态脱敏器：不持有 DSN/registry，不连接数据库，不依赖规则命中顺序。

    `ruleset_version` 缺省为 `RS_REDACTION_V2`（当前词表口径）；显式传
    `RS_REDACTION_V1` 可复现历史数据库行的 `redaction-v1` 口径。
    """

    def __init__(
        self,
        *,
        ruleset_version: str = RS_REDACTION_V2,
        max_depth: int = MAX_REDACTION_DEPTH,
        max_items: int = MAX_REDACTION_ITEMS,
        max_scan_chars: int = MAX_SCAN_CHARS,
    ) -> None:
        if not ruleset_version or ruleset_version == "none":
            raise ValueError("redaction_version 不得为空或 'none'")
        if max_depth < 1 or max_items < 1 or max_scan_chars < 1:
            raise ValueError("脱敏上限必须为正整数")
        self._ruleset_version = ruleset_version
        self._max_depth = max_depth
        self._max_items = max_items
        self._max_scan_chars = max_scan_chars

    @property
    def version(self) -> str:
        return self._ruleset_version

    def assert_only_pii_categories(self, payload: Any) -> tuple[SensitiveCategory, ...]:
        """**断言专用**通道的 PII 复扫（P1-T1）。

        为什么需要它：R01（SQL 字面量）先于 R02/R03 执行，会把 JSON 编码的 prompt 里
        **引号内的整段文本**替换成 ``<L:…>``，从而把其中的手机号/邮箱一起吞掉——
        `redact()` 因此报 `REDACTED` 而不是 `PHONE`，断言通道就看不到 PII。
        本方法绕过规则顺序，只按 PII 正则逐字符串叶子复扫，供"只断言、不改写"的
        闸门③/④ 判定；**不改变** `redact()` 的既有规则顺序与产物（改写型通道不受影响）。

        Args:
            payload: 任意可 JSON 化结构（闸门传的就是待发载荷）。

        Returns:
            命中的类别元组（按固定顺序去重）；无命中返回空元组。
        """
        found: set[SensitiveCategory] = set()
        budget = {"chars": self._max_scan_chars}

        def _visit(value: Any, depth: int) -> None:
            if budget["chars"] <= 0 or depth > MAX_REDACTION_DEPTH:
                return
            if isinstance(value, Mapping):
                for item in value.values():
                    _visit(item, depth + 1)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    _visit(item, depth + 1)
                return
            if not isinstance(value, str):
                return
            budget["chars"] -= len(value)
            if PHONE_PLAIN_RE.search(value) or PHONE_SEPARATED_RE.search(value):
                found.add(SensitiveCategory.PHONE)
            if EMAIL_RE.search(value):
                found.add(SensitiveCategory.EMAIL)

        _visit(payload, 0)
        order = list(SensitiveCategory)
        return tuple(item for item in order if item in found)

    def redact(self, payload: Any, *, channel: RedactionChannel) -> RedactionResult:
        """对任意载荷脱敏；非 dict 入参包在 `{"value": ...}` 下保持结构完整。"""
        if not isinstance(channel, RedactionChannel):
            raise ValueError("channel 必须是 RedactionChannel")
        source_hash = canonical_payload_hash(payload)
        root: Any = payload if isinstance(payload, Mapping) else {"value": payload}
        context = _ScanContext(
            max_depth=self._max_depth,
            max_items=self._max_items,
            max_scan_chars=self._max_scan_chars,
        )
        walked = self._walk(root, "$", 0, "", False, context)
        payload_redacted: dict[str, Any] = (
            dict(walked) if isinstance(walked, dict) else {"value": walked}
        )
        return RedactionResult(
            payload_redacted=payload_redacted,
            redaction_version=self._ruleset_version,
            detected_categories=context.ordered_categories(),
            redaction_count=context.count,
            unresolved_sensitive_spans=context.ordered_spans(),
            payload_hash=source_hash,
            status=context.resolve_status(),
            blockers=context.ordered_blockers(),
        )

    def redact_text(
        self, text: str, *, channel: RedactionChannel
    ) -> tuple[str, RedactionResult]:
        """字符串入口；结果载荷为 `{"text": ...}`，`payload_hash` 即该包装载荷的原文哈希。"""
        result = self.redact({"text": text}, channel=channel)
        return str(result.payload_redacted["text"]), result

    def summary(self, result: RedactionResult) -> RedactionSummary:
        """生成不含 payload 本体的审计/证据摘要。"""
        return RedactionSummary(
            redaction_version=result.redaction_version,
            status=result.status,
            detected_categories=result.detected_categories,
            redaction_count=result.redaction_count,
            source_payload_hash=result.payload_hash,
            redacted_payload_hash=canonical_payload_hash(result.payload_redacted),
        )

    # ---- traversal -----------------------------------------------------
    def _walk(
        self,
        value: Any,
        path: str,
        depth: int,
        key: str,
        key_sensitive: bool,
        context: _ScanContext,
    ) -> Any:
        context.items += 1
        if depth > context.max_depth:
            context.blockers.add(BLOCKER_DEPTH_EXCEEDED)
            return self._copy(value, path, context)
        if context.items > context.max_items:
            context.blockers.add(BLOCKER_SCAN_SIZE_EXCEEDED)
            return self._copy(value, path, context)

        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key in sorted(value.keys(), key=str):
                child_key = str(raw_key)
                child_sensitive = key_sensitive or is_sensitive_key(child_key)
                result[child_key] = self._walk(
                    value[raw_key],
                    f"{path}.{child_key}",
                    depth + 1,
                    child_key,
                    child_sensitive,
                    context,
                )
            return result
        if isinstance(value, (list, tuple)):
            items = [
                self._walk(
                    item, f"{path}[{index}]", depth + 1, key, key_sensitive, context
                )
                for index, item in enumerate(value)
            ]
            return tuple(items) if isinstance(value, tuple) else items
        if is_binary_leaf(value):
            context.blockers.add(BLOCKER_BINARY_LEAF)
            context.spans.append(
                SensitiveSpan(
                    category=SensitiveCategory.LOG_FIELD,
                    json_path=path,
                    start=0,
                    end=0,
                    reason=REASON_BINARY_LEAF,
                )
            )
            return "<REDACTED:BINARY_LEAF>"
        if isinstance(value, str):
            if context.scanned_chars + len(value) > context.max_scan_chars:
                context.blockers.add(BLOCKER_SCAN_SIZE_EXCEEDED)
                return value  # 不截断：原样保留，交由 REVIEW_REQUIRED 隔离
            context.scanned_chars += len(value)
            return self._scan_leaf(value, path, key, key_sensitive, context)
        if isinstance(value, (int, float, bool)) or value is None:
            if key_sensitive:
                context.detected.add(SensitiveCategory.LOG_FIELD)
                context.count += 1
                return "<REDACTED:LOG_FIELD>"
            return value
        # 非 JSON 原生对象：解码不可靠，绝不猜内容。
        context.blockers.add(BLOCKER_UNKNOWN_ENCODING)
        context.spans.append(
            SensitiveSpan(
                category=SensitiveCategory.LOG_FIELD,
                json_path=path,
                start=0,
                end=0,
                reason=BLOCKER_UNKNOWN_ENCODING,
            )
        )
        return "<REDACTED:UNKNOWN_ENCODING>"

    def _copy(self, value: Any, path: str, context: _ScanContext) -> Any:
        """超限分支的结构复制：不删键、不截断，只把二进制叶子换成占位符。"""
        if isinstance(value, Mapping):
            return {
                str(raw_key): self._copy(item, f"{path}.{raw_key}", context)
                for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            items = [self._copy(item, f"{path}[{i}]", context) for i, item in enumerate(value)]
            return tuple(items) if isinstance(value, tuple) else items
        if is_binary_leaf(value):
            context.blockers.add(BLOCKER_BINARY_LEAF)
            context.spans.append(
                SensitiveSpan(
                    category=SensitiveCategory.LOG_FIELD,
                    json_path=path,
                    start=0,
                    end=0,
                    reason=REASON_BINARY_LEAF,
                )
            )
            return "<REDACTED:BINARY_LEAF>"
        return value

    # ---- leaf scanning -------------------------------------------------
    def _apply(
        self,
        text: str,
        pattern: re.Pattern[str],
        category: SensitiveCategory,
        path: str,
        context: _ScanContext,
        replace_fn: Callable[[re.Match[str]], str | None],
        *,
        keep_fn: Callable[[re.Match[str]], bool] | None = None,
        blocked: bool = False,
        reason: str = "UNRESOLVED_SENSITIVE_SPAN",
    ) -> str:
        matches = list(pattern.finditer(text))
        if not matches:
            return text
        context.detected.add(category)
        chunks: list[str] = []
        last = 0
        for match in matches:
            chunks.append(text[last : match.start()])
            if blocked:
                chunks.append(f"<BLOCKED:{category.value}>")
                context.spans.append(
                    SensitiveSpan(
                        category=category,
                        json_path=path,
                        start=match.start(),
                        end=match.end(),
                        reason=reason,
                    )
                )
            elif keep_fn is not None and keep_fn(match):
                chunks.append(match.group(0))
            else:
                replacement = replace_fn(match)
                if replacement is None:
                    chunks.append(match.group(0))
                    context.spans.append(
                        SensitiveSpan(
                            category=category,
                            json_path=path,
                            start=match.start(),
                            end=match.end(),
                            reason=reason,
                        )
                    )
                else:
                    chunks.append(replacement)
                    context.count += 1
            last = match.end()
        chunks.append(text[last:])
        return "".join(chunks)

    def _scan_leaf(
        self,
        text: str,
        path: str,
        key: str,
        key_sensitive: bool,
        context: _ScanContext,
    ) -> str:
        # R01 SQL 字面量（单引号 + 双引号，排除短值/数字/NULL 等）
        for pattern in (SQL_SINGLE_RE, SQL_DOUBLE_RE):
            text = self._apply(
                text,
                pattern,
                SensitiveCategory.SQL_LITERAL,
                path,
                context,
                lambda match: f"'<L:{sha8(match.group(1))}>'",
                keep_fn=lambda match: is_sql_excluded(match.group(1)),
            )
        # R02 手机号
        for pattern in (PHONE_PLAIN_RE, PHONE_SEPARATED_RE):
            text = self._apply(
                text,
                pattern,
                SensitiveCategory.PHONE,
                path,
                context,
                lambda match: f"<PHONE:{sha8(match.group(0))}>",
            )
        # R03 邮箱
        text = self._apply(
            text,
            EMAIL_RE,
            SensitiveCategory.EMAIL,
            path,
            context,
            lambda match: f"<EMAIL:{sha8(match.group(0))}>",
        )
        # R04 身份证（校验位失败即记 span，绝不冒充脱敏）
        text = self._apply(
            text,
            ID_CARD_RE,
            SensitiveCategory.ID_CARD,
            path,
            context,
            lambda match: (
                f"<ID:{sha8(match.group(0))}>"
                if id_card_checksum_valid(match.group(0))
                else None
            ),
            reason=REASON_ID_CHECKSUM_INVALID,
        )
        # R05 人名/业务号：替换后仍需人工确认语义 → REVIEW_REQUIRED
        if is_name_key(key) or BIZ_ID_RE.fullmatch(text):
            context.detected.add(SensitiveCategory.PERSON_OR_BUSINESS_ID)
            token = f"<NAME:{sha8(text)}>" if is_name_key(key) else f"<BIZ:{sha8(text)}>"
            context.count += 1
            context.spans.append(
                SensitiveSpan(
                    category=SensitiveCategory.PERSON_OR_BUSINESS_ID,
                    json_path=path,
                    start=0,
                    end=len(text),
                    reason=REASON_SEMANTIC_CONFIRMATION,
                )
            )
            text = token
        # R06 连接串（不可安全改写）
        text = self._apply(
            text,
            CONNECTION_STRING_RE,
            SensitiveCategory.CONNECTION_STRING,
            path,
            context,
            lambda match: f"<BLOCKED:{SensitiveCategory.CONNECTION_STRING.value}>",
            blocked=True,
            reason=REASON_UNREPLACEABLE_CONNECTION_STRING,
        )
        # R07 用户名 + 口令（同一叶子内同时出现才成立）
        if PASSWORD_PRESENT_RE.search(text) and USER_ASSIGN_RE.search(text):
            for pattern in (USER_ASSIGN_RE, PASSWORD_ASSIGN_RE):
                text = self._apply(
                    text,
                    pattern,
                    SensitiveCategory.USERNAME_PASSWORD,
                    path,
                    context,
                    lambda match: _replace_group(match, 2, "<CREDENTIAL>"),
                )
        # R08 Token
        text = self._apply(
            text,
            BEARER_TOKEN_RE,
            SensitiveCategory.TOKEN,
            path,
            context,
            lambda match: _replace_group(match, 2, "<TOKEN>"),
        )
        text = self._apply(
            text,
            JWT_RE,
            SensitiveCategory.TOKEN,
            path,
            context,
            lambda match: "<TOKEN>",
        )
        text = self._apply(
            text,
            TOKEN_ASSIGN_RE,
            SensitiveCategory.TOKEN,
            path,
            context,
            lambda match: _replace_group(match, 2, "<TOKEN>"),
        )
        # R09 API Key
        text = self._apply(
            text,
            API_KEY_ASSIGN_RE,
            SensitiveCategory.API_KEY,
            path,
            context,
            lambda match: _replace_group(match, 2, "<API_KEY>"),
        )
        text = self._apply(
            text,
            AKIA_RE,
            SensitiveCategory.API_KEY,
            path,
            context,
            lambda match: "<API_KEY>",
        )
        # R10 / R11 密钥材料与证书（不可安全改写）
        text = self._apply(
            text,
            PRIVATE_KEY_RE,
            SensitiveCategory.PRIVATE_KEY,
            path,
            context,
            lambda match: f"<BLOCKED:{SensitiveCategory.PRIVATE_KEY.value}>",
            blocked=True,
            reason=REASON_UNREPLACEABLE_KEY_MATERIAL,
        )
        text = self._apply(
            text,
            CERTIFICATE_RE,
            SensitiveCategory.CERTIFICATE,
            path,
            context,
            lambda match: f"<BLOCKED:{SensitiveCategory.CERTIFICATE.value}>",
            blocked=True,
            reason=REASON_UNREPLACEABLE_CERTIFICATE,
        )
        # R12 敏感键整值替换（结构性规则，最后应用）
        if key_sensitive:
            context.detected.add(SensitiveCategory.LOG_FIELD)
            context.count += 1
            text = "<REDACTED:LOG_FIELD>"
        return text


__all__ = ["DataRedactionEngine"]
