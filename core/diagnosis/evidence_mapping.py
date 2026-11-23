"""W04 ToolResult → EvidenceCreate 映射。

约束：
- 证据归属（tenant/task/environment）由调用方显式传入，服务端仍会二次校验；
- 采集状态、质量与完整性必须反映工具真实结果，不得把降级结果标记为 HIGH；
- **真脱敏**（W09 T02）：`payload` 与 `redacted_payload` 都必须由脱敏引擎产出，
  `source_payload_hash` 取引擎给出的原文哈希，`redaction_version` 取引擎版本；
  历史上"把原文 `deepcopy` 进 `redacted_payload`、版本号硬编码 `tool-redaction-v1`"
  的声明式假脱敏已删除（`docs/w09/02-design.md` §1.1 第 3 行）；
- 时间窗来自工具真实 started_at/observed_at。

历史行**不改写**：本函数只影响新产出的 `EvidenceCreate`，不 UPDATE、不回填、
不重算任何既有哈希（R4-A / R4-D）。
"""

from __future__ import annotations

from typing import Any

from core.evidence.models import (
    CollectionStatus,
    EvidenceCompleteness,
    EvidenceCreate,
    EvidenceQuality,
)
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.rules import RS_REDACTION_V2
from core.tools.contracts import ToolResult, ToolStatus

# 当前默认规则集版本（引擎 `DataRedactionEngine()` 的 `ruleset_version` 缺省值）。
# 历史行的 `tool-redaction-v1` 只读不写，不得用于任何新产出（R4-A / R4-D）。
REDACTION_VERSION = RS_REDACTION_V2

# 引擎无状态（不持 DSN、不连库），模块级复用一个实例即可。
_REDACTION_ENGINE = DataRedactionEngine()

_STATUS_MAP: dict[ToolStatus, CollectionStatus] = {
    ToolStatus.SUCCESS: CollectionStatus.SUCCESS,
    ToolStatus.PARTIAL: CollectionStatus.PARTIAL,
    ToolStatus.DENIED: CollectionStatus.DENIED,
    ToolStatus.TIMEOUT: CollectionStatus.TIMEOUT,
    ToolStatus.NOT_SUPPORTED: CollectionStatus.NOT_SUPPORTED,
    ToolStatus.ERROR: CollectionStatus.UNKNOWN,
    ToolStatus.UNKNOWN_OUTCOME: CollectionStatus.UNKNOWN,
}


def _has_observation_window(result: ToolResult) -> bool:
    """采集开始与完成时间均存在时，才视为有明确观测窗口。"""

    return bool(result.started_at) and bool(result.observed_at)


def _quality(result: ToolResult, status: CollectionStatus) -> EvidenceQuality:
    if status is CollectionStatus.SUCCESS:
        if result.truncated:
            return EvidenceQuality.MEDIUM
        return (
            EvidenceQuality.HIGH
            if _has_observation_window(result)
            else EvidenceQuality.MEDIUM
        )
    if status is CollectionStatus.PARTIAL:
        return EvidenceQuality.LOW if result.data else EvidenceQuality.UNKNOWN
    return EvidenceQuality.UNKNOWN


def _completeness(
    result: ToolResult, status: CollectionStatus
) -> EvidenceCompleteness:
    if result.truncated:
        return EvidenceCompleteness.PARTIAL
    if status is CollectionStatus.SUCCESS:
        return EvidenceCompleteness.COMPLETE
    if status is CollectionStatus.PARTIAL:
        return (
            EvidenceCompleteness.PARTIAL
            if result.data
            else EvidenceCompleteness.MISSING
        )
    if status is CollectionStatus.NOT_SUPPORTED:
        return EvidenceCompleteness.MISSING
    return EvidenceCompleteness.UNKNOWN


def _payload(result: ToolResult) -> dict[str, Any]:
    return {
        "tool_status": result.status.value,
        "data": result.data,
        "warnings": list(result.warnings),
        "capability_ref": result.capability_ref,
        "source_metadata": dict(result.source_metadata),
        "truncated": result.truncated,
        "error": result.error.model_dump(mode="json") if result.error else None,
    }


def to_evidence_create(
    result: ToolResult,
    *,
    tenant_id: str,
    task_id: str,
    environment_id: str,
    step_id: str | None = None,
    gate: RedactionGate | None = None,
) -> EvidenceCreate:
    """把一次工具结果转为证据写入请求；不改变工具数据，也不补造缺失字段。

    Args:
        result: 工具结果（已过闸门①的 `data` 即脱敏后内容）。
        tenant_id: 租户（隔离区归属与审计必填）。
        task_id: 任务（审计必填）。
        environment_id: 环境（服务端仍会二次校验）。
        step_id: 可选步骤归属。
        gate: 可选闸门（组合根注入 audit/quarantine）；`None` 时就地构造，
            因此既有调用点无需改动，闸门**永远生效**。

    Raises:
        RedactionBlockedError: 载荷含不可安全改写内容（不返回可写请求）。
        RedactionReviewRequiredError: 载荷需人工复核（已写隔离区）。

    Returns:
        已脱敏的 `EvidenceCreate`：`payload == redacted_payload == 引擎产物`，
        `source_payload_hash` 是**原文**哈希（仅落库，不参与 `payload_hash` 计算），
        `redaction_version` 是引擎版本（`redaction-v2`）。
    """
    status = _STATUS_MAP[result.status]
    observed_at = result.observed_at or result.started_at
    request = EvidenceCreate(
        tenant_id=tenant_id,
        task_id=task_id,
        environment_id=environment_id,
        step_id=step_id,
        tool_call_id=result.tool_call_id,
        db_engine=result.source.engine,
        db_version=result.source.db_version,
        adapter_version=result.source.adapter_version or "unknown",
        source_tool=result.tool_name,
        observed_at=observed_at,
        time_window_start=result.started_at,
        time_window_end=result.observed_at,
        collection_status=status,
        quality=_quality(result, status),
        completeness=_completeness(result, status),
        payload=_payload(result),
        redaction_version=REDACTION_VERSION,
        reproducible=True,
    )
    # 真脱敏：`payload`/`redacted_payload` 由引擎产出，原文锚点由引擎给出。
    resolved_gate = gate or RedactionGate(_REDACTION_ENGINE)
    return resolved_gate.for_evidence(request, tenant_id=tenant_id, task_id=task_id)


__all__ = ["REDACTION_VERSION", "to_evidence_create"]
