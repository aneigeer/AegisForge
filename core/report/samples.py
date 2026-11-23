"""W09 T06 训练样本出口 + 版本化脱敏回归样本集（AC-07 训练样本分支 / AC-19）。

两条**互不耦合**的职责放在同一模块，因为它们共享同一份"五类回归样本"的口径：

1. `REGRESSION_SAMPLES`：版本化的五类回归样本（多语言 / SQL 注释 / ``\\`` 转义 /
   连接串 / 嵌套 JSON），每类一条，带 `raw_secret` 与 `expected_categories`。
   样本集版本 `SAMPLE_SET_VERSION` 随词表口径变化升版；运行时 `redaction_version`
   必须等于 `DataRedactionEngine.version`（由测试断言，模块本身不硬编码版本号）。

2. `build_training_sample()`：训练样本**出口**。把轨迹与证据**拼接成完整文档后**
   再走一次 `TRAINING_SAMPLE` 通道（`RedactionGate.for_document`），因此"拼接过程
   引入的新串"同样会被判定——AC-07 要求出口复扫，不是逐片段扫。

不落盘口径（`03-task-cards.md:657`）：

- 本模块是**纯函数**，不持有任何仓储/文件句柄/连接；"不落盘"由结构保证。
- `BLOCKED` / `REVIEW_REQUIRED` 时返回 `SkippedSample`（只有原因、类别、摘要），
  调用方拿不到任何可写样本；`SkippedSample` 也**没有**承载载荷的字段，杜绝
  "先返回再顺手写盘"的旁路。
- 证据侧只取 `redacted_payload`（闸门②产物），**绝不**读 `payload` 原文。

KD-W09-03 如实登记（唯一对外口径来源：``docs/w09/07-pm-ruling.md`` §4.3 / §4.4）：
本卡按 §4.4 第 2 条，训练样本走 ``TRAINING_SAMPLE`` 通道独立复扫；**不把**
``StepPrepared.request`` / ``TrajectoryRecorder`` 等内部通道原文当作对外通道输入。
本卡不改 ``redact_payload``、不升 ``redaction_version``（保持 ``redaction-v2``）、
不补冻结回归、不新增 AC；引用 ``docs/w09/06-qa-report.md`` §11.5 时必须并列引用
``docs/w09/07-pm-ruling.md`` §4。该发现项定性为**已声明的已知限制，不是本轮 FAIL**，
W10 再议；对外不得写「脱敏全链路无死角 / PII 零落盘」。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.errors import RedactionBlockedError
from core.redaction.models import RedactionChannel, RedactionStatus, SensitiveCategory
from core.redaction.payload_text import canonical_payload_hash

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与闸门模块循环导入
    from core.agent.trajectory import Trajectory
    from core.evidence.models import Evidence
    from core.redaction.gate import RedactionGate

# 样本集版本：与 `redaction_version` 不同轴——它标识"样本清单"这一版，
# 词表升版后需要重新核对样本期望值时才升它。
SAMPLE_SET_VERSION = "w09-redaction-samples-v1"

# 训练样本结构版本：样本头里的 `sample_version`，供下游消费方做兼容判断。
TRAINING_SAMPLE_VERSION = "training-sample-v1"

# `SkippedSample.reason` 的稳定取值（不得写成自由文本）。
REASON_REDACTION_BLOCKED = "REDACTION_BLOCKED"
REASON_REVIEW_REQUIRED = "REDACTION_REVIEW_REQUIRED"

# 训练样本里最多保留的轨迹事件数：样本是"示范"不是全量回放，超限按**尾部**截断
# （尾部事件最接近结论），并在样本头显式记 `events_truncated`，不静默丢。
MAX_SAMPLE_EVENTS = 200


class SkippedSample(BaseModel):
    """被出口闸门拦下的样本；**没有任何字段可以承载载荷本体**。

    `source_digest` 是出口扫描的**原文哈希**（不是原文），供事后核对"哪一份样本
    被拦了"，且不可逆。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    detected_categories: tuple[str, ...] = ()
    source_digest: str


