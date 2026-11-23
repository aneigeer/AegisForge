"""W08 验证计划：从审批披露快照构造、封闭文法解析、最差法汇总（AC-01/02/03/04/09）。

G1「模型自报不进任何判定」：`VerificationPlan` 是 `extra="forbid"` 的 pydantic
模型，模型文本字段一注入即 `ValidationError`（AC-04 的构造级保证）。
`expected` 只走**封闭文法**三种形式，绝不 `eval` / `exec`（设计 §1.3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from core.errors import DisclosureIncompleteError, RequestInvalidError
from core.security.canonical import canonical_digest
from core.tasks.models import utc_now
from core.tools.contracts import (
    MIN_VERIFICATION_ASSERTIONS,
    REQUIRED_VERIFICATION_SOURCES,
    VerificationAssertion,
)
from core.verification.models import AssertionOutcome, Verdict
from core.verification.readback import ReadbackSample

# ---- 封闭文法（设计 §1.3）：只认三种形式，其余一律 UNKNOWN -------------------
_PLACEHOLDER_RE = re.compile(r"^\$params\.([a-z][a-z0-9_]*)$")
_CONJUNCTION_RE = re.compile(r"^before==(.+?) && after==(.+)$")

# AssertionOutcome.reason 的稳定取值。
EXPECTED_UNSUPPORTED = "EXPECTED_UNSUPPORTED"
UNRESOLVED_PLACEHOLDER = "UNRESOLVED_PLACEHOLDER"
OBSERVED_MISSING = "OBSERVED_MISSING"
STEP_OBSERVED_MISSING = "STEP_OBSERVED_MISSING"
READBACK_FAILED = "READBACK_FAILED"
AUDIT_EVENT_UNAVAILABLE = "AUDIT_EVENT_UNAVAILABLE"
TOLERANCE_UNSUPPORTED = "TOLERANCE_UNSUPPORTED"

# 快照里 `before_after_snapshot` 断言的 metric（W07 冻结向量）。
_SNAPSHOT_METRIC = "before_after_snapshot"


@dataclass(frozen=True)
class ResolvedExpected:
    """`resolve_expected()` 的封闭解析结果（不含任何可执行表达式）。"""

    kind: Literal["literal", "parameter", "conjunction", "unresolved", "unsupported"]
    value: str | int | None = None
    before: str | int | None = None
    after: str | int | None = None
    reason: str | None = None


def _resolve_atom(
    expr: str, parameters: Mapping[str, Any]
) -> tuple[str | int | None, str | None]:
    """解析 `<expr>`：字面量或 `$params.*` 单占位；失败返回 UNRESOLVED 原因。"""
    match = _PLACEHOLDER_RE.match(expr)
    if match is None:
        return expr, None
    name = match.group(1)
    if name not in parameters:
        return None, UNRESOLVED_PLACEHOLDER
    value = parameters[name]
    if value is None or (isinstance(value, (str, int)) and not isinstance(value, bool)):
        return value, None
    return None, UNRESOLVED_PLACEHOLDER


def resolve_expected(expected: str, parameters: Mapping[str, Any]) -> ResolvedExpected:
    """封闭文法解析；**绝不 eval/exec**，未知形式 / 占位失败返回可判定的 UNKNOWN 原因。

    1. 字面量：`expected` 不含 `$params.` → `kind="literal"`；
    2. 单占位：`$params.<name>` → 服务端从 `parameters` 取标量值；
    3. 双等值合取：`before==<expr> && after==<expr>` → 两个等值子句。

    占位符解析失败（参数缺失 / 非标量）→ `kind="unresolved"`，**绝不按字面量比较**；
    文法外形式 → `kind="unsupported"`。两者都不抛未捕获异常（AC-02）。
    """
    text = expected.strip()
    conjunction = _CONJUNCTION_RE.match(text)
    if conjunction is not None:
        before, reason_b = _resolve_atom(conjunction.group(1), parameters)
        after, reason_a = _resolve_atom(conjunction.group(2), parameters)
        if reason_b is not None or reason_a is not None:
            return ResolvedExpected(kind="unresolved", reason=UNRESOLVED_PLACEHOLDER)
        return ResolvedExpected(kind="conjunction", before=before, after=after)
    if _PLACEHOLDER_RE.match(text) is not None:
        value, reason = _resolve_atom(text, parameters)
        if reason is not None:
            return ResolvedExpected(kind="unresolved", reason=reason)
        return ResolvedExpected(kind="parameter", value=value)
    if "$params." in text:
        return ResolvedExpected(kind="unsupported", reason=EXPECTED_UNSUPPORTED)
    return ResolvedExpected(kind="literal", value=text)


def aggregate(outcomes: Sequence[AssertionOutcome]) -> Verdict:
    """最差法汇总：任一 FAIL→FAIL；无 FAIL 有 UNKNOWN→UNKNOWN；全 PASS→PASS。

    顺序无关（按 `assertion_id` 排序后计算，保证确定性）；空输入按 UNKNOWN
    （不把「没有断言」注水成 PASS）。
    """
    ordered = sorted(outcomes, key=lambda item: item.assertion_id)
    if not ordered:
        return "UNKNOWN"
    verdicts = {item.verdict for item in ordered}
    if "FAIL" in verdicts:
        return "FAIL"
    if "UNKNOWN" in verdicts:
        return "UNKNOWN"
    return "PASS"


def plan_digest(assertions: Sequence[VerificationAssertion]) -> str:
    """对结构化断言取 `canonical_digest`（唯一入口）；按 ID 排序保证确定性。"""
    payload = [
        {
            "assertion_id": item.assertion_id,
            "metric": item.metric,
            "expected": item.expected,
            "tolerance": item.tolerance,
            "source": item.source,
        }
        for item in sorted(assertions, key=lambda a: a.assertion_id)
    ]
    return canonical_digest(payload)


class VerificationPlan(BaseModel):
    """验证计划（`extra="forbid"`）：注入任何模型文本字段都会 ValidationError。"""

    model_config = ConfigDict(extra="forbid")

    plan_digest: str
    assertions: tuple[VerificationAssertion, ...]

    @classmethod
    def from_disclosure(
        cls,
        approval: Any,
        *,
        definition: Any,
        parameters: Mapping[str, Any],
    ) -> "VerificationPlan":
        """从审批创建时刻的披露快照构造计划（fail closed）。

        `approval` 为已消费审批（`Approval` / `ApprovalDetail` 或含 `disclosure`
        的 Mapping）；`definition` 为工具契约，用于交叉校验快照断言与契约同源；
        `parameters` 是 `$params.*` 的**服务端唯一来源**（此处校验入参形态，
        逐条解析由 `resolve_expected()` 在比对阶段完成，不进 plan 字段集）。
        """
        if not isinstance(parameters, Mapping):
            raise RequestInvalidError(
                "from_disclosure 的 parameters 必须是 Mapping",
                details={"reason": "DISCLOSURE_INCOMPLETE"},
            )
        disclosure = _disclosure_of(approval)
        section = disclosure.get("verification_plan")
        raw = section.get("assertions") if isinstance(section, Mapping) else None
        if not isinstance(raw, list) or not raw:
            raise DisclosureIncompleteError(
                "审批披露快照缺少验证计划断言，无法构造 VerificationPlan",
                details={"reason": "DISCLOSURE_INCOMPLETE", "missing_fields": ["disclosure.verification_plan"]},
            )
        try:
            assertions = tuple(VerificationAssertion(**item) for item in raw)
        except (ValidationError, TypeError) as exc:
            raise DisclosureIncompleteError(
                "审批披露快照的验证计划断言非法",
                details={"reason": "DISCLOSURE_INCOMPLETE"},
            ) from exc

        # 交叉校验：快照断言必须与工具契约声明同源（防止契约漂移/快照被换）。
        contract = [a.model_dump() for a in getattr(definition, "verification_plan", [])]
        if contract and [a.model_dump() for a in assertions] != contract:
            raise DisclosureIncompleteError(
                "审批披露快照的验证计划与工具契约不一致",
                details={"reason": "DISCLOSURE_INCOMPLETE"},
            )

        ids = [a.assertion_id for a in assertions]
        if len(assertions) < MIN_VERIFICATION_ASSERTIONS or len(set(ids)) != len(ids):
            raise DisclosureIncompleteError(
                "验证计划断言不足或 ID 重复，fail closed",
                details={"reason": "DISCLOSURE_INCOMPLETE", "assertion_count": len(assertions)},
            )
        covered = {a.source for a in assertions}
        missing = [s for s in REQUIRED_VERIFICATION_SOURCES if s not in covered]
        if missing:
            raise DisclosureIncompleteError(
                "验证计划未覆盖必要的异路径证据来源",
                details={"reason": "DISCLOSURE_INCOMPLETE", "missing_sources": missing},
            )
        return cls(plan_digest=plan_digest(assertions), assertions=assertions)


def _disclosure_of(approval: Any) -> dict[str, Any]:
    if isinstance(approval, Mapping):
        value = approval.get("disclosure", approval)
    else:
        value = getattr(approval, "disclosure", None)
    if not isinstance(value, Mapping):
        raise DisclosureIncompleteError(
            "审批对象缺少披露快照", details={"reason": "DISCLOSURE_INCOMPLETE"}
        )
    return dict(value)


def _coerce_observed(value: Any) -> str | int | None:
    """把任意实测值收敛为 AssertionOutcome.observed 的 `str | int | None`。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, Mapping):
        return f"before={value.get('before')}|after={value.get('after')}"
    return str(value)


