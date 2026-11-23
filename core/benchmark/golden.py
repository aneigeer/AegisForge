"""P5-T2：金标回归数据集的**唯一导出路径**（清洗 → 切分 → 落盘 → 出口复检）。

与既有 W09 组件的关系（全部复用，不重写规则）：

- 去重 / 许可隔离 / 标签归一 / **Oracle 泄漏扫描** / 出口脱敏复扫：`clean_samples()`
  （命中真值字段名直接抛 `OracleLeakDetected`，fail closed）；
- 防泄漏切分：`split_samples()`（group split，确定性、无 RNG）；
- 本模块只做三件新增的事：**版本化落盘**、**写盘后二次复检**、**清单摘要**。

红线：

1. 落盘前必须经 `clean_samples`（即必经 `RedactionGate` 的 TRAINING_SAMPLE 通道）；
2. **写盘后**再对磁盘内容做一次 Oracle 泄漏扫描与脱敏复扫——防止"清洗后又拼回去"
   这类绕过；任一不通过即删除本次产物并抛错；
3. 不导出 `QUARANTINED` / `REJECTED` 样本，也不导出被脱敏闸门阻断的样本。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.benchmark.dataset import (
    PASSABLE_LABELS,
    SPLIT_RATIOS,
    DatasetSample,
    clean_samples,
    split_samples,
)
from core.benchmark.oracle import assert_no_oracle_leak
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionChannel, RedactionStatus
from core.security.canonical import canonical_digest

#: 默认根目录（相对仓库根）；测试可覆盖。
DEFAULT_ROOT = Path("datasets") / "golden"
#: 允许的出口状态（`REVIEW_REQUIRED` / `BLOCKED` 一律拒绝）。
_PASSABLE = frozenset({RedactionStatus.CLEAN.value, RedactionStatus.REDACTED.value})


@dataclass(frozen=True)
class GoldenExport:
    """一次导出的结果清单（可序列化，供 P5-T1 比对引用）。"""

    suite_version: str
    root: Path
    kept: int
    rejected: int
    quarantined: int
    counts: dict[str, int] = field(default_factory=dict)
    leakage_count: int = 0
    digest: str = ""
    files: tuple[str, ...] = ()

    def as_manifest(self) -> dict[str, Any]:
        return {
            "suite_version": self.suite_version,
            "kept": self.kept,
            "rejected": self.rejected,
            "quarantined": self.quarantined,
            "counts": dict(self.counts),
            "leakage_count": self.leakage_count,
            "digest": self.digest,
            "files": list(self.files),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def _default_gate() -> RedactionGate:
    from core.redaction.engine import DataRedactionEngine

    return RedactionGate(DataRedactionEngine())


def _split_of(manifest: Any, sample_id: str) -> str:
    return str(manifest.sample_assignments.get(sample_id, "train"))


def export_golden_dataset(
    *,
    suite_version: str,
    samples: Sequence[DatasetSample],
    root: Path | None = None,
    gate: RedactionGate | None = None,
    ratios: tuple[int, int, int] = SPLIT_RATIOS,
) -> GoldenExport:
    """清洗 + 切分 + 落盘 + 复检；返回清单。

    Raises:
        OracleLeakDetected: 输入或落盘内容命中 Oracle 真值字段名。
        ValueError: `suite_version` 为空或落盘复检未通过。
    """
    version = str(suite_version or "").strip()
    if not version:
        raise ValueError("suite_version 不能为空")
    resolved_gate = gate or _default_gate()

    clean = clean_samples(samples, gate=resolved_gate)
    passing = [
        sample
        for sample in clean.normalized_samples
        if sample.label in PASSABLE_LABELS
    ]
    split = split_samples(passing, ratios=ratios, suite_version=version)

    target = (root or DEFAULT_ROOT) / version
    target.mkdir(parents=True, exist_ok=True)
    records = []
    per_split: dict[str, int] = {}
    for sample in passing:
        name = _split_of(split, sample.sample_id)
        per_split[name] = per_split.get(name, 0) + 1
        records.append(
            {
                "sample_id": sample.sample_id,
                "split": name,
                "engine": sample.engine,
                "fault_family": sample.fault_family,
                # 刻意**不导出** fault_seed：Oracle 隔离闸门把它视为真值邻接字段
                # （`assert_no_oracle_leak` 会拒绝），导出件只需保留可复核的摘要。
                "workload_group": sample.workload_group,
                "label": sample.label.value,
                "license_id": sample.license_id,
                "content_digest": sample.content_digest,
                "text": sample.text,
            }
        )
    records.sort(key=lambda item: (item["split"], item["sample_id"]))

    dataset_path = target / "samples.jsonl"
    payload = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
    dataset_path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    # ---- 写盘后复检（防止"清洗后又拼回去"） ------------------------------
    try:
        on_disk = dataset_path.read_text(encoding="utf-8")
        assert_no_oracle_leak([on_disk])
        _, scan = resolved_gate.for_document(
            {"samples": on_disk},
            channel=RedactionChannel.TRAINING_SAMPLE,
            task_id=f"golden:{version}"
        )
        status = scan.status.value if hasattr(scan.status, "value") else str(scan.status)
        if status not in _PASSABLE:
            raise ValueError(f"落盘复检未通过：脱敏状态 {status}")
    except Exception:
        # 失败即清理本次产物，避免"半成品数据集"被下游当成有效数据。
        shutil.rmtree(target, ignore_errors=True)
        raise

    digest = canonical_digest(
        {
            "suite_version": version,
            "records": [
                {key: item[key] for key in ("sample_id", "split", "content_digest")}
                for item in records
            ],
        }
    )
    manifest = GoldenExport(
        suite_version=version,
        root=target,
        kept=clean.kept,
        rejected=clean.rejected,
        quarantined=clean.quarantined,
        counts=dict(sorted(per_split.items())),
        leakage_count=int(split.leakage_count),
        digest=digest,
        files=("samples.jsonl", "manifest.json"),
    )
    (target / "manifest.json").write_text(
        json.dumps(manifest.as_manifest(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = ["DEFAULT_ROOT", "GoldenExport", "export_golden_dataset"]
