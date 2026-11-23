# ADR-0002 控制库选型与迁移

- 状态：Accepted（本机开发）
- 日期：2026-11-07
- 相关：V2.2 W01/W03

## 背景

V2.2 要求控制库与目标数据库分离，且禁止复用目标库凭据。现有 Harness 元数据表（tasks/task_events/tool_calls/checkpoints）位于 MySQL 独立库 `dg_system`。

## 决策

1. 控制库使用 MySQL 8.0.46 的独立数据库 `dg_system`，与 `dg_lab`、`dg_lab_pg` 物理/逻辑隔离。
2. 控制库账号与应用工具账号分离；应用进程只使用受控 DSN，管理员凭据仅初始化脚本使用。
3. 迁移继续使用版本化 SQL（`database/migrations/`），由 `scripts/migrate.py` 幂等应用并记录 `schema_migrations`。
4. V2.2 需要的 tenants/users/memberships/environments/evidence 表在 W03 通过新增迁移引入；本周不提前建表。

## 备选方案与取舍

- 控制库改用 PostgreSQL：与双引擎目标一致，但会迁移现有任务表与脚本，收益低于成本。
- 复用目标库：违反凭据隔离与故障注入边界，直接排除。

## 后果

- 控制库故障不影响 LAB，但控制库不可用时任务/审计不可写，必须 fail closed。
- 跨库无分布式事务；审批与写操作按 W06 的幂等/核对策略处理。

## 验证方式

- `scripts/migrate.py` 连续执行返回空变更；
- `tests/integration/test_executor.py` 任务/事件/检查点持久化；
- 后续 W03 增加 tenant/evidence 归属测试与 `state_version` 并发测试。

## 已知限制

- 当前控制库仍为单实例，无备份/恢复演练（W11）。
