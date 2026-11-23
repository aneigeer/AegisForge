"""W11 T04：``publish_decision`` 纯函数与限制清单（P0-W11-06 / AC-17 / AC-18）。

设计依据：``docs/w11/02-design.md`` §10（§10.1 判定树、§10.2 自动/人工限制项）、
§11 签名；裁定 W11-Q8（结构化限制清单是 `TECH_PREVIEW_ONLY` 的唯一传递方式）、
W12-Q4（``release_type`` **只由**本纯函数派生，不可人工覆盖）、Q-W11-A（完备性守卫）。

四条硬约束：

1. **纯函数**（AC-18①）：无 I/O、无全局状态、无 ``utc_now()``；``PublishDecision``
   **不带时间戳**；同输入两次结果逐位相等；``inputs_digest`` 由
   ``core.security.canonical.canonical_digest`` 生成（复用，不重写）。
2. **不可覆盖**（AC-18②）：签名里没有 ``force_allow`` / ``override`` /
   ``manual_override`` / 任何 ``allow_*`` 形参；不读配置、不读环境变量。
3. **三条红线**（任一命中 → ``TECH_PREVIEW_ONLY``）：负面集未被拒/拒因不符/目标数据被改、
   并发出现重复副作用、故障未 fail closed 或恢复期重放/工具调用增量非 0。
4. **完备性守卫不是红线**（Q-W11-A）：结果集缺项或 ``environment_blocks`` 非空 →
   ``TECH_PREVIEW_ONLY`` + ``reason="ENV_BLOCK_UNRESOLVED"``，但
   ``failed_test_ids`` **必须保持为空**（PG 段仍是 ``NOT_EXECUTED``，不判 FAIL）。

限制清单（W11-Q8 / W12-Q8）：自动基础项共 6 项且**不可删/不可改**；人工项只能追加，
``origin=MANUAL`` 时 ``added_by`` 与 ``reason`` 必填。``docs/w11/limitations.json``
由 :func:`build_limitations` + :func:`limitations_to_json` 序列化生成，是单一数据源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.assurance.concurrency import (
    CONCURRENCY_CASE_IDS,
    ConcurrencyResult,
    DuplicateSideEffectDetected,
    assert_no_duplicate_side_effects,
)
from core.assurance.faults import FAULT_CLASSES, FaultInjectionResult
from core.assurance.negative import NEGATIVE_CASE_COUNT, NegativeResult
from core.security.canonical import canonical_digest

LIMITATION_VERSION = "w11-limitations-v1"
METRIC_DEFINITION_VERSION = "metrics-v1"

CONCURRENCY_CASE_TOTAL = len(CONCURRENCY_CASE_IDS)
FAULT_CLASS_TOTAL = len(FAULT_CLASSES)

#: 五项环境阻塞（``C-2``），冻结顺序与编号约定一致（``docs/w10-w12-lead-rulings.md`` §6）。
ENVIRONMENT_BLOCK_IDS: tuple[str, ...] = (
    "ENV-PG5432",
    "ENV-1045",
    "ENV-RESIDUE",
    "ENV-NV1",
    "ENV-MODEL",
)

#: 自动限制项的 id 前缀（人工项不得占用，否则会与不可删项混淆）。
AUTO_ITEM_PREFIXES: tuple[str, ...] = ("ENV-", "DECISION-")

PUBLISH_DECISION_INCONSISTENT = "PUBLISH_DECISION_INCONSISTENT"

REASON_ALL_CLEAR = "ALL_CLEAR"
REASON_RED_LINE_FAILED = "RED_LINE_FAILED"
REASON_ENV_BLOCK_UNRESOLVED = "ENV_BLOCK_UNRESOLVED"

SOURCE_NEGATIVE = "NEGATIVE"
SOURCE_CONCURRENCY = "CONCURRENCY"
SOURCE_FAULT = "FAULT"

#: 并发结果里"必须出现拒绝"的两个稳定码（AC-07）；未按预期出现即红线。
EXPECTED_DENIAL_CODES: frozenset[str] = frozenset({"FENCING_TOKEN_STALE", "WORKER_CONFLICT"})


class Decision(str, Enum):
    ALPHA_ALLOWED = "ALPHA_ALLOWED"
    TECH_PREVIEW_ONLY = "TECH_PREVIEW_ONLY"


class LimitationOrigin(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class LimitationMarker(str, Enum):
    NOT_SUPPORTED = "NOT_SUPPORTED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


LIMITATION_MARKERS: tuple[str, ...] = (
    LimitationMarker.NOT_SUPPORTED.value,
    LimitationMarker.PARTIAL.value,
    LimitationMarker.UNKNOWN.value,
)


class LimitationRemoved(ValueError):
    """自动限制项被删除/改写（W12-Q8 同款失败码）。"""

    code = "LIMITATION_REMOVED"


class LimitationError(ValueError):
    """人工追加项的合法性错误（非删除类）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class RedLineFailure:
    """一条红线失败（喂给 ``failed_test_ids`` 与限制清单）。"""

    test_id: str
    source: Literal["NEGATIVE", "CONCURRENCY", "FAULT"]
    reason: str


