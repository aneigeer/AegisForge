"""W12 T02 集成测试：安全与可靠性验收（AC-05…AC-09 + AC-20 结构保证）。

口径：`docs/w12/01-prd.md` §5、`docs/w12/02-design.md` §4.1/§7/§10、
`docs/w12/03-task-cards.md` T02、`docs/w10-w12-lead-rulings.md` §9（D-03/D-06/D-09）。

覆盖清单：

1. **AC-05** W11 负面集逐条复核：复核条数 == W11 冻结集 **29** 条，逐条 `denied is True`；
   `w11_source_status` 由**实测**决定（W11 已落地，不预设立场写 `NOT_AVAILABLE`）；
2. **AC-06** R3/R4 不进执行器：`not any(d.risk_level == "R4" ...)`（D-06）、
   R3 → `R3_AGENT_EXECUTION_FORBIDDEN`、R4 → `R4_NOT_EXECUTABLE`；
3. **AC-07** 模型 / SQL 注释 / MCP 参数 / UI 四类绕过逐类被拒，且目标行不变；
4. **AC-08** UNKNOWN → `NEEDS_RECONCILIATION`/`MANUAL_INTERVENTION` 且无 `TOOL_CALL_RETRIED`；
   审计失败即 fail closed（`emit_required` 抛 `AuditUnavailableError`，**不接** `try_emit`）；
5. **AC-09** 模型上下文/普通日志/训练集三处不含敏感样本；知识未审核 `is_indexable is False`；
6. **AC-20** `SecurityAcceptanceResult` 注入 `demo_passed` → `ValidationError`。

**复用而非重写**（纪律：禁止重写 W11 负面集逻辑）：AC-05 直接复用 W11 冻结清单
（`benchmarks/w11/negative_cases.json`）与 W11 端口实现（`tests/integration/
test_w11_security_negative.py` 的真实接缝），本文件只做**复核与计数**。

环境边界：控制库不可用 → 整文件 skip 并带原因（`ENV-*` 不判 FAIL）；
本文件不依赖 `DG_ADMIN_DSN`、不依赖 PG 5432、不做 DELETE/UPDATE/TRUNCATE/DROP。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.lab_assembly import build_lab_registry
from adapters.mysql.lab_repair import TOOL_NAME as LAB_WRITE_TOOL_NAME
from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOL_DEFINITIONS
from core.errors import DBGuardianError
from core.release import AcceptanceStatus
from core.release.functional import LAB_ORDER_ID
from core.release.security import (
    BYPASS_CHANNELS,
    DEMO_INPUT_FIELDS,
    GATE_EVIDENCE_REFS,
    REASON_R3_AGENT_EXECUTION_FORBIDDEN,
    REASON_R4_NOT_EXECUTABLE,
    W11_ARTIFACT_PATHS,
    W11_NEGATIVE_CASE_COUNT,
    SecurityAcceptanceResult,
    check_bypass_rejections,
    check_knowledge_indexable_gate,
    check_r4_not_registered,
    check_redaction,
    check_risk_denials,
    probe_fail_closed,
    probe_unknown_outcome,
    review_negative_cases,
    run_security_acceptance,
)
from core.tasks.repository import TaskRepository
from tests.integration.test_w11_security_negative import (  # noqa: F401 - 复用 W11 真实接缝
    _NegativePorts,
    provision_cross_tenant_objects,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOTE_SQL = "SELECT note FROM dg_lab.seed_orders WHERE order_id = %s LIMIT 1"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def control_db(control_client: Any) -> Any:
    """控制库可用性探针：不可用一律 skip（带原因），绝不判 FAIL。"""
    try:
        control_client.fetch_all("SELECT 1 AS ok")
    except DBGuardianError as exc:
        pytest.skip(f"控制库不可用（{exc.code.value}）：{exc.message}")
    return control_client


@pytest.fixture(scope="module")
def lab_registry(control_db: Any, settings: Any) -> Any:
    """经装配点构建 LAB 注册表（含唯一 R2 写工具）。"""
    repository = TaskRepository(control_db)
    environment_row = repository.get_environment(
        settings.default_environment_id, tenant_id=settings.default_tenant_id
    )
    if environment_row is None:
        pytest.skip("默认 MySQL 环境不存在，请先执行 bootstrap_mysql.py")
    return build_lab_registry(None, environment_row, "mysql")


@pytest.fixture(scope="module")
def negative_ports(control_db: Any, settings: Any) -> _NegativePorts:
    """复用 W11 的真实负面集端口（LIVE + STATIC 两类接缝原样装配）。"""
    objects = provision_cross_tenant_objects(control_db)
    return _NegativePorts(client=control_db, settings=settings, objects=objects)


@pytest.fixture(scope="module")
def row_probe(rw_client: Any) -> Any:
    """目标行探针：AC-07 前后目标行必须完全不变（读不到种子行则 skip）。"""

    def probe() -> Any:
        rows = rw_client.fetch_all(_NOTE_SQL, (LAB_ORDER_ID,))
        if not rows:
            pytest.skip("LAB 种子数据 seed_orders 不存在，请先执行 bootstrap_mysql.py")
        return rows[0].get("note")

    return probe


@pytest.fixture(scope="module")
def security_result(
    control_db: Any,
    lab_registry: Any,
    negative_ports: _NegativePorts,
    row_probe: Any,
) -> SecurityAcceptanceResult:
    """整份 T02 验收只跑一次（29 条复核 + 四类绕过 + 两段 fail-closed）。"""
    return run_security_acceptance(
        registry=lab_registry,
        control_client=control_db,
        negative_ports=negative_ports,
        row_probe=row_probe,
    )


# ---------------------------------------------------------------------------
# AC-20：演示 ≠ 验收（结构保证）
# ---------------------------------------------------------------------------


def test_ac20_result_model_has_no_demo_fields() -> None:
    """结构上不存在 demo 字段：常量与 `model_fields` 交集为空（AC-20）。"""
    fields = set(SecurityAcceptanceResult.model_fields)
    assert set(DEMO_INPUT_FIELDS) & fields == set()
    for name in DEMO_INPUT_FIELDS:
        assert name not in fields


@pytest.mark.parametrize("demo_field", DEMO_INPUT_FIELDS)
def test_ac20_demo_field_injection_is_rejected(demo_field: str) -> None:
    """注入 `demo_passed=True`（及另两个 demo 键）必须抛 `ValidationError`。"""
    payload: dict[str, Any] = {
        "run_id": "w12-sec-demo-injection",
        "negative_review": (),
        "r4_not_registered": True,
        "r3_denied": True,
        "bypass_rejections": {name: True for name in BYPASS_CHANNELS},
        "unknown_no_retry": True,
        "fail_closed_ok": True,
        "redaction_ok": True,
        "knowledge_indexable_gate_ok": True,
        "w11_source_status": AcceptanceStatus.PASSED,
        "evidence_refs": ("core/release/security.py",),
        demo_field: True,
    }
    with pytest.raises(ValidationError):
        SecurityAcceptanceResult(**payload)

    payload.pop(demo_field)
    assert SecurityAcceptanceResult(**payload).run_id == "w12-sec-demo-injection"


# ---------------------------------------------------------------------------
# AC-05：W11 负面集逐条复核
# ---------------------------------------------------------------------------


def test_ac05_negative_review_covers_29_frozen_cases(
    security_result: SecurityAcceptanceResult, negative_ports: _NegativePorts
) -> None:
    """复核条数 == W11 冻结集 29 条，逐条 `denied is True`（不放宽任何一条）。"""
    review = security_result.negative_review
    assert len(review) == W11_NEGATIVE_CASE_COUNT, f"复核条数 {len(review)} != 29"
    assert len({item.test_id for item in review}) == W11_NEGATIVE_CASE_COUNT
    for item in review:
        assert item.denied is True, f"{item.test_id} 未被拒绝"
        assert item.source_status is AcceptanceStatus.PASSED, item.test_id
        assert item.evidence_ref.startswith(W11_ARTIFACT_PATHS[0]), item.evidence_ref
    # 复核条数与冻结清单本身一致（不按记忆里的"29"硬断言）。
    from core.assurance.negative import load_negative_set

    manifest = load_negative_set(_REPO_ROOT / W11_ARTIFACT_PATHS[0])
    assert len(review) == len(manifest.cases)
    assert {item.test_id for item in review} == {case.test_id for case in manifest.cases}
    assert negative_ports is not None


def test_ac05_w11_source_status_is_measured_not_assumed(
    security_result: SecurityAcceptanceResult,
) -> None:
    """`w11_source_status` 由实测决定：W11 件齐全且 29 条全拒 → PASSED。"""
    missing = [path for path in W11_ARTIFACT_PATHS if not (_REPO_ROOT / path).is_file()]
    if missing:
        assert security_result.w11_source_status is AcceptanceStatus.NOT_AVAILABLE, missing
    else:
        assert security_result.w11_source_status is AcceptanceStatus.PASSED, missing


def test_ac05_review_declines_to_invent_results_without_ports() -> None:
    """端口缺失时必须 NOT_AVAILABLE + 空复核（**不编造** W11 结果）。"""
    review, status = review_negative_cases(ports=None)
    assert review == ()
    assert status is AcceptanceStatus.NOT_AVAILABLE


def test_ac05_review_declines_to_invent_results_without_manifest(tmp_path: Path) -> None:
    """冻结清单缺失时必须 NOT_AVAILABLE（不判 FAIL）。"""
    review, status = review_negative_cases(
        ports=object(), negative_set_path=tmp_path / "missing.json"
    )
    assert review == ()
    assert status is AcceptanceStatus.NOT_AVAILABLE


# ---------------------------------------------------------------------------
# AC-06：R3/R4 不进执行器
# ---------------------------------------------------------------------------


def test_ac06_no_r4_in_execution_registry(
    security_result: SecurityAcceptanceResult, lab_registry: Any
) -> None:
    """等价断言（`risk_levels_registered` 属性不存在，D-06）。"""
    assert security_result.r4_not_registered is True
    assert check_r4_not_registered(registry=lab_registry) is True
    assert [item.name for item in lab_registry.definitions() if item.risk_level == "R4"] == []
    assert [
        item.name for item in PG_TOOL_DEFINITIONS.values() if item.risk_level == "R4"
    ] == []


def test_ac06_r3_and_r4_are_denied_by_real_policy(
    security_result: SecurityAcceptanceResult,
) -> None:
    """R3 → `R3_AGENT_EXECUTION_FORBIDDEN`（`policy.py:225`）；R4 拒绝符号在 `:218`。"""
    denials = check_risk_denials()
    assert denials["r3_denied"] is True
    assert denials["r4_denied"] is True
    assert security_result.r3_denied is True

    policy_source = (_REPO_ROOT / "core" / "security" / "policy.py").read_text(encoding="utf-8")
    assert f'reason="{REASON_R4_NOT_EXECUTABLE}"' in policy_source
    assert f'reason="{REASON_R3_AGENT_EXECUTION_FORBIDDEN}"' in policy_source


# ---------------------------------------------------------------------------
# AC-07：四类绕过被拒（目标行不变）
# ---------------------------------------------------------------------------


def test_ac07_four_bypass_channels_rejected_and_target_unchanged(
    security_result: SecurityAcceptanceResult,
    lab_registry: Any,
    row_probe: Any,
) -> None:
    """模型 / SQL 注释 / MCP 参数 / UI 各 1 例被拒，且目标行完全不变。"""
    assert security_result.bypass_rejections == {
        name: True for name in BYPASS_CHANNELS
    }, security_result.bypass_rejections

    before = row_probe()
    verdicts = check_bypass_rejections(registry=lab_registry, row_probe=row_probe)
    assert verdicts == {name: True for name in BYPASS_CHANNELS}, verdicts
    assert row_probe() == before, "四类绕过不得改动目标行"


def test_ac07_model_bypass_requires_a_write_tool(lab_registry: Any) -> None:
    """模型通道断言的可证伪性：注册表里确有唯一写工具，才可能被诱导。"""
    writes = [item for item in lab_registry.definitions() if item.side_effect == "write"]
    assert [item.name for item in writes] == [LAB_WRITE_TOOL_NAME]
    verdicts = check_bypass_rejections(registry=lab_registry)
    assert verdicts["model"] is True


# ---------------------------------------------------------------------------
# AC-08：未知不重试 + 审计失败 fail closed
# ---------------------------------------------------------------------------


def test_ac08_unknown_outcome_is_not_retried(control_db: Any) -> None:
    """UNKNOWN → 核对状态（NEEDS_RECONCILIATION/MANUAL_INTERVENTION），零重试。"""
    observation = probe_unknown_outcome(control_client=control_db)
    if observation.status is AcceptanceStatus.NOT_EXECUTED:
        pytest.skip(f"控制库阻塞：{observation.detail}")
    assert observation.status is AcceptanceStatus.PASSED, observation.detail
    assert observation.conclusion in {"NEEDS_RECONCILIATION", "MANUAL_INTERVENTION"}
    assert observation.tool_calls_delta == 0, observation.detail
    assert observation.retried_events == 0, "恢复期绝不重放（TOOL_CALL_RETRIED 必须为 0）"


def test_ac08_audit_failure_blocks_execution(control_db: Any, lab_registry: Any) -> None:
    """审计写不进去 → `emit_required` 抛 `AuditUnavailableError`，链上零 tool_calls 行。"""
    ok, detail = probe_fail_closed(control_client=control_db, registry=lab_registry)
    assert ok is True, detail
    assert "emit_required_raised=True" in detail
    assert "try_emit_swallowed=False" in detail
    assert "tool_calls_delta=0" in detail


def test_ac08_fail_closed_is_required_not_best_effort() -> None:
    """方向证据：R2 链用 `emit_required`；`try_emit` 只在告警降级路径被允许。"""
    service_source = (_REPO_ROOT / "core" / "audit" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "raise AuditUnavailableError" in service_source
    assert "def emit_required" in service_source
    assert "def try_emit" in service_source
    executor_source = (_REPO_ROOT / "core" / "tools" / "executor.py").read_text(
        encoding="utf-8"
    )
    assert "self._audit.emit_required(" in executor_source
    assert "self._audit.try_emit(" not in executor_source, (
        "R2 执行链不得接 try_emit（W11 D-03）"
    )


# ---------------------------------------------------------------------------
# AC-09：敏感数据不越界
# ---------------------------------------------------------------------------


def test_ac09_redaction_covers_three_boundaries(security_result: SecurityAcceptanceResult) -> None:
    """模型上下文 / 普通日志 / 训练集三处不含敏感样本。"""
    assert security_result.redaction_ok is True
    assert check_redaction() is True


def test_ac09_unreviewed_knowledge_is_not_indexable(
    security_result: SecurityAcceptanceResult,
) -> None:
    """知识未审核 → `is_indexable is False`（门禁是判别式，不是恒假）。"""
    assert security_result.knowledge_indexable_gate_ok is True
    assert check_knowledge_indexable_gate() is True


# ---------------------------------------------------------------------------
# 整份结果件
# ---------------------------------------------------------------------------


def test_security_result_is_complete_and_honest(
    security_result: SecurityAcceptanceResult,
) -> None:
    """字段齐全、无 demo 字段、证据引用路径真实存在、W11 源状态如实。"""
    assert security_result.run_id.startswith("w12-sec-")
    assert len(security_result.negative_review) == W11_NEGATIVE_CASE_COUNT
    assert set(security_result.bypass_rejections) == set(BYPASS_CHANNELS)
    assert security_result.w11_source_status in {
        AcceptanceStatus.PASSED,
        AcceptanceStatus.FAILED,
        AcceptanceStatus.NOT_AVAILABLE,
    }

    assert security_result.evidence_refs
    for relative in security_result.evidence_refs:
        assert (_REPO_ROOT / relative).is_file(), f"证据引用不存在：{relative}"
    for gate_ref in GATE_EVIDENCE_REFS:
        assert gate_ref in security_result.evidence_refs

    dumped = security_result.model_dump()
    for name in DEMO_INPUT_FIELDS:
        assert name not in dumped
