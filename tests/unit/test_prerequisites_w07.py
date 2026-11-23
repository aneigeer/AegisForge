"""W07 T3：D2 九类前置条件的判定与失败即阻断（不连数据库）。

覆盖三条验收要点：

1. `{check_id}` 必须覆盖 9 条 D2 且整体 12 条（AC-04）；
2. 逐类"制造坏事实 → 该项非 PASS → 进入 blocking_checks"（9 个独立用例）；
3. 探针端口**没有**接受 SQL 文本的入口（AC-05）+ 标识符白名单 + 缺端口 fail closed。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from core.approval.digest import compute_prerequisites_digest
from core.approval.prerequisites import (
    D2_ADDITIONAL_CHECK_IDS,
    D2_CHECK_IDS,
    DISK_FREE_MIN_MIB,
    FIXED_CHECK_IDS,
    LEGACY_CHECK_IDS,
    LONG_TRX_MAX_SECONDS,
    TABLE_ROWS_MAX,
    TARGET_NOTE_MAX_CHARS,
    THREADS_CONNECTED_MAX_PERCENT,
    THREADS_RUNNING_MAX,
    PrepareObservation,
    PrepareProbePort,
    PrepareRequest,
    PrerequisiteProbe,
    TargetProbePort,
    blocking_checks,
)
from core.security.models import EnvironmentContext
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from tests.helpers.definitions import write_declarations

_ROW = {
    "environment_id": "env_mysql_local",
    "tenant_id": "tenant_local",
    "status": "LAB",
    "db_engine": "mysql",
    "db_version": "8.0.46",
    "image_digest": None,
    "network_id": "lab_net",
    "config_version": "w03-local",
}


class _Repo:
    """`TaskRepository.get_environment()` 的最小替身。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = dict(row or _ROW)

    def get_environment(
        self, environment_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        return dict(self._row)


def _observation(**overrides: Any) -> PrepareObservation:
    """一份**全部 PASS** 的观测；用例只覆盖需要变坏的那一项。"""
    values: dict[str, Any] = {
        "collected_at": "2026-11-11T03:21:45.120Z",
        "signature_matched_rows": 1,
        "signature_null_rows": 0,
        "signature_note_digest": "f" * 64,
        "signature_note_chars": 12,
        "table_rows": 200,
        "table_data_length_bytes": 16_384,
        "disk_free_mib": 10_240,
        "disk_datadir": "/var/lib/mysql/",
        "blocking_locks": 0,
        "longest_trx_s": 0,
        "threads_running": 3,
        "threads_connected": 10,
        "max_connections": 151,
        "available_capabilities": ("optimistic_update",),
        "account_current_user": "dg_rw@localhost",
        "account_read_only": False,
        "account_super_read_only": False,
        "account_update_on_target": True,
    }
    values.update(overrides)
    return PrepareObservation(**values)


class _PrepareProbe:
    def __init__(self, observation: PrepareObservation | None) -> None:
        self._observation = observation
        self.requests: list[PrepareRequest] = []

    def observe(self, request: PrepareRequest) -> PrepareObservation:
        self.requests.append(request)
        if self._observation is None:
            raise RuntimeError("probe failed")
        return self._observation


def _definition(*, prepare_required: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name="mysql_lab_update_order_note",
        description="W07 D2 检查用例桩",
        version="1.0",
        risk_level="R2",
        side_effect="write",
        permissions=["mysql:write"],
        idempotent=True,
        db_engines=["mysql"],
        allowed_environments=["LAB"],
        required_role="dg_rw",
        required_capabilities=["optimistic_update"],
        operation_class="repair",
        input_schema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "table": {"type": "string"},
                "order_id": {"type": "integer"},
                "keys": {"type": "string"},
                "note": {"type": "string"},
                "expected_note": {"type": ["string", "null"]},
            },
            "required": ["database", "table", "order_id", "keys", "note"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        target_objects=[
            TargetObjectSpec(
                object_type="row",
                database_field="database",
                name_field="table",
                keys_field="keys",
                required=True,
            )
        ],
        prepare_required=prepare_required,
        **write_declarations(),
    )


_PARAMS = {
    "database": "dg_lab",
    "table": "seed_orders",
    "order_id": 2001,
    "keys": "order_id=2001",
    "note": "w07",
    "expected_note": None,
}

_ENVIRONMENT = EnvironmentContext(
    environment_id="env_mysql_local",
    tenant_id="tenant_local",
    db_engine="mysql",
    tier="LAB",
    db_version="8.0.46",
)


def _probe(
    observation: PrepareObservation | None = None,
    *,
    row: dict[str, Any] | None = None,
) -> PrerequisiteProbe:
    probe = PrerequisiteProbe(
        _Repo(row),  # type: ignore[arg-type]
        prepare_probe=_PrepareProbe(observation),
    )
    probe.bind_policy_version("w06-v1")
    return probe


def _checks(
    observation: PrepareObservation | None = None,
    *,
    definition: ToolDefinition | None = None,
    row: dict[str, Any] | None = None,
    environment: EnvironmentContext | None = None,
) -> list[dict[str, Any]]:
    return _probe(observation, row=row).probe(
        definition=definition or _definition(),
        environment=environment or _ENVIRONMENT,
        target_objects=[
            {
                "object_type": "row",
                "database": "dg_lab",
                "schema": None,
                "name": "seed_orders",
                "keys": ["order_id=2001"],
            }
        ],
        actor=SimpleNamespace(user_id="user_operator"),  # type: ignore[arg-type]
        params=_PARAMS,
    )


def _by_id(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["check_id"]): item for item in checks}


