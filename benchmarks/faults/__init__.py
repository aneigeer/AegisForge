"""W04 故障库：Manifest、双引擎注入器与 Ground Truth Oracle 数据目录。

Oracle 真值只存放于 ``benchmarks/faults/oracle/*.json``，只能经
``core.faults.store.load_oracle`` 显式加载，不得进入 Agent 上下文或日志。
"""

__all__: list[str] = []
