"""W09 T03 / AC-09、AC-10、AC-11：结构化报告模型与引用校验单测（离线）。

覆盖：两组六态 + 一组五态封闭且两两不等价；措辞禁令；状态一致性
（VERIFIED / PROPOSED / STALE / CONFIRMED 主动复核）；引用归属同码失效；
环境不符降 STALE；弱证据不升格；ActionState 由证据链阶段唯一确定。
"""

from __future__ import annotations

from typing import Any, Sequence

import pytest
from pydantic import ValidationError

from core.diagnosis.models import ConfidenceBand
from core.errors import ErrorCode
from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceQuality,
    WEAK_COLLECTION_STATUSES,
)
from core.report.models import (
    FORBIDDEN_COMPLETION_TERMS,
    ActionState,
    FactStatus,
    Recommendation,
    StructuredReport,
    VerificationState,
)
from core.report.validate import (
    ActionChainObservation,
    EvidenceRefInvalidError,
    derive_action_state,
    derive_chain_stage,
    derive_fact_status,
    derive_verification_state,
    inspect_evidence_refs,
    validate_evidence_refs,
)

_TENANT = "tenant_local"
_TASK = "task_w09_report"
_ENV = "env_mysql_local"


def _report(**overrides: Any) -> StructuredReport:
    """构造一份通过全部模型校验的基线报告。"""
    values: dict[str, Any] = {
        "report_id": "rpt_w09_test",
        "tenant_id": _TENANT,
        "task_id": _TASK,
        "database_id": "lab_mysql_01",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w09-report-v1",
        "environment_id": _ENV,
        "policy_version": "w06-v1",
        "conclusion": "事实等级与动作状态如实记录，无完成措辞",
        "fact_status": FactStatus.CONFIRMED,
        "confidence_band": ConfidenceBand.HIGH,
        "supporting_evidence_ids": ["ev_ok"],
        "contradicting_evidence_ids": [],
        "missing_evidence": [],
        "weak_evidence_ids": [],
        "recommendations": [Recommendation(text="建议复核索引使用情况")],
        "action_state": ActionState.EXECUTED,
        "verification_state": VerificationState.NONE,
    }
    values.update(overrides)
    return StructuredReport(**values)


def _evidence(
    evidence_id: str = "ev_ok",
    *,
    tenant_id: str = _TENANT,
    task_id: str = _TASK,
    environment_id: str = _ENV,
    collection_status: CollectionStatus = CollectionStatus.SUCCESS,
    quality: EvidenceQuality = EvidenceQuality.HIGH,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        task_id=task_id,
        environment_id=environment_id,
        db_engine="mysql",
        db_version="8.0.46",
        adapter_version="w09-report-v1",
        source_tool="get_db_version",
        observed_at="2026-02-01T00:00:00+00:00",
        collection_status=collection_status,
        quality=quality,
        payload_hash="a" * 64,
    )


class _FakePort:
    """内存证据端口：与 EvidenceRepository.fetch_by_ids 同口径（强制租户过滤）。"""

    def __init__(self, rows: Sequence[Evidence]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, list[str]]] = []

    def fetch_by_ids(
        self, tenant_id: str, evidence_ids: Sequence[str]
    ) -> list[Evidence]:
        self.calls.append((tenant_id, list(evidence_ids)))
        wanted = set(evidence_ids)
        return [
            row
            for row in self.rows
            if row.tenant_id == tenant_id and row.evidence_id in wanted
        ]


# --------------------------------------------------------------------------- #
# AC-09：封闭枚举、extra=forbid
# --------------------------------------------------------------------------- #


def test_status_enums_are_closed_and_pairwise_distinct() -> None:
    assert len(FactStatus) == 6
    assert len(ActionState) == 6
    assert len(VerificationState) == 5
    assert len({item.value for item in FactStatus}) == 6
    assert len({item.value for item in ActionState}) == 6
    assert len({item.value for item in VerificationState}) == 5
    assert {item.value for item in FactStatus}.isdisjoint(
        {item.value for item in ActionState}
    )
    assert ActionState.PROPOSED.value != ActionState.NOT_EXECUTED.value
    assert ActionState.EXECUTED.value != ActionState.VERIFIED.value
    assert ActionState.OUTCOME_UNKNOWN.value != ActionState.VERIFIED.value