@dataclass(frozen=True)
class PublishDecision:
    """发布结论（**纯值对象**：无时间戳、无 I/O、同输入逐位相等）。"""

    decision: Decision
    failed_test_ids: tuple[str, ...]
    red_line_failures: tuple[RedLineFailure, ...]
    environment_blocks: tuple[str, ...]
    reason: str
    inputs_digest: str
    metric_definition_version: str = METRIC_DEFINITION_VERSION
    limitation_markers: tuple[str, ...] = LIMITATION_MARKERS

    def __post_init__(self) -> None:
        """构造期一致性守卫（AC-18②：结论不可被"手工拼装"绕过）。

        ``ALPHA_ALLOWED`` 的语义是"三条红线全过且无环境阻塞"，因此一旦结论是
        ``ALPHA_ALLOWED``，``failed_test_ids`` / ``red_line_failures`` /
        ``environment_blocks`` **必须全为空**；否则直接拒绝构造。

        为什么必须放在 ``__post_init__``：``dataclasses.replace()`` 同样会走
        ``__init__`` → ``__post_init__``，因此"先构造合法对象再 replace 改结论"
        这条旁路也会被拦（L-5）。
        """
        if self.decision is Decision.ALPHA_ALLOWED:
            if self.failed_test_ids or self.red_line_failures or self.environment_blocks:
                raise ValueError(
                    "PUBLISH_DECISION_INCONSISTENT: decision=ALPHA_ALLOWED 时 "
                    "failed_test_ids / red_line_failures / environment_blocks 必须全为空"
                    f"（实际 failed={list(self.failed_test_ids)}、"
                    f"red_lines={len(self.red_line_failures)}、"
                    f"blocks={list(self.environment_blocks)}）"
                )


class LimitationItem(BaseModel):
    """限制清单的一条（``extra="forbid"``）。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    marker: LimitationMarker
    origin: LimitationOrigin
    statement: str = Field(min_length=1)
    added_by: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _manual_requires_attribution(self) -> "LimitationItem":
        if self.origin is LimitationOrigin.MANUAL:
            if not str(self.added_by or "").strip():
                raise LimitationError("MANUAL_ITEM_UNATTRIBUTED", "人工限制项必须填写 added_by")
            if not str(self.reason or "").strip():
                raise LimitationError("MANUAL_ITEM_UNATTRIBUTED", "人工限制项必须填写 reason")
        return self


class LimitationList(BaseModel):
    """限制清单（自动项不可删；人工项只能追加）。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal["w11-limitations-v1"] = LIMITATION_VERSION
    decision: Decision
    items: tuple[LimitationItem, ...]

    @model_validator(mode="after")
    def _auto_items_immutable(self) -> "LimitationList":
        """重算自动集合并逐项比对：删除或改写任一自动项 → ``LIMITATION_REMOVED``。"""
        seen: set[str] = set()
        for item in self.items:
            if item.item_id in seen:
                raise LimitationError("LIMITATION_DUPLICATE_ID", f"item_id 重复：{item.item_id}")
            seen.add(item.item_id)
        present = {item.item_id: item for item in self.items}
        for expected in _auto_items(self.decision):
            actual = present.get(expected.item_id)
            if actual is None:
                raise LimitationRemoved(
                    f"LIMITATION_REMOVED: 自动限制项不可删除：{expected.item_id}"
                )
            if actual.origin is not LimitationOrigin.AUTO:
                raise LimitationRemoved(
                    f"LIMITATION_REMOVED: 自动限制项不得改为人工项：{expected.item_id}"
                )
            if actual.marker is not expected.marker:
                raise LimitationRemoved(
                    f"LIMITATION_REMOVED: 自动限制项 marker 不可修改：{expected.item_id}"
                )
            if actual.statement != expected.statement:
                raise LimitationRemoved(
                    f"LIMITATION_REMOVED: 自动限制项 statement 不可修改：{expected.item_id}"
                )
        return self


