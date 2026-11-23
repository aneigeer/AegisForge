"""W10 T05：最小 ``AppTelemetryAdapter`` + Evidence 字段（AC-11 / AC-12 / AC-13）。

口径来源：``docs/w10/02-design.md`` §10 / §4.4；任务卡 ``docs/w10/03-task-cards.md`` T05；
主理人裁定 W10-Q4（人工导入快照优先，不新起 exporter、不新增依赖）。

硬约束：

1. **恰好 6 个逻辑键**（``TELEMETRY_FIELD_ALLOWLIST``）；``pool_connections``
   是 ``{active, idle}`` 复合键。禁用字段一律剔除并记入 ``dropped_fields``，
   **不进入 payload、不进入 Evidence**。
2. **缺失不冒充**：``raw is None`` → ``collection_status=UNKNOWN`` +
   ``message="应用池状态未确认"`` + 六项指标全 ``None``；绝不填 0 / "健康" 默认值。
3. **source 闭集** ``{hikaricp, pgbouncer}``；用 DB 连接数冒充池指标
   （``mysql_server`` / ``pg_stat_activity`` 等）→ ``ValidationError``。
4. **窗口时序**：``window_start < window_end <= sampled_at``；
   ``collection_status=UNKNOWN`` 时 ``quality`` 不得为 ``HIGH``
   （否则 ``EvidenceService.create`` 会拒）。
5. **不越过 W09 闸门**：本模块只产出 ``EvidenceCreate``；落库必经
   ``EvidenceService.create`` 内部的 ``for_evidence``（闸门②），
   **禁止直接 INSERT evidence**、禁止改 ``core/evidence/models.py``。
6. **无采集面**：不新起 exporter / 后台线程 / 定时任务，不引入
   ``prometheus_client`` / ``psutil`` / ``apscheduler`` 等 SDK。

依赖方向（设计 §2.2）：只依赖 ``core*``；**不** import ``apps/**``、
``workers/**``、``core/gateway/**``、``core/approval/**``、``core/agent/**``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.errors import BenchmarkInvalidError
from core.evidence.models import (
    CollectionStatus,
    EvidenceCompleteness,
    EvidenceCreate,
    EvidenceQuality,
    WEAK_COLLECTION_STATUSES,
)
from core.security.canonical import canonical_digest
from core.tasks.models import Task

# 采集白名单：**恰好 6 个逻辑键**（设计 §10.1）。pool_connections 为复合键。
TELEMETRY_FIELD_ALLOWLIST: tuple[str, ...] = (
    "pool_connections",
    "pending_threads",
    "acquire_timeout_total",
    "acquire_latency_p95_ms",
    "pool_max_size",
    "application_error_rate",
)

# 白名单外一律剔除；下列为明确的业务请求正文 / 用户数据 / 凭据类禁用字段。
TELEMETRY_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "request_body",
    "user_id",
    "sql",
    "query_text",
    "session_user",
    "authorization",
    "cookie",
    "payload",
    "dsn",
    "password",
)

MISSING_POOL_MESSAGE: str = "应用池状态未确认"

# 应用池身份证明：闭集（PRD AC-12②）。用数据库连接数冒充池指标即被拒。
POOL_SOURCES: frozenset[str] = frozenset({"hikaricp", "pgbouncer"})
APPLICATION_POOL_KIND: Literal["application_pool"] = "application_pool"

# 快照 manifest 字段（P1-W10-02 冻结字段，设计 §4.4）。
SNAPSHOT_VERSION: Literal["w10-snapshot-v1"] = "w10-snapshot-v1"

# raw 中允许保留的键：6 个采集键 + 6 个来源/时间窗元数据键。
_METADATA_KEYS: tuple[str, ...] = (
    "source",
    "source_kind",
    "sampled_at",
    "window_start",
    "window_end",
    "exporter_version",
)
_ALLOWED_RAW_KEYS: frozenset[str] = frozenset(
    (*TELEMETRY_FIELD_ALLOWLIST, *_METADATA_KEYS)
)

# 无快照时的结构占位值：它不是「指标」，也不冒充观测时间或 exporter 版本。
_MISSING_SENTINEL: str = "UNKNOWN"

# 适配器声明的默认目标来源（仅用于生成 source_tool 标识，不代表已观测到该池）。
_DEFAULT_SOURCE: Literal["hikaricp"] = "hikaricp"

# 应用池不是数据库：db_version / db_engine 一律 "n/a"，不填数据库版本冒充。
_NOT_A_DATABASE: str = "n/a"
_SOURCE_TOOL_PREFIX: str = "app_telemetry"


class PoolConnections(BaseModel):
    """复合键 ``pool_connections = {active:int, idle:int}``（设计 §10.1 AC-11①）。"""

    model_config = ConfigDict(extra="forbid")

    active: int = Field(ge=0)
    idle: int = Field(ge=0)


class TelemetrySnapshot(BaseModel):
    """一次人工导入快照的解析结果；``extra="forbid"``。

    ``source: Literal["hikaricp","pgbouncer"]`` 是**闭集**：任何 DB 连接数
    冒充（``mysql_server`` / ``pg_stat_activity`` / ``database_server`` 等）
    都会由 pydantic 抛 ``ValidationError``（AC-12②）。
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["hikaricp", "pgbouncer"]
    source_kind: Literal["application_pool"] = APPLICATION_POOL_KIND
    sampled_at: str = Field(min_length=1)
    window_start: str = Field(min_length=1)
    window_end: str = Field(min_length=1)
    exporter_version: str = Field(min_length=1)
    # 6 个逻辑键：真值缺席时全 None。
    pool_connections: PoolConnections | None = None
    pending_threads: int | None = None
    acquire_timeout_total: int | None = None
    acquire_latency_p95_ms: float | None = None
    pool_max_size: int | None = None
    application_error_rate: float | None = None
    collection_status: CollectionStatus = CollectionStatus.UNKNOWN
    dropped_fields: tuple[str, ...] = ()
    message: str | None = None

    @model_validator(mode="after")
    def _window_order(self) -> "TelemetrySnapshot":
        """真实快照必须满足 ``window_start < window_end <= sampled_at``。

        无快照（``UNKNOWN``）时不存在观测窗口，跳过校验，且**不伪造**时间。
        """

        if self.collection_status is CollectionStatus.UNKNOWN:
            return self
        assert_window_order(self.window_start, self.window_end, self.sampled_at)
        return self

    def metric_payload(self) -> dict[str, Any]:
        """返回**恰好 6 个逻辑键**的采集字段（AC-11① 的断言面）。

        ``pool_connections`` 以普通 ``{active, idle}`` 字典返回，便于直接作为
        Evidence payload 的一部分并保证 JSON 可序列化。
        """

        pool = (
            self.pool_connections.model_dump(mode="python")
            if self.pool_connections is not None
            else None
        )
        return {
            "pool_connections": pool,
            "pending_threads": self.pending_threads,
            "acquire_timeout_total": self.acquire_timeout_total,
            "acquire_latency_p95_ms": self.acquire_latency_p95_ms,
            "pool_max_size": self.pool_max_size,
            "application_error_rate": self.application_error_rate,
        }

    @property
    def metric_fields(self) -> tuple[str, ...]:
        """采集字段名称集合（恒等于 ``TELEMETRY_FIELD_ALLOWLIST``）。"""

        return TELEMETRY_FIELD_ALLOWLIST