def test_report_and_recommendation_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _report(unexpected_field="x")
    with pytest.raises(ValidationError):
        Recommendation(text="建议复核索引", unexpected_field="x")


# --------------------------------------------------------------------------- #
# AC-11：措辞禁令
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("term", FORBIDDEN_COMPLETION_TERMS)
def test_recommendation_rejects_completion_wording(term: str) -> None:
    with pytest.raises(ValidationError):
        Recommendation(text=f"建议：{term}，请复核")


@pytest.mark.parametrize("term", ["FIXED", "Resolved", "ALREADY APPLIED", "已修复"])
def test_wording_ban_is_case_insensitive(term: str) -> None:
    with pytest.raises(ValidationError):
        Recommendation(text=f"建议：{term}")


@pytest.mark.parametrize(
    "action_state",
    [
        ActionState.PROPOSED,
        ActionState.NOT_EXECUTED,
        ActionState.AWAITING_APPROVAL,
    ],
)
def test_unexecuted_conclusion_rejects_completion_wording(
    action_state: ActionState,
) -> None:
    with pytest.raises(ValidationError):
        _report(action_state=action_state, conclusion="索引问题已修复，请知悉")


def test_executed_conclusion_is_not_subject_to_unexecuted_wording_ban() -> None:
    report = _report(
        action_state=ActionState.EXECUTED, conclusion="执行动作已修复该项"
    )
    assert report.action_state is ActionState.EXECUTED


# --------------------------------------------------------------------------- #
# 状态一致性（设计 §4.2「模型层校验器（不可绕过）」）
# --------------------------------------------------------------------------- #


def test_verified_requires_pass_and_non_empty_result_id() -> None:
    ok = _report(
        action_state=ActionState.VERIFIED,
        verification_state=VerificationState.PASS,
        verification_result_id="vr_1",
    )
    assert ok.action_state is ActionState.VERIFIED
    with pytest.raises(ValidationError):
        _report(action_state=ActionState.VERIFIED, verification_result_id="vr_1")
    with pytest.raises(ValidationError):
        _report(
            action_state=ActionState.VERIFIED,
            verification_state=VerificationState.FAIL,
            verification_result_id="vr_1",
        )
    with pytest.raises(ValidationError):
        _report(
            action_state=ActionState.VERIFIED,
            verification_state=VerificationState.PASS,
            verification_result_id=None,
        )


def test_proposed_requires_only_proposed_recommendations() -> None:
    ok = _report(action_state=ActionState.PROPOSED)
    assert ok.action_state is ActionState.PROPOSED
    with pytest.raises(ValidationError):
        _report(action_state=ActionState.PROPOSED, recommendations=[])
    with pytest.raises(ValidationError):
        _report(
            action_state=ActionState.PROPOSED,
            recommendations=[
                Recommendation(text="建议复核索引", state=ActionState.EXECUTED)
            ],
        )


def test_stale_requires_non_empty_missing_evidence() -> None:
    ok = _report(fact_status=FactStatus.STALE, missing_evidence=["ev_lost"])
    assert ok.fact_status is FactStatus.STALE
    with pytest.raises(ValidationError):
        _report(fact_status=FactStatus.STALE, missing_evidence=[])


def test_confirmed_active_recheck_blocks_weak_or_dropped_refs() -> None:
    """防手工构造 StructuredReport(...) 绕过 derive_fact_status。"""
    with pytest.raises(ValidationError):
        _report(weak_evidence_ids=["ev_partial"])
    with pytest.raises(ValidationError):
        _report(missing_evidence=["ev_lost"])
    with pytest.raises(ValidationError):
        _report(contradicting_evidence_ids=["ev_other"])


def test_unknown_verdict_never_maps_to_executed_or_verified() -> None:
    with pytest.raises(ValidationError):
        _report(
            action_state=ActionState.EXECUTED,
            verification_state=VerificationState.UNKNOWN,
        )
    with pytest.raises(ValidationError):
        _report(
            action_state=ActionState.VERIFIED,
            verification_state=VerificationState.UNKNOWN,
            verification_result_id="vr_1",
        )


# --------------------------------------------------------------------------- #
# AC-09 / AC-10 / AC-12：引用校验与事实等级
# --------------------------------------------------------------------------- #


