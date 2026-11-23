"""W07 T2：披露快照的**落库闭环**（真实 MySQL，不需要管理员账号）。

三个不可退让的断言：

1. 快照在 `create()` 时刻就已落库，`GET` 返回的就是落库那份；
2. 库内 JSON 被改（`dg_ctl` 有 UPDATE 权限，必须假设会被改）→ 读取即拒绝；
3. 被改过的审批**不能**被消费：不写 `approval_consumptions`、状态不变。
"""

from __future__ import annotations

from typing import Any

import pytest

from adapters.mysql.lab_repair import TOOL_NAME
from core.approval.disclosure import DISCLOSURE_TAMPERED
from core.errors import ErrorCode, StateConflictError
from core.security.models import Principal
from core.security.roles import Role
from core.tasks.models import TaskStatus
from core.tools.contracts import ToolStatus
from tests.integration.test_w07_rejection_matrix import _envelope  # noqa: F401
from tests.integration.test_w07_repair_e2e import (  # noqa: F401 - 夹具需在本模块可见
    ORDER_ID,
    RESTORE_SQL,
    _current_note,
    _params,
    w07_stack,
)

pytestmark = pytest.mark.integration

_OPERATOR = Principal(
    user_id="user_w07_disclosure_operator",
    tenant_id="tenant_local",
    roles=(Role.OPERATOR,),
    session_id="sess_w07_disclosure_operator",
)
_APPROVER = Principal(
    user_id="user_w07_disclosure_approver",
    tenant_id="tenant_local",
    roles=(Role.APPROVER,),
    session_id="sess_w07_disclosure_approver",
)


def _created(stack: dict[str, Any], params: dict[str, Any]) -> tuple[Any, Any]:
    service = stack["service"]
    task = service.create_task("W07 披露快照落库")
    task = service.transition(task.task_id, TaskStatus.PLANNING)
    task = service.transition(task.task_id, TaskStatus.EXECUTING)
    created = stack["approvals"].create(
        principal=_OPERATOR,
        task_id=task.task_id,
        step_id=None,
        tool_name=TOOL_NAME,
        params=params,
        target_objects=[],
    )
    return task, created


