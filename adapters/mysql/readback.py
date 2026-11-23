"""W08 独立只读回读适配器（AC-05 异路径采集）。

硬规则（可逐条核对，延续 W07 `MysqlPrepareProbe` 口径）：

1. 本模块**没有任何接受 SQL 文本 / host / port / DSN 的入口**：`read_target()`
   只吃 `TargetObject`（冻结 dataclass，四元组在构造期已过 `READBACK_TARGETS`
   封闭白名单）；
2. SQL 原文由模块级字典按白名单条目**固化**，参数一律 `%s` 绑定主键值；
3. 使用**独立只读 client**（`read_only=True`、`autocommit=True`），与执行连接
   不同实例：`client_identity()` 返回该 client 的 `id()` 供 AC-05 断言；
4. 不复用 `step_observed.result`，不在执行事务内读（`MysqlClient` 每次
   `fetch_all` 新建连接、事务外）。
"""

from __future__ import annotations

from core.tasks.models import new_id, utc_now
from core.verification.readback import (
    READBACK_TARGETS,
    ReadbackSample,
    TargetObject,
    validate_target,
)
from database.mysql.connection import MysqlClient

# 固定 SQL：标识符全部来自封闭白名单常量（非客户端输入），只有主键值走绑定参数。
_READBACK_SQL: dict[tuple[str, str, str, str], str] = {
    (db, table, key_column, column): (
        f"SELECT `{column}` AS readback_value FROM `{db}`.`{table}` "
        f"WHERE `{key_column}` = %s LIMIT 1"
    )
    for (db, table, key_column, column) in READBACK_TARGETS
}


class MysqlReadbackAdapter:
    """`ReadbackPort` 的 MySQL 实现：独立只读 client、事务外、白名单固定 SQL。"""

    def __init__(self, ro_client: MysqlClient) -> None:
        if not ro_client.read_only:
            raise ValueError("回读适配器必须使用 read_only=True 的独立只读 client")
        self._client = ro_client

    @property
    def read_only(self) -> bool:
        return self._client.read_only

    def client_identity(self) -> int:
        """回读 client 的对象标识；AC-05 断言其 `!= id(执行 client)`。"""
        return id(self._client)

    def read_target(self, target: TargetObject) -> ReadbackSample:
        """按白名单固定 SQL 读一行；无行时 `value=None`（上层判 UNKNOWN）。"""
        if not isinstance(target, TargetObject):
            raise TypeError("MysqlReadbackAdapter.read_target 只接受 TargetObject")
        validate_target(target)
        sql = _READBACK_SQL[
            (target.database, target.table, target.key_column, target.column)
        ]
        rows = self._client.fetch_all(sql, (target.key_value,), max_rows=1)
        value = rows[0].get("readback_value") if rows else None
        return ReadbackSample(
            value=value,
            sample_id=new_id("readback"),
            collected_at=utc_now(),
        )


__all__ = ["MysqlReadbackAdapter"]