def _equal(observed: Any, expected: Any, tolerance: str) -> bool | None:
    """返回 True/False；无法判定返回 None（调用方按 UNKNOWN 处理）。"""
    if tolerance == "exact":
        if observed is None and expected is None:
            return True
        if observed is None or expected is None:
            return False
        return str(observed) == str(expected)
    if tolerance == "int":
        try:
            return int(observed) == int(expected)
        except (TypeError, ValueError):
            return None
    return None


def evaluate_assertion(
    assertion: VerificationAssertion,
    *,
    parameters: Mapping[str, Any],
    observed: Any,
    collected_at: datetime | None = None,
    evidence_ref: str | None = None,
) -> AssertionOutcome:
    """单条断言比对；任何无法判定的情形一律 UNKNOWN（不抛未捕获异常）。"""
    when = collected_at or utc_now()
    resolved = resolve_expected(assertion.expected, parameters)
    base: dict[str, Any] = {
        "assertion_id": assertion.assertion_id,
        "metric": assertion.metric,
        "expected": assertion.expected,
        "tolerance": assertion.tolerance,
        "source": assertion.source,
        "observed": _coerce_observed(observed),
        "evidence_ref": evidence_ref,
        "collected_at": when,
    }
    if resolved.kind in ("unresolved", "unsupported"):
        return AssertionOutcome(**base, verdict="UNKNOWN", reason=resolved.reason)
    if assertion.tolerance not in ("exact", "int"):
        return AssertionOutcome(**base, verdict="UNKNOWN", reason=TOLERANCE_UNSUPPORTED)
    if observed is None:
        return AssertionOutcome(**base, verdict="UNKNOWN", reason=OBSERVED_MISSING)
    if resolved.kind == "conjunction":
        if not isinstance(observed, Mapping):
            return AssertionOutcome(**base, verdict="UNKNOWN", reason=OBSERVED_MISSING)
        before = _equal(observed.get("before"), resolved.before, assertion.tolerance)
        after = _equal(observed.get("after"), resolved.after, assertion.tolerance)
        if before is None or after is None:
            return AssertionOutcome(**base, verdict="UNKNOWN", reason=OBSERVED_MISSING)
        return AssertionOutcome(
            **base, verdict="PASS" if (before and after) else "FAIL", reason=None
        )
    flag = _equal(observed, resolved.value, assertion.tolerance)
    if flag is None:
        return AssertionOutcome(**base, verdict="UNKNOWN", reason=TOLERANCE_UNSUPPORTED)
    return AssertionOutcome(**base, verdict="PASS" if flag else "FAIL", reason=None)


