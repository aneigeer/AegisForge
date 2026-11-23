"""W10 T04 单测：数据清洗 / group split / 泄漏实测（AC-14 / AC-15 / AC-21）。

零 DB、零网络；冻结资产只读；不放宽断言、不加 skip/xfail。

覆盖：

- AC-14① 同源模板跨三组 → 三组交集为空且 ``leakage_count == 0``；
  ``leakage_count`` 由用例**实测重算**（三维度 × 三组两两求交），并额外用
  「人为跨组放置同源样本」证明它不是常量；
- AC-14② 许可不明 → ``label == QUARANTINED``、不进任何 split、不计入 ``kept``；
- AC-15 ``oracle_in_training_samples is False``，训练样本文本无 Oracle 真值
  字段名，且复用 T01 ``assert_no_oracle_leak`` 命中即 fail closed；
- AC-21 出口复扫：``BLOCKED`` / ``REVIEW_REQUIRED`` 拒绝入集
  （``REVIEW_REQUIRED`` 不静默通过），复用 W09 ``RedactionGate`` 口径；
- 冻结件 ``benchmarks/w10/dataset_manifest.json`` 可被 ``SplitManifest``
  加载，且 ``leakage_count == 0`` 由用例从 ``split_groups`` 独立重算验证；
- 静态守护：只依赖 ``core*``、复用 ``canonical_digest`` /
  ``assert_no_oracle_leak``、无 ``random.shuffle`` / ``random.sample``。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.benchmark.dataset import (
    GROUP_KEYS,
    PASSABLE_LABELS,
    SPLIT_BUCKETS,
    SPLIT_NAMES,
    SPLIT_RATIOS,
    CleanReport,
    DatasetLabel,
    DatasetSample,
    SplitManifest,
    assert_no_group_leakage,
    clean_samples,
    content_digest,
    measure_dimension_disjoint,
    measure_group_leakage,
    split_samples,
)
from core.benchmark.oracle import (
    OracleLeakDetected,
    assert_no_oracle_leak,
    scan_texts_for_oracle_leak,
)
from core.errors import BenchmarkInvalidError, ErrorCode, RedactionBlockedError
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionChannel, RedactionStatus
from core.security.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[2]
DATASET_MODULE = ROOT / "core" / "benchmark" / "dataset.py"
MANIFEST_PATH = ROOT / "benchmarks" / "w10" / "dataset_manifest.json"

FORBIDDEN_TRUTH_LITERALS: tuple[str, ...] = (
    "expected_root_cause",
    "injection_params",
    "truth_assertions",
)


def _sample(
    sample_id: str,
    *,
    seed: int,
    workload_group: str,
    text: str,
    license_id: str | None = "Apache-2.0",
    label: DatasetLabel = DatasetLabel.REVIEWED,
    engine: str = "mysql",
    fault_family: str = "slow_query",
) -> DatasetSample:
    """构造一条合法样本（``content_digest`` 由实现同款 helper 计算）。"""

    return DatasetSample(
        sample_id=sample_id,
        engine=engine,
        fault_family=fault_family,
        fault_seed=seed,
        workload_group=workload_group,
        label=label,
        license_id=license_id,
        content_digest=content_digest(
            engine=engine, fault_family=fault_family, text=text
        ),
        text=text,
    )


def _reference_samples(count: int = 24) -> list[DatasetSample]:
    """确定性参考样本集：24 个互不共享 seed / workload_group 的 cluster。"""

    samples: list[DatasetSample] = []
    for index in range(count):
        samples.append(
            _sample(
                f"s-{index:02d}",
                seed=4100 + index,
                workload_group=f"wg-{index}",
                text=(
                    f"观测样本 {index}：连接池等待与慢查询，"
                    "建议核对索引与执行计划。"
                ),
                engine="mysql" if index % 2 == 0 else "postgresql",
                fault_family="slow_query" if index % 3 == 0 else "lock_wait",
            )
        )
    return samples


def _homologous_samples() -> list[DatasetSample]:
    """同源模板跨三组：三个 cluster，每个 cluster 内共享模板、seed、workload_group。

    cluster 内多条样本共享 ``fault_seed`` 与 ``workload_group``（并查集必合并），
    cluster 之间互不共享任何维度，用于验证「同一 seed 不得跨 split」。
    """

    groups: list[tuple[int, str, str]] = [
        (501, "wl-alpha", "同源模板甲：慢查询与索引核对。"),
        (502, "wl-beta", "同源模板乙：锁等待与事务核对。"),
        (503, "wl-gamma", "同源模板丙：连接池等待与超时核对。"),
    ]
    samples: list[DatasetSample] = []
    for group_index, (seed, workload_group, template) in enumerate(groups):
        for repetition in range(1, 4):
            samples.append(
                _sample(
                    f"h-{group_index}-{repetition}",
                    seed=seed,
                    workload_group=workload_group,
                    text=f"{template}（重复 {repetition}，不同 engine 视图）",
                    engine="mysql" if repetition % 2 else "postgresql",
                    fault_family="slow_query",
                )
            )
    return samples


class _StubResult:
    """测试桩：只暴露 ``status``，模拟 ``RedactionResult``。"""

    def __init__(self, status: RedactionStatus) -> None:
        self.status = status


class _StubGate:
    """测试桩：可控 ``for_document`` 判定，精确覆盖 BLOCKED / REVIEW_REQUIRED 分支。"""

    def __init__(self, status: RedactionStatus) -> None:
        self._status = status
        self.calls: list[dict[str, Any]] = []

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], _StubResult]:
        self.calls.append(
            {"channel": channel, "task_id": task_id, "tenant_id": tenant_id}
        )
        return dict(payload), _StubResult(self._status)


class _RaisingGate:
    """测试桩：``BLOCKED`` 时抛 W09 ``RedactionBlockedError``（真实闸门行为）。"""

    def for_document(
        self,
        payload: dict[str, Any],
        *,
        channel: RedactionChannel,
        task_id: str,
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], _StubResult]:
        raise RedactionBlockedError("结构性阻断", details={"channel": channel.value})


def _leakage_from_groups(split_groups: dict[str, dict[str, list[str]]]) -> int:
    """用例侧**独立**重算：三维度 × 三组两两求交再求和（不读 leakage_count）。"""

    total = 0
    dimensions = ("seed", "workload_group", "triple")
    for index, left in enumerate(SPLIT_NAMES):
        for right in SPLIT_NAMES[index + 1 :]:
            for dimension in dimensions:
                total += len(
                    set(split_groups[left][dimension])
                    & set(split_groups[right][dimension])
                )
    return total


# --------------------------------------------------------------------------- #
# 常量 / 模型契约
# --------------------------------------------------------------------------- #


def test_split_constants_and_group_keys_are_frozen() -> None:
    assert SPLIT_BUCKETS == 100
    assert SPLIT_RATIOS == (80, 10, 10)
    assert sum(SPLIT_RATIOS) == SPLIT_BUCKETS
    assert GROUP_KEYS == ("engine", "fault_family", "seed", "workload_group")
    assert SPLIT_NAMES == ("train", "dev", "test")


def test_dataset_label_is_exactly_five_values() -> None:
    assert {member.value for member in DatasetLabel} == {
        "VERIFIED",
        "REVIEWED",
        "GENERATED",
        "REJECTED",
        "QUARANTINED",
    }
    assert len(DatasetLabel) == 5
    assert PASSABLE_LABELS == frozenset(
        {
            DatasetLabel.VERIFIED,
            DatasetLabel.REVIEWED,
            DatasetLabel.GENERATED,
        }
    )


def test_dataset_sample_and_reports_are_closed_models() -> None:
    with pytest.raises(ValidationError):
        DatasetSample.model_validate(
            {
                **_sample("x", seed=1, workload_group="w", text="干净文本").model_dump(),
                "unexpected": 1,
            }
        )
    with pytest.raises(ValidationError):
        CleanReport.model_validate({"input_count": 0, "kept": 0, "extra": 1})
    with pytest.raises(ValidationError):
        SplitManifest.model_validate(
            {
                "suite_version": "suite-v1",
                "cluster_count": 0,
                "leakage_count": 0,
                "seed_disjoint": True,
                "workload_group_disjoint": True,
                "oracle_in_training_samples": False,
                "split_digest": "abc",
                "frozen_at": "2026-01-01T00:00:00+00:00",
                "extra": 1,
            }
        )


def test_content_digest_reuses_canonical_digest_and_normalizes_whitespace() -> None:
    left = content_digest(engine="mysql", fault_family="slow_query", text="A   B\nC")
    right = content_digest(engine="mysql", fault_family="slow_query", text=" A B C ")
    assert left == right == canonical_digest(
        {"template": "A B C", "engine": "mysql", "fault_family": "slow_query"}
    )
    assert left != content_digest(engine="postgresql", fault_family="slow_query", text="A B C")


# --------------------------------------------------------------------------- #
# AC-14① 无泄漏 + leakage_count 实测重算
# --------------------------------------------------------------------------- #


def test_ac14_homologous_templates_have_empty_intersections_and_zero_leakage() -> None:
    samples = _homologous_samples()
    report = clean_samples(samples)
    assert report.input_count == 9
    assert report.kept == 9

    split = split_samples(report.normalized_samples, suite_version="suite-v1")
    assert split.strategy == "group_split"
    assert split.group_keys == GROUP_KEYS
    assert split.ratios == SPLIT_RATIOS
    assert split.cluster_count == 3  # 三个同源 cluster 不得被拆开
    assert sum(split.counts.values()) == report.kept

    # 三组两两交集为空（三维度逐一）。
    for index, left in enumerate(SPLIT_NAMES):
        for right in SPLIT_NAMES[index + 1 :]:
            for dimension in ("seed", "workload_group", "triple"):
                assert not (
                    set(split.split_groups[left][dimension])
                    & set(split.split_groups[right][dimension])
                ), f"{left}∩{right} 在 {dimension} 维泄漏"

    # 用例独立重算（不读 split.leakage_count）。
    assert _leakage_from_groups(split.split_groups) == 0
    assert measure_group_leakage(
        report.normalized_samples, split.sample_assignments
    ) == 0
    assert split.leakage_count == 0
    assert split.seed_disjoint is True
    assert split.workload_group_disjoint is True
    assert measure_dimension_disjoint(
        report.normalized_samples, split.sample_assignments, dimension="seed"
    ) is True
    assert_no_group_leakage(split, report.normalized_samples)


def test_ac14_full_reference_split_is_deterministic_without_rng() -> None:
    samples = _reference_samples()
    first = split_samples(samples, suite_version="suite-v1")
    second = split_samples(samples, suite_version="suite-v1")
    # frozen_at 是冻结时刻（允许不同），其余确定性字段必须逐字节一致。
    assert first.split_digest == second.split_digest
    assert first.counts == second.counts
    assert first.assignments == second.assignments
    assert first.sample_assignments == second.sample_assignments
    assert first.split_groups == second.split_groups
    assert first.cluster_count == second.cluster_count == 24
    assert first.leakage_count == second.leakage_count == 0
    assert set(first.counts) == set(SPLIT_NAMES)
    assert all(first.counts[name] > 0 for name in SPLIT_NAMES)
    assert_no_group_leakage(first, samples)
    assert _leakage_from_groups(first.split_groups) == 0
    # 分桶确由 canonical_digest 派生（可复算，不是随机）。
    for cluster_id, assigned in first.assignments.items():
        assert cluster_id.startswith("cluster_")
        assert assigned in SPLIT_NAMES


def test_ac14_leakage_count_is_measured_not_a_constant() -> None:
    """人为把同源样本塞进不同 split → 实测泄漏 > 0（证明不是写死 0）。"""

    a = _sample("a-01", seed=77, workload_group="shared-wg", text="同源模板 A")
    b = _sample("b-01", seed=77, workload_group="shared-wg", text="同源模板 B 不同文本")
    leaked = measure_group_leakage([a, b], {"a-01": "train", "b-01": "test"})
    # 同 seed / 同 workload_group / 同 triple → train∩test 三个维度各 1 → 3。
    assert leaked == 3
    assert measure_group_leakage([a, b], {"a-01": "train", "b-01": "train"}) == 0
    # split_samples 的结构性聚类把二者放进同一 split，因此实测恒为 0。
    split = split_samples([a, b], suite_version="suite-v1")
    assert split.leakage_count == 0
    assert len({split.sample_assignments["a-01"], split.sample_assignments["b-01"]}) == 1


def test_split_rejects_invalid_ratios() -> None:
    with pytest.raises(BenchmarkInvalidError) as caught:
        split_samples(_reference_samples(3), ratios=(80, 10, 5), suite_version="suite-v1")
    assert caught.value.code is ErrorCode.BENCHMARK_INVALID
    with pytest.raises(BenchmarkInvalidError):
        split_samples(_reference_samples(3), ratios=(-1, 50, 51), suite_version="suite-v1")


def test_assert_no_group_leakage_detects_tampered_manifest() -> None:
    samples = _reference_samples(6)
    split = split_samples(samples, suite_version="suite-v1")
    with pytest.raises(BenchmarkInvalidError):
        assert_no_group_leakage(split.model_copy(update={"leakage_count": 99}), samples)
    with pytest.raises(BenchmarkInvalidError):
        assert_no_group_leakage(
            split.model_copy(update={"seed_disjoint": not split.seed_disjoint}), samples
        )
    with pytest.raises(BenchmarkInvalidError):
        assert_no_group_leakage(
            split.model_copy(update={"oracle_in_training_samples": True}), samples
        )


# --------------------------------------------------------------------------- #
# AC-14② 许可不明
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("license_id", [None, "UNKNOWN", "unknown", "", "   "])
def test_ac14_unknown_license_is_quarantined_and_excluded(license_id: str | None) -> None:
    sample = _sample("lic-01", seed=9001, workload_group="wg-lic", text="许可不明样本", license_id=license_id)
    good = _sample("good-01", seed=9002, workload_group="wg-good", text="许可明确样本")
    report = clean_samples([sample, good])

    assert report.input_count == 2
    assert report.license_unknown == 1
    assert report.quarantined == 1
    assert report.kept == 1
    labels = {item.sample_id: item.label for item in report.normalized_samples}
    assert labels["lic-01"] is DatasetLabel.QUARANTINED
    assert labels["good-01"] is DatasetLabel.REVIEWED

    split = split_samples(report.normalized_samples, suite_version="suite-v1")
    assert "lic-01" not in split.sample_assignments
    assert sum(split.counts.values()) == 1
    assert set(split.sample_assignments) == {"good-01"}


def test_clean_report_counts_are_internally_consistent_and_honest() -> None:
    samples = _reference_samples(6)
    duplicate = samples[0].model_copy(update={"sample_id": "dup-01"})
    quarantine = _sample("q-01", seed=9999, workload_group="wg-q", text="隔离样本", license_id="UNKNOWN")
    # 独立文本（content_digest 不同）才能真正走到「③ 标签归一」被拒。
    rejected = _sample(
        "r-01",
        seed=9998,
        workload_group="wg-r",
        text="标签为 REJECTED 的独立文本",
        label=DatasetLabel.REJECTED,
    )
    report = clean_samples([*samples, duplicate, quarantine, rejected])
    assert report.input_count == 9
    assert report.duplicates == 1
    assert report.license_unknown == 1
    assert report.quarantined == 1
    assert report.rejected == 1
    # 每个输入样本恰好落一个桶（不虚报、不重复计数）。
    assert (
        report.kept
        + report.duplicates
        + report.quarantined
        + report.rejected
        == report.input_count
    )
    assert len(report.normalized_samples) == report.input_count - report.duplicates
    notes_text = " ".join(report.notes)
    assert "100–300" in notes_text
    assert "目标而非实测" in notes_text
    assert f"input_count={report.input_count}" in notes_text
    assert "条数不等于有效训练规模" in notes_text


def test_clean_order_dedup_precedes_label_normalization() -> None:
    """设计 §7.1 顺序：去重在标签归一之前；同 digest 的 REJECTED 副本计入 duplicates。"""

    original = _sample("base-01", seed=8801, workload_group="wg-base", text="同一模板文本")
    rejected_copy = original.model_copy(
        update={"sample_id": "copy-01", "label": DatasetLabel.REJECTED}
    )
    report = clean_samples([original, rejected_copy])
    assert report.input_count == 2
    assert report.duplicates == 1
    assert report.rejected == 0  # 副本在 ① 去重阶段已被移除，未到 ③
    assert report.kept == 1
    assert len(report.normalized_samples) == 1


def test_unknown_label_is_normalized_to_rejected() -> None:
    bad = DatasetSample.model_construct(
        sample_id="bad-01",
        engine="mysql",
        fault_family="slow_query",
        fault_seed=3,
        workload_group="wg-bad",
        label="MYSTERY",
        license_id="MIT",
        content_digest="digest-placeholder",
        text="未知标签样本",
    )
    report = clean_samples([bad])
    assert report.kept == 0
    assert report.rejected == 1
    assert report.normalized_samples[0].label is DatasetLabel.REJECTED


# --------------------------------------------------------------------------- #
# AC-15 Oracle 不入训练样本
# --------------------------------------------------------------------------- #


def test_ac15_training_texts_have_no_oracle_truth_field_names() -> None:
    samples = _reference_samples()
    split = split_samples(samples, suite_version="suite-v1")
    assert split.oracle_in_training_samples is False

    train_texts = [
        item.text
        for item in samples
        if split.sample_assignments.get(item.sample_id) == "train"
    ]
    assert train_texts, "train split 必须非空才能验证无泄漏"
    assert scan_texts_for_oracle_leak(train_texts) == ()
    assert_no_oracle_leak(train_texts)  # 复用 T01，不抛即通过
    serialized = json.dumps(train_texts, ensure_ascii=False)
    for forbidden in FORBIDDEN_TRUTH_LITERALS:
        assert forbidden not in serialized
    assert "fault_seed" not in serialized
    assert "expected_root_cause" not in serialized


def test_oracle_leak_sample_is_rejected_by_t01_assert() -> None:
    leaky = _sample("o-01", seed=4, workload_group="wg-o", text="含 fault_seed 字段名的样本")
    with pytest.raises(OracleLeakDetected) as caught:
        clean_samples([leaky])
    assert caught.value.code is ErrorCode.ORACLE_LEAK_DETECTED
    assert caught.value.details == {"leaked_fields": ["fault_seed"]}
    # 直接 split 也会在训练样本扫描处 fail closed。
    with pytest.raises(OracleLeakDetected):
        split_samples([leaky], suite_version="suite-v1")


def test_dataset_module_reuses_t01_oracle_assert() -> None:
    source = DATASET_MODULE.read_text(encoding="utf-8")
    assert "from core.benchmark.oracle import" in source
    assert "assert_no_oracle_leak" in source
    assert "scan_texts_for_oracle_leak" in source
    # 隔离：源码本身不得出现 Oracle 真值字段名 / 读取入口符号（S1 同向）。
    for forbidden in (*FORBIDDEN_TRUTH_LITERALS, "load_oracle", "OracleTruth"):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------- #
# AC-21 数据集出口：BLOCKED / REVIEW_REQUIRED 拒绝入集
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [RedactionStatus.BLOCKED, RedactionStatus.REVIEW_REQUIRED]
)
def test_ac21_not_passable_statuses_are_rejected(status: RedactionStatus) -> None:
    sample = _sample("gate-01", seed=7001, workload_group="wg-gate", text="待复扫样本文本")
    gate = _StubGate(status)
    report = clean_samples([sample], gate=gate)
    assert report.kept == 0
    assert report.rejected == 1
    assert report.normalized_samples[0].label is DatasetLabel.REJECTED
    # 复扫确实走了 TRAINING_SAMPLE 通道，且带 task_id。
    assert gate.calls and gate.calls[0]["channel"] is RedactionChannel.TRAINING_SAMPLE
    assert gate.calls[0]["task_id"] == "gate-01"


def test_ac21_blocked_exception_path_is_rejected() -> None:
    sample = _sample("gate-02", seed=7002, workload_group="wg-gate2", text="待复扫样本文本")
    report = clean_samples([sample], gate=_RaisingGate())
    assert report.kept == 0
    assert report.rejected == 1
    assert report.normalized_samples[0].label is DatasetLabel.REJECTED


def test_ac21_real_w09_gate_blocks_connection_string() -> None:
    """用真实 W09 闸门（非桩）：连接串命中 BLOCKED → 拒绝入集、绝不静默通过。"""

    sample = _sample(
        "gate-real-01",
        seed=7003,
        workload_group="wg-gate-real",
        text="target mysql://dg_ro:pw@127.0.0.1:3306/dg_lab",
    )
    gate = RedactionGate(DataRedactionEngine())
    report = clean_samples([sample], gate=gate)
    assert report.kept == 0
    assert report.rejected == 1
    assert report.normalized_samples[0].label is DatasetLabel.REJECTED


def test_ac21_clean_and_redacted_samples_are_passable() -> None:
    clean = _sample("ok-01", seed=7004, workload_group="wg-ok", text="干净样本文本")
    redacted = _sample(
        "ok-02",
        seed=7005,
        workload_group="wg-ok2",
        text="联系电话 13812345678 需要脱敏",
    )
    report = clean_samples([clean, redacted], gate=_StubGate(RedactionStatus.CLEAN))
    assert report.kept == 2
    assert report.rejected == 0
    assert_gate = RedactionGate(DataRedactionEngine())
    real_report = clean_samples([clean, redacted], gate=assert_gate)
    assert real_report.kept == 2


def test_dataset_module_does_not_rewrite_w09_gate() -> None:
    source = DATASET_MODULE.read_text(encoding="utf-8")
    assert "from core.redaction.gate import RedactionGate" in source
    assert "from core.redaction.engine import DataRedactionEngine" in source
    assert "for_document(" in source
    assert "TRAINING_SAMPLE" in source
    assert "class RedactionGate" not in source
    assert "def for_document" not in source
    assert "RedactionStatus.REVIEW_REQUIRED" in source  # 不静默通过


# --------------------------------------------------------------------------- #
# 冻结件 dataset_manifest.json
# --------------------------------------------------------------------------- #


def test_dataset_manifest_is_a_valid_split_manifest_instance() -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = SplitManifest.model_validate(payload)
    assert set(payload) == set(SplitManifest.model_fields)
    assert manifest.strategy == "group_split"
    assert manifest.suite_version == "suite-v1"
    assert manifest.group_keys == GROUP_KEYS
    assert manifest.ratios == SPLIT_RATIOS
    assert manifest.cluster_count == 24
    assert manifest.cluster_count == len(manifest.assignments)
    assert sum(manifest.counts.values()) == len(manifest.sample_assignments)
    assert manifest.oracle_in_training_samples is False
    assert manifest.seed_disjoint is True
    assert manifest.workload_group_disjoint is True
    assert manifest.frozen_at
    assert manifest.split_digest
    assert any("目标而非实测" in note for note in manifest.notes)


def test_dataset_manifest_leakage_zero_is_recomputed_not_read_back() -> None:
    """卡内要求：leakage_count==0 由用例重算，不是读回文件里的值。"""

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    recomputed = _leakage_from_groups(payload["split_groups"])
    assert recomputed == 0
    assert payload["leakage_count"] == recomputed
    # 三组两两交集实测为空（seed / workload_group / triple）。
    for index, left in enumerate(SPLIT_NAMES):
        for right in SPLIT_NAMES[index + 1 :]:
            for dimension in ("seed", "workload_group", "triple"):
                assert not (
                    set(payload["split_groups"][left][dimension])
                    & set(payload["split_groups"][right][dimension])
                )
    disjoint_seed = all(
        not (
            set(payload["split_groups"][left]["seed"])
            & set(payload["split_groups"][right]["seed"])
        )
        for index, left in enumerate(SPLIT_NAMES)
        for right in SPLIT_NAMES[index + 1 :]
    )
    assert disjoint_seed is payload["seed_disjoint"] is True


def test_dataset_manifest_matches_fresh_split_on_deterministic_fields() -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    split = split_samples(_reference_samples(), suite_version="suite-v1")
    for field in (
        "strategy",
        "suite_version",
        "group_keys",
        "ratios",
        "cluster_count",
        "assignments",
        "sample_assignments",
        "split_groups",
        "counts",
        "leakage_count",
        "seed_disjoint",
        "workload_group_disjoint",
        "oracle_in_training_samples",
        "empty_splits",
        "split_digest",
    ):
        assert split.model_dump(mode="json")[field] == payload[field], field
    assert_no_group_leakage(split, _reference_samples())


# --------------------------------------------------------------------------- #
# 静态守护：依赖方向 / 无 RNG / 复用点
# --------------------------------------------------------------------------- #


def test_dataset_module_only_depends_on_core_and_has_no_rng() -> None:
    source = DATASET_MODULE.read_text(encoding="utf-8")
    assert "random.shuffle" not in source
    assert "random.sample" not in source
    assert "import numpy" not in source
    assert "import scipy" not in source
    banned = {"apps", "workers"}
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not (modules & banned), modules & banned
    for forbidden in ("core.gateway", "core.approval", "core.agent"):
        assert f"from {forbidden}" not in source
    assert "from core.security.canonical import canonical_digest" in source
