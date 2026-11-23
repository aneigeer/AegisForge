"""Evidence 仓储/服务集成测试（真实 MySQL 控制库，W03）。"""

from __future__ import annotations

import pytest

from core.errors import DatabaseQueryError, EvidenceInvalidError, TenantInvalidError
from core.evidence.models import (
    CollectionStatus,
    EvidenceCreate,
    EvidenceQuality,
    ReportFacts,
)
from core.evidence.repository import EvidenceRepository
from core.evidence.service import EvidenceService, canonical_payload_hash
from core.tasks.repository import TaskRepository
from core.tasks.service import TaskService
from core.tools.registry import ToolRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
def task_repository(control_client) -> TaskRepository:
    return TaskRepository(control_client)


@pytest.fixture
def task_service(task_repository: TaskRepository) -> TaskService:
    return TaskService(task_repository, ToolRegistry())


@pytest.fixture
def evidence_repository(control_client) -> EvidenceRepository:
    return EvidenceRepository(control_client)


@pytest.fixture
def evidence_service(
    evidence_repository: EvidenceRepository, task_repository: TaskRepository
) -> EvidenceService:
    return EvidenceService(evidence_repository, task_repository)


@pytest.fixture(autouse=True)
def _cleanup_created_tasks(control_client, monkeypatch):
    """W09 纪律：需要 DB 的用例只追加写入、不清理，历史残留行继续保留。

    参照 tests/integration/test_w09_redaction_pipeline.py:5-10：本夹具不再请求
    管理员账号夹具（该路径在本机建连阶段认证失败），teardown 不发出任何删除
    语句；只以只读方式核对「本用例自己新建任务」范围内的证据行数
    （``count_for_task``），不断言全表为空、不触碰任何历史残留行。
    """
    created: list[tuple[str, str]] = []
    written: dict[str, int] = {}
    original_create_task = TaskService.create_task
    original_create_evidence = EvidenceService.create

    def tracked_create_task(self, goal, **kwargs):
        task = original_create_task(self, goal, **kwargs)
        created.append((task.task_id, task.tenant_id))
        return task

    def tracked_create_evidence(self, request, **kwargs):
        evidence = original_create_evidence(self, request, **kwargs)
        written[evidence.task_id] = written.get(evidence.task_id, 0) + 1
        return evidence

    monkeypatch.setattr(TaskService, "create_task", tracked_create_task)
    monkeypatch.setattr(EvidenceService, "create", tracked_create_evidence)
    yield
    repository = EvidenceRepository(control_client)
    for task_id, tenant_id in created:
        counted = repository.count_for_task(task_id, tenant_id)
        assert counted == written.get(task_id, 0), (
            f"本用例新建任务 {task_id} 的证据行只读计数与写入数不一致："
            f"count_for_task={counted}，写入数={written.get(task_id, 0)}"
        )


def _make_request(task, **overrides) -> EvidenceCreate:
    values = {
        "tenant_id": task.tenant_id,
        "task_id": task.task_id,
        "environment_id": task.environment_id,
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "adapter_version": "w03-integration",
        "source_tool": "get_db_version",
        "observed_at": "2026-02-01T00:00:00+00:00",
        "collection_status": CollectionStatus.SUCCESS,
        "payload": {"rows": [{"id": 1}], "nested": {"b": 2, "a": 1}},
        "time_window_start": "2026-01-31T23:59:00+00:00",
        "time_window_end": "2026-02-01T00:00:00+00:00",
    }
    values.update(overrides)
    return EvidenceCreate(**values)


def test_create_list_get_roundtrip(evidence_service: EvidenceService, task_service):
    task = task_service.create_task("Evidence 往返")
    request = _make_request(task, redacted_payload={"rows": [{"id": "***"}]})

    evidence = evidence_service.create(request)

    assert evidence.evidence_id.startswith("ev_")
    assert evidence.tenant_id == task.tenant_id
    assert evidence.task_id == task.task_id
    assert evidence.payload_hash == canonical_payload_hash(request.payload)
    assert evidence.redacted_payload == {"rows": [{"id": "***"}]}
    assert evidence.observed_at == "2026-02-01T00:00:00+00:00"

    stored = evidence_service.get(evidence.evidence_id, task.tenant_id)
    assert stored.evidence_id == evidence.evidence_id
    assert stored.payload == request.payload
    assert stored.redacted_payload == request.redacted_payload

    items, next_cursor = evidence_service.list(task.task_id, task.tenant_id)
    assert [item.evidence_id for item in items] == [evidence.evidence_id]
    assert next_cursor is None

    with pytest.raises(EvidenceInvalidError):
        evidence_service.get(evidence.evidence_id, "tenant_absent")


