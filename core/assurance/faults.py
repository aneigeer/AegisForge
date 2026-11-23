"""W11 T03：六类故障注入与恢复核对（P0-W11-03 / AC-08…AC-11）。

设计依据：``docs/w11/02-design.md`` §7（§7.1 严格六类、§7.2 注入方式、§7.3
fail-closed/告警/恢复）、§11 签名；裁定 W11-Q5（**严格六类**，网络分区/磁盘满不纳入）。

本模块只做三件事，**不新增生产注入分支**（``docs/w08/02-design.md:517``
「不为测试加生产分支」）：

1. 冻结六类与「可自动恢复 vs 必须人工介入」边界（:data:`FAULT_BOUNDARIES`）；
2. 把**既有接缝**上的注入结果包成 :class:`FaultInjectionResult`；
3. 提供恢复完整性的唯一谓词 :func:`assert_recovery_integrity`（AC-10/AC-11）。

注入本身发生在测试侧：``monkeypatch`` 仓储 ``insert_observed``/``insert_event``、
客户端 ``fetch_all``/``execute``、``stack.reconciler.sweep`` 抛错、子进程
``Popen.kill()``。真正的 W04 故障会话（``benchmarks/faults/injector.py:308
FaultSession``）只在调用方显式注入 ``session_factory`` 时使用，且**函数内惰性
import**（顶层 import 会在只装 wheel 的环境下 ``ImportError``，W10 设计 R-13 同源）。

``audit_chain_ok`` 一律由 ``core/audit/service.py:153 AuditService.verify`` 给出
（复用 ``verify_event_chain``，**不在此重写链校验**）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

FAULT_CASE_COUNT = 6
FAULT_SET_VERSION = "fault-set-v1"


class FaultClass(str, Enum):
    """严格六类（W11-Q5）。网络分区 / 磁盘满**不在**枚举内。"""

    EVIDENCE_STORE = "EVIDENCE_STORE"
    AUDIT_WRITER = "AUDIT_WRITER"
    DB_DISCONNECT = "DB_DISCONNECT"
    MODEL_SERVICE = "MODEL_SERVICE"
    WORKER_CRASH = "WORKER_CRASH"
    CONTROL_DB_TRANSIENT = "CONTROL_DB_TRANSIENT"


#: 冻结顺序（与 ``benchmarks/w11/fault_cases.json`` / 报告逐条对齐）。
FAULT_CLASSES: tuple[FaultClass, ...] = (
    FaultClass.EVIDENCE_STORE,
    FaultClass.AUDIT_WRITER,
    FaultClass.DB_DISCONNECT,
    FaultClass.MODEL_SERVICE,
    FaultClass.WORKER_CRASH,
    FaultClass.CONTROL_DB_TRANSIENT,
)


class RecoveryBoundary(str, Enum):
    """恢复边界：``AUTO`` 可自动恢复 / ``MANUAL`` 必须人工介入。"""

    AUTO = "AUTO"
    MANUAL = "MANUAL"


#: 六类的恢复边界（**不得**写成"全部可自动恢复"）。
#: - AUTO：故障窗口结束后下一轮即可自愈，且不存在"结果未知"的副作用；
#: - MANUAL：存在无法自动判定的副作用（审计缺口/写结果未知/需核对是否已执行）。
FAULT_BOUNDARIES: dict[FaultClass, RecoveryBoundary] = {
    FaultClass.EVIDENCE_STORE: RecoveryBoundary.AUTO,
    FaultClass.AUDIT_WRITER: RecoveryBoundary.MANUAL,
    FaultClass.DB_DISCONNECT: RecoveryBoundary.MANUAL,
    FaultClass.MODEL_SERVICE: RecoveryBoundary.AUTO,
    FaultClass.WORKER_CRASH: RecoveryBoundary.MANUAL,
    FaultClass.CONTROL_DB_TRANSIENT: RecoveryBoundary.AUTO,
}

#: 恢复边界的理由（报告与 JSON 共用同一套文案，避免两处漂移）。
FAULT_BOUNDARY_REASONS: dict[FaultClass, str] = {
    FaultClass.EVIDENCE_STORE: "证据写入失败后重试即可补齐，不存在写目标库的副作用",
    FaultClass.AUDIT_WRITER: (
        "账本不可写期间的“必须成功”事件无法自动补写，只能靠结构化日志人工核对（ADR-0006:129）"
    ),
    FaultClass.DB_DISCONNECT: "目标库断连时写操作结果未知，必须人工确认是否已提交（不得自动判成功/失败）",
    FaultClass.MODEL_SERVICE: "模型不可用时进入确定性降级路径（规则/已采集证据），恢复后自动重试规划",
    FaultClass.WORKER_CRASH: "崩溃时“已 Prepared 未 Observed”只能收敛为待核对，是否已执行须人工确认",
    FaultClass.CONTROL_DB_TRANSIENT: "控制库短暂故障为可重试的瞬时错误，fail closed 后下一轮自动恢复",
}

REASON_RED_LINE_REPLAY = "REPLAY_OCCURRED"
REASON_RED_LINE_TOOL_CALLS = "TOOL_CALLS_DELTA"
REASON_RED_LINE_SEQ = "SEQ_NOT_CONTINUOUS"
REASON_RED_LINE_FAIL_OPEN = "FAIL_OPEN"
RECOVERY_INTEGRITY_FAILED = "RECOVERY_INTEGRITY_FAILED"


class FaultTypeError(TypeError):
    """非六大类之一（含"第 7 类"）：直接拒绝，不做兼容。"""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = "UNKNOWN_FAULT_CLASS"
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {message}")


class RecoveryIntegrityError(AssertionError):
    """恢复完整性谓词不成立（AC-10/AC-11 红线）。"""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = RECOVERY_INTEGRITY_FAILED
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class RecoveryCheck:
    """一次故障后的恢复完整性事实（六项，缺一不可）。"""

    seq_continuous: bool
    audit_chain_ok: bool
    evidence_refs_resolvable: bool
    state_version_monotonic: bool
    tool_calls_delta: int
    replay_occurred: bool


@dataclass(frozen=True)
class FaultProbeOutcome:
    """探针在**故障窗口内**实测到的事实（由测试在既有接缝上采集）。

    刻意要求显式 ``injected``：注入没真正生效时必须如实为 ``False``，
    不允许"看起来跑过"就当作注入成功（W10-Q6 同源口径）。
    """

    injected: bool
    fail_closed: bool
    alert_emitted: bool
    recovery: RecoveryCheck
    notes: str = ""


@dataclass(frozen=True)
class FaultInjectionResult:
    """单类故障的完整结论（喂给 ``publish_decision`` 的第三份结果）。"""

    test_id: str
    fault_class: FaultClass
    injected: bool
    fail_closed_ok: bool
    alert_emitted: bool
    recovery: RecoveryCheck
    boundary: RecoveryBoundary
    notes: str = ""


class FaultCaseSpec(BaseModel):
    """冻结的一条故障用例（``extra="forbid"``）。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    fault_class: FaultClass
    boundary: RecoveryBoundary
    recovery_scope: str = Field(min_length=1)
    notes: str = ""


