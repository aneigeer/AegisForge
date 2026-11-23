"""W10 T04：数据集清洗 / group split / 泄漏实测（AC-14 / AC-15 / AC-21）。

口径来源：``docs/w10/02-design.md`` §7（清洗流水线、并查集聚类、诚实规模）、
§4.5（数据模型）；任务卡 ``docs/w10/03-task-cards.md`` T04。

五条硬约束：

1. **清洗顺序固定**（设计 §7.1）：去重（``canonical_digest``）→ 许可检查 →
   标签归一（5 值闭集）→ Oracle 泄漏扫描 → 出口脱敏复扫。
2. **出口复扫只复用 W09 闸门**：``RedactionGate.for_document`` 的
   ``TRAINING_SAMPLE`` 通道（与 ``core/report/samples.py::build_training_sample``
   同口径）。``BLOCKED`` / ``REVIEW_REQUIRED`` 一律拒绝入集，
   ``REVIEW_REQUIRED`` **不得静默通过**；本模块不重写闸门、不另建复扫。
3. **split 无 RNG**：并查集按三维度（同 seed / 同 workload_group /
   同 ``(engine, fault_family, workload_group)``）聚类，``canonical_digest``
   分桶（80/10/10）。``leakage_count`` 是**实测重算**（三维度 × 三组两两求交
   再求和），不是常量 0。
4. **Oracle 真值不出训练样本**：复用 T01 的
   ``assert_no_oracle_leak`` / ``scan_texts_for_oracle_leak``；
   ``oracle_in_training_samples`` 由真实文本扫描得出。
5. **不编造**：``CleanReport`` 记录实际条数；100–300 条只是目标，写入 ``notes``；
   ``QUARANTINED`` 不进任何 split / 任何统计。

依赖方向（设计 §2.2）：只依赖 ``core*``；**不** import ``apps/**``、
``workers/**``、``core/gateway/**``、``core/approval/**``、``core/agent/**``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.benchmark.oracle import (
    OracleLeakDetected,
    assert_no_oracle_leak,
    scan_texts_for_oracle_leak,
)
from core.errors import BenchmarkInvalidError, RedactionBlockedError
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionChannel, RedactionStatus
from core.security.canonical import canonical_digest

# 分桶与比例：冻结常量（设计 §7.2）。train/dev/test = 80/10/10。
SPLIT_BUCKETS: int = 100
SPLIT_RATIOS: tuple[int, int, int] = (80, 10, 10)
GROUP_KEYS: tuple[str, ...] = ("engine", "fault_family", "seed", "workload_group")
SPLIT_NAMES: tuple[str, str, str] = ("train", "dev", "test")

# PRD P0-W10-04 要求 3 的规模目标（**只是目标，不是实测**）。
TARGET_MIN_SAMPLES: int = 100
TARGET_MAX_SAMPLES: int = 300

# 三维度标签：``seed`` 对应 ``DatasetSample.fault_seed``（公开 Manifest 的
# data_seed 同值）；``triple`` 对应 (engine, fault_family, workload_group)。
DIMENSIONS: tuple[str, ...] = ("seed", "workload_group", "triple")


class DatasetLabel(str, Enum):
    """数据集标签 5 值闭集（设计 §4.5）；禁止第 6 值。"""

    VERIFIED = "VERIFIED"
    REVIEWED = "REVIEWED"
    GENERATED = "GENERATED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"


# 允许进入 split 的标签。REJECTED / QUARANTINED 一律排除。
PASSABLE_LABELS: frozenset[DatasetLabel] = frozenset(
    {
        DatasetLabel.VERIFIED,
        DatasetLabel.REVIEWED,
        DatasetLabel.GENERATED,
    }
)


class DatasetSample(BaseModel):
    """一条待清洗样本文本；``extra="forbid"``，字段封闭。"""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    fault_family: str = Field(min_length=1)
    fault_seed: int
    workload_group: str = Field(min_length=1)
    label: DatasetLabel
    license_id: str | None = None
    content_digest: str = Field(min_length=1)
    text: str


class CleanReport(BaseModel):
    """清洗结果报告（设计 §4.5 的计数 + 归一化后样本）。

    ``normalized_samples`` 是本卡为「清洗 → 切分」可组合而补充的最小扩展：
    保留去重后的全部输入样本及其**最终标签**（许可不明 → ``QUARANTINED``、
    未知标签 / 出口阻断 → ``REJECTED``），使 AC-14② 的 ``label == QUARANTINED``
    可被直接断言，且 ``split_samples`` 无需重读文件。
    """

    model_config = ConfigDict(extra="forbid")

    input_count: int = Field(ge=0)
    kept: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    license_unknown: int = Field(ge=0)
    rejected: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    normalized_samples: tuple[DatasetSample, ...] = ()
    notes: tuple[str, ...] = ()


class SplitManifest(BaseModel):
    """group split 冻结清单（设计 §4.5）。

    除设计列出的字段外，补充三项**实测/可复核**字段：``sample_assignments``
    （样本→split，供 ``assert_no_group_leakage`` 重算）、``split_groups``
    （每 split 的三维度取值集合，使落盘件可脱离源码独立重算泄漏）、
    ``split_digest``（对确定性部分取 ``canonical_digest``，证明无 RNG）。
    """

    model_config = ConfigDict(extra="forbid")

    strategy: str = "group_split"
    suite_version: str = Field(min_length=1)
    group_keys: tuple[str, ...] = GROUP_KEYS
    ratios: tuple[int, int, int] = SPLIT_RATIOS
    cluster_count: int = Field(ge=0)
    assignments: dict[str, str] = Field(default_factory=dict)
    sample_assignments: dict[str, str] = Field(default_factory=dict)
    split_groups: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    leakage_count: int = Field(ge=0)
    seed_disjoint: bool
    workload_group_disjoint: bool
    oracle_in_training_samples: bool
    empty_splits: tuple[str, ...] = ()
    split_digest: str = Field(min_length=1)
    frozen_at: str = Field(min_length=1)
    notes: tuple[str, ...] = ()


def _normalize_text(text: str) -> str:
    """去重用的模板归一：折叠空白，避免同模板因排版差异被当成两条。"""

    return " ".join(str(text).split()).strip()


def content_digest(*, engine: str, fault_family: str, text: str) -> str:
    """设计 §7.1：``content_digest = canonical_digest({template, engine, fault_family})``。

    复用 ``core.security.canonical.canonical_digest``（禁止重写摘要）。
    """

    return canonical_digest(
        {
            "template": _normalize_text(text),
            "engine": engine,
            "fault_family": fault_family,
        }
    )


def _effective_label(sample: DatasetSample) -> DatasetLabel:
    """标签归一到 5 值闭集；未知字面 → ``REJECTED``（设计 §7.1 步骤 3）。

    ``DatasetSample.label`` 声明为 ``DatasetLabel``，正常构造已由 pydantic 校验；
    这里仍做运行期归一，覆盖 ``model_construct`` / 反序列化注入的越界字面。
    """

    raw = getattr(sample, "label", None)
    value = getattr(raw, "value", raw)
    try:
        return DatasetLabel(str(value))
    except ValueError:
        return DatasetLabel.REJECTED


def _license_unknown(license_id: str | None) -> bool:
    """许可不明：空 / ``None`` / 空白 / ``"UNKNOWN"``（大小写不敏感）。"""

    if license_id is None:
        return True
    normalized = str(license_id).strip().upper()
    return normalized == "" or normalized == "UNKNOWN"


def _triple(sample: DatasetSample) -> str:
    """``(engine, fault_family, workload_group)`` 的稳定字符串表示。"""

    return f"{sample.engine}|{sample.fault_family}|{sample.workload_group}"


def _dimension_values(sample: DatasetSample, dimension: str) -> str:
    """返回单条样本在给定维度的取值（供交集实测）。"""

    if dimension == "seed":
        return str(sample.fault_seed)
    if dimension == "workload_group":
        return sample.workload_group
    if dimension == "triple":
        return _triple(sample)
    raise BenchmarkInvalidError(
        "未知 group split 维度", details={"dimension": dimension}
    )


def _group_sets(
    samples: Sequence[DatasetSample], assignments: Mapping[str, str]
) -> dict[str, dict[str, set[str]]]:
    """按 split × 维度聚合实测取值集合。"""

    groups: dict[str, dict[str, set[str]]] = {
        name: {dimension: set() for dimension in DIMENSIONS}
        for name in SPLIT_NAMES
    }
    for sample in samples:
        split = assignments.get(sample.sample_id)
        if split not in SPLIT_NAMES:
            continue
        for dimension in DIMENSIONS:
            groups[split][dimension].add(_dimension_values(sample, dimension))
    return groups


def measure_group_leakage(
    samples: Sequence[DatasetSample], assignments: Mapping[str, str]
) -> int:
    """实测泄漏数：三维度 × 三组两两求交，累加交集基数（设计 §7.2）。

    这是 ``leakage_count`` 的**唯一来源**：绝不返回常量，允许非零（例如人为
    把同源样本塞进不同 split）。返回值可复现、可断言。
    """

    groups = _group_sets(samples, assignments)
    total = 0
    for index, left in enumerate(SPLIT_NAMES):
        for right in SPLIT_NAMES[index + 1 :]:
            for dimension in DIMENSIONS:
                total += len(groups[left][dimension] & groups[right][dimension])
    return total


def measure_dimension_disjoint(
    samples: Sequence[DatasetSample], assignments: Mapping[str, str], *, dimension: str
) -> bool:
    """实测单维度跨三组是否两两交集为空。"""

    groups = _group_sets(samples, assignments)
    for index, left in enumerate(SPLIT_NAMES):
        for right in SPLIT_NAMES[index + 1 :]:
            if groups[left][dimension] & groups[right][dimension]:
                return False
    return True


def assert_no_group_leakage(
    split: SplitManifest, samples: Sequence[DatasetSample]
) -> None:
    """独立重算并校验：零泄漏，且 ``split`` 的记录与实测一致。

    Raises:
        BenchmarkInvalidError: 存在跨组泄漏，或 manifest 记录与重算不符。
    """

    recomputed = measure_group_leakage(samples, split.sample_assignments)
    if recomputed != 0:
        raise BenchmarkInvalidError(
            "group split 存在跨组泄漏",
            details={"recomputed_leakage_count": recomputed},
        )
    if split.leakage_count != recomputed:
        raise BenchmarkInvalidError(
            "SplitManifest.leakage_count 与实测重算不一致",
            details={"recorded": split.leakage_count, "recomputed": recomputed},
        )
    seed_ok = measure_dimension_disjoint(
        samples, split.sample_assignments, dimension="seed"
    )
    workload_ok = measure_dimension_disjoint(
        samples, split.sample_assignments, dimension="workload_group"
    )
    if split.seed_disjoint != seed_ok or split.workload_group_disjoint != workload_ok:
        raise BenchmarkInvalidError(
            "SplitManifest 的 disjoint 标志与实测不一致",
            details={
                "seed_disjoint": split.seed_disjoint,
                "seed_recomputed": seed_ok,
                "workload_group_disjoint": split.workload_group_disjoint,
                "workload_group_recomputed": workload_ok,
            },
        )
    if split.oracle_in_training_samples:
        raise BenchmarkInvalidError("训练样本中出现 Oracle 真值字段名")


def _default_gate() -> RedactionGate:
    """``gate=None`` 时装配 W09 默认闸门（无 DSN、无状态旁路）。

    只复用 ``RedactionGate`` / ``DataRedactionEngine``；不另建出口复扫。
    """

    return RedactionGate(DataRedactionEngine())


def clean_samples(
    samples: Sequence[DatasetSample], *, gate: Any | None = None
) -> CleanReport:
    """按固定顺序清洗样本，返回 ``CleanReport``（AC-14② / AC-15 / AC-21）。

    顺序（设计 §7.1，不可调）：

    1. 去重：``content_digest``（复用 ``canonical_digest``）；
    2. 许可检查：不明 → ``QUARANTINED``，不进任何 split、不计入 ``kept``；
    3. 标签归一：非 5 值闭集 → ``REJECTED``；
    4. Oracle 泄漏扫描：命中即抛 ``OracleLeakDetected``（fail closed）；
    5. 出口脱敏复扫：``gate.for_document(channel=TRAINING_SAMPLE)``，
       ``BLOCKED`` / ``REVIEW_REQUIRED`` 拒绝入集（``REVIEW_REQUIRED``
       不静默通过），``CLEAN`` / ``REDACTED`` 放行并采用脱敏后文本。

    Raises:
        OracleLeakDetected: 待外发文本命中 Oracle 真值字段名（隔离红线）。
    """

    resolved_gate: Any = gate if gate is not None else _default_gate()
    input_count = len(samples)
    seen_digests: set[str] = set()
    normalized: list[DatasetSample] = []
    duplicates = 0
    license_unknown = 0
    rejected = 0
    quarantined = 0

    for sample in samples:
        # ① 去重：权威 digest 由内容重算，不信任可能过期的字段值。
        digest = content_digest(
            engine=sample.engine,
            fault_family=sample.fault_family,
            text=sample.text,
        )
        if digest in seen_digests:
            duplicates += 1
            continue
        seen_digests.add(digest)

        # ② 许可检查（AC-14②）。
        if _license_unknown(sample.license_id):
            license_unknown += 1
            quarantined += 1
            normalized.append(
                sample.model_copy(update={"label": DatasetLabel.QUARANTINED})
            )
            continue

        # ③ 标签归一（5 值闭集）。
        label = _effective_label(sample)
        if label not in PASSABLE_LABELS:
            if label is DatasetLabel.QUARANTINED:
                quarantined += 1
                normalized.append(
                    sample.model_copy(update={"label": DatasetLabel.QUARANTINED})
                )
            else:
                rejected += 1
                normalized.append(
                    sample.model_copy(update={"label": DatasetLabel.REJECTED})
                )
            continue

        # ④ Oracle 泄漏扫描（复用 T01；只扫外发的 text，不扫模型字段名）。
        assert_no_oracle_leak([sample.text])

        # ⑤ 出口脱敏复扫（复用 W09 闸门②同款 for_document / TRAINING_SAMPLE）。
        payload = {
            "sample_id": sample.sample_id,
            "engine": sample.engine,
            "fault_family": sample.fault_family,
            "label": label.value,
            "text": sample.text,
        }
        try:
            document, result = resolved_gate.for_document(
                payload,
                channel=RedactionChannel.TRAINING_SAMPLE,
                task_id=sample.sample_id,
            )
        except RedactionBlockedError:
            rejected += 1
            normalized.append(
                sample.model_copy(update={"label": DatasetLabel.REJECTED})
            )
            continue
        if result.status in (
            RedactionStatus.BLOCKED,
            RedactionStatus.REVIEW_REQUIRED,
        ):
            # REVIEW_REQUIRED 不得静默通过：与 build_training_sample 同口径拒绝入集。
            rejected += 1
            normalized.append(
                sample.model_copy(update={"label": DatasetLabel.REJECTED})
            )
            continue

        redacted_text = str(document.get("text", sample.text))
        normalized.append(
            sample.model_copy(update={"text": redacted_text, "label": label})
        )

    kept = sum(1 for item in normalized if item.label in PASSABLE_LABELS)
    notes = (
        f"规模目标 {TARGET_MIN_SAMPLES}–{TARGET_MAX_SAMPLES} 条（PRD P0-W10-04 "
        "要求 3）为目标而非实测；条数不等于有效训练规模。",
        f"本批实测：input_count={input_count}、kept={kept}、"
        f"duplicates={duplicates}、license_unknown={license_unknown}、"
        f"rejected={rejected}、quarantined={quarantined}。",
        "QUARANTINED / REJECTED 样本不进任何 split，也不计入任何统计。",
    )
    return CleanReport(
        input_count=input_count,
        kept=kept,
        duplicates=duplicates,
        license_unknown=license_unknown,
        rejected=rejected,
        quarantined=quarantined,
        normalized_samples=tuple(normalized),
        notes=notes,
    )


class _UnionFind:
    """标准库并查集（无第三方依赖）；用于把同源样本聚成不可分割 cluster。"""

    def __init__(self, size: int) -> None:
        self._parent: list[int] = list(range(size))
        self._rank: list[int] = [0] * size

    def find(self, item: int) -> int:
        """路径压缩查找根节点。"""

        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        """按秩合并，保证确定性。"""

        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self._rank[root_left] < self._rank[root_right]:
            root_left, root_right = root_right, root_left
        self._parent[root_right] = root_left
        if self._rank[root_left] == self._rank[root_right]:
            self._rank[root_left] += 1


def _cluster_key(members: Sequence[DatasetSample]) -> list[list[Any]]:
    """cluster 的确定性键：成员 (engine, fault_family, seed, workload_group, id) 排序。"""

    rows: list[list[Any]] = [
        [
            member.engine,
            member.fault_family,
            int(member.fault_seed),
            member.workload_group,
            member.sample_id,
        ]
        for member in members
    ]
    rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))
    return rows


def split_samples(
    samples: Sequence[DatasetSample],
    *,
    ratios: tuple[int, int, int] = SPLIT_RATIOS,
    suite_version: str,
) -> SplitManifest:
    """并查集聚类 + ``canonical_digest`` 分桶（AC-14①，无 RNG、可复现）。

    边（设计 §7.2）：同 ``fault_seed`` ∨ 同 ``workload_group`` ∨
    同 ``(engine, fault_family, workload_group)``。连通分量 = 一个不可分割
    cluster。``QUARANTINED`` / ``REJECTED`` / 未知标签样本被排除。

    分桶：``bucket = int(canonical_digest(cluster_key)[:8], 16) % 100``；
    ``< ratios[2]`` → test，``< ratios[2] + ratios[1]`` → dev，其余 → train。
    """

    if sum(ratios) != SPLIT_BUCKETS or any(value < 0 for value in ratios):
        raise BenchmarkInvalidError(
            "split 比例之和必须等于 SPLIT_BUCKETS 且各项非负",
            details={"ratios": list(ratios), "buckets": SPLIT_BUCKETS},
        )

    # 只保留可入集样本（禁止把 QUARANTINED 计入任何 split 或统计）。
    kept = [
        sample
        for sample in samples
        if _effective_label(sample) in PASSABLE_LABELS
        and not _license_unknown(sample.license_id)
    ]

    uf = _UnionFind(len(kept))
    key_functions: tuple[Callable[[DatasetSample], Any], ...] = (
        lambda item: ("seed", item.fault_seed),
        lambda item: ("workload_group", item.workload_group),
        lambda item: ("triple", item.engine, item.fault_family, item.workload_group),
    )
    for key_function in key_functions:
        buckets: dict[Any, list[int]] = {}
        for index, sample in enumerate(kept):
            buckets.setdefault(key_function(sample), []).append(index)
        for indices in buckets.values():
            first = indices[0]
            for other in indices[1:]:
                uf.union(first, other)

    clusters: dict[int, list[DatasetSample]] = {}
    for index, sample in enumerate(kept):
        clusters.setdefault(uf.find(index), []).append(sample)

    test_cut = ratios[2]
    dev_cut = ratios[2] + ratios[1]
    assignments: dict[str, str] = {}
    sample_assignments: dict[str, str] = {}
    counts: dict[str, int] = {name: 0 for name in SPLIT_NAMES}
    for members in clusters.values():
        key = _cluster_key(members)
        digest = canonical_digest(key)
        bucket = int(digest[:8], 16) % SPLIT_BUCKETS
        split = "test" if bucket < test_cut else (
            "dev" if bucket < dev_cut else "train"
        )
        cluster_id = "cluster_" + digest[:16]
        assignments[cluster_id] = split
        for member in members:
            sample_assignments[member.sample_id] = split
            counts[split] += 1

    groups = _group_sets(kept, sample_assignments)
    split_groups: dict[str, dict[str, list[str]]] = {
        name: {
            dimension: sorted(groups[name][dimension])
            for dimension in DIMENSIONS
        }
        for name in SPLIT_NAMES
    }
    # leakage_count 由实测重算（不是常量 0）。
    leakage_count = measure_group_leakage(kept, sample_assignments)
    seed_disjoint = measure_dimension_disjoint(
        kept, sample_assignments, dimension="seed"
    )
    workload_group_disjoint = measure_dimension_disjoint(
        kept, sample_assignments, dimension="workload_group"
    )

    train_texts = [
        item.text
        for item in kept
        if sample_assignments.get(item.sample_id) == "train"
    ]
    # 复用 T01 文本扫描；命中即 fail closed，绝不放进训练样本。
    hits = scan_texts_for_oracle_leak(train_texts)
    assert_no_oracle_leak(train_texts)
    oracle_in_training_samples = bool(hits)

    empty_splits = tuple(
        name for name in SPLIT_NAMES if counts.get(name, 0) == 0
    )
    digest_payload: dict[str, Any] = {
        "strategy": "group_split",
        "suite_version": suite_version,
        "group_keys": list(GROUP_KEYS),
        "ratios": list(ratios),
        "cluster_count": len(clusters),
        "assignments": dict(sorted(assignments.items())),
        "sample_assignments": dict(sorted(sample_assignments.items())),
        "counts": dict(sorted(counts.items())),
    }
    split_digest = canonical_digest(digest_payload)
    notes: list[str] = [
        "group split：并查集三维度聚类 + canonical_digest 分桶，无 RNG。",
        f"leakage_count={leakage_count} 由 seed / workload_group / "
        "(engine,fault_family,workload_group) 三维度两两求交实测得出。",
        f"规模目标 {TARGET_MIN_SAMPLES}–{TARGET_MAX_SAMPLES} 条（PRD P0-W10-04）"
        f"为目标而非实测；本清单实际样本数={len(kept)}，条数不等于有效训练规模。",
    ]
    if empty_splits:
        notes.append(
            "空 split 如实登记（design R-06）：" + ", ".join(empty_splits)
        )
    return SplitManifest(
        strategy="group_split",
        suite_version=suite_version,
        group_keys=GROUP_KEYS,
        ratios=ratios,
        cluster_count=len(clusters),
        assignments=assignments,
        sample_assignments=sample_assignments,
        split_groups=split_groups,
        counts=counts,
        leakage_count=leakage_count,
        seed_disjoint=seed_disjoint,
        workload_group_disjoint=workload_group_disjoint,
        oracle_in_training_samples=oracle_in_training_samples,
        empty_splits=empty_splits,
        split_digest=split_digest,
        frozen_at=datetime.now(timezone.utc).isoformat(),
        notes=tuple(notes),
    )


__all__ = [
    "DIMENSIONS",
    "GROUP_KEYS",
    "PASSABLE_LABELS",
    "SPLIT_BUCKETS",
    "SPLIT_NAMES",
    "SPLIT_RATIOS",
    "TARGET_MAX_SAMPLES",
    "TARGET_MIN_SAMPLES",
    "CleanReport",
    "DatasetLabel",
    "DatasetSample",
    "OracleLeakDetected",
    "SplitManifest",
    "assert_no_group_leakage",
    "clean_samples",
    "content_digest",
    "measure_dimension_disjoint",
    "measure_group_leakage",
    "split_samples",
]