def test_snapshot_is_persisted_at_create(w07_stack, rw_client) -> None:
    before = _current_note(rw_client)
    params = _params(before, "snapshot-persisted")
    try:
        _task, created = _created(w07_stack, params)
        row = w07_stack["control_client"].fetch_all(
            "SELECT disclosure_json, disclosure_digest, disclosure_version, disclosed_at "
            "FROM dg_system.approvals WHERE approval_id = %s",
            (created.approval_id,),
        )
        assert len(row) == 1
        stored = row[0]
        assert stored["disclosure_digest"] == created.disclosure_digest
        assert len(str(stored["disclosure_digest"])) == 64
        assert stored["disclosure_version"] == "w07-v1"
        assert stored["disclosed_at"] is not None
        assert stored["disclosure_json"], "快照必须真的落库（不是读取时现算）"
        # 三节内容在库里可读
        import json

        snapshot = json.loads(stored["disclosure_json"])
        assert snapshot["risk"]["effective_risk"] == "R2"
        assert snapshot["compensation"]["is_rollback"] is False
        assert len(snapshot["verification_plan"]["assertions"]) == 3
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_snapshot_is_immutable_after_contract_change(w07_stack, rw_client) -> None:
    """批准后把契约声明改掉，`GET` 仍返回旧快照且摘要自洽（快照不随契约漂移）。"""
    before = _current_note(rw_client)
    params = _params(before, "snapshot-immutable")
    tool = w07_stack["registry"].get(TOOL_NAME)
    original_definition = tool.definition
    try:
        _task, created = _created(w07_stack, params)
        approved = w07_stack["approvals"].approve(
            created.approval_id, principal=_APPROVER, reason="快照不可变"
        )
        original_compensation = approved.disclosure["compensation"]["description"]

        # 篡改**契约**（不是库里的快照）
        tool._definition = original_definition.model_copy(
            update={"description": "契约在批准后被改写了"}
        )
        reloaded = w07_stack["approvals"].get(
            created.approval_id, tenant_id="tenant_local"
        )
        assert reloaded.disclosure["compensation"]["description"] == original_compensation
        from core.security.canonical import canonical_digest

        assert canonical_digest(reloaded.disclosure) == reloaded.disclosure_digest
    finally:
        tool._definition = original_definition
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_tamper_json_is_detected_on_read(w07_stack, rw_client) -> None:
    before = _current_note(rw_client)
    params = _params(before, "tamper-on-read")
    try:
        _task, created = _created(w07_stack, params)
        # 具备 UPDATE 权限的账号改写披露内容（保留 digest 列不动）
        w07_stack["control_client"].execute(
            "UPDATE dg_system.approvals "
            "SET disclosure_json = JSON_SET(disclosure_json, '$.risk.effective_risk', 'R0') "
            "WHERE approval_id = %s",
            (created.approval_id,),
        )
        with pytest.raises(StateConflictError) as excinfo:
            w07_stack["approvals"].get(created.approval_id, tenant_id="tenant_local")
        assert excinfo.value.code is ErrorCode.STATE_CONFLICT
        assert excinfo.value.details["reason"] == DISCLOSURE_TAMPERED
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_consume_detects_tamper_before_consumption(w07_stack, rw_client) -> None:
    """被改写的审批不得被消费：台账无记录、状态仍 APPROVED、目标库未改。"""
    before = _current_note(rw_client)
    params = _params(before, "tamper-before-consume")
    try:
        task, created = _created(w07_stack, params)
        w07_stack["approvals"].approve(
            created.approval_id, principal=_APPROVER, reason="先批准再篡改"
        )
        w07_stack["control_client"].execute(
            "UPDATE dg_system.approvals "
            "SET disclosure_json = JSON_SET(disclosure_json, "
            "'$.compensation.is_rollback', true) "
            "WHERE approval_id = %s",
            (created.approval_id,),
        )
        lease = w07_stack["worker"].acquire_lease(
            task.task_id, task.environment_id, ttl_s=30
        )
        result = w07_stack["gateway"].execute(
            _envelope(
                w07_stack,
                task,
                lease,
                approval_id=created.approval_id,
                params=params,
            ),
            params,
        )
        assert result.status is ToolStatus.DENIED
        assert result.error is not None
        assert result.error.code == ErrorCode.STATE_CONFLICT.value
        assert result.error.details.get("reason") == DISCLOSURE_TAMPERED
        assert _current_note(rw_client) == before, "被改写披露的审批不得产生写入"

        consumptions = w07_stack["control_client"].fetch_all(
            "SELECT consumption_id FROM dg_system.approval_consumptions "
            "WHERE approval_id = %s",
            (created.approval_id,),
        )
        assert consumptions == [], "篡改必须发现于消费之前（不得留下消费台账）"
        status_row = w07_stack["control_client"].fetch_all(
            "SELECT status FROM dg_system.approvals WHERE approval_id = %s",
            (created.approval_id,),
        )
        assert status_row[0]["status"] == "APPROVED"
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))


def test_approve_blocked_when_disclosure_section_missing(w07_stack, rw_client) -> None:
    """历史行（无披露快照）批准必须被 `DISCLOSURE_INCOMPLETE` 阻断且状态不变。"""
    before = _current_note(rw_client)
    params = _params(before, "legacy-row-blocked")
    try:
        _task, created = _created(w07_stack, params)
        # 模拟 W06 历史行：清空四列
        w07_stack["control_client"].execute(
            "UPDATE dg_system.approvals SET disclosure_json = NULL, "
            "disclosure_digest = NULL, disclosure_version = NULL, disclosed_at = NULL "
            "WHERE approval_id = %s",
            (created.approval_id,),
        )
        with pytest.raises(Exception) as excinfo:
            w07_stack["approvals"].approve(
                created.approval_id, principal=_APPROVER, reason="历史行"
            )
        error = excinfo.value
        assert error.code is ErrorCode.DISCLOSURE_INCOMPLETE, error
        assert error.status_code == 409
        assert "disclosure.risk" in error.details["missing_fields"]
        assert "disclosure.compensation" in error.details["missing_fields"]
        assert "disclosure.verification_plan" in error.details["missing_fields"]
        status_row = w07_stack["control_client"].fetch_all(
            "SELECT status FROM dg_system.approvals WHERE approval_id = %s",
            (created.approval_id,),
        )
        assert status_row[0]["status"] == "PENDING", "缺披露不改变审批状态"
    finally:
        rw_client.execute(RESTORE_SQL, (before, ORDER_ID))
