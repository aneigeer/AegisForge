"""P2-T1B：四类外部基准的**转换器**（JSONL → `ExternalCase`）。

输入约定：每个来源一个 `cases.jsonl`（每行一个对象），字段名按来源不同，
本模块只做**字段改名 + 取值校验**，不做语义推断、不补默认真值。

**语料缺失即失败**：路径不存在/不是 JSONL/字段不全一律抛 `CorpusMissingError`，
绝不产出"看起来有数据"的占位案例（ENV 口径，见台账 §24）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from benchmarks.external.schema import CorpusMissingError, ExternalCase

#: 各来源的字段映射：目标字段 → 源字段候选（按顺序取第一个存在的）。
_FIELD_MAPS: dict[str, dict[str, tuple[str, ...]]] = {
    "diagdb": {
        "source_case_id": ("id", "case_id"),
        "engine": ("db", "engine"),
        "scenario": ("fault", "scenario"),
        "fault_seed": ("seed", "data_seed"),
    },
    "dbot": {
        "source_case_id": ("case_id", "id"),
        "engine": ("database", "engine"),
        "scenario": ("issue_type", "scenario"),
        "fault_seed": ("data_seed", "seed"),
    },
    "bird_critic": {
        "source_case_id": ("question_id", "id"),
        "engine": ("db_engine", "engine"),
        "scenario": ("category", "scenario"),
        "fault_seed": ("seed", "data_seed"),
    },
    "benchbase": {
        "source_case_id": ("workload_id", "id"),
        "engine": ("engine", "db"),
        "scenario": ("scenario", "fault"),
        "fault_seed": ("seed", "data_seed"),
    },
}

SUPPORTED_SOURCES: tuple[str, ...] = tuple(_FIELD_MAPS)


def _pick(row: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _read_jsonl(source: str, path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise CorpusMissingError(source, str(path), "文件不存在")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # pragma: no cover - 权限/占用等
        raise CorpusMissingError(source, str(path), f"读取失败：{type(exc).__name__}") from exc
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorpusMissingError(
                source, str(path), f"第 {index} 行不是合法 JSON：{exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise CorpusMissingError(source, str(path), f"第 {index} 行不是对象")
        rows.append(payload)
    if not rows:
        raise CorpusMissingError(source, str(path), "文件为空（0 条案例）")
    return rows


def convert(source: str, path: Path) -> tuple[ExternalCase, ...]:
    """把某个来源的 `cases.jsonl` 归一化为 `ExternalCase` 元组（字段缺失即失败）。"""
    if source not in _FIELD_MAPS:
        raise ValueError(f"不支持的来源：{source!r}，可选 {SUPPORTED_SOURCES}")
    field_map = _FIELD_MAPS[source]
    cases: list[ExternalCase] = []
    for index, row in enumerate(_read_jsonl(source, Path(path)), start=1):
        values: dict[str, Any] = {}
        for target, keys in field_map.items():
            value = _pick(row, keys)
            if value is None:
                raise CorpusMissingError(
                    source,
                    str(path),
                    f"第 {index} 行缺少字段 {target}（候选 {list(keys)}）",
                )
            values[target] = value
        try:
            seed = int(values["fault_seed"])
        except (TypeError, ValueError) as exc:
            raise CorpusMissingError(
                source, str(path), f"第 {index} 行 fault_seed 不是整数"
            ) from exc
        try:
            cases.append(
                ExternalCase(
                    source=source,
                    source_case_id=str(values["source_case_id"]),
                    engine=str(values["engine"]),
                    scenario=str(values["scenario"]),
                    fault_seed=seed,
                    notes=str(row.get("notes") or ""),
                )
            )
        except ValueError as exc:
            raise CorpusMissingError(source, str(path), f"第 {index} 行非法：{exc}") from exc
    return tuple(cases)


def converters() -> dict[str, Callable[[Path], tuple[ExternalCase, ...]]]:
    """按来源名返回转换函数（便于 CLI/脚本批量调用）。"""

    return {source: (lambda path, _s=source: convert(_s, Path(path))) for source in _FIELD_MAPS}


__all__ = ["SUPPORTED_SOURCES", "convert", "converters"]