def _auto_items(decision: Decision) -> tuple[LimitationItem, ...]:
    """自动基础项（**纯派生**，共 6 项；marker 三值齐备）。

    只依赖 ``decision`` 与五项环境阻塞常量，因此校验器可以**重算**它并与清单比对。
    """
    items: list[LimitationItem] = [
        LimitationItem(
            item_id="ENV-PG5432",
            marker=LimitationMarker.NOT_SUPPORTED,
            origin=LimitationOrigin.AUTO,
            statement=(
                "PostgreSQL LAB（5432）拒绝连接：跨引擎只读诊断的 PG 段未执行，"
                "按 NOT_EXECUTED 处置，不判通过、不冒充已执行。"
            ),
        ),
        LimitationItem(
            item_id="ENV-1045",
            marker=LimitationMarker.NOT_SUPPORTED,
            origin=LimitationOrigin.AUTO,
            statement=(
                "DG_ADMIN_DSN（root@localhost）认证失败 1045：库级物理备份恢复不可用，"
                "W11 只做逻辑影子回放，不声称物理恢复已支持。"
            ),
        ),
        LimitationItem(
            item_id="ENV-RESIDUE",
            marker=LimitationMarker.PARTIAL,
            origin=LimitationOrigin.AUTO,
            statement=(
                "控制库只追加表存在未观测历史残留（W09 实测 140 行且随时间增长）："
                "W11 各卡继续只追加不清理，所有计数按 task_id/run_id 过滤。"
            ),
        ),
        LimitationItem(
            item_id="ENV-NV1",
            marker=LimitationMarker.UNKNOWN,
            origin=LimitationOrigin.AUTO,
            statement=(
                "容器运行态 NOT_VERIFIED、镜像 digest 为 UNKNOWN：容器网络隔离未在运行态验证，"
                "不声称已验证。"
            ),
        ),
        LimitationItem(
            item_id="ENV-MODEL",
            marker=LimitationMarker.UNKNOWN,
            origin=LimitationOrigin.AUTO,
            statement=(
                "模型环境 BLOCKED_MODEL_ENV_MISSING：模型版本与生成时延等指标未测得，"
                "相关字段一律保持 null。"
            ),
        ),
    ]
    if decision is Decision.TECH_PREVIEW_ONLY:
        items.append(
            LimitationItem(
                item_id="DECISION-TECH-PREVIEW",
                marker=LimitationMarker.PARTIAL,
                origin=LimitationOrigin.AUTO,
                statement=(
                    "发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，"
                    "不得表述为完整 Alpha。"
                ),
            )
        )
    else:
        items.append(
            LimitationItem(
                item_id="DECISION-ALPHA-ALLOWED",
                marker=LimitationMarker.PARTIAL,
                origin=LimitationOrigin.AUTO,
                statement=(
                    "发布结论为 ALPHA_ALLOWED：三条红线全过且环境阻塞清单为空；"
                    "该结论来自冻结测试集，不是安全性的数学证明。"
                ),
            )
        )
    return tuple(items)


def _negative_failure(result: NegativeResult) -> str | None:
    """红线①：未被拒 / 拒因不符 / 目标数据被改。"""
    if result.actual is None:  # 防御性：结果类型缺 actual
        return "MISSING_OBSERVATION"
    if result.actual.denied is not True:
        return "NOT_DENIED"
    if result.actual.deny_reason != result.expected_deny_reason:
        return "DENY_REASON_MISMATCH"
    if result.actual.target_before != result.actual.target_after:
        return "TARGET_CHANGED"
    return None


def _concurrency_failures(result: ConcurrencyResult) -> list[str]:
    """红线②：零重复副作用谓词不通过 / 预期拒绝未出现。"""
    reasons: list[str] = []
    try:
        assert_no_duplicate_side_effects(result)
    except DuplicateSideEffectDetected as exc:
        failures = exc.details.get("failures") or [exc.code]
        reasons.extend(str(item) for item in failures)
    if result.expected_code in EXPECTED_DENIAL_CODES and result.denied is not True:
        reasons.append("EXPECTED_DENIAL_MISSING")
    return reasons


def _fault_failures(result: FaultInjectionResult) -> list[str]:
    """红线③：未 fail closed / 序列断裂 / 重放 / 工具调用增量非 0。"""
    reasons: list[str] = []
    if result.fail_closed_ok is False:
        reasons.append("FAIL_OPEN")
    if result.recovery.seq_continuous is False:
        reasons.append("SEQ_NOT_CONTINUOUS")
    if result.recovery.replay_occurred is True:
        reasons.append("REPLAY_OCCURRED")
    if result.recovery.tool_calls_delta != 0:
        reasons.append("TOOL_CALLS_DELTA")
    return reasons


