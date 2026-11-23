# DBGuardian V2.2 W04 只读诊断流程（双引擎 × 三类 P0）

本文描述 `core/diagnosis/{evidence_mapping,analyzers,runner}.py` 的输入、证据映射、
判定规则、反证/未知项、停止条件与适用范围。所有流程只读，不执行任意 SQL，不读取
Oracle 真值（`expected_root_cause` 等仅存在于 benchmark 验收路径）。

## 1. 总体流程

```
Manifest（公开字段）
  → DiagnosisRunner 创建诊断任务（goal 含 scenario_id，tenant/environment 显式）
  → 按 allowed_tools 经 Adapter 调用固定只读工具
  → 每个 ToolResult 映射为 Evidence（归属、时间窗、状态、质量、完整性）
  → 场景分析器（规则）汇总支持/反证/缺失证据
  → DiagnosisResult(status, hypotheses, evidence_ids, missing_evidence, stop_conditions)
```

- 任务状态：`PENDING → PLANNING → OBSERVING → DIAGNOSING → COMPLETED`。
- 证据写入失败：任务标记 `FAILED`，返回 `UNKNOWN`，不伪造假设。
- 工具 `DENIED/NOT_SUPPORTED/TIMEOUT/截断`：如实写入证据并进入缺失项；不会转写为成功。
- `DiagnosisResult.status=PARTIAL` 允许任务仍为 `COMPLETED`；能力缺失通过
  `missing_evidence` 表达。
- 所有 `Hypothesis.supporting_evidence_ids` 必须存在于 `result.evidence_ids`。

## 2. 证据映射（ToolResult → EvidenceCreate）

| ToolResult | CollectionStatus | EvidenceQuality | Completeness |
|---|---|---|---|
| SUCCESS，有 started_at+observed_at，未截断 | SUCCESS | HIGH | COMPLETE |
| SUCCESS，缺少明确观测窗口或截断 | SUCCESS | MEDIUM | PARTIAL（截断时） |
| PARTIAL，有数据 | PARTIAL | LOW | PARTIAL |
| PARTIAL，无数据 | PARTIAL | UNKNOWN | MISSING |
| DENIED | DENIED | UNKNOWN | UNKNOWN |
| TIMEOUT | TIMEOUT | UNKNOWN | UNKNOWN |
| NOT_SUPPORTED | NOT_SUPPORTED | UNKNOWN | MISSING |
| ERROR / UNKNOWN_OUTCOME | UNKNOWN | UNKNOWN | UNKNOWN |

- `tenant_id/task_id/environment_id` 由 Runner 传入，服务端二次校验归属。
- `source_tool=result.tool_name`；`db_engine/db_version/adapter_version` 取自 `result.source`。
- `observed_at=result.observed_at or result.started_at`；时间窗为 `started_at → observed_at`。
- payload 结构：`tool_status + data + warnings + capability_ref + source_metadata +
  truncated + error`；`redacted_payload` 与 payload 相同（工具已脱敏），
  `redaction_version="tool-redaction-v1"`，`reproducible=True`。

## 3. 慢查询（slow_query）

### 输入工具

| 引擎 | allowed_tools | 关键数据 |
|---|---|---|
| MySQL | `get_query_stats`, `explain_query`, `get_table_stats` | digest/count_star/rows_examined/no_index_used；`access_type`；TABLE_ROWS/索引字节 |
| PostgreSQL | 同上 | query_stats（pg_stat_statements）；`Seq Scan`；reltuples/seq_scan/idx_scan |

### 判定规则

1. `EXPLAIN` 计划中出现 MySQL `access_type=ALL` 或 PostgreSQL `Seq Scan` → 支持
   “缺少可用索引/全表扫描”候选根因。
2. `query_stats` 中与场景沙箱表/高扫描量/无索引标记匹配的 digest 作为辅助支持。
3. `table_stats` 中沙箱表存在且 `index_bytes=0`（MySQL）或 `idx_scan=0 且 seq_scan>0`
   （PostgreSQL）作为索引缺失辅助支持。
4. 执行计划被截断、query_stats 无样本或 `NOT_SUPPORTED`、table_stats 不可用 →
   进入 `missing_evidence`，整体状态降为 `PARTIAL`。
5. `query_stats` 是累计统计（自统计重置起），不作为当前窗口的唯一证明；该假设写入
   `Hypothesis.assumptions`，且仅有累计统计时置信度封顶 `LOW`。
6. 若 EXPLAIN 显示索引访问且无全表扫描信号 → 候选假设置为 `REJECTED`，
   置信度 `LOW`，`contradicting_evidence_ids` 记录对应证据。

### 反证 / 未知项

- 反证：索引访问计划、扫描行数低、无 `no_index_used` 信号。
- 未知：计划截断、计划格式不可识别、query_stats 缺失/采样不足、沙箱表未覆盖。
- 置信度：`HIGH`（完整全表扫描 + 多源支持）→ `MEDIUM`（缺少摘要或存在截断/反证）
  → `LOW`（仅累计统计或反证）→ `UNKNOWN`（无可用证据）。

