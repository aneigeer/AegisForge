# W07｜单一 R2 动作规范与操作风险评估

> 依据：`DBGuardian_W07_单引擎沙箱修复.docx` §二 D1、§三、§四；引擎选择见 `docs/adr/ADR-0007-single-engine-lab-repair.md`。

## 1. 动作定义

| 项 | 值 |
|---|---|
| 名称 / 版本 | `mysql_lab_update_order_note` / `1.0` |
| 引擎与环境 | MySQL 8.0.46，仅 `LAB`；`PROD_READ_ONLY` 无写入口 |
| 风险等级 / 副作用 | `R2` / `write`；必须人工审批 |
| 唯一 SQL 形态 | `UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s AND note <=> %s LIMIT 1` |
| 目标白名单（服务端硬编码） | database=`dg_lab`、table=`seed_orders`、column=`note`、key=`order_id` |
| 主键范围 | 2001..2200（对应 `01_schema.sql` 的 200 行种子数据） |
| 影响范围 | 恰好 1 行、1 列；`LIMIT 1` + 主键定位 |
| 资源预算 | 单次 1 条 UPDATE；`note` 长度 ≤128；超时 5000 ms |
| 幂等键 | `apr:<approval_id>`（一张审批只允许一次写） |
| 停止条件 | 目标不存在、`expected_note` 不匹配、affected≠1 且结果不是幂等重放、连接只读、任何越界参数 |

## 2. 固定参数 Schema（`extra="forbid"`）

| 字段 | 类型 / 约束 | 用途 |
|---|---|---|
| `database` | 字面量 `dg_lab` | 目标库白名单，纳入 `target_objects` |
| `table` | 字面量 `seed_orders` | 目标表白名单，纳入 `target_objects` |
| `order_id` | int，2001..2200 | 主键定位 |
| `keys` | str，形如 `order_id=<id>` | 目标对象声明；工具层硬校验与 `order_id` 一致，防止"批准对象 ≠ 执行对象" |
| `note` | str，≤128 | 新值 |
| `expected_note` | str \| null，≤128 | 执行前基线；纳入 `parameters`/`action_digest`，前置条件变化即旧审批失效 |

`target_objects` 抽取规则：`object_type=row`、`database_field=database`、`name_field=table`、`keys_field=keys`、`required=true`。

## 3. 前置条件（D2，服务端事实，不进摘要的人工输入一律排除）

1. 环境指纹与 `environment_id` 匹配；
2. 目标库身份与权限：连接必须是可写的 `dg_rw`，且 `read_only=False`；
3. 目标对象存在（`order_id` 命中且唯一）；
4. 工具版本 / 策略版本匹配；
5. **执行前基线**：`note` 的当前值必须等于 `expected_note`；
6. 表大小 / 磁盘余量 / 锁 / 长事务 / 当前负载：W07 D2 由 Prepare 采集并持久化；本动作规模固定为单行，任一检查非 PASS 即 fail closed。

前置条件变化时必须重新 Prepare 并重新审批，不得沿用旧审批。工具层的 `WHERE note <=> expected_note` 是最后一道防线：即使 Gateway 复核通过，对象在两次读之间发生变化也会让 `affected=0`，此时返回 `DENIED / PREREQUISITE_CHANGED`，不猜测执行结果。

## 4. 结果语义

| 结果 | 条件 | 处理 |
|---|---|---|
| `SUCCESS` | affected=1，前后快照可对照 | 写 `step_observed`，任务继续 |
| `SUCCESS`（幂等空操作） | 执行前基线检查通过，但 `UPDATE` 返回 affected=0 且复核后当前值已等于目标 `note`（两次读之间的竞态） | 标记 `idempotent_noop=true`，不重复计数 |
| `DENIED / PREREQUISITE_CHANGED` | `expected_note` 不匹配或对象已被他人改动 | 旧审批失效，重新 Prepare + 重新审批；不重试 |

> 注意：用**同一份审批参数**（`expected_note` 仍为旧值）重复调用时，执行前基线检查会直接拒绝，这是预期的 fail closed，不算幂等空操作；真正的重复调用由 Executor 的幂等键重放（`_replay_if_possible`）在执行前拦截。
| `DENIED / POLICY_DENIED` | 只读连接、越界参数、keys 与主键不一致 | 工具未执行 |
| `TIMEOUT` / 连接异常 | 由 Executor 归一为 `UNKNOWN_OUTCOME` | 进 `NEEDS_RECONCILIATION`，禁止盲目重试 |

## 5. 补偿与人工路径

- 补偿动作：把 `note` 写回 `before.note`。**它是独立 R2 动作**，需要新的 Prepare、新的 `approval_id` 与新的审批，不称为"回滚"。
- 证据不足（StepPrepared 有行、StepObserved 缺失，或结果未知）：转 `NEEDS_RECONCILIATION` → `MANUAL_INTERVENTION`，由人工按 `before` 快照决定是否发起补偿。
- 禁止：复用 `approval_id`、把"审批已耗"当作"操作已执行"、自动重试非幂等操作。

## 6. 验证计划（D5）

1. 在干净 LAB 快照上重复执行，记录前后 `note`、affected、耗时；
2. 六类拒绝负测：无审批、审批过期、改参数、换环境、越界目标、重复消费；
3. 确认 PostgreSQL 侧仍只有 7 个只读工具，没有任何 `side_effect=write` 定义；
4. 确认 `PROD_READ_ONLY` 没有任何 Agent 写入口；
5. 整理动作运行手册、回退/补偿说明、演示日志。

## 7. 风险登记

| 风险 | 缓解 |
|---|---|
| 为演示扩大影响面 | 服务端硬编码白名单 + `LIMIT 1` + 主键范围 2001..2200 |
| MySQL DDL 隐式提交 | 本动作不使用 DDL，全部为受控 DML |
| 前置条件在审批后变化 | `expected_note` 纳入摘要 + SQL 层 `<=>` 复核 + affected 判定 |
| 跨库无分布式原子事务 | 不宣称 Exactly Once；幂等键、`action_digest` 核对、停止策略 |
| 响应丢失 / Worker 崩溃 | StepPrepared/StepObserved、`UNKNOWN_OUTCOME` → `NEEDS_RECONCILIATION` |
| 补偿被误当回滚 | 本规范与运行手册统一口径：补偿是需另审批的独立动作 |
