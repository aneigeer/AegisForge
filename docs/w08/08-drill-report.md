# W08 T06｜生产调度 + 崩溃矩阵演练报告

> 对应任务卡：`docs/w08/03-task-cards.md:136-153`（T06）；设计 `docs/w08/02-design.md` §3.1/§4.3/§5/§6；
> 验收项 AC-17 / AC-18 / AC-19 / AC-21（`docs/w08/01-prd.md:146-150`）。
>
> 本文件**只写实跑结果**。没跑的部分在 §5「未执行项」逐条列出并说明原因，不写"应该通过"。

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| `sweep()` 进生产调度（AC-17） | 通过：`run_once()` 每轮**恰好 1 次** `sweep()`，失败不中断主循环 |
| 崩溃矩阵（AC-18，7 类注入） | 通过：7/7，`tool_calls` 增量 ≤1、`note` 变化 ≤1 次、`seq` 连续、无 `TOOL_CALL_RETRIED` |
| PAUSE / CANCEL 语义（AC-19） | 通过：PAUSE 不伪造"已停止"；CANCEL 经 `CANCEL_REQUESTED` 收敛，全程零执行 |
| 验证确定性（AC-21） | 通过：同一 `tool_call_id` 10 轮 `verdict` 集合大小 ==1 |
| 单元回归 | `pytest tests/unit` = **891 passed / 0 failed**（T06 新增 34 例，基线 857） |
| `RECONCILIATION_SWEPT`（设计 §5 第 8 行） | 通过：`sweep()` 成功即发（含空结果），payload 取自实际返回值与实测耗时 |
| `AGENT_RECOVERY_STARTED`（设计 §5 第 10 行） | 通过：`LOOP_DETECTED` / `NO_PROGRESS` / `UNKNOWN_OUTCOME` 三类接管各发一条，同一次接管幂等 |
| `--once` 真实子进程（AC-17 第三项） | 通过：`tests/integration/test_w08_worker_process.py` **2 passed**，任务收敛 `NEEDS_RECONCILIATION`、`tool_calls` 增量 0、`RECONCILIATION_SWEPT` 落库 |
| 受影响的既有集成用例复跑 | `test_worker_process.py` **4 passed**（真实子进程，生产装配已带 sweep）；`test_w08_recovery_e2e.py` **2 passed**（停止事件常量替换回归） |

执行环境：Windows 11 + 本机 MySQL（控制库 `dg_system` + LAB 目标库 `dg_lab`），
解释器 `.venv/Scripts/python.exe`（Python 3.14），跑前设 `DG_RECOVER_ON_STARTUP=false`
（避免进程启动自愈干扰注入事实）。

---

## 1. 生产调度（AC-17）

### 1.1 实现要点

- `workers/runner.py`：`RunReport` 新增 `reconciled: int = 0`，`merged()`/`to_public_dict()` 同步；
  `clean` 语义不变（`reconciled` **不算**不干净——核对收敛不代表发生副作用）。
- `run_once()` 顺序固定为 **先核对、后发现**：`_sweep_once()` → `discover_candidates()` → 逐候选执行。
  位置在代码里由注释与顺序共同固定（`workers/runner.py` 的 `run_once` docstring）。
- `sweep()` 参数固定 `tenant_id=stack.tenant_id`、`older_than_s=settings.reconcile_older_than_s`。
- `sweep()` 成功（含空结果）：写 `RECONCILIATION_SWEPT`，payload 为
  `scanned` / `converged` / `conclusions[]{task_id,tool_call_id,conclusion}` / `duration_ms`，
  全部取自 `sweep()` 的实际返回值与本轮 `perf_counter` 实测耗时。
- `sweep()` 抛错：只记 `RECONCILIATION_FAILED`（成功/失败互斥，不双发），
  `clean=False`，**本轮候选照常执行**；连续失败计数在成功一轮后归零。