def test_d2_nine_checks_present() -> None:
    """AC-04：`{check_id}` 必须是 9 条 D2 的超集，且整体固定为 12 条。"""
    checks = _checks(_observation())
    ids = {str(item["check_id"]) for item in checks}
    assert set(D2_CHECK_IDS) <= ids
    assert len(D2_CHECK_IDS) == 9
    assert set(FIXED_CHECK_IDS) <= ids
    assert len(FIXED_CHECK_IDS) == 12
    assert set(LEGACY_CHECK_IDS) <= ids
    # 顺序：5 条 W06 → 7 条 D2 新增 → 声明项（此处无声明项）
    assert [item["check_id"] for item in checks][5:12] == list(D2_ADDITIONAL_CHECK_IDS)


def test_healthy_observation_has_no_blocking_checks() -> None:
    """全部事实正常时不得有任何阻断项（否则审批永远建不出来）。"""
    checks = _checks(_observation())
    assert blocking_checks(checks) == []
    statuses = {item["check_id"]: item["status"] for item in checks}
    assert statuses["target_object_exists"] == "NOT_APPLICABLE"
    assert statuses["target_object_signature"] == "PASS"
    assert statuses["db_identity_privilege"] == "PASS"
    assert statuses["tool_capabilities"] == "PASS"


# ---------------------------------------------------------------------------
# 逐类"变坏即阻断"（9 个独立用例，每个只破坏一项）
# ---------------------------------------------------------------------------


def test_environment_fingerprint_mismatch_blocks() -> None:
    environment = EnvironmentContext(
        environment_id="env_mysql_local",
        tenant_id="tenant_local",
        db_engine="postgresql",  # 与 environments 行声明的 mysql 不一致
        tier="LAB",
    )
    checks = _by_id(_checks(_observation(), environment=environment))
    assert checks["environment_fingerprint"]["status"] == "FAIL"
    assert checks["environment_fingerprint"]["observed"]["reason"] == "DB_ENGINE_MISMATCH"


def test_target_object_signature_missing_row_blocks() -> None:
    checks = _by_id(_checks(_observation(signature_matched_rows=0, signature_note_chars=0)))
    item = checks["target_object_signature"]
    assert item["status"] == "FAIL"
    assert item["observed"]["reason"] == "TARGET_ROW_MISSING"


def test_target_object_signature_too_long_blocks() -> None:
    checks = _by_id(
        _checks(_observation(signature_note_chars=TARGET_NOTE_MAX_CHARS + 1))
    )
    assert checks["target_object_signature"]["status"] == "FAIL"
    assert checks["target_object_signature"]["observed"]["reason"] == "NOTE_TOO_LONG"


def test_table_size_exceeded_blocks() -> None:
    checks = _by_id(_checks(_observation(table_rows=TABLE_ROWS_MAX + 1)))
    assert checks["table_size"]["status"] == "FAIL"
    assert checks["table_size"]["observed"]["reason"] == "TABLE_ROWS_EXCEEDED"


def test_disk_headroom_insufficient_blocks() -> None:
    checks = _by_id(_checks(_observation(disk_free_mib=DISK_FREE_MIN_MIB - 1)))
    assert checks["disk_headroom"]["status"] == "FAIL"
    assert checks["disk_headroom"]["observed"]["reason"] == "DISK_HEADROOM_INSUFFICIENT"


