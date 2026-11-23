"""W09 T02b 接线单元测试：闸门①②与组合根（全离线，不连库）。

验证"接线真的生效"，而不是只有闸门单测绿：

- `ToolExecutor.execute` 内部**恰好一次**调用闸门①，且 `ToolStatus` 不变；
- `EvidenceService.create` 在哈希计算之前调用闸门②，`BLOCKED`/`REVIEW_REQUIRED`
  时 `insert_calls == 0`（AC-05）；
- `to_evidence_create` 已换成真脱敏（`payload == redacted_payload == 引擎产物`，
  `source_payload_hash` 是原文锚点、`redaction_version` 是 `redaction-v2`）；
- 闸门**恒开**：不注入也算得出脱敏结果（引擎无状态默认构造，无配置项可关）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.context.builder import ContextBuilder
from core.context.contracts import ContextBundle
from core.diagnosis.evidence_mapping import REDACTION_VERSION, to_evidence_create
from core.errors import RedactionBlockedError, RedactionReviewRequiredError
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCreate,
)
from core.evidence.service import EvidenceService, canonical_payload_hash
from core.models.contracts import ModelMessage
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
)
from core.tasks.models import TaskEvent
from core.tools.contracts import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSource,
    ToolStatus,
)
from core.tools.executor import ToolExecutor

PHONE_RAW = "13812345678"
NAME_RAW = "张三"
CONNECTION_RAW = "mysql://user:pass@host.internal:3306/db"
PRIVATE_KEY_RAW = "-----BEGIN RSA PRIVATE KEY-----MIIBOgIBAAJBAK-----END RSA PRIVATE KEY-----"

_TENANT = "tenant_w09"
_TASK = "task_w09"
_ENV = "env_w09"
_OBSERVED = "2026-03-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------------- #


class _FakeClient:
    """`ToolContext.client` 的最小替身（只读连接、带身份事实）。"""

    read_only = True
    database_id = "lab_unit"

    def identity(self) -> Any:
        return SimpleNamespace(server_version="8.0.46", user="dg_ro")


class _FakeTool:
    def __init__(self, *, data: dict[str, Any], status: ToolStatus = ToolStatus.SUCCESS) -> None:
        self._data = data
        self._status = status
        self.definition = ToolDefinition(
            name="stub_read",
            description="测试用只读工具",
            risk_level="R0",
            side_effect="read",
            permissions=["mysql:read"],
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

    def run(self, context: ToolContext, params: dict[str, Any] | None = None) -> ToolResult:
        return ToolResult(
            tool_name=self.definition.name,
            status=self._status,
            task_id=context.task_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            started_at=_OBSERVED,
            duration_ms=1.0,
            data=dict(self._data),
            source=ToolSource(
                database_id="lab_unit", db_version="8.0.46", collected_at=_OBSERVED
            ),
            observed_at=_OBSERVED,
        )


class _FakeRegistry:
    def __init__(self, tool: _FakeTool) -> None:
        self._tool = tool

    def get(self, name: str) -> _FakeTool:
        return self._tool


class _SpyGate(RedactionGate):
    """记录调用次数的闸门替身（用来证明"接线真的走到闸门"）。"""

    def __init__(self, engine: DataRedactionEngine) -> None:
        super().__init__(engine)
        self.calls: list[tuple[str, str]] = []

    def for_tool_result(self, result: ToolResult, *, tenant_id: str, task_id: str) -> ToolResult:
        self.calls.append((tenant_id, task_id))
        return super().for_tool_result(result, tenant_id=tenant_id, task_id=task_id)


class _FakeTaskRepository:
    def __init__(self, environment_id: str = _ENV) -> None:
        self._environment_id = environment_id

    def task_belongs_to(self, task_id: str, tenant_id: str) -> bool:
        return True

    def environment_belongs_to(self, environment_id: str, tenant_id: str) -> bool:
        return True

    def get_task(self, task_id: str, *, tenant_id: str | None = None) -> Any:
        return SimpleNamespace(
            task_id=task_id, tenant_id=tenant_id, environment_id=self._environment_id
        )


class _CountingEvidenceRepository:
    def __init__(self) -> None:
        self.rows: list[Evidence] = []

    def insert(self, evidence: Evidence) -> Evidence:
        self.rows.append(evidence)
        return evidence

    @property
    def insert_calls(self) -> int:
        return len(self.rows)


class FakeAudit:
    """`AuditService.emit_required(event)` 的离线替身。"""

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
    """`QuarantineRepository` 替身（只数调用，不落库）。"""

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


# --------------------------------------------------------------------------- #
# 闸门①：executor 接线
# --------------------------------------------------------------------------- #


def _execute(gate: RedactionGate | None, tool: _FakeTool) -> tuple[ToolResult, _SpyGate | None]:
    executor = ToolExecutor(
        _FakeRegistry(tool),  # type: ignore[arg-type]
        None,
        redaction_gate=gate,
    )
    spy = gate if isinstance(gate, _SpyGate) else None
    task = SimpleNamespace(task_id=_TASK, run_id="run_w09", tenant_id=_TENANT)
    result = executor.execute(
        tool.definition.name,
        {},
        ToolContext(client=_FakeClient(), task_id=_TASK),  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
    )
    return result, spy


def test_executor_calls_gate_once_and_masks_data() -> None:
    spy = _SpyGate(DataRedactionEngine())
    result, _ = _execute(spy, _FakeTool(data={"phone": PHONE_RAW, "n": 1}))

    assert len(spy.calls) == 1  # 有且仅有一处调用
    assert spy.calls[0] == (_TENANT, _TASK)
    assert result.status is ToolStatus.SUCCESS  # ToolStatus 不动
    assert PHONE_RAW not in json.dumps(result.data, ensure_ascii=False)
    assert result.data["n"] == 1
    assert result.redaction is not None
    assert result.redaction.redaction_version == "redaction-v2"


def test_executor_default_gate_is_always_on() -> None:
    """不注入闸门也算得出脱敏结果（恒开，不是开关）。"""
    result, _ = _execute(None, _FakeTool(data={"phone": PHONE_RAW}))

    assert result.redaction is not None
    assert PHONE_RAW not in json.dumps(result.data, ensure_ascii=False)


def test_executor_blocked_payload_clears_data_but_keeps_status() -> None:
    result, _ = _execute(None, _FakeTool(data={"target": CONNECTION_RAW}))

    assert result.status is ToolStatus.SUCCESS
    assert result.data == {}
    assert result.truncated is True
    assert result.redaction is not None
    assert result.redaction.status.value == "BLOCKED"
    assert CONNECTION_RAW not in json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


def test_executor_review_required_payload_clears_data_but_keeps_status() -> None:
    result, _ = _execute(None, _FakeTool(data={"name": NAME_RAW}))

    assert result.status is ToolStatus.SUCCESS
    assert result.data == {}
    assert result.truncated is True
    assert result.redaction is not None
    assert result.redaction.status.value == "REVIEW_REQUIRED"
    assert NAME_RAW not in json.dumps(result.model_dump(mode="json"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 闸门②：EvidenceService.create 接线
# --------------------------------------------------------------------------- #


def _service(gate: RedactionGate | None = None) -> tuple[EvidenceService, _CountingEvidenceRepository]:
    repository = _CountingEvidenceRepository()
    service = EvidenceService(
        repository,  # type: ignore[arg-type]
        _FakeTaskRepository(),  # type: ignore[arg-type]
        redaction_gate=gate,
    )
    return service, repository


def _request(payload: dict[str, Any], **overrides: Any) -> EvidenceCreate:
    values: dict[str, Any] = {
        "tenant_id": _TENANT,
        "task_id": _TASK,
        "environment_id": _ENV,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-unit",
        "source_tool": "get_locks",
        "observed_at": _OBSERVED,
        "collection_status": CollectionStatus.SUCCESS,
        "payload": payload,
    }
    values.update(overrides)
    return EvidenceCreate(**values)


def test_service_blocked_payload_raises_and_inserts_zero_rows() -> None:
    service, repository = _service()

    with pytest.raises(RedactionBlockedError):
        service.create(_request({"target": CONNECTION_RAW}))

    assert repository.insert_calls == 0  # AC-05


def test_service_review_required_payload_raises_and_inserts_zero_rows() -> None:
    service, repository = _service()

    with pytest.raises(RedactionReviewRequiredError):
        service.create(_request({"name": NAME_RAW}))

    assert repository.insert_calls == 0


def test_service_masks_payload_and_records_source_anchor() -> None:
    service, repository = _service()
    original = {"phone": PHONE_RAW, "rows": []}

    stored = service.create(_request(original))

    assert repository.insert_calls == 1
    assert PHONE_RAW not in json.dumps(stored.payload, ensure_ascii=False)
    assert stored.redaction_version == "redaction-v2"
    # A2：payload_hash = 写入载荷哈希；source_payload_hash = 原文锚点，两者不同源。
    assert stored.payload_hash == canonical_payload_hash(stored.payload)
    assert stored.source_payload_hash == canonical_payload_hash(original)
    assert stored.source_payload_hash != stored.payload_hash


def test_service_clean_payload_keeps_hash_invariant() -> None:
    """W08 冻结口径：CLEAN 载荷的 `payload_hash == canonical_payload_hash(payload)`。"""
    service, repository = _service()
    original = {"rows": [{"id": 1}], "nested": {"b": 2, "a": 1}}

    stored = service.create(_request(original))

    assert stored.payload == original
    assert stored.payload_hash == canonical_payload_hash(original)
    assert stored.source_payload_hash == canonical_payload_hash(original)


# --------------------------------------------------------------------------- #
# 真脱敏：to_evidence_create
# --------------------------------------------------------------------------- #


def _tool_result(data: dict[str, Any], *, status: ToolStatus = ToolStatus.SUCCESS) -> ToolResult:
    return ToolResult(
        tool_name="get_locks",
        status=status,
        started_at=_OBSERVED,
        duration_ms=2.0,
        data=data,
        source=ToolSource(database_id="lab_unit", db_version="8.0.46", collected_at=_OBSERVED),
        observed_at=_OBSERVED,
        capability_ref="mysql:1.0:unit",
        source_metadata={"adapter": "unit"},
    )


def test_evidence_mapping_uses_engine_product_not_deepcopy() -> None:
    original = _tool_result({"phone": PHONE_RAW})

    request = to_evidence_create(
        original, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.payload == request.redacted_payload
    assert request.payload["data"]["phone"] != PHONE_RAW
    assert request.source_payload_hash is not None
    assert request.source_payload_hash != canonical_payload_hash(request.payload)
    assert request.redaction_version == REDACTION_VERSION == "redaction-v2"


def test_evidence_mapping_clean_payload_is_unchanged_and_has_anchor() -> None:
    original = _tool_result({"tables": [], "available": True})

    request = to_evidence_create(
        original, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )

    assert request.payload["data"] == original.data
    assert request.payload == request.redacted_payload
    # 无敏感命中时原文哈希 == 写入哈希（同一份内容），但仍各占一格。
    assert request.source_payload_hash == canonical_payload_hash(request.payload)


def test_evidence_mapping_blocked_payload_raises() -> None:
    original = _tool_result({"target": CONNECTION_RAW})

    with pytest.raises(RedactionBlockedError):
        to_evidence_create(
            original, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
        )


def test_evidence_mapping_accepts_injected_gate() -> None:
    """组合根注入闸门（带 audit/quarantine）时走注入实例。"""
    spy = _SpyGate(DataRedactionEngine())
    original = _tool_result({"phone": PHONE_RAW})

    request = to_evidence_create(
        original, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV, gate=spy
    )

    assert request.redaction_version == "redaction-v2"
    assert spy.calls == []  # 走的是 for_evidence，不是闸门①
    assert PHONE_RAW not in json.dumps(request.payload, ensure_ascii=False)


def test_two_stage_chain_preserves_original_anchor() -> None:
    """mapping（引擎产物）→ 闸门②复查：原文锚点不得被二次扫描覆盖。"""
    original = _tool_result({"phone": PHONE_RAW})
    request = to_evidence_create(
        original, tenant_id=_TENANT, task_id=_TASK, environment_id=_ENV
    )
    anchor = request.source_payload_hash

    service, _ = _service()
    stored = service.create(request)

    assert stored.source_payload_hash == anchor
    assert stored.redaction_version == "redaction-v2"
    assert PHONE_RAW not in json.dumps(stored.payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 闸门③：ContextBuilder.render_messages（未通过不产出任何消息）
# --------------------------------------------------------------------------- #


def _builder(gate: RedactionGate | None = None) -> ContextBuilder:
    from core.tools.registry import ToolRegistry

    return ContextBuilder(ToolRegistry(), redaction_gate=gate)


def _context_bundle(items: list[Any]) -> ContextBundle:
    return ContextBundle(
        goal="定位锁等待",
        database_id="lab_unit",
        db_version="8.0.46",
        db_user="dg_ro",
        evidence=items,
    )


def _context_item(summary: str, *, redaction: Any = None) -> Any:
    from core.context.contracts import EvidenceItem

    return EvidenceItem(
        evidence_id="ev_ctx_1",
        tool_name="get_locks",
        status="success",
        summary=summary,
        database_id="lab_unit",
        db_version="8.0.46",
        collected_at=_OBSERVED,
        data_digest="d" * 64,
        redaction=redaction,
    )


def test_gate3_context_builder_refuses_to_render_blocked_summary() -> None:
    bundle = _context_bundle([_context_item(f"pem：{PRIVATE_KEY_RAW}")])

    with pytest.raises(RedactionBlockedError):
        _builder().render_messages(bundle, "系统安全提示")


def test_gate3_context_builder_rejects_item_flagged_blocked_upstream() -> None:
    """上游已判 BLOCKED 的锚点：即使摘要在复扫时是干净的，也禁止渲染。"""
    engine = DataRedactionEngine()
    blocked = engine.redact({"t": CONNECTION_RAW}, channel=RedactionChannel.EVIDENCE)
    assert blocked.status is RedactionStatus.BLOCKED
    summary = engine.summary(blocked)

    bundle = _context_bundle([_context_item("锁等待 12ms", redaction=summary)])

    with pytest.raises(RedactionBlockedError):
        _builder().render_messages(bundle, "系统安全提示")


def test_gate3_context_builder_renders_clean_bundle() -> None:
    bundle = _context_bundle([_context_item('{"lock_wait_s": 12.5}')])

    messages = _builder().render_messages(
        bundle, "系统安全提示", tenant_id=_TENANT, task_id=_TASK
    )

    assert [str(message.role) for message in messages] == ["system", "user"]
    assert "lock_wait_s" in messages[1].content
    assert "redaction" not in messages[1].content  # 摘要不进 prompt


def test_gate3_context_builder_masks_nothing_but_rejects_review_required() -> None:
    bundle = _context_bundle([_context_item("AB12345678")])

    with pytest.raises(RedactionReviewRequiredError):
        _builder().render_messages(bundle, "系统安全提示")


# --------------------------------------------------------------------------- #
# 闸门④：ModelGateway（不通过不调用适配器）
# --------------------------------------------------------------------------- #


def _gateway(gate: RedactionGate | None = None) -> tuple[Any, Any]:
    from core.models.adapters.fake import FakeModelAdapter
    from core.models.gateway import ModelGateway

    adapter = FakeModelAdapter(["hello"])
    return ModelGateway(adapter, redaction_gate=gate), adapter


def test_gate4_gateway_blocks_before_adapter_and_emits_event() -> None:
    audit = FakeAudit()
    gateway, adapter = _gateway(RedactionGate(DataRedactionEngine(), audit=audit))
    messages = [
        ModelMessage(role="system", content="你是只读 MySQL 诊断 Agent"),
        ModelMessage(role="user", content=f"看下这个 DSN：{CONNECTION_RAW}"),
    ]

    with pytest.raises(RedactionBlockedError):
        gateway.generate(messages, metadata={"task_id": _TASK, "tenant_id": _TENANT})

    assert adapter.requests == []  # 未调用适配器（不发 HTTP）
    assert audit.event_types == ["REDACTION_BLOCKED"]
    assert audit.last_payload()["status"] == "BLOCKED"


def test_gate4_gateway_generate_structured_also_checks() -> None:
    from pydantic import BaseModel

    class _Answer(BaseModel):
        value: int

    gateway, adapter = _gateway()
    messages = [ModelMessage(role="user", content=f"pem：{PRIVATE_KEY_RAW}")]

    with pytest.raises(RedactionBlockedError):
        gateway.generate_structured(messages, _Answer, metadata={"task_id": _TASK})

    assert adapter.requests == []


def test_gate4_gateway_allows_clean_messages() -> None:
    gateway, adapter = _gateway()

    response = gateway.generate(
        [ModelMessage(role="user", content="请给出下一步只读排查步骤")],
        metadata={"task_id": _TASK},
    )

    assert response.text == "hello"
    assert len(adapter.requests) == 1


# --------------------------------------------------------------------------- #
# 轨迹通道：BLOCKED 置占位，其余保持审计链路口径
# --------------------------------------------------------------------------- #


def test_trajectory_blocked_event_carries_no_raw_content() -> None:
    from core.agent.trajectory import TrajectoryRecorder

    recorder = TrajectoryRecorder(task_id=_TASK, run_id="run_w09", goal="g")

    event = recorder.record("tool_result", {"target": CONNECTION_RAW, "n": 1})

    assert event.payload == {"redaction": "BLOCKED"}
    assert CONNECTION_RAW not in json.dumps(event.payload, ensure_ascii=False)


def test_trajectory_clean_event_keeps_audit_redaction_semantics() -> None:
    """`redact_payload` 口径不变（审计链路不动），只是额外扫一次判 BLOCKED。"""
    from core.agent.trajectory import TrajectoryRecorder

    recorder = TrajectoryRecorder(task_id=_TASK, run_id="run_w09", goal="g")

    event = recorder.record("model_call", {"purpose": "plan", "password": "secret-value"})

    assert event.payload["password"] == "<redacted>"
    assert event.payload["purpose"] == "plan"


# --------------------------------------------------------------------------- #
# 隔离区静默跳过修复：`quarantine_skipped` 可被查询
# --------------------------------------------------------------------------- #


def test_quarantine_skip_is_flagged_in_event_when_tenant_missing() -> None:
    audit = FakeAudit()
    quarantine = FakeQuarantine()
    gate = RedactionGate(DataRedactionEngine(), audit=audit, quarantine=quarantine)

    # `for_document` 不接收 tenant_id：REVIEW_REQUIRED 时不能静默不写隔离区。
    gate.for_document({"name": NAME_RAW}, channel=RedactionChannel.REPORT, task_id=_TASK)

    assert quarantine.insert_calls == 0
    payload = audit.last_payload()
    assert payload["quarantine_skipped"] is True
    assert payload["channel"] == RedactionChannel.REPORT.value
    assert audit.event_types == ["REDACTION_REVIEW_REQUIRED"]


def test_quarantine_skip_flagged_when_repository_missing() -> None:
    audit = FakeAudit()
    gate = RedactionGate(DataRedactionEngine(), audit=audit)

    with pytest.raises(RedactionReviewRequiredError):
        gate.for_model_messages(
            [ModelMessage(role="user", content="AB12345678")], task_id=_TASK
        )

    assert audit.last_payload()["quarantine_skipped"] is True


def test_quarantine_not_flagged_when_written() -> None:
    audit = FakeAudit()
    quarantine = FakeQuarantine()
    gate = RedactionGate(DataRedactionEngine(), audit=audit, quarantine=quarantine)

    with pytest.raises(RedactionReviewRequiredError):
        gate.for_evidence(
            _request({"name": NAME_RAW}), tenant_id=_TENANT, task_id=_TASK
        )

    assert quarantine.insert_calls == 1
    assert "quarantine_skipped" not in audit.last_payload()


# --------------------------------------------------------------------------- #
# 阶段 D：真实 `LabOrderNoteUpdateTool` 的 data 过闸门①
# --------------------------------------------------------------------------- #

LAB_ORDER_ID = 2001
LAB_KEYS = f"order_id={LAB_ORDER_ID}"
LAB_SEED_NOTE = "sandbox-seed"


class _FakeRwClient:
    """`LabOrderNoteUpdateTool` 需要的可写连接替身（不走真实 SQL）。"""

    read_only = False
    database_id = "lab_mysql_01"

    def __init__(self, note: str) -> None:
        self._note = note

    def fetch_all(self, sql: str, params: Any) -> list[dict[str, Any]]:
        return [{"note": self._note}]

    def execute(self, sql: str, params: Any) -> int:
        self._note = params[0]
        return 1

    def identity(self) -> Any:
        return SimpleNamespace(server_version="8.0.46")


def _lab_tool_result(note: str) -> ToolResult:
    """用真实 `LabOrderNoteUpdateTool` 构造 `ToolResult`（真实契约，非合成）。"""
    from adapters.mysql.lab_repair import LabOrderNoteUpdateTool

    tool = LabOrderNoteUpdateTool()
    context = ToolContext(
        client=_FakeRwClient(LAB_SEED_NOTE),  # type: ignore[arg-type]
        task_id=_TASK,
        run_id="run_lab",
        tool_call_id="call_lab",
    )
    return tool.run(
        context,
        {
            "order_id": LAB_ORDER_ID,
            "keys": LAB_KEYS,
            "note": note,
            "expected_note": LAB_SEED_NOTE,
        },
    )


def test_stage_d_lab_repair_result_passes_gate_clean() -> None:
    """阶段 D 实测结论：真实写工具结果过闸门① = `CLEAN`，data 未被清空。"""
    raw = _lab_tool_result("w09-redaction-check")
    assert raw.status is ToolStatus.SUCCESS

    gated = _gate_for_stage_d().for_tool_result(
        raw, tenant_id=_TENANT, task_id=_TASK
    )

    assert gated.redaction is not None
    assert gated.redaction.status is RedactionStatus.CLEAN
    assert gated.status is ToolStatus.SUCCESS  # ToolStatus 不动
    assert gated.data == {
        "before": LAB_SEED_NOTE,
        "after": "w09-redaction-check",
        "affected_rows": 1,
        "idempotent_noop": False,
    }
    assert gated.truncated is False


@pytest.mark.parametrize(
    "note,expected",
    [
        ("w09-redaction-check", RedactionStatus.CLEAN),
        ("call 13812345678 back", RedactionStatus.REDACTED),
        # `^[A-Z]{2,6}\\d{6,}$` 与 R05 键名规则是**有意**的 fail-closed（KD-W09-02）：
        # 自由文本 note 若形如业务号，data 会被清空（可用性降级，不是泄露）。
        ("AB12345678", RedactionStatus.REVIEW_REQUIRED),
        ("mysql://u:p@h/db", RedactionStatus.BLOCKED),
    ],
)
def test_stage_d_lab_repair_note_matrix_keeps_tool_status(
    note: str, expected: RedactionStatus
) -> None:
    gated = _gate_for_stage_d().for_tool_result(
        _lab_tool_result(note), tenant_id=_TENANT, task_id=_TASK
    )

    assert gated.redaction is not None
    assert gated.redaction.status is expected
    assert gated.status is ToolStatus.SUCCESS  # 四种情况都不改 ToolStatus
    if expected is RedactionStatus.CLEAN:
        assert gated.data
    if expected in (RedactionStatus.REVIEW_REQUIRED, RedactionStatus.BLOCKED):
        assert gated.data == {}
        assert gated.truncated is True


def _gate_for_stage_d() -> RedactionGate:
    return RedactionGate(DataRedactionEngine())