- `reconciled` 只累计 `ReconcileResult.transitioned is True`（真的推进了状态）的条目。

### 1.2 断言证据

`tests/unit/test_worker_scheduler.py`（18 例，全部离线）：

| 用例 | 断言 |
|---|---|
| `test_run_once_calls_sweep_exactly_once` | `len(reconciler.calls) == 1`，入参 `{tenant_id, older_than_s}` 精确匹配 |
| `test_sweep_runs_once_even_with_many_candidates` | 3 个候选执行完，`sweep` 仍为 1 次 |
| `test_reconciled_counts_only_transitioned_results` | 3 条结论（2 真推进 + 1 已收敛）→ `reconciled == 2` |
| `test_sweep_failure_does_not_stop_candidate_execution` | `executed == 1`、`clean is False`、`failed == 1` |
| `test_sweep_failure_emits_reconciliation_failed_event` | 事件 `RECONCILIATION_FAILED`，payload 含 `code/error_code/reason/error_type/message_digest/consecutive_failures`，**不含错误原文** |
| `test_consecutive_sweep_failures_are_counted` / `test_successful_sweep_resets_consecutive_failures` | 连续失败 1→2→3，成功一轮后重新从 1 开始 |
| `test_merged_accumulates_reconciled` / `test_public_dict_exposes_reconciled` | 跨轮累加、对外字段可见 |
| `test_sweep_happens_before_discovery` | 调用顺序 `["sweep", "discover"]` |
| `test_stack_without_reconciler_keeps_original_behaviour` | 历史栈（无 `reconciler` 属性）`reconciled == 0`，不伪造核对 |
| `test_sweep_success_emits_reconciliation_swept` | 2 条结论 → `scanned=2`、`converged=1`、`conclusions` 三元组逐条对齐、`duration_ms >= 0`、`tool_calls_delta=0` |
| `test_sweep_success_emits_even_when_result_is_empty` | 空结果也发：`scanned=0`、`conclusions=[]` |
| `test_sweep_failure_does_not_emit_swept` / `test_swept_and_failed_share_scheduler_task_id` | 失败轮只有 FAILED；两类调度事件同一伪任务 |
| `test_swept_events_are_per_cycle` | `max_cycles=3` → 3 条 SWEPT（每轮一条） |

### 1.3 已知边界（不是缺陷，是设计选择）

1. **调度事件没有具体任务**：`task_events.task_id` 为 `NOT NULL` 且审计查询按 `tasks` JOIN 过滤租户，
   因此 `RECONCILIATION_FAILED` 挂在固定伪任务 `system:worker_scheduler` 上，
   **其 `tenant_id` 过滤列为 NULL**，`AuditService.query(tenant_id=...)` 查不到它。
   检索方式：按 `task_id='system:worker_scheduler' AND event_type='RECONCILIATION_FAILED'` 直查事件表
   （集成用例 `test_03` 即这么断言）。
2. **sweep 失败计入 `failed`**：`RunReport` 字段集已冻结（只增 `reconciled`），没有"核对失败"计数位；
   为满足 AC-17 的 `clean=False`，失败计入 `failed`（退出码因此非 0，语义正确：本轮存在未收敛事实）。
   权威证据仍是 `RECONCILIATION_FAILED` 事件。
3. **`reconcile_interval_s` 未使用**：设计 §4.3 的循环是"每个 `run_once()` 周期调用 1 次 sweep"，
   周期节奏由 `worker_poll_interval_s`/调用方决定；配置项 `DG_RECONCILE_INTERVAL_S` 本轮只被校验、未被读取。
4. **`scanned` 的口径**：`Reconciler.sweep()` 只返回**产出结论**的条目（二次确认已补齐 `Observed`
   的不产出结论），因此 `scanned = len(results)` 是"产出结论条数"，不是全表扫描行数；
   真实扫描总数需 W09 扩展 `sweep()` 的返回值（本卡未改 `reconciler.py` 内部）。
