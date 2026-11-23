"""W07 T2：披露快照的构建 / 摘要 / 校验（纯领域，不连数据库）。

这组用例守的是三件事：

1. `disclosure_json` 的键集**封闭**（新增键必须升 `DISCLOSURE_VERSION`）；
2. `disclosure_digest` 用 `canonical_digest` 唯一入口算，且能发现任何字段改动；
3. 披露四列**不可能**被状态迁移改写（L1 白名单）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.approval.disclosure import (
    DISCLOSURE_KEYS,
    DISCLOSURE_TAMPERED,
    DISCLOSURE_VERSION,
    build_disclosure,
    disclosure_digest,
    has_complete_disclosure,
    missing_disclosure_sections,
    verify_disclosure,
)
from core.approval.models import ApprovalStatus
from core.approval.repository import DISCLOSURE_COLUMNS, _MUTABLE_COLUMNS, ApprovalRepository
from core.errors import (
    DisclosureIncompleteError,
    ErrorCode,
    RequestInvalidError,
    StateConflictError,
)
from core.security.canonical import canonical_digest
from core.security.policy import RiskResolution
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from tests.helpers.definitions import write_declarations

_CAPTURED_AT = datetime(2026, 11, 11, 3, 21, 45, 120000, tzinfo=timezone.utc)


def _definition(**overrides: object) -> ToolDefinition:
    payload: dict[str, object] = {
        "name": "mysql_lab_update_order_note",
        "description": "披露快照用例桩",
        "version": "1.0",
        "risk_level": "R2",
        "side_effect": "write",
        "permissions": ["mysql:write"],
        "db_engines": ["mysql"],
        "allowed_environments": ["LAB"],
        "required_role": "dg_rw",
        "required_capabilities": ["optimistic_update"],
        "operation_class": "repair",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "target_objects": [
            TargetObjectSpec(
                object_type="row",
                database_field="database",
                name_field="table",
                keys_field="keys",
            )
        ],
        "prepare_required": True,
        **write_declarations(),
    }
    payload.update(overrides)
    return ToolDefinition(**payload)


def _resolution() -> RiskResolution:
    return RiskResolution(
        effective_risk="R2",
        factors=(
            "definition.risk_level=R2",
            "operation_class=repair",
            "side_effect=write",
        ),
        notes=(),
        model_hint=None,
        model_hint_accepted=False,
        operation_known=True,
        side_effect_known=True,
    )


def _snapshot(**overrides: object) -> dict[str, object]:
    definition = _definition(**overrides)
    return build_disclosure(
        resolution=_resolution(),
        definition=definition,
        captured_at=_CAPTURED_AT,
        policy_version="w06-v1",
    )


def test_snapshot_has_closed_key_set() -> None:
    snapshot = _snapshot()
    assert set(snapshot) == set(DISCLOSURE_KEYS)
    assert snapshot["disclosure_version"] == DISCLOSURE_VERSION == "w07-v1"
    assert snapshot["captured_at"] == "2026-11-11T03:21:45.120Z"
    assert has_complete_disclosure(snapshot) is True
    assert missing_disclosure_sections(snapshot) == []


def test_snapshot_risk_section_comes_from_resolver() -> None:
    risk = _snapshot()["risk"]  # type: ignore[index]
    assert risk["effective_risk"] == "R2"
    assert risk["factors"] == [
        "definition.risk_level=R2",
        "operation_class=repair",
        "side_effect=write",
    ]
    assert risk["model_hint"] is None
    assert risk["model_hint_accepted"] is False
    assert risk["source"] == "risk_resolver"
    assert risk["policy_version"] == "w06-v1"


def test_snapshot_compensation_is_not_rollback() -> None:
    compensation = _snapshot()["compensation"]  # type: ignore[index]
    assert compensation["action"] == "mysql_lab_update_order_note"
    assert compensation["is_rollback"] is False
    assert compensation["requires_own_approval"] is True
    assert compensation["tool_version"] == "1.0"


def test_snapshot_verification_plan_has_three_assertions() -> None:
    plan = _snapshot()["verification_plan"]  # type: ignore[index]
    assert plan["min_assertions"] == 3
    assert len(plan["assertions"]) == 3
    sources = {item["source"] for item in plan["assertions"]}
    assert {"step_observed", "target_readback"} <= sources


def test_snapshot_impact_is_server_side_fact() -> None:
    impact = _snapshot()["impact"]  # type: ignore[index]
    assert impact["reversible"] is True
    assert impact["affects_other_tasks"] is False
    assert impact["reversible_reason"]
    assert impact["affects_other_tasks_reason"]


def test_snapshot_contains_no_float_and_no_datetime() -> None:
    """`canonical_digest` 只接受 JSON 值域：出现 float/datetime 会直接抛错。"""
    snapshot = _snapshot()
    digest = disclosure_digest(snapshot)
    assert len(digest) == 64
    assert digest == canonical_digest(snapshot)
    # 稳定性：同一输入两次构建必须同摘要
    assert disclosure_digest(_snapshot()) == digest


def test_build_disclosure_without_compensation_raises_incomplete() -> None:
    """运行期第二道防线：写工具即使绕过构造期校验，也不得写出"半份披露"。"""
    from types import SimpleNamespace

    broken_write = SimpleNamespace(
        name="mysql_update_rows",
        version="1.0",
        side_effect="write",
        compensation=None,
        verification_plan=[],
        target_objects=[],
    )
    with pytest.raises(DisclosureIncompleteError) as excinfo:
        build_disclosure(
            resolution=_resolution(),
            definition=broken_write,  # type: ignore[arg-type]
            captured_at=_CAPTURED_AT,
        )
    assert excinfo.value.code is ErrorCode.DISCLOSURE_INCOMPLETE
    assert excinfo.value.status_code == 409
    assert "disclosure.compensation" in excinfo.value.details["missing_fields"]


def test_read_tool_disclosure_declares_sections_not_applicable() -> None:
    """只读审批仍然有三节，但如实声明 `applicable=false`（不伪造补偿内容）。"""
    definition = ToolDefinition(
        name="get_db_version",
        description="只读工具",
        side_effect="read",
        permissions=["mysql:read"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    snapshot = build_disclosure(
        resolution=_resolution(),
        definition=definition,
        captured_at=_CAPTURED_AT,
    )
    assert set(snapshot) == set(DISCLOSURE_KEYS)
    assert snapshot["compensation"]["applicable"] is False
    assert snapshot["compensation"]["requires_own_approval"] is False
    assert snapshot["verification_plan"]["applicable"] is False
    assert snapshot["verification_plan"]["assertions"] == []
    # 三节仍然"非空"，因此 missing_required_fields 不会报缺——只读审批可正常批准
    assert has_complete_disclosure(snapshot) is True


def test_verify_disclosure_accepts_matching_digest() -> None:
    snapshot = _snapshot()
    verify_disclosure(snapshot, disclosure_digest(snapshot))


@pytest.mark.parametrize(
    ("mutate", "cause"),
    [
        (lambda value: value["risk"].__setitem__("effective_risk", "R1"), "DISCLOSURE_DIGEST_MISMATCH"),
        (
            lambda value: value["compensation"].__setitem__("is_rollback", True),
            "DISCLOSURE_DIGEST_MISMATCH",
        ),
        (
            lambda value: value["verification_plan"]["assertions"].pop(),
            "DISCLOSURE_DIGEST_MISMATCH",
        ),
        (lambda value: value.__setitem__("extra", {"note": "人工补充"}), "DISCLOSURE_KEYS_MISMATCH"),
    ],
)
def test_verify_disclosure_detects_tamper(mutate, cause: str) -> None:
    snapshot = _snapshot()
    expected = disclosure_digest(snapshot)
    mutate(snapshot)
    with pytest.raises(StateConflictError) as excinfo:
        verify_disclosure(snapshot, expected)
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT
    assert excinfo.value.details["reason"] == DISCLOSURE_TAMPERED
    assert excinfo.value.details["cause"] == cause


def test_verify_disclosure_rejects_missing_digest() -> None:
    with pytest.raises(StateConflictError) as excinfo:
        verify_disclosure(_snapshot(), None)
    assert excinfo.value.details["reason"] == DISCLOSURE_TAMPERED
    assert excinfo.value.details["cause"] == "DISCLOSURE_DIGEST_MISSING"


def test_disclosure_columns_are_not_mutable() -> None:
    """L1：披露四列不得出现在状态更新的列白名单里。"""
    assert set(DISCLOSURE_COLUMNS) == {
        "disclosure_json",
        "disclosure_digest",
        "disclosure_version",
        "disclosed_at",
    }
    assert not set(DISCLOSURE_COLUMNS) & set(_MUTABLE_COLUMNS)


@pytest.mark.parametrize("column", list(DISCLOSURE_COLUMNS))
def test_transition_rejects_disclosure_column(column: str) -> None:
    """`transition(fields={"disclosure_json": ...})` 必须在触达数据库之前就被拒绝。"""
    repository = ApprovalRepository(object())  # type: ignore[arg-type]
    with pytest.raises(RequestInvalidError) as excinfo:
        repository.transition(
            "apr_test",
            expected=[ApprovalStatus.PENDING],
            target=ApprovalStatus.APPROVED,
            fields={column: "tampered"},
        )
    assert excinfo.value.details["column"] == column


def test_insert_requires_disclosure_digest() -> None:
    from datetime import timedelta

    from core.approval.models import Approval

    repository = ApprovalRepository(object())  # type: ignore[arg-type]
    now = datetime.now(timezone.utc)
    approval = Approval(
        approval_id="apr_no_disclosure",
        tenant_id="tenant_local",
        task_id="task_test",
        environment_id="env_mysql_local",
        db_engine="mysql",
        environment_fingerprint={"db_engine": "mysql"},
        environment_fingerprint_digest="f" * 64,
        action_digest="a" * 64,
        parameters_digest="b" * 64,
        target_objects_digest="c" * 64,
        prerequisites_digest="d" * 64,
        action_descriptor={"normalized_operation": {"side_effect": "write"}},
        policy_version="w06-v1",
        tool_name="mysql_lab_update_order_note",
        tool_version="1.0",
        requested_by="user_operator",
        nonce_hash="e" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with pytest.raises(RequestInvalidError) as excinfo:
        repository.insert(approval)
    assert excinfo.value.details["reason"] == "DISCLOSURE_DIGEST_REQUIRED"


def test_locked_row_disclosure_tamper_is_detected() -> None:
    """L3：`consume()` 在行锁内用锁内 JSON 重算摘要，不一致即阻断。

    这一层覆盖"读取之后、加锁之前"被改写的窗口（L2 只覆盖读取时刻）。
    """
    from core.approval.service import ApprovalService

    snapshot = _snapshot()
    digest = disclosure_digest(snapshot)
    locked_row = {
        "approval_id": "apr_locked",
        "disclosure_json": json.dumps(snapshot, ensure_ascii=False),
        "disclosure_digest": digest,
    }
    # 一致时不得抛错
    ApprovalService._verify_locked_disclosure("apr_locked", locked_row)

    tampered = json.loads(locked_row["disclosure_json"])
    tampered["impact"]["reversible"] = False
    locked_row["disclosure_json"] = json.dumps(tampered, ensure_ascii=False)
    with pytest.raises(StateConflictError) as excinfo:
        ApprovalService._verify_locked_disclosure("apr_locked", locked_row)
    assert excinfo.value.details["reason"] == DISCLOSURE_TAMPERED
    assert excinfo.value.details["approval_id"] == "apr_locked"


def test_locked_row_with_corrupt_json_is_detected() -> None:
    from core.approval.service import ApprovalService

    with pytest.raises(StateConflictError) as excinfo:
        ApprovalService._verify_locked_disclosure(
            "apr_broken", {"disclosure_json": "{not-json", "disclosure_digest": "a" * 64}
        )
    assert excinfo.value.details["cause"] == "DISCLOSURE_NOT_JSON"


def _detail_without_disclosure():
    from datetime import timedelta

    from core.approval.models import ApprovalDetail

    now = datetime.now(timezone.utc)
    return ApprovalDetail(
        approval_id="apr_legacy",
        tenant_id="tenant_local",
        task_id="task_test",
        environment_id="env_mysql_local",
        db_engine="mysql",
        environment_fingerprint={"db_engine": "mysql"},
        environment_fingerprint_digest="f" * 64,
        action_digest="a" * 64,
        parameters_digest="b" * 64,
        target_objects_digest="c" * 64,
        prerequisites_digest="d" * 64,
        action_descriptor={
            "normalized_operation": {"side_effect": "write"},
            "target_objects": [{"object_type": "row", "name": "seed_orders"}],
        },
        policy_version="w06-v1",
        tool_name="mysql_lab_update_order_note",
        tool_version="1.0",
        requested_by="user_operator",
        nonce_hash="e" * 64,
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def test_api_disclosure_marks_legacy_row_without_snapshot() -> None:
    """历史行必须显式标注"未提供 + 原因"，不得折叠隐藏也不得凭空展示。"""
    from apps.api.routes.approvals import _disclosure

    block = _disclosure(_detail_without_disclosure())
    for section in ("风险等级", "补偿方案", "验证计划"):
        assert block[section]["provided"] is False, section
        assert block[section]["reason"] == "LEGACY_ROW_WITHOUT_DISCLOSURE", section
    assert block["影响面"]["reversible"] is None
    assert block["影响面"]["missing"] == ["reversible", "affects_other_tasks"]
    assert block["绑定信息"]["disclosure_digest"] is None


def test_api_disclosure_reads_snapshot_and_digest() -> None:
    """有快照时三节 provided=true，并回显 digest / version / disclosed_at。"""
    from apps.api.routes.approvals import _disclosure

    snapshot = _snapshot()
    detail = _detail_without_disclosure().model_copy(
        update={
            "disclosure": snapshot,
            "disclosure_digest": disclosure_digest(snapshot),
            "disclosure_version": DISCLOSURE_VERSION,
            "disclosed_at": _CAPTURED_AT,
        }
    )
    block = _disclosure(detail)
    assert block["风险等级"]["provided"] is True
    assert block["风险等级"]["effective_risk"] == "R2"
    assert block["风险等级"]["captured_at"] == "2026-11-11T03:21:45.120Z"
    assert "reason" not in block["风险等级"]
    assert block["补偿方案"]["provided"] is True
    assert block["补偿方案"]["is_rollback"] is False
    assert block["验证计划"]["assertion_count"] == 3
    assert block["验证计划"]["min_assertions"] == 3
    assert block["影响面"]["reversible"] is True
    assert block["影响面"]["missing"] == []
    assert block["绑定信息"]["disclosure_version"] == "w07-v1"
    assert block["绑定信息"]["disclosed_at"]
