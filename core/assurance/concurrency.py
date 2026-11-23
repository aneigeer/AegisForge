"""W11 T02：跨租户对象级授权 + 十类并发回归（P0-W11-02 / AC-05…AC-07）。

设计依据：``docs/w11/02-design.md`` §4.2、§6（§6.1 跨租户、§6.2 十类并发、
§6.3 零重复副作用谓词）、§11；裁定：``docs/w10-w12-lead-rulings.md`` §3（W11-Q2）、
§8（Q-W11-A…E）、**D-12**（``OBJECT_AUTH_DENIED`` 是 W11 断言层归一化标签，
**不是**生产 ``ErrorCode``）。

三条硬约束：

1. **AC-05 跨租户**：六类对象（Task / Evidence / Approval / Report / Environment /
   Audit）各 1 探针 + 伪任务 ``system:worker_scheduler`` 边界 1 探针 = 7；
   ``deny_reason == OBJECT_AUTH_DENIED`` 且 ``result_set_size == 0``，
   生产真实码保留在 :attr:`CrossTenantResult.production_code`。
2. **AC-06 零重复副作用**：:func:`assert_no_duplicate_side_effects` 是唯一谓词
   （``tool_calls_delta <= 1`` / ``target_row_changes <= 1`` / ``seq`` 连续 /
   无 ``TOOL_CALL_RETRIED`` / ``state_version`` 单调），**不得放宽**。
3. **AC-07 fencing/冲突**：旧 Worker → ``FENCING_TOKEN_STALE``；双 Worker →
   ``WORKER_CONFLICT``（复用 ``core/gateway/leases.py``，不改其语义）。

本模块**零 DDL、零生产语义改动**：不碰 ``core/gateway/leases.py`` /
``core/tasks/service.py`` / ``core/approval/service.py``，不新增错误码与事件类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.assurance.negative import ObjectKind

#: W11 断言层归一化标签（D-12）。**全仓无此 ErrorCode**，也不得新增。
OBJECT_AUTH_DENIED = "OBJECT_AUTH_DENIED"

#: 生产侧"未抛错但返回空集"的归一化占位码（事实描述，不是错误码）。
EMPTY_RESULT_CODE = "EMPTY_RESULT"

#: 伪任务 task_id（``tenant_id`` 过滤列为 NULL 的历史边界，见
#: ``docs/w08/08-drill-report.md:66-70``；W09 KD-3 口径：历史 NULL 行不 UPDATE）。
PSEUDO_TASK_ID = "system:worker_scheduler"

CROSS_TENANT_PROBE_COUNT = 7

#: 十类并发用例（冻结；顺序即设计 §6.2 表序）。
CONCURRENCY_CASE_IDS: tuple[str, ...] = (
    "old_worker_fencing",
    "lease_expired",
    "wrong_fencing_token",
    "two_workers_same_task",
    "state_version_conflict",
    "duplicate_message",
    "approval_consume_race",
    "nonce_replay",
    "audit_write_failure",
    "gateway_unreachable",
)

#: 七个跨租户探针（六类对象 + 伪任务边界）。
CROSS_TENANT_PROBE_IDS: tuple[str, ...] = (
    "probe_task_cross_tenant",
    "probe_evidence_cross_tenant",
    "probe_approval_cross_tenant",
    "probe_report_cross_tenant",
    "probe_environment_cross_tenant",
    "probe_audit_cross_tenant",
    "probe_pseudo_task_boundary",
)

#: 伪任务边界探针 id（单独常量，便于断言"按 tenant 不可见 / 按 task_id 可见"并存）。
PSEUDO_TASK_PROBE_ID = "probe_pseudo_task_boundary"

CONCURRENCY_MANIFEST_VERSION = "concurrency-set-v1"

#: 生产侧"拒绝语义"的错误码集合：命中或返回空集都归一化为 OBJECT_AUTH_DENIED。
PRODUCTION_DENIAL_CODES: frozenset[str] = frozenset(
    {
        "TENANT_INVALID",
        "TASK_NOT_FOUND",
        "EVIDENCE_INVALID",
        "APPROVAL_NOT_FOUND",
        "ENVIRONMENT_NOT_FOUND",
        "STATE_CONFLICT",
        "FORBIDDEN",
        "POLICY_DENIED",
    }
)

#: 零重复副作用谓词的失败码（喂给 ``publish_decision`` 的红线来源）。
DUPLICATE_SIDE_EFFECT = "DUPLICATE_SIDE_EFFECT"

#: 设计 §6.3 明确禁止出现的事件类型（重试即重放）。
FORBIDDEN_REPLAY_EVENT = "TOOL_CALL_RETRIED"


class DuplicateSideEffectDetected(Exception):
    """零重复副作用谓词不成立（AC-06 红线）。

    与 :class:`core.assurance.negative.NegativeSetError` 同风格：**不**继承
    ``DBGuardianError``（W11 零生产错误码）。
    """

    def __init__(self, *, code: str, test_id: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.test_id = test_id
        self.details = dict(details or {})
        super().__init__(f"{code}: {test_id}")


@dataclass(frozen=True)
class CrossTenantResult:
    """一次跨租户对象读取的归一化结果（AC-05）。"""

    probe_id: str
    object_kind: ObjectKind
    denied: bool
    deny_reason: str
    production_code: str
    result_set_size: int
    pseudo_task_checked: bool


@dataclass(frozen=True)
class ConcurrencyResult:
    """一类并发/竞争用例的观测事实（AC-06/AC-07）。"""

    test_id: str
    case_id: str
    denied: bool | None
    expected_code: str | None
    tool_calls_delta: int
    target_row_changes: int
    seq: tuple[int, ...]
    event_types: tuple[str, ...]
    state_version_before: int
    state_version_after: int

    @property
    def no_duplicate_side_effect(self) -> bool:
        """设计 §6.3 谓词（逐项与 :func:`assert_no_duplicate_side_effects` 同源）。"""
        return (
            self.tool_calls_delta <= 1
            and self.target_row_changes <= 1
            and list(self.seq) == list(range(len(self.seq)))
            and FORBIDDEN_REPLAY_EVENT not in self.event_types
            and self.state_version_after >= self.state_version_before
        )


class ConcurrencyCaseSpec(BaseModel):
    """冻结的一类并发用例（``extra="forbid"``）。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    expected_code: str | None = None
    expected_denied: bool | None = None
    notes: str = ""


