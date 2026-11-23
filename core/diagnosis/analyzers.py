"""W04 三类 P0 只读诊断分析器（引擎无关）。

输入为已落库的 Evidence 列表与场景，输出候选 Hypothesis、缺失项、停止条件与
整体诊断状态。分析器不读取 Oracle 真值，也不把累计统计当作当前窗口；任何能力
缺失、截断、视图不一致都必须进入 missing_evidence 并降级。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from core.evidence.models import (
    CollectionStatus,
    Evidence,
    EvidenceCompleteness,
    EvidenceQuality,
)
from core.diagnosis.models import (
    ConfidenceBand,
    DiagnosisStatus,
    Hypothesis,
    HypothesisStatus,
)
from core.faults.models import FaultScenario

# 连接压力阈值仅为建议值，未在本 LAB 实测校准；不得当作数据库真实上限。
SUGGESTED_SESSION_WARNING = 100
SUGGESTED_IDLE_TRANSACTION_SECONDS = 30
SUGGESTED_LONG_TRANSACTION_SECONDS = 60
THRESHOLD_DISCLAIMER = (
    "阈值为建议值/未实测"
    f"（会话数告警 {SUGGESTED_SESSION_WARNING}、空闲事务 "
    f"{SUGGESTED_IDLE_TRANSACTION_SECONDS}s、长事务 {SUGGESTED_LONG_TRANSACTION_SECONDS}s）"
)
APP_POOL_MISSING = "应用连接池状态未确认"
CUMULATIVE_STATS_ASSUMPTION = (
    "query_stats 为累计统计（自统计重置起），不代表当前采样窗口，不能单独作为当前根因证明"
)

_STOP_CONDITIONS: dict[FaultScenario, list[str]] = {
    FaultScenario.SLOW_QUERY: [
        "预算耗尽：allowed_tools 已全部执行或达到最大工具调用数",
        "重复证据：同一 digest/执行计划已采集且无新增信息",
        "能力缺失：get_query_stats 与 explain_query 均不可用，无法形成可验证假设",
        "采样不足：query_stats 无样本且 EXPLAIN 被截断，当前窗口无法确认",
    ],
    FaultScenario.CONNECTION_PRESSURE: [
        "预算耗尽：allowed_tools 已全部执行或达到最大工具调用数",
        "重复证据：同一会话快照/事务快照已采集且无新增信息",
        "能力缺失：get_connection_snapshot 不可用，无法确认数据库侧会话数",
        "未知项：应用连接池与 max_connections 上限始终未确认时停止并保留 PARTIAL",
    ],
    FaultScenario.LOCK_WAIT: [
        "预算耗尽：allowed_tools 已全部执行或达到最大工具调用数",
        "重复证据：同一阻塞树/锁视图已采集且无新增信息",
        "能力缺失：get_lock_wait_edges 与 get_lock_details 均不可用",
        "锁等待消失：reset 或事务结束导致阻塞边为空，诊断转为止损确认",
    ],
}

_USABLE_STATUSES = frozenset({CollectionStatus.SUCCESS, CollectionStatus.PARTIAL})
_CONFIDENCE_ORDER = {
    ConfidenceBand.UNKNOWN: -1,
    ConfidenceBand.LOW: 0,
    ConfidenceBand.MEDIUM: 1,
    ConfidenceBand.HIGH: 2,
}


@dataclass
class AnalysisOutcome:
    status: DiagnosisStatus
    hypotheses: list[Hypothesis] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    observed_window: dict[str, str] = field(default_factory=dict)


def stop_conditions_for(scenario: FaultScenario) -> list[str]:
    return list(_STOP_CONDITIONS.get(scenario, []))


# --------------------------------------------------------------------------- #
# 通用证据读取
# --------------------------------------------------------------------------- #


def _payload(evidence: Evidence) -> dict[str, Any]:
    return evidence.payload if isinstance(evidence.payload, dict) else {}


def _data(evidence: Evidence) -> dict[str, Any]:
    payload = _payload(evidence)
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _warnings(evidence: Evidence) -> list[str]:
    payload = _payload(evidence)
    warnings = payload.get("warnings")
    return [str(item) for item in warnings] if isinstance(warnings, list) else []


def _truncated(evidence: Evidence) -> bool:
    payload = _payload(evidence)
    data = _data(evidence)
    return bool(
        payload.get("truncated")
        or data.get("truncated")
        or data.get("plan_truncated")
        or evidence.completeness is EvidenceCompleteness.PARTIAL
    )


def _usable(evidence: Evidence) -> bool:
    return evidence.collection_status in _USABLE_STATUSES


def _material(evidence: Evidence) -> bool:
    return _usable(evidence) and bool(_data(evidence))


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _cap_confidence(
    value: ConfidenceBand, maximum: ConfidenceBand
) -> ConfidenceBand:
    if value is ConfidenceBand.UNKNOWN:
        return value
    if _CONFIDENCE_ORDER[value] <= _CONFIDENCE_ORDER[maximum]:
        return value
    return maximum


def _observed_window(evidences: Sequence[Evidence]) -> dict[str, str]:
    starts = [
        evidence.time_window_start or evidence.observed_at
        for evidence in evidences
        if evidence.time_window_start or evidence.observed_at
    ]
    ends = [
        evidence.time_window_end or evidence.observed_at
        for evidence in evidences
        if evidence.time_window_end or evidence.observed_at
    ]
    if not starts or not ends:
        return {}
    return {"start": min(starts), "end": max(ends)}


def _build_hypothesis(
    *,
    scenario_id: str,
    suffix: str,
    explanation: str,
    supporting: Iterable[str],
    contradicting: Iterable[str],
    missing: Iterable[str],
    confidence: ConfidenceBand,
    status: HypothesisStatus,
    assumptions: Iterable[str],
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"hyp_{scenario_id}_{suffix}",
        scenario_id=scenario_id,
        explanation=explanation[:2000],
        supporting_evidence_ids=_dedupe(supporting),
        contradicting_evidence_ids=_dedupe(contradicting),
        missing_evidence=list(dict.fromkeys(missing)),
        confidence_band=confidence,
        status=status,
        assumptions=list(dict.fromkeys(assumptions)),
    )


def _overall_status(
    usable: bool, missing: Sequence[str], degraded: bool
) -> DiagnosisStatus:
    if not usable:
        return DiagnosisStatus.UNKNOWN
    if missing or degraded:
        return DiagnosisStatus.PARTIAL
    return DiagnosisStatus.SUCCESS


# --------------------------------------------------------------------------- #
# 慢查询
# --------------------------------------------------------------------------- #


def _any_key_value(value: Any, key: str, expected: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get(key)).upper() == str(expected).upper():
            return True
        return any(_any_key_value(item, key, expected) for item in value.values())
    if isinstance(value, list):
        return any(_any_key_value(item, key, expected) for item in value)
    return False


def _has_mysql_table_node(value: Any) -> bool:
    if isinstance(value, dict):
        if "access_type" in value or "table_name" in value:
            return True
        return any(_has_mysql_table_node(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_mysql_table_node(item) for item in value)
    return False


def _detect_full_scan(plan: Any, engine: str) -> bool | None:
    """返回 True/False；None 表示无法判断（例如计划被截断或格式未知）。"""

    if plan is None or plan == {} or plan == []:
        return None
    text = plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False, default=str)
    lowered = text.lower()
    if engine == "postgresql" or engine == "postgres":
        if "seq scan" in lowered:
            return True
        if isinstance(plan, (dict, list)):
            return False
        return None
    # MySQL：结构化计划中 access_type=ALL 即全表扫描。
    if _any_key_value(plan, "access_type", "ALL"):
        return True
    if isinstance(plan, str):
        if '"access_type": "all"' in lowered or "access_type=all" in lowered:
            return True
        return None
    if _has_mysql_table_node(plan):
        return False
    return None


def _statement_matches_slow(statement: dict[str, Any]) -> bool:
    text = str(
        statement.get("digest_text") or statement.get("query_summary") or ""
    ).lower()
    if "fault_" in text or "fault_lab" in text:
        return True
    for key in ("no_index_used", "no_good_index_used"):
        value = statement.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return True
    rows = statement.get("rows_examined")
    if isinstance(rows, (int, float)) and rows >= 1000:
        return True
    mean_ms = statement.get("mean_time_ms") or statement.get("avg_time_ms")
    if isinstance(mean_ms, (int, float)) and mean_ms >= 1000:
        return True
    return False


def _sandbox_table_facts(evidence: Evidence) -> list[dict[str, Any]]:
    data = _data(evidence)
    tables = data.get("tables")
    if not isinstance(tables, list):
        return []
    facts: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        name = str(table.get("table_name") or table.get("table") or "")
        if name.startswith("fault_") or "fault_" in str(table.get("schema") or ""):
            facts.append(table)
    return facts


def _table_looks_unindexed(table: dict[str, Any]) -> bool:
    index_bytes = table.get("index_bytes")
    if isinstance(index_bytes, (int, float)) and index_bytes <= 0:
        return True
    idx_scan = table.get("idx_scan")
    seq_scan = table.get("seq_scan")
    if isinstance(idx_scan, (int, float)) and idx_scan <= 0:
        if isinstance(seq_scan, (int, float)) and seq_scan > 0:
            return True
    return False


def analyze_slow_query(
    evidences: Sequence[Evidence], *, scenario_id: str
) -> AnalysisOutcome:
    scenario = FaultScenario.SLOW_QUERY
    query_stats = [item for item in evidences if item.source_tool == "get_query_stats"]
    explains = [item for item in evidences if item.source_tool == "explain_query"]
    table_stats = [item for item in evidences if item.source_tool == "get_table_stats"]

    missing: list[str] = []
    supporting: list[str] = []
    contradicting: list[str] = []
    assumptions = [CUMULATIVE_STATS_ASSUMPTION]
    degraded = False

    full_scan_evidence: Evidence | None = None
    plan_unknown = False
    index_plan_confirmed = False
    # KD-W12-02：``unindexed`` 原先只在「存在 table_stats 证据」的 else 分支内
    # 赋值，但下方决策树在任何全表扫描路径都会读取它；无 table_stats 证据时会
    # 触发 UnboundLocalError。函数入口给确定初值，保证所有路径都有定义；
    # 「无 table_stats 证据」另走显式降级结论（见下方 UNKNOWN 分支）。
    unindexed: bool = False

    for evidence in explains:
        if not _usable(evidence):
            missing.append(
                f"EXPLAIN 不可用（{evidence.collection_status.value}）：无法确认当前执行计划"
            )
            degraded = True
            continue
        data = _data(evidence)
        plan = data.get("plan")
        truncated = _truncated(evidence)
        if not plan:
            missing.append("EXPLAIN 未返回可用执行计划")
            plan_unknown = True
            continue
        signal = _detect_full_scan(plan, evidence.db_engine)
        if signal is True:
            supporting.append(evidence.evidence_id)
            if truncated:
                missing.append("执行计划被截断：全表扫描结论不完整")
                plan_unknown = True
                degraded = True
            else:
                full_scan_evidence = evidence
        elif signal is False:
            contradicting.append(evidence.evidence_id)
            if not truncated:
                index_plan_confirmed = True
            else:
                missing.append("执行计划被截断：索引访问结论不完整")
                degraded = True
        else:
            if truncated:
                missing.append("执行计划被截断：无法判断全表扫描")
            else:
                missing.append("执行计划格式无法识别或能力缺失，未能判断全表扫描")
            plan_unknown = True
            degraded = True

    if not explains:
        missing.append("未获得 EXPLAIN 执行计划（工具未执行或失败）")
        plan_unknown = True

    matched_statements: list[dict[str, Any]] = []
    stats_evidence_ids: list[str] = []
    if not query_stats:
        missing.append("未获得 query_stats 摘要统计")
        plan_unknown = True
    else:
        for evidence in query_stats:
            if not _usable(evidence):
                missing.append(
                    "query_stats 能力不可用"
                    f"（{evidence.collection_status.value}）：无法用摘要统计印证全表扫描"
                )
                degraded = True
                continue
            data = _data(evidence)
            statements = data.get("statements")
            if not isinstance(statements, list) or not statements:
                missing.append("query_stats 采样不足（无可用语句摘要）")
                degraded = True
                continue
            stats_evidence_ids.append(evidence.evidence_id)
            if _truncated(evidence):
                missing.append("query_stats 结果被截断，摘要不完整")
                degraded = True
            matched_statements.extend(
                item
                for item in statements
                if isinstance(item, dict) and _statement_matches_slow(item)
            )
        if stats_evidence_ids and not matched_statements:
            missing.append("query_stats 未观察到与场景沙箱表/高扫描量匹配的 digest")
        if matched_statements:
            supporting.extend(stats_evidence_ids)

    if not table_stats:
        missing.append("未获得 table_stats 表规模/索引信息")
    else:
        found_sandbox = False
        for evidence in table_stats:
            if not _usable(evidence):
                missing.append(
                    f"table_stats 不可用（{evidence.collection_status.value}）：表规模与索引信息缺失"
                )
                degraded = True
                continue
            if _truncated(evidence):
                missing.append("table_stats 结果被截断，表/索引信息不完整")
                degraded = True
            facts = _sandbox_table_facts(evidence)
            if facts:
                found_sandbox = True
                supporting.append(evidence.evidence_id)
                unindexed = unindexed or any(_table_looks_unindexed(item) for item in facts)
            for warning in _warnings(evidence):
                if "估算" in warning or "滞后" in warning:
                    assumptions.append(f"table_stats 限制：{warning}")
        if not found_sandbox:
            missing.append("table_stats 未覆盖场景沙箱表，无法核对索引/规模")

    usable = any(_material(item) for item in evidences)

    if full_scan_evidence is not None:
        explanation = (
            "场景沙箱表上的过滤/聚合查询缺少可用索引，EXPLAIN 显示全表扫描"
            "（MySQL access_type=ALL / PostgreSQL Seq Scan），支持“缺少可用索引/全表扫描”候选根因"
        )
        status = HypothesisStatus.CANDIDATE
        confidence = ConfidenceBand.HIGH
        if plan_unknown or not matched_statements or not supporting:
            confidence = _cap_confidence(confidence, ConfidenceBand.MEDIUM)
        if index_plan_confirmed or degraded or _truncated(full_scan_evidence):
            confidence = _cap_confidence(confidence, ConfidenceBand.MEDIUM)
        if unindexed and matched_statements and not plan_unknown and not index_plan_confirmed:
            status = HypothesisStatus.CONFIRMED
        if not table_stats:
            # KD-W12-02：观察到全表扫描但**完全没有** table_stats 证据时，
            # 证据不足以确认「缺少可用索引」，输出明确降级结论（UNKNOWN），
            # 不抛异常、不判 CONFIRMED；missing_evidence 已登记缺件。
            explanation = (
                "证据不足：EXPLAIN 已观察到全表扫描，但未获得 table_stats 表规模/索引信息，"
                "无法确认“缺少可用索引/全表扫描”候选根因；结论记 UNKNOWN，待补齐证据后复核"
            )
            status = HypothesisStatus.UNKNOWN
            confidence = ConfidenceBand.UNKNOWN
    elif index_plan_confirmed:
        explanation = (
            "EXPLAIN 显示使用索引访问而非全表扫描，未观察到“缺少可用索引/全表扫描”信号；"
            "当前证据不支持该候选根因"
        )
        status = HypothesisStatus.REJECTED
        confidence = ConfidenceBand.LOW
    elif matched_statements:
        explanation = (
            "query_stats 累计摘要显示高扫描量/无索引信号，但缺少当前执行计划或计划被截断；"
            "不能确认当前窗口存在全表扫描"
        )
        status = HypothesisStatus.CANDIDATE
        confidence = ConfidenceBand.LOW
        missing.append("缺少当前窗口执行计划")
    else:
        explanation = (
            "证据不足，无法形成可验证的慢查询根因假设；缺少执行计划与当前窗口采样"
        )
        status = HypothesisStatus.UNKNOWN
        confidence = ConfidenceBand.UNKNOWN

    hypothesis = _build_hypothesis(
        scenario_id=scenario_id,
        suffix="missing_index_full_scan",
        explanation=explanation,
        supporting=supporting,
        contradicting=contradicting,
        missing=missing,
        confidence=confidence,
        status=status,
        assumptions=assumptions,
    )
    overall = _overall_status(usable, missing, degraded)
    if full_scan_evidence is None and index_plan_confirmed and not missing and not degraded:
        overall = DiagnosisStatus.SUCCESS
    return AnalysisOutcome(
        status=overall,
        hypotheses=[hypothesis],
        missing_evidence=list(dict.fromkeys(missing)),
        stop_conditions=stop_conditions_for(scenario),
        observed_window=_observed_window(evidences),
    )


# --------------------------------------------------------------------------- #
# 连接压力
# --------------------------------------------------------------------------- #


def _session_count(data: dict[str, Any], sessions: list[Any]) -> int:
    for key in ("session_count", "row_count", "active_session_count"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return len(sessions)


def _row_age(session: dict[str, Any]) -> Any:
    """兼容 MySQL trx_age_seconds 与统一字段 transaction_age_seconds。"""
    age = session.get("transaction_age_seconds")
    if age is None:
        age = session.get("trx_age_seconds")
    return age


def _is_idle_session(session: dict[str, Any]) -> bool:
    state = str(session.get("state") or "").strip().lower()
    command = str(session.get("command") or "").strip().lower()
    return state in {"idle", "idle in transaction", "idle in transaction (aborted)"} or command == "sleep"


def _is_idle_in_transaction_session(session: dict[str, Any]) -> bool:
    state = str(session.get("state") or "").strip().lower()
    command = str(session.get("command") or "").strip().lower()
    if "idle in transaction" in state:
        return True
    if command == "sleep" and str(session.get("transaction_state") or "").upper() in {
        "RUNNING",
        "LOCK WAIT",
    }:
        return True
    return False


def _max_connections(evidence: Evidence) -> int | None:
    payload = _payload(evidence)
    for container in (_data(evidence), payload, payload.get("source_metadata")):
        if not isinstance(container, dict):
            continue
        value = container.get("max_connections")
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _app_pool_confirmed(evidence: Evidence) -> bool:
    payload = _payload(evidence)
    for container in (_data(evidence), payload, payload.get("source_metadata")):
        if not isinstance(container, dict):
            continue
        if container.get("application_pool") or container.get("app_pool_status"):
            return True
    return False


def analyze_connection_pressure(
    evidences: Sequence[Evidence], *, scenario_id: str
) -> AnalysisOutcome:
    scenario = FaultScenario.CONNECTION_PRESSURE
    snapshots = [item for item in evidences if item.source_tool == "get_connection_snapshot"]
    transactions = [
        item for item in evidences if item.source_tool == "get_transaction_metadata"
    ]
    query_stats = [item for item in evidences if item.source_tool == "get_query_stats"]

    missing: list[str] = []
    supporting: list[str] = []
    contradicting: list[str] = []
    assumptions = [THRESHOLD_DISCLAIMER]
    degraded = False

    counts: list[int] = []
    idle_count = 0
    idle_tx_count = 0
    long_tx_count = 0
    max_connections: int | None = None
    app_pool_confirmed = False
    snapshot_evidence: Evidence | None = None
    session_age_max = 0.0
    unobservable_state = 0

    for evidence in snapshots:
        if not _usable(evidence):
            missing.append(
                f"连接快照不可用（{evidence.collection_status.value}）：无法确认会话数"
            )
            degraded = True
            continue
        data = _data(evidence)
        sessions = data.get("sessions")
        sessions = sessions if isinstance(sessions, list) else []
        count = _session_count(data, sessions)
        counts.append(count)
        snapshot_evidence = evidence
        supporting.append(evidence.evidence_id)
        app_pool_confirmed = app_pool_confirmed or _app_pool_confirmed(evidence)
        if max_connections is None:
            max_connections = _max_connections(evidence)
        for session in sessions:
            if not isinstance(session, dict):
                continue
            if (
                session.get("state") is None
                and session.get("command") is None
                and session.get("database")
            ):
                unobservable_state += 1
            if _is_idle_session(session):
                idle_count += 1
            if _is_idle_in_transaction_session(session):
                idle_tx_count += 1
            age = _row_age(session)
            if not isinstance(age, (int, float)):
                age = session.get("time_seconds")
            if isinstance(age, (int, float)):
                session_age_max = max(session_age_max, float(age))
        if _truncated(evidence):
            missing.append("会话快照被截断，会话数与空闲会话数可能不完整")
            degraded = True

    if unobservable_state > 0:
        missing.append(
            "会话状态可见性受限（只读账号可能缺少 pg_read_all_stats），"
            "空闲会话/idle in transaction 判定不完整"
        )
        degraded = True

    if not snapshots:
        missing.append("未获得 get_connection_snapshot 会话快照")
        degraded = True

    if not transactions:
        missing.append("未获得 get_transaction_metadata 事务快照")
        degraded = True
    else:
        for evidence in transactions:
            if not _usable(evidence):
                missing.append(
                    f"事务快照不可用（{evidence.collection_status.value}）："
                    "idle in transaction/长事务状态未确认"
                )
                degraded = True
                continue
            data = _data(evidence)
            rows = data.get("transactions")
            rows = rows if isinstance(rows, list) else []
            observed = False
            for row in rows:
                if not isinstance(row, dict):
                    continue
                observed = True
                if row.get("idle_in_transaction") or _is_idle_in_transaction_session(row):
                    idle_tx_count += 1
                age = _row_age(row)
                if isinstance(age, (int, float)) and float(age) >= SUGGESTED_LONG_TRANSACTION_SECONDS:
                    long_tx_count += 1
                    session_age_max = max(session_age_max, float(age))
            if observed:
                supporting.append(evidence.evidence_id)
            if _truncated(evidence):
                missing.append("事务快照被截断，长事务统计可能不完整")
                degraded = True

    connection_errors: int | None = None
    if not query_stats:
        missing.append("未获得 get_query_stats：连接/语句错误统计未确认")
    else:
        for evidence in query_stats:
            if not _usable(evidence):
                missing.append(
                    f"query_stats 不可用（{evidence.collection_status.value}）："
                    "连接/语句错误统计未确认"
                )
                degraded = True
                continue
            data = _data(evidence)
            statements = data.get("statements")
            if not isinstance(statements, list) or not statements:
                missing.append("query_stats 采样不足：连接/语句错误统计未确认")
                degraded = True
                continue
            errors = sum(
                int(item.get("errors") or 0)
                for item in statements
                if isinstance(item, dict)
            )
            connection_errors = (connection_errors or 0) + errors
            if errors > 0:
                supporting.append(evidence.evidence_id)
            if _truncated(evidence):
                missing.append("query_stats 结果被截断，连接/语句错误统计不完整")
                degraded = True

    if long_tx_count > 0:
        assumptions.append(
            f"观察到 {long_tx_count} 个超过建议阈值 {SUGGESTED_LONG_TRANSACTION_SECONDS}s 的事务（建议值/未实测）"
        )
    if connection_errors is not None:
        assumptions.append(f"累计语句错误数 {connection_errors}（累计统计，不代表当前窗口）")

    if not app_pool_confirmed:
        missing.append(APP_POOL_MISSING)
    if max_connections is None:
        missing.append("max_connections 上限未确认（无法计算连接使用率）")

    usable = any(_material(item) for item in evidences)
    observed_count = max(counts) if counts else 0

    if snapshot_evidence is None:
        explanation = "未获得可用连接快照，无法形成连接压力候选根因"
        status = HypothesisStatus.UNKNOWN
        confidence = ConfidenceBand.UNKNOWN
        overall = DiagnosisStatus.UNKNOWN
    else:
        usage: float | None = None
        if max_connections:
            usage = observed_count / max_connections
        rejected = (
            usage is not None
            and usage < 0.5
            and idle_count == 0
            and idle_tx_count == 0
            and long_tx_count == 0
        )
        if rejected:
            explanation = (
                f"连接快照显示会话数 {observed_count}，远低于 max_connections={max_connections}，"
                "且未观察到空闲/长事务；当前证据不支持连接压力候选根因"
            )
            status = HypothesisStatus.REJECTED
            confidence = ConfidenceBand.LOW
            contradicting.extend(item.evidence_id for item in snapshots)
        elif usage is not None and usage >= 0.8:
            explanation = (
                f"数据库侧会话数 {observed_count}，已接近 max_connections={max_connections}"
                f"（建议阈值 {SUGGESTED_SESSION_WARNING} 为建议值/未实测）；"
                f"空闲会话 {idle_count}、idle in transaction {idle_tx_count}；"
                f"{APP_POOL_MISSING}，不能排除连接泄漏或池配置问题"
            )
            status = HypothesisStatus.CANDIDATE
            confidence = ConfidenceBand.MEDIUM
        elif idle_count > 0 or idle_tx_count > 0 or long_tx_count > 0:
            explanation = (
                f"连接快照显示会话数 {observed_count}（空闲 {idle_count}），"
                f"idle in transaction {idle_tx_count}、长事务 {long_tx_count}；"
                f"可能接近连接上限或存在连接占用，但 max_connections 上限与{APP_POOL_MISSING}，"
                "不能确认是否由应用连接池引起"
            )
            status = HypothesisStatus.CANDIDATE
            confidence = ConfidenceBand.MEDIUM
            if not app_pool_confirmed:
                confidence = _cap_confidence(confidence, ConfidenceBand.MEDIUM)
        else:
            explanation = (
                f"连接快照显示会话数 {observed_count}，空闲会话 {idle_count}；"
                f"未观察到明显空闲事务或长事务，且{APP_POOL_MISSING}"
            )
            status = HypothesisStatus.CANDIDATE
            confidence = ConfidenceBand.LOW
        if degraded or missing:
            confidence = _cap_confidence(confidence, ConfidenceBand.MEDIUM)
        overall = DiagnosisStatus.PARTIAL

    hypothesis = _build_hypothesis(
        scenario_id=scenario_id,
        suffix="connection_pressure_pool_unknown",
        explanation=explanation,
        supporting=supporting,
        contradicting=contradicting,
        missing=missing,
        confidence=confidence,
        status=status,
        assumptions=assumptions,
    )
    if snapshot_evidence is not None:
        overall = DiagnosisStatus.PARTIAL
    elif not usable:
        overall = DiagnosisStatus.UNKNOWN
    return AnalysisOutcome(
        status=overall,
        hypotheses=[hypothesis],
        missing_evidence=list(dict.fromkeys(missing)),
        stop_conditions=stop_conditions_for(scenario),
        observed_window=_observed_window(evidences),
    )


# --------------------------------------------------------------------------- #
# 锁等待
# --------------------------------------------------------------------------- #


def _normalize_edge(edge: dict[str, Any], engine: str) -> dict[str, Any]:
    if engine == "postgresql":
        return {
            "waiter": edge.get("blocked_pid"),
            "blocker": edge.get("blocking_pid"),
            "object": edge.get("relation") or edge.get("relation_name") or "未知对象",
            "schema": edge.get("schema"),
            "wait_seconds": edge.get("wait_seconds"),
            "transaction_age_seconds": None,
            "mode": edge.get("mode"),
            "lock_type": edge.get("lock_type"),
        }
    return {
        "waiter": edge.get("waiting_processlist_id") or edge.get("waiting_thread_id"),
        "blocker": edge.get("blocking_processlist_id") or edge.get("blocking_thread_id"),
        "object": edge.get("object_name") or edge.get("relation") or "未知对象",
        "schema": edge.get("waiting_database") or edge.get("object_schema"),
        "wait_seconds": edge.get("waiting_time_seconds"),
        "transaction_age_seconds": edge.get("blocking_transaction_age_seconds")
        or edge.get("waiting_transaction_age_seconds"),
        "mode": edge.get("blocking_lock_mode") or edge.get("waiting_lock_mode"),
        "lock_type": edge.get("lock_type"),
    }


def _transaction_age_map(transactions: Sequence[Evidence]) -> dict[str, float]:
    ages: dict[str, float] = {}
    for evidence in transactions:
        if not _usable(evidence):
            continue
        data = _data(evidence)
        rows = data.get("transactions")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("processlist_id") or row.get("pid") or row.get("trx_mysql_thread_id")
            if key is None:
                continue
            age = row.get("transaction_age_seconds")
            if isinstance(age, (int, float)):
                ages[str(key)] = float(age)
    return ages


def _root_blockers(root_payload: Any) -> set[str]:
    roots: set[str] = set()
    if not isinstance(root_payload, list):
        return roots
    for item in root_payload:
        if isinstance(item, dict):
            value = (
                item.get("blocking_processlist_id")
                or item.get("blocking_thread_id")
                or item.get("blocking_pid")
            )
        else:
            value = item
        if value is not None:
            roots.add(str(value))
    return roots


def analyze_lock_wait(
    evidences: Sequence[Evidence], *, scenario_id: str
) -> AnalysisOutcome:
    scenario = FaultScenario.LOCK_WAIT
    edge_evidence = [
        item for item in evidences if item.source_tool == "get_lock_wait_edges"
    ]
    detail_evidence = [
        item for item in evidences if item.source_tool == "get_lock_details"
    ]
    transactions = [
        item for item in evidences if item.source_tool == "get_transaction_metadata"
    ]

    missing: list[str] = []
    supporting: list[str] = []
    contradicting: list[str] = []
    assumptions = [
        "阻塞树来自 performance_schema.data_lock_waits / pg_locks + pg_blocking_pids，"
        "跨视图不一致时保留 PARTIAL",
    ]
    degraded = False

    normalized_edges: list[dict[str, Any]] = []
    roots: set[str] = set()
    edge_count = 0
    for evidence in edge_evidence:
        if not _usable(evidence):
            missing.append(
                f"锁等待阻塞边不可用（{evidence.collection_status.value}）：无法构建阻塞树"
            )
            degraded = True
            continue
        data = _data(evidence)
        edges = data.get("edges")
        edges = edges if isinstance(edges, list) else []
        edge_count += len(edges)
        roots = roots | _root_blockers(data.get("roots") or data.get("root_pids"))
        for edge in edges:
            if isinstance(edge, dict):
                normalized_edges.append(_normalize_edge(edge, evidence.db_engine))
        supporting.extend(
            [evidence.evidence_id] if edges else []
        )
        if _truncated(evidence):
            missing.append("锁等待阻塞边被截断，阻塞树不完整")
            degraded = True
        for warning in _warnings(evidence):
            if "权限" in warning or "不可用" in warning or "未采集" in warning:
                assumptions.append(f"锁视图限制：{warning}")

    if not edge_evidence:
        missing.append("未获得 get_lock_wait_edges 阻塞边")
        degraded = True

    detail_waiting: int | None = None
    detail_evidence_ids: list[str] = []
    for evidence in detail_evidence:
        if not _usable(evidence):
            missing.append(
                f"锁明细不可用（{evidence.collection_status.value}）：无法交叉核对等待锁"
            )
            degraded = True
            continue
        data = _data(evidence)
        waiting = data.get("waiting_count")
        if isinstance(waiting, (int, float)):
            detail_waiting = int(waiting)
            if detail_waiting > 0:
                detail_evidence_ids.append(evidence.evidence_id)
                supporting.append(evidence.evidence_id)
        elif isinstance(data.get("locks"), list):
            waiting_rows = [
                row
                for row in data["locks"]
                if isinstance(row, dict) and not row.get("granted", True)
            ]
            if waiting_rows:
                detail_waiting = len(waiting_rows)
                detail_evidence_ids.append(evidence.evidence_id)
                supporting.append(evidence.evidence_id)
        if _truncated(evidence):
            missing.append("锁明细被截断，等待锁统计可能不完整")
            degraded = True

    if not detail_evidence:
        missing.append("未获得 get_lock_details 锁明细")

    age_map = _transaction_age_map(transactions)
    if not transactions:
        missing.append("未获得 get_transaction_metadata 事务年龄")
    else:
        usable_transactions = [item for item in transactions if _usable(item)]
        if usable_transactions:
            supporting.extend(item.evidence_id for item in usable_transactions)
        else:
            missing.append(
                "事务元数据不可用（"
                + ",".join(sorted({item.collection_status.value for item in transactions}))
                + "）：事务年龄/等待时长未确认"
            )
            degraded = True

    # 为边补充事务年龄（优先使用边自带值，其次按 pid 关联事务元数据）。
    for edge in normalized_edges:
        if edge["transaction_age_seconds"] is not None:
            continue
        blocker = edge.get("blocker")
        if blocker is not None and str(blocker) in age_map:
            edge["transaction_age_seconds"] = age_map[str(blocker)]

    if normalized_edges and detail_waiting is not None:
        if detail_waiting != len(normalized_edges):
            missing.append(
                "锁视图不一致：阻塞边数量 "
                f"{len(normalized_edges)} 与锁明细 waiting_count={detail_waiting} 不一致"
            )
            degraded = True
            contradicting.extend(detail_evidence_ids[:1])

    ages_observed = [
        edge["transaction_age_seconds"]
        for edge in normalized_edges
        if isinstance(edge["transaction_age_seconds"], (int, float))
    ]
    if normalized_edges and not ages_observed:
        missing.append("阻塞事务年龄/等待时长未确认")
        degraded = True
    if normalized_edges and any(
        isinstance(edge["wait_seconds"], (int, float)) for edge in normalized_edges
    ):
        assumptions.append("等待时长来自锁视图的会话查询开始时间，不等于事务总年龄")

    usable = any(_material(item) for item in evidences)

    if normalized_edges:
        primary = next(
            (edge for edge in normalized_edges if str(edge.get("blocker")) in roots),
            normalized_edges[0],
        )
        blocker = primary.get("blocker")
        waiter = primary.get("waiter")
        obj = primary.get("object")
        wait_seconds = primary.get("wait_seconds")
        age_seconds = primary.get("transaction_age_seconds")
        details_text = []
        if isinstance(wait_seconds, (int, float)):
            details_text.append(f"等待 {float(wait_seconds):.1f}s")
        if isinstance(age_seconds, (int, float)):
            details_text.append(f"阻塞事务年龄 {float(age_seconds):.1f}s")
        explanation = (
            f"会话/事务 {blocker} 持有 {obj} 上的锁，会话 {waiter} 正在等待"
            + ("（" + "，".join(details_text) + "）" if details_text else "")
            + "（lock_wait）"
        )
        status = HypothesisStatus.CANDIDATE
        confidence = ConfidenceBand.HIGH
        if degraded or missing or not detail_evidence_ids:
            confidence = _cap_confidence(confidence, ConfidenceBand.MEDIUM)
        if any(_truncated(item) for item in edge_evidence):
            confidence = _cap_confidence(confidence, ConfidenceBand.LOW)
        if detail_evidence_ids and ages_observed and not degraded:
            status = HypothesisStatus.CONFIRMED
        supporting.extend(
            item.evidence_id for item in [*edge_evidence, *detail_evidence]
            if _usable(item)
        )
    elif detail_waiting is not None and detail_waiting > 0:
        explanation = (
            "锁明细显示存在等待中的锁，但阻塞边为空或不可用；无法唯一确定阻塞者"
        )
        status = HypothesisStatus.UNKNOWN
        confidence = ConfidenceBand.UNKNOWN
        contradicting.extend(detail_evidence_ids)
    elif edge_evidence and all(_usable(item) for item in edge_evidence):
        explanation = "锁等待视图未观察到阻塞边，当前未确认锁等待根因"
        status = HypothesisStatus.REJECTED
        confidence = ConfidenceBand.LOW
    else:
        explanation = "锁等待证据不足，无法形成可验证的阻塞根因假设"
        status = HypothesisStatus.UNKNOWN
        confidence = ConfidenceBand.UNKNOWN

    hypothesis = _build_hypothesis(
        scenario_id=scenario_id,
        suffix="blocking_transaction_lock_wait",
        explanation=explanation,
        supporting=supporting,
        contradicting=contradicting,
        missing=missing,
        confidence=confidence,
        status=status,
        assumptions=assumptions,
    )

    if normalized_edges or (detail_waiting is not None and detail_waiting > 0):
        overall = DiagnosisStatus.PARTIAL if (missing or degraded) else DiagnosisStatus.SUCCESS
    elif edge_evidence and all(_usable(item) for item in edge_evidence):
        overall = DiagnosisStatus.SUCCESS
    elif usable:
        overall = DiagnosisStatus.PARTIAL
    else:
        overall = DiagnosisStatus.UNKNOWN

    return AnalysisOutcome(
        status=overall,
        hypotheses=[hypothesis],
        missing_evidence=list(dict.fromkeys(missing)),
        stop_conditions=stop_conditions_for(scenario),
        observed_window=_observed_window(evidences),
    )


def analyze_scenario(
    scenario: FaultScenario | str,
    evidences: Sequence[Evidence],
    *,
    scenario_id: str,
) -> AnalysisOutcome:
    value = FaultScenario(scenario)
    if value is FaultScenario.SLOW_QUERY:
        return analyze_slow_query(evidences, scenario_id=scenario_id)
    if value is FaultScenario.CONNECTION_PRESSURE:
        return analyze_connection_pressure(evidences, scenario_id=scenario_id)
    return analyze_lock_wait(evidences, scenario_id=scenario_id)


__all__ = [
    "APP_POOL_MISSING",
    "AnalysisOutcome",
    "SUGGESTED_IDLE_TRANSACTION_SECONDS",
    "SUGGESTED_LONG_TRANSACTION_SECONDS",
    "SUGGESTED_SESSION_WARNING",
    "THRESHOLD_DISCLAIMER",
    "analyze_connection_pressure",
    "analyze_lock_wait",
    "analyze_scenario",
    "analyze_slow_query",
    "stop_conditions_for",
]