def _observed_payload(result: Mapping[str, Any]) -> Mapping[str, Any]:
    """定位业务载荷：优先 `result["data"]`，回落 `result` 顶层。

    执行器 `executor.py:486` 落盘的是 `ToolResult.model_dump(mode="json")` **全量
    快照**（业务指标位于 `result["data"]` 之下）；而历史注入用例给的是**扁平**
    `result`（指标直接在顶层）。两种形状都必须能读，故此处做形状归一：
    `data` 缺失或不是 Mapping 时整体按顶层处理。
    """
    payload = result.get("data")
    if isinstance(payload, Mapping):
        return payload
    return result


def _collect_observed(
    assertion: VerificationAssertion,
    *,
    step_observed: Mapping[str, Any] | None,
    readback: ReadbackSample | None,
) -> tuple[Any, str | None, str | None]:
    """按 `source` 从**异路径**证据取实测值；缺失返回稳定 UNKNOWN 原因。

    `step_observed.result` 的形状归一（KD-W12-01）：**优先 `result["data"]`**
    （执行器 `ToolResult.model_dump(mode="json")` 落盘的**真实形状**，业务指标
    `affected_rows` / `before` / `after` 都在 `data` 之下），**回落顶层**
    （兼容历史扁平注入的 `result`）。快照 metric 在两者间双向兜底，普通 metric
    先取 `data`、为 `None` 时再取顶层，保证两种形状都可判定、且不因形状差异
    把实测值误判成 UNKNOWN。
    """
    if assertion.source == "target_readback":
        if readback is None:
            return None, READBACK_FAILED, None
        return readback.value, None, readback.sample_id
    if assertion.source == "step_observed":
        if not isinstance(step_observed, Mapping):
            return None, STEP_OBSERVED_MISSING, None
        raw_id = step_observed.get("observed_id")
        evidence_ref = str(raw_id) if raw_id else None
        result = step_observed.get("result")
        if not isinstance(result, Mapping):
            return None, STEP_OBSERVED_MISSING, evidence_ref
        payload = _observed_payload(result)
        if assertion.metric == _SNAPSHOT_METRIC:
            before = payload.get("before")
            after = payload.get("after")
            if payload is not result:
                # `data` 内含业务指标但未带快照键时，回落顶层双等值字段。
                if before is None:
                    before = result.get("before")
                if after is None:
                    after = result.get("after")
            return {"before": before, "after": after}, None, evidence_ref
        observed = payload.get(assertion.metric)
        if observed is None and payload is not result:
            observed = result.get(assertion.metric)
        return observed, None, evidence_ref
    return None, AUDIT_EVENT_UNAVAILABLE, None