class SnapshotManifest(BaseModel):
    """P1-W10-02：快照 manifest 冻结字段（设计 §4.4）；``extra="forbid"``。

    ``snapshot_digest`` 复用 ``canonical_digest`` 对全部冻结字段计算，
    模型校验器会核对一致性 —— 改任一字段而不升 digest 即 ``ValidationError``。
    """

    model_config = ConfigDict(extra="forbid")

    # 全部必填（无默认值）：缺任一字段即 ValidationError（P1-W10-02 / 卡内判据）。
    snapshot_version: Literal["w10-snapshot-v1"]
    source: Literal["hikaricp", "pgbouncer"]
    source_kind: Literal["application_pool"]
    sampled_at: str = Field(min_length=1)
    window_start: str = Field(min_length=1)
    window_end: str = Field(min_length=1)
    exporter_version: str = Field(min_length=1)
    snapshot_digest: str = Field(min_length=1)
    notes: tuple[str, ...]

    def digest_payload(self) -> dict[str, Any]:
        """参与 ``canonical_digest`` 的冻结字段（含 ``notes``）。"""

        return {
            "snapshot_version": self.snapshot_version,
            "source": self.source,
            "source_kind": self.source_kind,
            "sampled_at": self.sampled_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "exporter_version": self.exporter_version,
            "notes": list(self.notes),
        }

    @model_validator(mode="after")
    def _frozen_fields_consistency(self) -> "SnapshotManifest":
        """窗口时序 + digest 一致性；任一不符 → ``ValidationError``。"""

        assert_window_order(self.window_start, self.window_end, self.sampled_at)
        expected = canonical_digest(self.digest_payload())
        if self.snapshot_digest != expected:
            raise ValueError("snapshot_digest 与冻结字段不一致")
        return self


