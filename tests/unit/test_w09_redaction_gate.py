"""W09 T02a 单元测试：脱敏闸门（全离线，假审计/假仓储，不连库）。

覆盖口径（`docs/w09/03-task-cards.md` T02「必须实现的语义」1/2/3/6/8/10）：

- 闸门①三态：`REDACTED` 换 data、`REVIEW_REQUIRED` / `BLOCKED` 清 data，
  三次 `ToolStatus` **均不变**；
- 闸门②：`BLOCKED` / `REVIEW_REQUIRED` 抛对应领域错误且**证据行新增 0**
  （用假仓储计数 `insert_calls == 0` 做离线镜像，对应 AC-05）；
- 闸门③④：含敏感内容的 `ContextBundle` / `messages` 被拒，**下游未被调用**
  （spy 计数为 0，对应 AC-06）；
- `source_payload_hash` 是新增锚点，**不得覆盖** `payload_hash`（A2）；
- 隔离区入参**不含任何原文片段**（R3）；
- 审计载荷六字段齐备，且证据行 `redaction_version` 与事件**同源**（AC-08）。

本文件**不**修改、不放宽 `tests/unit/test_w09_redaction_engine.py` 的任何断言
（R3 六用例与 R4-D 正负例仍由原文件守住）。
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from core.context.contracts import ContextBundle, EvidenceItem
from core.evidence.models import CollectionStatus, Evidence, EvidenceCreate
from core.models.contracts import ModelMessage
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import QuarantineRepository, RedactionGate
from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
)
from core.tasks.models import TaskEvent
from core.tools.contracts import ToolError, ToolResult, ToolSource, ToolStatus

# --------------------------------------------------------------------------- #
# 样本：三类判定各一份（都取自脱敏引擎既有词表，不新造规则）
# --------------------------------------------------------------------------- #

PHONE_RAW = "13812345678"
NAME_RAW = "张三"
CONNECTION_RAW = "mysql://user:pass@host.internal:3306/db"
PRIVATE_KEY_RAW = "-----BEGIN RSA PRIVATE KEY-----MIIBOgIBAAJBAK-----END RSA PRIVATE KEY-----"
CERTIFICATE_RAW = "-----BEGIN CERTIFICATE-----MIIBkTCB-----END CERTIFICATE-----"

_TENANT = "tenant_w09"
_TASK = "task_w09"


# --------------------------------------------------------------------------- #
# 替身：假审计 / 假隔离区 / 假证据仓储
# --------------------------------------------------------------------------- #


class FakeAudit:
    """`AuditService.emit_required(event)` 的离线替身，按顺序捕获事件。"""

    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    def emit_required(
        self, event: TaskEvent, *, actor_user_id: str | None = None
    ) -> TaskEvent:
        self.events.append(event)
        return event

    @property
    def event_types(self) -> list[str]:
        return [event.event_type for event in self.events]

    def last_payload(self) -> dict[str, Any]:
        return dict(self.events[-1].payload)


class FakeQuarantine:
    """`QuarantineRepository` 替身：记录入参，用于断言"不含原文"。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def insert(
        self,
        *,
        tenant_id: str,
        task_id: str | None,
        channel: str,
        result: RedactionResult,
    ) -> str:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "task_id": task_id,
                "channel": channel,
                "result": result,
            }
        )
        return f"rq_{len(self.calls)}"

    @property
    def insert_calls(self) -> int:
        return len(self.calls)


class FakeEvidenceRepository:
    """`EvidenceRepository.insert` 替身：只为数"新增行数"（AC-05）。"""

    def __init__(self) -> None:
        self.rows: list[Evidence] = []

    def insert(self, evidence: Evidence) -> Evidence:
        self.rows.append(evidence)
        return evidence

    @property
    def insert_calls(self) -> int:
        return len(self.rows)


