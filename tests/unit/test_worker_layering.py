"""W07 收口静态守护：`workers/` 是组合根，`core/worker/` 仍是干净领域层。

这一组断言不启动任何进程、不连数据库，只读源码与部署文件，因此可以在
`tests/unit` 里无条件运行。它们保护的是**架构不变量**，不是行为：

1. 生产装配必须引用唯一装配点 `adapters/lab_assembly.py::build_lab_stack`
   与唯一执行入口 `core/gateway/tool_gateway.py::ToolGateway`（AC④）；
2. `core/worker/` 不得出现数据库/适配层依赖（W05 隔离不变量，不能被本轮破坏）；
3. `workers/` 不得写裸 SQL、不得直连驱动（连接由 `build_lab_stack` 提供）；
4. 部署面（Dockerfile/compose/pyproject）必须真的把 `workers` 当进程入口。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_WORKERS_DIR = _ROOT / "workers"
_CORE_WORKER_DIR = _ROOT / "core" / "worker"

_CORE_WORKER_BANNED = (
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

_WORKERS_BANNED_DRIVERS = (
    "import pymysql",
    "import mysql.connector",
    "from mysql import connector",
    "import psycopg",
    "from psycopg",
)

#: 裸 SQL 特征（workers/ 只允许经既有仓储/服务接口读写）。
_RAW_SQL_PATTERNS = (
    re.compile(r"\bSELECT\b.+\bFROM\b", re.IGNORECASE),
    re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+dg_system\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bCREATE\s+TABLE\b", re.IGNORECASE),
)


def _py_sources(directory: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.py"))
    }


# ---------------------------------------------------------------------------
# 正向：组合根必须引用唯一装配点与唯一执行入口
# ---------------------------------------------------------------------------


def test_workers_package_references_lab_assembly_and_gateway() -> None:
    sources = _py_sources(_WORKERS_DIR)
    assert sources, "workers/ 下未找到任何源码"
    combined = "\n".join(sources.values())
    assert "build_lab_stack" in combined, "workers/ 必须引用唯一 LAB 装配点"
    assert "ToolGateway" in combined, "workers/ 必须装配唯一 ToolGateway"


def test_stack_module_imports_lab_assembly_and_tool_gateway() -> None:
    source = (_WORKERS_DIR / "stack.py").read_text(encoding="utf-8")
    assert "from adapters.lab_assembly import build_lab_stack" in source
    assert "from core.gateway.tool_gateway import ToolGateway" in source
    # 装配点必须是调用而不是"导入即忘"。
    assert "build_lab_stack(" in source
    assert "ToolGateway(" in source
    # 不允许自己手搓注册表（那等于开出第二条写入口）。
    assert "ToolRegistry()" not in source


def test_stack_is_the_only_module_building_the_gateway() -> None:
    builders = [
        name
        for name, source in _py_sources(_WORKERS_DIR).items()
        if "ToolGateway(" in source
    ]
    assert builders == ["stack.py"], builders


# ---------------------------------------------------------------------------
# 反向：领域层与组合根各自守住自己的边界
# ---------------------------------------------------------------------------


def test_core_worker_remains_free_of_database_dependencies() -> None:
    sources = _py_sources(_CORE_WORKER_DIR)
    assert sources, "core/worker 下未找到任何源码"
    for name, source in sources.items():
        for snippet in _CORE_WORKER_BANNED:
            assert snippet not in source, f"core/worker/{name} 含禁用依赖：{snippet}"


def test_workers_do_not_import_database_drivers_directly() -> None:
    for name, source in _py_sources(_WORKERS_DIR).items():
        for snippet in _WORKERS_BANNED_DRIVERS:
            assert snippet not in source, f"workers/{name} 直接依赖驱动：{snippet}"


def test_workers_contain_no_raw_sql() -> None:
    for name, source in _py_sources(_WORKERS_DIR).items():
        for pattern in _RAW_SQL_PATTERNS:
            match = pattern.search(source)
            assert match is None, f"workers/{name} 出现裸 SQL：{match.group(0)!r}"


def test_worker_domain_layer_does_not_import_workers_package() -> None:
    for name, source in _py_sources(_CORE_WORKER_DIR).items():
        assert "from workers" not in source, f"core/worker/{name} 反向依赖组合根"
        assert "import workers" not in source, f"core/worker/{name} 反向依赖组合根"


# ---------------------------------------------------------------------------
# 部署面：进程入口真的存在
# ---------------------------------------------------------------------------


def test_worker_dockerfile_uses_module_entrypoint() -> None:
    path = _ROOT / "deploy" / "Dockerfile.worker"
    assert path.is_file(), "缺少 deploy/Dockerfile.worker"
    content = path.read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "workers"]' in content
    assert "COPY workers ./workers" in content
    assert "COPY adapters ./adapters" in content
    assert "USER dguser" in content
    assert "uid 10001" in content or "10001" in content


def test_pyproject_ships_workers_package() -> None:
    content = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"workers*"' in content, "pyproject 未包含 workers 包，镜像内无法 python -m workers"


def test_docker_compose_declares_worker_without_model_network() -> None:
    content = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^  worker:\s*$", content, re.MULTILINE), "compose 缺少 worker 服务"
    worker_block = content.split("  worker:", 1)[1].split("\n  model_service:", 1)[0]
    assert "control_net" in worker_block and "lab_net" in worker_block
    assert "model_net" not in worker_block, "Worker 不得接入 model_net"
    assert "DG_MODEL_PROVIDER: disabled" in worker_block
    assert "DG_GATEWAY_SIGNING_KEY" in worker_block