def test_disk_headroom_unreadable_is_unknown() -> None:
    """远程目标 / 路径不可访问：刻意 UNKNOWN（阻断），不猜余量。"""
    checks = _by_id(_checks(_observation(disk_free_mib=None)))
    assert checks["disk_headroom"]["status"] == "UNKNOWN"
    assert checks["disk_headroom"]["observed"]["reason"] == "DATADIR_NOT_ACCESSIBLE"


def test_blocking_locks_present_blocks() -> None:
    checks = _by_id(_checks(_observation(blocking_locks=2)))
    assert checks["blocking_locks"]["status"] == "FAIL"
    assert checks["blocking_locks"]["observed"]["reason"] == "BLOCKING_LOCKS_PRESENT"


def test_long_transaction_blocks() -> None:
    checks = _by_id(_checks(_observation(longest_trx_s=LONG_TRX_MAX_SECONDS + 1)))
    assert checks["long_transactions"]["status"] == "FAIL"
    assert checks["long_transactions"]["observed"]["reason"] == "LONG_TRANSACTION_PRESENT"


def test_server_load_exceeded_blocks() -> None:
    checks = _by_id(_checks(_observation(threads_running=THREADS_RUNNING_MAX + 1)))
    assert checks["server_load"]["status"] == "FAIL"
    assert checks["server_load"]["observed"]["reason"] == "THREADS_RUNNING_EXCEEDED"


def test_server_load_connection_budget_blocks() -> None:
    checks = _by_id(
        _checks(_observation(threads_connected=100, max_connections=100))
    )
    assert checks["server_load"]["status"] == "FAIL"
    assert (
        checks["server_load"]["observed"]["reason"] == "THREADS_CONNECTED_EXCEEDED"
    )
    assert checks["server_load"]["observed"]["within_connection_budget"] is False
    assert THREADS_CONNECTED_MAX_PERCENT == 80


def test_tool_capability_missing_blocks() -> None:
    checks = _by_id(_checks(_observation(available_capabilities=())))
    assert checks["tool_capabilities"]["status"] == "FAIL"
    assert checks["tool_capabilities"]["observed"]["missing"] == ["optimistic_update"]


def test_tool_capability_report_missing_is_unknown() -> None:
    checks = _by_id(_checks(_observation(available_capabilities=None)))
    assert checks["tool_capabilities"]["status"] == "UNKNOWN"
    assert (
        checks["tool_capabilities"]["observed"]["reason"]
        == "CAPABILITY_REPORT_UNAVAILABLE"
    )


def test_db_identity_read_only_connection_blocks() -> None:
    checks = _by_id(_checks(_observation(account_read_only=True)))
    assert checks["db_identity_privilege"]["status"] == "FAIL"
    assert checks["db_identity_privilege"]["observed"]["reason"] == "CONNECTION_READ_ONLY"


def test_db_identity_missing_dml_grant_blocks() -> None:
    checks = _by_id(_checks(_observation(account_update_on_target=False)))
    assert checks["db_identity_privilege"]["status"] == "FAIL"
    assert checks["db_identity_privilege"]["observed"]["reason"] == "MISSING_DML_GRANT"
    assert checks["db_identity_privilege"]["observed"]["verified_against_target"] is True


# ---------------------------------------------------------------------------
# 端口纪律（AC-05）
# ---------------------------------------------------------------------------


def test_prepare_probe_protocol_has_no_sql_parameter() -> None:
    """`PrepareProbePort.observe` 只能有 `request` 一个参数（不接受 SQL 文本）。"""
    signature = inspect.signature(PrepareProbePort.observe)
    assert set(signature.parameters) == {"self", "request"} or set(
        signature.parameters
    ) == {"request"}
    request_type = signature.parameters["request"].annotation
    assert request_type in {"PrepareRequest", PrepareRequest} or (
        request_type is PrepareRequest
    )
    # `PrepareRequest` 自身不得有任何 SQL/查询文本字段
    fields = set(PrepareRequest.__dataclass_fields__)
    assert fields == {"environment_id", "order_id", "target_database", "target_table"}
    for forbidden in ("sql", "query", "statement", "text"):
        assert forbidden not in fields


def test_prerequisite_probe_has_no_sql_accepting_public_method() -> None:
    """`PrerequisiteProbe` 不得暴露任何接受 SQL 的公开方法（防旁路）。"""
    for name in dir(PrerequisiteProbe):
        if name.startswith("_"):
            continue
        attribute = getattr(PrerequisiteProbe, name)
        if not callable(attribute):
            continue
        parameters = set(inspect.signature(attribute).parameters)
        assert not ({"sql", "query", "statement"} & parameters), name


