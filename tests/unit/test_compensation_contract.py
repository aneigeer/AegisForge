"""W08 T04 / AC-10、AC-11：补偿是独立审批的 R2 动作（契约级，不依赖数据库）。

断言口径：
1. 补偿参数 = 原参数 `note` / `expected_note` **互换** → `action_digest` 必不同；
2. 同 `before` 两次构造 → 摘要相同；换 `before` → 摘要变化（确定性）；
3. 复用原 `approval_id` / 摘要相同 → 独立性断言拒绝（`details.reason` 区分）；
4. `to_public_dict().is_rollback is False`，面向人字段禁含 `rollback`；
5. `compensation_of` **不进** `action_digest`（`DESCRIPTOR_KEYS` / 版本号不动）；
6. `CompensationService.request()` 真的用新 `approval_id` 建审批、带 `compensation_of`、
   且留 `COMPENSATION_REQUESTED` 审计。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.mysql.lab_repair import TOOL_NAME, LabOrderNoteUpdateTool
from core.approval.digest import (
    ACTION_DIGEST_VERSION,
    DESCRIPTOR_KEYS,
    build_action_descriptor,
    build_normalized_operation,
    compute_action_digest,
    extract_target_objects,
    normalize_tool_parameters,
)
from core.approval.models import Approval, ApprovalStatus
from core.errors import StateConflictError
from core.security.models import AuthMethod, Principal
from core.security.roles import Role
from core.tools.contracts import CompensationSpec
from core.verification.compensation import (
    COMPENSATION_DIGEST_NOT_INDEPENDENT,
    COMPENSATION_ORIGINAL_NOT_CONSUMED,
    COMPENSATION_REUSES_ORIGINAL_APPROVAL,
    CompensationService,
)

DEFINITION = LabOrderNoteUpdateTool().definition
FINGERPRINT = {
    "db_engine": "mysql",
    "db_version": "8.0.36",
    "image_digest": None,
    "network_id": "net_lab",
    "config_version": "cfg_v1",
}
ORDER_ID = 2001
TENANT_ID = "tenant_local"
BEFORE = "w08-note-before"
AFTER = "w08-note-after"


# ---------------------------------------------------------------------------
# 摘要构造（与 ApprovalService.create() 同源的两个函数）
# ---------------------------------------------------------------------------


def _parameters(note: str, expected_note: str) -> dict[str, Any]:
    return normalize_tool_parameters(
        DEFINITION,
        {
            "database": "dg_lab",
            "table": "seed_orders",
            "order_id": ORDER_ID,
            "keys": f"order_id={ORDER_ID}",
            "note": note,
            "expected_note": expected_note,
        },
    )


def _digests(note: str, expected_note: str) -> tuple[str, str, str]:
    params = _parameters(note, expected_note)
    targets = extract_target_objects(DEFINITION, params)
    descriptor = build_action_descriptor(
        tenant_id=TENANT_ID,
        environment_id="env_mysql_local",
        db_engine="mysql",
        fingerprint=FINGERPRINT,
        definition=DEFINITION,
        normalized_operation=build_normalized_operation(DEFINITION, targets),
        target_objects=targets,
        parameters=params,
        policy_version="w06-v1",
    )
    return compute_action_digest(descriptor)


def _approval(
    *, approval_id: str, action_digest: str, status: ApprovalStatus = ApprovalStatus.CONSUMED
) -> Approval:
    """只填本卡用到的字段（构造期跳过校验的模式专供契约测试）。"""
    return Approval.model_construct(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        task_id="task_unit",
        step_id=None,
        tool_name=TOOL_NAME,
        tool_version="1.0",
        policy_version="w06-v1",
        action_digest=action_digest,
        parameters_digest="d" * 64,
        status=status,
        action_descriptor={"parameters": _parameters(AFTER, BEFORE)},
    )


# ---------------------------------------------------------------------------
# 1) 参数互换 → 摘要不同
# ---------------------------------------------------------------------------


def test_compensation_digest_differs_from_original() -> None:
    original = _digests(AFTER, BEFORE)
    compensation = _digests(BEFORE, AFTER)

    assert original[0] != compensation[0], "补偿与原动作的 action_digest 必须不同"
    assert original[1] != compensation[1], "parameters_digest 也必须不同"
    # 目标对象未变：只有参数互换，目标通道摘要应当一致。
    assert original[2] == compensation[2]


# ---------------------------------------------------------------------------
# 2) 确定性
# ---------------------------------------------------------------------------


def test_same_before_twice_yields_same_digest_and_change_moves_it() -> None:
    first = _digests(BEFORE, AFTER)
    second = _digests(BEFORE, AFTER)
    different = _digests("w08-another-before", AFTER)

    assert first[0] == second[0], "同 before 两次构造必须得到相同 action_digest"
    assert different[0] != first[0], "换 before 必须得到不同 action_digest"


# ---------------------------------------------------------------------------
# 3) 独立性断言
# ---------------------------------------------------------------------------


def test_reusing_original_approval_is_rejected() -> None:
    service = CompensationService(
        approvals=SimpleNamespace(), audit=SimpleNamespace(), tasks=SimpleNamespace()
    )
    original = _approval(approval_id="appr_unit_original", action_digest="a" * 64)

    with pytest.raises(StateConflictError) as excinfo:
        service.assert_independent(original, original)

    assert excinfo.value.details["reason"] == COMPENSATION_REUSES_ORIGINAL_APPROVAL


def test_identical_action_digest_is_rejected() -> None:
    service = CompensationService(
        approvals=SimpleNamespace(), audit=SimpleNamespace(), tasks=SimpleNamespace()
    )
    digest = "b" * 64
    original = _approval(approval_id="appr_unit_original", action_digest=digest)
    compensation = _approval(approval_id="appr_unit_comp", action_digest=digest)

    with pytest.raises(StateConflictError) as excinfo:
        service.assert_independent(compensation, original)

    assert excinfo.value.details["reason"] == COMPENSATION_DIGEST_NOT_INDEPENDENT


def test_independent_pair_passes() -> None:
    service = CompensationService(
        approvals=SimpleNamespace(), audit=SimpleNamespace(), tasks=SimpleNamespace()
    )
    original = _approval(approval_id="appr_unit_original", action_digest="a" * 64)
    compensation = _approval(approval_id="appr_unit_comp", action_digest="c" * 64)

    assert service.assert_independent(compensation, original) is None


# ---------------------------------------------------------------------------
# 4) 面向人字段：不叫回滚
# ---------------------------------------------------------------------------


def test_public_view_is_not_rollback_and_has_no_forbidden_word() -> None:
    spec = DEFINITION.compensation
    assert isinstance(spec, CompensationSpec)

    public = spec.to_public_dict()
    assert public["is_rollback"] is False
    for key, value in public.items():
        if isinstance(value, str):
            assert "rollback" not in value.lower(), f"{key} 面向人文案不得含 rollback"

    # 构造期拦截：直接写 rollback 的文案连模型都建不出来。
    with pytest.raises(ValidationError):
        CompensationSpec(action=TOOL_NAME, description="rollback to previous note")


# ---------------------------------------------------------------------------
# 5) compensation_of 不进 action_digest
# ---------------------------------------------------------------------------


def test_compensation_of_is_not_part_of_action_digest() -> None:
    assert "compensation_of" not in DESCRIPTOR_KEYS
    assert ACTION_DIGEST_VERSION == "w06-v1"

    params = _parameters(BEFORE, AFTER)
    targets = extract_target_objects(DEFINITION, params)
    descriptor = build_action_descriptor(
        tenant_id=TENANT_ID,
        environment_id="env_mysql_local",
        db_engine="mysql",
        fingerprint=FINGERPRINT,
        definition=DEFINITION,
        normalized_operation=build_normalized_operation(DEFINITION, targets),
        target_objects=targets,
        parameters=params,
        policy_version="w06-v1",
    )
    assert set(descriptor) == set(DESCRIPTOR_KEYS)
    # 同一份参数两次算出的摘要相同 —— 加入 compensation_of 也无处可进。
    assert compute_action_digest(descriptor)[0] == compute_action_digest(descriptor)[0]


# ---------------------------------------------------------------------------
# 6) request() 的装配契约（假 ApprovalService，不起数据库）
# ---------------------------------------------------------------------------


class _FakeApprovals:
    def __init__(self, *, compensation: Approval, compensation_of: str | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.repository = _FakeRepository(
            compensation=compensation, compensation_of=compensation_of
        )
        self._compensation = compensation

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(approval_id=self._compensation.approval_id)


class _FakeRepository:
    def __init__(self, *, compensation: Approval, compensation_of: str | None) -> None:
        self._compensation = compensation
        self._compensation_of = compensation_of

    def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        _ = (approval_id, tenant_id)
        return self._compensation

    def get_compensation_of(self, approval_id: str) -> str | None:
        _ = approval_id
        return self._compensation_of


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_required(self, event: Any, *, actor_user_id: str | None = None) -> Any:
        _ = actor_user_id
        self.events.append(event)
        return event


class _FakeTasks:
    def get_task(self, task_id: str) -> Any:  # noqa: ARG002
        return SimpleNamespace(run_id="run_unit")


def _principal() -> Principal:
    return Principal(
        user_id="user_unit_operator",
        tenant_id=TENANT_ID,
        roles=(Role.OPERATOR,),
        auth_method=AuthMethod.DEV_LOCAL,
    )


def _service(
    *, original: Approval, compensation: Approval, compensation_of: str | None
) -> tuple[CompensationService, _FakeApprovals, _FakeAudit]:
    approvals = _FakeApprovals(compensation=compensation, compensation_of=compensation_of)
    audit = _FakeAudit()
    service = CompensationService(
        approvals=approvals, audit=audit, tasks=_FakeTasks()  # type: ignore[arg-type]
    )
    _ = original
    return service, approvals, audit


def test_request_creates_new_approval_with_compensation_of_and_swapped_params() -> None:
    original = _approval(approval_id="appr_unit_original", action_digest="a" * 64)
    compensation = _approval(approval_id="appr_unit_comp", action_digest="c" * 64)
    service, approvals, audit = _service(
        original=original,
        compensation=compensation,
        compensation_of=original.approval_id,
    )

    returned = service.request(
        original_approval=original, before=BEFORE, after=AFTER, principal=_principal()
    )

    assert returned is compensation
    assert len(approvals.calls) == 1
    call = approvals.calls[0]
    assert call["compensation_of"] == original.approval_id
    assert call["tool_name"] == TOOL_NAME
    assert call["task_id"] == original.task_id

    params = call["params"]
    assert params["note"] == BEFORE, "补偿必须把 note 写回 before"
    assert params["expected_note"] == AFTER, "补偿必须把 expected_note 设为 after"
    # 其余参数逐字沿用（否则就不是"同一个目标行、只改一列"）。
    assert params["keys"] == f"order_id={ORDER_ID}"
    assert params["order_id"] == ORDER_ID
    assert params["database"] == "dg_lab"
    assert params["table"] == "seed_orders"

    requested = [event for event in audit.events if event.event_type == "COMPENSATION_REQUESTED"]
    assert len(requested) == 1
    payload = requested[0].payload
    assert payload["original_approval_id"] == original.approval_id
    assert payload["compensation_approval_id"] == compensation.approval_id
    assert payload["requested_by"] == "user_unit_operator"
    assert payload["parameters_digest"] and payload["action_digest"]


def test_request_rejects_unconsumed_original_and_audits_denial() -> None:
    original = _approval(
        approval_id="appr_unit_pending",
        action_digest="a" * 64,
        status=ApprovalStatus.APPROVED,
    )
    compensation = _approval(approval_id="appr_unit_comp", action_digest="c" * 64)
    service, approvals, audit = _service(
        original=original, compensation=compensation, compensation_of=None
    )

    with pytest.raises(StateConflictError) as excinfo:
        service.request(
            original_approval=original, before=BEFORE, after=AFTER, principal=_principal()
        )

    assert excinfo.value.details["reason"] == COMPENSATION_ORIGINAL_NOT_CONSUMED
    assert approvals.calls == [], "被拒不得到留下待批单"

    denied = [event for event in audit.events if event.event_type == "COMPENSATION_DENIED"]
    assert len(denied) == 1
    assert denied[0].payload["reason"] == COMPENSATION_ORIGINAL_NOT_CONSUMED
    assert denied[0].payload["deny_code"]
    assert denied[0].payload["original_approval_id"] == original.approval_id
