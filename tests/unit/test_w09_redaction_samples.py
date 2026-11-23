"""W09 T06 / AC-19 + AC-07（训练样本分支）：版本化脱敏回归样本集 + 出口复扫。

两条判据：

1. **AC-19**：五类样本（多语言 / SQL 注释 / ``\\`` 转义 / 连接串 / 嵌套 JSON）
   各 1 例，**逐例三断言**——"原始含敏感"、"脱敏后不含"、"`detected_categories` 正确"；
   样本集 `redaction_version` 非空且与引擎一致。**不允许**"只要有 1 例通过"的放水。
2. **AC-07（训练样本）**：`build_training_sample()` 对**拼接后**的样本再走
   `TRAINING_SAMPLE` 通道；`BLOCKED` / `REVIEW_REQUIRED` → `SkippedSample`（不可写、
   无载荷字段）；`CLEAN` / `REDACTED` → 样本头含 `redaction_version` 与
   `source_payload_hash`。

全离线：只用引擎与闸门的纯函数面，不连库、不依赖 `DG_ADMIN_DSN` / PG 5432。
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Sequence

import pytest

from core.agent.trajectory import Trajectory, TrajectoryEvent
from core.evidence.models import CollectionStatus, Evidence, EvidenceQuality
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import (
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
    SensitiveCategory,
)
from core.redaction.payload_text import canonical_payload_hash, canonical_payload_text
from core.report.samples import (
    MAX_SAMPLE_EVENTS,
    REASON_REDACTION_BLOCKED,
    REASON_REVIEW_REQUIRED,
    REGRESSION_SAMPLES,
    SAMPLE_SET_VERSION,
    TRAINING_SAMPLE_VERSION,
    RegressionSample,
    SkippedSample,
    build_training_sample,
)

TENANT = "tenant_local"
TASK_ID = "task_w09_samples"
PHONE = "13812345678"
CONNECTION_STRING = "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab"


def _engine() -> DataRedactionEngine:
    return DataRedactionEngine()


def _gate() -> RedactionGate:
    """无审计/无隔离区的纯闸门：本文件只验证判定与出口形状。"""
    return RedactionGate(_engine())


def _redact(sample: RegressionSample) -> RedactionResult:
    return _engine().redact(sample.payload, channel=RedactionChannel.TOOL_RESULT)


def _evidence(
    *,
    evidence_id: str = "ev_w09_samples",
    payload: dict[str, Any] | None = None,
    redacted_payload: dict[str, Any] | None = None,
    tenant_id: str = TENANT,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        task_id=TASK_ID,
        environment_id="env_mysql_local",
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-samples-v1",
        source_tool="mysql_lab_inspect_table",
        observed_at="2026-01-01T00:00:00+00:00",
        collection_status=CollectionStatus.SUCCESS,
        quality=EvidenceQuality.HIGH,
        payload=payload if payload is not None else {},
        redacted_payload=redacted_payload,
        payload_hash=canonical_payload_hash(payload or {}),
        redaction_version="redaction-v2",
        reproducible=True,
    )


def _trajectory(
    *,
    goal: str = "核对订单表备注字段是否被覆盖",
    events: Sequence[TrajectoryEvent] | None = None,
    usage: dict[str, Any] | None = None,
) -> Trajectory:
    return Trajectory(
        trajectory_id="traj_w09_samples",
        task_id=TASK_ID,
        run_id="run_w09_samples",
        goal=goal,
        model_version="model_w09_stub",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        stop_reason="DONE",
        usage=dict(usage or {}),
        events=list(events or ()),
    )


def _event(sequence: int, payload: dict[str, Any]) -> TrajectoryEvent:
    return TrajectoryEvent(
        sequence=sequence,
        kind="PLAN_STEP",
        at="2026-01-01T00:00:01+00:00",
        payload=payload,
    )


class _SpyGate:
    """记录出口扫描入参，再委托真实闸门（证明"拼接完成后"才扫描）。"""

    def __init__(self) -> None:
        self.inner = _gate()
        self.calls: list[dict[str, Any]] = []

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], RedactionResult]:
        self.calls.append(
            {
                "payload": dict(payload),
                "channel": channel,
                "task_id": task_id,
                "tenant_id": tenant_id,
            }
        )
        return self.inner.for_document(
            payload, channel=channel, task_id=task_id, tenant_id=tenant_id
        )


# ---------------------------------------------------------------------------
# AC-19：五类样本逐例三断言
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample", REGRESSION_SAMPLES, ids=[item.name for item in REGRESSION_SAMPLES]
)
def test_ac19_each_sample_hits_three_assertions(sample: RegressionSample) -> None:
    """三断言必须逐例执行，不做"任一类通过即算通过"的聚合。"""
    engine = _engine()

    # ① 原始载荷确实含敏感串（否则这条样本没有回归价值）。
    assert sample.raw_secret in canonical_payload_text(sample.payload), (
        f"{sample.name}: 样本原文不含声明的敏感串"
    )

    result = engine.redact(sample.payload, channel=RedactionChannel.TOOL_RESULT)

    # ② 脱敏后不含原串（含 JSON 转义形态也要扫不到）。
    redacted_text = canonical_payload_text(result.payload_redacted)
    assert sample.raw_secret not in redacted_text, f"{sample.name}: 原串残留"
    assert json.dumps(sample.raw_secret)[1:-1] not in redacted_text

    # ③ 命中类别与期望逐字相等（引擎按枚举声明序输出，不按命中顺序）。
    assert result.detected_categories == sample.expected_categories, sample.name
    # 样本集版本轴：每次扫描的 redaction_version 必须非空且与引擎一致。
    assert result.redaction_version
    assert result.redaction_version == engine.version


def test_ac19_sample_set_covers_exactly_the_five_named_categories() -> None:
    assert [item.name for item in REGRESSION_SAMPLES] == [
        "multilingual",
        "sql_comment",
        "backslash_escape",
        "connection_string",
        "nested_json",
    ]
    # 名称唯一、描述非空、期望类别非空（防止有人加一条空样本凑数）。
    assert len({item.name for item in REGRESSION_SAMPLES}) == 5
    for item in REGRESSION_SAMPLES:
        assert item.description.strip()
        assert item.expected_categories
        assert item.raw_secret
        assert item.payload


def test_ac19_sample_set_version_is_non_empty_and_engine_version_matches() -> None:
    engine = _engine()
    assert SAMPLE_SET_VERSION
    assert SAMPLE_SET_VERSION != "none"
    assert engine.version
    # 样本集与引擎同一版本口径：改词表要升版，样本期望值必须同时复核。
    for sample in REGRESSION_SAMPLES:
        result = engine.redact(sample.payload, channel=RedactionChannel.TOOL_RESULT)
        assert result.redaction_version == engine.version


def test_ac19_connection_string_sample_is_blocked_not_redacted() -> None:
    """连接串属"不可安全改写"：必须是 `BLOCKED` + 固定占位符，不得当成已脱敏。"""
    sample = next(item for item in REGRESSION_SAMPLES if item.name == "connection_string")

    result = _redact(sample)

    assert result.status is RedactionStatus.BLOCKED
    assert result.is_passable is False
    assert result.redaction_count == 0
    assert canonical_payload_text(result.payload_redacted) == canonical_payload_text(
        {"target": "<BLOCKED:CONNECTION_STRING>"}
    )
    assert result.unresolved_sensitive_spans


def test_ac19_backslash_sample_raw_text_really_contains_backslashes() -> None:
    """守住样本本身没被"顺手规范化"：反斜杠必须真的在载荷里。"""
    sample = next(item for item in REGRESSION_SAMPLES if item.name == "backslash_escape")

    raw_text = str(sample.payload["line"])
    assert "\\" in raw_text
    assert "\\db\\dump" in raw_text


def test_ac19_nested_json_sample_is_deep_enough_to_matter() -> None:
    sample = next(item for item in REGRESSION_SAMPLES if item.name == "nested_json")

    assert "outer" in sample.payload
    assert "middle" in sample.payload["outer"]
    assert "inner" in sample.payload["outer"]["middle"]
    result = _redact(sample)
    # 深层叶子也要命中：不能只扫顶层。
    assert SensitiveCategory.EMAIL in result.detected_categories
    assert SensitiveCategory.PHONE in result.detected_categories


# ---------------------------------------------------------------------------
# AC-07 训练样本分支：拼接后复扫
# ---------------------------------------------------------------------------


def test_training_sample_is_clean_and_header_carries_version_and_hash() -> None:
    sample = build_training_sample(
        trajectory=_trajectory(events=[_event(1, {"tool": "inspect_table", "rows": 3})]),
        evidences=[_evidence(redacted_payload={"table": "orders", "rows": 3})],
        gate=_gate(),
    )

    assert isinstance(sample, dict), sample
    header = sample["header"]
    assert header["redaction_version"] == _engine().version
    assert header["redaction_version"]
    assert header["source_payload_hash"]
    assert len(header["source_payload_hash"]) == 64
    assert header["redaction_status"] == RedactionStatus.CLEAN.value
    assert sample["sample_version"] == TRAINING_SAMPLE_VERSION
    assert sample["sample_set_version"] == SAMPLE_SET_VERSION
    assert sample["evidence"][0]["evidence_id"] == "ev_w09_samples"
    assert sample["trajectory"]["events"][0]["kind"] == "PLAN_STEP"


def test_training_sample_rescans_the_assembled_document() -> None:
    """闸门必须拿到**拼接完成**的整份文档，而不是逐个片段。"""
    spy = _SpyGate()

    build_training_sample(
        trajectory=_trajectory(events=[_event(1, {"tool": "inspect_table"})]),
        evidences=[_evidence(redacted_payload={"table": "orders"})],
        gate=spy,  # type: ignore[arg-type]
    )

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["channel"] is RedactionChannel.TRAINING_SAMPLE
    assert call["task_id"] == TASK_ID
    assert call["tenant_id"] == TENANT
    # 三段（头 / 轨迹 / 证据）在同一次调用里被扫描：拼接后才判定。
    assert set(call["payload"]) == {
        "sample_version",
        "sample_set_version",
        "header",
        "trajectory",
        "evidence",
    }
    assert "goal" in call["payload"]["header"]
    assert call["payload"]["evidence"][0]["redacted_payload"] == {"table": "orders"}


def test_training_sample_redacts_phone_in_free_text_goal() -> None:
    sample = build_training_sample(
        trajectory=_trajectory(goal=f"联系电话 {PHONE} 已核对"),
        evidences=[_evidence(redacted_payload={"table": "orders"})],
        gate=_gate(),
    )

    assert isinstance(sample, dict), sample
    text = json.dumps(sample, ensure_ascii=False, sort_keys=True)
    assert PHONE not in text, "出口复扫后原串不得残留"
    assert sample["header"]["redaction_status"] == RedactionStatus.REDACTED.value
    assert sample["header"]["redaction_count"] >= 1
    assert "<PHONE:" in text


def test_training_sample_returns_skipped_for_blocked_connection_string() -> None:
    sample = build_training_sample(
        trajectory=_trajectory(goal=CONNECTION_STRING),
        evidences=[_evidence(redacted_payload={"table": "orders"})],
        gate=_gate(),
    )

    assert isinstance(sample, SkippedSample), sample
    assert sample.reason == REASON_REDACTION_BLOCKED
    assert "CONNECTION_STRING" in sample.detected_categories
    assert len(sample.source_digest) == 64
    # 摘要不可逆、且不含原串。
    assert CONNECTION_STRING not in sample.model_dump_json()


def test_training_sample_returns_skipped_for_review_required_business_id() -> None:
    sample = build_training_sample(
        trajectory=_trajectory(goal="AB12345678"),
        evidences=[_evidence(redacted_payload={"table": "orders"})],
        gate=_gate(),
    )

    assert isinstance(sample, SkippedSample), sample
    assert sample.reason == REASON_REVIEW_REQUIRED
    assert "PERSON_OR_BUSINESS_ID" in sample.detected_categories
    assert len(sample.source_digest) == 64
    assert "AB12345678" not in sample.model_dump_json()


def test_training_sample_rescan_catches_residue_in_upstream_redacted_payload() -> None:
    """纵深防御：上游"已脱敏"载荷若仍含连接串，出口这一次必须拦住。"""
    sample = build_training_sample(
        trajectory=_trajectory(),
        evidences=[
            _evidence(
                evidence_id="ev_residue",
                redacted_payload={"target": CONNECTION_STRING},
            )
        ],
        gate=_gate(),
    )

    assert isinstance(sample, SkippedSample), sample
    assert sample.reason == REASON_REDACTION_BLOCKED
    assert "CONNECTION_STRING" in sample.detected_categories


def test_training_sample_never_reads_evidence_raw_payload() -> None:
    """证据只取 `redacted_payload`：原文载荷里的手机号不得进入样本。"""
    evidences = [
        _evidence(
            payload={"note": f"联系电话 {PHONE}"},
            redacted_payload=None,
        )
    ]

    sample = build_training_sample(
        trajectory=_trajectory(),
        evidences=evidences,
        gate=_SpyGate(),  # type: ignore[arg-type]
    )

    assert isinstance(sample, dict), sample
    assert sample["evidence"][0]["redacted_payload"] == {}
    assert PHONE not in json.dumps(sample, ensure_ascii=False)
    assert sample["header"]["redaction_status"] == RedactionStatus.CLEAN.value


def test_training_sample_truncates_events_but_declares_it() -> None:
    events = [_event(index, {"index": index}) for index in range(MAX_SAMPLE_EVENTS + 3)]

    sample = build_training_sample(
        trajectory=_trajectory(events=events),
        evidences=[_evidence()],
        gate=_gate(),
    )

    assert isinstance(sample, dict), sample
    assert sample["header"]["events_total"] == MAX_SAMPLE_EVENTS + 3
    assert sample["header"]["events_truncated"] is True
    assert len(sample["trajectory"]["events"]) == MAX_SAMPLE_EVENTS
    # 按尾部保留（最接近结论）。
    assert sample["trajectory"]["events"][-1]["payload"] == {
        "index": MAX_SAMPLE_EVENTS + 2
    }


def test_training_sample_tenant_falls_back_to_none_without_evidence() -> None:
    spy = _SpyGate()

    sample = build_training_sample(
        trajectory=_trajectory(), evidences=[], gate=spy  # type: ignore[arg-type]
    )

    assert isinstance(sample, dict), sample
    assert spy.calls[0]["tenant_id"] is None
    assert sample["header"]["evidence_count"] == 0


def test_training_sample_with_usage_counters_is_redacted_by_r12_vocabulary() -> None:
    """已知口径登记：`usage.prompt_tokens` 的键名含 "token"，命中 R12 敏感键词表。

    这是引擎**冻结**的键名规则（R1 v2 已撤回白名单，`docs/w09/05-rulings.md`），
    不是本卡引入的行为：因此真实轨迹（带 token 计数）产出的样本一律是
    `REDACTED` 且 `detected_categories == (LOG_FIELD,)`，值被替换为固定占位符。
    `REDACTED` 仍是**可放行**状态，样本可正常交付；此处锁定该事实，避免有人
    误以为"样本必须 CLEAN 才算成功"而去改闸门。
    """
    sample = build_training_sample(
        trajectory=_trajectory(usage={"prompt_tokens": 10, "completion_tokens": 5}),
        evidences=[_evidence(redacted_payload={"table": "orders"})],
        gate=_gate(),
    )

    assert isinstance(sample, dict), sample
    header = sample["header"]
    assert header["redaction_status"] == RedactionStatus.REDACTED.value
    assert header["detected_categories"] == [SensitiveCategory.LOG_FIELD.value]
    assert header["redaction_count"] == 2
    # 计数被抹成占位符，但样本结构完整可交付。
    assert sample["trajectory"]["usage"] == {
        "completion_tokens": "<REDACTED:LOG_FIELD>",
        "prompt_tokens": "<REDACTED:LOG_FIELD>",
    }


# ---------------------------------------------------------------------------
# "不落盘"是结构性质，不是口头承诺
# ---------------------------------------------------------------------------


def test_skipped_sample_has_no_field_that_can_carry_payload() -> None:
    assert set(SkippedSample.model_fields) == {
        "reason",
        "detected_categories",
        "source_digest",
    }
    with pytest.raises(Exception):
        SkippedSample(
            reason=REASON_REDACTION_BLOCKED,
            detected_categories=(),
            source_digest="0" * 64,
            sample={"leak": True},
        )


def test_samples_module_has_no_write_capability() -> None:
    """结构证明：模块源码里没有任何文件写入口（纯函数，无仓储/句柄）。"""
    import core.report.samples as samples_module

    source = inspect.getsource(samples_module)
    for forbidden in (
        "open(",
        "write_text",
        "write_bytes",
        "json.dump(",
        "Path(",
        "os.replace",
        "tempfile",
    ):
        assert forbidden not in source, forbidden
    # 也不持有任何 DSN/连接（AC-18 同款结构断言）。
    parameters = inspect.signature(build_training_sample).parameters
    assert list(parameters) == ["trajectory", "evidences", "gate"]
    assert all("dsn" not in name.lower() for name in parameters)
