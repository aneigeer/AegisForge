"""W11 T01：安全负面集四元组与冻结清单（P0-W11-01 / AC-01…AC-04）。

设计依依据：``docs/w11/02-design.md`` §4.1、§5（§5.1 冻结 29 条、§5.2 稳定码表、
§5.3 四元组与"目标数据不变"）、§11（关键签名）。

四条硬约束（逐条对应 AC）：

1. **AC-01 四元组**：每条 :class:`NegativeResult` 必带
   ``test_id`` / ``expected_deny_reason`` / ``actual`` / ``evidence``，非空率 100%；
   ``evidence`` 只能是结构化摘要（事件 id、源码行号 + ``canonical_digest``、
   计数快照），**禁止** ``payload`` / ``request`` / SQL 文本 / 参数原文。
2. **AC-02 逐条被拒**：``actual.denied is True`` 且
   ``actual.deny_reason == expected_deny_reason``；``expected_deny_reason`` 取值
   来自设计 §5.2 稳定码表（不新增生产 ``ErrorCode``）。
3. **AC-03 拒绝不改数据**：``target_before == target_after``；计数探针由
   ``NegativeCase.no_change_probe`` 标识，**必须按本用例归属过滤**，
   绝不 ``COUNT(*)`` 全表（``ENV-RESIDUE``=140 会污染）。
4. **AC-04 manifest 唯一**：``test_id`` 无重复、``version == "negative-set-v1"``。

``mode`` 二分（设计 §5.1/§5.3）：

- ``LIVE``：走真实 Gateway / Policy / 审批接缝构造入参并取真实拒绝；
- ``STATIC``（任意 host/port、DNS rebinding、SSRF、不可信重定向等网络类）：
  只做"构造入参被拒 + 源码/配置静态证据 + digest"，**不发起任何真实网络连接**。

报告口径：任何"100%"只能与 :data:`SAFETY_DISCLAIMER` 同时出现——**100% 仅代表
当前冻结测试集通过，不代表数学意义绝对安全**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

NEGATIVE_CASE_COUNT = 29
NEGATIVE_SET_VERSION = "negative-set-v1"

#: 四组条数冻结值（设计 §5.1：G1=9 / G2=7 / G3=7 / G4=6）。
GROUP_ORDER: tuple[str, ...] = ("G1", "G2", "G3", "G4")
GROUP_COUNTS: dict[str, int] = {"G1": 9, "G2": 7, "G3": 7, "G4": 6}

#: 跨租户归一化标签（设计 D-12）。**权威常量在** ``core.assurance.concurrency``
#: （T02）；这里保留同值镜像，避免 T01 反向依赖 T02 的模块（依赖方向必须单向）。
#: 一致性由 ``tests/integration/test_w11_cross_tenant.py`` 断言两者相等。
CROSS_TENANT_DENY_TAG = "OBJECT_AUTH_DENIED"

#: 设计 §5.2 稳定码表（闭集）；manifest 的 ``expected_deny_reason`` 只能取这些值。
STABLE_DENY_REASONS: tuple[str, ...] = (
    "R4_NOT_EXECUTABLE",
    "R3_AGENT_EXECUTION_FORBIDDEN",
    "R2_REQUIRES_APPROVAL",
    "APPROVAL_REQUIRED",
    "PROD_TARGET_NOT_ALLOWED",
    "ENVIRONMENT_NOT_ALLOWED",
    "TOOL_NOT_REGISTERED",
    "WRITE_NOT_AUTHORIZED",
    "APPROVAL_TAMPERED",
    "APPROVAL_EXPIRED",
    "APPROVAL_REVOKED",
    "APPROVAL_ALREADY_CONSUMED",
    "APPROVAL_DIGEST_MISMATCH",
    "NONCE_REPLAY",
    "ACTION_DIGEST_MISMATCH",
    "ENV_FINGERPRINT_MISMATCH",
    "OBJECT_AUTH_DENIED",
    "POLICY_DENIED",
)

#: 证据键**黑名单**（AC-01 与纪律 9）：命中即视为证据不合规，直接 fail。
#: 按 ``_`` 分词后整词比较，故 ``mysql_*`` 不会被 ``sql`` 误伤。
FORBIDDEN_EVIDENCE_TOKENS: tuple[str, ...] = (
    "payload",
    "request",
    "sql",
    "args",
    "arguments",
    "parameters",
    "params",
    "body",
    "dsn",
    "password",
    "secret",
    "credential",
)

#: 报告固定句（PRD §1 G1 原文）。不得改写、不得省略。
SAFETY_DISCLAIMER = "100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全"

#: 表述禁令（自查用）：出现即视为口径违规。
FORBIDDEN_CLAIMS: tuple[str, ...] = ("绝对安全", "100% 安全")


class NegativeSetError(Exception):
    """负面集清单/证据不合法。

    刻意**不**继承 ``DBGuardianError``：W11 零生产错误码（纪律 10），
    ``code`` 只是 W11 断言层标签。
    """

    def __init__(self, code: str, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(f"{code}: {self.message}")


class InducementMode(str, Enum):
    """诱导执行模式（设计 §5.1）。

    - ``LIVE``：真实 Gateway / Policy / 审批接缝；
    - ``STATIC``：源码/配置静态证据 + digest，不发起真实网络连接。
    """

    LIVE = "LIVE"
    STATIC = "STATIC"


class ObjectKind(str, Enum):
    """跨租户对象类型（AC-05 六类；T02 依赖本类型）。"""

    TASK = "TASK"
    EVIDENCE = "EVIDENCE"
    APPROVAL = "APPROVAL"
    REPORT = "REPORT"
    ENVIRONMENT = "ENVIRONMENT"
    AUDIT = "AUDIT"


class NegativeCase(BaseModel):
    """一条冻结的负面用例（``extra="forbid"``）。"""

    model_config = ConfigDict(extra="forbid")

    test_id: str = Field(min_length=1)
    group: Literal["G1", "G2", "G3", "G4"]
    inducement: str = Field(min_length=1)
    mode: InducementMode
    expected_deny_reason: str = Field(min_length=1)
    target: ObjectKind
    no_change_probe: str = Field(min_length=1)
    notes: str = ""


@dataclass(frozen=True)
class DenyObservation:
    """一次诱导的**观测事实**：被拒与否 + 拒因 + 生产码 + 目标行计数。"""

    denied: bool
    deny_reason: str | None
    error_code: str | None
    target_before: int
    target_after: int


@dataclass(frozen=True)
class DenyProbe:
    """端口层返回的**纯拒绝事实**（计数由 :func:`run_negative_case` 负责填）。"""

    denied: bool
    deny_reason: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class NegativeResult:
    """AC-01 四元组：``test_id`` / ``expected_deny_reason`` / ``actual`` / ``evidence``。"""

    test_id: str
    expected_deny_reason: str
    actual: DenyObservation
    evidence: dict[str, Any]

    @property
    def passed(self) -> bool:
        """AC-02 + AC-03 的合取：被拒、拒因一致、目标数据未变。"""
        return (
            self.actual.denied is True
            and self.actual.deny_reason == self.expected_deny_reason
            and self.actual.target_before == self.actual.target_after
        )


class NegativeSetManifest(BaseModel):
    """冻结清单（``extra="forbid"``，``test_id`` 唯一）。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal["negative-set-v1"] = NEGATIVE_SET_VERSION
    cases: tuple[NegativeCase, ...]

    @model_validator(mode="after")
    def _unique_ids(self) -> "NegativeSetManifest":
        ids = [case.test_id for case in self.cases]
        if len(ids) != len(set(ids)):
            duplicated = sorted({item for item in ids if ids.count(item) > 1})
            raise ValueError(f"负面集 test_id 必须唯一（AC-04），重复项：{duplicated}")
        return self

    def by_id(self) -> dict[str, NegativeCase]:
        return {case.test_id: case for case in self.cases}

    def group_counts(self) -> dict[str, int]:
        counts = {group: 0 for group in GROUP_ORDER}
        for case in self.cases:
            counts[case.group] += 1
        return counts