class CrossTenantProbeSpec(BaseModel):
    """冻结的一个跨租户探针（``extra="forbid"``）。"""

    model_config = ConfigDict(extra="forbid")

    probe_id: str = Field(min_length=1)
    object_kind: ObjectKind
    notes: str = ""


class ConcurrencyManifest(BaseModel):
    """冻结清单：10 个 case_id + 7 个 probe_id（缺项即不通过）。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal["concurrency-set-v1"] = CONCURRENCY_MANIFEST_VERSION
    cases: tuple[ConcurrencyCaseSpec, ...]
    cross_tenant_probes: tuple[CrossTenantProbeSpec, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> "ConcurrencyManifest":
        case_ids = [item.case_id for item in self.cases]
        probe_ids = [item.probe_id for item in self.cross_tenant_probes]
        for label, ids in (("case_id", case_ids), ("probe_id", probe_ids)):
            if len(ids) != len(set(ids)):
                duplicated = sorted({item for item in ids if ids.count(item) > 1})
                raise ValueError(f"并发清单 {label} 必须唯一，重复项：{duplicated}")
        return self

    def case_ids(self) -> tuple[str, ...]:
        return tuple(item.case_id for item in self.cases)

    def probe_ids(self) -> tuple[str, ...]:
        return tuple(item.probe_id for item in self.cross_tenant_probes)


def assert_no_duplicate_side_effects(r: ConcurrencyResult) -> None:
    """AC-06 唯一谓词：任何一项不成立即抛 :class:`DuplicateSideEffectDetected`。

    Raises:
        DuplicateSideEffectDetected: 出现重复副作用/重试/序列断裂/版本回退。
    """
    failures: list[str] = []
    if r.tool_calls_delta > 1:
        failures.append("TOOL_CALLS_DUPLICATED")
    if r.target_row_changes > 1:
        failures.append("TARGET_ROWS_DUPLICATED")
    if list(r.seq) != list(range(len(r.seq))):
        failures.append("SEQ_NOT_CONTIGUOUS")
    if FORBIDDEN_REPLAY_EVENT in r.event_types:
        failures.append("TOOL_CALL_RETRIED_OBSERVED")
    if r.state_version_after < r.state_version_before:
        failures.append("STATE_VERSION_REGRESSED")
    if failures:
        raise DuplicateSideEffectDetected(
            code=DUPLICATE_SIDE_EFFECT,
            test_id=r.test_id,
            details={
                "case_id": r.case_id,
                "failures": failures,
                "tool_calls_delta": r.tool_calls_delta,
                "target_row_changes": r.target_row_changes,
                "seq_length": len(r.seq),
                "state_version_before": r.state_version_before,
                "state_version_after": r.state_version_after,
            },
        )


def normalize_object_denial(
    production_code: str | None,
    result_set_size: int,
    *,
    probe_id: str = "",
    object_kind: ObjectKind = ObjectKind.TASK,
) -> CrossTenantResult:
    """把"生产抛租户类拒绝 / 返回空集"统一归一到 W11 标签 ``OBJECT_AUTH_DENIED``。

    **不改生产**：``production_code`` 原样保留（``TENANT_INVALID`` /
    ``TASK_NOT_FOUND`` / ``EVIDENCE_INVALID`` / ``APPROVAL_NOT_FOUND`` …）；
    生产未抛错时用 :data:`EMPTY_RESULT_CODE` 标注"确实是空集"，不编造错误码。

    Args:
        production_code: 生产侧错误码（未抛错传 ``None``）。
        result_set_size: 该次跨租户读取返回的行数（AC-05 要求 0）。
        probe_id: 探针标识（关键字参数；卡内签名的前两个位置参数保持不变）。
        object_kind: 对象类型（同上，关键字参数）。

    Returns:
        :class:`CrossTenantResult`：``deny_reason`` 恒为 ``OBJECT_AUTH_DENIED``。
    """
    if result_set_size < 0:
        raise ValueError("result_set_size 不得为负")
    normalized = str(production_code) if production_code else EMPTY_RESULT_CODE
    denied = result_set_size == 0 or normalized in PRODUCTION_DENIAL_CODES
    return CrossTenantResult(
        probe_id=probe_id,
        object_kind=object_kind,
        denied=denied,
        deny_reason=OBJECT_AUTH_DENIED,
        production_code=normalized,
        result_set_size=int(result_set_size),
        pseudo_task_checked=probe_id == PSEUDO_TASK_PROBE_ID,
    )


def assert_concurrency_manifest_frozen(manifest: ConcurrencyManifest) -> None:
    """冻结口径：版本 / 10 个 case_id / 7 个 probe_id 逐项相等（缺项即失败）。"""
    if manifest.version != CONCURRENCY_MANIFEST_VERSION:
        raise ValueError(f"清单版本必须为 {CONCURRENCY_MANIFEST_VERSION}")
    if tuple(manifest.case_ids()) != CONCURRENCY_CASE_IDS:
        raise ValueError(
            "并发用例清单与 CONCURRENCY_CASE_IDS 不一致："
            f"{manifest.case_ids()} != {CONCURRENCY_CASE_IDS}"
        )
    if tuple(manifest.probe_ids()) != CROSS_TENANT_PROBE_IDS:
        raise ValueError(
            "跨租户探针清单与 CROSS_TENANT_PROBE_IDS 不一致："
            f"{manifest.probe_ids()} != {CROSS_TENANT_PROBE_IDS}"
        )
    if len(manifest.cross_tenant_probes) != CROSS_TENANT_PROBE_COUNT:
        raise ValueError("跨租户探针数必须为 7（AC-05）")


def load_concurrency_manifest(path: Path) -> ConcurrencyManifest:
    """读取并校验冻结清单（``benchmarks/w11/concurrency_cases.json``）。"""
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    manifest = ConcurrencyManifest.model_validate(payload)
    assert_concurrency_manifest_frozen(manifest)
    return manifest


def summarize_cross_tenant(results: Sequence[CrossTenantResult]) -> dict[str, Any]:
    """跨租户探针汇总（结构化，不含任何业务请求正文）。"""
    kinds = sorted({result.object_kind.value for result in results})
    return {
        "probe_count": len(results),
        "denied_count": sum(1 for result in results if result.denied),
        "empty_result_count": sum(1 for result in results if result.result_set_size == 0),
        "object_kinds": kinds,
        "production_codes": sorted({result.production_code for result in results}),
        "pseudo_task_checked": any(result.pseudo_task_checked for result in results),
    }


__all__ = [
    "CONCURRENCY_CASE_IDS",
    "CONCURRENCY_MANIFEST_VERSION",
    "CROSS_TENANT_PROBE_COUNT",
    "CROSS_TENANT_PROBE_IDS",
    "ConcurrencyCaseSpec",
    "ConcurrencyManifest",
    "ConcurrencyResult",
    "CrossTenantProbeSpec",
    "CrossTenantResult",
    "DUPLICATE_SIDE_EFFECT",
    "DuplicateSideEffectDetected",
    "EMPTY_RESULT_CODE",
    "FORBIDDEN_REPLAY_EVENT",
    "OBJECT_AUTH_DENIED",
    "PRODUCTION_DENIAL_CODES",
    "PSEUDO_TASK_ID",
    "PSEUDO_TASK_PROBE_ID",
    "assert_concurrency_manifest_frozen",
    "assert_no_duplicate_side_effects",
    "load_concurrency_manifest",
    "normalize_object_denial",
    "summarize_cross_tenant",
]