def _completeness_gaps(
    negative: Sequence[NegativeResult],
    concurrency: Sequence[ConcurrencyResult],
    faults: Sequence[FaultInjectionResult],
    environment_blocks: Sequence[str],
) -> tuple[str, ...]:
    """完备性守卫（**不是红线**）：结果集缺项或环境阻塞未清。"""
    gaps: list[str] = []
    negative_ids = [result.test_id for result in negative]
    if len(negative) != NEGATIVE_CASE_COUNT or len(set(negative_ids)) != len(negative):
        gaps.append("NEGATIVE_SET_INCOMPLETE")
    concurrency_ids = [result.case_id for result in concurrency]
    if len(concurrency) != CONCURRENCY_CASE_TOTAL or set(concurrency_ids) != set(
        CONCURRENCY_CASE_IDS
    ):
        gaps.append("CONCURRENCY_SET_INCOMPLETE")
    fault_classes = {result.fault_class for result in faults}
    if len(faults) != FAULT_CLASS_TOTAL or fault_classes != set(FAULT_CLASSES):
        gaps.append("FAULT_SET_INCOMPLETE")
    if tuple(environment_blocks):
        gaps.append(REASON_ENV_BLOCK_UNRESOLVED)
    return tuple(gaps)


def _inputs_digest(
    negative: Sequence[NegativeResult],
    concurrency: Sequence[ConcurrencyResult],
    faults: Sequence[FaultInjectionResult],
    environment_blocks: Sequence[str],
) -> str:
    """三份结果 + 环境阻塞的规范化摘要（复用 ``canonical_digest``）。"""
    payload: dict[str, Any] = {
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "limitation_version": LIMITATION_VERSION,
        "environment_blocks": sorted(str(item) for item in environment_blocks),
        "negative": [
            {
                "test_id": result.test_id,
                "expected": result.expected_deny_reason,
                "denied": bool(result.actual.denied) if result.actual else False,
                "deny_reason": (result.actual.deny_reason if result.actual else None),
                "error_code": (result.actual.error_code if result.actual else None),
                "target_before": int(result.actual.target_before) if result.actual else 0,
                "target_after": int(result.actual.target_after) if result.actual else 0,
            }
            for result in sorted(negative, key=lambda item: item.test_id)
        ],
        "concurrency": [
            {
                "case_id": result.case_id,
                "expected_code": result.expected_code,
                "denied": result.denied,
                "tool_calls_delta": int(result.tool_calls_delta),
                "target_row_changes": int(result.target_row_changes),
                "seq_length": len(result.seq),
                "event_types": list(result.event_types),
                "state_version_before": int(result.state_version_before),
                "state_version_after": int(result.state_version_after),
            }
            for result in sorted(concurrency, key=lambda item: item.case_id)
        ],
        "faults": [
            {
                "fault_class": result.fault_class.value,
                "injected": bool(result.injected),
                "fail_closed_ok": bool(result.fail_closed_ok),
                "alert_emitted": bool(result.alert_emitted),
                "boundary": result.boundary.value,
                "seq_continuous": bool(result.recovery.seq_continuous),
                "audit_chain_ok": bool(result.recovery.audit_chain_ok),
                "evidence_refs_resolvable": bool(result.recovery.evidence_refs_resolvable),
                "state_version_monotonic": bool(result.recovery.state_version_monotonic),
                "tool_calls_delta": int(result.recovery.tool_calls_delta),
                "replay_occurred": bool(result.recovery.replay_occurred),
            }
            for result in sorted(faults, key=lambda item: item.fault_class.value)
        ],
    }
    return canonical_digest(payload)