def test_cross_tenant_and_missing_ids_share_the_same_ref_invalid_code() -> None:
    """跨租户与不存在返回同一个 EVIDENCE_REF_INVALID，不泄漏存在性。"""
    foreign = _evidence("ev_ok", tenant_id="tenant_other")
    cases = [
        _FakePort([foreign]),
        _FakePort([]),
    ]
    codes: list[ErrorCode] = []
    for port in cases:
        check = inspect_evidence_refs(_report(fact_status=FactStatus.UNKNOWN), port)
        assert check.invalid_ids == ("ev_ok",)
        assert check.error_code is ErrorCode.EVIDENCE_REF_INVALID
        assert check.fact_status is FactStatus.STALE
        assert check.missing_evidence
        with pytest.raises(EvidenceRefInvalidError) as raised:
            validate_evidence_refs(_report(fact_status=FactStatus.UNKNOWN), port)
        codes.append(raised.value.code)
    assert codes == [ErrorCode.EVIDENCE_REF_INVALID] * 2
    assert codes[0] is codes[1]


def test_env_mismatch_is_recorded_and_downgrades_to_stale() -> None:
    port = _FakePort([_evidence("ev_ok", environment_id="env_pg_local")])
    check = inspect_evidence_refs(_report(fact_status=FactStatus.UNKNOWN), port)
    assert check.env_mismatch_ids == ("ev_ok",)
    assert check.invalid_ids == ()
    assert check.fact_status is FactStatus.STALE
    assert "ev_ok" in check.missing_evidence


@pytest.mark.parametrize(
    "status,expected",
    [
        (CollectionStatus.PARTIAL, FactStatus.PARTIAL),
        (CollectionStatus.TIMEOUT, FactStatus.PARTIAL),
        (CollectionStatus.NOT_SUPPORTED, FactStatus.PARTIAL),
        (CollectionStatus.UNKNOWN, FactStatus.PARTIAL),
        (CollectionStatus.DENIED, FactStatus.DENIED),
        (CollectionStatus.SUCCESS, FactStatus.CONFIRMED),
    ],
)
def test_derive_fact_status_priority_table(
    status: CollectionStatus, expected: FactStatus
) -> None:
    report = _report(fact_status=FactStatus.UNKNOWN)
    assert derive_fact_status(report, [_evidence(collection_status=status)]) is expected


@pytest.mark.parametrize("quality", [EvidenceQuality.LOW, EvidenceQuality.UNKNOWN])
def test_low_or_unknown_quality_never_confirmed(quality: EvidenceQuality) -> None:
    report = _report(fact_status=FactStatus.UNKNOWN)
    assert (
        derive_fact_status(report, [_evidence(quality=quality)])
        is FactStatus.PARTIAL
    )


def test_weak_collection_status_set_is_defined_only_in_evidence_models() -> None:
    assert WEAK_COLLECTION_STATUSES == frozenset(
        {
            CollectionStatus.PARTIAL,
            CollectionStatus.DENIED,
            CollectionStatus.TIMEOUT,
            CollectionStatus.NOT_SUPPORTED,
            CollectionStatus.UNKNOWN,
        }
    )
    for status in WEAK_COLLECTION_STATUSES:
        report = _report(fact_status=FactStatus.UNKNOWN)
        assert (
            derive_fact_status(report, [_evidence(collection_status=status)])
            is not FactStatus.CONFIRMED
        )


def test_remaining_priority_rules() -> None:
    assert (
        derive_fact_status(
            _report(fact_status=FactStatus.UNKNOWN, supporting_evidence_ids=[]), []
        )
        is FactStatus.UNKNOWN
    )
    assert (
        derive_fact_status(
            _report(
                fact_status=FactStatus.UNKNOWN,
                contradicting_evidence_ids=["ev_other"],
            ),
            [_evidence(), _evidence("ev_other")],
        )
        is FactStatus.CONFLICTED
    )
    assert (
        derive_fact_status(_report(fact_status=FactStatus.UNKNOWN), [])
        is FactStatus.STALE
    )
    assert (
        derive_fact_status(
            _report(
                fact_status=FactStatus.UNKNOWN,
                contradicting_evidence_ids=["ev_other"],
                verification_state=VerificationState.PASS,
            ),
            [
                _evidence(collection_status=CollectionStatus.DENIED),
                _evidence("ev_other"),
            ],
        )
        is FactStatus.DENIED
    )


