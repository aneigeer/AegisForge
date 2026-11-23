"""审批仓储：状态表读写 + 消费台账插入（全部 SQL 集中在此）。

约束：
- 状态迁移一律使用**条件 UPDATE**（`WHERE status IN (...)`）并返回受影响行数，
  由调用方根据 0 行结果重读状态并映射稳定错误码；
- 消费台账只 INSERT（`uq_consumption_approval` / `uq_consumption_nonce` 双唯一键
  是"只能消费一次"的最终保障）；
- 所有时间入库前转 naive UTC，读出一律 tz-aware UTC。
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pymysql

from core.approval.disclosure import DISCLOSURE_TAMPERED, verify_disclosure
from core.approval.models import (
    Approval,
    ApprovalStatus,
    PrerequisiteCheck,
    can_transition,
)
from core.errors import (
    ApprovalNotFoundError,
    DatabaseQueryError,
    EvidenceInvalidError,
    RequestInvalidError,
    StateConflictError,
)
from core.logging import get_logger
from database.mysql.connection import MysqlClient
from database.mysql.transaction import execute_in, fetch_all_in

logger = get_logger(__name__)

APPROVALS = "dg_system.approvals"
CONSUMPTIONS = "dg_system.approval_consumptions"

# 条件 UPDATE 允许修改的列（白名单；不允许改摘要、租户、申请人等身份字段）。
_MUTABLE_COLUMNS = (
    "status",
    "approver_id",
    "approver_session_id",
    "decided_at",
    "decision_reason",
    "consumed_at",
    "consumed_by",
    "revoked_at",
    "revoked_by",
)

# W07：披露快照四列（创建时刻写一次，之后只能由人工 DBA 修数据）。
# L1 约束：它们**绝不能**出现在 `_MUTABLE_COLUMNS` 里，否则状态迁移就能改披露内容。
DISCLOSURE_COLUMNS: tuple[str, ...] = (
    "disclosure_json",
    "disclosure_digest",
    "disclosure_version",
    "disclosed_at",
)

if set(DISCLOSURE_COLUMNS) & set(_MUTABLE_COLUMNS):  # pragma: no cover - 装配期自检
    raise RuntimeError("披露快照列不得出现在审批状态更新的列白名单中")


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RequestInvalidError(
            "审批字段无法序列化为 JSON", details={"reason": str(exc)}
        ) from None


def _json_load(value: Any, default: Any, *, column: str = "json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        # 损坏的证据全文不得静默降级成空描述符（会让 get/list 展示假数据）。
        logger.error(
            "审批证据 JSON 损坏，拒绝映射",
            extra={"alert": "APPROVAL_EVIDENCE_CORRUPT", "column": column},
        )
        raise EvidenceInvalidError(
            "审批证据 JSON 损坏，拒绝映射",
            details={"column": column},
        ) from None


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc)
    return None


def _encode_cursor(approval: Approval) -> str:
    raw = f"{approval.created_at.isoformat()}|{approval.approval_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        timestamp, approval_id = raw.split("|", 1)
        moment = datetime.fromisoformat(timestamp)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise RequestInvalidError("审批游标非法", details={"cursor_invalid": True}) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment, approval_id


class ApprovalRepository:
    """`dg_system.approvals` 与 `dg_system.approval_consumptions` 的读写。"""

    def __init__(self, client: MysqlClient) -> None:
        self._client = client

    @property
    def client(self) -> MysqlClient:
        return self._client

    # ---- 写入 ----------------------------------------------------------
    def insert(
        self, approval: Approval, *, compensation_of: str | None = None
    ) -> None:
        """写入审批行（含 W07 披露快照四列 + W08 `compensation_of`）。

        `compensation_of` 是**插入时写一次**的关联列（唯一键
        `uq_approvals_compensation_of`），刻意不进 `_MUTABLE_COLUMNS`：
        状态迁移不能改写"谁是补偿对象"。它也不进 `action_digest`（ADR-0005 口径：
        补偿与原动作靠参数互换产生确定性不同摘要，不靠这个列）。

        L1 不变量：新行的 `disclosure_digest` 必须非空。历史行允许 NULL（W06 无法回填），
        但**新行不允许**——否则"没有披露的审批"会重新变得可批准。
        """
        if not approval.disclosure_digest:
            raise RequestInvalidError(
                "审批缺少披露快照摘要，禁止写入",
                details={
                    "approval_id": approval.approval_id,
                    "reason": "DISCLOSURE_DIGEST_REQUIRED",
                },
            )
        try:
            self._client.execute(
                f"INSERT INTO {APPROVALS} (approval_id, tenant_id, task_id, step_id, "
                "environment_id, db_engine, environment_fingerprint, "
                "environment_fingerprint_json, action_digest, parameters_digest, "
                "target_objects_digest, prerequisites_digest, prerequisites_json, "
                "action_descriptor_json, disclosure_json, disclosure_digest, "
                "disclosure_version, disclosed_at, policy_version, tool_name, tool_version, "
                "requested_by, requested_session_id, approver_id, approver_session_id, "
                "authn_context_json, nonce_hash, status, decision_reason, operator_note, "
                "created_at, expires_at, decided_at, consumed_at, consumed_by, revoked_at, "
                "revoked_by, compensation_of) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    approval.approval_id,
                    approval.tenant_id,
                    approval.task_id,
                    approval.step_id,
                    approval.environment_id,
                    approval.db_engine,
                    approval.environment_fingerprint_digest,
                    _json_dump(approval.environment_fingerprint),
                    approval.action_digest,
                    approval.parameters_digest,
                    approval.target_objects_digest,
                    approval.prerequisites_digest,
                    _json_dump([check.to_digest_dict() for check in approval.prerequisites]),
                    _json_dump(approval.action_descriptor),
                    _json_dump(approval.disclosure),
                    approval.disclosure_digest,
                    approval.disclosure_version,
                    _to_db(approval.disclosed_at),
                    approval.policy_version,
                    approval.tool_name,
                    approval.tool_version,
                    approval.requested_by,
                    approval.requested_session_id,
                    approval.approver_id,
                    approval.approver_session_id,
                    _json_dump(approval.authn_context),
                    approval.nonce_hash,
                    approval.status.value,
                    approval.decision_reason,
                    approval.operator_note,
                    _to_db(approval.created_at),
                    _to_db(approval.expires_at),
                    _to_db(approval.decided_at),
                    _to_db(approval.consumed_at),
                    approval.consumed_by,
                    _to_db(approval.revoked_at),
                    approval.revoked_by,
                    compensation_of,
                ),
            )
        except DatabaseQueryError as exc:
            if not duplicate_key(exc):
                raise
            # 唯一键冲突必须映射为领域错误：原审批只能有一份补偿审批。
            raise StateConflictError(
                "该原审批已存在补偿审批，禁止重复发起"
                if compensation_of
                else "审批行已存在，禁止重复写入",
                details={
                    "reason": (
                        "COMPENSATION_ALREADY_REQUESTED"
                        if compensation_of
                        else "APPROVAL_ALREADY_EXISTS"
                    ),
                    "approval_id": approval.approval_id,
                    "compensation_of": compensation_of,
                },
            ) from None

    def get_compensation_of(self, approval_id: str) -> str | None:
        """读取补偿关联列（`NULL` = 该审批不是补偿）。

        单列读取而非扩 `Approval` 模型字段：本卡不改 `core/approval/models.py`，
        避免影响披露/状态语义；需要对外展示时再由后续卡统一提升。
        """
        rows = self._client.fetch_all(
            f"SELECT compensation_of FROM {APPROVALS} WHERE approval_id = %s LIMIT 1",
            (approval_id,),
        )
        if not rows:
            return None
        value = rows[0].get("compensation_of")
        return str(value) if value is not None else None

    def find_compensation_for(self, original_approval_id: str) -> str | None:
        """W09 T03 / W08 KD-1：反查「谁补偿了这份原审批」。

        与 `get_compensation_of()` 方向相反：后者读「本审批补偿了谁」，
        本方法读 `approvals.compensation_of = 原审批` 的独立补偿审批 ID。
        唯一键 `uq_approvals_compensation_of` 保证最多一条；只读 SELECT，
        不改任何行、不扩大 `dg_ctl` 的写权限。
        """
        rows = self._client.fetch_all(
            f"SELECT approval_id FROM {APPROVALS} "
            "WHERE compensation_of = %s ORDER BY approval_id LIMIT 1",
            (original_approval_id,),
        )
        if not rows:
            return None
        value = rows[0].get("approval_id")
        return str(value) if value is not None else None

    def transition(
        self,
        approval_id: str,
        *,
        expected: Sequence[ApprovalStatus],
        target: ApprovalStatus,
        fields: Mapping[str, Any] | None = None,
    ) -> int:
        """条件 UPDATE：仅当当前状态在 `expected` 中才生效；返回受影响行数。

        仓储层复核：每个 `expected` 前置态到 `target` 的迁移都必须在 P0-05
        白名单内，防止调用方（或越权改写）传入如 CONSUMED → APPROVED 的非法迁移。
        """
        for status in expected:
            if not can_transition(status, target):
                raise StateConflictError(
                    "非法审批状态迁移",
                    details={"from": status.value, "to": target.value},
                )
        assignments: list[str] = ["status = %s"]
        params: list[Any] = [target.value]
        for column, value in (fields or {}).items():
            if column not in _MUTABLE_COLUMNS:
                raise RequestInvalidError(
                    "审批状态更新包含不允许的列", details={"column": column}
                )
            assignments.append(f"{column} = %s")
            params.append(_to_db(value) if isinstance(value, datetime) else value)
        placeholders = ",".join(["%s"] * len(expected))
        params.append(approval_id)
        params.extend(status.value for status in expected)
        return self._client.execute(
            f"UPDATE {APPROVALS} SET {', '.join(assignments)} "
            f"WHERE approval_id = %s AND status IN ({placeholders})",
            tuple(params),
        )

    # ---- 查询 ----------------------------------------------------------
    def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        rows = self._client.fetch_all(
            f"SELECT * FROM {APPROVALS} WHERE approval_id = %s AND tenant_id = %s LIMIT 1",
            (approval_id, tenant_id),
        )
        if not rows:
            # 跨租户按"不存在"处理，不泄漏审批是否存在。
            raise ApprovalNotFoundError(
                "审批不存在", details={"approval_id": approval_id}
            )
        return self._to_approval(rows[0])

    def list(
        self,
        *,
        tenant_id: str,
        status: ApprovalStatus | None = None,
        task_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Approval], str | None]:
        if limit < 1 or limit > 200:
            raise RequestInvalidError("limit 必须在 1..200 之间", details={"limit": limit})
        conditions = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if status is not None:
            conditions.append("status = %s")
            params.append(status.value)
        if task_id is not None:
            conditions.append("task_id = %s")
            params.append(task_id)
        if cursor:
            moment, approval_id = _decode_cursor(cursor)
            conditions.append(
                "(created_at < %s OR (created_at = %s AND approval_id < %s))"
            )
            params.extend([_to_db(moment), _to_db(moment), approval_id])
        params.append(limit + 1)
        rows = self._client.fetch_all(
            f"SELECT * FROM {APPROVALS} WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC, approval_id DESC LIMIT %s",
            tuple(params),
        )
        approvals = [self._to_approval(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(approvals[-1]) if len(rows) > limit and approvals else None
        return approvals, next_cursor

    def list_due_expiry(self, *, now: datetime, limit: int = 100) -> list[Approval]:
        """待过期的审批（PENDING/APPROVED 且 expires_at <= now）。"""
        rows = self._client.fetch_all(
            f"SELECT * FROM {APPROVALS} "
            "WHERE status IN (%s,%s) AND expires_at <= %s "
            "ORDER BY expires_at ASC LIMIT %s",
            (
                ApprovalStatus.PENDING.value,
                ApprovalStatus.APPROVED.value,
                _to_db(now),
                limit,
            ),
        )
        return [self._to_approval(row) for row in rows]

    # ---- 事务内操作（消费路径专用）-------------------------------------
    def lock_row(
        self, conn: pymysql.Connection, approval_id: str, *, tenant_id: str
    ) -> dict[str, Any] | None:
        """`SELECT ... FOR UPDATE`：当前读，可见最新已提交状态。"""
        rows = fetch_all_in(
            self._client,
            conn,
            f"SELECT * FROM {APPROVALS} WHERE approval_id = %s AND tenant_id = %s "
            "FOR UPDATE",
            (approval_id, tenant_id),
        )
        return rows[0] if rows else None

    def reread_status(
        self, conn: pymysql.Connection, approval_id: str
    ) -> ApprovalStatus | None:
        rows = fetch_all_in(
            self._client,
            conn,
            f"SELECT status FROM {APPROVALS} WHERE approval_id = %s LIMIT 1",
            (approval_id,),
        )
        if not rows:
            return None
        try:
            return ApprovalStatus(str(rows[0]["status"]))
        except ValueError:
            return None

    def insert_consumption(
        self,
        conn: pymysql.Connection,
        *,
        consumption_id: str,
        approval_id: str,
        tenant_id: str,
        task_id: str,
        nonce_hash: str,
        action_digest: str,
        policy_version: str,
        consumed_by: str,
        consumed_at: datetime,
        tool_call_id: str | None = None,
        envelope_id: str | None = None,
    ) -> None:
        """在同一事务内插入消费台账；撞唯一键由调用方映射为"已消费"。"""
        execute_in(
            self._client,
            conn,
            f"INSERT INTO {CONSUMPTIONS} (consumption_id, approval_id, tenant_id, task_id, "
            "tool_call_id, envelope_id, nonce_hash, action_digest, policy_version, "
            "consumed_by, consumed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                consumption_id,
                approval_id,
                tenant_id,
                task_id,
                tool_call_id,
                envelope_id,
                nonce_hash,
                action_digest,
                policy_version,
                consumed_by,
                _to_db(consumed_at),
            ),
        )

    def mark_consumed(
        self,
        conn: pymysql.Connection,
        approval_id: str,
        *,
        consumed_by: str,
        consumed_at: datetime,
    ) -> int:
        """条件更新为 CONSUMED；返回受影响行数（0 表示状态已被他人改变）。"""
        return execute_in(
            self._client,
            conn,
            f"UPDATE {APPROVALS} SET status = %s, consumed_at = %s, consumed_by = %s "
            "WHERE approval_id = %s AND status = %s",
            (
                ApprovalStatus.CONSUMED.value,
                _to_db(consumed_at),
                consumed_by,
                approval_id,
                ApprovalStatus.APPROVED.value,
            ),
        )

    def get_consumption(self, approval_id: str) -> dict[str, Any] | None:
        rows = self._client.fetch_all(
            f"SELECT * FROM {CONSUMPTIONS} WHERE approval_id = %s LIMIT 1", (approval_id,)
        )
        return dict(rows[0]) if rows else None

    # ---- 映射 ----------------------------------------------------------
    @staticmethod
    def _to_approval(row: Mapping[str, Any]) -> Approval:
        created_at = _from_db(row.get("created_at"))
        expires_at = _from_db(row.get("expires_at"))
        if created_at is None or expires_at is None:
            # 绝不用 now 兜底：缺失/损坏的时间列不得被解释成"永不过期"或"立即可用"。
            logger.error(
                "审批时间列缺失或损坏，拒绝映射",
                extra={
                    "alert": "APPROVAL_EVIDENCE_CORRUPT",
                    "approval_id": str(row.get("approval_id") or ""),
                },
            )
            raise EvidenceInvalidError(
                "审批时间列缺失或损坏，拒绝映射",
                details={"approval_id": str(row.get("approval_id") or "")},
            )
        prerequisites_raw = _json_load(
            row.get("prerequisites_json"), [], column="prerequisites_json"
        )
        checks: list[PrerequisiteCheck] = []
        if isinstance(prerequisites_raw, list):
            for item in prerequisites_raw:
                if isinstance(item, dict):
                    checks.append(PrerequisiteCheck.model_validate(item))
        # W07：披露快照列的读取必须**先 json.loads 再 canonical_digest**
        # （MySQL JSON 列在驱动侧可能以 str 返回，直接喂给摘要函数会算错）。
        try:
            disclosure = _json_load(
                row.get("disclosure_json"), {}, column="disclosure_json"
            )
        except EvidenceInvalidError:
            raise StateConflictError(
                "审批披露快照不可解析，拒绝映射",
                details={
                    "reason": DISCLOSURE_TAMPERED,
                    "cause": "DISCLOSURE_NOT_JSON",
                    "approval_id": str(row.get("approval_id") or ""),
                },
            ) from None
        if not isinstance(disclosure, dict):
            raise StateConflictError(
                "审批披露快照结构非法，拒绝映射",
                details={
                    "reason": DISCLOSURE_TAMPERED,
                    "cause": "DISCLOSURE_NOT_OBJECT",
                    "approval_id": str(row.get("approval_id") or ""),
                },
            )
        raw_digest = row.get("disclosure_digest")
        disclosure_digest_column = str(raw_digest) if raw_digest is not None else None
        raw_version = row.get("disclosure_version")
        disclosure_version = str(raw_version) if raw_version is not None else None
        approval = Approval(
            approval_id=str(row["approval_id"]),
            tenant_id=str(row["tenant_id"]),
            task_id=str(row["task_id"]),
            step_id=row.get("step_id"),
            environment_id=str(row["environment_id"]),
            db_engine=str(row["db_engine"]),  # type: ignore[arg-type]
            environment_fingerprint=_json_load(row.get("environment_fingerprint_json"), {}),
            environment_fingerprint_digest=str(row["environment_fingerprint"]),
            action_digest=str(row["action_digest"]),
            parameters_digest=str(row["parameters_digest"]),
            target_objects_digest=str(row["target_objects_digest"]),
            prerequisites_digest=str(row["prerequisites_digest"]),
            prerequisites=checks,
            action_descriptor=_json_load(row.get("action_descriptor_json"), {}),
            policy_version=str(row["policy_version"]),
            tool_name=str(row["tool_name"]),
            tool_version=str(row["tool_version"]),
            requested_by=str(row["requested_by"]),
            requested_session_id=row.get("requested_session_id"),
            approver_id=row.get("approver_id"),
            approver_session_id=row.get("approver_session_id"),
            authn_context=_json_load(row.get("authn_context_json"), {}),
            nonce_hash=str(row["nonce_hash"]),
            status=ApprovalStatus(str(row["status"])),
            decision_reason=row.get("decision_reason"),
            operator_note=row.get("operator_note"),
            created_at=_from_db(row.get("created_at")) or datetime.now(timezone.utc),
            expires_at=_from_db(row.get("expires_at")) or datetime.now(timezone.utc),
            decided_at=_from_db(row.get("decided_at")),
            consumed_at=_from_db(row.get("consumed_at")),
            consumed_by=row.get("consumed_by"),
            revoked_at=_from_db(row.get("revoked_at")),
            revoked_by=row.get("revoked_by"),
            disclosure=disclosure,
            disclosure_digest=disclosure_digest_column,
            disclosure_version=disclosure_version,
            disclosed_at=_from_db(row.get("disclosed_at")),
        )
        # L2 自校验（**唯一**读取入口：get/list/approve/reject/revoke/consume 全经此处）：
        # 库内 JSON 必须能复算出 `disclosure_digest` 列。历史行（无摘要且无快照）放行，
        # 由 `missing_required_fields()` 在批准时以 DISCLOSURE_INCOMPLETE 阻断。
        if disclosure_digest_column is not None or disclosure:
            verify_disclosure(disclosure, disclosure_digest_column)
        return approval


def duplicate_key(error: DatabaseQueryError | pymysql.MySQLError) -> bool:
    """判断是否为唯一键冲突（MySQL 1062）。"""
    if isinstance(error, DatabaseQueryError):
        return error.details.get("mysql_error_code") == 1062
    return bool(error.args and error.args[0] == 1062)


__all__ = [
    "APPROVALS",
    "CONSUMPTIONS",
    "DISCLOSURE_COLUMNS",
    "ApprovalRepository",
    "duplicate_key",
]