class RegressionSample(BaseModel):
    """一条版本化回归样本：原文载荷 + 其中的敏感串 + 期望类别。

    `payload` 是**待扫描的原始载荷**（故意含敏感内容），不是脱敏结果。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    payload: dict[str, Any]
    raw_secret: str
    expected_categories: tuple[SensitiveCategory, ...] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 五类回归样本（AC-19）：每类 1 例，逐例三断言（原文含 / 脱敏后不含 / 类别正确）
# ---------------------------------------------------------------------------

# 第三类样本刻意只用**字面量反斜杠**（`\\`），不掺双引号：一旦规则漏检转义内容，
# 该样本会直接失败，而不是被 R01 顺带兜住。
_ESCAPED_TEXT = "path=C:\\db\\dump\\orders.sql phone=13812345678"

REGRESSION_SAMPLES: tuple[RegressionSample, ...] = (
    RegressionSample(
        name="multilingual",
        description="中/日/英混排文本里的手机号与邮箱（多语言不得绕过匹配）",
        payload={
            "note": "客户 张伟 电话 13812345678 / 顧客 alice@example.com / customer",
            "duration_ms": 3.5,
        },
        raw_secret="13812345678",
        expected_categories=(SensitiveCategory.PHONE, SensitiveCategory.EMAIL),
    ),
    RegressionSample(
        name="sql_comment",
        description="SQL 行内注释与字面量同时出现在一条语句里（注释内容同样要脱敏）",
        payload={
            "query": (
                "SELECT * FROM orders -- contact 13812345678 "
                "WHERE note = 'abcdefghijklmnop'"
            )
        },
        raw_secret="13812345678",
        expected_categories=(SensitiveCategory.SQL_LITERAL, SensitiveCategory.PHONE),
    ),
    RegressionSample(
        name="backslash_escape",
        description="含反斜杠转义路径的日志行里的手机号（转义不得导致漏检）",
        payload={"line": _ESCAPED_TEXT},
        raw_secret="13812345678",
        expected_categories=(SensitiveCategory.PHONE,),
    ),
    RegressionSample(
        name="connection_string",
        description="连接串（不可安全改写 → BLOCKED，占位符替换原串）",
        payload={"target": "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab"},
        raw_secret="mysql://dg_ro:pw@127.0.0.1:3306/dg_lab",
        expected_categories=(SensitiveCategory.CONNECTION_STRING,),
    ),
    RegressionSample(
        name="nested_json",
        description="深层嵌套 JSON（数组套对象）里的邮箱与手机号",
        payload={
            "outer": {
                "middle": {
                    "inner": {
                        "events": [
                            {"actor": "alice@example.com", "note": "ok"},
                            {"actor": "bob", "note": "13812345678"},
                        ]
                    }
                }
            }
        },
        raw_secret="alice@example.com",
        expected_categories=(SensitiveCategory.PHONE, SensitiveCategory.EMAIL),
    ),
)


# ---------------------------------------------------------------------------
# 训练样本出口
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """枚举/None 安全的字符串化，避免把 `None` 写成字面量 `'None'` 进样本。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _evidence_entry(evidence: "Evidence") -> dict[str, Any]:
    """证据侧只取**已脱敏变体**与结构化元数据，不读 `payload` 原文。"""
    redacted = getattr(evidence, "redacted_payload", None)
    return {
        "evidence_id": _text(getattr(evidence, "evidence_id", None)),
        "source_tool": _text(getattr(evidence, "source_tool", None)),
        "collection_status": _text(getattr(evidence, "collection_status", None)),
        "quality": _text(getattr(evidence, "quality", None)),
        "completeness": _text(getattr(evidence, "completeness", None)),
        "payload_hash": _text(getattr(evidence, "payload_hash", None)),
        "source_payload_hash": getattr(evidence, "source_payload_hash", None),
        "redaction_version": _text(getattr(evidence, "redaction_version", None)),
        "redacted_payload": dict(redacted) if isinstance(redacted, dict) else {},
    }


