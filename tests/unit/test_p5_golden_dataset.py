"""P5-T2 单元测试：金标数据集导出的出口红线。

验收锚点（roadmap P5-T2）：

- 导出的金标回归样本**通过 RedactionGate**（含 PII 时被改写，不落原文）；
- **零 Oracle 泄漏**：输入或落盘内容命中真值字段名即 fail closed 并清理产物；
- 不导出 `QUARANTINED` / `REJECTED` 样本，切分无组泄漏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.benchmark.dataset import DatasetLabel, DatasetSample, content_digest
from core.benchmark.golden import export_golden_dataset
from core.errors import OracleLeakDetected


def _sample(
    sample_id: str,
    *,
    text: str | None = None,
    label: DatasetLabel = DatasetLabel.VERIFIED,
    license_id: str | None = "internal",
    fault_seed: int = 4103,
    workload_group: str = "wg_a",
) -> DatasetSample:
    payload = text or f"lock wait timeout on seed_orders [{sample_id}]"
    return DatasetSample(
        sample_id=sample_id,
        engine="mysql",
        fault_family="lock_wait",
        fault_seed=fault_seed,
        workload_group=workload_group,
        label=label,
        license_id=license_id,
        content_digest=content_digest(engine="mysql", fault_family="lock_wait", text=payload),
        text=payload,
    )


def _samples() -> list[DatasetSample]:
    return [
        _sample("s1", fault_seed=4101, workload_group="wg_a"),
        _sample("s2", fault_seed=4102, workload_group="wg_b", text="deadlock detected [s2]"),
        _sample("s3", fault_seed=4103, workload_group="wg_c", text="slow query missing index [s3]"),
        _sample("s4", label=DatasetLabel.REJECTED, fault_seed=4104, workload_group="wg_d"),
        _sample("s5", license_id=None, fault_seed=4105, workload_group="wg_e"),
    ]


def test_export_writes_versioned_dataset_with_manifest(tmp_path: Path) -> None:
    export = export_golden_dataset(
        suite_version="golden-v1", samples=_samples(), root=tmp_path
    )
    assert export.root == tmp_path / "golden-v1"
    dataset = export.root / "samples.jsonl"
    manifest = export.root / "manifest.json"
    assert dataset.exists() and manifest.exists()
    assert export.leakage_count == 0
    # 5 条输入里：REJECTED 标签与许可不明各 1 条被排除，剩余 3 条入集。
    assert export.kept == 3
    lines = [line for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == export.kept
    assert export.digest and len(export.digest) == 64


def test_pii_is_redacted_in_exported_text(tmp_path: Path) -> None:
    samples = [_sample("pii", text="contact 13800138000 or user@example.com")]
    export = export_golden_dataset(suite_version="golden-pii", samples=samples, root=tmp_path)
    text = (export.root / "samples.jsonl").read_text(encoding="utf-8")
    assert "13800138000" not in text
    assert "user@example.com" not in text


def test_oracle_leak_in_input_fails_closed_and_writes_nothing(tmp_path: Path) -> None:
    samples = [_sample("leak", text="expected_root_cause=lock_wait")]
    with pytest.raises(OracleLeakDetected):
        export_golden_dataset(suite_version="golden-leak", samples=samples, root=tmp_path)
    assert not (tmp_path / "golden-leak").exists(), "失败必须清理产物"


def test_manifest_records_counts_and_files(tmp_path: Path) -> None:
    export = export_golden_dataset(suite_version="golden-v2", samples=_samples(), root=tmp_path)
    payload = export.as_manifest()
    assert payload["suite_version"] == "golden-v2"
    assert payload["counts"] == export.counts
    assert "samples.jsonl" in payload["files"]


def test_empty_suite_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        export_golden_dataset(suite_version="  ", samples=_samples(), root=tmp_path)


def test_export_is_deterministic(tmp_path: Path) -> None:
    first = export_golden_dataset(suite_version="golden-det", samples=_samples(), root=tmp_path / "a")
    second = export_golden_dataset(suite_version="golden-det", samples=_samples(), root=tmp_path / "b")
    assert first.digest == second.digest
    assert first.counts == second.counts
