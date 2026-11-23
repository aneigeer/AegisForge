# 双引擎能力差异矩阵（V2.2 W02）

本地验证环境：MySQL 8.0.46（3306）、PostgreSQL 12.18（55432，无 `pg_stat_statements`）。
Compose 候选：MySQL 8.0.46、PostgreSQL 16.4（未运行验证）。

## 固定只读工具（每引擎 7 个）

| 统一工具名 | MySQL 实现 | PostgreSQL 实现 | 备注 |
| --- | --- | --- | --- |
| `get_connection_snapshot` | `performance_schema.threads` + `events_statements_current` | `pg_stat_activity` | SQL 字面量/查询文本脱敏 |
| `get_query_stats` | `events_statements_summary_by_digest` | `pg_stat_statements` | PG 扩展缺失时 `NOT_SUPPORTED` + warning，不伪造数据 |
| `get_lock_wait_edges` | `data_lock_waits` + `data_locks` + `threads` | `pg_locks` + `pg_blocking_pids` | 输出等待/阻塞边与根节点 |
| `get_lock_details` | `data_locks` | `pg_locks` | 锁对象/模式/granted |
| `get_transaction_metadata` | `information_schema.innodb_trx` | `pg_stat_activity` 的事务字段 | 事务年龄、状态、查询摘要 |
| `explain_query` | `EXPLAIN FORMAT=JSON` | `EXPLAIN (FORMAT JSON)` | 仅单条 `SELECT`/`WITH`，禁 `ANALYZE` 与多语句 |
| `get_table_stats` | `information_schema.TABLES` + `innodb_trx` | `pg_class` / `pg_stat_user_tables` / `pg_total_relation_size` | 行数估算、扫描、大小 |

## 能力状态（本地实测）

| 能力 | MySQL | PostgreSQL 12 本地 | PG 16 Compose |
| --- | --- | --- | --- |
| 连接快照 | AVAILABLE | AVAILABLE | 待探测 |
| 锁等待边/详情 | AVAILABLE | AVAILABLE | 待探测 |
| 事务元数据 | AVAILABLE | AVAILABLE | 待探测 |
| 查询摘要统计 | AVAILABLE | NOT_SUPPORTED（扩展未安装） | 待探测 |
| EXPLAIN JSON | AVAILABLE | AVAILABLE | 待探测 |
| 表统计 | AVAILABLE | AVAILABLE | 待探测 |

## 已知差异与不假设

- MySQL DDL 隐式提交；PG DDL 事务语义不同。W07 修复动作不得跨引擎假设一致。
- MySQL 使用 `performance_schema` consumer/表；PG 使用统计视图与扩展。缺失时必须返回 `PARTIAL/DENIED/NOT_SUPPORTED/UNKNOWN`。
- 只读约束：MySQL 由账号权限 + `TRANSACTION READ ONLY`；PG 由仅 `SELECT` 账号 + 适配层只读会话。
- 行数/超时/参数校验由适配层与工具 Pydantic 双层执行；不接受任意 SQL/host/port。
- 未验证项：PG16 镜像 digest、Compose 网络隔离（Docker daemon 不可用）。
