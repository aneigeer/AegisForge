"""W11 T01 单元测试：负面集冻结清单与四元组不变量（AC-01…AC-04）。

只跑纯逻辑（不连库、不做网络），覆盖：

- 冻结口径：29 条 / G1=9、G2=7、G3=7、G4=6 / version=``negative-set-v1``；
- ``test_id`` 唯一（AC-04）与 ``expected_deny_reason`` 取自设计 §5.2 稳定码表；
- ``mode`` 二分（LIVE 23 / STATIC 6）与分组分布（G1 STATIC 2、G3 STATIC 4）；
- 四元组非空（AC-01）、``passed`` 的合取语义（AC-02 + AC-03）；
- 证据键黑名单（禁止参数原文/SQL/请求正文）；
- 报告固定句（R-01）：**100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest
from pydantic import ValidationError

from core.assurance.negative import (
    CROSS_TENANT_DENY_TAG,
    FORBIDDEN_CLAIMS,
    GROUP_COUNTS,
    NEGATIVE_CASE_COUNT,
    NEGATIVE_SET_VERSION,
    SAFETY_DISCLAIMER,
    STABLE_DENY_REASONS,
    DenyObservation,
    DenyProbe,
    InducementMode,
    NegativeCase,
    NegativeResult,
    NegativeSetError,
    NegativeSetManifest,
    ObjectKind,
    assert_manifest_frozen,
    assert_negative_set_complete,
    format_run_summary,
    load_negative_set,
    run_negative_case,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "benchmarks" / "w11" / "negative_cases.json"
_NEGATIVE_SOURCE = _REPO_ROOT / "core" / "assurance" / "negative.py"

_PROBE_CATALOG = frozenset(
    {
        "task_events_by_task_id",
        "tasks_by_task_id",
        "evidence_by_task_id",
        "approvals_by_task_id",
        "report_snapshots_by_task_id",
        "environments_by_tenant_id",
        "audit_events_by_task_id",
    }
)


@pytest.fixture(scope="module")
def manifest() -> NegativeSetManifest:
    return load_negative_set(_MANIFEST_PATH)


def _case(**overrides: Any) -> NegativeCase:
    payload: dict[str, Any] = {
        "test_id": "g1_stub_case",
        "group": "G1",
        "inducement": "stub_inducement",
        "mode": InducementMode.LIVE,
        "expected_deny_reason": "R4_NOT_EXECUTABLE",
        "target": ObjectKind.TASK,
        "no_change_probe": "task_events_by_task_id",
    }
    payload.update(overrides)
    return NegativeCase.model_validate(payload)


class _FakePorts:
    """端口桩：按构造参数返回拒绝事实与两次计数（before/after）。"""

    def __init__(
        self,
        *,
        denied: bool = True,
        deny_reason: str | None = "R4_NOT_EXECUTABLE",
        error_code: str | None = "POLICY_DENIED",
        counts: Sequence[int] = (0, 0),
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._denied = denied
        self._deny_reason = deny_reason
        self._error_code = error_code
        self._counts = list(counts)
        self._extra = dict(extra or {"source_lines": "core/security/policy.py:218"})

    def observe(self, case: NegativeCase) -> DenyProbe:
        return DenyProbe(
            denied=self._denied, deny_reason=self._deny_reason, error_code=self._error_code
        )

    def count_target(self, case: NegativeCase) -> int:
        return self._counts.pop(0)

    def evidence(self, case: NegativeCase, observation: DenyObservation) -> dict[str, Any]:
        return dict(self._extra)


def _result(test_id: str, *, expected: str = "R4_NOT_EXECUTABLE", denied: bool = True,
            before: int = 0, after: int = 0, evidence: dict[str, Any] | None = None) -> NegativeResult:
    return NegativeResult(
        test_id=test_id,
        expected_deny_reason=expected,
        actual=DenyObservation(
            denied=denied,
            deny_reason=expected,
            error_code="POLICY_DENIED",
            target_before=before,
            target_after=after,
        ),
        evidence=dict(evidence or {"mode": "LIVE"}),
    )


# ---------------------------------------------------------------------------
# 冻结口径
# ---------------------------------------------------------------------------


def test_manifest_version_and_total_count_are_frozen(manifest: NegativeSetManifest) -> None:
    assert manifest.version == NEGATIVE_SET_VERSION == "negative-set-v1"
    assert len(manifest.cases) == NEGATIVE_CASE_COUNT == 29


def test_group_counts_are_frozen(manifest: NegativeSetManifest) -> None:
    assert manifest.group_counts() == GROUP_COUNTS == {"G1": 9, "G2": 7, "G3": 7, "G4": 6}
    assert sum(GROUP_COUNTS.values()) == NEGATIVE_CASE_COUNT


def test_expected_deny_reasons_come_from_stable_table(manifest: NegativeSetManifest) -> None:
    allowed = set(STABLE_DENY_REASONS)
    for case in manifest.cases:
        assert case.expected_deny_reason in allowed, (
            f"{case.test_id} 的 expected_deny_reason={case.expected_deny_reason} 不在设计 §5.2 稳定码表内"
        )


def test_mode_split_is_frozen(manifest: NegativeSetManifest) -> None:
    """设计 §5.1：LIVE 23 / STATIC 6；G1 STATIC 2、G2 0、G3 4、G4 0。"""
    live = sum(1 for case in manifest.cases if case.mode is InducementMode.LIVE)
    static = sum(1 for case in manifest.cases if case.mode is InducementMode.STATIC)
    assert (live, static) == (23, 6)
    static_per_group = {
        group: sum(
            1
            for case in manifest.cases
            if case.group == group and case.mode is InducementMode.STATIC
        )
        for group in ("G1", "G2", "G3", "G4")
    }
    assert static_per_group == {"G1": 2, "G2": 0, "G3": 4, "G4": 0}


def test_group_prefix_object_kind_and_probe_are_well_formed(
    manifest: NegativeSetManifest,
) -> None:
    for case in manifest.cases:
        assert case.test_id.startswith(case.group.lower() + "_"), case.test_id
        assert isinstance(case.target, ObjectKind)
        assert case.no_change_probe in _PROBE_CATALOG, case.no_change_probe
        assert case.inducement.strip() == case.inducement and case.inducement


def test_cross_tenant_cases_use_w11_normalized_tag(manifest: NegativeSetManifest) -> None:
    """G4 六条必须使用 W11 归一化标签（D-12），且六类对象各 1 条。"""
    cross = [case for case in manifest.cases if case.group == "G4"]
    assert len(cross) == 6
    assert {case.expected_deny_reason for case in cross} == {CROSS_TENANT_DENY_TAG}
    assert {case.target for case in cross} == {
        ObjectKind.TASK,
        ObjectKind.EVIDENCE,
        ObjectKind.APPROVAL,
        ObjectKind.REPORT,
        ObjectKind.ENVIRONMENT,
        ObjectKind.AUDIT,
    }


def test_manifest_rejects_duplicate_test_ids(manifest: NegativeSetManifest) -> None:
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    payload["cases"].append(dict(payload["cases"][0]))
    with pytest.raises(ValidationError) as excinfo:
        NegativeSetManifest.model_validate(payload)
    assert "唯一" in str(excinfo.value)


@pytest.mark.parametrize(
    "override",
    [
        {"group": "G5"},
        {"mode": "HYBRID"},
        {"target": "INDEX"},
        {"expected_deny_reason": ""},
        {"no_change_probe": ""},
        {"unknown_field": 1},
    ],
    ids=["bad_group", "bad_mode", "bad_target", "empty_reason", "empty_probe", "extra_field"],
)
def test_case_model_is_closed(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _case(**override)


# ---------------------------------------------------------------------------
# load_negative_set 的冻结守卫
# ---------------------------------------------------------------------------


def _mutated_payload() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_load_negative_set_detects_missing_case(tmp_path: Path) -> None:
    payload = _mutated_payload()
    payload["cases"].pop()
    path = tmp_path / "negative_cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NegativeSetError) as excinfo:
        load_negative_set(path)
    assert excinfo.value.code == "NEGATIVE_SET_COUNT_MISMATCH"
    assert excinfo.value.details["actual"] == 28


def test_load_negative_set_detects_group_drift(tmp_path: Path) -> None:
    payload = _mutated_payload()
    # 把一条 G1 改成 G2：总数不变，分组条数漂移。
    for case in payload["cases"]:
        if case["group"] == "G1":
            case["group"] = "G2"
            break
    path = tmp_path / "negative_cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NegativeSetError) as excinfo:
        load_negative_set(path)
    assert excinfo.value.code == "NEGATIVE_GROUP_COUNT_MISMATCH"


def test_load_negative_set_detects_version_drift(tmp_path: Path) -> None:
    """版本号先被 ``Literal["negative-set-v1"]`` 拦住（模型层 fail closed）。

    ``assert_manifest_frozen`` 里的版本分支只是二次防御；此处断言**任一**拒绝都
    必须抛 ``NegativeSetError``，且绝不放行到"读入后照常使用"。
    """
    payload = _mutated_payload()
    payload["version"] = "negative-set-v2"
    path = tmp_path / "negative_cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NegativeSetError) as excinfo:
        load_negative_set(path)
    assert excinfo.value.code == "NEGATIVE_SET_INVALID"


def test_load_negative_set_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NegativeSetError) as excinfo:
        load_negative_set(tmp_path / "not_there.json")
    assert excinfo.value.code == "NEGATIVE_SET_MISSING"


def test_assert_manifest_frozen_accepts_frozen_manifest(manifest: NegativeSetManifest) -> None:
    assert_manifest_frozen(manifest)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 四元组与零副作用
# ---------------------------------------------------------------------------


def test_run_negative_case_returns_complete_tuple(manifest: NegativeSetManifest) -> None:
    case = manifest.by_id()["g1_unauthorized_sql_execution"]
    result = run_negative_case(case, ports=_FakePorts())
    assert result.test_id == case.test_id
    assert result.expected_deny_reason == case.expected_deny_reason
    assert result.actual.target_before == result.actual.target_after == 0
    assert result.evidence["mode"] == "LIVE"
    assert result.evidence["deny_source"] == "LIVE_SEAM"
    assert result.evidence["counts"] == {"before": 0, "after": 0}
    assert result.passed is True


def test_run_negative_case_static_mode_marks_static_evidence(
    manifest: NegativeSetManifest,
) -> None:
    case = manifest.by_id()["g3_arbitrary_host_port"]
    result = run_negative_case(
        case,
        ports=_FakePorts(
            deny_reason="POLICY_DENIED",
            error_code=None,
            extra={"source_digest": "sha256:stub", "files": ["core/config.py"]},
        ),
    )
    assert result.evidence["mode"] == "STATIC"
    assert result.evidence["deny_source"] == "STATIC_EVIDENCE"
    assert result.passed is True


def test_passed_requires_denied_and_matching_reason(manifest: NegativeSetManifest) -> None:
    case = manifest.by_id()["g1_unauthorized_sql_execution"]
    not_denied = run_negative_case(case, ports=_FakePorts(denied=False))
    assert not_denied.passed is False

    wrong_reason = run_negative_case(case, ports=_FakePorts(deny_reason="POLICY_DENIED"))
    assert wrong_reason.passed is False


def test_passed_requires_unchanged_target(manifest: NegativeSetManifest) -> None:
    """AC-03：拒绝前后目标行数必须相等（按本用例归属过滤）。"""
    case = manifest.by_id()["g1_unauthorized_sql_execution"]
    changed = run_negative_case(case, ports=_FakePorts(counts=(0, 1)))
    assert changed.actual.target_before == 0
    assert changed.actual.target_after == 1
    assert changed.passed is False


def test_run_negative_case_rejects_unsafe_evidence(manifest: NegativeSetManifest) -> None:
    """AC-01：证据不得携带 payload / request / SQL 文本 / 参数原文。"""
    case = manifest.by_id()["g1_unauthorized_sql_execution"]
    for key in ("payload", "request_body", "sql_text", "arguments", "params", "dsn"):
        with pytest.raises(NegativeSetError) as excinfo:
            run_negative_case(case, ports=_FakePorts(extra={key: "..."}))
        assert excinfo.value.code == "EVIDENCE_UNSAFE", key


def test_run_negative_case_requires_three_port_methods(manifest: NegativeSetManifest) -> None:
    case = manifest.by_id()["g1_unauthorized_sql_execution"]

    class _OnlyObserve:
        def observe(self, case: NegativeCase) -> DenyProbe:
            return DenyProbe(denied=True, deny_reason="R4_NOT_EXECUTABLE")

    with pytest.raises(NegativeSetError) as excinfo:
        run_negative_case(case, ports=_OnlyObserve())
    assert excinfo.value.code == "PORTS_INCOMPLETE"


def test_run_negative_case_rejects_non_probe_return(manifest: NegativeSetManifest) -> None:
    case = manifest.by_id()["g1_unauthorized_sql_execution"]

    class _WrongReturn(_FakePorts):
        def observe(self, case: NegativeCase) -> Any:
            return {"denied": True}

    with pytest.raises(NegativeSetError) as excinfo:
        run_negative_case(case, ports=_WrongReturn())
    assert excinfo.value.code == "PORTS_INVALID"


# ---------------------------------------------------------------------------
# 清单完备性（AC-02：用例数 == 冻结清单数）
# ---------------------------------------------------------------------------


def test_assert_negative_set_complete_accepts_full_set(manifest: NegativeSetManifest) -> None:
    results = [
        _result(case.test_id, expected=case.expected_deny_reason) for case in manifest.cases
    ]
    assert_negative_set_complete(results, manifest)


def test_assert_negative_set_complete_detects_missing_one(manifest: NegativeSetManifest) -> None:
    results = [
        _result(case.test_id, expected=case.expected_deny_reason)
        for case in manifest.cases[:-1]
    ]
    with pytest.raises(NegativeSetError) as excinfo:
        assert_negative_set_complete(results, manifest)
    assert excinfo.value.code == "NEGATIVE_SET_INCOMPLETE"
    assert excinfo.value.details["missing"] == [manifest.cases[-1].test_id]


def test_assert_negative_set_complete_detects_expectation_drift(
    manifest: NegativeSetManifest,
) -> None:
    results = []
    for index, case in enumerate(manifest.cases):
        expected = "POLICY_DENIED" if index == 0 else case.expected_deny_reason
        results.append(_result(case.test_id, expected=expected))
    with pytest.raises(NegativeSetError) as excinfo:
        assert_negative_set_complete(results, manifest)
    assert excinfo.value.code == "NEGATIVE_EXPECTATION_DRIFT"


def test_assert_negative_set_complete_detects_empty_evidence(
    manifest: NegativeSetManifest,
) -> None:
    results = [
        _result(case.test_id, expected=case.expected_deny_reason) for case in manifest.cases
    ]
    broken = NegativeResult(
        test_id=results[0].test_id,
        expected_deny_reason=results[0].expected_deny_reason,
        actual=results[0].actual,
        evidence={},
    )
    results[0] = broken
    with pytest.raises(NegativeSetError) as excinfo:
        assert_negative_set_complete(results, manifest)
    assert excinfo.value.code == "NEGATIVE_TUPLE_INCOMPLETE"


# ---------------------------------------------------------------------------
# 报告口径（R-01）
# ---------------------------------------------------------------------------


def test_summary_contains_fixed_disclaimer_last(manifest: NegativeSetManifest) -> None:
    results = [
        _result(case.test_id, expected=case.expected_deny_reason) for case in manifest.cases
    ]
    summary = format_run_summary(results, manifest)
    assert summary.splitlines()[-1] == SAFETY_DISCLAIMER
    assert "29" in summary
    assert "通过 29 条" in summary
    # 固定句本身含"绝对安全"（"**不**代表数学意义绝对安全"），因此禁令只扫其余行。
    body = "\n".join(summary.splitlines()[:-1])
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in body, claim


def test_disclaimer_text_matches_design_wording() -> None:
    assert SAFETY_DISCLAIMER == "100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全"


def test_negative_module_and_asset_have_no_overclaim() -> None:
    source = _NEGATIVE_SOURCE.read_text(encoding="utf-8")
    assert SAFETY_DISCLAIMER in source
    assert "SAFETY_DISCLAIMER" in source
    asset = _MANIFEST_PATH.read_text(encoding="utf-8")
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in asset, claim


def test_negative_module_does_not_define_production_error_codes() -> None:
    """W11 零生产枚举改动：本模块只定义 W11 标签，不新增 ErrorCode。"""
    source = _NEGATIVE_SOURCE.read_text(encoding="utf-8")
    assert "from core.errors" not in source
    assert "ErrorCode." not in source
