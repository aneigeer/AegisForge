"""W05 Gateway 组件：执行信封、Worker 租约与唯一工具执行入口。

注意：本包不在 `__init__` 中导入 `leases`/`tool_gateway`，避免 Worker 仅需信封
签名能力时被迫加载数据库适配层。
"""
