"""W09 T02 脱敏闸门：五道闸门（①工具结果 ②证据 ③上下文 ④模型消息 ⑤文档）。

设计口径（`docs/w09/02-design.md` §1.2 / §3 / §8.2）：

- 闸门是**通道入口的唯一判定点**："脱敏"由引擎产物证明，不由版本号声明；
- 判定只有两种结论：`CLEAN` / `REDACTED` 放行，`REVIEW_REQUIRED` / `BLOCKED`
  一律**不放行**（fail closed），且都发对应审计事件；
- `BLOCKED` 抛 `RedactionBlockedError` 前，引擎产物里原串已被固定占位
  `<BLOCKED:CATEGORY>` 抹除（占位属 T01 引擎语义，闸门不重写）；
- `REVIEW_REQUIRED` 额外写隔离区，隔离区**只存哈希 / 类别 / `json_path` 定位**，
  绝不含原文片段（`QuarantineRepository` 的入参就是 `RedactionResult`，落库实现
  只允许取 `payload_hash` / `detected_categories` / `unresolved_sensitive_spans`）；
- 闸门**不修改** `ToolStatus`：把"内容被隔离"写成 `DENIED` 会误报权限事实，
  写成 `ERROR` 会误报采集失败（`docs/w09/02-design.md:253`）；
- 闸门不是开关：本模块没有 `enabled` / `skip` / `allow_unsafe` 之类的旁路形参。

为什么契约类型全部延迟导入（见各方法内的局部 import）：

`core.tools.contracts` 需要 `RedactionSummary`（`core.redaction.models`），而本模块
需要 `ToolResult` / `EvidenceCreate` / `ContextBundle` / `ModelMessage`。顶层互相
导入会在"先导入 `core.tools.contracts`"的路径上触发 `core.redaction.__init__` →
`gate` → `core.tools.contracts`（此时 `ToolResult` 尚未定义）而失败。因此：
**本模块顶层只依赖脱敏/审计/错误三个纯领域模块**，契约类走 `TYPE_CHECKING` +
方法内局部导入，从结构上消除循环导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

from core.audit.events import AuditEventType, make_event
from core.errors import RedactionBlockedError, RedactionReviewRequiredError
from core.redaction.engine import DataRedactionEngine
from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
    RedactionSummary,
    SensitiveCategory,
)
from core.redaction.payload_text import canonical_payload_hash

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时不导入（避免循环导入）
    from core.context.contracts import ContextBundle, EvidenceItem
    from core.evidence.models import EvidenceCreate
    from core.models.contracts import ModelMessage
    from core.tools.contracts import ToolError, ToolResult

_PASSABLE: frozenset[RedactionStatus] = frozenset(
    {RedactionStatus.CLEAN, RedactionStatus.REDACTED}
)

#: **断言专用**通道（闸门③/④：只断言、不改写）不得出现的硬 PII 类别。
#:
#: 为什么不能直接看状态：引擎把 `SQL_LITERAL` 也渲染成 `REDACTED`（普通 JSON 里的
#: 数值字面量就会命中），而 `REDACTED` 对**改写型**通道是正常结果。断言通道不写回，
#: 因此只按"是否检出身份/凭据类命中"判定，避免把 `{"lock_wait_s": 12.5}` 判成泄漏。
#: P1-T1 实测缺口：自由文本里的手机号此前以 `REDACTED` 状态**原样**发往模型。
_ASSERT_ONLY_FORBIDDEN_CATEGORIES: frozenset[SensitiveCategory] = frozenset(
    {
        SensitiveCategory.PHONE,
        SensitiveCategory.EMAIL,
        SensitiveCategory.ID_CARD,
        SensitiveCategory.PERSON_OR_BUSINESS_ID,
        SensitiveCategory.CONNECTION_STRING,
        SensitiveCategory.USERNAME_PASSWORD,
        SensitiveCategory.TOKEN,
        SensitiveCategory.API_KEY,
        SensitiveCategory.PRIVATE_KEY,
        SensitiveCategory.CERTIFICATE,
        SensitiveCategory.LOG_FIELD,
    }
)

#: 断言通道的升级标记（写进 `blockers`，便于在事件里区分"模型可见性阻断"）。
ASSERT_ONLY_BLOCKER: str = "ASSERT_ONLY_UNREDACTED_PII"

# 三态 → 审计事件（`core/audit/events.py:110-112`，W09 T01 已定义）。
_EVENT_FOR_STATUS: dict[RedactionStatus, AuditEventType] = {
    RedactionStatus.CLEAN: AuditEventType.REDACTION_APPLIED,
    RedactionStatus.REDACTED: AuditEventType.REDACTION_APPLIED,
    RedactionStatus.REVIEW_REQUIRED: AuditEventType.REDACTION_REVIEW_REQUIRED,
    RedactionStatus.BLOCKED: AuditEventType.REDACTION_BLOCKED,
}

_DOCUMENT_CHANNELS: frozenset[RedactionChannel] = frozenset(
    {
        RedactionChannel.REPORT,
        RedactionChannel.EXPORT,
        RedactionChannel.TRAINING_SAMPLE,
    }
)


@runtime_checkable
class QuarantineRepository(Protocol):
    """隔离区写入口（RECOVERY 侧的落库实现在 `core.redaction.quarantine`）。

    `result` 是**引擎产物**：实现方只允许读取 `payload_hash`、
    `detected_categories`、`unresolved_sensitive_spans`、`blockers`、
    `redaction_version` —— `payload_redacted` 里可能仍有未脱敏结构，
    **不得整包落库**，更不得写任何原文片段。
    """

    def insert(
        self,
        *,
        tenant_id: str,
        task_id: str | None,
        channel: str,
        result: RedactionResult,
    ) -> str:
        """写入一条隔离记录并返回隔离 ID（实现方保证幂等键唯一）。"""
        ...


class RedactionGate:
    """七条通道共用的脱敏闸门（无 DSN、无连接、无状态旁路）。

    形参与属性里**没有** `ro_dsn` / `rw_dsn` / `control_dsn`（AC-18 签名断言），
    闸门不认识数据库账号，只做确定性判定与事件/隔离落点。
    """

    def __init__(
        self,
        engine: DataRedactionEngine,
        *,
        audit: Any | None = None,
        quarantine: QuarantineRepository | None = None,
    ) -> None:
        self._engine = engine
        self._audit = audit
        self._quarantine = quarantine

    # ---- properties -----------------------------------------------------
    @property
    def engine(self) -> DataRedactionEngine:
        return self._engine

    @property
    def version(self) -> str:
        """当前规则集版本（与证据行 / 审计事件里的 `redaction_version` 同源）。"""
        return self._engine.version

    @property
    def audit(self) -> Any | None:
        return self._audit

    @property
    def quarantine(self) -> QuarantineRepository | None:
        return self._quarantine

    # ---- 闸门①：工具结果 ------------------------------------------------
    def for_tool_result(
        self, result: ToolResult, *, tenant_id: str, task_id: str
    ) -> ToolResult:
        """工具结果汇聚点脱敏（`ToolExecutor.execute` 内唯一调用点）。

        Args:
            result: 适配器原始结果。
            tenant_id: 租户（隔离区归属）。
            task_id: 任务（审计事件必填）。

        Returns:
            新的 `ToolResult`：放行时 `data`/`warnings`/`source_metadata` 换成脱敏后
            内容；不放行时 `data={}` + `truncated=True`。**`status` 一律不变**。
            两种情况都带 `redaction` 摘要，供下游判定与取证。
        """
        from core.tools.contracts import ToolError  # 局部导入：消除循环导入

        scan = self._scan(
            _tool_result_payload(result),
            channel=RedactionChannel.TOOL_RESULT,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=result.run_id,
            tool_call_id=result.tool_call_id,
        )
        summary = self._engine.summary(scan)
        if not _is_passable(scan.status):
            # 内容被隔离：清空 data 并显式标注截断；ToolStatus 保持适配器事实。
            # 错误对象同样换成引擎产物（`error.message`/`details` 里可能带原文），
            # 但 Shape 不变，调用方仍能按 `ToolError` 读。
            update: dict[str, Any] = {
                "data": {},
                "truncated": True,
                "redaction": summary,
            }
            blocked_error = _redacted_error(result.error, scan.payload_redacted, ToolError)
            if blocked_error is not None:
                update["error"] = blocked_error
            return result.model_copy(update=update)

        redacted = scan.payload_redacted
        update: dict[str, Any] = {
            "data": dict(redacted.get("data") or {}),
            "warnings": [str(item) for item in (redacted.get("warnings") or [])],
            "source_metadata": dict(redacted.get("source_metadata") or {}),
            "redaction": summary,
        }
        cleaned_error = _redacted_error(result.error, redacted, ToolError)
        if cleaned_error is not None:
            update["error"] = cleaned_error
        return result.model_copy(update=update)

    # ---- 闸门②：证据持久化 ----------------------------------------------
    def for_evidence(
        self, request: EvidenceCreate, *, tenant_id: str, task_id: str
    ) -> EvidenceCreate:
        """证据写入前的闸门（`EvidenceService.create` 内、哈希计算之前）。

        `payload` 与 `redacted_payload` 一律替换为**引擎产物**，`source_payload_hash`
        取引擎给出的**原文哈希**（落库到新增列，不参与 `payload_hash` 计算）。

        两段式链路（`evidence_mapping` 已用引擎产出脱敏后载荷 → 闸门②再复查）：
        入参已带 `source_payload_hash` 时**保留该锚点**，不再用"对已脱敏载荷再算一次"
        的哈希覆盖它 —— 否则原文锚点会被二次扫描抹掉（A2 要求它指向原文）。

        Raises:
            RedactionBlockedError: 载荷含不可安全改写内容（结构性阻断）。
            RedactionReviewRequiredError: 载荷需人工复核（已写隔离区）。
        """
        original_hash = request.source_payload_hash or None
        scan = self._scan(
            dict(request.payload),
            channel=RedactionChannel.EVIDENCE,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=task_id,
            tool_call_id=request.tool_call_id,
            source_payload_hash=original_hash,
        )
        if not _is_passable(scan.status):
            # 不返回可写请求：调用方拿不到任何可落库对象。
            self._raise_not_passable(
                scan,
                channel=RedactionChannel.EVIDENCE,
                tenant_id=tenant_id,
                task_id=task_id,
            )

        redacted = dict(scan.payload_redacted)
        # W03 既有语义：调用方可以自带更严格的脱敏变体（如 `{"id": "***"}`）。
        # 该变体予以保留，但**必须再过一次引擎**：既不让更严格的掩码被引擎产物覆盖，
        # 也不允许它成为"原文旁路"（若变体里仍有可判定命中，就地抹掉/阻断）。
        redacted_variant = redacted
        if request.redacted_payload is not None:
            variant_scan = self._engine.redact(
                dict(request.redacted_payload), channel=RedactionChannel.EVIDENCE
            )
            if not _is_passable(variant_scan.status):
                self._record(
                    variant_scan,
                    channel=RedactionChannel.EVIDENCE,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    run_id=task_id,
                    tool_call_id=request.tool_call_id,
                    source_payload_hash=original_hash,
                )
                self._raise_not_passable(
                    variant_scan,
                    channel=RedactionChannel.EVIDENCE,
                    tenant_id=tenant_id,
                    task_id=task_id,
                )
            redacted_variant = dict(variant_scan.payload_redacted)

        return request.model_copy(
            update={
                "payload": redacted,
                "redacted_payload": redacted_variant,
                "source_payload_hash": original_hash or scan.payload_hash,
                "redaction_version": scan.redaction_version,
            }
        )

    # ---- 闸门③：上下文包 ------------------------------------------------
    def for_model_input(
        self, bundle: ContextBundle, *, tenant_id: str, task_id: str
    ) -> ContextBundle:
        """渲染前的全量基线断言（对 `bundle.evidence` 逐条复扫）。

        **只断言、不改写**：证据摘要进入 prompt 前若仍能命中，说明上游通道漏检，
        此时必须拒绝渲染而不是"就地打码"。

        Raises:
            RedactionBlockedError / RedactionReviewRequiredError: 任一证据项未通过。
        """
        payload = {
            "evidence": [_evidence_item_payload(item) for item in bundle.evidence]
        }
        scan = self._scan(
            payload,
            channel=RedactionChannel.MODEL_INPUT,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=task_id,
            assert_only=True,
        )
        if not _is_passable(scan.status):
            self._raise_not_passable(
                scan,
                channel=RedactionChannel.MODEL_INPUT,
                tenant_id=tenant_id,
                task_id=task_id,
            )
        return bundle.model_copy(update={"redaction": self._engine.summary(scan)})

    # ---- 闸门④：模型消息 ------------------------------------------------
    def for_model_messages(
        self,
        messages: list[ModelMessage],
        *,
        task_id: str,
        tenant_id: str | None = None,
    ) -> list[ModelMessage]:
        """适配器调用前的纵深防御复查（`messages[].content` 全量复扫）。

        **只断言、不改写**模型消息：prompt 里允许出现 `<PHONE:...>` 之类的占位，
        但不允许出现任何可判定的原文命中。

        Args:
            messages: 待发送消息（按原顺序）。
            task_id: 任务（审计事件必填）。
            tenant_id: 可选；给出时 `REVIEW_REQUIRED` 才会写隔离区。

        Raises:
            RedactionBlockedError / RedactionReviewRequiredError: 未通过复查。
        """
        payload = {
            "messages": [
                {"role": _role_value(getattr(message, "role", "")), "content": getattr(message, "content", "")}
                for message in messages
            ]
        }
        scan = self._scan(
            payload,
            channel=RedactionChannel.MODEL_MESSAGES,
            tenant_id=tenant_id or "",
            task_id=task_id,
            run_id=task_id,
            assert_only=True,
        )
        if not _is_passable(scan.status):
            self._raise_not_passable(
                scan,
                channel=RedactionChannel.MODEL_MESSAGES,
                tenant_id=tenant_id,
                task_id=task_id,
            )
        return list(messages)

    # ---- 闸门⑤：文档（报告 / 导出 / 训练样本） ---------------------------
    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], RedactionResult]:
        """报告 / 导出 / 训练样本的出口扫描（T03/T06 调用）。

        返回值语义：

        - `BLOCKED`：抛 `RedactionBlockedError`（不产出半成品文件）；
        - `REVIEW_REQUIRED`：**返回**已抹除原串的文档与结果，由调用方把
          `redaction.status` 落到报告上并置 `published=False`（设计 §3 第 5 行）；
        - `CLEAN` / `REDACTED`：照常返回。
        """
        if channel not in _DOCUMENT_CHANNELS:
            raise ValueError(
                "for_document 只接受 REPORT / EXPORT / TRAINING_SAMPLE 通道"
            )
        scan = self._scan(
            dict(payload),
            channel=channel,
            tenant_id=tenant_id or "",
            task_id=task_id,
            run_id=task_id,
        )
        if scan.status is RedactionStatus.BLOCKED:
            self._raise_not_passable(
                scan, channel=channel, tenant_id=tenant_id, task_id=task_id
            )
        return dict(scan.payload_redacted), scan

    # ---- 内部通道：轨迹事件 ----------------------------------------------
    def scan_event(
        self, payload: dict[str, Any], *, task_id: str, tenant_id: str | None = None
    ) -> RedactionResult:
        """内部通道（`TrajectoryRecorder`）的单次扫描入口。

        轨迹不属于七条对外通道（`docs/w09/02-design.md:67`），审计链路仍是
        `redact_payload`（不改动）；这里只把**引擎判定 + 事件/隔离落点**复用同一
        套实现，避免轨迹自己拼一份判定逻辑。返回原始 `RedactionResult`，由调用方
        决定 `BLOCKED` 时如何置占位（轨迹侧置 `{"redaction": "BLOCKED"}`）。
        """
        return self._scan(
            dict(payload),
            channel=RedactionChannel.EVIDENCE,
            tenant_id=tenant_id or "",
            task_id=task_id,
            run_id=task_id,
        )

    # ---- internals -------------------------------------------------------
    def _scan(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        tenant_id: str,
        task_id: str,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        source_payload_hash: str | None = None,
        assert_only: bool = False,
    ) -> RedactionResult:
        """引擎扫描 + 审计落点（每次扫描恰好一条事件）。

        `source_payload_hash` 仅用于**两段式**链路：上游已算出原文锚点时，事件里
        记的应是那个锚点，而不是"对已脱敏载荷再算一次"的值。
        """
        result = self._engine.redact(payload, channel=channel)
        if assert_only:
            result = _enforce_assert_only(
                result, self._engine.assert_only_pii_categories(payload)
            )
        self._record(
            result,
            channel=channel,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id or task_id,
            tool_call_id=tool_call_id,
            source_payload_hash=source_payload_hash,
        )
        return result

    def _record(
        self,
        result: RedactionResult,
        *,
        channel: RedactionChannel,
        tenant_id: str,
        task_id: str,
        run_id: str,
        tool_call_id: str | None = None,
        source_payload_hash: str | None = None,
    ) -> None:
        """发三态事件；`REVIEW_REQUIRED` 额外写隔离区（无原文）。

        审计不可用属"必须成功"语义：异常向上传播，由调用方阻断执行
        （P0-14 / ADR-0006 §3），闸门不吞异常。

        **隔离区不静默跳过**：`REVIEW_REQUIRED` 却写不了隔离区（`tenant_id` 缺失，
        或组合根未装配 `quarantine`）时，事件 payload 追加 `quarantine_skipped: true`
        与 `channel` —— "未写隔离区"因此可被事件检索到，不留无痕迹的留痕缺口。
        """
        needs_quarantine = result.status is RedactionStatus.REVIEW_REQUIRED
        will_quarantine = (
            needs_quarantine and self._quarantine is not None and bool(tenant_id)
        )
        quarantine_skipped = needs_quarantine and not will_quarantine

        if self._audit is not None and task_id:
            payload = self.audit_payload(
                result, source_payload_hash=source_payload_hash
            )
            if quarantine_skipped:
                payload["quarantine_skipped"] = True
                payload["channel"] = channel.value
            self._audit.emit_required(
                make_event(
                    _EVENT_FOR_STATUS[result.status],
                    task_id=task_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    payload=payload,
                )
            )
        if will_quarantine:
            self._quarantine.insert(  # type: ignore[union-attr]
                tenant_id=tenant_id,
                task_id=task_id or None,
                channel=channel.value,
                result=result,
            )

    def _raise_not_passable(
        self,
        result: RedactionResult,
        *,
        channel: RedactionChannel,
        tenant_id: str | None,
        task_id: str,
    ) -> None:
        """按状态抛领域错误；`details` **只带定位与计数，不带任何原文**。"""
        details: dict[str, Any] = {
            "channel": channel.value,
            "status": result.status.value,
            "redaction_version": result.redaction_version,
            "source_payload_hash": result.payload_hash,
            "detected_categories": [item.value for item in result.detected_categories],
            "redaction_count": result.redaction_count,
            "json_paths": [span.json_path for span in result.unresolved_sensitive_spans],
            "blockers": list(result.blockers),
            "task_id": task_id,
        }
        if tenant_id:
            details["tenant_id"] = tenant_id
        if result.status is RedactionStatus.BLOCKED:
            raise RedactionBlockedError(
                "载荷含不可安全改写的敏感信息，已阻断（未落库、未送模型）",
                details=details,
            )
        raise RedactionReviewRequiredError(
            "载荷含需人工复核的敏感命中，已隔离（未落库、未送模型）",
            details=details,
        )

    @staticmethod
    def audit_payload(
        result: RedactionResult, *, source_payload_hash: str | None = None
    ) -> dict[str, Any]:
        """审计事件载荷（AC-08）：六个字段，证据行 `redaction_version` 与之同源。"""
        return {
            "redaction_version": result.redaction_version,
            "source_payload_hash": source_payload_hash or result.payload_hash,
            "redacted_payload_hash": canonical_payload_hash(result.payload_redacted),
            "status": result.status.value,
            "detected_categories": [item.value for item in result.detected_categories],
            "redaction_count": result.redaction_count,
        }


def _redacted_error(
    error: ToolError | None, redacted_payload: dict[str, Any], error_type: Any
) -> ToolError | None:
    """把 `ToolError` 换成引擎产物里的同名结构（`message`/`details` 可能带原文）。

    结构不变（`code`/`message`/`details` 三字段），因此调用方与既有断言看到的是
    同一模型；只有命中片段被替换成占位符。
    """
    if error is None:
        return None
    raw = redacted_payload.get("error")
    if not isinstance(raw, dict):
        # 引擎未产出可校验结构（理论上不发生）：宁可丢掉细节也不回传原文。
        return error_type(code=error.code, message="<REDACTED:ERROR>", details={})
    try:
        return error_type.model_validate(raw)
    except Exception:  # pragma: no cover - 结构异常时的 fail closed 兜底
        return error_type(code=error.code, message="<REDACTED:ERROR>", details={})


def _enforce_assert_only(
    result: RedactionResult,
    extra_categories: tuple[SensitiveCategory, ...] = (),
) -> RedactionResult:
    """断言通道：把"需要改写才能安全"的硬 PII 命中升级为 `BLOCKED`。

    只升级 `REDACTED`——`BLOCKED`/`REVIEW_REQUIRED` 已有各自的异常与事件，
    不在此改写（保持 W09 既有三态语义）。`SQL_LITERAL` 不在硬 PII 集合内，
    因此普通 JSON 数值不会被误判。
    """
    if result.status is not RedactionStatus.REDACTED:
        return result
    hits = (
        set(result.detected_categories) & _ASSERT_ONLY_FORBIDDEN_CATEGORIES
    ) | (set(extra_categories) & _ASSERT_ONLY_FORBIDDEN_CATEGORIES)
    if not hits:
        return result
    return result.model_copy(
        update={
            "status": RedactionStatus.BLOCKED,
            "blockers": tuple(dict.fromkeys((*result.blockers, ASSERT_ONLY_BLOCKER))),
        }
    )


def _is_passable(status: RedactionStatus) -> bool:
    return status in _PASSABLE


def _role_value(role: Any) -> str:
    value = getattr(role, "value", role)
    return str(value)


def _tool_result_payload(result: ToolResult) -> dict[str, Any]:
    """待扫描载荷：只含业务内容，**不含** `tool_name`/`status` 等契约元数据。

    契约元数据（`tool_name` / `source` / `capability_ref` ...）属 R4-B 的结构化
    字段，不参与脱敏判定，避免把元数据键误当业务键。
    """
    payload: dict[str, Any] = {
        "data": dict(result.data),
        "warnings": [str(item) for item in result.warnings],
        "source_metadata": dict(result.source_metadata),
    }
    if result.error is not None:
        payload["error"] = result.error.model_dump(mode="json")
    return payload


def _evidence_item_payload(item: EvidenceItem) -> dict[str, Any]:
    """证据项待扫描载荷：只取会进入 prompt 的文本字段。"""
    return {
        "evidence_id": item.evidence_id,
        "tool_name": item.tool_name,
        "status": item.status,
        "summary": item.summary,
        "warnings": [str(warning) for warning in item.warnings],
        "data_digest": item.data_digest,
    }


__all__ = ["QuarantineRepository", "RedactionGate"]
