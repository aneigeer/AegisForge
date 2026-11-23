"""服务端前置条件探测（W06 P0-03b + W07 D2 九类）。

边界（硬规则，逐条可核对）：

- **禁止任意 SQL**：`TargetProbePort` 只暴露"某对象是否存在"这一个固定检查；
  新增的 `PrepareProbePort.observe(request)` 只吃冻结 dataclass，**没有任何接受
  SQL 文本的入口**（SQL 原文全部固化在 `adapters/mysql/prepare_probe.py`）。
- `TargetProbePort` / `PrepareProbePort` 为 None 时**不谎称通过**：前者记
  `NOT_APPLICABLE`（W06 显式降级），后者记 7 项 `UNKNOWN`（W07 fail closed）。
- 人工输入一律**不进入**摘要：`operator_note` 只在审计事件与状态表里出现。
- 任一 check 不是 PASS/NOT_APPLICABLE → 拒绝创建审批（`APPROVAL_PREREQUISITE_FAILED`）。

向后兼容（W06 夹具不得连坐）：

- `PrerequisiteProbe(repository)` 与 `PrerequisiteProbe(repository, target_probe)`
  两种既有构造继续可用（第三参数 keyword-only）。
- 只读工具（`prepare_required=False`）仍只跑 5 条 W06 检查，其
  `prerequisites_digest` 与 W06 完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from core.approval.digest import (
    environment_fingerprint_from_row,
    fingerprint_completeness,
    statement_kind_for,
)
from core.security.models import EnvironmentContext, Principal
from core.tasks.repository import TaskRepository
from core.tools.contracts import ToolDefinition

# ---------------------------------------------------------------------------
# check_id 命名与集合（常量写死，逐项不得裁剪）
# ---------------------------------------------------------------------------

CHECK_ENVIRONMENT_FINGERPRINT = "environment_fingerprint"
CHECK_TARGET_OBJECT_SIGNATURE = "target_object_signature"
CHECK_TABLE_SIZE = "table_size"
CHECK_DISK_HEADROOM = "disk_headroom"
CHECK_BLOCKING_LOCKS = "blocking_locks"
CHECK_LONG_TRANSACTIONS = "long_transactions"
CHECK_SERVER_LOAD = "server_load"
CHECK_TOOL_CAPABILITIES = "tool_capabilities"
CHECK_DB_IDENTITY_PRIVILEGE = "db_identity_privilege"
CHECK_TARGET_OBJECT_EXISTS = "target_object_exists"
CHECK_TOOL_VERSION_MATCHES = "tool_version_matches"
CHECK_POLICY_VERSION_MATCHES = "policy_version_matches"

# D2 九类（PRD P0-W07-02 语义）。
D2_CHECK_IDS: tuple[str, ...] = (
    CHECK_ENVIRONMENT_FINGERPRINT,
    CHECK_TARGET_OBJECT_SIGNATURE,
    CHECK_TABLE_SIZE,
    CHECK_DISK_HEADROOM,
    CHECK_BLOCKING_LOCKS,
    CHECK_LONG_TRANSACTIONS,
    CHECK_SERVER_LOAD,
    CHECK_TOOL_CAPABILITIES,
    CHECK_DB_IDENTITY_PRIVILEGE,
)

# W06 三条版本/存在性检查保留（不重复语义）。
LEGACY_CHECK_IDS: tuple[str, ...] = (
    CHECK_TARGET_OBJECT_EXISTS,
    CHECK_TOOL_VERSION_MATCHES,
    CHECK_POLICY_VERSION_MATCHES,
)

# 12 条固定集合 = 9 条 D2 + 3 条 W06 版本绑定。
FIXED_CHECK_IDS: tuple[str, ...] = D2_CHECK_IDS + LEGACY_CHECK_IDS

# `prepare_required=True` 时新增的 7 条（其余 2 条 D2 是既有检查的收紧）。
D2_ADDITIONAL_CHECK_IDS: tuple[str, ...] = (
    CHECK_TARGET_OBJECT_SIGNATURE,
    CHECK_TABLE_SIZE,
    CHECK_DISK_HEADROOM,
    CHECK_BLOCKING_LOCKS,
    CHECK_LONG_TRANSACTIONS,
    CHECK_SERVER_LOAD,
    CHECK_TOOL_CAPABILITIES,
)

# 这些 check 的事实来自"目标库现算"，跨时刻必然漂移（threads_running / disk_free 等）。
# 因此 consume() 只用**审批登记时**的观测值参与摘要比较，控制面事实才重算。
TARGET_OBSERVED_CHECK_IDS: frozenset[str] = frozenset(
    set(D2_ADDITIONAL_CHECK_IDS) | {CHECK_DB_IDENTITY_PRIVILEGE}
)

# ---------------------------------------------------------------------------
# 阈值（代码常量，不新增配置项；W08 若需按环境调整再抽 Settings）
# ---------------------------------------------------------------------------

DISK_FREE_MIN_MIB = 512
BLOCKING_LOCK_MAX = 0
LONG_TRX_MAX_SECONDS = 60
THREADS_RUNNING_MAX = 64
THREADS_CONNECTED_MAX_PERCENT = 80
TABLE_ROWS_MAX = 100_000
TABLE_DATA_LENGTH_MAX_BYTES = 536_870_912  # 512 MiB
TARGET_MATCHED_ROWS_EXPECTED = 1
TARGET_NOTE_MAX_CHARS = 128
PREPARE_PROBE_VERSION = "w07-v1"

# 标识符白名单：`PrepareRequest` 只接受服务端常量，不接受客户端拼接的库/表名。
ALLOWED_TARGET_DATABASES: tuple[str, ...] = ("dg_lab",)
ALLOWED_TARGET_TABLES: tuple[str, ...] = ("seed_orders",)

# 角色与副作用的声明一致性（控制面可校验的不变量）。
_ROLE_FOR_SIDE_EFFECT = {"none": "dg_ro", "read": "dg_ro", "write": "dg_rw"}


# ---------------------------------------------------------------------------
# 端口与观测结构
# ---------------------------------------------------------------------------


@runtime_checkable
class TargetProbePort(Protocol):
    """只读目标库探测端口：**只有一个固定检查**，不接受任意 SQL。"""

    def object_exists(
        self, *, environment_id: str, target_object: Mapping[str, Any]
    ) -> bool | None:
        """返回 True/False；无法判定时返回 None（调用方按 UNKNOWN 处理，fail closed）。"""
        ...


@dataclass(frozen=True)
class PrepareRequest:
    """Prepare 探针入参：**冻结 dataclass，不含任何 SQL/查询文本字段**。

    `target_database` / `target_table` 由服务端常量给出（不取自客户端参数），
    且必须在白名单内，否则构造即 `ValueError`（AC-05 的签名检查之外的第二道防线）。
    """

    environment_id: str
    order_id: int
    target_database: str = "dg_lab"
    target_table: str = "seed_orders"

    def __post_init__(self) -> None:
        if not isinstance(self.environment_id, str) or not self.environment_id.strip():
            raise ValueError("PrepareRequest.environment_id 不能为空")
        if isinstance(self.order_id, bool) or not isinstance(self.order_id, int):
            raise ValueError("PrepareRequest.order_id 必须是整数主键")
        if self.target_database not in ALLOWED_TARGET_DATABASES:
            raise ValueError(
                f"target_database 不在白名单内：{self.target_database!r}"
            )
        if self.target_table not in ALLOWED_TARGET_TABLES:
            raise ValueError(f"target_table 不在白名单内：{self.target_table!r}")


@dataclass(frozen=True)
class PrepareObservation:
    """一次 Prepare 观测的快照；任一字段为 `None` 即表示"该项无法判定"。

    不设默认值：调用方必须显式给出全部字段，避免"漏填被当成通过"。
    """

    collected_at: str
    signature_matched_rows: int | None
    signature_null_rows: int | None
    signature_note_digest: str | None
    signature_note_chars: int | None
    table_rows: int | None
    table_data_length_bytes: int | None
    disk_free_mib: int | None
    disk_datadir: str | None
    blocking_locks: int | None
    longest_trx_s: int | None
    threads_running: int | None
    threads_connected: int | None
    max_connections: int | None
    available_capabilities: tuple[str, ...] | None
    account_current_user: str | None
    account_read_only: bool | None
    account_super_read_only: bool | None
    account_update_on_target: bool | None


@runtime_checkable
class PrepareProbePort(Protocol):
    """Prepare 探针端口：**只有一个 `request` 参数**，不接受 SQL 文本。"""

    def observe(self, request: PrepareRequest) -> PrepareObservation: ...


def _check(
    check_id: str,
    status: str,
    observed: Mapping[str, Any],
    *,
    source: str = "server_probe",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "observed": dict(observed),
        "source": source,
    }


def _unknown_prepare_check(check_id: str, reason: str) -> dict[str, Any]:
    """探针不可用/无法判定时的统一形态：`UNKNOWN` + 明确原因（阻断审批）。"""
    return _check(
        check_id,
        "UNKNOWN",
        {"reason": reason, "probe_version": PREPARE_PROBE_VERSION},
        source="target_probe",
    )


def extract_order_id(params: Mapping[str, Any] | None) -> int | None:
    """从已规范化参数取整数主键；缺失或类型非法返回 None（调用方按 UNKNOWN 处理）。"""
    if not isinstance(params, Mapping):
        return None
    value = params.get("order_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class PrerequisiteProbe:
    """按固定 check 集探测前置条件；所有事实来自控制库、服务端探测或固定 SQL 探针。"""

    def __init__(
        self,
        repository: TaskRepository,
        target_probe: TargetProbePort | None = None,
        *,
        prepare_probe: PrepareProbePort | None = None,
    ) -> None:
        self._repository = repository
        self._target_probe = target_probe
        # W07：keyword-only，`PrerequisiteProbe(repository)` 与
        # `PrerequisiteProbe(repository, probe)` 两种既有构造行为不变。
        self._prepare_probe = prepare_probe
        # 由 ApprovalService 在装配时绑定，避免客户端传入策略版本。
        self._policy_version: str | None = None

    @property
    def policy_version(self) -> str | None:
        return self._policy_version

    @property
    def prepare_probe_configured(self) -> bool:
        return self._prepare_probe is not None

    def bind_policy_version(self, policy_version: str) -> None:
        """装配期绑定当前 `policy_version`（服务端唯一来源）。"""
        if not policy_version.strip():
            raise ValueError("policy_version 不能为空")
        self._policy_version = policy_version.strip()

    # ---- 端口 ----------------------------------------------------------
    def probe(
        self,
        *,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        target_objects: Sequence[Mapping[str, Any]],
        actor: Principal,
        params: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """返回固定 check 集（+工具契约声明的额外项）的探测结果。

        顺序固定（避免"先报哪一项"随机）：5 条 W06 检查 →
        （仅 `prepare_required`）7 条 D2 新检查 → `_declared_preconditions`。
        """
        row = self._repository.get_environment(
            environment.environment_id, tenant_id=environment.tenant_id
        )
        observation, observe_reason = self._observe(definition, environment, params)
        checks: list[dict[str, Any]] = [
            self._check_environment_fingerprint(definition, environment, row),
            self._check_db_identity_privilege(
                definition,
                environment,
                row,
                actor=actor,
                observation=observation,
                observe_reason=observe_reason,
            ),
            self._check_target_object_exists(definition, environment, target_objects),
            self._check_tool_version(definition),
            self._check_policy_version(definition),
        ]
        if definition.prepare_required:
            checks.extend(
                self._prepare_checks(definition, observation, observe_reason)
            )
        checks.extend(self._declared_preconditions(definition))
        return checks

    def control_plane_checks(
        self,
        *,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        target_objects: Sequence[Mapping[str, Any]],
        actor: Principal,
    ) -> list[dict[str, Any]]:
        """只重算**控制面**事实的 checks（消费路径用，不连目标库）。

        目标库观测（第 ②–⑦⑨ 项之外的 7 条 + 身份/权限）跨时刻必然漂移，因此在
        消费时沿用审批登记值；控制面事实（环境指纹/工具版本/策略版本）每次都重算，
        用于发现"环境换了 / 策略升版了 / 契约漂移了"。
        """
        row = self._repository.get_environment(
            environment.environment_id, tenant_id=environment.tenant_id
        )
        checks: list[dict[str, Any]] = [
            self._check_environment_fingerprint(definition, environment, row),
            self._check_db_identity_privilege(
                definition, environment, row, actor=actor
            ),
            self._check_target_object_exists(definition, environment, target_objects),
            self._check_tool_version(definition),
            self._check_policy_version(definition),
        ]
        checks.extend(self._declared_preconditions(definition))
        return checks

    # ---- W07 观测 ------------------------------------------------------
    def _observe(
        self,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        params: Mapping[str, Any] | None,
    ) -> tuple[PrepareObservation | None, str | None]:
        """取一次 Prepare 观测；返回 `(observation, reason)`，任一为 None 表示不可用。"""
        if not definition.prepare_required:
            return None, None
        if self._prepare_probe is None:
            return None, "PREPARE_PROBE_NOT_CONFIGURED"
        order_id = extract_order_id(params)
        if order_id is None:
            return None, "ORDER_ID_MISSING"
        try:
            observation = self._prepare_probe.observe(
                PrepareRequest(
                    environment_id=environment.environment_id,
                    order_id=order_id,
                )
            )
        except Exception:  # noqa: BLE001 - 探测失败按 UNKNOWN fail closed
            return None, "PREPARE_PROBE_FAILED"
        if not isinstance(observation, PrepareObservation):
            return None, "PREPARE_OBSERVATION_INVALID"
        return observation, None

    def _prepare_checks(
        self,
        definition: ToolDefinition,
        observation: PrepareObservation | None,
        observe_reason: str | None,
    ) -> list[dict[str, Any]]:
        """7 条 D2 新检查：探针不可用时全部 `UNKNOWN`（fail closed，不猜）。"""
        reason = observe_reason or "PREPARE_PROBE_NOT_CONFIGURED"
        if observation is None:
            return [
                _unknown_prepare_check(check_id, reason)
                for check_id in D2_ADDITIONAL_CHECK_IDS
            ]
        return [
            self._check_target_object_signature(definition, observation),
            self._check_table_size(observation),
            self._check_disk_headroom(observation),
            self._check_blocking_locks(observation),
            self._check_long_transactions(observation),
            self._check_server_load(observation),
            self._check_tool_capabilities(definition, observation),
        ]

    @staticmethod
    def _check_target_object_signature(
        definition: ToolDefinition, observation: PrepareObservation
    ) -> dict[str, Any]:
        matched = observation.signature_matched_rows
        null_rows = observation.signature_null_rows
        note_chars = observation.signature_note_chars
        observed: dict[str, Any] = {
            "matched_rows": matched,
            "null_rows": null_rows,
            "note_digest": observation.signature_note_digest,
            "note_chars": note_chars,
            "note_is_null": None if null_rows is None else null_rows > 0,
            "order_id": None,
            "collected_at": observation.collected_at,
            "expected_matched_rows": TARGET_MATCHED_ROWS_EXPECTED,
            "max_note_chars": TARGET_NOTE_MAX_CHARS,
        }
        if matched is None or null_rows is None or note_chars is None:
            observed["reason"] = "SIGNATURE_UNAVAILABLE"
            return _check(
                CHECK_TARGET_OBJECT_SIGNATURE, "UNKNOWN", observed, source="target_probe"
            )
        # 注意：`null_rows ∈ {0,1}` 是"行签名合法"的判据之一（NULL 也是合法当前值）。
        if matched == 0:
            observed["reason"] = "TARGET_ROW_MISSING"
            status = "FAIL"
        elif matched > TARGET_MATCHED_ROWS_EXPECTED:
            observed["reason"] = "TARGET_ROW_NOT_UNIQUE"
            status = "FAIL"
        elif note_chars > TARGET_NOTE_MAX_CHARS:
            observed["reason"] = "NOTE_TOO_LONG"
            status = "FAIL"
        elif null_rows not in (0, 1):
            observed["reason"] = "SIGNATURE_INCONSISTENT"
            status = "FAIL"
        else:
            observed["reason"] = "OK"
            status = "PASS"
        return _check(
            CHECK_TARGET_OBJECT_SIGNATURE, status, observed, source="target_probe"
        )

    @staticmethod
    def _check_table_size(observation: PrepareObservation) -> dict[str, Any]:
        rows = observation.table_rows
        length = observation.table_data_length_bytes
        observed: dict[str, Any] = {
            "table_rows": rows,
            "data_length_bytes": length,
            "limits": {
                "table_rows_max": TABLE_ROWS_MAX,
                "data_length_max_bytes": TABLE_DATA_LENGTH_MAX_BYTES,
            },
            "collected_at": observation.collected_at,
        }
        if rows is None or length is None:
            observed["reason"] = "TABLE_SIZE_UNAVAILABLE"
            return _check(CHECK_TABLE_SIZE, "UNKNOWN", observed, source="target_probe")
        if rows > TABLE_ROWS_MAX:
            observed["reason"] = "TABLE_ROWS_EXCEEDED"
            return _check(CHECK_TABLE_SIZE, "FAIL", observed, source="target_probe")
        if length > TABLE_DATA_LENGTH_MAX_BYTES:
            observed["reason"] = "TABLE_LENGTH_EXCEEDED"
            return _check(CHECK_TABLE_SIZE, "FAIL", observed, source="target_probe")
        observed["reason"] = "OK"
        return _check(CHECK_TABLE_SIZE, "PASS", observed, source="target_probe")

    @staticmethod
    def _check_disk_headroom(observation: PrepareObservation) -> dict[str, Any]:
        free_mib = observation.disk_free_mib
        observed: dict[str, Any] = {
            "datadir": observation.disk_datadir,
            "free_mib": free_mib,
            "required_mib": DISK_FREE_MIN_MIB,
            "collected_at": observation.collected_at,
        }
        if free_mib is None or observation.disk_datadir is None:
            # 远端目标 / 路径不可访问：刻意 fail closed，不猜测余量。
            observed["reason"] = "DATADIR_NOT_ACCESSIBLE"
            return _check(CHECK_DISK_HEADROOM, "UNKNOWN", observed, source="target_probe")
        if free_mib < DISK_FREE_MIN_MIB:
            observed["reason"] = "DISK_HEADROOM_INSUFFICIENT"
            return _check(CHECK_DISK_HEADROOM, "FAIL", observed, source="target_probe")
        observed["reason"] = "OK"
        return _check(CHECK_DISK_HEADROOM, "PASS", observed, source="target_probe")

    @staticmethod
    def _check_blocking_locks(observation: PrepareObservation) -> dict[str, Any]:
        locks = observation.blocking_locks
        observed: dict[str, Any] = {
            "blocking_locks": locks,
            "table": f"{ALLOWED_TARGET_DATABASES[0]}.{ALLOWED_TARGET_TABLES[0]}",
            "max_allowed": BLOCKING_LOCK_MAX,
            "collected_at": observation.collected_at,
        }
        if locks is None:
            observed["reason"] = "LOCK_PROBE_UNAVAILABLE"
            return _check(CHECK_BLOCKING_LOCKS, "UNKNOWN", observed, source="target_probe")
        if locks > BLOCKING_LOCK_MAX:
            observed["reason"] = "BLOCKING_LOCKS_PRESENT"
            return _check(CHECK_BLOCKING_LOCKS, "FAIL", observed, source="target_probe")
        observed["reason"] = "OK"
        return _check(CHECK_BLOCKING_LOCKS, "PASS", observed, source="target_probe")

    @staticmethod
    def _check_long_transactions(observation: PrepareObservation) -> dict[str, Any]:
        longest = observation.longest_trx_s
        observed: dict[str, Any] = {
            "longest_trx_s": longest,
            "threshold_s": LONG_TRX_MAX_SECONDS,
            "collected_at": observation.collected_at,
        }
        if longest is None:
            observed["reason"] = "TRX_PROBE_UNAVAILABLE"
            return _check(
                CHECK_LONG_TRANSACTIONS, "UNKNOWN", observed, source="target_probe"
            )
        if longest > LONG_TRX_MAX_SECONDS:
            observed["reason"] = "LONG_TRANSACTION_PRESENT"
            return _check(
                CHECK_LONG_TRANSACTIONS, "FAIL", observed, source="target_probe"
            )
        observed["reason"] = "OK"
        return _check(CHECK_LONG_TRANSACTIONS, "PASS", observed, source="target_probe")

    @staticmethod
    def _check_server_load(observation: PrepareObservation) -> dict[str, Any]:
        running = observation.threads_running
        connected = observation.threads_connected
        max_connections = observation.max_connections
        budget = (
            None
            if connected is None or max_connections is None
            else connected * 100 <= max_connections * THREADS_CONNECTED_MAX_PERCENT
        )
        observed: dict[str, Any] = {
            "threads_running": running,
            "threads_connected": connected,
            "max_connections": max_connections,
            "within_connection_budget": budget,
            "threads_running_max": THREADS_RUNNING_MAX,
            "threads_connected_max_percent": THREADS_CONNECTED_MAX_PERCENT,
            "collected_at": observation.collected_at,
        }
        if running is None or budget is None:
            # `max_connections` 缺失按 UNKNOWN，不按 0（否则会把"未知"算成"超限"）。
            observed["reason"] = "LOAD_PROBE_UNAVAILABLE"
            return _check(CHECK_SERVER_LOAD, "UNKNOWN", observed, source="target_probe")
        if running > THREADS_RUNNING_MAX:
            observed["reason"] = "THREADS_RUNNING_EXCEEDED"
            return _check(CHECK_SERVER_LOAD, "FAIL", observed, source="target_probe")
        if not budget:
            observed["reason"] = "THREADS_CONNECTED_EXCEEDED"
            return _check(CHECK_SERVER_LOAD, "FAIL", observed, source="target_probe")
        observed["reason"] = "OK"
        return _check(CHECK_SERVER_LOAD, "PASS", observed, source="target_probe")

    @staticmethod
    def _check_tool_capabilities(
        definition: ToolDefinition, observation: PrepareObservation
    ) -> dict[str, Any]:
        required = list(definition.required_capabilities)
        available = observation.available_capabilities
        observed: dict[str, Any] = {
            "required": required,
            "available": None if available is None else list(available),
            "missing": None,
            "collected_at": observation.collected_at,
        }
        if available is None:
            observed["reason"] = "CAPABILITY_REPORT_UNAVAILABLE"
            return _check(
                CHECK_TOOL_CAPABILITIES, "UNKNOWN", observed, source="server_probe"
            )
        missing = [item for item in required if item not in set(available)]
        observed["missing"] = missing
        if missing:
            observed["reason"] = "CAPABILITY_MISSING"
            return _check(CHECK_TOOL_CAPABILITIES, "FAIL", observed, source="server_probe")
        observed["reason"] = "OK"
        return _check(CHECK_TOOL_CAPABILITIES, "PASS", observed, source="server_probe")

    # ---- W06 单项检查 --------------------------------------------------
    def _check_environment_fingerprint(
        self,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        row: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if row is None:
            return _check(
                CHECK_ENVIRONMENT_FINGERPRINT,
                "FAIL",
                {
                    "environment_id": environment.environment_id,
                    "reason": "ENVIRONMENT_RECORD_MISSING",
                    "tenant_id": environment.tenant_id,
                },
                source="control_db",
            )
        fingerprint = environment_fingerprint_from_row(row)
        completeness = fingerprint_completeness(fingerprint)
        status = "PASS"
        reason = "OK"
        if fingerprint.get("db_engine") != environment.db_engine:
            status = "FAIL"
            reason = "DB_ENGINE_MISMATCH"
        elif str(row.get("status") or "").strip().upper() != "LAB":
            status = "FAIL"
            reason = "ENVIRONMENT_NOT_LAB"
        return _check(
            CHECK_ENVIRONMENT_FINGERPRINT,
            status,
            {
                "environment_id": environment.environment_id,
                "environment_status": row.get("status"),
                "fingerprint": fingerprint,
                "fingerprint_completeness": completeness,
                "reason": reason,
                "engine_supported": environment.db_engine in definition.db_engines,
            },
            source="control_db",
        )

    def _check_db_identity_privilege(
        self,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        row: Mapping[str, Any] | None,
        *,
        actor: Principal,
        observation: PrepareObservation | None = None,
        observe_reason: str | None = None,
    ) -> dict[str, Any]:
        """校验"声明的数据库角色与副作用一致"，并在有观测时核实目标库权限。

        W06 只能校验控制面不变量（`verified_against_target=False`，如实标注）；
        W07 的 `prepare_required` 工具会用执行账号（`dg_rw`）的固定 SQL 观测结果
        补齐 `read_only` / `super_read_only` / `UPDATE on dg_lab.*` 三项。
        """
        expected_role = _ROLE_FOR_SIDE_EFFECT.get(definition.side_effect, "dg_ro")
        observed: dict[str, Any] = {
            "required_role": definition.required_role,
            "expected_role": expected_role,
            "side_effect": definition.side_effect,
            "environment_id": environment.environment_id,
            "current_user": None,
            "read_only": None,
            "super_read_only": None,
            "has_update_on_target": None,
            "verified_against_target": False,
        }
        if definition.required_role != expected_role:
            observed["reason"] = "DECLARED_ROLE_MISMATCH"
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "FAIL",
                observed,
                source="server_probe",
            )
        if row is None or str(row.get("status") or "").strip().upper() != "LAB":
            observed["reason"] = "ENVIRONMENT_NOT_LAB"
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "FAIL",
                observed,
                source="server_probe",
            )
        if not definition.prepare_required or observation is None:
            # W06 口径：不宣称已验证目标库权限（如实标注）。
            observed["reason"] = "DECLARED_ROLE_CONSISTENT"
            if definition.prepare_required:
                observed["probe_reason"] = observe_reason
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "PASS",
                observed,
                source="server_probe",
            )

        observed.update(
            {
                "current_user": observation.account_current_user,
                "read_only": observation.account_read_only,
                "super_read_only": observation.account_super_read_only,
                "has_update_on_target": observation.account_update_on_target,
                "verified_against_target": True,
                "collected_at": observation.collected_at,
            }
        )
        if (
            observation.account_current_user is None
            or observation.account_read_only is None
            or observation.account_super_read_only is None
            or observation.account_update_on_target is None
        ):
            observed["reason"] = "IDENTITY_PROBE_UNAVAILABLE"
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "UNKNOWN",
                observed,
                source="target_probe",
            )
        if observation.account_read_only or observation.account_super_read_only:
            observed["reason"] = "CONNECTION_READ_ONLY"
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "FAIL",
                observed,
                source="target_probe",
            )
        if not observation.account_update_on_target:
            observed["reason"] = "MISSING_DML_GRANT"
            return _check(
                CHECK_DB_IDENTITY_PRIVILEGE,
                "FAIL",
                observed,
                source="target_probe",
            )
        observed["reason"] = "OK"
        return _check(
            CHECK_DB_IDENTITY_PRIVILEGE, "PASS", observed, source="target_probe"
        )

    def _check_target_object_exists(
        self,
        definition: ToolDefinition,
        environment: EnvironmentContext,
        target_objects: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        objects = [dict(item) for item in target_objects]
        if definition.prepare_required:
            # 行签名检查（`target_object_signature`）已覆盖更强的语义：存在 + 唯一 + 当前值。
            return _check(
                CHECK_TARGET_OBJECT_EXISTS,
                "NOT_APPLICABLE",
                {
                    "object_count": len(objects),
                    "reason": "SUPERSEDED_BY_SIGNATURE",
                },
            )
        if not objects:
            return _check(
                CHECK_TARGET_OBJECT_EXISTS,
                "NOT_APPLICABLE",
                {"object_count": 0, "reason": "NO_TARGET_OBJECTS"},
            )
        if self._target_probe is None:
            # 显式降级：未注入只读端口时不做存在性判断，也不谎称已通过。
            return _check(
                CHECK_TARGET_OBJECT_EXISTS,
                "NOT_APPLICABLE",
                {
                    "object_count": len(objects),
                    "checked": False,
                    "reason": "TARGET_PROBE_NOT_CONFIGURED",
                },
            )
        found: list[dict[str, Any]] = []
        unknown = False
        missing = False
        for item in objects:
            try:
                result = self._target_probe.object_exists(
                    environment_id=environment.environment_id, target_object=item
                )
            except Exception:  # noqa: BLE001 - 探测失败按 UNKNOWN fail closed
                result = None
            if result is None:
                unknown = True
            elif not result:
                missing = True
            found.append(
                {
                    "object_type": item.get("object_type"),
                    "database": item.get("database"),
                    "name": item.get("name"),
                    "exists": result,
                }
            )
        status = "UNKNOWN" if unknown else ("FAIL" if missing else "PASS")
        reason = (
            "OBJECT_EXISTENCE_UNKNOWN" if unknown else ("OBJECT_MISSING" if missing else "OK")
        )
        return _check(
            CHECK_TARGET_OBJECT_EXISTS,
            status,
            {"object_count": len(objects), "checked": True, "objects": found, "reason": reason},
        )

    @staticmethod
    def _check_tool_version(definition: ToolDefinition) -> dict[str, Any]:
        version = str(definition.version or "").strip()
        status = "PASS" if version else "FAIL"
        return _check(
            CHECK_TOOL_VERSION_MATCHES,
            status,
            {
                "tool_name": definition.name,
                "tool_version": version or None,
                "status_label": definition.risk_level,
                "statement_kind": statement_kind_for(definition),
                "reason": "OK" if version else "TOOL_VERSION_MISSING",
            },
            source="control_db",
        )

    def _check_policy_version(self, definition: ToolDefinition) -> dict[str, Any]:
        if not self._policy_version:
            return _check(
                CHECK_POLICY_VERSION_MATCHES,
                "UNKNOWN",
                {
                    "tool_name": definition.name,
                    "reason": "POLICY_VERSION_NOT_BOUND",
                },
            )
        return _check(
            CHECK_POLICY_VERSION_MATCHES,
            "PASS",
            {
                "tool_name": definition.name,
                "policy_version": self._policy_version,
                "reason": "OK",
            },
        )

    @staticmethod
    def _declared_preconditions(definition: ToolDefinition) -> list[dict[str, Any]]:
        """工具契约声明的额外前置条件：本轮只能如实记为"仅声明、未核验"。"""
        declared = getattr(definition, "preconditions", None) or []
        checks: list[dict[str, Any]] = []
        for text in declared:
            if not isinstance(text, str) or not text.strip():
                continue
            checks.append(
                _check(
                    f"declared:{text.strip()[:48]}",
                    "NOT_APPLICABLE",
                    {"text": text.strip(), "reason": "DECLARATIVE_ONLY", "verified": False},
                    source="control_db",
                )
            )
        return checks


def blocking_checks(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """返回阻断创建的 check（状态不是 PASS/NOT_APPLICABLE）。"""
    return [
        dict(check)
        for check in checks
        if str(check.get("status")) not in {"PASS", "NOT_APPLICABLE"}
    ]


def merge_with_stored_observations(
    fresh: Sequence[Mapping[str, Any]],
    stored: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """消费路径：把"本次重算的控制面检查"与"审批登记时的目标库观测"合并。

    为什么必须这样合并：`threads_running` / `disk_free_mib` / `blocking_locks` 等
    目标库事实**每次探测都会变**，若参与 `prerequisites_digest` 比较，任何审批都
    永远无法消费。因此目标库观测沿用登记值（它已经决定了"能否创建审批"），
    目标行是否仍是批准时的样子由 `action_digest`（含 `expected_note` 基线）与
    工具 SQL 的 `WHERE note <=> ?` 双重保证。
    """
    fresh_by_id = {str(item.get("check_id")): dict(item) for item in fresh}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in stored:
        check_id = str(item.get("check_id"))
        seen.add(check_id)
        if check_id in TARGET_OBSERVED_CHECK_IDS:
            merged.append(dict(item))
        else:
            merged.append(fresh_by_id.get(check_id, dict(item)))
    for check_id, item in fresh_by_id.items():
        if check_id not in seen:
            merged.append(item)
    return merged


__all__ = [
    "ALLOWED_TARGET_DATABASES",
    "ALLOWED_TARGET_TABLES",
    "BLOCKING_LOCK_MAX",
    "CHECK_BLOCKING_LOCKS",
    "CHECK_DB_IDENTITY_PRIVILEGE",
    "CHECK_DISK_HEADROOM",
    "CHECK_ENVIRONMENT_FINGERPRINT",
    "CHECK_LONG_TRANSACTIONS",
    "CHECK_POLICY_VERSION_MATCHES",
    "CHECK_SERVER_LOAD",
    "CHECK_TABLE_SIZE",
    "CHECK_TARGET_OBJECT_EXISTS",
    "CHECK_TARGET_OBJECT_SIGNATURE",
    "CHECK_TOOL_CAPABILITIES",
    "CHECK_TOOL_VERSION_MATCHES",
    "D2_ADDITIONAL_CHECK_IDS",
    "D2_CHECK_IDS",
    "DISK_FREE_MIN_MIB",
    "FIXED_CHECK_IDS",
    "LEGACY_CHECK_IDS",
    "LONG_TRX_MAX_SECONDS",
    "PREPARE_PROBE_VERSION",
    "PrepareObservation",
    "PrepareProbePort",
    "PrepareRequest",
    "PrerequisiteProbe",
    "TABLE_DATA_LENGTH_MAX_BYTES",
    "TABLE_ROWS_MAX",
    "TARGET_MATCHED_ROWS_EXPECTED",
    "TARGET_NOTE_MAX_CHARS",
    "TARGET_OBSERVED_CHECK_IDS",
    "THREADS_CONNECTED_MAX_PERCENT",
    "THREADS_RUNNING_MAX",
    "TargetProbePort",
    "blocking_checks",
    "extract_order_id",
    "merge_with_stored_observations",
]
