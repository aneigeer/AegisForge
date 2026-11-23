"""P5-T1：评测中台 API（记录实测运行 + 候选对比）。

原则（INV-06 + P3-T1 的 Machine Gate 口径）：

- **只接收实测指标**：允许 `null`（表示未测得），不接收"默认 0"或估算值；
- **对比只给方向性结论**：全部可比指标同向才判 `CANDIDATE_BETTER` /
  `BASELINE_BETTER`，否则 `INCONCLUSIVE`（不挑指标、不加权总分）；
- 结论落 `eval_comparisons`，供 P3-T1 的切换前确认引用；
- 租户隔离：一律按 `principal.tenant_id`，跨租户 404。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import get_control_client, get_principal
from core.errors import KnowledgeNotFoundError
from core.eval.repository import EvalRepository, compare_metrics
from core.security.authz import ensure_permission
from core.security.models import Principal
from core.security.roles import Permission

router = APIRouter(prefix="/v1/eval", tags=["eval"])


class SuiteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_version: str = Field(min_length=1, max_length=64)
    kind: Literal["benchmark", "golden", "wiki_snapshot"] = "benchmark"
    case_count: int = Field(ge=1, le=100_000)
    digest: str = Field(min_length=64, max_length=64)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1, max_length=64)
    candidate_label: str = Field(min_length=1, max_length=64)
    provider_type: str = Field(min_length=1, max_length=32)
    model_name: str = Field(min_length=1, max_length=128)
    wiki_snapshot: str | None = Field(default=None, max_length=64)
    status: Literal["COMPLETED", "PARTIAL", "FAILED", "NOT_EXECUTED"] = "COMPLETED"
    #: 指标字典：键为指标名，值为实测数字或 null（缺测）。**不接受字符串/布尔**。
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=512)


class CompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_run_id: str = Field(min_length=1, max_length=64)
    candidate_run_id: str = Field(min_length=1, max_length=64)


def _repo(control_client: Any) -> EvalRepository:
    return EvalRepository(control_client)


@router.post("/suites", response_model=dict)
def create_suite(
    body: SuiteBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.EVAL_MANAGE)
    suite_id = _repo(control_client).create_suite(
        principal.tenant_id,
        suite_version=body.suite_version,
        kind=body.kind,
        case_count=body.case_count,
        digest=body.digest,
    )
    return {"suite_id": suite_id, **body.model_dump()}


@router.post("/runs", response_model=dict)
def create_run(
    body: RunBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.EVAL_MANAGE)
    run_id = _repo(control_client).create_run(
        principal.tenant_id,
        suite_id=body.suite_id,
        candidate_label=body.candidate_label,
        provider_type=body.provider_type,
        model_name=body.model_name,
        wiki_snapshot=body.wiki_snapshot,
        status=body.status,
        metrics=body.metrics,
        notes=body.notes,
    )
    return {"run_id": run_id, **body.model_dump()}


class ExecuteEvalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_path: str = Field(default="benchmarks/w10/suite-v1.json", min_length=1, max_length=256)
    candidate_label: str = Field(default="console-probe", min_length=1, max_length=128)
    model_name: str = Field(default="harness-readonly", min_length=1, max_length=128)


@router.post("/runs/execute", response_model=dict)
def execute_eval_plan(
    body: ExecuteEvalBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    """从控制台触发一次诚实只读评测规划与登记（绝不伪造未实测模型分数，INV-06）。"""
    from pathlib import Path

    from core.benchmark.__main__ import _plan_payload

    ensure_permission(principal, Permission.EVAL_MANAGE)
    target = Path(body.suite_path)
    if not target.is_file():
        raise KnowledgeNotFoundError("评测套件文件不存在", details={"suite_path": body.suite_path})
    plan = _plan_payload(target)
    repo = _repo(control_client)
    suite_id = repo.create_suite(
        principal.tenant_id,
        suite_version=str(plan["suite_version"]),
        kind="harness",
        case_count=int(plan["case_count"]),
        digest=str(plan["plan_digest"]),
    )
    run_id = repo.create_run(
        principal.tenant_id,
        suite_id=suite_id,
        candidate_label=body.candidate_label,
        provider_type="harness",
        model_name=body.model_name,
        wiki_snapshot=None,
        status="PLANNED",
        metrics={},
        notes=f"Console 触发只读计划：cases={plan['case_count']} digest={plan['plan_digest'][:12]}（未填伪造分数）",
    )
    return {
        "run_id": run_id,
        "suite_id": suite_id,
        "status": "PLANNED",
        "case_count": int(plan["case_count"]),
        "plan_digest": plan["plan_digest"],
    }


@router.get("/runs", response_model=list)
def list_runs(
    limit: int = Query(default=20, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> list[dict]:
    ensure_permission(principal, Permission.EVAL_READ)
    return list(_repo(control_client).list_runs(principal.tenant_id, limit=limit))


@router.get("/runs/{run_id}", response_model=dict)
def get_run(
    run_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.EVAL_READ)
    row = _repo(control_client).get_run(principal.tenant_id, run_id)
    if row is None:
        raise KnowledgeNotFoundError("评测运行不存在", details={"run_id": run_id})
    return row


@router.post("/comparisons", response_model=dict)
def create_comparison(
    body: CompareBody,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.EVAL_MANAGE)
    repo = _repo(control_client)
    baseline = repo.get_run(principal.tenant_id, body.baseline_run_id)
    candidate = repo.get_run(principal.tenant_id, body.candidate_run_id)
    if baseline is None or candidate is None:
        raise KnowledgeNotFoundError(
            "对比运行不存在",
            details={
                "baseline_run_id": body.baseline_run_id,
                "candidate_run_id": body.candidate_run_id,
            },
        )
    verdict = compare_metrics(baseline["metrics"], candidate["metrics"])
    comparison_id = repo.record_comparison(
        principal.tenant_id,
        baseline_run_id=body.baseline_run_id,
        candidate_run_id=body.candidate_run_id,
        verdict=verdict,
    )
    return {
        "comparison_id": comparison_id,
        "baseline": {
            "run_id": baseline["run_id"],
            "candidate_label": baseline["candidate_label"],
            "metrics": baseline["metrics"],
        },
        "candidate": {
            "run_id": candidate["run_id"],
            "candidate_label": candidate["candidate_label"],
            "metrics": candidate["metrics"],
        },
        **verdict.as_payload(),
    }


@router.get("/comparisons/{comparison_id}", response_model=dict)
def get_comparison(
    comparison_id: str,
    principal: Principal = Depends(get_principal),
    control_client: Any = Depends(get_control_client),
) -> dict:
    ensure_permission(principal, Permission.EVAL_READ)
    row = _repo(control_client).get_comparison(principal.tenant_id, comparison_id)
    if row is None:
        raise KnowledgeNotFoundError(
            "对比记录不存在", details={"comparison_id": comparison_id}
        )
    return row


__all__ = ["router"]