def _event_entry(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    return {
        "sequence": int(getattr(event, "sequence", 0) or 0),
        "kind": _text(getattr(event, "kind", None)),
        "payload": dict(payload) if isinstance(payload, dict) else {},
    }


def _tenant_of(evidences: Sequence["Evidence"]) -> str | None:
    """租户取自证据行（服务端事实），缺失时留 `None` 由闸门决定是否写隔离区。"""
    for evidence in evidences:
        tenant = getattr(evidence, "tenant_id", None)
        if isinstance(tenant, str) and tenant.strip():
            return tenant.strip()
    return None


def _assemble(
    *,
    trajectory: "Trajectory",
    evidences: Sequence["Evidence"],
) -> dict[str, Any]:
    """拼接完整样本文档（**先拼接、后复扫**）。

    刻意把 `goal`（模型产出的自然语言）与证据的 `redacted_payload` 一起拼进来：
    单片段合规不代表拼接后合规（相邻片段可能拼出新的连接串/邮箱）。
    """
    raw_events = list(getattr(trajectory, "events", None) or ())
    truncated = len(raw_events) > MAX_SAMPLE_EVENTS
    kept_events = raw_events[-MAX_SAMPLE_EVENTS:] if truncated else raw_events
    usage = getattr(trajectory, "usage", None)
    evidence_entries = [_evidence_entry(item) for item in evidences]
    return {
        "sample_version": TRAINING_SAMPLE_VERSION,
        "sample_set_version": SAMPLE_SET_VERSION,
        "header": {
            "task_id": _text(getattr(trajectory, "task_id", None)),
            "run_id": _text(getattr(trajectory, "run_id", None)),
            "trajectory_id": _text(getattr(trajectory, "trajectory_id", None)),
            "model_version": _text(getattr(trajectory, "model_version", None)),
            "stop_reason": _text(getattr(trajectory, "stop_reason", None)),
            "started_at": _text(getattr(trajectory, "started_at", None)),
            "finished_at": _text(getattr(trajectory, "finished_at", None)),
            "goal": _text(getattr(trajectory, "goal", None)),
            "evidence_count": len(evidence_entries),
            "events_total": len(raw_events),
            "events_truncated": truncated,
        },
        "trajectory": {
            "usage": dict(usage) if isinstance(usage, dict) else {},
            "events": [_event_entry(item) for item in kept_events],
        },
        "evidence": evidence_entries,
    }


def build_training_sample(
    *,
    trajectory: "Trajectory",
    evidences: Sequence["Evidence"],
    gate: "RedactionGate",
) -> dict[str, Any] | SkippedSample:
    """拼接后复扫训练样本；只有 `CLEAN` / `REDACTED` 才产出样本。

    Args:
        trajectory: 轨迹（可能含模型自由文本；不得假设它已干净）。
        evidences: 证据行序列（只读取 `redacted_payload`）。
        gate: 脱敏闸门（`TRAINING_SAMPLE` 属文档类通道，走 `for_document`）。

    Returns:
        `CLEAN` / `REDACTED`：样本字典，`header` 含 `redaction_version` 与
        `source_payload_hash`（出口原文锚点），以及扫描得到的判定摘要。
        `BLOCKED` / `REVIEW_REQUIRED`：`SkippedSample`（**不可写**，无载荷字段）。

    Raises:
        不会为脱敏判定抛错：`RedactionBlockedError` 在此被转成 `SkippedSample`。
        闸门自身的审计写入失败（`AuditUnavailableError`）仍然向上传播——审计不可写
        属"必须成功"语义，不得用一句"样本跳过"掩盖。
    """
    assembled = _assemble(trajectory=trajectory, evidences=evidences)
    task_id = _text(getattr(trajectory, "task_id", None))
    try:
        document, scan = gate.for_document(
            assembled,
            channel=RedactionChannel.TRAINING_SAMPLE,
            task_id=task_id,
            tenant_id=_tenant_of(evidences),
        )
    except RedactionBlockedError as exc:
        # 闸门已发 `REDACTION_BLOCKED` 事件并按类别抹除原串；这里只登记跳过原因。
        details = exc.details if isinstance(exc.details, dict) else {}
        raw_categories = details.get("detected_categories") or ()
        return SkippedSample(
            reason=REASON_REDACTION_BLOCKED,
            detected_categories=tuple(str(item) for item in raw_categories),
            source_digest=str(
                details.get("source_payload_hash") or canonical_payload_hash(assembled)
            ),
        )

    if scan.status is RedactionStatus.REVIEW_REQUIRED:
        # 复核态文档不对外交付（设计 §3 第 5 行）：连样本头都不返回。
        return SkippedSample(
            reason=REASON_REVIEW_REQUIRED,
            detected_categories=tuple(item.value for item in scan.detected_categories),
            source_digest=scan.payload_hash,
        )

    header = document.get("header")
    sample = dict(document)
    sample["header"] = {
        **(dict(header) if isinstance(header, dict) else {}),
        # 出口扫描的权威判定必须盖在样本头上：消费方据此证明"这份样本被复扫过"。
        "redaction_version": scan.redaction_version,
        "source_payload_hash": scan.payload_hash,
        "redaction_status": scan.status.value,
        "redaction_count": scan.redaction_count,
        "detected_categories": [item.value for item in scan.detected_categories],
    }
    return sample


__all__ = [
    "MAX_SAMPLE_EVENTS",
    "REASON_REDACTION_BLOCKED",
    "REASON_REVIEW_REQUIRED",
    "REGRESSION_SAMPLES",
    "SAMPLE_SET_VERSION",
    "TRAINING_SAMPLE_VERSION",
    "RegressionSample",
    "SkippedSample",
    "build_training_sample",
]