## 4. 连接压力（connection_pressure）

### 输入工具

| 引擎 | allowed_tools | 关键数据 |
|---|---|---|
| MySQL | `get_connection_snapshot`, `get_transaction_metadata`, `get_query_stats` | sessions/command/state、事务年龄、语句错误 |
| PostgreSQL | 同上 | pg_stat_activity 会话、xact 年龄、idle in transaction |

### 判定规则

1. 会话数取 `session_count`/`row_count`/sessions 长度；识别 `Sleep`/`idle` 会话。
2. `idle in transaction`：PostgreSQL state 直接判定；MySQL 由 `Sleep` 会话 +
   InnoDB `RUNNING/LOCK WAIT` 事务状态近似判定。
3. 长事务阈值 60s、空闲事务阈值 30s、会话数告警 100 均标注为
   **“建议值/未实测”**，不作为数据库真实上限。
4. `max_connections` 不在固定工具载荷内，无法确认时写入
   `max_connections 上限未确认（无法计算连接使用率）`。
5. 固定工具无法观测应用连接池，因此必须写入
   **`应用连接池状态未确认`**（若不存在该缺失项，不得给出连接池根因结论）。
6. 会话数接近已知 `max_connections`（≥80%）或存在空闲/长事务 → 候选假设；
   会话数明显低于上限且无空闲/长事务 → 假设 `REJECTED`。
7. 会话/事务快照截断、query_stats 不可用 → `PARTIAL` 并记录缺失项。

### 反证 / 未知项

- 反证：已知 `max_connections` 且使用率 <50%，无空闲、无长事务。
- 未知：`max_connections`、应用连接池、连接错误累计统计、截断。
- 置信度：`MEDIUM`（会话证据可用但连接池未知）→ `LOW`（无空闲/长事务信号）
  → `UNKNOWN`（无会话快照）。

## 5. 锁等待（lock_wait）

### 输入工具

| 引擎 | allowed_tools | 关键数据 |
|---|---|---|
| MySQL | `get_lock_wait_edges`, `get_lock_details`, `get_transaction_metadata`, `get_connection_snapshot` | waiting/blocking processlist、object_name、事务年龄/等待时长 |
| PostgreSQL | 同上 | blocked_pid/blocking_pid、relation、pg_blocking_pids、事务年龄 |

### 判定规则

1. 从 `edges` 构建阻塞边；MySQL `roots`、PostgreSQL `root_pids` 识别根阻塞者。
2. 优先选择根阻塞边生成假设：`会话/事务 B 持有对象 X 上的锁，会话 W 正在等待`。
3. 事务年龄优先取阻塞边自带值；缺失时按 pid 关联
   `get_transaction_metadata`；仍缺失则进入缺失项。
4. 等待时长来自锁视图的查询开始时间，不等于事务总年龄（写入 assumptions）。
5. 跨视图不一致：阻塞边数量与 `get_lock_details.waiting_count` 不一致、视图截断、
   权限缺失 → `PARTIAL/UNKNOWN`，并保留 `contradicting_evidence_ids`。
6. 无阻塞边但锁明细存在等待 → 假设 `UNKNOWN`（无法唯一确定阻塞者）；
   两边均无等待 → 假设 `REJECTED`（未观察锁等待）。

### 反证 / 未知项

- 反证：锁明细 `waiting_count=0` 且阻塞边为空。
- 未知：视图截断、权限不足（PROCESS/EXECUTE）、事务年龄缺失、跨视图不一致。
- 置信度：`HIGH`（边 + 明细 + 事务年龄，无截断/不一致）→ `MEDIUM`
  （缺少事务年龄或部分截断）→ `LOW`（截断/不一致）→ `UNKNOWN`（无可用视图）。

## 6. 停止条件

| 场景 | 停止条件 |
|---|---|
| 慢查询 | 预算耗尽（allowed_tools 全执行）；重复 digest/计划；query_stats 与 explain 均不可用；采样不足且计划截断 |
| 连接压力 | 预算耗尽；重复快照；connection snapshot 不可用；应用连接池/max_connections 始终未知（保留 PARTIAL） |
| 锁等待 | 预算耗尽；重复阻塞树/锁视图；lock edges 与 lock details 均不可用；锁等待消失（reset/事务结束） |

Runner 在 allowed_tools 全部执行后额外记录“allowed_tools 已全部执行（工具预算耗尽）”。

## 7. 适用范围与限制

- 仅适用于 LAB 环境与 W02 固定 7 个只读工具；不执行写操作、不执行 `EXPLAIN ANALYZE`。
- 结论为规则驱动的候选根因（`generated_by="rules-v0"`），不是概率预测。
- PostgreSQL 12 LAB 未安装 `pg_stat_statements`，慢查询诊断保留 `PARTIAL`，
  不伪造摘要统计；MySQL 的 `query_stats` 为累计统计，不代表当前窗口。
- `max_connections`、应用连接池、连接错误累计统计可能不可见，必须显式保留未知。
- Oracle 真值不进入任务目标、证据 payload、日志或 `DiagnosisResult`。