@runtime_checkable
class NegativePortsProtocol(Protocol):
    """``run_negative_case`` 依赖的三件事（duck-typed，卡内签名 ``ports: Any``）。"""

    def observe(self, case: NegativeCase) -> DenyProbe: ...

    def count_target(self, case: NegativeCase) -> int: ...

    def evidence(self, case: NegativeCase, observation: DenyObservation) -> Mapping[str, Any]: ...


def assert_manifest_frozen(manifest: NegativeSetManifest) -> None:
    """校验冻结口径：版本、总条数、四组条数（少 1 条即不通过）。

    Raises:
        NegativeSetError: 版本不符 / 条数不符 / 分组条数不符。
    """
    if manifest.version != NEGATIVE_SET_VERSION:
        raise NegativeSetError(
            "NEGATIVE_SET_VERSION_MISMATCH",
            f"version 必须为 {NEGATIVE_SET_VERSION}",
            details={"version": manifest.version},
        )
    if len(manifest.cases) != NEGATIVE_CASE_COUNT:
        raise NegativeSetError(
            "NEGATIVE_SET_COUNT_MISMATCH",
            f"冻结用例数必须为 {NEGATIVE_CASE_COUNT}（AC-02）",
            details={
                "expected": NEGATIVE_CASE_COUNT,
                "actual": len(manifest.cases),
                "missing_groups": {
                    group: GROUP_COUNTS[group]
                    for group in GROUP_ORDER
                    if manifest.group_counts()[group] < GROUP_COUNTS[group]
                },
            },
        )
    counts = manifest.group_counts()
    if counts != GROUP_COUNTS:
        raise NegativeSetError(
            "NEGATIVE_GROUP_COUNT_MISMATCH",
            "四组条数必须为 G1=9 / G2=7 / G3=7 / G4=6（设计 §5.1）",
            details={"expected": dict(GROUP_COUNTS), "actual": counts},
        )


