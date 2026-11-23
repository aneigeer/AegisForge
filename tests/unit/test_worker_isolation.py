"""W05 单元测试：Worker 与数据库适配层隔离。"""

from __future__ import annotations

import inspect
from pathlib import Path

from core.worker.worker import Worker

_WORKER_DIR = Path(__file__).resolve().parents[2] / "core" / "worker"
_BANNED_SNIPPETS = (
    "import pymysql",
    "psycopg",
    "from database",
    "import database",
    "from adapters",
    "MysqlClient",
    "PgClient",
    "ro_dsn",
    "rw_dsn",
)


def _worker_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_WORKER_DIR.glob("*.py"))
    }


def test_worker_sources_do_not_reference_database_or_drivers() -> None:
    sources = _worker_sources()
    assert sources, "core/worker 下未找到任何源码"

    for name, source in sources.items():
        for snippet in _BANNED_SNIPPETS:
            assert snippet not in source, f"{name} 包含被禁止的依赖：{snippet}"


def test_worker_constructor_has_no_dsn_parameter() -> None:
    parameters = inspect.signature(Worker.__init__).parameters
    names = set(parameters)

    assert "signing_key" in names
    assert "lease_port" in names
    assert "gateway_port" in names
    assert not any(name == "dsn" or name.endswith("_dsn") for name in names), names
