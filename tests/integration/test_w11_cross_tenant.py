"""W11 T02 集成测试：跨租户对象级授权（AC-05，7 探针）。

口径（``docs/w11/02-design.md`` §6.1、裁定 **D-12**）：

- 六类对象（Task / Evidence / Approval / Report / Environment / Audit）**各 1 例**
  跨租户读取：``deny_reason == "OBJECT_AUTH_DENIED"`` 且 ``result_set_size == 0``；
- 第 7 探针覆盖伪任务 ``system:worker_scheduler``（``docs/w08/08-drill-report.md:66-70``
  的 ``tenant_id`` 过滤列为 NULL 边界）：**按 ``tenant_id`` 查询不返回**该伪任务事件、
  **按 ``task_id`` 直查可见**，两者**并存如实记录**（历史 NULL 行不 UPDATE）；
- **归一化不改生产**：``TENANT_INVALID`` / ``TASK_NOT_FOUND`` / ``EVIDENCE_INVALID`` /
  ``APPROVAL_NOT_FOUND`` / 空集 → 统一标签 ``OBJECT_AUTH_DENIED``，
  ``production_code`` 保留真实码；并且断言 ``core/errors.py`` **未新增**
  ``OBJECT_AUTH_DENIED``（W11 零生产枚举改动）。

为什么每条探针都要"归属租户读得到"：只断言"其他租户读不到"会被"对象压根不存在"
假通过。因此每条探针都同时取归属租户的正向读（≥1 行）作为反证。

数据边界：只 INSERT 追加（对象由 T01 的 ``provision_cross_tenant_objects`` 提供），
不 DELETE / UPDATE / TRUNCATE / DROP，不清理；控制库不可用时整文件 skip 并带原因。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.approval.repository import ApprovalRepository
from core.assurance.concurrency import (
    CONCURRENCY_CASE_IDS,
    CONCURRENCY_MANIFEST_VERSION,
    CROSS_TENANT_PROBE_COUNT,
    CROSS_TENANT_PROBE_IDS,
    EMPTY_RESULT_CODE,
    OBJECT_AUTH_DENIED,
    PSEUDO_TASK_ID,
    PSEUDO_TASK_PROBE_ID,
    CrossTenantResult,
    load_concurrency_manifest,
    normalize_object_denial,
    summarize_cross_tenant,
)
from core.assurance.negative import CROSS_TENANT_DENY_TAG, ObjectKind
from core.audit.service import AuditService
from core.errors import DBGuardianError, ErrorCode, TenantInvalidError
from core.evidence.repository import EvidenceRepository
from core.report.repository import ReportRepository
from core.tasks.repository import TaskRepository
from tests.integration.test_w11_security_negative import (
    CrossTenantObjects,
    provision_cross_tenant_objects,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "benchmarks" / "w11" / "concurrency_cases.json"
_ERRORS_SOURCE = _REPO_ROOT / "core" / "errors.py"


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@pytest.fixture(scope="module")
def objects(control_db: Any) -> CrossTenantObjects:
    return provision_cross_tenant_objects(control_db)


def _denied(
    probe_id: str,
    kind: ObjectKind,
    *,
    production_code: str | None,
    result_set_size: int,
    owner_visible: int,
    decided_by: str,
    extra: dict[str, Any] | None = None,
) -> tuple[CrossTenantResult, dict[str, Any]]:
    """把一次跨租户读取归一化，并返回可核对的结构化证据。

    正向反证（``owner_visible >= 1``）在断言里检查：没有它，"返回空集"可能只是
    "对象不存在"。
    """
    result = normalize_object_denial(
        production_code,
        result_set_size,
        probe_id=probe_id,
        object_kind=kind,
    )
    evidence: dict[str, Any] = {
        "probe_id": probe_id,
        "object_kind": kind.value,
        "production_code": result.production_code,
        "result_set_size": result.result_set_size,
        "owner_tenant_visible": owner_visible,
        "decided_by": decided_by,
    }
    evidence.update(extra or {})
    return result, evidence


def _probe_task(client: Any, obj: CrossTenantObjects) -> tuple[CrossTenantResult, dict[str, Any]]:
    tasks = TaskRepository(client)
    code: str | None = None
    try:
        tasks.get_task(obj.task_id, tenant_id=obj.other_tenant_id)
    except DBGuardianError as exc:
        code = exc.code.value
    else:
        raise AssertionError("跨租户读取任务未被拒绝")
    owner_visible = 1 if tasks.task_belongs_to(obj.task_id, obj.tenant_id) else 0
    return _denied(
        "probe_task_cross_tenant",
        ObjectKind.TASK,
        production_code=code,
        result_set_size=0,
        owner_visible=owner_visible,
        decided_by="core.tasks.repository.TaskRepository.get_task",
    )


def _probe_evidence(
    client: Any, obj: CrossTenantObjects
) -> tuple[CrossTenantResult, dict[str, Any]]:
    repository = EvidenceRepository(client)
    code: str | None = None
    try:
        repository.get(obj.evidence_id, obj.other_tenant_id)
    except DBGuardianError as exc:
        code = exc.code.value
    else:
        raise AssertionError("跨租户读取证据未被拒绝")
    owner_visible = repository.count_for_task(obj.task_id, obj.tenant_id)
    return _denied(
        "probe_evidence_cross_tenant",
        ObjectKind.EVIDENCE,
        production_code=code,
        result_set_size=0,
        owner_visible=owner_visible,
        decided_by="core.evidence.repository.EvidenceRepository.get",
    )


def _probe_approval(
    client: Any, obj: CrossTenantObjects
) -> tuple[CrossTenantResult, dict[str, Any]]:
    repository = ApprovalRepository(client)
    code: str | None = None
    try:
        repository.get(obj.approval_id, tenant_id=obj.other_tenant_id)
    except DBGuardianError as exc:
        code = exc.code.value
    else:
        raise AssertionError("跨租户读取审批未被拒绝")
    rows = client.fetch_all(
        "SELECT COUNT(*) AS n FROM dg_system.approvals WHERE tenant_id = %s AND task_id = %s",
        (obj.tenant_id, obj.task_id),
    )
    owner_visible = int(rows[0]["n"]) if rows else 0
    return _denied(
        "probe_approval_cross_tenant",
        ObjectKind.APPROVAL,
        production_code=code,
        result_set_size=0,
        owner_visible=owner_visible,
        decided_by="core.approval.repository.ApprovalRepository.get",
    )


def _probe_report(
    client: Any, obj: CrossTenantObjects
) -> tuple[CrossTenantResult, dict[str, Any]]:
    repository = ReportRepository(client)
    other_rows = repository.list_by_task(obj.task_id, obj.other_tenant_id)
    owner_rows = repository.list_by_task(obj.task_id, obj.tenant_id)
    return _denied(
        "probe_report_cross_tenant",
        ObjectKind.REPORT,
        production_code=None if not other_rows else "UNEXPECTED_ROWS",
        result_set_size=len(other_rows),
        owner_visible=len(owner_rows),
        decided_by="core.report.repository.ReportRepository.list_by_task",
    )


def _probe_environment(
    client: Any, obj: CrossTenantObjects
) -> tuple[CrossTenantResult, dict[str, Any]]:
    tasks = TaskRepository(client)
    other_row = tasks.get_environment(obj.environment_id, tenant_id=obj.other_tenant_id)
    owner_row = tasks.get_environment(obj.environment_id, tenant_id=obj.tenant_id)
    return _denied(
        "probe_environment_cross_tenant",
        ObjectKind.ENVIRONMENT,
        production_code=None if other_row is None else "UNEXPECTED_ROW",
        result_set_size=0 if other_row is None else 1,
        owner_visible=0 if owner_row is None else 1,
        decided_by="core.tasks.repository.TaskRepository.get_environment",
    )


def _probe_audit(client: Any, obj: CrossTenantObjects) -> tuple[CrossTenantResult, dict[str, Any]]:
    service = AuditService(TaskRepository(client))
    code: str | None = None
    try:
        service.query(tenant_id=obj.other_tenant_id, task_id=obj.task_id, limit=10)
    except DBGuardianError as exc:
        code = exc.code.value
    else:
        raise AssertionError("跨租户查询审计未被拒绝")
    owner_events, _ = service.query(tenant_id=obj.tenant_id, task_id=obj.task_id, limit=10)
    return _denied(
        "probe_audit_cross_tenant",
        ObjectKind.AUDIT,
        production_code=code,
        result_set_size=0,
        owner_visible=len(owner_events),
        decided_by="core.audit.service.AuditService.query",
    )


def _probe_pseudo_task(
    client: Any, obj: CrossTenantObjects
) -> tuple[CrossTenantResult, dict[str, Any]]:
    """伪任务边界：两种事实并存（按 tenant 不可见 / 按 task_id 可见）。"""
    tasks = TaskRepository(client)
    service = AuditService(tasks)

    tenant_visible_code: str | None = None
    tenant_visible_size = 0
    try:
        events, _ = service.query(tenant_id=obj.tenant_id, task_id=PSEUDO_TASK_ID, limit=50)
        tenant_visible_size = len(events)
    except DBGuardianError as exc:
        tenant_visible_code = exc.code.value

    direct_events = tasks.list_events(PSEUDO_TASK_ID, limit=50)
    direct_rows = client.fetch_all(
        "SELECT COUNT(*) AS n FROM dg_system.task_events WHERE task_id = %s",
        (PSEUDO_TASK_ID,),
    )
    direct_total = int(direct_rows[0]["n"]) if direct_rows else 0

    return _denied(
        PSEUDO_TASK_PROBE_ID,
        ObjectKind.AUDIT,
        production_code=tenant_visible_code,
        result_set_size=tenant_visible_size,
        owner_visible=direct_total,
        decided_by="core.audit.service.AuditService.query + TaskRepository.list_events",
        extra={
            "pseudo_task_id": PSEUDO_TASK_ID,
            "tenant_filtered_rows": tenant_visible_size,
            "tenant_filtered_code": tenant_visible_code,
            "direct_task_id_rows_loaded": len(direct_events),
            "direct_task_id_rows_total": direct_total,
        },
    )


# ---------------------------------------------------------------------------
# 六类对象 + 伪任务边界：七个独立探针
# ---------------------------------------------------------------------------


def test_cross_tenant_task_read_is_denied(control_db: Any, objects: CrossTenantObjects) -> None:
    result, evidence = _probe_task(control_db, objects)
    assert result.object_kind is ObjectKind.TASK
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == "TASK_NOT_FOUND"
    assert evidence["owner_tenant_visible"] >= 1, "归属租户读不到任务：探针会假通过"
    assert result.pseudo_task_checked is False


def test_cross_tenant_evidence_read_is_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    result, evidence = _probe_evidence(control_db, objects)
    assert result.object_kind is ObjectKind.EVIDENCE
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == "EVIDENCE_INVALID"
    assert evidence["owner_tenant_visible"] >= 1


def test_cross_tenant_approval_read_is_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    result, evidence = _probe_approval(control_db, objects)
    assert result.object_kind is ObjectKind.APPROVAL
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == "APPROVAL_NOT_FOUND"
    assert evidence["owner_tenant_visible"] >= 1, "跨租户按不存在处理，但归属租户必须可见"


def test_cross_tenant_report_read_is_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    result, evidence = _probe_report(control_db, objects)
    assert result.object_kind is ObjectKind.REPORT
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == EMPTY_RESULT_CODE
    assert evidence["owner_tenant_visible"] >= 1, "报告快照在归属租户必须可读"


def test_cross_tenant_environment_read_is_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    result, evidence = _probe_environment(control_db, objects)
    assert result.object_kind is ObjectKind.ENVIRONMENT
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == EMPTY_RESULT_CODE
    assert evidence["owner_tenant_visible"] == 1


def test_cross_tenant_audit_read_is_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    result, evidence = _probe_audit(control_db, objects)
    assert result.object_kind is ObjectKind.AUDIT
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True
    assert result.result_set_size == 0
    assert result.production_code == "TASK_NOT_FOUND"
    assert evidence["owner_tenant_visible"] >= 1, "归属租户必须读得到本任务事件"


def test_pseudo_task_boundary_records_both_facts(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    """伪任务 ``system:worker_scheduler``：按 tenant 查不到 / 按 task_id 直查可见。"""
    result, evidence = _probe_pseudo_task(control_db, objects)
    assert result.probe_id == PSEUDO_TASK_PROBE_ID
    assert result.pseudo_task_checked is True
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.result_set_size == 0
    assert result.production_code == "TASK_NOT_FOUND"
    # 事实一：按 tenant_id 查询不返回（历史 tenant_id 为 NULL，不 UPDATE）
    assert evidence["tenant_filtered_rows"] == 0
    assert evidence["tenant_filtered_code"] == "TASK_NOT_FOUND"
    # 事实二：按 task_id 直查可见（两者并存，如实记录）
    assert evidence["direct_task_id_rows_total"] >= 1, (
        "伪任务事件不存在：本环境的 tenant_id NULL 边界事实不成立，需如实登记"
    )
    assert evidence["direct_task_id_rows_loaded"] >= 1


def test_all_seven_probes_are_frozen_and_denied(
    control_db: Any, objects: CrossTenantObjects
) -> None:
    probes = [
        _probe_task(control_db, objects),
        _probe_evidence(control_db, objects),
        _probe_approval(control_db, objects),
        _probe_report(control_db, objects),
        _probe_environment(control_db, objects),
        _probe_audit(control_db, objects),
        _probe_pseudo_task(control_db, objects),
    ]
    results = [item[0] for item in probes]
    assert len(results) == CROSS_TENANT_PROBE_COUNT == 7
    assert tuple(result.probe_id for result in results) == CROSS_TENANT_PROBE_IDS
    assert {result.object_kind for result in results} == {
        ObjectKind.TASK,
        ObjectKind.EVIDENCE,
        ObjectKind.APPROVAL,
        ObjectKind.REPORT,
        ObjectKind.ENVIRONMENT,
        ObjectKind.AUDIT,
    }
    for result in results:
        assert result.denied is True, result.probe_id
        assert result.deny_reason == OBJECT_AUTH_DENIED, result.probe_id
        assert result.result_set_size == 0, result.probe_id
        assert result.production_code, result.probe_id

    summary = summarize_cross_tenant(results)
    assert summary["probe_count"] == 7
    assert summary["denied_count"] == 7
    assert summary["empty_result_count"] == 7
    assert summary["pseudo_task_checked"] is True
    assert "TASK_NOT_FOUND" in summary["production_codes"]


# ---------------------------------------------------------------------------
# 归一化不改生产（D-12）
# ---------------------------------------------------------------------------


def test_object_auth_denied_is_not_a_production_error_code() -> None:
    """``core/errors.py`` **未新增** ``OBJECT_AUTH_DENIED``（W11 零生产枚举改动）。"""
    assert "OBJECT_AUTH_DENIED" not in {member.value for member in ErrorCode}
    assert "OBJECT_AUTH_DENIED" not in {member.name for member in ErrorCode}
    source = _ERRORS_SOURCE.read_text(encoding="utf-8")
    assert "OBJECT_AUTH_DENIED" not in source


def test_normalization_preserves_production_code() -> None:
    """生产码原样保留：``TENANT_INVALID`` 仍是 ``TENANT_INVALID``。"""
    tenant_error = TenantInvalidError("跨租户访问", details={"reason": "TENANT_MISMATCH"})
    assert tenant_error.code is ErrorCode.TENANT_INVALID
    result = normalize_object_denial("TENANT_INVALID", 0, probe_id="probe_x")
    assert result.production_code == "TENANT_INVALID"
    assert result.deny_reason == OBJECT_AUTH_DENIED
    assert result.denied is True


def test_normalization_marks_empty_result_without_inventing_code() -> None:
    """"未抛错但空集"必须标注为事实标签，不得编造错误码。"""
    result = normalize_object_denial(None, 0, probe_id="probe_y")
    assert result.production_code == EMPTY_RESULT_CODE
    assert result.production_code not in {member.value for member in ErrorCode}
    assert result.deny_reason == OBJECT_AUTH_DENIED


def test_normalization_rejects_negative_result_size() -> None:
    with pytest.raises(ValueError):
        normalize_object_denial("TASK_NOT_FOUND", -1)


def test_deny_tag_mirror_matches_across_packages() -> None:
    """T01 的镜像常量与 T02 的权威常量必须同值（单向依赖纪律）。"""
    assert CROSS_TENANT_DENY_TAG == OBJECT_AUTH_DENIED


# ---------------------------------------------------------------------------
# 冻结清单：10 个 case_id + 7 个 probe_id
# ---------------------------------------------------------------------------


def test_manifest_freezes_ten_cases_and_seven_probes() -> None:
    manifest = load_concurrency_manifest(_MANIFEST_PATH)
    assert manifest.version == CONCURRENCY_MANIFEST_VERSION == "concurrency-set-v1"
    assert len(manifest.cases) == 10
    assert tuple(manifest.case_ids()) == CONCURRENCY_CASE_IDS
    assert len(set(manifest.case_ids())) == 10
    assert len(manifest.cross_tenant_probes) == CROSS_TENANT_PROBE_COUNT == 7
    assert tuple(manifest.probe_ids()) == CROSS_TENANT_PROBE_IDS
    assert [item.object_kind for item in manifest.cross_tenant_probes].count(
        ObjectKind.AUDIT
    ) == 2  # 审计探针 + 伪任务边界探针
    pseudo = [item for item in manifest.cross_tenant_probes if item.probe_id == PSEUDO_TASK_PROBE_ID]
    assert len(pseudo) == 1
    assert "tenant_id" in pseudo[0].notes and "task_id" in pseudo[0].notes


def test_manifest_rejects_missing_case(tmp_path: Path) -> None:
    payload = load_concurrency_manifest(_MANIFEST_PATH).model_dump(mode="json")
    payload["cases"].pop()
    path = tmp_path / "concurrency_cases.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_concurrency_manifest(path)


def test_manifest_rejects_missing_probe(tmp_path: Path) -> None:
    payload = load_concurrency_manifest(_MANIFEST_PATH).model_dump(mode="json")
    payload["cross_tenant_probes"].pop()
    path = tmp_path / "concurrency_cases.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_concurrency_manifest(path)
