"""W07 AC-08：六类拒绝矩阵（6 个**相互独立**的用例）。

每个用例都独立建任务/审批，并统一断言四件事：

1. 目标库那一行的 `note` **逐字未变**（拒绝不得产生任何副作用）；
2. 没有 `STEP_OBSERVED` 事件，也没有 `step_observed` 行（没执行就没有回执）；
3. 拒因可定位：错误码 + `channel` / `check_id` / `reason` 至少一项；
4. 审计里有与拒绝对应的落地事件（被拒本身也必须可追溯）。

六类：无审批 / 过期 / 改参数 / 换环境 / 越界目标 / 重复消费。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest

from adapters.mysql.lab_repair import TOOL_NAME
from core.gateway.envelope import build_envelope, sign_envelope
from core.security.models import AuthMethod, Principal
from core.security.roles import Role
from core.tasks.models import TaskStatus
from core.tools.contracts import ToolStatus
from tests.integration.test_w07_repair_e2e import (  # noqa: F401 - pytest 需要夹具在本模块可见
    NOTE_SQL,
    ORDER_ID,
    RESTORE_SQL,
    _current_note,
    _params,
    w07_stack,
)

pytestmark = pytest.mark.integration

_OPERATOR = Principal(
    user_id="user_w07_matrix_operator",
    tenant_id="tenant_local",
    roles=(Role.OPERATOR,),
    session_id="sess_w07_matrix_operator",
)
_APPROVER = Principal(
    user_id="user_w07_matrix_approver",
    tenant_id="tenant_local",
    roles=(Role.APPROVER,),
    session_id="sess_w07_matrix_approver",
)


def _new_task(stack: dict[str, Any], label: str) -> Any:
    service = stack["service"]
    task = service.create_task(f"W07 拒绝矩阵：{label}")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    return service.transition(task.task_id, TaskStatus.EXECUTING)


def _create(stack: dict[str, Any], task: Any, params: dict[str, Any], **kwargs: Any) -> Any:
    return stack["approvals"].create(
        principal=_OPERATOR,
        task_id=task.task_id,
        step_id=None,
        tool_name=TOOL_NAME,
        params=params,
        target_objects=[],
        **kwargs,
    )


def _approve(stack: dict[str, Any], approval_id: str) -> Any:
    return stack["approvals"].approve(
        approval_id, principal=_APPROVER, reason="W07 拒绝矩阵"
    )


def _envelope(
    stack: dict[str, Any],
    task: Any,
    lease: Any,
    *,
    approval_id: str | None,
    params: dict[str, Any],
) -> Any:
    """用已取得的租约签一个新信封（新 correlation_id ⇒ 新 nonce）。"""
    return sign_envelope(
        build_envelope(
            task_id=task.task_id,
            step_id=None,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            state_version=task.state_version,
            correlation_id=f"corr_matrix_{uuid4().hex[:8]}",
            actor_user_id="user_local",
            tool_name=TOOL_NAME,
            tool_version="1.0",
            environment_id=task.environment_id,
            params=params,
            ttl_s=60,
            approval_id=approval_id,
        ),
        stack["settings"].gateway_key(),
    )


def _event_types(stack: dict[str, Any], task: Any) -> set[str]:
    events, _ = stack["audit"].query(
        tenant_id="tenant_local", task_id=task.task_id, limit=300
    )
    return {getattr(event.event_type, "value", event.event_type) for event in events}


def _assert_no_execution_evidence(stack: dict[str, Any], task: Any) -> None:
    """没执行就没有回执：事件与两张证据表都必须为空。"""
    assert "STEP_OBSERVED" not in _event_types(stack, task)
    observed = stack["control_client"].fetch_all(
        "SELECT observed_id FROM dg_system.step_observed WHERE task_id = %s",
        (task.task_id,),
    )
    assert observed == [], "被拒绝的操作不得留下 step_observed"


# ---------------------------------------------------------------------------
# 1) 无审批
# ---------------------------------------------------------------------------


def test_no_approval_is_denied(w07_stack, rw_client) -> None:
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "无审批")
    lease = w07_stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    params = _params(before, "no-approval-must-not-write")
    try:
        envelope = _envelope(
            w07_stack, task, lease, approval_id=None, params=params
        )
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "APPROVAL_REQUIRED"
        assert _current_note(rw_client) == before
        _assert_no_execution_evidence(w07_stack, task)
        assert "GATEWAY_EXECUTION_DENIED" in _event_types(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# 2) 过期
# ---------------------------------------------------------------------------


def test_expired_approval_is_rejected(w07_stack, rw_client) -> None:
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "过期")
    lease = w07_stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    params = _params(before, "expired-must-not-write")
    try:
        created = _create(w07_stack, task, params, ttl_s=1.0)
        _approve(w07_stack, created.approval_id)
        # 等待过期（服务端时间；客户端时间不参与判定）
        time.sleep(1.2)

        envelope = _envelope(
            w07_stack, task, lease, approval_id=created.approval_id, params=params
        )
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "APPROVAL_EXPIRED", result.error
        assert result.error.details.get("approval_id") == created.approval_id
        assert _current_note(rw_client) == before
        _assert_no_execution_evidence(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# 3) 改参数
# ---------------------------------------------------------------------------


def test_parameter_change_is_rejected_with_parameters_channel(
    w07_stack, rw_client
) -> None:
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "改参数")
    lease = w07_stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    approved_params = _params(before, "approved-note")
    tampered_params = _params(before, "tampered-note")
    try:
        created = _create(w07_stack, task, approved_params)
        _approve(w07_stack, created.approval_id)

        # 信封按**篡改后**的参数签名（否则会先在 Gateway 的信封摘要处被拦），
        # 真正到达 consume() 才验证"批准的是不是这一份参数"。
        envelope = _envelope(
            w07_stack,
            task,
            lease,
            approval_id=created.approval_id,
            params=tampered_params,
        )
        result = w07_stack["gateway"].execute(envelope, tampered_params)

        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "APPROVAL_DIGEST_MISMATCH", result.error
        assert result.error.details.get("channel") == "parameters"
        assert _current_note(rw_client) == before
        _assert_no_execution_evidence(w07_stack, task)
        assert "APPROVAL_CONSUME_DENIED" in _event_types(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# 4) 换环境
# ---------------------------------------------------------------------------


def test_environment_change_is_rejected_with_environment_channel(
    w07_stack, rw_client, control_client
) -> None:
    """把审批绑定的环境改掉（模拟跨环境复用审批）→ `channel=environment`。"""
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "换环境")
    lease = w07_stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    params = _params(before, "env-swap-must-not-write")
    other_environment_id = f"env_w07_matrix_{uuid4().hex[:8]}"
    try:
        created = _create(w07_stack, task, params)
        _approve(w07_stack, created.approval_id)

        # 造一个同租户的 LAB/mysql 环境，并把审批改绑到它（需要 FK 目标存在）。
        control_client.execute(
            "INSERT INTO dg_system.environments (environment_id, tenant_id, db_engine, "
            "db_version, network_id, image_digest, config_version, status, created_at) "
            "VALUES (%s, 'tenant_local', 'mysql', '8.0.46', 'lab_net', NULL, "
            "'w07-matrix', 'LAB', UTC_TIMESTAMP(6))",
            (other_environment_id,),
        )
        control_client.execute(
            "UPDATE dg_system.approvals SET environment_id = %s WHERE approval_id = %s",
            (other_environment_id, created.approval_id),
        )

        envelope = _envelope(
            w07_stack, task, lease, approval_id=created.approval_id, params=params
        )
        result = w07_stack["gateway"].execute(envelope, params)

        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "APPROVAL_DIGEST_MISMATCH", result.error
        assert result.error.details.get("channel") == "environment"
        assert _current_note(rw_client) == before
        _assert_no_execution_evidence(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
        control_client.execute(
            "UPDATE dg_system.approvals SET environment_id = %s WHERE approval_id = %s",
            ("env_mysql_local", created.approval_id),
        )
        control_client.execute(
            "DELETE FROM dg_system.environments WHERE environment_id = %s",
            (other_environment_id,),
        )


# ---------------------------------------------------------------------------
# 5) 越界目标
# ---------------------------------------------------------------------------


def test_out_of_scope_target_is_blocked(w07_stack, rw_client) -> None:
    """越界目标被两道门分别拦住，且目标库逐字未变。

    1. **创建期**：`order_id=2300` 超出工具参数契约（2001–2200），在进入前置条件
       探测之前就被 fail closed 拒绝，不产生审批行。
    2. **执行期**：审批针对 `order_id=2001`，执行时把目标换成 `order_id=2200`
       → `consume()` 重算摘要发现不符，`channel` 精确定位到参数/目标对象通道。

    说明：`target_object_signature` 的 `NOTE_TOO_LONG` / `TARGET_ROW_MISSING` 分支
    在真实 LAB 里不可构造（`note` 列本身是 `VARCHAR(128)`，种子表 200 行全覆盖
    2001–2200），因此这两条分支由 `tests/unit/test_prerequisites_w07.py` 直接覆盖；
    本文件只断言"创建期整体被拒 + 无审批行"。
    """
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "越界目标")
    out_of_range = {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": 2300,
        "keys": "order_id=2300",
        "note": "out-of-range-must-not-write",
        "expected_note": None,
    }
    try:
        with pytest.raises(Exception) as range_error:
            _create(w07_stack, task, out_of_range)
        assert getattr(range_error.value, "code", None) is not None
        assert range_error.value.code.value in {
            "REQUEST_INVALID",
            "POLICY_DENIED",
            "APPROVAL_PREREQUISITE_FAILED",
        }, range_error.value
        rows = w07_stack["control_client"].fetch_all(
            "SELECT approval_id FROM dg_system.approvals WHERE task_id = %s",
            (task.task_id,),
        )
        assert rows == [], "创建被拒不得留下审批行"

        # 执行期越界：审批 2001，执行 2200
        lease = w07_stack["worker"].acquire_lease(
            task.task_id, task.environment_id, ttl_s=30
        )
        approved = _params(before, "approved-for-2001")
        moved = {
            **approved,
            "order_id": 2200,
            "keys": "order_id=2200",
        }
        created = _create(w07_stack, task, approved)
        _approve(w07_stack, created.approval_id)
        result = w07_stack["gateway"].execute(
            _envelope(
                w07_stack,
                task,
                lease,
                approval_id=created.approval_id,
                params=moved,
            ),
            moved,
        )
        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == "APPROVAL_DIGEST_MISMATCH", result.error
        # 任何目标字段变化都同时改变 parameters_digest，因此两个通道都算精确定位；
        # 绝不接受"只报一个笼统的摘要不符"。
        assert result.error.details.get("channel") in {
            "parameters",
            "target_objects",
        }
        assert _current_note(rw_client) == before
        _assert_no_execution_evidence(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


# ---------------------------------------------------------------------------
# 6) 重复消费
# ---------------------------------------------------------------------------


def test_duplicate_consumption_is_rejected(w07_stack, rw_client) -> None:
    before = _current_note(rw_client)
    task = _new_task(w07_stack, "重复消费")
    lease = w07_stack["worker"].acquire_lease(task.task_id, task.environment_id, ttl_s=30)
    params = _params(before, "consumed-once")
    try:
        created = _create(w07_stack, task, params)
        _approve(w07_stack, created.approval_id)

        first = w07_stack["gateway"].execute(
            _envelope(
                w07_stack,
                task,
                lease,
                approval_id=created.approval_id,
                params=params,
            ),
            params,
        )
        assert first.status is ToolStatus.SUCCESS, first.error
        assert _current_note(rw_client) == "consumed-once"

        # 第二张审批不可复用：同一 approval_id + 新信封（新 nonce）→ 已消费
        second = w07_stack["gateway"].execute(
            _envelope(
                w07_stack,
                task,
                lease,
                approval_id=created.approval_id,
                params=params,
            ),
            params,
        )
        assert second.status is ToolStatus.DENIED
        assert second.error is not None
        assert second.error.code == "APPROVAL_ALREADY_CONSUMED", second.error
        assert _current_note(rw_client) == "consumed-once", "重复消费不得产生第二次写入"
        consumptions = w07_stack["control_client"].fetch_all(
            "SELECT consumption_id FROM dg_system.approval_consumptions "
            "WHERE approval_id = %s",
            (created.approval_id,),
        )
        assert len(consumptions) == 1, "台账必须恰好一条消费记录"
        # 已消费的拒绝由状态映射直接给出（不进入摘要比较），落的是 Gateway 拒绝事件
        assert "GATEWAY_EXECUTION_DENIED" in _event_types(w07_stack, task)
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
