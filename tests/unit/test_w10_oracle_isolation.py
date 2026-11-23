"""W10 T01｜Oracle 隔离底座静态与行为断言（**零 DB**）。

覆盖任务卡 ``03-task-cards.md:87-97`` 的 S1…S10 中可在单测判定的部分，
以及设计 ``02-design.md`` §9.1 静态检查表 S1…S6：

- S1 导入白名单 / S2 产品路径零引用 / S3 文本扫描（只回字段名） /
  S4 冻结资产四标志 / S5 终态闸门 / S6 单次读取 + records + 运行产物无真值；
- S7 审计留痕与"不可写不阻塞" / S8 非终态边界 / S9 不复制 store 逻辑 /
  S10 的三态与审计行在 ``tests/integration/test_w10_oracle_timing.py`` 复验。

纪律：不连库、不依赖 ``DG_ADMIN_DSN`` / PG 5432；冻结资产只读；
不修改 ``core/faults/**``；不放宽断言、不加 skip/xfail。
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import core.benchmark as benchmark_package
from core.audit.events import AuditEventType
from core.benchmark import BENCHMARK_PACKAGE_VERSION, oracle as oracle_module
from core.benchmark.oracle import (
    ALERT_ORACLE_AUDIT_UNAVAILABLE,
    ORACLE_FORBIDDEN_FIELDS,
    ORACLE_IMPORT_ALLOWLIST,
    REASON_ALREADY_READ,
    REASON_NOT_TERMINAL,
    REASON_TERMINAL_OK,
    OracleAccessDenied,
    OracleAccessRecord,
    OracleGate,
    OracleLeakDetected,
    OraclePort,
    assert_no_oracle_leak,
    audit_oracle_imports,
    scan_texts_for_oracle_leak,
)
from core.errors import BenchmarkInvalidError, ErrorCode
from core.faults.store import load_oracle
from core.tasks.models import TERMINAL_STATUSES, TaskStatus
from core.tasks.state_machine import is_terminal

ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = ROOT / "benchmarks" / "baseline_v0"
PROMPT_PATH = BASELINE_DIR / "prompt.txt"
TESTSET_PATH = BASELINE_DIR / "testset_manifest.json"

PRODUCT_PATHS: tuple[str, ...] = (
    "apps",
    "workers",
    "core/agent",
    "core/context",
    "core/diagnosis",
    "core/report",
    "core/memory",
)
ORACLE_REFERENCE_TOKENS: tuple[str, ...] = ("load_oracle", "OracleTruth")
TRUTH_MARKER = "ORACLE_TRUTH_VALUE_9f2c_DO_NOT_LEAK"
FORBIDDEN_TRUTH_LITERALS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
)


@dataclass(frozen=True)
class _FakeTruth:
    """测试桩：模拟 loader 返回的真值对象（只活在测试局部变量里）。"""

    scenario_id: str
    expected_root_cause: str


class _StatusReader:
    """记录调用的状态读取桩；证明闸门自己读状态而非信调用方。"""

    def __init__(self, status: TaskStatus) -> None:
        self.status = status
        self.calls: list[str] = []

    def __call__(self, task_id: str) -> TaskStatus:
        self.calls.append(task_id)
        return self.status

    def set(self, status: TaskStatus) -> None:
        self.status = status


class _Loader:
    def __init__(self, marker: str = TRUTH_MARKER) -> None:
        self.marker = marker
        self.calls: list[str] = []

    def __call__(self, scenario_id: str) -> _FakeTruth:
        self.calls.append(scenario_id)
        return _FakeTruth(scenario_id=scenario_id, expected_root_cause=self.marker)


class _RecordingAudit:
    """组合根形态的审计桩：带 tenant_id，可模拟不可写。"""

    def __init__(self, *, mode: str = "ok", tenant_id: str = "tenant_local") -> None:
        self.mode = mode
        self.tenant_id = tenant_id
        self.events: list[Any] = []
        self.tenant_ids: list[str | None] = []

    def try_emit(
        self,
        event: Any,
        *,
        tenant_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> bool:
        self.events.append(event)
        self.tenant_ids.append(tenant_id)
        if self.mode == "raise":
            raise RuntimeError("audit ledger unavailable")
        if self.mode == "false":
            return False
        return True


def _gate(
    status: TaskStatus,
    *,
    audit: Any | None = None,
    loader: Any | None = None,
) -> tuple[OracleGate, _StatusReader, _Loader]:
    reader = _StatusReader(status)
    fake_loader = loader if loader is not None else _Loader()
    gate = OracleGate(status_reader=reader, audit=audit, loader=fake_loader)
    return gate, reader, fake_loader


def _call_read(gate: OracleGate, scenario_id: str = "mysql_slow_query") -> Any:
    return gate.read(scenario_id, task_id="task_w10_t01", run_id="run_w10_t01")


# --------------------------------------------------------------------------- #
# 包与契约
# --------------------------------------------------------------------------- #


def test_package_version_and_offline_tooling_docstring() -> None:
    assert BENCHMARK_PACKAGE_VERSION == "w10-v1"
    package_doc = inspect.getdoc(benchmark_package) or ""
    assert "离线评测工具" in package_doc
    assert "不进生产镜像" in package_doc
    assert "不新增 API 端点" in package_doc
    assert (ROOT / "core" / "benchmark" / "__init__.py").is_file()


def test_error_codes_and_exception_status_codes_are_appended() -> None:
    assert ErrorCode.ORACLE_ACCESS_DENIED.value == "ORACLE_ACCESS_DENIED"
    assert ErrorCode.ORACLE_LEAK_DETECTED.value == "ORACLE_LEAK_DETECTED"
    assert ErrorCode.BENCHMARK_INVALID.value == "BENCHMARK_INVALID"
    assert OracleAccessDenied("x").default_status_code == 403
    assert OracleLeakDetected("x").default_status_code == 400
    assert BenchmarkInvalidError("x").default_status_code == 400
    assert OracleAccessDenied("x").code is ErrorCode.ORACLE_ACCESS_DENIED
    assert OracleLeakDetected("x").code is ErrorCode.ORACLE_LEAK_DETECTED


def test_audit_event_types_are_appended() -> None:
    assert AuditEventType.BENCHMARK_RUN_STARTED.value == "BENCHMARK_RUN_STARTED"
    assert AuditEventType.BENCHMARK_RUN_FINISHED.value == "BENCHMARK_RUN_FINISHED"
    assert AuditEventType.ORACLE_READ.value == "ORACLE_READ"
    assert AuditEventType.ORACLE_ACCESS_DENIED.value == "ORACLE_ACCESS_DENIED"


def test_oracle_port_contract_has_no_caller_supplied_status() -> None:
    port_params = set(inspect.signature(OraclePort.read).parameters)
    gate_params = set(inspect.signature(OracleGate.read).parameters)
    assert port_params == {"self", "scenario_id", "task_id", "run_id"}
    assert gate_params == port_params
    assert "status" not in gate_params
    assert "task_status" not in gate_params
    assert set(inspect.signature(OracleGate.__init__).parameters) == {
        "self",
        "status_reader",
        "audit",
        "loader",
    }


def test_no_enabled_or_strict_switch_exists() -> None:
    gate, _, _ = _gate(TaskStatus.COMPLETED)
    assert not hasattr(gate, "enabled")
    assert not hasattr(gate, "strict")


# --------------------------------------------------------------------------- #
# S1 导入白名单
# --------------------------------------------------------------------------- #


def test_s1_import_allowlist_is_exact() -> None:
    assert ORACLE_IMPORT_ALLOWLIST == frozenset(
        {
            "core/benchmark/oracle.py",
            "core/faults/store.py",
            "core/faults/models.py",
            "core/faults/__init__.py",
            "scripts/fault_lab.py",
        }
    )
    assert ORACLE_FORBIDDEN_FIELDS == ("expected_root_cause", "fault_seed")


def test_s1_audit_oracle_imports_finds_no_violation_in_repo() -> None:
    assert audit_oracle_imports(ROOT) == ()


def test_s1_audit_detects_references_and_excludes_test_and_data_dirs(
    tmp_path: Path,
) -> None:
    (tmp_path / "prod").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "benchmarks" / "data").mkdir(parents=True)
    (tmp_path / "prod" / "leak.py").write_text(
        "from core.faults.store import load_oracle\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_ok.py").write_text(
        "from core.faults.store import load_oracle\n", encoding="utf-8"
    )
    # benchmarks/** 只放数据：docstring 提及符号名不算读取逻辑。
    (tmp_path / "benchmarks" / "data" / "README.py").write_text(
        '"""数据资产说明：core.faults.store.load_oracle 是唯一读取入口。"""\n',
        encoding="utf-8",
    )
    assert audit_oracle_imports(tmp_path) == ("prod/leak.py",)

    # 但 benchmarks/** 一旦出现真实读取逻辑即计入违规（S1 后半条）。
    (tmp_path / "benchmarks" / "data" / "reader.py").write_text(
        "from core.faults.store import load_oracle\n"
        "def read():\n"
        '    return load_oracle("mysql_slow_query")\n',
        encoding="utf-8",
    )
    assert audit_oracle_imports(tmp_path) == (
        "benchmarks/data/reader.py",
        "prod/leak.py",
    )


def test_s1_benchmarks_directory_has_no_oracle_read_logic() -> None:
    """冻结资产目录只放数据；所有 .py 都不得含 import / 调用型读取逻辑。"""

    benchmark_py = sorted((ROOT / "benchmarks").rglob("*.py"))
    assert benchmark_py, "冻结资产目录应至少有 .py 文件参与扫描"
    for path in benchmark_py:
        text = path.read_text(encoding="utf-8")
        assert not oracle_module._has_oracle_read_logic(path, text), path.as_posix()


# --------------------------------------------------------------------------- #
# S2 产品路径零引用
# --------------------------------------------------------------------------- #


def test_s2_product_paths_have_zero_oracle_references() -> None:
    for relative in PRODUCT_PATHS:
        directory = ROOT / relative
        assert directory.is_dir(), relative
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            hits = [token for token in ORACLE_REFERENCE_TOKENS if token in text]
            assert hits == [], f"{path.relative_to(ROOT).as_posix()} 命中 {hits}"


# --------------------------------------------------------------------------- #
# S3 文本扫描
# --------------------------------------------------------------------------- #


def test_s3_scan_returns_only_field_names_never_original_text() -> None:
    leaked = (
        "训练样本：expected_root_cause=缺少复合索引；fault_seed=17；"
        "原文片段=缺少复合索引；秘密上下文=锁等待超时"
    )
    result = scan_texts_for_oracle_leak([leaked])
    assert result == ("expected_root_cause", "fault_seed")
    assert set(result).issubset(set(ORACLE_FORBIDDEN_FIELDS))
    assert "缺少复合索引" not in "".join(result)
    assert "锁等待超时" not in "".join(result)
    assert "17" not in "".join(result)


def test_s3_scan_is_case_insensitive_and_deduplicates() -> None:
    texts = ["EXPECTED_ROOT_CAUSE", "Fault_Seed", "expected_root_cause", "fault_seed"]
    assert scan_texts_for_oracle_leak(texts) == ("expected_root_cause", "fault_seed")
    assert scan_texts_for_oracle_leak([]) == ()
    assert scan_texts_for_oracle_leak(["完全干净的样本", "另一条干净样本"]) == ()


def test_s3_frozen_prompt_and_clean_training_samples_are_clear() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    samples = [
        "样本 A：观测到慢查询，建议检查索引与执行计划。",
        "样本 B：连接数上升，需补充应用侧池指标。",
        json.dumps(
            {"sample_id": "s-001", "label": "REVIEWED", "rca_match": True},
            ensure_ascii=False,
        ),
    ]
    texts = [prompt, *samples]
    assert scan_texts_for_oracle_leak(texts) == ()
    assert_no_oracle_leak(texts)  # 不抛即通过


def test_s3_assert_no_oracle_leak_raises_without_original_text() -> None:
    sample = f"构造训练样本 fault_seed=42 且含原文 {TRUTH_MARKER}"
    with pytest.raises(OracleLeakDetected) as caught:
        assert_no_oracle_leak([sample])
    exc = caught.value
    assert exc.code is ErrorCode.ORACLE_LEAK_DETECTED
    assert exc.status_code == 400
    assert exc.details == {"leaked_fields": ["fault_seed"]}
    assert TRUTH_MARKER not in json.dumps(exc.details, ensure_ascii=False)
    assert TRUTH_MARKER not in exc.message
    assert "42" not in exc.message


def test_s3_leaked_sample_with_expected_root_cause_is_detected() -> None:
    """设计 §9.1 S5：构造含 fault_seed 的样本必被检出。"""

    sample = "{\"sample_id\": \"s-002\", \"fault_seed\": 7, \"expected_root_cause\": \"x\"}"
    assert scan_texts_for_oracle_leak([sample]) == (
        "expected_root_cause",
        "fault_seed",
    )
    with pytest.raises(OracleLeakDetected) as caught:
        assert_no_oracle_leak([sample])
    assert caught.value.details["leaked_fields"] == [
        "expected_root_cause",
        "fault_seed",
    ]


# --------------------------------------------------------------------------- #
# S4 冻结资产四标志
# --------------------------------------------------------------------------- #


def test_s4_testset_manifest_oracle_isolation_four_flags_are_false() -> None:
    testset = json.loads(TESTSET_PATH.read_text(encoding="utf-8"))
    flags = testset["split"]["oracle_isolation"]
    for name in (
        "oracle_visible_to_agent",
        "oracle_visible_to_model",
        "oracle_in_logs",
        "oracle_in_training_samples",
    ):
        assert flags[name] is False, f"{name} 必须为 false"
    assert flags["enabled"] is True  # 隔离启用，四标志是"外泄面"而非开关


# --------------------------------------------------------------------------- #
# S5 终态闸门
# --------------------------------------------------------------------------- #


def test_s5_read_queries_status_reader_itself_and_denies_non_terminal() -> None:
    gate, reader, loader = _gate(TaskStatus.EXECUTING)
    with pytest.raises(OracleAccessDenied) as caught:
        _call_read(gate)
    assert caught.value.code is ErrorCode.ORACLE_ACCESS_DENIED
    assert caught.value.status_code == 403
    assert caught.value.details["reason"] == REASON_NOT_TERMINAL
    assert caught.value.details["task_status"] == "EXECUTING"
    assert reader.calls == ["task_w10_t01"], "状态必须由闸门自己读"
    assert loader.calls == [], "非终态时 loader 绝不能被调用"


def test_s5_callers_cannot_forge_status() -> None:
    gate, _, _ = _gate(TaskStatus.EXECUTING)
    with pytest.raises(TypeError):
        gate.read(
            "mysql_slow_query",
            task_id="task_w10_t01",
            run_id="run_w10_t01",
            task_status=TaskStatus.COMPLETED,
        )


@pytest.mark.parametrize(
    "status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
)
def test_s5_first_read_on_terminal_status_is_allowed(status: TaskStatus) -> None:
    assert is_terminal(status) is True
    assert status in TERMINAL_STATUSES
    gate, _, loader = _gate(status)
    truth = _call_read(gate)
    assert truth.scenario_id == "mysql_slow_query"
    assert truth.expected_root_cause == TRUTH_MARKER
    assert loader.calls == ["mysql_slow_query"]
    records = gate.records
    assert len(records) == 1
    assert records[0].allowed is True
    assert records[0].reason == REASON_TERMINAL_OK
    assert records[0].task_status == status.value


# --------------------------------------------------------------------------- #
# S8 非终态边界（NEEDS_RECONCILIATION / MANUAL_INTERVENTION）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.NEEDS_RECONCILIATION,
        TaskStatus.MANUAL_INTERVENTION,
        TaskStatus.TIMED_OUT,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
    ],
)
def test_s8_non_terminal_states_are_denied(status: TaskStatus) -> None:
    assert status not in TERMINAL_STATUSES
    assert is_terminal(status) is False
    gate, _, loader = _gate(status)
    with pytest.raises(OracleAccessDenied) as caught:
        _call_read(gate)
    assert caught.value.details["reason"] == REASON_NOT_TERMINAL
    assert loader.calls == []


def test_s8_non_terminal_rationale_is_written_in_source_comment() -> None:
    source = (ROOT / "core" / "benchmark" / "oracle.py").read_text(encoding="utf-8")
    assert "NEEDS_RECONCILIATION" in source
    assert "MANUAL_INTERVENTION" in source
    assert "不是终态" in source


# --------------------------------------------------------------------------- #
# S6 单次闸门 / records / 运行产物
# --------------------------------------------------------------------------- #


def test_s6_second_read_is_denied_with_already_read() -> None:
    gate, _, loader = _gate(TaskStatus.COMPLETED)
    assert _call_read(gate).expected_root_cause == TRUTH_MARKER
    with pytest.raises(OracleAccessDenied) as caught:
        _call_read(gate)
    assert caught.value.details["reason"] == REASON_ALREADY_READ
    assert loader.calls == ["mysql_slow_query"], "第二次读取不得再调 loader"


def test_s6_records_keep_all_allowed_and_refused_decisions() -> None:
    gate, reader, loader = _gate(TaskStatus.EXECUTING)
    with pytest.raises(OracleAccessDenied):
        _call_read(gate)
    reader.set(TaskStatus.COMPLETED)
    _call_read(gate)
    with pytest.raises(OracleAccessDenied):
        _call_read(gate)

    records = gate.records
    assert isinstance(records, tuple)
    assert len(records) == 3
    assert all(isinstance(item, OracleAccessRecord) for item in records)
    assert [item.reason for item in records] == [
        REASON_NOT_TERMINAL,
        REASON_TERMINAL_OK,
        REASON_ALREADY_READ,
    ]
    assert [item.allowed for item in records] == [False, True, False]
    assert [item.task_status for item in records] == [
        "EXECUTING",
        "COMPLETED",
        "COMPLETED",
    ]
    assert all(item.read_at for item in records)
    assert all(item.task_id == "task_w10_t01" for item in records)
    assert all(item.run_id == "run_w10_t01" for item in records)
    assert loader.calls == ["mysql_slow_query"]


def test_s6_single_read_gate_has_no_extra_relaxation() -> None:
    gate, _, loader = _gate(TaskStatus.COMPLETED)
    _call_read(gate)
    for _attempt in range(3):
        with pytest.raises(OracleAccessDenied) as caught:
            _call_read(gate)
        assert caught.value.details["reason"] == REASON_ALREADY_READ
    assert len(loader.calls) == 1
    # 闸门按 (run_id, scenario_id) 计次：不同 run / 不同 scenario 各允许一次，
    # 但**不是**"同一任务可读 3 次"那种额外放开。
    gate.read("mysql_slow_query", task_id="task_w10_t01", run_id="run_other")
    gate.read("pg_lock_wait", task_id="task_w10_t01", run_id="run_w10_t01")
    assert len(loader.calls) == 3
    with pytest.raises(OracleAccessDenied):
        gate.read("pg_lock_wait", task_id="task_w10_t01", run_id="run_w10_t01")


def test_s6_serialized_run_artifact_contains_no_truth_fields() -> None:
    """设计 §9.1 S6：运行产物（cases.jsonl 形态）不得出现真值字段字面。"""

    case_line = json.dumps(
        {
            "case_id": "mysql-slow_query-1",
            "engine": "mysql",
            "scenario": "slow_query",
            "execution_mode": "HARNESS",
            "ranked_root_causes": ["missing_index"],
            "rca_match": True,
            "task_status": "COMPLETED",
        },
        ensure_ascii=False,
    )
    artifact = case_line + "\n"
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in artifact
    assert TRUTH_MARKER not in artifact
    assert scan_texts_for_oracle_leak([artifact]) == ()
    assert_no_oracle_leak([artifact])


# --------------------------------------------------------------------------- #
# S7 审计留痕
# --------------------------------------------------------------------------- #


def test_s7_audit_emitted_for_every_allow_and_deny() -> None:
    audit = _RecordingAudit(tenant_id="tenant_w10_t01")
    gate, reader, _ = _gate(TaskStatus.EXECUTING, audit=audit)
    with pytest.raises(OracleAccessDenied):
        _call_read(gate)
    reader.set(TaskStatus.COMPLETED)
    _call_read(gate)
    with pytest.raises(OracleAccessDenied):
        _call_read(gate)

    assert len(audit.events) == 3
    assert [event.event_type for event in audit.events] == [
        AuditEventType.ORACLE_ACCESS_DENIED.value,
        AuditEventType.ORACLE_READ.value,
        AuditEventType.ORACLE_ACCESS_DENIED.value,
    ]
    assert audit.tenant_ids == ["tenant_w10_t01"] * 3
    assert [event.payload["reason"] for event in audit.events] == [
        REASON_NOT_TERMINAL,
        REASON_TERMINAL_OK,
        REASON_ALREADY_READ,
    ]
    for event in audit.events:
        payload_text = json.dumps(event.payload, ensure_ascii=False)
        assert set(event.payload).issubset(
            {"scenario_id", "task_id", "run_id", "task_status", "reason", "allowed"}
        )
        for forbidden in FORBIDDEN_TRUTH_LITERALS:
            assert forbidden not in payload_text
        assert TRUTH_MARKER not in payload_text


@pytest.mark.parametrize("mode", ["raise", "false", "none"])
def test_s7_audit_unavailable_never_blocks_but_alerts(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    audit = None if mode == "none" else _RecordingAudit(mode=mode)
    gate, _, loader = _gate(TaskStatus.COMPLETED, audit=audit)
    truth = _call_read(gate)
    assert truth.expected_root_cause == TRUTH_MARKER
    assert loader.calls == ["mysql_slow_query"]
    alerts = [getattr(record, "alert", None) for record in caplog.records]
    assert ALERT_ORACLE_AUDIT_UNAVAILABLE in alerts
    assert "ORACLE_AUDIT_UNAVAILABLE" == ALERT_ORACLE_AUDIT_UNAVAILABLE


def test_s7_denial_details_never_contain_truth_values() -> None:
    gate, _, _ = _gate(TaskStatus.EXECUTING, audit=_RecordingAudit(mode="raise"))
    with pytest.raises(OracleAccessDenied) as caught:
        _call_read(gate)
    details_text = json.dumps(caught.value.details, ensure_ascii=False)
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in details_text
    assert TRUTH_MARKER not in details_text
    assert TRUTH_MARKER not in caught.value.message


# --------------------------------------------------------------------------- #
# S9 不复制 store.py 逻辑
# --------------------------------------------------------------------------- #


def test_s9_default_loader_is_store_load_oracle() -> None:
    default = inspect.signature(OracleGate.__init__).parameters["loader"].default
    assert default is load_oracle
    # 默认 loader 真能读到冻结 Oracle（本地 JSON，零 DB），且不被复制实现替换。
    gate = OracleGate(status_reader=_StatusReader(TaskStatus.COMPLETED))
    truth = gate.read("mysql_slow_query", task_id="task_default", run_id="run_default")
    assert truth.scenario_id == "mysql_slow_query"
    assert truth.expected_root_cause


def test_s9_oracle_module_does_not_duplicate_store_loading_logic() -> None:
    source = (ROOT / "core" / "benchmark" / "oracle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "json" not in imported
    assert "hashlib" not in imported
    for duplicated in (
        "_read_json",
        "model_validate",
        "ORACLE_DIR",
        "compute_fault_script_hash",
        "json.loads",
        "hashlib.sha256",
    ):
        assert duplicated not in source, f"不得复制 store.py 逻辑：{duplicated}"


def test_s9_faults_store_seed_validation_is_intact() -> None:
    """冻结文件零改动：load_oracle 内部的 seed 一致性校验仍在。"""

    source = (ROOT / "core" / "faults" / "store.py").read_text(encoding="utf-8")
    assert "truth.fault_seed != manifest.data_seed" in source