def test_create_rejects_cross_tenant_and_environment_mismatch(
    evidence_service: EvidenceService, task_service
):
    task = task_service.create_task("Evidence 归属校验")

    with pytest.raises(TenantInvalidError):
        evidence_service.create(_make_request(task, tenant_id="tenant_absent"))

    with pytest.raises(EvidenceInvalidError):
        evidence_service.create(
            _make_request(task, environment_id="env_pg_local")
        )

    with pytest.raises(TenantInvalidError):
        evidence_service.create(
            _make_request(task, environment_id="env_not_exists")
        )


def test_reference_validation_cross_task_tenant_environment(
    evidence_service: EvidenceService,
    evidence_repository: EvidenceRepository,
    task_service,
):
    task = task_service.create_task("引用校验-任务一")
    other = task_service.create_task("引用校验-任务二")
    evidence = evidence_service.create(_make_request(task))
    other_evidence = evidence_service.create(_make_request(other))

    assert (
        evidence_repository.validate_references(
            task.task_id, task.tenant_id, task.environment_id, [evidence.evidence_id]
        )
        == []
    )
    assert evidence_repository.validate_references(
        task.task_id, "tenant_absent", task.environment_id, [evidence.evidence_id]
    ) == [evidence.evidence_id]
    assert evidence_repository.validate_references(
        task.task_id, task.tenant_id, task.environment_id, [other_evidence.evidence_id]
    ) == [other_evidence.evidence_id]
    assert evidence_repository.validate_references(
        task.task_id, task.tenant_id, "env_pg_local", [evidence.evidence_id]
    ) == [evidence.evidence_id]
    assert evidence_repository.count_for_task(task.task_id, task.tenant_id) == 1


def test_report_facts_rejects_foreign_evidence_and_ignores_missing(
    evidence_service: EvidenceService, task_service
):
    task = task_service.create_task("报告事实校验")
    other = task_service.create_task("报告事实校验-其他任务")
    evidence = evidence_service.create(_make_request(task))
    foreign = evidence_service.create(_make_request(other))

    facts = ReportFacts(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        supporting_evidence_ids=[evidence.evidence_id, foreign.evidence_id],
        contradicting_evidence_ids=[foreign.evidence_id],
        missing_evidence=["ev_not_collected"],
    )
    assert evidence_service.validate_report_facts(facts) == [foreign.evidence_id]

    missing_only = ReportFacts(
        tenant_id=task.tenant_id,
        task_id=task.task_id,
        missing_evidence=["ev_not_collected"],
    )
    assert evidence_service.validate_report_facts(missing_only) == []


def test_quality_constraint_for_degraded_status(
    evidence_service: EvidenceService, task_service
):
    task = task_service.create_task("Evidence 质量约束")

    with pytest.raises(EvidenceInvalidError):
        evidence_service.create(
            _make_request(
                task,
                collection_status=CollectionStatus.DENIED,
                quality=EvidenceQuality.HIGH,
            )
        )

    denied = evidence_service.create(
        _make_request(task, collection_status=CollectionStatus.DENIED)
    )
    assert denied.collection_status == CollectionStatus.DENIED
    assert denied.quality == EvidenceQuality.UNKNOWN


def test_cursor_pagination_has_no_duplicates(
    evidence_service: EvidenceService, task_service
):
    task = task_service.create_task("Evidence 游标分页")
    created = [
        evidence_service.create(_make_request(task, payload={"seq": index}))
        for index in range(5)
    ]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        items, cursor = evidence_service.list(task.task_id, task.tenant_id, 2, cursor)
        seen.extend(item.evidence_id for item in items)
        pages += 1
        if cursor is None:
            break

    assert pages >= 3
    assert len(seen) == len(set(seen)) == len(created)
    assert set(seen) == {item.evidence_id for item in created}