def load_negative_set(path: Path) -> NegativeSetManifest:
    """读取并校验冻结清单（``benchmarks/w11/negative_cases.json``）。

    Raises:
        NegativeSetError: 清单结构非法或冻结口径不符。
    """
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NegativeSetError(
            "NEGATIVE_SET_MISSING", "负面集清单不存在", details={"path": str(target)}
        ) from None
    except (OSError, ValueError) as exc:
        raise NegativeSetError(
            "NEGATIVE_SET_UNREADABLE",
            "负面集清单不可读取或不是合法 JSON",
            details={"path": str(target), "cause": type(exc).__name__},
        ) from None
    try:
        manifest = NegativeSetManifest.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError 及模型校验器
        raise NegativeSetError(
            "NEGATIVE_SET_INVALID",
            "负面集清单未通过模型校验",
            details={"path": str(target), "cause": type(exc).__name__},
        ) from None
    assert_manifest_frozen(manifest)
    return manifest


def _assert_evidence_safe(evidence: Mapping[str, Any]) -> None:
    """AC-01：证据只允许结构化摘要，禁止参数原文 / SQL 文本 / 请求正文。"""
    for key in evidence:
        tokens = {token.strip().lower() for token in str(key).split("_")}
        hit = [item for item in FORBIDDEN_EVIDENCE_TOKENS if item in tokens]
        if hit:
            raise NegativeSetError(
                "EVIDENCE_UNSAFE",
                "证据键命中黑名单（不得携带参数原文/SQL/请求正文）",
                details={"key": str(key), "forbidden": hit},
            )


def run_negative_case(case: NegativeCase, *, ports: Any) -> NegativeResult:
    """执行一条冻结用例，返回 AC-01 四元组。

    端口契约（``ports`` 只需满足 :class:`NegativePortsProtocol`）：

    - ``observe(case) -> DenyProbe``：构造诱导入参并取真实/静态拒绝事实；
    - ``count_target(case) -> int``：按**本用例归属**统计目标行数（非全表）；
    - ``evidence(case, observation) -> Mapping``：结构化证据（事件 id/源码 digest/计数）。

    Args:
        case: 冻结用例。
        ports: 端口实现（真实接缝装配在测试里，生产包不持 DSN）。

    Returns:
        :class:`NegativeResult`，其 ``passed`` 为 AC-02 与 AC-03 的合取。

    Raises:
        NegativeSetError: 端口不完整、计数非法或证据不合规。
    """
    observe = getattr(ports, "observe", None)
    count_target = getattr(ports, "count_target", None)
    collect = getattr(ports, "evidence", None)
    if not (callable(observe) and callable(count_target) and callable(collect)):
        raise NegativeSetError(
            "PORTS_INCOMPLETE",
            "ports 必须实现 observe / count_target / evidence",
            details={"test_id": case.test_id},
        )

    # AC-03：拒绝前后各取一次只读计数（按本用例归属过滤）。
    target_before = int(count_target(case))
    probe = observe(case)
    if not isinstance(probe, DenyProbe):
        raise NegativeSetError(
            "PORTS_INVALID",
            "observe 必须返回 DenyProbe",
            details={"test_id": case.test_id, "returned": type(probe).__name__},
        )
    target_after = int(count_target(case))

    observation = DenyObservation(
        denied=bool(probe.denied),
        deny_reason=probe.deny_reason,
        error_code=probe.error_code,
        target_before=target_before,
        target_after=target_after,
    )

    evidence: dict[str, Any] = {
        "mode": case.mode.value,
        "deny_source": (
            "LIVE_SEAM" if case.mode is InducementMode.LIVE else "STATIC_EVIDENCE"
        ),
        "no_change_probe": case.no_change_probe,
        "counts": {"before": target_before, "after": target_after},
    }
    evidence.update(dict(collect(case, observation)))
    _assert_evidence_safe(evidence)

    if not evidence:
        raise NegativeSetError(
            "EVIDENCE_EMPTY", "证据不得为空（AC-01 非空率 100%）", details={"test_id": case.test_id}
        )
    if not case.test_id or not case.expected_deny_reason:
        raise NegativeSetError(
            "CASE_INCOMPLETE", "用例缺 test_id 或 expected_deny_reason", details={"test_id": case.test_id}
        )

    return NegativeResult(
        test_id=case.test_id,
        expected_deny_reason=case.expected_deny_reason,
        actual=observation,
        evidence=evidence,
    )


