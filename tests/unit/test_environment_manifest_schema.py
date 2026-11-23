"""W01 环境指纹 Schema 校验（示例与本地生成文件）。"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs" / "environment-manifest.schema.json"
EXAMPLE = ROOT / "deploy" / "environment-manifest.example.json"
GENERATED = [
    ROOT / "deploy" / "environment-manifest.mysql.json",
    ROOT / "deploy" / "environment-manifest.postgresql.json",
]


def test_example_manifest_matches_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)


@pytest.mark.parametrize("path", GENERATED, ids=lambda p: p.name)
def test_generated_manifest_matches_schema(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.name} 未生成；先运行 scripts/environment_manifest.py")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(payload)
    assert "password" not in path.read_text(encoding="utf-8").lower()
