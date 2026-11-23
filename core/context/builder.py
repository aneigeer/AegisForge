"""上下文构建器：把工具目录、数据库身份与证据压缩成模型可消费的上下文包。

安全约束：
- 工具输出与数据库内容视为不可信数据，只能作为证据引用；
- 摘要先脱敏再截断，绝不把 DSN/密码写入上下文或 prompt；
- 上下文超长时优先丢弃最早的证据，保留 goal、工具目录与安全提示。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.context.contracts import ContextBundle, EvidenceItem, ToolCatalogEntry
from core.errors import RedactionBlockedError, RedactionReviewRequiredError
from core.logging import redact_text
from core.models.contracts import ModelMessage
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionStatus
from core.tools.contracts import ToolDefinition, ToolResult, ToolStatus
from core.tools.registry import ToolRegistry
from database.mysql.connection import MysqlIdentity

_TRUNCATED_SUFFIX = "...<truncated>"


class ContextBuilder:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_evidence_items: int = 8,
        max_summary_chars: int = 800,
        max_prompt_chars: int = 24_000,
        redaction_gate: RedactionGate | None = None,
        permissions: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._max_evidence_items = max_evidence_items
        self._max_summary_chars = max_summary_chars
        self._max_prompt_chars = max_prompt_chars
        # W09 闸门③（`docs/w09/02-design.md:255`）：**恒开**，未注入时默认构造
        # （引擎无状态、无 DSN）。没有 enabled/配置项，不提供"跳过脱敏"路径。
        self._redaction = redaction_gate or RedactionGate(DataRedactionEngine())
        # P2-T1：构造期可固定引擎权限（PG = `postgresql:read`）；缺省仍按 MySQL 口径。
        self._permissions = list(permissions) if permissions is not None else None

    def build(
        self,
        *,
        goal: str,
        identity: MysqlIdentity,
        read_only: bool,
        budget: dict[str, Any],
        evidence: list[EvidenceItem] | None = None,
        permissions: list[str] | None = None,
    ) -> ContextBundle:
        # P2-T1：显式 permissions 用于**双引擎**（PG 工具权限是 `postgresql:read`）；
        # 缺省保持 MySQL 口径，既有调用方行为逐字不变。
        if permissions is None:
            permissions = self._permissions
        if permissions is None:
            permissions = (
                ["mysql:read"] if read_only else ["mysql:read", "mysql:write"]
            )
        items = list(evidence or [])
        if self._max_evidence_items <= 0:
            kept: list[EvidenceItem] = []
            truncated = len(items)
        elif len(items) > self._max_evidence_items:
            kept = items[-self._max_evidence_items :]
            truncated = len(items) - self._max_evidence_items
        else:
            kept = items
            truncated = 0

        return ContextBundle(
            goal=goal,
            database_id=identity.database_id,
            db_version=identity.server_version,
            db_user=identity.user,
            permissions=permissions,
            tools=[_catalog_entry(definition) for definition in self._registry.definitions()],
            evidence=kept,
            budget=dict(budget),
            truncated_evidence=truncated,
        )

    def summarize_result(
        self, result: ToolResult, *, evidence_id: str | None = None
    ) -> EvidenceItem:
        canonical = json.dumps(
            result.data,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        summary = redact_text(canonical)
        if len(summary) > self._max_summary_chars:
            summary = summary[: self._max_summary_chars] + _TRUNCATED_SUFFIX

        status = result.status.value if isinstance(result.status, ToolStatus) else str(result.status)
        source = result.source
        return EvidenceItem(
            evidence_id=evidence_id or f"{result.tool_name}:{digest[:12]}",
            tool_name=result.tool_name,
            status=status,
            summary=summary,
            warnings=[redact_text(warning) for warning in result.warnings],
            database_id=source.database_id,
            db_version=source.db_version,
            collected_at=source.collected_at,
            data_digest=digest,
            # W09：把闸门① 的脱敏摘要（锚点）带到上下文项上，`render_messages`
            # 据此做逐项复核；工具结果未过闸门时保持 `None`（旧路径，由复扫兜底）。
            redaction=result.redaction,
        )

    def render_messages(
        self,
        bundle: ContextBundle,
        system_prompt: str,
        *,
        tenant_id: str = "",
        task_id: str = "",
    ) -> list[ModelMessage]:
        """渲染模型消息（**W09 闸门③**：未通过断言就不产出任何消息）。

        两道判定，缺一不可：

        1. **整包复扫**（`for_model_input`）：把即将进入 prompt 的证据字段再喂一次
           脱敏引擎 —— 这是"调用方漏检也拦得住"的纵深防御，命中即抛；
        2. **逐项复核上游锚点**：`EvidenceItem.redaction` 若已标注且状态不是
           `CLEAN/REDACTED`（例如闸门① 已判 `BLOCKED` 并把 `data` 清空），
           同样拒绝渲染 —— 不能用一个"干净但目前为空"的摘要掩盖上游阻断。

        `EvidenceItem.redaction is None` 只表示该证据来自**未经闸门的旧路径**：
        此时第 1 步刚对同一内容做过全量复扫且已通过，防护强度不降（复扫比历史
        锚点更强，因为它是针对当前字节做的判定）。

        Raises:
            RedactionBlockedError: 证据项含结构性阻断（连接串/私钥/证书）。
            RedactionReviewRequiredError: 证据项命中需人工复核的敏感类别。
        """
        gated = self._redaction.for_model_input(
            bundle, tenant_id=tenant_id, task_id=task_id
        )
        for item in gated.evidence:
            summary = item.redaction
            if summary is None:
                continue
            if summary.status is RedactionStatus.BLOCKED:
                raise RedactionBlockedError(
                    "证据项含不可安全改写的敏感信息，禁止渲染模型输入",
                    details={
                        "evidence_id": item.evidence_id,
                        "tool_name": item.tool_name,
                        "redaction_version": summary.redaction_version,
                        "source_payload_hash": summary.source_payload_hash,
                        "detected_categories": [
                            category.value for category in summary.detected_categories
                        ],
                    },
                )
            if summary.status not in (
                RedactionStatus.CLEAN,
                RedactionStatus.REDACTED,
            ):
                raise RedactionReviewRequiredError(
                    "证据项含需人工复核的敏感命中，禁止渲染模型输入",
                    details={
                        "evidence_id": item.evidence_id,
                        "tool_name": item.tool_name,
                        "status": summary.status.value,
                        "redaction_version": summary.redaction_version,
                        "source_payload_hash": summary.source_payload_hash,
                    },
                )

        payload = gated.model_dump_for_prompt()
        user_content = json.dumps(payload, ensure_ascii=False, default=str)

        if len(user_content) > self._max_prompt_chars:
            evidence_items = list(payload.get("evidence", []))
            original_count = len(evidence_items)
            payload["context_truncated"] = True
            while evidence_items and len(user_content) > self._max_prompt_chars:
                evidence_items.pop(0)
                payload["evidence"] = evidence_items
                payload["truncated_evidence"] = (
                    bundle.truncated_evidence + original_count - len(evidence_items)
                )
                user_content = json.dumps(payload, ensure_ascii=False, default=str)

        return [
            ModelMessage(role="system", content=system_prompt),
            ModelMessage(role="user", content=user_content),
        ]


def _catalog_entry(definition: ToolDefinition) -> ToolCatalogEntry:
    return ToolCatalogEntry(
        name=definition.name,
        description=definition.description,
        risk_level=definition.risk_level,
        side_effect=definition.side_effect,
        permissions=list(definition.permissions),
        timeout_ms=definition.timeout_ms,
        input_schema=dict(definition.input_schema),
    )


__all__ = ["ContextBuilder"]