def evaluate_plan(
    plan: VerificationPlan,
    *,
    parameters: Mapping[str, Any],
    step_observed: Mapping[str, Any] | None,
    readback: ReadbackSample | None,
    collected_at: datetime | None = None,
) -> tuple[AssertionOutcome, ...]:
    """逐条自动比对（AC-01/09）：按 source 取异路径证据，缺失即 UNKNOWN。

    - `target_readback` 只读独立只读样本，**不复用** `step_observed.result`；
    - `step_observed` 只读只追加证据表；行缺失时对应断言一律 UNKNOWN；
    - `audit_event` 本卡不实现事件回放 → UNKNOWN（fail closed）。
    """
    when = collected_at or utc_now()
    outcomes: list[AssertionOutcome] = []
    for assertion in plan.assertions:
        observed, reason, evidence_ref = _collect_observed(
            assertion, step_observed=step_observed, readback=readback
        )
        if reason is not None:
            outcomes.append(
                AssertionOutcome(
                    assertion_id=assertion.assertion_id,
                    metric=assertion.metric,
                    expected=assertion.expected,
                    tolerance=assertion.tolerance,
                    source=assertion.source,
                    observed=_coerce_observed(observed),
                    verdict="UNKNOWN",
                    reason=reason,
                    evidence_ref=evidence_ref,
                    collected_at=when,
                )
            )
            continue
        outcomes.append(
            evaluate_assertion(
                assertion,
                parameters=parameters,
                observed=observed,
                collected_at=when,
                evidence_ref=evidence_ref,
            )
        )
    return tuple(outcomes)


__all__ = [
    "AUDIT_EVENT_UNAVAILABLE",
    "EXPECTED_UNSUPPORTED",
    "OBSERVED_MISSING",
    "READBACK_FAILED",
    "STEP_OBSERVED_MISSING",
    "TOLERANCE_UNSUPPORTED",
    "UNRESOLVED_PLACEHOLDER",
    "ResolvedExpected",
    "VerificationPlan",
    "aggregate",
    "evaluate_assertion",
    "evaluate_plan",
    "plan_digest",
    "resolve_expected",
]
