"""V3.0 多模态双向脱敏网关（UPG-01）。

从 V2.0 的单一 SQL AST 解析器升级为**按载荷格式路由**的多模态解析器：
SQL AST / K8s YAML / JSON 报文 / 后端源码与系统日志。Phase 2 先交付 SQL 与自由文本
两个处理器，并预留 YAML / JSON / CodeAST 的注册点（Phase 3/4 接入）。

安全红线（继续沿用 M07 的信封加密口径）：

- 映射表一律经 :class:`ModelGatewayReversibleRedactor` 以 Fernet 信封加密持久化，
  绑定 ``workflow_run_id`` 与 TTL；磁盘上只有密文，``ciphertext_sha256`` 可外部核验。
- 还原前先做**结构校验**（``handler.validate``），结构不合法绝不进入替换流程；
  还原后再校验一次，防止映射错位把 YAML 缩进/SQL 语法改坏。
- 变更闭环（``close``）立即擦除进程内工作副本并销毁密文记录，不留跨天残留。

落盘与内存的边界：**磁盘零明文**（只有 Fernet 密文）；为完成跨模态还原，进程内保留
受 TTL 约束的工作副本（``_vault``，仅在控制面进程内存中，``close`` 即擦除）。
这与 M07 自身"解密到内存再还原"的运行模型一致，不是明文落盘。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from core.platform.ten_modules import (
    MappingExpiredError,
    MappingNotFoundError,
    ModelGatewayReversibleRedactor,
    SqlAstAdapter,
)

MODALITY_SQL = "sql"
MODALITY_TEXT = "text"
MODALITY_YAML = "yaml"
MODALITY_JSON = "json"
MODALITY_CODE = "code"

DETECTION_THRESHOLD = 0.5

_SQL_HINT_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|GRANT|REVOKE|TRUNCATE|SET|SHOW|FLUSH)\b",
    re.IGNORECASE,
)
_SQL_STRUCTURE_RE = re.compile(r"\b(?:FROM|WHERE|JOIN|INTO|TABLE|DATABASE|USER|ROLE)\b", re.IGNORECASE)
_PII_TOKEN_PREFIX = "__PII_"


class ModalityStructureError(ValueError):
    """载荷结构校验失败：脱敏/还原都不得在结构损坏的载荷上进行。"""


@runtime_checkable
class ModalityHandler(Protocol):
    """模态处理器：``detect`` 打分，``mask``/``unmask`` 互逆，``validate`` 校验结构。"""

    def modality(self) -> str: ...

    def detect(self, payload: str) -> float: ...

    def mask(self, payload: str) -> tuple[str, dict[str, str]]: ...

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str: ...

    def validate(self, payload: str) -> None: ...


def _apply_mapping(payload: str, mapping: Mapping[str, str]) -> str:
    """按 token 长度倒序替换，避免 ``__IDENT_1__`` 命中 ``__IDENT_10__`` 的前缀。

    PII 令牌永不还原为原文（与 M07 ``restore_inbound`` 口径一致），只还原为掩码。
    """
    restored = payload
    for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
        restored = restored.replace(token, "[REDACTED_PII]" if token.startswith(_PII_TOKEN_PREFIX) else original)
    return restored


class SqlModalityHandler:
    """SQL 模态：复用 V2.0 :class:`SqlAstAdapter` 的词法节点分类与语法校验。"""

    def __init__(self, sql_ast: SqlAstAdapter | None = None) -> None:
        self._sql_ast = sql_ast or SqlAstAdapter()

    def modality(self) -> str:
        return MODALITY_SQL

    def detect(self, payload: str) -> float:
        if not payload or not _SQL_HINT_RE.search(payload):
            return 0.0
        score = 0.86
        if _SQL_STRUCTURE_RE.search(payload):
            score += 0.08
        if payload.rstrip().endswith(";"):
            score += 0.03
        return min(0.99, score)

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        return self._sql_ast.redact(payload)

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        return _apply_mapping(payload, mapping)

    def validate(self, payload: str) -> None:
        self._sql_ast.validate(payload)


class TextModalityHandler:
    """自由文本模态：复用 M07 的实体识别替换（表名 / IP / 凭据 / 手机号）。"""

    def __init__(self, redactor: ModelGatewayReversibleRedactor) -> None:
        self._redactor = redactor

    def modality(self) -> str:
        return MODALITY_TEXT

    def detect(self, payload: str) -> float:
        return 0.1

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        # 复用 M07 公开入口：非 SQL 载荷自动走实体识别替换
        return self._redactor.redact_outbound(payload)

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        return self._redactor.restore_inbound(payload, dict(mapping))

    def validate(self, payload: str) -> None:
        if not isinstance(payload, str):
            raise ModalityStructureError("文本载荷必须是字符串")
        if "\x00" in payload:
            raise ModalityStructureError("文本载荷包含 NUL 控制字符")


@dataclass(frozen=True)
class MaskOutcome:
    modality: str
    masked_text: str
    token_count: int
    mapping_persisted: bool
    structure_valid: bool
    ciphertext_sha256: str = ""
    ttl_seconds: int = 0
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "modality": self.modality,
            "masked_text": self.masked_text,
            "token_count": self.token_count,
            "mapping_persisted": self.mapping_persisted,
            "structure_valid": self.structure_valid,
            "ciphertext_sha256": self.ciphertext_sha256,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "plaintext_persisted": False,
        }


@dataclass(frozen=True)
class RestoreOutcome:
    modality: str
    restored_text: str
    token_count: int
    structure_valid: bool
    destroyed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "modality": self.modality,
            "restored_text": self.restored_text,
            "token_count": self.token_count,
            "structure_valid": self.structure_valid,
            "destroyed": self.destroyed,
        }


@dataclass
class _VaultRecord:
    mapping: dict[str, str]
    expires_at_epoch: float
    modality: str
    token_count: int
    ciphertext_sha256: str


class MultimodalPrivacyGateway:
    """按载荷格式路由的双向可逆脱敏网关。"""

    def __init__(
        self,
        *,
        redactor: ModelGatewayReversibleRedactor | None = None,
        default_ttl_seconds: int = 86_400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redactor = redactor or ModelGatewayReversibleRedactor()
        self.default_ttl_seconds = default_ttl_seconds
        self._clock = clock
        self._handlers: dict[str, ModalityHandler] = {}
        self._vault: dict[str, _VaultRecord] = {}
        self._events: list[dict[str, Any]] = []
        self.register(SqlModalityHandler(self._redactor.sql_ast), replace=True)
        self.register(TextModalityHandler(self._redactor), replace=True)
        self._register_optional_modalities()

    def _register_optional_modalities(self) -> None:
        """注册 YAML 模态（局部 import 以避免与 yaml_masker 形成模块级循环依赖）。"""
        try:
            from core.privacy_gateway.yaml_masker import YamlMasker
        except ImportError:  # pragma: no cover - yaml_masker 与网关同包发布，缺失即环境损坏
            return
        self.register(YamlMasker(), replace=True)

    # -- 注册与路由 ----------------------------------------------------
    def register(self, handler: ModalityHandler, *, replace: bool = False) -> "MultimodalPrivacyGateway":
        modality = handler.modality()
        if not modality:
            raise ValueError("模态处理器必须声明 modality()")
        if modality in self._handlers and not replace:
            raise ValueError(f"模态处理器重复注册: {modality}")
        self._handlers[modality] = handler
        return self

    def redactor(self) -> ModelGatewayReversibleRedactor:
        return self._redactor

    def modalities(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def handler(self, modality: str) -> ModalityHandler:
        handler = self._handlers.get(modality)
        if handler is None:
            raise KeyError(f"未注册的模态处理器: {modality}")
        return handler

    def detect_modality(self, payload: str) -> str:
        best_modality, best_score = MODALITY_TEXT, 0.0
        for modality, handler in self._handlers.items():
            try:
                score = float(handler.detect(payload))
            except Exception:  # noqa: BLE001 - 单个处理器探测异常不得影响整体路由
                score = 0.0
            if score > best_score:
                best_modality, best_score = modality, score
        return best_modality if best_score >= DETECTION_THRESHOLD else MODALITY_TEXT

    # -- 出站脱敏 ------------------------------------------------------
    def mask(
        self,
        payload: str,
        *,
        modality: str | None = None,
        workflow_run_id: str = "",
        ttl_seconds: int | None = None,
    ) -> MaskOutcome:
        chosen = modality or self.detect_modality(payload)
        handler = self.handler(chosen)
        masked_text, mapping = handler.mask(payload)
        # 脱敏后必须仍是合法结构，否则说明节点替换破坏了语法
        handler.validate(masked_text)

        ttl = int(self.default_ttl_seconds if ttl_seconds is None else ttl_seconds)
        persisted = False
        cipher_sha = ""
        expires_at = ""
        if workflow_run_id and mapping:
            if ttl <= 0:
                raise ValueError("映射表 TTL 必须大于 0")
            info = self._redactor.persist_encrypted_mapping(workflow_run_id, mapping, ttl_seconds=ttl)
            persisted = True
            cipher_sha = str(info["ciphertext_sha256"])
            expires_at = str(info["expires_at"])
            self._vault[workflow_run_id] = _VaultRecord(
                mapping=dict(mapping),
                expires_at_epoch=self._clock() + ttl,
                modality=chosen,
                token_count=len(mapping),
                ciphertext_sha256=cipher_sha,
            )

        self._append_event("GATEWAY_MASKED", workflow_run_id, {
            "modality": chosen,
            "token_count": len(mapping),
            "mapping_persisted": persisted,
        })
        return MaskOutcome(
            modality=chosen,
            masked_text=masked_text,
            token_count=len(mapping),
            mapping_persisted=persisted,
            structure_valid=True,
            ciphertext_sha256=cipher_sha,
            ttl_seconds=ttl if persisted else 0,
            expires_at=expires_at,
        )

    # -- 入站还原 ------------------------------------------------------
    def restore(
        self,
        masked_text: str,
        *,
        workflow_run_id: str,
        modality: str | None = None,
        destroy: bool = True,
    ) -> RestoreOutcome:
        record = self._vault.get(workflow_run_id)
        if record is None:
            raise MappingNotFoundError(f"未找到 workflow_run_id={workflow_run_id} 的脱敏映射")
        if self._clock() > record.expires_at_epoch:
            self.close(workflow_run_id, reason="TTL_EXPIRED")
            raise MappingExpiredError(f"workflow_run_id={workflow_run_id} 的脱敏映射已过期并销毁")

        chosen = modality or record.modality
        handler = self.handler(chosen)
        # 还原前结构校验：不合法的载荷绝不进入替换
        handler.validate(masked_text)
        restored = handler.unmask(masked_text, record.mapping)
        handler.validate(restored)

        destroyed = False
        if destroy:
            self.close(workflow_run_id, reason="WORKFLOW_CLOSED")
            destroyed = True
        self._append_event("GATEWAY_RESTORED", workflow_run_id, {
            "modality": chosen,
            "token_count": record.token_count,
            "destroyed": destroyed,
        })
        return RestoreOutcome(
            modality=chosen,
            restored_text=restored,
            token_count=record.token_count,
            structure_valid=True,
            destroyed=destroyed,
        )

    def close(self, workflow_run_id: str, *, reason: str = "WORKFLOW_CLOSED") -> dict[str, Any]:
        """闭环销毁：擦除进程内工作副本 + 销毁信封加密记录。"""
        record = self._vault.pop(workflow_run_id, None)
        destroyed = self._redactor.destroy_mapping(workflow_run_id, reason=reason)
        self._append_event("GATEWAY_MAPPING_DESTROYED", workflow_run_id, {
            "reason": reason,
            "had_vault_record": record is not None,
            "token_count": record.token_count if record else 0,
        })
        return {
            "workflow_run_id": workflow_run_id,
            "vault_erased": record is not None,
            "ciphertext_destroyed": bool(destroyed.get("destroyed")),
            "reason": reason,
        }

    # -- 可观测性 ------------------------------------------------------
    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def snapshot(self) -> dict[str, Any]:
        return {
            "modalities": list(self.modalities()),
            "active_mappings": len(self._vault),
            "event_count": len(self._events),
            "redactor": self._redactor.mapping_lifecycle_summary(),
        }

    def _append_event(self, event_type: str, workflow_run_id: str, detail: dict[str, Any]) -> None:
        self._events.append(
            {
                "seq": len(self._events) + 1,
                "event_type": event_type,
                "workflow_run_id": workflow_run_id,
                "detail": detail,
            }
        )


__all__ = [
    "DETECTION_THRESHOLD",
    "MODALITY_CODE",
    "MODALITY_JSON",
    "MODALITY_SQL",
    "MODALITY_TEXT",
    "MODALITY_YAML",
    "MaskOutcome",
    "ModalityHandler",
    "ModalityStructureError",
    "MultimodalPrivacyGateway",
    "RestoreOutcome",
    "SqlModalityHandler",
    "TextModalityHandler",
]