def test_target_probe_port_still_only_object_exists() -> None:
    """AC-05：`TargetProbePort` 原样保留，只有一个方法、无 SQL 入参。"""
    members = {
        name
        for name in dir(TargetProbePort)
        if not name.startswith("_")
    }
    assert members == {"object_exists"}
    parameters = set(inspect.signature(TargetProbePort.object_exists).parameters)
    assert parameters == {"self", "environment_id", "target_object"}


def test_prepare_probe_rejects_non_allowlisted_table() -> None:
    """标识符只接受服务端白名单常量：拼接/注入式表名在构造期即失败。"""
    with pytest.raises(ValueError):
        PrepareRequest(
            environment_id="env_mysql_local",
            order_id=2001,
            target_table="seed_orders; DROP TABLE seed_orders",
        )
    with pytest.raises(ValueError):
        PrepareRequest(
            environment_id="env_mysql_local",
            order_id=2001,
            target_database="dg_system",
        )
    with pytest.raises(ValueError):
        PrepareRequest(environment_id="env_mysql_local", order_id="2001")  # type: ignore[arg-type]


def test_prepare_required_without_port_is_blocked() -> None:
    """`prepare_required=true` + 无 `prepare_probe` → 7 项 `UNKNOWN`（fail closed）。"""
    probe = PrerequisiteProbe(_Repo())  # type: ignore[arg-type]
    probe.bind_policy_version("w06-v1")
    checks = probe.probe(
        definition=_definition(),
        environment=_ENVIRONMENT,
        target_objects=[
            {
                "object_type": "row",
                "database": "dg_lab",
                "schema": None,
                "name": "seed_orders",
                "keys": ["order_id=2001"],
            }
        ],
        actor=SimpleNamespace(user_id="user_operator"),  # type: ignore[arg-type]
        params=_PARAMS,
    )
    by_id = _by_id(checks)
    for check_id in D2_ADDITIONAL_CHECK_IDS:
        assert by_id[check_id]["status"] == "UNKNOWN", check_id
        assert (
            by_id[check_id]["observed"]["reason"] == "PREPARE_PROBE_NOT_CONFIGURED"
        )
    blocking_ids = {item["check_id"] for item in blocking_checks(checks)}
    assert set(D2_ADDITIONAL_CHECK_IDS) <= blocking_ids
    # 探针异常同样 fail closed
    failing = _probe(None)
    failed_ids = {
        item["check_id"]
        for item in blocking_checks(
            failing.probe(
                definition=_definition(),
                environment=_ENVIRONMENT,
                target_objects=[],
                actor=SimpleNamespace(user_id="user_operator"),  # type: ignore[arg-type]
                params=_PARAMS,
            )
        )
    }
    assert set(D2_ADDITIONAL_CHECK_IDS) <= failed_ids


def test_read_only_tool_keeps_w06_five_checks_and_digest_shape() -> None:
    """只读工具仍只有 5 条 W06 检查（+声明项），W06 摘要输入形态不变。"""
    definition = _definition(prepare_required=False)
    checks = _checks(_observation(), definition=definition)
    ids = [str(item["check_id"]) for item in checks]
    assert ids == list(LEGACY_CHECK_IDS[:1]) + [
        "db_identity_privilege",
        "target_object_exists",
        "tool_version_matches",
        "policy_version_matches",
    ] or set(ids) == {
        "environment_fingerprint",
        "db_identity_privilege",
        "target_object_exists",
        "tool_version_matches",
        "policy_version_matches",
    }
    assert not set(D2_ADDITIONAL_CHECK_IDS) & set(ids)
    digest = compute_prerequisites_digest(action_digest="a" * 64, checks=checks)
    assert len(digest) == 64


def test_blocking_checks_reports_full_observed() -> None:
    """阻断明细必须带 `observed`（PRD P0-W07-02 末条：不得只报第一项）。"""
    checks = _checks(
        _observation(
            signature_matched_rows=0,
            signature_note_chars=0,
            blocking_locks=1,
            threads_running=THREADS_RUNNING_MAX + 1,
        )
    )
    blocking = blocking_checks(checks)
    ids = {item["check_id"] for item in blocking}
    assert {
        "target_object_signature",
        "blocking_locks",
        "server_load",
    } <= ids
    for item in blocking:
        assert isinstance(item["observed"], dict)
        assert item["observed"], item["check_id"]
        assert "reason" in item["observed"]
