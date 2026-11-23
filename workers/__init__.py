"""W07 收口：独立 Worker 进程的**组合根**包。

分层契约（`docs/w07/02-design.md` §7 与 W07 收口决议）：

- `core/worker/` 是**领域层**：`Worker` 只持有签名密钥与 `GatewayPort`/`LeasePort`
  两个端口协议，不 import 数据库适配层、不持有 DSN；
- `workers/` 是**组合根**：持有 DSN、import `adapters`，把控制库事实、LAB 执行栈
  （`adapters/lab_assembly.py::build_lab_stack`）与 `ToolGateway` 装配成一个可运行
  的进程。

因此"`workers/` 能连库"与"`core/worker/` 零数据库依赖"并不矛盾，两者是不同层。
静态守护见 `tests/unit/test_worker_layering.py`（正向）与
`tests/unit/test_worker_isolation.py`（反向）。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
