"""W08 读回端口：窄接口 + 封闭白名单（AC-05 异路径采集）。

硬约束：
- `ReadbackPort` 只接受 `TargetObject`，**不接受** SQL 字符串 / host / port / DSN；
- 目标四元组 `(database, table, key_column, column)` 必须命中 `READBACK_TARGETS`
  封闭白名单，`TargetObject` 构造期即校验（构造上就不能传非法目标）；
- 回读样本与执行连接分离：由 `adapters.mysql.readback.MysqlReadbackAdapter`
  用独立只读 client 采集，`client_identity()` 返回该 client 的 `id()` 供 AC-05 断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.errors import RequestInvalidError

# 目标库四元组的封闭白名单；新增目标必须显式改这里并评审（与 W07 冻结向量同源）。
# 标识符由服务端常量给出，客户端无法注入任意库/表/列。
READBACK_TARGETS: frozenset[tuple[str, str, str, str]] = frozenset(
    {("dg_lab", "seed_orders", "order_id", "note")}
)

# 白名单外的稳定原因码（复用既有 REQUEST_INVALID，不新增 ErrorCode 枚举）。
READBACK_TARGET_NOT_ALLOWED = "READBACK_TARGET_NOT_ALLOWED"


def validate_target(target: "TargetObject") -> None:
    """校验目标四元组命中封闭白名单；未命中抛 `REQUEST_INVALID`。"""
    key = (target.database, target.table, target.key_column, target.column)
    if key not in READBACK_TARGETS:
        raise RequestInvalidError(
            "回读目标不在封闭白名单内",
            details={"reason": READBACK_TARGET_NOT_ALLOWED, "target": list(key)},
        )


@dataclass(frozen=True)
class TargetObject:
    """目标行定位四元组 + 主键值；**不含任何 SQL / 连接字段**。"""

    database: str
    table: str
    key_column: str
    key_value: int
    column: str

    def __post_init__(self) -> None:
        if isinstance(self.key_value, bool) or not isinstance(self.key_value, int):
            raise ValueError("TargetObject.key_value 必须是整数主键")
        validate_target(self)


@dataclass(frozen=True)
class ReadbackSample:
    """一次独立只读采集的结果（带 sample_id 与采集时刻，可复核）。"""

    value: Any
    sample_id: str
    collected_at: datetime


@runtime_checkable
class ReadbackPort(Protocol):
    """独立只读回读端口：`read_only is True`、与执行 client 不同实例。"""

    @property
    def read_only(self) -> bool: ...

    def client_identity(self) -> int: ...

    def read_target(self, target: TargetObject) -> ReadbackSample: ...


__all__ = [
    "READBACK_TARGETS",
    "READBACK_TARGET_NOT_ALLOWED",
    "ReadbackPort",
    "ReadbackSample",
    "TargetObject",
    "validate_target",
]