5. **`conclusions` 会被审计载荷上限截断**：`redact_payload` 的列表上限是 50 条（`MAX_AUDIT_ITEMS`），
   一次 sweep 命中 >50 条时事件里只保留前 50 条 + `<truncated N items>`（实测 72 条即如此）；
   `scanned` / `converged` 仍是完整计数。

---

## 2. 崩溃矩阵（AC-18 / AC-19）

文件：`tests/integration/test_w08_crash_matrix.py`（7 个独立用例，可单独 `-k` 定位）。
统一硬断言（`_assert_no_duplicate_effect`）：`tool_calls` 增量 ≤1、目标行 `note` 只可能
"基线 → 目标值"变一次、`task_events.seq` 连续（`list(range(n))`）、**无 `TOOL_CALL_RETRIED`**、
`step_observed` ≤1 行。

| # | 注入方式 | 实际结果 | 断言证据 | 可自动恢复 vs 人工 |
|---|---|---|---|---|
| 1 | 直接写一条 `step_prepared`（`prepared_at = now-300s`）、`step_observed` 缺失，任务停在 `EXECUTING`（等价"执行前崩溃"） | 核对收敛 `NEEDS_RECONCILIATION`，`tool_calls` 增量 **0**，`note` 不变 | 任务状态 `NEEDS_RECONCILIATION`；`_tool_calls_total` 前后相等；`report.reconciled ≥ 1` | **可自动恢复**（标记待核对，人工确认是否真的执行） |
| 2 | 先用 rw 账号 `UPDATE dg_lab.seed_orders`（模拟已提交），再写 `step_prepared`，无 `observed`（等价"响应丢失"） | 同上收敛 `NEEDS_RECONCILIATION`；**不重放**，`note` 保留目标值（只变 1 次） | `tool_calls` 增量 0；`note == target`；无 `TOOL_CALL_RETRIED` | **可自动恢复**（结论 `NEEDS_RECONCILIATION`，不自动判定成功/失败） |
| 3 | `monkeypatch` 把 `stack.reconciler.sweep` 换成抛 `DatabaseQueryError`（等价"核对期连接中断"） | `run_once()` **不抛错**；本轮候选照常执行（`executed==1`）；`clean is False`、`failed==1`；`RECONCILIATION_FAILED` 落库 | `task_events(task_id='system:worker_scheduler')` 有该事件，payload 含 `RECONCILIATION_SWEEP_FAILED` 与 `message_digest`；`note == target` | **可自动恢复**（下一轮 sweep 成功即恢复；连续失败靠 `consecutive_failures` 告警） |
| 4 | 旧 Worker 取租约（token1）→ 强制 `expires_at` 过期 → 新 Worker 取到 token2 并释放 → 把旧租约记录"复活"为 `ACTIVE`（模拟崩溃进程未清理），旧 Worker 持 token1 提交 | 旧 Worker 被 **`FENCING_TOKEN_STALE`** 拒绝；租约在旧进程视角也已过期时本地即拒 `WORKER_LEASE_INVALID`；零副作用 | `pytest.raises(FencingTokenStaleError)` 且 `code == 'FENCING_TOKEN_STALE'`；`code == 'WORKER_LEASE_INVALID'`；`tool_calls` 0；`note` 不变 | **可自动恢复**（新 Worker 接管；旧 Worker 结构性写不进去） |
| 5 | 两个 Worker 同时 `acquire_lease` 同一任务 | 第二个 `WORKER_CONFLICT`；第一个执行成功一次 | `code == 'WORKER_CONFLICT'`；`tool_calls == 1`；`note == target`；`seq` 连续 | **可自动恢复**（冲突方跳过，不延时重试） |
| 6 | 同一候选消息连投两轮 `run_once()` | 第一轮 `executed==1`；第二轮 `executed==0`，`tool_calls` 仍 1，`step_observed` 仍 1 | `run_once()` 两次的 `executed` 计数；`tool_calls`/`step_observed` 计数；`note == target` | **可自动恢复**（审批一次性消费 + 任务状态迁移共同挡住重复） |
| 7 | (a) `EXECUTING`（在途）+ 注入 Prepared，再 `request_pause()`；(b) `WAITING_APPROVAL` 上 `cancel()` | (a) 状态**仍是 `EXECUTING`**（不伪造"已停止"），随后核对收敛 `NEEDS_RECONCILIATION`，`TASK_PAUSE_REQUESTED` 在链上；(b) 落 `TASK_CANCEL_REQUESTED` → `CANCELLED`，零执行 | `after_pause is EXECUTING`；`TASK_PAUSE_REQUESTED in types`；`TASK_CANCEL_REQUESTED in types`；`final ∈ {CANCELLED, NEEDS_RECONCILIATION}`；`tool_calls` 0；`note` 不变 | **可自动恢复**（PAUSE 只阻后续；CANCEL 由核对决定终态） |

