"""W04 三类 P0 只读诊断流程编排。

约束：
- 只读取公开 Manifest 字段，不读取、不导入任何 Oracle 真值；
- 每个 ToolResult 都写入 Evidence；工具 DENIED/NOT_SUPPORTED/截断如实保留；
- DiagnosisResult 中的 Hypothesis 只能引用真实写入的 evidence_id；
- 证据写入失败时任务标记 FAILED 并返回 UNKNOWN，不伪造诊断成功。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from adapters.base import DatabaseAdapter
from adapters.registry import build_adapter_registry
from core.diagnosis.analyzers import AnalysisOutcome, analyze_scenario
from core.diagnosis.evidence_mapping import to_evidence_create
from core.diagnosis.models import DiagnosisResult, DiagnosisStatus
from core.errors import DBGuardianError
from core.evidence.models import Evidence
from core.evidence.service import EvidenceService
from core.faults.models import FaultCaseManifest
from core.tasks.models import Task, TaskStatus, new_id
from core.tasks.service import TaskService
from core.tools.contracts import ToolContext
from core.tools.executor import ToolExecutor

_MANIFEST_DIR = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "faults" / "manifests"
)
_SCENARIO_ID_PATTERN = re.compile(r"^[a-z0-9_]{3,64}$")
_PUBLIC_TOOL_ALLOWLIST = frozenset(
    {
        "get_connection_snapshot",
        "get_query_stats",
        "get_lock_wait_edges",
        "get_lock_details",
        "get_transaction_metadata",
        "explain_query",
        "get_table_stats",
    }
)
_QUERY_LIMIT = 100


class ManifestReadError(ValueError):
    """公开 Manifest 读取/校验失败；错误信息不含 Oracle 字段。"""


def load_public_manifest(scenario_id: str) -> FaultCaseManifest:
    """只读取公开 Manifest；不读取 Oracle 真值。"""

    if not isinstance(scenario_id, str) or not _SCENARIO_ID_PATTERN.fullmatch(
        scenario_id
    ):
        raise ManifestReadError("非法 scenario_id")
    path = _MANIFEST_DIR / f"{scenario_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestReadError(f"Manifest 不存在：{scenario_id}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestReadError(
            f"Manifest 无法读取（{type(exc).__name__}）：{scenario_id}"
        ) from None
    manifest = FaultCaseManifest.model_validate(payload)
    if manifest.scenario_id != scenario_id:
        raise ManifestReadError("Manifest 文件名与 scenario_id 不一致")
    unknown = sorted(set(manifest.allowed_tools) - _PUBLIC_TOOL_ALLOWLIST)
    if unknown:
        raise ManifestReadError(f"Manifest 含非固定只读工具：{unknown}")
    return manifest


class DiagnosisRunner:
    """按 Manifest 白名单执行只读工具 → 落证据 → 规则分析 → DiagnosisResult。"""

    def __init__(
        self,
        task_service: TaskService,
        evidence_service: EvidenceService,
        adapter: DatabaseAdapter,
        tenant_id: str,
        environment_id: str,
    ) -> None:
        self._task_service = task_service
        self._evidence_service = evidence_service
        self._adapter = adapter
        self._tenant_id = tenant_id
        self._environment_id = environment_id
        self._executor = ToolExecutor(
            build_adapter_registry(adapter), task_service.repository
        )

    def run(self, scenario_id: str) -> DiagnosisResult:
        manifest = load_public_manifest(scenario_id)
        task = self._task_service.create_task(
            goal=(
                f"只读诊断场景 {manifest.scenario_id}"
                f"（{manifest.db_engine.value} {manifest.db_version}，"
                f"{manifest.scenario.value}）"
            ),
            tenant_id=self._tenant_id,
            environment_id=self._environment_id,
            risk_level="R0",
            max_tool_calls=max(1, len(manifest.allowed_tools)),
        )
        try:
            self._task_service.transition(task.task_id, TaskStatus.PLANNING)
            self._task_service.transition(task.task_id, TaskStatus.OBSERVING)
            evidences, evidence_ids, failure = self._collect(manifest, task)
            if failure is not None:
                return self._failed_result(manifest, task, evidence_ids, failure)
            self._task_service.transition(task.task_id, TaskStatus.DIAGNOSING)
            analysis = analyze_scenario(
                manifest.scenario, evidences, scenario_id=manifest.scenario_id
            )
            result = self._build_result(manifest, task, evidences, analysis)
            self._task_service.transition(task.task_id, TaskStatus.COMPLETED)
            return result
        except DBGuardianError:
            self._mark_failed(task)
            raise
        except Exception:
            self._mark_failed(task)
            raise

    # ---- collection ----------------------------------------------------
    def _collect(
        self, manifest: FaultCaseManifest, task: Task
    ) -> tuple[list[Evidence], list[str], str | None]:
        evidences: list[Evidence] = []
        evidence_ids: list[str] = []
        for tool_name in manifest.allowed_tools:
            call_id = new_id("call")
            params = self._tool_params(tool_name, manifest)
            step = self._task_service.add_step(
                task.task_id,
                title=f"采集 {tool_name}",
                tool_name=tool_name,
                params=params,
            )
            context = self._tool_context(task, call_id)
            # 必须经 Executor：写 tool_calls、审计事件、权限/超时/幂等预检。
            result = self._executor.execute(
                tool_name, params, context, task=task, step_id=step.step_id
            )
            try:
                # 映射处也要带**同一个**闸门实例：`payload` 原文在离开工具后第一次
                # 被组装成证据载荷，判定/审计/隔离必须落在服务装配的那个落点上。
                request = to_evidence_create(
                    result,
                    tenant_id=self._tenant_id,
                    task_id=task.task_id,
                    environment_id=self._environment_id,
                    step_id=step.step_id,
                    gate=self._evidence_service.redaction_gate,
                )
                evidence = self._evidence_service.create(request)
            except DBGuardianError as exc:
                self._mark_failed(task)
                return (
                    evidences,
                    evidence_ids,
                    f"证据写入失败（{exc.code.value}）：{exc.message}",
                )
            evidences.append(evidence)
            evidence_ids.append(evidence.evidence_id)
        return evidences, evidence_ids, None

    def _tool_context(self, task: Task, call_id: str) -> ToolContext:
        client = getattr(self._adapter, "_client", None)
        return ToolContext(
            client=client,
            task_id=task.task_id,
            run_id=task.run_id,
            tool_call_id=call_id,
        )

    def _tool_params(
        self, tool_name: str, manifest: FaultCaseManifest
    ) -> dict[str, Any]:
        engine = manifest.db_engine.value
        table = f"fault_{manifest.scenario.value}_{manifest.data_seed}"
        database = self._mysql_database()
        if tool_name == "explain_query":
            if engine == "mysql":
                sql = (
                    f"SELECT COUNT(*) AS c, SUM(k) AS s FROM {database}.{table} "
                    "WHERE k > 0 AND payload >= 'row-'"
                )
            else:
                schema = str(manifest.workload_config.get("schema") or "fault_lab")
                sql = (
                    f"SELECT count(*) AS c, sum(k) AS s FROM {schema}.{table} "
                    "WHERE k > 0 AND payload >= 'row-'"
                )
            return {"sql": sql}
        if tool_name == "get_table_stats":
            if engine == "mysql":
                return {"database_name": database, "limit": _QUERY_LIMIT}
            return {"table": table}
        if tool_name == "get_query_stats":
            if engine == "mysql":
                return {"database_name": database, "limit": _QUERY_LIMIT}
            return {"limit": _QUERY_LIMIT}
        if tool_name == "get_connection_snapshot":
            if engine == "mysql":
                return {"include_idle": True, "limit": _QUERY_LIMIT}
            return {"limit": _QUERY_LIMIT}
        if tool_name in {
            "get_transaction_metadata",
            "get_lock_wait_edges",
            "get_lock_details",
        }:
            return {"limit": _QUERY_LIMIT}
        return {}

    def _mysql_database(self) -> str:
        client = getattr(self._adapter, "_client", None)
        database = getattr(client, "database", None)
        return str(database) if database else "dg_lab"

    # ---- result --------------------------------------------------------
    def _build_result(
        self,
        manifest: FaultCaseManifest,
        task: Task,
        evidences: list[Evidence],
        analysis: AnalysisOutcome,
    ) -> DiagnosisResult:
        evidence_ids = [item.evidence_id for item in evidences]
        valid_ids = set(evidence_ids)
        hypotheses = [
            hypothesis.model_copy(
                update={
                    "supporting_evidence_ids": [
                        item
                        for item in hypothesis.supporting_evidence_ids
                        if item in valid_ids
                    ],
                    "contradicting_evidence_ids": [
                        item
                        for item in hypothesis.contradicting_evidence_ids
                        if item in valid_ids
                    ],
                }
            )
            for hypothesis in analysis.hypotheses
        ]
        stop_conditions = list(analysis.stop_conditions)
        if manifest.allowed_tools:
            stop_conditions.append("allowed_tools 已全部执行（工具预算耗尽）")
        return DiagnosisResult(
            task_id=task.task_id,
            scenario_id=manifest.scenario_id,
            scenario=manifest.scenario,
            db_engine=manifest.db_engine,
            status=analysis.status,
            hypotheses=hypotheses,
            evidence_ids=evidence_ids,
            missing_evidence=list(analysis.missing_evidence),
            stop_conditions=list(dict.fromkeys(stop_conditions)),
            scope="LAB",
            generated_by="rules-v0",
            observed_window=analysis.observed_window,
        )

    def _failed_result(
        self,
        manifest: FaultCaseManifest,
        task: Task,
        evidence_ids: list[str],
        failure: str,
    ) -> DiagnosisResult:
        self._mark_failed(task)
        return DiagnosisResult(
            task_id=task.task_id,
            scenario_id=manifest.scenario_id,
            scenario=manifest.scenario,
            db_engine=manifest.db_engine,
            status=DiagnosisStatus.UNKNOWN,
            hypotheses=[],
            evidence_ids=evidence_ids,
            missing_evidence=[failure],
            stop_conditions=[
                "证据写入失败：停止诊断，不伪造成功",
            ],
            scope="LAB",
            generated_by="rules-v0",
        )

    def _mark_failed(self, task: Task) -> None:
        try:
            current = self._task_service.get_task(task.task_id)
            if current.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                self._task_service.transition(task.task_id, TaskStatus.FAILED)
        except DBGuardianError:
            # 失败标记本身不得掩盖原始错误。
            pass


__all__ = ["DiagnosisRunner", "ManifestReadError", "load_public_manifest"]