def _parse_iso(value: str) -> datetime:
    """ISO-8601 解析（兼容 ``Z`` 结尾）；非法值向上抛 ``ValueError``。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def assert_window_order(window_start: str, window_end: str, sampled_at: str) -> None:
    """``window_start < window_end <= sampled_at``（设计 §10.3 AC-13）。

    Raises:
        ValueError: 时间格式非法或时序不成立（由 pydantic 包成 ValidationError）。
    """

    try:
        start = _parse_iso(window_start)
        end = _parse_iso(window_end)
        sampled = _parse_iso(sampled_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("观测窗口时间必须是合法 ISO-8601 字符串") from exc
    if not (start < end <= sampled):
        raise ValueError("必须满足 window_start < window_end <= sampled_at")


def compute_snapshot_digest(payload: Mapping[str, Any]) -> str:
    """从 manifest 字段计算 ``snapshot_digest``（复用 ``canonical_digest``）。"""

    return canonical_digest(
        {
            "snapshot_version": payload["snapshot_version"],
            "source": payload["source"],
            "source_kind": payload["source_kind"],
            "sampled_at": payload["sampled_at"],
            "window_start": payload["window_start"],
            "window_end": payload["window_end"],
            "exporter_version": payload["exporter_version"],
            "notes": list(payload["notes"]),
        }
    )


class AppTelemetryAdapter:
    """最小应用观测适配器：只读解析人工导入快照 → ``EvidenceCreate``。

    ``gate`` 仅用于单测/组合根显式复扫（``for_evidence``）；本类不落库、不采集、
    不启动任何后台线程。落库由 ``EvidenceService.create`` 承担（闸门②）。
    """

    def __init__(self, *, gate: Any | None = None) -> None:
        self._gate = gate

    @property
    def gate(self) -> Any | None:
        """组合根注入的可选脱敏闸门（只读）。"""

        return self._gate

    def parse(self, raw: Mapping[str, Any] | None) -> TelemetrySnapshot:
        """解析原始快照；禁用字段剔除并记入 ``dropped_fields``（AC-11 / AC-12）。

        Args:
            raw: 人工导入的快照映射；``None`` 表示应用池状态未确认。

        Returns:
            ``TelemetrySnapshot``。``raw is None`` 时六项指标全 ``None``、
            ``collection_status=UNKNOWN``、``message=MISSING_POOL_MESSAGE``。

        Raises:
            ValidationError: ``source`` / ``source_kind`` 越出闭集，或字段类型非法。
        """

        if raw is None:
            return TelemetrySnapshot(
                source=_DEFAULT_SOURCE,
                source_kind=APPLICATION_POOL_KIND,
                sampled_at=_MISSING_SENTINEL,
                window_start=_MISSING_SENTINEL,
                window_end=_MISSING_SENTINEL,
                exporter_version=_MISSING_SENTINEL,
                pool_connections=None,
                pending_threads=None,
                acquire_timeout_total=None,
                acquire_latency_p95_ms=None,
                pool_max_size=None,
                application_error_rate=None,
                collection_status=CollectionStatus.UNKNOWN,
                dropped_fields=(),
                message=MISSING_POOL_MESSAGE,
            )

        # 白名单外一律剔除（含 10 个禁用字段），并如实记录被剔除的键。
        dropped = tuple(
            sorted(str(key) for key in raw if key not in _ALLOWED_RAW_KEYS)
        )
        payload: dict[str, Any] = {
            key: raw[key] for key in _ALLOWED_RAW_KEYS if key in raw
        }
        payload.setdefault("source_kind", APPLICATION_POOL_KIND)

        metric_values = [
            payload.get(key) for key in TELEMETRY_FIELD_ALLOWLIST
        ]
        all_present = all(value is not None for value in metric_values)
        status = (
            CollectionStatus.SUCCESS if all_present else CollectionStatus.PARTIAL
        )
        payload["collection_status"] = status
        payload["dropped_fields"] = dropped
        payload["message"] = None
        return TelemetrySnapshot.model_validate(payload)

    def to_evidence_request(
        self,
        snapshot: TelemetrySnapshot,
        *,
        task: Task,
        environment_id: str,
    ) -> EvidenceCreate:
        """把快照映射为 ``EvidenceCreate``（设计 §10.3，AC-13）。

        ``adapter_version`` 取 ``exporter_version``；``db_version="n/a"``；
        ``source_tool="app_telemetry:<source>"``；时间窗取快照三时间。
        ``collection_status=UNKNOWN`` 时 ``quality`` 恒 ``UNKNOWN``（**不得 HIGH**）。

        Raises:
            BenchmarkInvalidError: 窗口时序不成立（防御性；正常快照在 parse 已校验）。
        """

        if snapshot.collection_status is not CollectionStatus.UNKNOWN:
            try:
                assert_window_order(
                    snapshot.window_start, snapshot.window_end, snapshot.sampled_at
                )
            except ValueError as exc:
                raise BenchmarkInvalidError(
                    "遥测快照时间窗不合法", details={"reason": "WINDOW_ORDER_INVALID"}
                ) from exc

        weak = snapshot.collection_status in WEAK_COLLECTION_STATUSES
        quality = (
            EvidenceQuality.UNKNOWN if weak else EvidenceQuality.MEDIUM
        )
        if snapshot.collection_status is CollectionStatus.UNKNOWN:
            completeness = EvidenceCompleteness.MISSING
        elif weak:
            completeness = EvidenceCompleteness.UNKNOWN
        else:
            completeness = EvidenceCompleteness.COMPLETE
        # 双重守卫：UNKNOWN 采集状态绝不与 HIGH 质量组合（EvidenceService 会拒）。
        if quality is EvidenceQuality.HIGH:
            raise BenchmarkInvalidError(
                "UNKNOWN 采集状态不得标记 HIGH 质量",
                details={"collection_status": snapshot.collection_status.value},
            )

        evidence_payload: dict[str, Any] = {
            **snapshot.metric_payload(),
            # 设计 §10.3：payload 只有 6 个逻辑键 + source/source_kind（身份键）。
            "source": snapshot.source,
            "source_kind": snapshot.source_kind,
        }
        return EvidenceCreate(
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            environment_id=environment_id,
            db_engine=_NOT_A_DATABASE,
            db_version=_NOT_A_DATABASE,
            adapter_version=snapshot.exporter_version,
            source_tool=f"{_SOURCE_TOOL_PREFIX}:{snapshot.source}",
            observed_at=snapshot.sampled_at,
            time_window_start=snapshot.window_start,
            time_window_end=snapshot.window_end,
            collection_status=snapshot.collection_status,
            quality=quality,
            completeness=completeness,
            payload=evidence_payload,
            redacted_payload=None,
            source_payload_hash=None,
            redaction_version="none",
            reproducible=False,
        )


__all__ = [
    "APPLICATION_POOL_KIND",
    "AppTelemetryAdapter",
    "MISSING_POOL_MESSAGE",
    "POOL_SOURCES",
    "PoolConnections",
    "SNAPSHOT_VERSION",
    "SnapshotManifest",
    "TELEMETRY_FIELD_ALLOWLIST",
    "TELEMETRY_FORBIDDEN_FIELDS",
    "TelemetrySnapshot",
    "assert_window_order",
    "compute_snapshot_digest",
]