### 2.1 边界口径（演练要把"停在哪"说清楚）

- **响应丢失 → `NEEDS_RECONCILIATION`**：核对器只做"存在未观测写"的判定与状态收敛，
  **不猜测**结果是成功还是失败（不重放、不写 `step_observed`）。
- **证据不足 → `MANUAL_INTERVENTION`**：任务不存在 / 无合法迁移边时转人工（本卡 7 例未直接命中，
  该分支由 T05 `core/recovery/controller.py` 的 `reconcile_unknown(evidence_sufficient=False)`
  与 W06 `Reconciler._target_status()` 覆盖）。
- **PAUSE 不等于 SQL 已停止**：状态机显式如此设计，本卡用"状态不变 + 需核对"把它钉住。
- **`TOOL_CALL_RETRIED` 必须为 0**：任何注入路径都不允许出现；实现层没有任何重试分支。

---

## 3. 验证确定性（AC-21）

文件：`tests/unit/test_verification_determinism.py`（6 例，离线）。

- 同一 `tool_call_id` 连跑 10 轮：`{verdict}` 大小 ==1 且为 `{"PASS"}`；
- `{plan_digest}` 大小 ==1；逐条断言 `(assertion_id, verdict, observed)` 三元组集合大小 ==1；
- `{(disposition, approval_required)}` 集合大小 ==1；
- 断言来源必须同时覆盖 `step_observed` 与 `target_readback`（否则"确定性"只是单来源重复）；
- 每轮都真的读计划 + 读回读（`plan_source.calls == readback.calls == 10`），
  证明确定性来自"同输入"，不是"缓存第一次结果"；
- `collected_at` 允许不同（采集时间是事实，不是判定输入）。

---

## 3.1 恢复接管事件（设计 §5 第 10 行）

`core/recovery/controller.py` 新增 `_begin_recovery()`：**同一次接管只发一条**
`AGENT_RECOVERY_STARTED`，payload = `trigger` / `call_key` / `plan_version` /
`evidence_count`（+ `tool_calls_delta=0`）。

幂等闸门 `_takeover_open`：首次判停/升级置位；`observe_call` / `observe_evidence`
再次观测到进展（`CONTINUE`）时清零，于是**下一次接管**会重新发一条。
`trigger` 取值封闭在 `LOOP_DETECTED` / `NO_PROGRESS` / `UNKNOWN_OUTCOME`——
`MODEL_OUTPUT_INVALID`（AC-14）不是"接管"触发条件，只发 `AGENT_RECOVERY_STOPPED`。

`tests/unit/test_recovery_controller.py` 新增 5 例：三类触发各一条、同一次接管不重复、
"进展后再次判停"会发第二条、非法输出路径不发 STARTED、STARTED 在 STOPPED 之前。
T05 已验收的判定语义 / 阈值 / `explanation` 非空 / `tool_calls_delta=0` 均未改。

---