def assert_negative_set_complete(
    results: Sequence[NegativeResult], manifest: NegativeSetManifest
) -> None:
    """AC-01/AC-02/AC-04：结果集与冻结清单**逐条相等**且每条有四元组。

    Raises:
        NegativeSetError: 缺项、多项、期望值漂移或四元组不完整。
    """
    expected = manifest.by_id()
    actual_ids = {result.test_id for result in results}
    if len(results) != len(expected):
        raise NegativeSetError(
            "NEGATIVE_SET_INCOMPLETE",
            "结果条数与冻结清单不一致（少 1 条即不通过）",
            details={
                "expected": len(expected),
                "actual": len(results),
                "missing": sorted(set(expected) - actual_ids),
                "unexpected": sorted(actual_ids - set(expected)),
            },
        )
    missing = sorted(set(expected) - actual_ids)
    unexpected = sorted(actual_ids - set(expected))
    if missing or unexpected:
        raise NegativeSetError(
            "NEGATIVE_SET_INCOMPLETE",
            "结果集与冻结清单的 test_id 集合不相等",
            details={"missing": missing, "unexpected": unexpected},
        )
    for result in results:
        if not result.test_id or not result.expected_deny_reason:
            raise NegativeSetError(
                "NEGATIVE_TUPLE_INCOMPLETE",
                "四元组缺 test_id / expected_deny_reason",
                details={"test_id": result.test_id},
            )
        if result.actual is None or not isinstance(result.evidence, dict) or not result.evidence:
            raise NegativeSetError(
                "NEGATIVE_TUPLE_INCOMPLETE",
                "四元组缺 actual / evidence（AC-01 非空率 100%）",
                details={"test_id": result.test_id},
            )
        if result.expected_deny_reason != expected[result.test_id].expected_deny_reason:
            raise NegativeSetError(
                "NEGATIVE_EXPECTATION_DRIFT",
                "结果的 expected_deny_reason 与冻结清单不一致",
                details={
                    "test_id": result.test_id,
                    "manifest": expected[result.test_id].expected_deny_reason,
                    "result": result.expected_deny_reason,
                },
            )


def format_run_summary(
    results: Sequence[NegativeResult], manifest: NegativeSetManifest | None = None
) -> str:
    """生成汇总文本；**固定句必须出现在最后一行**（设计 §5.3 / R-01）。"""
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    lines = [f"W11 安全负面集：冻结用例 {total} 条，通过 {passed} 条，未通过 {total - passed} 条"]
    if manifest is not None:
        groups = manifest.by_id()
        for group in GROUP_ORDER:
            group_results = [
                result for result in results if groups.get(result.test_id) is not None
                and groups[result.test_id].group == group
            ]
            group_passed = sum(1 for result in group_results if result.passed)
            lines.append(
                f"  {group}: 冻结 {len(group_results)} 条 / 通过 {group_passed} 条"
            )
    live = sum(1 for result in results if result.evidence.get("mode") == InducementMode.LIVE.value)
    static = sum(1 for result in results if result.evidence.get("mode") == InducementMode.STATIC.value)
    lines.append(f"  模式分布：LIVE {live} 条 / STATIC {static} 条")
    lines.append(SAFETY_DISCLAIMER)
    return "\n".join(lines)


__all__ = [
    "CROSS_TENANT_DENY_TAG",
    "DenyObservation",
    "DenyProbe",
    "FORBIDDEN_CLAIMS",
    "FORBIDDEN_EVIDENCE_TOKENS",
    "GROUP_COUNTS",
    "GROUP_ORDER",
    "InducementMode",
    "NEGATIVE_CASE_COUNT",
    "NEGATIVE_SET_VERSION",
    "NegativeCase",
    "NegativePortsProtocol",
    "NegativeResult",
    "NegativeSetError",
    "NegativeSetManifest",
    "ObjectKind",
    "SAFETY_DISCLAIMER",
    "STABLE_DENY_REASONS",
    "assert_manifest_frozen",
    "assert_negative_set_complete",
    "format_run_summary",
    "load_negative_set",
    "run_negative_case",
]