def publish_decision(
    *,
    negative: Sequence[NegativeResult],
    concurrency: Sequence[ConcurrencyResult],
    faults: Sequence[FaultInjectionResult],
    environment_blocks: Sequence[str] = (),
) -> PublishDecision:
    """纯函数：三份结果 + 环境阻塞 → 发布结论（**不可覆盖**）。

    判定顺序（设计 §10.1）：先查三条红线，再查完备性；红线命中时
    ``reason="RED_LINE_FAILED"`` 且 ``failed_test_ids`` 列出失败用例；
    仅完备性不足时 ``reason="ENV_BLOCK_UNRESOLVED"`` 且 ``failed_test_ids`` **保持为空**。

    Args:
        negative: 负面集四元组结果（应为 29 条）。
        concurrency: 十类并发结果（应为 10 条，覆盖全部 ``case_id``）。
        faults: 六类故障结果（应为 6 条，覆盖全部 ``FaultClass``）。
        environment_blocks: 未清的环境阻塞编号（``ENV-*``）。

    Returns:
        :class:`PublishDecision`；同输入两次调用逐位相等。
    """
    negative_seq = tuple(negative)
    concurrency_seq = tuple(concurrency)
    fault_seq = tuple(faults)
    blocks = tuple(sorted({str(item) for item in environment_blocks}))

    failures: list[RedLineFailure] = []
    for result in negative_seq:
        reason = _negative_failure(result)
        if reason is not None:
            failures.append(
                RedLineFailure(test_id=result.test_id, source=SOURCE_NEGATIVE, reason=reason)
            )
    for case_result in concurrency_seq:
        for reason in _concurrency_failures(case_result):
            failures.append(
                RedLineFailure(
                    test_id=case_result.test_id, source=SOURCE_CONCURRENCY, reason=reason
                )
            )
    for fault_result in fault_seq:
        for reason in _fault_failures(fault_result):
            failures.append(
                RedLineFailure(
                    test_id=fault_result.test_id, source=SOURCE_FAULT, reason=reason
                )
            )

    gaps = _completeness_gaps(negative_seq, concurrency_seq, fault_seq, blocks)
    failed_test_ids = tuple(sorted({item.test_id for item in failures}))

    if failures:
        decision = Decision.TECH_PREVIEW_ONLY
        reason = REASON_RED_LINE_FAILED
    elif gaps:
        decision = Decision.TECH_PREVIEW_ONLY
        reason = REASON_ENV_BLOCK_UNRESOLVED
    else:
        decision = Decision.ALPHA_ALLOWED
        reason = REASON_ALL_CLEAR

    return PublishDecision(
        decision=decision,
        failed_test_ids=failed_test_ids,
        red_line_failures=tuple(failures),
        environment_blocks=blocks,
        reason=reason,
        inputs_digest=_inputs_digest(negative_seq, concurrency_seq, fault_seq, blocks),
    )


def build_limitations(decision: PublishDecision) -> LimitationList:
    """由发布结论**纯派生**自动限制清单（6 个自动基础项，marker 三值齐备）。"""
    return LimitationList(decision=decision.decision, items=_auto_items(decision.decision))


def append_manual_limitation(items: LimitationList, *, item: LimitationItem) -> LimitationList:
    """追加一条人工限制项；**不得**覆盖/修改自动项。

    Raises:
        LimitationError: 非人工项、id 与自动前缀冲突、或 id 重复。
    """
    if item.origin is not LimitationOrigin.MANUAL:
        raise LimitationError("MANUAL_APPEND_REQUIRED", "只允许追加 origin=MANUAL 的限制项")
    if item.item_id.startswith(AUTO_ITEM_PREFIXES):
        raise LimitationError(
            "AUTO_ITEM_PREFIX_CONFLICT",
            f"人工项 item_id 不得使用自动项前缀 {AUTO_ITEM_PREFIXES}：{item.item_id}",
        )
    if any(existing.item_id == item.item_id for existing in items.items):
        raise LimitationError("LIMITATION_DUPLICATE_ID", f"item_id 重复：{item.item_id}")
    return LimitationList(decision=items.decision, items=(*items.items, item))


def limitations_to_json(items: LimitationList) -> str:
    """把限制清单序列化成稳定 JSON 文本（供 ``docs/w11/limitations.json`` 落盘与内嵌对账）。

    纯函数：只做 ``json.dumps``，不写文件（落盘由调用方负责），保证 T04 与 T06 用**同一**
    序列化口径，避免出现两个真值。
    """
    payload: Mapping[str, Any] = items.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


__all__ = [
    "AUTO_ITEM_PREFIXES",
    "PUBLISH_DECISION_INCONSISTENT",
    "CONCURRENCY_CASE_TOTAL",
    "Decision",
    "ENVIRONMENT_BLOCK_IDS",
    "FAULT_CLASS_TOTAL",
    "LIMITATION_MARKERS",
    "LIMITATION_VERSION",
    "LimitationError",
    "LimitationItem",
    "LimitationList",
    "LimitationMarker",
    "LimitationOrigin",
    "LimitationRemoved",
    "METRIC_DEFINITION_VERSION",
    "PublishDecision",
    "REASON_ALL_CLEAR",
    "REASON_ENV_BLOCK_UNRESOLVED",
    "REASON_RED_LINE_FAILED",
    "RedLineFailure",
    "append_manual_limitation",
    "build_limitations",
    "limitations_to_json",
    "publish_decision",
]