## 4. 装配（`workers/stack.py`）

`build_worker_stack()` 追加注入三个端口，均为**可选字段（默认 `None`）**，不改变既有测试的可构造性：

| 端口 | 类型 | 关键约束 |
|---|---|---|
| `readback` | `MysqlReadbackAdapter(ro_client)` | 独立**只读** client，与执行侧 `rw_client` 不同实例（AC-05） |
| `verifier` | `Verifier(plan_source=ApprovalPlanSource(approvals, tenant_id), ...)` | `plan_source` 的租户由组合根固定，调用方无法跨租户读披露快照 |
| `reconciler` | `Reconciler(step_records, repository, audit)` | 只被 `run_once()` 调用；内部实现未改 |

`WorkerStack.recovery_for(task_id, run_id)` 按任务派生并 `bind()` 恢复控制器，
避免共享未绑定实例导致 `skipped_events` 静默丢事件。

---

## 5. 未执行项（如实列出，不冒充通过）

| 项 | 状态 | 原因 |
|---|---|---|
| `--once` 进程的 `outstanding_candidates` 退出码 | **未断言固定值** | 库内存在其它历史未收敛候选时退出码为 1；用例只断言 `returncode in (0, 1)` 且报告 `reconciled >= 1`，不把环境状态当断言 |
| PostgreSQL 相关用例 | **未执行** | 本机 PG 5432 拒绝连接（环境问题，按约定如实归因、不修） |
| 控制库残留清理（`task_events` 等只追加表） | **部分未执行** | `DG_ADMIN_DSN`（root）认证失败 1045，无法 DELETE；用例改做两件可控清理：撤销未消费审批（防残留候选被其它用例执行）+ 恢复 LAB 目标行，并在测试输出 warning 列出未清理事实 |
| 7 类注入之外的网络分区/磁盘满 | **未执行** | 不在任务书 D5 的 7 类注入清单内 |

---

## 6. 复现命令

```bash
# 单元回归（基线 857 + T06 新增 34）
timeout 180 .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q

# 本卡验收：单元（18 + 6）+ 崩溃矩阵 7 + 子进程 2 + 恢复回归 2
DG_RECOVER_ON_STARTUP=false timeout 400 .venv/Scripts/python.exe -m pytest \
  tests/unit/test_worker_scheduler.py \
  tests/unit/test_verification_determinism.py \
  tests/integration/test_w08_crash_matrix.py \
  tests/integration/test_w08_worker_process.py \
  tests/integration/test_w08_recovery_e2e.py -o addopts="" -q
```

实跑结果：单元 **891 passed / 0 failed**；集成四个文件全绿（崩溃矩阵 7、
`--once` 子进程 2、恢复回归 2），逐例结果见随卡交付消息。

---

## 7. 事件 payload 实证（取自控制库 `dg_system.task_events` 的真实落库行）

`RECONCILIATION_SWEPT`（`task_id='system:worker_scheduler'`；本机当时残留 72 条待核对，
`conclusions` 被审计上限截断为 50 条 + `<truncated 22 items>`）：

```json
{
  "scanned": 72,
  "converged": 1,
  "conclusions": [
    {"task_id": "task_1b73...", "tool_call_id": "call_e4cd...", "conclusion": "MANUAL_INTERVENTION"},
    {"task_id": "task_b573...", "tool_call_id": "call_9c79...", "conclusion": "NEEDS_RECONCILIATION"},
    "...（截断至 50 条）"
  ],
  "duration_ms": 6944.958,
  "tenant_id": "tenant_local",
  "worker_id": "worker_local_01",
  "tool_calls_delta": 0
}
```

`AGENT_RECOVERY_STARTED`（真实任务链，`trigger=LOOP_DETECTED`）：

```json
{
  "trigger": "LOOP_DETECTED",
  "call_key": "k1",
  "plan_version": 0,
  "evidence_count": 2,
  "tool_calls_delta": 0
}
```
