"""W05 静态隔离：模型服务不持有目标库凭据，也不加入 lab_net。

网络运行态（Docker）在无 Docker 环境时无法验证；这里只做可重复的静态负测，
运行态结论必须在有 Docker 的环境按 deploy/network/README.md 复测。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_MODEL_PATTERNS = (
    "import pymysql",
    "import psycopg",
    "from database",
    "import database",
    "from adapters",
    "import adapters",
    "MysqlClient",
    "PgClient",
    "ro_dsn",
    "rw_dsn",
    "pg_ro_dsn",
    "pg_rw_dsn",
    "control_dsn",
    "admin_dsn",
)


def _model_files() -> list[Path]:
    return sorted(path for path in (ROOT / "core" / "models").rglob("*.py"))


def test_model_package_never_imports_database_or_dsn() -> None:
    files = _model_files()
    assert files, "core/models 下未找到 Python 文件"

    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_MODEL_PATTERNS:
            if pattern in text:
                violations.append(f"{path.relative_to(ROOT)}: {pattern}")
    assert not violations, "模型包出现数据库凭据/直连痕迹：" + "; ".join(violations)


def test_model_service_is_not_attached_to_lab_net() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    model_block = compose.split("model_service:", 1)[1].split("\nnetworks:", 1)[0]
    network_lines = {
        match.group(1).strip()
        for match in re.finditer(r"^\s*-\s*(\S+)\s*$", model_block, flags=re.MULTILINE)
    }
    assert "model_net" in network_lines
    assert "lab_net" not in network_lines, "model_service 不得加入 lab_net"
