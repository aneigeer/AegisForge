"""W01 依赖与运行时契约：Python 版本策略、双引擎依赖、无训练依赖。

V5.0 收尾起，版本策略从"锁死 3.14"改为"声明可验证的下限、不锁小版本上限"——
库形态的可复用组件（core.guardrails / core.eval / core.evolution）必须能被
其他 Python 项目直接 `pip install -e`，因此约束只保留"必需的下限"。
本文件用契约测试守住这条策略，避免有人再把它改回单一小版本。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _dependencies() -> str:
    return "\n".join(_pyproject()["project"]["dependencies"]).lower()


def test_python_requires_policy_is_lower_bound_only() -> None:
    spec = str(_pyproject()["project"]["requires-python"])
    floor = re.match(r">=\s*(\d+)\.(\d+)", spec)
    assert floor is not None, f"版本下界必须写成 >=X.Y 形式: {spec!r}"
    assert (int(floor.group(1)), int(floor.group(2))) <= (3, 12), (
        f"版本下界不应高于 3.12，否则主项目无法安装: {spec!r}"
    )
    # 不锁单一小版本：允许 3.12 及以上的任何 3.x（含 3.13/3.14/3.15）
    assert "<" not in spec, f"不应设置小版本上限: {spec!r}"


def test_python_floor_is_currently_supported_by_runtime() -> None:
    import sys

    spec = str(_pyproject()["project"]["requires-python"])
    floor = re.match(r">=\s*(\d+)\.(\d+)", spec)
    assert floor is not None
    assert sys.version_info[:2] >= (int(floor.group(1)), int(floor.group(2))), (
        f"当前解释器 {sys.version_info[:2]} 低于声明的下界 {spec}"
    )


def test_dual_engine_dependencies_declared() -> None:
    deps = _dependencies()
    for package in ("fastapi", "pydantic", "pymysql", "psycopg", "jsonschema", "httpx"):
        assert package in deps


def test_no_training_dependencies() -> None:
    deps = _dependencies()
    for package in ("torch", "transformers", "peft", "trl", "accelerate", "deepspeed"):
        assert package not in deps


def test_dependencies_do_not_pin_micro_versions() -> None:
    """依赖只约束必需的下限（以及会破坏 API 的主版本上限），不锁小版本。"""
    deps = _pyproject()["project"]["dependencies"]
    for item in deps:
        # 允许 "pkg>=2.0,<3.0" 这种主版本上限；禁止 "pkg==x.y.z" 精确锁定
        assert "==" not in item, f"依赖不应精确锁定版本: {item}"


def test_lockfile_includes_new_dependencies() -> None:
    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").lower()
    assert "psycopg==" in lock
    assert "jsonschema==" in lock
    assert "python 3.1" in lock  # 锁文件注明生成时的解释器版本