class FaultCaseManifest(BaseModel):
    """冻结清单：恰 6 条、六类各一条。"""

    model_config = ConfigDict(extra="forbid")

    version: str = FAULT_SET_VERSION
    cases: tuple[FaultCaseSpec, ...]

    @model_validator(mode="after")
    def _unique_and_complete(self) -> "FaultCaseManifest":
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("fault_cases.case_id 必须唯一")
        classes = [item.fault_class for item in self.cases]
        if len(classes) != len(set(classes)):
            raise ValueError("fault_cases.fault_class 必须唯一（六类各一条）")
        return self

    def by_class(self) -> dict[FaultClass, FaultCaseSpec]:
        return {item.fault_class: item for item in self.cases}


def assert_fault_manifest_frozen(manifest: FaultCaseManifest) -> None:
    """冻结口径：版本、条数恰 6、六类齐备、边界与 :data:`FAULT_BOUNDARIES` 一致。"""
    if manifest.version != FAULT_SET_VERSION:
        raise FaultTypeError(
            f"清单版本必须为 {FAULT_SET_VERSION}", details={"version": manifest.version}
        )
    if len(manifest.cases) != FAULT_CASE_COUNT:
        raise FaultTypeError(
            f"冻结故障用例数必须为 {FAULT_CASE_COUNT}（W11-Q5 严格六类）",
            details={"actual": len(manifest.cases)},
        )
    by_class = manifest.by_class()
    missing = [item.value for item in FAULT_CLASSES if item not in by_class]
    if missing:
        raise FaultTypeError("缺少故障类", details={"missing": missing})
    extra = [item.value for item in by_class if item not in FAULT_CLASSES]
    if extra:
        raise FaultTypeError(
            "出现第 7 类故障（网络分区/磁盘满不纳入 W11-Q5）", details={"extra": extra}
        )
    for fault_class, spec in by_class.items():
        expected = FAULT_BOUNDARIES[fault_class]
        if spec.boundary is not expected:
            raise FaultTypeError(
                "恢复边界与冻结口径不一致",
                details={
                    "fault_class": fault_class.value,
                    "manifest": spec.boundary.value,
                    "expected": expected.value,
                },
            )


