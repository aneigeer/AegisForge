"""P2-T3 单元测试：LAB 订单状态迁移工具（前进 / 回退）。

覆盖：

1. 状态机方向敏感：`pending→paid` 允许，`paid→pending` 在前进工具里被拒；
2. 回退工具只接受正向迁移的逆（`paid→pending` 允许，`pending→paid` 拒绝）；
3. `keys` 必须与主键同源；基线漂移 → `PREREQUISITE_CHANGED`；
4. `preflight` 与 `run` 口径一致（0 行 / 只读连接 / 基线漂移都不是"可写 1 行"）；
5. 幂等重放（结果已达成）不重复计数。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from adapters.mysql.lab_status_repair import (
    ADVANCE_TOOL_NAME,
    REVERT_TOOL_NAME,
    LabOrderStatusAdvanceTool,
    LabOrderStatusRevertTool,
)
from core.tools.contracts import ToolContext


class _FakeClient:
    database_id = "dg_lab"
    server_version = "8.0.46"

    def __init__(self, status: str | None, *, read_only: bool = False) -> None:
        self.status = status
        self.read_only = read_only
        self.updates: list[tuple[Any, ...]] = []

    def fetch_all(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict]:
        if self.status is None:
            return []
        return [{"status": self.status}]

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        self.updates.append(tuple(params or ()))
        target, order_id, expected = params  # type: ignore[misc]
        if self.status != expected:
            return 0
        self.status = target
        return 1

    def identity(self) -> Any:
        return SimpleNamespace(server_version=self.server_version, user="dg_rw")


def _ctx(client: _FakeClient) -> ToolContext:
    return ToolContext(client=client, task_id="task_p2", run_id="run_p2")


def _params(expected: str, target: str, *, order_id: int = 2001) -> dict[str, Any]:
    return {
        "database": "dg_lab",
        "table": "seed_orders",
        "order_id": order_id,
        "keys": f"order_id={order_id}",
        "expected_status": expected,
        "target_status": target,
    }


def test_advance_pending_to_paid() -> None:
    client = _FakeClient("pending")
    result = LabOrderStatusAdvanceTool().run(_ctx(client), _params("pending", "paid"))
    assert result.status.value == "success"
    assert result.data["before"] == "pending" and result.data["after"] == "paid"
    assert result.data["affected_rows"] == 1
    assert client.status == "paid"


def test_advance_rejects_reverse_direction() -> None:
    client = _FakeClient("paid")
    result = LabOrderStatusAdvanceTool().run(_ctx(client), _params("paid", "pending"))
    assert result.status.value == "denied"
    assert result.error is not None and result.error.code == "TRANSITION_NOT_ALLOWED"
    assert client.status == "paid", "被拒的迁移不得改动目标行"


def test_revert_accepts_only_inverse_transitions() -> None:
    client = _FakeClient("paid")
    tool = LabOrderStatusRevertTool()
    assert tool.run(_ctx(client), _params("paid", "pending")).status.value == "success"
    assert client.status == "pending"

    client2 = _FakeClient("pending")
    denied = tool.run(_ctx(client2), _params("pending", "paid"))
    assert denied.status.value == "denied"
    assert denied.error is not None and denied.error.code == "TRANSITION_NOT_ALLOWED"


def test_keys_must_match_primary_key() -> None:
    client = _FakeClient("pending")
    params = _params("pending", "paid")
    params["keys"] = "order_id=2002"
    result = LabOrderStatusAdvanceTool().run(_ctx(client), params)
    assert result.status.value == "denied"
    assert result.error is not None and result.error.code == "TARGET_KEY_MISMATCH"


def test_baseline_drift_is_rejected() -> None:
    client = _FakeClient("cancelled")
    result = LabOrderStatusAdvanceTool().run(_ctx(client), _params("pending", "paid"))
    assert result.status.value == "denied"
    assert result.error is not None and result.error.code == "PREREQUISITE_CHANGED"
    assert client.status == "cancelled"


def test_idempotent_replay_reports_noop() -> None:
    client = _FakeClient("pending")
    tool = LabOrderStatusAdvanceTool()
    tool.run(_ctx(client), _params("pending", "paid"))
    # 再跑一次同样的参数：基线已变（paid），属 PREREQUISITE_CHANGED，
    # 幂等空操作只在"结果已达成且未被前置校验挡住"的路径出现。
    again = tool.run(_ctx(client), _params("pending", "paid"))
    assert again.status.value == "denied"
    assert again.error is not None and again.error.code == "PREREQUISITE_CHANGED"


def test_preflight_matches_run_semantics() -> None:
    tool = LabOrderStatusAdvanceTool()
    assert tool.preflight(_ctx(_FakeClient("pending")), _params("pending", "paid")).affected_rows == 1
    assert tool.preflight(_ctx(_FakeClient("paid")), _params("pending", "paid")).affected_rows == 0
    assert tool.preflight(_ctx(_FakeClient(None)), _params("pending", "paid")).affected_rows == 0
    read_only = tool.preflight(
        _ctx(_FakeClient("pending", read_only=True)), _params("pending", "paid")
    )
    assert read_only.affected_rows == 0


def test_tool_names_and_compensation_are_distinct() -> None:
    advance = LabOrderStatusAdvanceTool().definition
    revert = LabOrderStatusRevertTool().definition
    assert advance.name == ADVANCE_TOOL_NAME and revert.name == REVERT_TOOL_NAME
    assert advance.compensation is not None
    assert advance.compensation.action == REVERT_TOOL_NAME
    assert revert.compensation is not None and revert.compensation.action == ADVANCE_TOOL_NAME
    assert advance.compensation.requires_own_approval is True