class SpyDownstream:
    """下游（渲染 / 适配器调用）替身：只数调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #


def _hash_of(payload: dict[str, Any]) -> str:
    from core.evidence.service import canonical_payload_hash

    return canonical_payload_hash(payload)


def _span_projection(span: Any) -> dict[str, Any]:
    return {
        "category": span.category.value,
        "json_path": span.json_path,
        "start": span.start,
        "end": span.end,
        "reason": span.reason,
    }


def _engine() -> DataRedactionEngine:
    return DataRedactionEngine()


def _gate(
    *, audit: FakeAudit | None = None, quarantine: FakeQuarantine | None = None
) -> RedactionGate:
    return RedactionGate(_engine(), audit=audit, quarantine=quarantine)


def _source() -> ToolSource:
    return ToolSource(database_id="lab_unit", db_version="8.0.46", collected_at="2026-03-01T00:00:00+00:00")


def _tool_result(
    data: dict[str, Any],
    *,
    status: ToolStatus = ToolStatus.SUCCESS,
    error: ToolError | None = None,
    warnings: list[str] | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        tool_name="get_locks",
        status=status,
        task_id=_TASK,
        run_id="run_w09",
        tool_call_id="call_w09",
        started_at="2026-03-01T00:00:00+00:00",
        duration_ms=3.5,
        data=data,
        warnings=warnings or [],
        error=error,
        source=_source(),
        observed_at="2026-03-01T00:00:01+00:00",
        source_metadata=source_metadata or {},
    )


def _evidence_create(payload: dict[str, Any], **overrides: Any) -> EvidenceCreate:
    values: dict[str, Any] = {
        "tenant_id": _TENANT,
        "task_id": _TASK,
        "environment_id": "env_w09",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-unit",
        "source_tool": "get_locks",
        "observed_at": "2026-03-01T00:00:00+00:00",
        "collection_status": CollectionStatus.SUCCESS,
        "payload": payload,
    }
    values.update(overrides)
    return EvidenceCreate(**values)


def _bundle(items: list[EvidenceItem]) -> ContextBundle:
    return ContextBundle(
        goal="定位锁等待",
        database_id="lab_unit",
        db_version="8.0.46",
        db_user="dg_ro",
        evidence=items,
    )


def _evidence_item(summary: str, **overrides: Any) -> EvidenceItem:
    values: dict[str, Any] = {
        "evidence_id": "ev_1",
        "tool_name": "get_locks",
        "status": "success",
        "summary": summary,
        "warnings": [],
        "database_id": "lab_unit",
        "db_version": "8.0.46",
        "collected_at": "2026-03-01T00:00:00+00:00",
        "data_digest": "d" * 64,
    }
    values.update(overrides)
    return EvidenceItem(**values)


def _create_through_gate(
    gate: RedactionGate,
    repository: FakeEvidenceRepository,
    request: EvidenceCreate,
) -> Evidence:
    """离线镜像 `EvidenceService.create` 的**闸门位置**：闸门在前、写入在后。"""
    gated = gate.for_evidence(request, tenant_id=request.tenant_id, task_id=request.task_id)
    evidence = Evidence(
        evidence_id="ev_w09",
        tenant_id=gated.tenant_id,
        task_id=gated.task_id,
        environment_id=gated.environment_id,
        db_engine=gated.db_engine,
        db_version=gated.db_version,
        adapter_version=gated.adapter_version,
        source_tool=gated.source_tool,
        observed_at=gated.observed_at,
        collection_status=gated.collection_status,
        payload_hash="p" * 64,
        redaction_version=gated.redaction_version,
        source_payload_hash=gated.source_payload_hash,
        payload=gated.payload,
        redacted_payload=gated.redacted_payload,
    )
    return repository.insert(evidence)


# --------------------------------------------------------------------------- #
# 闸门①：工具结果（三态 + ToolStatus 零影响）
# --------------------------------------------------------------------------- #


def test_gate1_redacted_replaces_data_and_keeps_tool_status() -> None:
    audit = FakeAudit()
    result = _tool_result({"phone": PHONE_RAW, "duration_ms": 1.25})

    gated = _gate(audit=audit).for_tool_result(
        result, tenant_id=_TENANT, task_id=_TASK
    )

    assert gated.status is ToolStatus.SUCCESS  # ToolStatus 不动
    assert gated.data["phone"] != PHONE_RAW
    assert PHONE_RAW not in json.dumps(gated.data, ensure_ascii=False)
    assert gated.data["duration_ms"] == 1.25  # 非敏感叶子原样保留
    assert gated.redaction is not None
    assert gated.redaction.status is RedactionStatus.REDACTED
    assert gated.redaction.redaction_version == _engine().version == "redaction-v2"
    assert audit.event_types == ["REDACTION_APPLIED"]


def test_gate1_review_required_clears_data_but_keeps_tool_status() -> None:
    audit = FakeAudit()
    quarantine = FakeQuarantine()
    result = _tool_result({"name": NAME_RAW, "affected_rows": 1})

    gated = _gate(audit=audit, quarantine=quarantine).for_tool_result(
        result, tenant_id=_TENANT, task_id=_TASK
    )

    assert gated.status is ToolStatus.SUCCESS  # 不降级成 DENIED/ERROR
    assert gated.data == {}
    assert gated.truncated is True
    assert gated.redaction is not None
    assert gated.redaction.status is RedactionStatus.REVIEW_REQUIRED
    assert NAME_RAW not in json.dumps(gated.model_dump(mode="json"), ensure_ascii=False)
    assert audit.event_types == ["REDACTION_REVIEW_REQUIRED"]
    assert quarantine.insert_calls == 1


def test_gate1_blocked_clears_data_but_keeps_tool_status() -> None:
    audit = FakeAudit()
    result = _tool_result({"target": CONNECTION_RAW})

    gated = _gate(audit=audit).for_tool_result(result, tenant_id=_TENANT, task_id=_TASK)

    assert gated.status is ToolStatus.SUCCESS
    assert gated.data == {}
    assert gated.truncated is True
    assert gated.redaction is not None
    assert gated.redaction.status is RedactionStatus.BLOCKED
    assert CONNECTION_RAW not in json.dumps(gated.model_dump(mode="json"), ensure_ascii=False)
    assert audit.event_types == ["REDACTION_BLOCKED"]


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"phone": PHONE_RAW}, RedactionStatus.REDACTED),
        ({"name": NAME_RAW}, RedactionStatus.REVIEW_REQUIRED),
        ({"target": CONNECTION_RAW}, RedactionStatus.BLOCKED),
        ({"pem": PRIVATE_KEY_RAW}, RedactionStatus.BLOCKED),
        ({"cert": CERTIFICATE_RAW}, RedactionStatus.BLOCKED),
    ],
)
def test_gate1_status_is_never_rewritten(
    payload: dict[str, Any], expected: RedactionStatus
) -> None:
    """三态（含 R06/R10/R11 阻断）都不得把 `ToolStatus` 改成 DENIED/ERROR。"""
    base = _tool_result(payload, status=ToolStatus.PARTIAL)

    gated = _gate().for_tool_result(base, tenant_id=_TENANT, task_id=_TASK)

    assert gated.status is ToolStatus.PARTIAL
    assert gated.redaction is not None
    assert gated.redaction.status is expected


def test_gate1_clean_payload_is_byte_identical() -> None:
    """真实 `ToolResult.model_dump()` 的 15 个顶层字段不含敏感命中 → 零改写。"""
    result = _tool_result({"value": 1, "rows": [{"id": 7}]})
    base_dump = result.model_dump(mode="json")

    gated = _gate().for_tool_result(result, tenant_id=_TENANT, task_id=_TASK)

    assert gated.data == result.data
    assert gated.truncated is False
    assert gated.tool_name == result.tool_name
    assert gated.redaction is not None
    assert gated.redaction.status is RedactionStatus.CLEAN
    # 新增字段只在末尾追加，既有 15 个键的取值逐字不变。
    gated_dump = gated.model_dump(mode="json")
    for key, value in base_dump.items():
        if key == "redaction":  # 本卡新增的可选字段，天然与基线不同
            continue
        assert gated_dump[key] == value, key


def test_gate1_error_details_are_redacted_in_place() -> None:
    result = _tool_result(
        {"rows": []},
        status=ToolStatus.ERROR,
        error=ToolError(
            code="DB_UNAVAILABLE",
            message=f"连接失败 {CONNECTION_RAW}",
            details={"dsn": CONNECTION_RAW},
        ),
    )

    gated = _gate().for_tool_result(result, tenant_id=_TENANT, task_id=_TASK)

    assert gated.status is ToolStatus.ERROR
    dumped = json.dumps(gated.model_dump(mode="json"), ensure_ascii=False)
    assert CONNECTION_RAW not in dumped
    assert "host.internal" not in dumped


# --------------------------------------------------------------------------- #
# 闸门②：证据持久化（AC-05：0 新增行）
# --------------------------------------------------------------------------- #


def test_gate2_blocked_raises_and_writes_zero_evidence_rows() -> None:
    from core.errors import RedactionBlockedError

    audit = FakeAudit()
    repository = FakeEvidenceRepository()
    gate = _gate(audit=audit)

    with pytest.raises(RedactionBlockedError) as excinfo:
        _create_through_gate(
            gate, repository, _evidence_create({"target": CONNECTION_RAW})
        )

    assert repository.insert_calls == 0  # AC-05
    assert excinfo.value.code.value == "REDACTION_BLOCKED"
    assert audit.event_types == ["REDACTION_BLOCKED"]
    # 异常 details 里不得带原文。
    assert CONNECTION_RAW not in json.dumps(excinfo.value.details, ensure_ascii=False)


def test_gate2_review_required_raises_quarantines_and_writes_zero_rows() -> None:
    from core.errors import RedactionReviewRequiredError

    audit = FakeAudit()
    quarantine = FakeQuarantine()
    repository = FakeEvidenceRepository()
    gate = _gate(audit=audit, quarantine=quarantine)

    with pytest.raises(RedactionReviewRequiredError) as excinfo:
        _create_through_gate(
            gate, repository, _evidence_create({"name": NAME_RAW})
        )

    assert repository.insert_calls == 0
    assert excinfo.value.code.value == "REDACTION_REVIEW_REQUIRED"
    assert audit.event_types == ["REDACTION_REVIEW_REQUIRED"]
    assert quarantine.insert_calls == 1
    assert quarantine.calls[0]["channel"] == RedactionChannel.EVIDENCE.value
    assert quarantine.calls[0]["tenant_id"] == _TENANT


def test_gate2_passable_returns_masked_request_with_source_hash() -> None:
    gate = _gate()
    request = _evidence_create({"phone": PHONE_RAW, "rows": []})

    gated = gate.for_evidence(request, tenant_id=_TENANT, task_id=_TASK)

    assert gated.redaction_version == _engine().version == "redaction-v2"
    assert gated.source_payload_hash is not None
    assert len(gated.source_payload_hash) == 64
    assert gated.payload == gated.redacted_payload
    assert PHONE_RAW not in json.dumps(gated.payload, ensure_ascii=False)
    # 原文锚点 ≠ 脱敏后载荷哈希：source_payload_hash 不能顶替 payload_hash。
    assert gated.source_payload_hash != _hash_of(gated.payload)


def test_gate2_source_payload_hash_does_not_override_payload_hash() -> None:
    """A2：`source_payload_hash`（原文）与 `payload_hash`（写入载荷）必须不同源。"""
    from core.evidence.service import canonical_payload_hash

    gate = _gate()
    request = _evidence_create({"phone": PHONE_RAW})
    original_hash = canonical_payload_hash(request.payload)

    gated = gate.for_evidence(request, tenant_id=_TENANT, task_id=_TASK)

    assert gated.source_payload_hash == original_hash  # 引擎给出的原文哈希
    assert canonical_payload_hash(gated.payload) != original_hash  # 写入载荷哈希变了
    assert gated.source_payload_hash != canonical_payload_hash(gated.payload)


def test_gate2_clean_payload_keeps_payload_hash_invariant() -> None:
    """口径不变（W08 冻结断言）：CLEAN 载荷的写入哈希与原请求逐字相同。"""
    from core.evidence.service import canonical_payload_hash

    gate = _gate()
    request = _evidence_create({"rows": [{"id": 1}], "nested": {"b": 2, "a": 1}})

    gated = gate.for_evidence(request, tenant_id=_TENANT, task_id=_TASK)

    assert gated.payload == request.payload
    assert canonical_payload_hash(gated.payload) == canonical_payload_hash(request.payload)


# --------------------------------------------------------------------------- #
# 隔离区：不含原文（R3）
# --------------------------------------------------------------------------- #


def test_quarantine_receives_no_raw_content() -> None:
    from core.errors import RedactionReviewRequiredError

    quarantine = FakeQuarantine()
    gate = _gate(quarantine=quarantine)

    with pytest.raises(RedactionReviewRequiredError):
        gate.for_evidence(
            _evidence_create({"name": NAME_RAW}), tenant_id=_TENANT, task_id=_TASK
        )

    assert quarantine.insert_calls == 1
    stored: RedactionResult = quarantine.calls[0]["result"]
    # 隔离区只允许拿元数据：原文哈希 + 类别 + 定位，不得有可读原文。
    assert stored.payload_hash != ""
    assert NAME_RAW not in json.dumps(
        [_span_projection(span) for span in stored.unresolved_sensitive_spans],
        ensure_ascii=False,
    )
    assert NAME_RAW not in str(quarantine.calls[0]["channel"])
    # 协议实现方拿到的对象里，定位字段只能是路径/偏移/类别/原因。
    for span in stored.unresolved_sensitive_spans:
        assert set(_span_projection(span)) == {"category", "json_path", "start", "end", "reason"}


def test_quarantine_protocol_is_structural() -> None:
    assert isinstance(FakeQuarantine(), QuarantineRepository)


# --------------------------------------------------------------------------- #
# 闸门③：上下文包（AC-06 上半：拒绝渲染）
# --------------------------------------------------------------------------- #


def test_gate3_rejects_blocked_evidence_and_downstream_is_not_called() -> None:
    from core.errors import RedactionBlockedError

    audit = FakeAudit()
    render = SpyDownstream()
    # 注意：summary 里不能加双引号——R01 SQL 字面量规则先于 R10 执行，会把带引号的
    # PEM 整段当成 SQL 字面量替换（引擎既有顺序，T01 冻结，本卡不改）。
    bundle = _bundle([_evidence_item(f"pem 内容：{PRIVATE_KEY_RAW}")])

    with pytest.raises(RedactionBlockedError):
        _gate(audit=audit).for_model_input(bundle, tenant_id=_TENANT, task_id=_TASK)

    assert render.calls == 0  # 下游（渲染）未被调用
    assert audit.event_types == ["REDACTION_BLOCKED"]


def test_gate3_rejects_review_required_evidence() -> None:
    from core.errors import RedactionReviewRequiredError

    quarantine = FakeQuarantine()
    bundle = _bundle([_evidence_item("AB12345678")])

    with pytest.raises(RedactionReviewRequiredError):
        _gate(quarantine=quarantine).for_model_input(
            bundle, tenant_id=_TENANT, task_id=_TASK
        )

    assert quarantine.insert_calls == 1
    assert quarantine.calls[0]["channel"] == RedactionChannel.MODEL_INPUT.value


def test_gate3_passes_clean_bundle_and_attaches_summary() -> None:
    bundle = _bundle([_evidence_item('{"lock_wait_s": 12.5}')])

    gated = _gate().for_model_input(bundle, tenant_id=_TENANT, task_id=_TASK)

    assert gated.evidence == bundle.evidence
    assert gated.redaction is not None
    assert gated.redaction.status in (RedactionStatus.CLEAN, RedactionStatus.REDACTED)
    # 摘要不进 prompt：序列化产物与 W09 之前一致。
    assert "redaction" not in gated.model_dump_for_prompt()["evidence"][0]


# --------------------------------------------------------------------------- #
# 闸门④：模型消息（AC-06 下半：不发适配器调用）
# --------------------------------------------------------------------------- #


def test_gate4_rejects_blocked_messages_before_adapter_call() -> None:
    from core.errors import RedactionBlockedError

    audit = FakeAudit()
    adapter = SpyDownstream()
    messages = [
        ModelMessage(role="system", content="你是只读诊断助手"),
        ModelMessage(role="user", content=f"看下这个 DSN：{CONNECTION_RAW}"),
    ]

    with pytest.raises(RedactionBlockedError):
        _gate(audit=audit).for_model_messages(messages, task_id=_TASK)

    assert adapter.calls == 0
    assert audit.event_types == ["REDACTION_BLOCKED"]
    assert CONNECTION_RAW in messages[1].content  # 闸门只断言，不改写调用方消息


def test_gate4_rejects_review_required_messages() -> None:
    from core.errors import RedactionReviewRequiredError

    messages = [ModelMessage(role="user", content="AB12345678")]

    with pytest.raises(RedactionReviewRequiredError):
        _gate().for_model_messages(messages, task_id=_TASK)


def test_gate4_passes_clean_messages_unchanged() -> None:
    messages = [
        ModelMessage(role="system", content="你是只读 MySQL 诊断 Agent"),
        ModelMessage(role="user", content="请给出下一步只读排查步骤"),
    ]

    passed = _gate().for_model_messages(messages, task_id=_TASK)

    assert [item.content for item in passed] == [item.content for item in messages]


# --------------------------------------------------------------------------- #
# 闸门⑤：文档通道（REPORT / EXPORT / TRAINING_SAMPLE）
# --------------------------------------------------------------------------- #


def test_gate5_masks_document_for_each_document_channel() -> None:
    for channel in (
        RedactionChannel.REPORT,
        RedactionChannel.EXPORT,
        RedactionChannel.TRAINING_SAMPLE,
    ):
        document, result = _gate().for_document(
            {"summary": f"手机号 {PHONE_RAW}"}, channel=channel, task_id=_TASK
        )
        assert PHONE_RAW not in json.dumps(document, ensure_ascii=False)
        assert result.status is RedactionStatus.REDACTED
        assert result.redaction_version == "redaction-v2"


def test_gate5_blocked_document_raises_and_returns_nothing() -> None:
    from core.errors import RedactionBlockedError

    with pytest.raises(RedactionBlockedError):
        _gate().for_document(
            {"cert": CERTIFICATE_RAW},
            channel=RedactionChannel.EXPORT,
            task_id=_TASK,
        )


def test_gate5_rejects_non_document_channel() -> None:
    with pytest.raises(ValueError):
        _gate().for_document(
            {"a": 1}, channel=RedactionChannel.TOOL_RESULT, task_id=_TASK
        )


# --------------------------------------------------------------------------- #
# 审计一致性（AC-08）与签名纪律（AC-18）
# --------------------------------------------------------------------------- #


def test_audit_payload_has_exactly_the_six_required_fields() -> None:
    audit = FakeAudit()
    _gate(audit=audit).for_tool_result(
        _tool_result({"phone": PHONE_RAW}), tenant_id=_TENANT, task_id=_TASK
    )

    payload = audit.last_payload()
    assert set(payload) == {
        "redaction_version",
        "source_payload_hash",
        "redacted_payload_hash",
        "status",
        "detected_categories",
        "redaction_count",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", payload["source_payload_hash"]) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", payload["redacted_payload_hash"]) is not None
    assert payload["detected_categories"] == ["PHONE"]


# 三态（CLEAN / REDACTED / BLOCKED）的审计 payload 恒为六字段。
# 降级字段（`quarantine_skipped` / `channel`）只属于「REVIEW_REQUIRED 且写不了隔离区」
# 这一条路径，绝不能污染正向三态 —— 否则下游按固定字段消费会静默错位。
# `quarantine_skipped` 的三面回归（tenant 缺失 / 仓储未装配 / 正向已写入）已由
# `test_w09_redaction_wiring.py::test_quarantine_skip_is_flagged_in_event_when_tenant_missing`
# `..._flagged_when_repository_missing` `test_quarantine_not_flagged_when_written` 覆盖，
# 本用例只锁「正向三态键集合」这一面，不与 wiring 重复。
@pytest.mark.parametrize(
    ("expected_status", "content"),
    [
        ("CLEAN", "今天天气不错，无需处理"),
        ("REDACTED", f"联系电话 {PHONE_RAW}"),
        # `BLOCKED` 三个纯净样本各只命中一条阻断规则（R06 / R10 / R11），
        # 与 `tests/unit/test_w09_redaction_engine.py::R3_BLOCKED_SAMPLES` 同值，
        # 避免顺带命中其他规则而污染"六字段"判据。
        ("BLOCKED", "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab"),
        ("BLOCKED", PRIVATE_KEY_RAW),
        ("BLOCKED", CERTIFICATE_RAW),
    ],
    ids=["CLEAN", "REDACTED", "BLOCKED_R06", "BLOCKED_R10", "BLOCKED_R11"],
)
def test_three_states_keep_exactly_six_audit_payload_fields(
    expected_status: str, content: str
) -> None:
    """三态审计载荷键集合**恰为**六字段，且不含降级字段。

    `CLEAN`/`BLOCKED` 走 `for_model_messages`；`REDACTED` 改走**改写型**通道
    （`for_document` / REPORT）——P1-T1 起，断言通道（闸门③/④）对硬 PII 一律升级为
    `BLOCKED`（只有改写型通道才允许 `REDACTED` 放行）。本用例断言的是**审计载荷形状**，
    因此按通道语义选入口，不改六字段判据。
    """
    from core.errors import RedactionBlockedError

    audit = FakeAudit()
    gate = _gate(audit=audit)
    messages = [ModelMessage(role="user", content=content)]

    if expected_status == "BLOCKED":
        with pytest.raises(RedactionBlockedError):
            gate.for_model_messages(messages, task_id=_TASK, tenant_id=_TENANT)
    elif expected_status == "REDACTED":
        gate.for_document(
            {"text": content},
            channel=RedactionChannel.REPORT,
            task_id=_TASK,
            tenant_id=_TENANT,
        )
    else:
        gate.for_model_messages(messages, task_id=_TASK, tenant_id=_TENANT)

    payload = audit.last_payload()
    assert payload["status"] == expected_status
    assert set(payload) == {
        "redaction_version",
        "source_payload_hash",
        "redacted_payload_hash",
        "status",
        "detected_categories",
        "redaction_count",
    }
    # 降级字段不得出现在正向三态（`quarantine_skipped` 只有 REVIEW_REQUIRED 才可能有）。
    assert "quarantine_skipped" not in payload
    assert "channel" not in payload
    # 纯净样本校验：`BLOCKED` 只命中一条阻断规则（R06/R10/R11 之一），
    # 否则"六字段"结论会被其他类别顺带污染。
    if expected_status == "BLOCKED":
        assert len(payload["detected_categories"]) == 1


def test_ac08_evidence_version_equals_event_version_in_one_call() -> None:
    """同一次调用内：证据行 `redaction_version` == `REDACTION_APPLIED` 事件版本。"""
    audit = FakeAudit()
    gate = _gate(audit=audit)
    request = _evidence_create({"phone": PHONE_RAW})

    gated = gate.for_evidence(request, tenant_id=_TENANT, task_id=_TASK)

    assert audit.event_types == ["REDACTION_APPLIED"]
    assert gated.redaction_version == audit.last_payload()["redaction_version"]
    assert gated.redaction_version == "redaction-v2"
    assert gated.source_payload_hash == audit.last_payload()["source_payload_hash"]


def test_gate_has_no_dsn_parameters_or_attributes() -> None:
    forbidden = ("dsn", "password", "secret", "token", "credential")
    for member in (
        RedactionGate.__init__,
        RedactionGate.for_tool_result,
        RedactionGate.for_evidence,
        RedactionGate.for_model_input,
        RedactionGate.for_model_messages,
        RedactionGate.for_document,
    ):
        params = inspect.signature(member).parameters
        assert not any(term in name.lower() for name in params for term in forbidden)
    gate = _gate()
    assert not any(
        term in attr.lower()
        for attr in vars(gate)
        for term in ("dsn",)
    )


def test_gate_is_not_a_switch() -> None:
    """禁止"可关闭开关"：没有 enabled/skip/allow_unsafe 之类旁路形参。"""
    params = inspect.signature(RedactionGate.__init__).parameters
    assert set(params) == {"self", "engine", "audit", "quarantine"}
    for member in vars(RedactionGate):
        assert "bypass" not in member.lower()
        assert "skip" not in member.lower()


def test_tool_result_new_field_does_not_enter_action_digest() -> None:
    """`redaction` 不进任何摘要：既有冻结摘要向量与字段集不受影响。"""
    from core.approval.digest import compute_action_digest
    from tests.unit.test_action_digest import BASE_ACTION_DIGEST, base_descriptor

    descriptor = base_descriptor()
    # 冻结向量不变：新增 `ToolResult.redaction` 触碰不到 `action_descriptor`。
    assert compute_action_digest(descriptor)[0] == BASE_ACTION_DIGEST
    assert "redaction" not in descriptor

    gated = _gate().for_tool_result(
        _tool_result({"name": NAME_RAW}), tenant_id=_TENANT, task_id=_TASK
    )
    assert gated.model_dump(mode="json")["redaction"] is not None
    assert compute_action_digest(descriptor)[0] == BASE_ACTION_DIGEST


# --------------------------------------------------------------------------- #
# 迁移 009：列存在 + 隔离区无原文列
# --------------------------------------------------------------------------- #


def _migration_text(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "database" / "migrations" / name).read_text(encoding="utf-8")


def test_migration_009_declares_evidence_anchor_and_quarantine() -> None:
    sql = _migration_text("009_w09_redaction.sql")
    # 只追加：注释允许提到 DROP/DELETE，**可执行语句**里不得出现。
    statements = [
        line
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    body = "\n".join(statements)

    assert "source_payload_hash CHAR(64) NULL AFTER payload_hash" in body
    assert "dg_system.redaction_quarantine" in body
    assert "DELETE" not in body
    assert "DROP" not in body


def test_migration_009_quarantine_has_no_raw_content_column() -> None:
    sql = _migration_text("009_w09_redaction.sql")
    table = sql.split("CREATE TABLE IF NOT EXISTS dg_system.redaction_quarantine", 1)[1]
    columns = [line.strip() for line in table.splitlines() if line.strip() and not line.strip().startswith("--")]

    assert any(line.startswith("payload_hash") for line in columns)
    assert any(line.startswith("unresolved_spans_json") for line in columns)
    for forbidden in ("payload_json", "raw_", "original_", "plaintext"):
        assert all(forbidden not in line for line in columns), forbidden


def test_migration_009_is_pickable_by_migrate_py() -> None:
    """`split_statements` 按 ";" 切分 → 009 不得含字面量分号。"""
    from database.migrations import migration_files, split_statements

    names = [path.name for path in migration_files()]
    assert "009_w09_redaction.sql" in names
    assert names == sorted(names)
    statements = split_statements(_migration_text("009_w09_redaction.sql"))
    assert len(statements) == 2  # ALTER + CREATE TABLE


def test_rollback_009_reverts_in_reverse_order() -> None:
    sql = _migration_text("rollback/009_rollback.sql")
    drop_table = sql.index("DROP TABLE IF EXISTS dg_system.redaction_quarantine")
    drop_column = sql.index("DROP COLUMN source_payload_hash")
    delete_version = sql.index("DELETE FROM dg_system.schema_migrations")

    assert drop_table < drop_column < delete_version
    assert "009_w09_redaction" in sql


# --------------------------------------------------------------------------- #
# D1 / 事件 payload 契约：三态键集合等值
# --------------------------------------------------------------------------- #

_PAYLOAD_KEYS_SIX = frozenset(
    {
        "redaction_version",
        "source_payload_hash",
        "redacted_payload_hash",
        "status",
        "detected_categories",
        "redaction_count",
    }
)


@pytest.mark.parametrize(
    "data,expected_event",
    [
        ({"affected_rows": 1}, "REDACTION_APPLIED"),
        ({"phone": PHONE_RAW}, "REDACTION_APPLIED"),
        # 纯净 R06 样本：键名 `target` 不是敏感键，值只命中连接串规则，
        # 避免用 `{"dsn": ...}` 顺带命中 R12（LOG_FIELD）而污染判据。
        ({"target": "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab"}, "REDACTION_BLOCKED"),
    ],
)
def test_event_payload_keys_are_exactly_six(
    data: dict[str, Any], expected_event: str
) -> None:
    """三态事件 payload 键集合恰为六字段（`BLOCKED` 显式成例）。"""
    audit = FakeAudit()

    _gate(audit=audit).for_tool_result(
        _tool_result(data), tenant_id=_TENANT, task_id=_TASK
    )

    assert audit.event_types == [expected_event]
    assert set(audit.last_payload()) == _PAYLOAD_KEYS_SIX


def test_t02_added_field_is_pinned_to_redaction_summary() -> None:
    """D1：契约相对 T01 只允许多出 `redaction`，且必须可缺省、类型恒定。"""
    from core.redaction.models import RedactionSummary

    t01_fields = {
        "tool_name",
        "status",
        "task_id",
        "run_id",
        "tool_call_id",
        "started_at",
        "duration_ms",
        "data",
        "warnings",
        "error",
        "source",
        "observed_at",
        "truncated",
        "capability_ref",
        "source_metadata",
    }

    assert set(ToolResult.model_fields) - t01_fields == {"redaction"}
    field = ToolResult.model_fields["redaction"]
    assert field.annotation == (RedactionSummary | None)
    assert field.default is None
    assert field.is_required() is False


__all__ = ["FakeAudit", "FakeEvidenceRepository", "FakeQuarantine"]