# --------------------------------------------------------------------------- #
# ActionState 由证据链阶段唯一确定（禁止布尔组合推导、禁止第 7 态）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "observation,expected",
    [
        (ActionChainObservation(), ActionState.PROPOSED),
        (
            ActionChainObservation(approval_status="PENDING"),
            ActionState.AWAITING_APPROVAL,
        ),
        (
            ActionChainObservation(approval_status="APPROVED"),
            ActionState.NOT_EXECUTED,
        ),
        (ActionChainObservation(has_step_prepared=True), ActionState.OUTCOME_UNKNOWN),
        (
            ActionChainObservation(has_step_prepared=True, has_step_observed=True),
            ActionState.EXECUTED,
        ),
        (
            ActionChainObservation(
                has_step_observed=True, verdict="FAIL", verification_result_id="vr_1"
            ),
            ActionState.EXECUTED,
        ),
        (
            ActionChainObservation(
                has_step_observed=True, verdict="PASS", verification_result_id="vr_1"
            ),
            ActionState.VERIFIED,
        ),
        (
            ActionChainObservation(has_step_observed=True, verdict="UNKNOWN"),
            ActionState.OUTCOME_UNKNOWN,
        ),
    ],
)
def test_action_state_is_determined_by_chain_stage(
    observation: ActionChainObservation, expected: ActionState
) -> None:
    assert derive_action_state(observation) is expected
    assert derive_chain_stage(observation).value == expected.value


def test_all_six_action_states_reachable_and_no_seventh_exists() -> None:
    states = {
        derive_action_state(observation)
        for observation in (
            ActionChainObservation(),
            ActionChainObservation(approval_status="PENDING"),
            ActionChainObservation(approval_status="APPROVED"),
            ActionChainObservation(has_step_prepared=True),
            ActionChainObservation(has_step_observed=True),
            ActionChainObservation(
                has_step_observed=True, verdict="PASS", verification_result_id="vr_1"
            ),
        )
    }
    assert states == set(ActionState)
    assert len(states) == 6


def test_unknown_verdict_never_becomes_executed_or_verified() -> None:
    for observed in (False, True):
        state = derive_action_state(
            ActionChainObservation(
                has_step_observed=observed,
                verdict="UNKNOWN",
                verification_result_id="vr_1",
            )
        )
        assert state is ActionState.OUTCOME_UNKNOWN
        assert state not in (ActionState.EXECUTED, ActionState.VERIFIED)


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("PASS", VerificationState.PASS),
        ("FAIL", VerificationState.FAIL),
        ("UNKNOWN", VerificationState.UNKNOWN),
        (None, VerificationState.NONE),
    ],
)
def test_verification_state_mapping(verdict: str | None, expected: VerificationState) -> None:
    assert (
        derive_verification_state(ActionChainObservation(verdict=verdict)) is expected
    )
    assert (
        derive_verification_state(
            ActionChainObservation(verdict=None, approval_status="PENDING")
        )
        is VerificationState.PENDING
    )


# --------------------------------------------------------------------------- #
# 完成判据 9：ReportRepository.insert 只追加（假 client，不连库）
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.executed.append((sql, params))
        return None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        self.fetched.append((sql, params))
        return []


def test_report_repository_insert_is_append_only() -> None:
    from core.report.repository import REPORT_SNAPSHOTS, ReportRepository

    client = _FakeClient()
    repository = ReportRepository(client)  # type: ignore[arg-type]
    report = _report()
    assert repository.insert(report) is report

    assert len(client.executed) == 1
    sql, params = client.executed[0]
    assert sql.lstrip().upper().startswith("INSERT INTO")
    assert REPORT_SNAPSHOTS in sql
    assert params[0] == report.report_id
    assert params[4] == FactStatus.CONFIRMED.value
    assert params[5] == ActionState.EXECUTED.value
    assert params[6] == VerificationState.NONE.value
    assert params[11] == "none"
    for forbidden in ("DELETE", "UPDATE", "TRUNCATE", "DROP"):
        assert forbidden not in sql.upper()

    assert repository.list_by_task(_TASK, _TENANT) == []
    assert client.fetched[0][0].upper().startswith("SELECT")
    assert _TENANT in client.fetched[0][1]
