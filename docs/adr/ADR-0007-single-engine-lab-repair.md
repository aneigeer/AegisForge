# ADR-0007｜W07 单引擎沙箱修复的引擎与动作选择

- 状态：已接受（W07 D1，2026-11-11）
- 依据：`项目规划/项目规划2.2/DBGuardian_W07_单引擎沙箱修复.docx` §二 D1、§四、§五
- 关联：ADR-0001（运行时与双引擎）、ADR-0003（LAB 版本矩阵）、ADR-0005（action_digest 规范化）

## 决策

1. **引擎选 MySQL 8.0.46**（本机 3306，隔离库 `dg_lab`）；PostgreSQL 继续只读，不开放任何写入口。
2. **唯一 R2 动作**：`mysql_lab_update_order_note` —— 按主键更新 `dg_lab.seed_orders` 单行的 `note` 列，并带 `expected_note` 乐观前置校验。
3. 不提供通用 SQL 写工具；不接受任意 SQL / host / port / 表名 / 列名。

## 备选与否决理由

| 备选 | 否决理由 |
|---|---|
| PostgreSQL 12.18（55432 LAB） | W01 验收记录：PG 12 已 EOL、本机 LAB 未运行、Docker daemon 不可用；W02 记录 PG12 无 `pg_stat_statements`、且缺少 MySQL 等价的会话只读能力。选它会使 W07 门禁"至少一个引擎的一种有限 LAB 写操作可经人工审批执行"在本机无法验证 |
| 受控索引 / DDL 实验 | 任务书 §五点名"MySQL DDL 隐式提交"与"PG 并发索引失败留下 INVALID 对象"。DDL 无法用事务回滚，补偿必须是独立动作且需再次审批，显著抬高 W07 风险 |
| 双引擎同时开放写 | 直接违反 W07 §一"另一个引擎继续只读"与 §四验收标准 |

## 选择依据（本机可验证事实）

- `scripts/bootstrap_mysql.py:35-40`：`dg_rw` 仅被授予 `dg_lab.*` 的 DML/DDL，无跨库权限；`dg_ro` 只有 SELECT + performance_schema + PROCESS。
- `database/mysql/init/01_schema.sql:18-29`：`seed_orders` 有 200 行种子数据（`order_id` 2001..2200），`note VARCHAR(128) NULL`，非外键、不被其他表引用、不参与任何索引。
- W02 的 `MysqlClient` 与 W06 的 `ToolGateway` 已在 MySQL 路径上完整串联；`step_prepared` / `step_observed` / 审批消费都落在 `dg_system` 控制库，与 LAB 引擎解耦。

## 后果

- **正面**：影响面为单行单列，执行前后可精确对照；`note` 不参与约束或索引，补偿可定义为"把旧 note 写回"。补偿本身是**独立 R2 动作**，需要新审批，不称为回滚。
- **限制**：不开放 DDL、不开放跨表/批量更新、不开放生产环境写入；`PROD_READ_ONLY` 不提供任何 Agent 写入口。
- **不宣称 Exactly Once**：控制库事务与目标库写入是两笔独立事务，只保证"一张审批至多消费一次"；兜底是幂等键 + `action_digest` 核对 + 停止策略。
- 具体参数、白名单、资源预算与风险登记见 `docs/w07/00-action-spec.md`。
