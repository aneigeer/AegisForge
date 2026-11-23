"""W01 依赖与运行时契约：Python 3.14、双引擎依赖、无训练依赖。"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _dependencies() -> str:
    return "\n".join(_pyproject()["project"]["dependencies"]).lower()


def test_python_requires_314() -> None:
    assert _pyproject()["project"]["requires-python"] == ">=3.14,<3.15"


def test_dual_engine_dependencies_declared() -> None:
    deps = _dependencies()
    for package in ("fastapi", "pydantic", "pymysql", "psycopg", "jsonschema", "httpx"):
        assert package in deps


def test_no_training_dependencies() -> None:
    deps = _dependencies()
    for package in ("torch", "transformers", "peft", "trl", "accelerate", "deepspeed"):
        assert package not in deps


def test_lockfile_includes_new_dependencies() -> None:
    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").lower()
    assert "psycopg==" in lock
    assert "jsonschema==" in lock
    assert "python 3.14" in lock or "python 3.14" in lock