def load_fault_cases(path: Path) -> FaultCaseManifest:
    """读取并校验冻结清单（``benchmarks/w11/fault_cases.json``）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    manifest = FaultCaseManifest.model_validate(payload)
    assert_fault_manifest_frozen(manifest)
    return manifest


def assert_recovery_integrity(check: RecoveryCheck) -> None:
    """AC-10/AC-11 唯一谓词：六项全过才允许判"恢复完整"。

    Raises:
        RecoveryIntegrityError: 任一项不成立（含**恢复期重放**，这是红线）。
    """
    failures: list[str] = []
    if not check.seq_continuous:
        failures.append(REASON_RED_LINE_SEQ)
    if not check.audit_chain_ok:
        failures.append("AUDIT_CHAIN_BROKEN")
    if not check.evidence_refs_resolvable:
        failures.append("EVIDENCE_REF_UNRESOLVED")
    if not check.state_version_monotonic:
        failures.append("STATE_VERSION_REGRESSED")
    if check.tool_calls_delta != 0:
        failures.append(REASON_RED_LINE_TOOL_CALLS)
    if check.replay_occurred:
        failures.append(REASON_RED_LINE_REPLAY)
    if failures:
        raise RecoveryIntegrityError(
            "恢复完整性不足：不允许判为「恢复完成」",
            details={
                "failures": failures,
                "tool_calls_delta": check.tool_calls_delta,
                "replay_occurred": check.replay_occurred,
            },
        )


class FaultSessionAdapter:
    """把"既有接缝上的注入 + 恢复取样"包成一次 :class:`FaultInjectionResult`。

    Args:
        session_factory: 可选；返回 W04 ``FaultSession`` 的**零参**可调用对象
            （调用方用 ``functools.partial(FaultSession, "<冻结 scenario_id>")`` 绑定，
            本模块**不编造** scenario_id）。为 ``None`` 时走测试侧 monkeypatch 接缝。

    惰性 import 纪律：``benchmarks.*`` 只在本类**方法体内**导入，模块顶层不 import。
    """

    _TEST_ID_PREFIX = "fault"

    def __init__(self, *, session_factory: Callable[..., Any] | None = None) -> None:
        self._session_factory = session_factory

    # ---- public --------------------------------------------------------
    def run(self, cls: FaultClass, *, probe: Callable[[], Any]) -> FaultInjectionResult:
        """在第 ``cls`` 类故障下执行 ``probe`` 并汇总结论。

        Args:
            cls: 六大类之一（其它值一律 :class:`FaultTypeError`）。
            probe: 零参可调用；必须返回 :class:`FaultProbeOutcome`。

        Returns:
            :class:`FaultInjectionResult`；``boundary`` 取冻结边界，
            ``injected`` 由"会话结果 ∧ 探针自报"共同决定（任一为假即假）。
        """
        if not isinstance(cls, FaultClass):
            raise FaultTypeError(
                f"未知故障类：{cls!r}（严格六类，网络分区/磁盘满不纳入）",
                details={"fault_class": str(cls)},
            )
        if not callable(probe):
            raise FaultTypeError("probe 必须是可调用对象", details={"fault_class": cls.value})

        if self._session_factory is None:
            outcome = self._call_probe(probe)
            injected = bool(outcome.injected)
            source = "EXISTING_SEAM"
        else:
            outcome, session_injected = self._run_in_session(probe)
            injected = bool(session_injected and outcome.injected)
            source = "FAULT_SESSION"

        notes = outcome.notes
        return FaultInjectionResult(
            test_id=f"{self._TEST_ID_PREFIX}-{cls.value.lower().replace('_', '-')}",
            fault_class=cls,
            injected=injected,
            fail_closed_ok=bool(outcome.fail_closed),
            alert_emitted=bool(outcome.alert_emitted),
            recovery=outcome.recovery,
            boundary=FAULT_BOUNDARIES[cls],
            notes=f"{source}；{notes}" if notes else source,
        )

    # ---- internals -----------------------------------------------------
    def _call_probe(self, probe: Callable[[], Any]) -> FaultProbeOutcome:
        outcome = probe()
        if not isinstance(outcome, FaultProbeOutcome):
            raise FaultTypeError(
                "probe 必须返回 FaultProbeOutcome（注入结果必须显式，不得推断）",
                details={"returned": type(outcome).__name__},
            )
        return outcome

    def _run_in_session(
        self, probe: Callable[[], Any]
    ) -> tuple[FaultProbeOutcome, bool]:
        """打开 W04 ``FaultSession`` 执行探针；任何注入/重置失败都不得伪造成功。"""
        # 函数内惰性 import（R-13）：顶层 import benchmarks.* 在只装 wheel 环境会失败。
        from benchmarks.faults.injector import (  # noqa: PLC0415
            FaultInjectionError,
            InjectionResult,
        )

        factory = self._session_factory
        assert factory is not None  # run() 已判空
        try:
            with factory() as injection:
                outcome = self._call_probe(probe)
        except FaultInjectionError as exc:
            raise FaultTypeError(
                "故障注入失败（不得伪造注入成功）",
                details={"code": exc.code, "details": exc.details},
            ) from None
        injected = True
        if isinstance(injection, InjectionResult):
            injected = bool(injection.injected)
        return outcome, injected


def summarize_fault_results(results: Sequence[FaultInjectionResult]) -> dict[str, Any]:
    """六类故障汇总（结构化；不含任何业务请求正文）。"""
    return {
        "case_count": len(results),
        "fault_classes": [result.fault_class.value for result in results],
        "injected": sum(1 for result in results if result.injected),
        "fail_closed_ok": sum(1 for result in results if result.fail_closed_ok),
        "alert_emitted": sum(1 for result in results if result.alert_emitted),
        "auto": sum(1 for result in results if result.boundary is RecoveryBoundary.AUTO),
        "manual": sum(1 for result in results if result.boundary is RecoveryBoundary.MANUAL),
        "replay_occurred": sum(1 for result in results if result.recovery.replay_occurred),
        "tool_calls_delta_total": sum(result.recovery.tool_calls_delta for result in results),
    }


__all__ = [
    "FAULT_BOUNDARIES",
    "FAULT_BOUNDARY_REASONS",
    "FAULT_CASE_COUNT",
    "FAULT_CLASSES",
    "FAULT_SET_VERSION",
    "FaultCaseManifest",
    "FaultCaseSpec",
    "FaultClass",
    "FaultInjectionResult",
    "FaultProbeOutcome",
    "FaultSessionAdapter",
    "FaultTypeError",
    "RECOVERY_INTEGRITY_FAILED",
    "RecoveryBoundary",
    "RecoveryCheck",
    "RecoveryIntegrityError",
    "assert_fault_manifest_frozen",
    "assert_recovery_integrity",
    "load_fault_cases",
    "summarize_fault_results",
]
