# W09 收口复验报告（M08 + M09）

- 执行人：严过关（Yan）· QA 工程师｜任务：M08 + M09 / 任务 #7｜语言：简体中文
- 现场：`D:/DBGuaredian`，`git HEAD = 2850eca`（全部改动未提交），`.venv/Scripts/python.exe` = Python 3.14.7
- 依据：`docs/w09/10-lead-rulings.md`、`docs/w09/08-worker-design-and-tasks.md`、`docs/w09/03-task-cards.md`（AC 总表 `:690-714`）、`docs/w09/02-design.md`（§8.3/§8.4 冻结 DDL `:554-614`）、`docs/w09/11-worker-ac-baseline.md`（M03 基线锚）
- 方法：**不采信工程师回传数字，全部自己串行实跑**；`-o addopts="" -q -p no:cacheprovider`；需 DB 加外层 `timeout 180`；不写 `--timeout=`；无路径全量 pytest 未跑（`tests/unit` 为定向目录，按 §5-4 允许）
- 唯一写入文件：**本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs；未手工 INSERT/UPDATE/DELETE/TRUNCATE/ALTER；未执行 rollback；未改账号 / `.env`；未 git commit / add / checkout / reset

---

## ① 执行摘要（verdict + 智能路由）

**Verdict：T03–T06 已实现并通过本轮验收；W09 门禁未评。**
依据：T03/T04/T05/T06 四张卡的定向测试文件全部实跑通过（A–D 组均绿），`tests/unit` 全量独立复跑 **1294 passed**（与工程师报数一致），四条 W07 Worker 载体 AC 的收口对照与 M03 基线逐条一致（10 / 2 / 38 / 1），迁移 010/011 结构与冻结 DDL 逐字一致，M01 满足「一个断言都不许动」硬保护。

**智能路由判定：`NoOne`（环境项 E-2/E-3）。**
- 唯一的红项是 C6（`test_step_prepared.py -k "sweep or reconcile"`：`2 failed, 18 deselected, 2 errors`），经三重证据判定为 **E-2（root 1045）+ E-3（残留）的必然结果，不是 T06 回归**（详见 ④）。
- 未发现源码 Bug（不需要回派 Engineer）；M01 未发现测试代码 Bug（不需要 QA 自修）。
- 2 个 error 是 teardown 的 `DatabaseAuthError`（1045），与用例失败同源，不是新的实现缺陷。

**一句话**：新增的四张卡全绿；唯一的红是「历史残留 + 管理员凭据从未可用」这一环境项在既有 W03 用例上的既有表现，换旧实现同样红。

---

## ② 逐命令原文汇总行（A–F 分组）

### A. T03（结构化报告 + KD-1 补偿拒绝事件）

```
##### tests/unit/test_w09_report_schema.py
..................................................                       [100%]
50 passed in 0.20s

##### tests/unit/test_w09_compensation_denied.py
.....                                                                    [100%]
5 passed in 0.12s

##### tests/unit/test_w09_report_export.py
.....                                                                    [100%]
5 passed in 0.17s

##### tests/integration/test_w09_report_evidence_ref.py
...                                                                      [100%]
3 passed in 0.94s

##### tests/integration/test_w08_compensation.py
..                                                                       [100%]
2 passed in 8.38s
```

### B. T04（五类记忆 + `KnowledgeEntry` + 冲突解析）

```
##### tests/unit/test_w09_memory_models.py
...................................                                      [100%]
35 passed in 0.14s

##### tests/integration/test_w09_conflict_resolver.py
...                                                                      [100%]
3 passed in 0.40s
```

### C. T05（模型降级）

```
##### tests/unit/test_w09_model_degradation.py
.................                                                        [100%]
17 passed in 0.30s

##### tests/unit/test_model_gateway.py
..................                                                       [100%]
18 passed in 0.23s

##### tests/unit/test_model_isolation.py
..                                                                       [100%]
2 passed in 0.05s

##### tests/integration/test_w09_degrade_audit.py
..                                                                       [100%]
2 passed in 1.35s
```

### D. T06（样本集 + `sweep()` 计数 + KD-3/4/5）

```
##### tests/unit/test_w09_redaction_samples.py
......................                                                   [100%]
22 passed in 0.23s

##### tests/unit/test_w09_sweep_scanned.py
..........                                                               [100%]
10 passed in 0.06s

##### tests/unit/test_worker_scheduler.py
...................................                                      [100%]
35 passed in 0.18s

##### tests/unit/test_write_tool_gate.py
......                                                                   [100%]
6 passed in 0.12s

##### tests/unit/test_tools_contracts.py
.....                                                                    [100%]
5 passed in 0.07s
```

### E. 汇总回归（`tests/unit`）

```
##### tests/unit
........................................................................ [ 94%]
......................................................................   [100%]
1294 passed in 8.77s
```

### F. 四条 AC 收口复验（对照 M03 基线）

```
##### tests/unit/test_worker_layering.py
..........                                                               [100%]
10 passed in 0.08s
##### tests/unit/test_worker_isolation.py
..                                                                       [100%]
2 passed in 0.07s
##### tests/unit/test_worker_runner.py
......................................                                   [100%]
38 passed in 0.58s
##### tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once
.
============================== warnings summary ===============================
  tests/integration/test_worker_process.py:237: UserWarning: 控制库清理不完整（需可用的 DG_ADMIN_DSN）：
  task_id=task_7f45644c0a3744439d7013363d31c2ab 未清理表=approval_consumptions,step_observed,
  step_prepared,tool_calls,execution_envelopes,approvals,task_events,task_steps,worker_leases,
  checkpoints,evidence,tasks
1 passed, 1 warning in 25.66s
```

### 补充定向（为 AC-01…AC-08 取证；不在 A–F 必跑清单内，QA 自行追加）

```
##### tests/unit/test_w09_redaction_engine.py
173 passed in 0.45s
##### tests/unit/test_w09_redaction_gate.py
44 passed in 0.26s
##### tests/unit/test_w09_redaction_wiring.py
30 passed in 0.55s
##### tests/integration/test_w09_redaction_pipeline.py  (-rs)
4 passed in 2.88s          # -rs 无 skip 行 → 真实连库
```

> 说明：`tests/unit`（E）是 unit 层全量，已包含 A–D 的 unit 文件与 `test_w09_redaction_engine/gate/wiring`；上表分组为**逐文件独立实跑**，不重复计入总计。F 组命令 4 的 1 条 `UserWarning` 为 `DG_ADMIN_DSN` root 1045 引起的清理降级（`test_worker_process.py:228-241`，`except DBGuardianError` → `warnings.warn`），**非 failed/error**，残留未 DELETE。

---

## ③ AC-01…AC-21 逐条验收表

| AC | 判据要点 | 卡 | 测试文件（`03-task-cards.md:690-714`） | 本轮实跑 | 判定 |
|---|---|---|---|---|---|
| AC-01 | 12 类逐类命中 | T01 | `tests/unit/test_w09_redaction_engine.py` | 173 passed | **PASS**（unit） |
| AC-02 | 5 轮确定性（count/version/hash） | T01 | 同上 | 173 passed | **PASS**（unit） |
| AC-03 | 未覆盖样本 → 非 `REDACTED` | T01 | 同上 | 173 passed | **PASS**（unit） |
| AC-04 | 哈希口径 + `extra="forbid"` | T01 | 同上 | 173 passed | **PASS**（unit） |
| AC-05 | 持久化前闸门（0 新增行） | T02 | `tests/integration/test_w09_redaction_pipeline.py`（+ `test_w09_redaction_gate.py`） | 4 passed（真实库）+ 44 passed | **PASS** |
| AC-06 | 模型输入前闸门 + `REDACTION_BLOCKED` | T02 | 同上 + `test_w09_redaction_wiring.py` | 4 passed + 30 passed | **PASS**（闸门③④ unit 级）；**业务侧端到端未验证**（见 ⑧-9） |
| AC-07 | 导出/训练集再扫描 | T02/T03/T06 | 同上 + `test_w09_report_export.py` + `test_w09_redaction_samples.py` | 4 + 5 + 22 passed | **PASS** |
| AC-08 | 证据行与事件 `redaction_version` 一致 | T02 | `test_w09_redaction_pipeline.py` | 4 passed（真实库） | **PASS** |
| AC-09 | 引用归属 + 六态两两不等价 | T03 | `tests/unit/test_w09_report_schema.py` | 50 passed | **PASS** |
| AC-10 | 弱证据不升格 | T03 | 同上 | 50 passed | **PASS** |
| AC-11 | 措辞禁令 | T03 | 同上 | 50 passed | **PASS**（另由 `test_w09_compensation_denied.py` 5 passed 佐证 KD-1） |
| AC-12 | 安全渲染 + `STALE`/`missing_evidence` | T03 | `tests/integration/test_w09_report_evidence_ref.py` | 3 passed（真实库） | **PASS（组件级）**；**生产装配链路未验证**（见 ⑧-8） |
| AC-13 | 五类枚举 + `is_indexable` | T04 | `tests/unit/test_w09_memory_models.py` | 35 passed | **PASS** |
| AC-14 | 硬过滤先行 + 冲突组 + 撤销可查 | T04 | 同上 + `test_w09_conflict_resolver.py` | 35 + 3 passed | **PASS** |
| AC-15 | 记忆不授权执行（签名/AST） | T04 | `tests/unit/test_w09_memory_models.py` | 35 passed | **PASS** |
| AC-16 | `disabled` → 暂停规划 + 降级简报 | T05 | `tests/unit/test_w09_model_degradation.py` | 17 passed | **PASS** |
| AC-17 | 非法输出只重试 1 次 | T05 | 同上 | 17 passed | **PASS** |
| AC-18 | 降级不触凭据 + `MODEL_DEGRADED` + 0 工具调用 | T05 | `tests/integration/test_w09_degrade_audit.py` | 2 passed（真实库） | **PASS** |
| AC-19 | 五类样本三断言 + 版本非空 | T06 | `tests/unit/test_w09_redaction_samples.py` | 22 passed | **PASS**（`usage`→R12→`REDACTED` 口径见 ⑧-7） |
| AC-20 | `scanned_total` / `conclusions` 分离 | T06 | `tests/unit/test_w09_sweep_scanned.py` + `test_worker_scheduler.py` | 10 + 35 passed | **PASS**（与 C6 红无关，见 ④） |
| AC-21 | 写面与引擎不回退 | T06 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py` | 6 + 5 passed | **PASS** |

**汇总**：AC-01…AC-21 共 21 条，**19 条 PASS、2 条 PASS（组件级）但生产装配/业务端到端未验证（AC-06、AC-12）**；无 FAIL。生产装配/业务端到端未验证项单列 ⑧，不作为通过依据。

---

## ④ C6 红独立判定与证据

### 4.1 实跑原文（工程师引用命令，QA 独立复跑）

```
$ timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_step_prepared.py \
    -k "sweep or reconcile" -o addopts="" -q -p no:cacheprovider
...
E       AssertionError: assert [ReconcileRes...b290b7'), ...] == []
E         Left contains 101 more items, first extra item: ReconcileResult(prepared_id='spr_82853a...'
tests\integration\test_step_prepared.py:211: AssertionError

E       AssertionError: assert 102 == 1
E        +  where 102 = len([ReconcileResult(...), ...])
tests\integration\test_step_prepared.py:385: AssertionError
=========================== short test summary info ============================
FAILED ...::test_reconciler_skips_calls_that_gained_observation
FAILED ...::test_sweep_converges_terminal_task_to_manual_intervention
ERROR  ...::test_reconciler_skips_calls_that_gained_observation
ERROR  ...::test_sweep_converges_terminal_task_to_manual_intervention
2 failed, 18 deselected, 2 errors in 46.20s
```

与工程师报数逐字一致（`2 failed, 18 deselected, 2 errors`）。

### 4.2 证据一：控制库残留计数（只读 SELECT，实测）

```
step_prepared total = 223 ;  step_observed total = 122  → 未观测 = 101
unobserved by tenant = [{'tenant_id': 'tenant_local', 'n': 101}]      # 其他租户 0
unobserved distinct tasks = 101（一 task 一条）
unobserved task status mix = NEEDS_RECONCILIATION 89 / PENDING 7 / COMPLETED 5
prepared_at 分布：2026-11-10=55, 11-11=3, 11-12=35, 11-13=6, 11-14=2
```

→ **101 这个数真实存在**（`223-122=101`），且**随时间增长**：跨 2026-11-10…11-14 累积，每天新增若干，来源是历次集成测试只 INSERT、`admin_client` 清理从未成功（root 1045）。本轮复跑后计数 **101→102**（`test_sweep_converges_terminal_task_to_manual_intervention` 自身新增 1 条未观测，sweep 不写 `step_observed`，永久留存）。

### 4.3 证据二：新旧 `sweep()` 等价性（QA 自证，零写入）

- **结论逻辑未变**：`ast` 比对 `git show HEAD:core/execution/reconciler.py` 与工作区，`Reconciler._reconcile_one`（唯一决定结论的函数）**AST 完全相同**（`_reconcile_one AST identical: True`）。
- **扫描集合相同**：把新旧实现的 `_reconcile_one` 都替换为**无副作用哨兵**（返回 sentinel，**不写库**），在同一真实仓储上对跑：
  ```
  OLD sweep() conclusions: 102
  NEW sweep() conclusions: 102
  identical ordered prepared_id list: True
  NEW sweep_detailed(): scanned_total=102, conclusion_count=102
  ```
  → **旧实现也返回 102 条**（当前残留数），新旧输出逐条相同。因此 C6 的两条断言（`results == []`、`len(results) == 1`）在 HEAD 的旧实现下同样红。
- **差异仅是新能力**：`git diff` 显示新增 `sweep_detailed() -> SweepReport` 与 `scanned_total` 计数，`sweep()` 改为 `list(sweep_detailed().conclusions)`；**旧实现的分页循环、`_reconcile_one` 均未改**（diff 无该函数 hunk）。

### 4.4 证据三：2 个 error 的类型（逐条）

| # | 用例 | error 阶段 | 异常 | 触发点 |
|---|---|---|---|---|
| 1 | `test_reconciler_skips_calls_that_gained_observation` | **teardown** | `core.errors.DatabaseAuthError: 数据库认证失败，请检查账号配置` | `test_step_prepared.py:66-70` 的 `created_tasks` 夹具对 11 张表 `admin_client.execute(DELETE ...)`，建连即 1045 |
| 2 | `test_sweep_converges_terminal_task_to_manual_intervention` | **teardown** | 同上 | 同上 |

底层原文：`OperationalError(1045, "Access denied for user 'root'@'localhost' (using password: YES)")`，`database_id='dg_admin'`，经 `database/mysql/connection.py:59-60` 归一化为 `DatabaseAuthError`。该夹具**没有** `try/except`（不同于 `test_worker_process.py` 的降级写法），故 1045 直接升级为 teardown ERROR。缺省计数 18 为 `-k` 过滤掉的其余用例。

### 4.5 判定

- **性质**：C6 红 = **E-2（`DG_ADMIN_DSN` root 1045）+ E-3（`tenant_local` 101→102 条未观测 `step_prepared` 残留）的必然结果**，**不是 T06 回归**。
- **智能路由：`NoOne`（环境项）**。理由：① 两条断言假设「租户内只有本用例自己的行」，而 `sweep(tenant_id=tenant_local)` 按设计扫全租户；② 旧实现同样返回 102 条（4.3 实证）；③ `test_step_prepared.py` 本身既未被 T06 改动（`git diff` 未列该文件），也未被 Engineer 改断言；④ 清理需 DELETE，而 W06 权限收口禁止放宽 `dg_ctl` 且全局纪律禁止 DELETE 残留。
- **不回派 Engineer**：无源码缺陷证据。**不判 QA 自修**：该文件是 W03 既有用例，本轮不改历史用例、不放宽断言（`03-task-cards.md:7`）。
- **改进建议（只写不做，留 W10/另立卡）**：若要在未来让 C6 变绿，应让两条用例只断言「本用例 `task_id` 的结论」而非整租户结果集，或让 `sweep` 支持按 `task_id` 过滤；但这会改既有用例的断言范围，须单独立卡评审，本轮不执行。
- **证据强度：高**。三重独立证据（DB 计数 101/102 + 新旧实现逐字等价 `102==102`/`_reconcile_one` AST 相同 + pytest 原文 `101 more items`/`102==1`），且 2 个 error 的异常类型与触发点已逐条定位到 `DatabaseAuthError(1045)`。

---

## ⑤ M01 独立复核结论

**结论：满足「一个断言都不许动」硬保护（`10-lead-rulings.md` §4）。**

| 检查项 | 结果 |
|---|---|
| `--collect-only -q`（改后，真实 pytest） | 6 条 node id，全部 `tests/integration/test_evidence_service.py::test_*` |
| 改前用例清单（`git show HEAD:...` AST 提取） | 6 个 `test_*` 函数名与改后**逐行一致**；无 `@pytest.mark.parametrize`、无测试类 → 收集数必为 6 |
| `TEST_NAMES_IDENTICAL` | **True**（6/6，顺序一致） |
| `TEST_FUNC_BODIES_IDENTICAL` | **True**（6 个测试函数体 AST 完全相同） |
| `ASSERT_PER_TEST_IDENTICAL` | **True** |
| 测试函数内断言总数 | **HEAD 23 / CUR 23（23/23 一致，与工程师报数一致）** |
| `pytest.raises` 集合 | **一致**（1+3+0+0+1+0 = 5 处） |
| 整个模块断言总数 | HEAD 23 / CUR 24（**+1 在清理夹具 `_cleanup_created_tasks` 内**） |
| 实跑 | **`6 passed in 3.85s`（0 errors）** |

说明与边界：
- 硬保护要求的「测试函数体 / 断言 / 参数化 / 用例数」**全部未变**；新增的 1 条 `assert counted == written[...]` 位于 autouse 清理夹具（`count_for_task` 只读核对），正是 `10-lead-rulings.md` §4 指定的改法（「只断言本用例范围内事实」），**不是**对测试断言的放宽或删减。
- 因本任务禁止写临时文件，改前的「用例清单」以 `git show HEAD` 的 AST 等价提取代替在 HEAD 文件上真跑 `--collect-only`；结合「无参数化/无测试类」可严格推出收集数不变。此为方法说明，不降低结论强度。
- 该文件改后 `6 passed / 0 errors` **仅表示 teardown 环境报错不再出现**，**不代表** `DG_ADMIN_DSN` 已修、**不代表**历史残留已清、**不代表**整体回归通过。历史基线仍按固定表述登记（见 ⑨-3）。

---

## ⑥ 迁移结构只读复核（information_schema）

### 6.1 `report_snapshots`（010_w09_report）— 14 列，与 `02-design.md:558-576` 冻结 DDL 逐字一致

- 列序/类型/可空性：`report_id VARCHAR(64) NO / tenant_id VARCHAR(64) NO / task_id VARCHAR(64) NO / report_version VARCHAR(32) NO / fact_status VARCHAR(16) NO / action_state VARCHAR(24) NO / verification_state VARCHAR(16) NO / verification_result_id VARCHAR(64) YES / evidence_ids_json JSON NO / missing_evidence_json JSON NO / body_json JSON NO / redaction_version VARCHAR(32) NO / degraded TINYINT(1) NO / created_at DATETIME(6) NO` —— **14/14 一致**
- PK：`report_id`；索引：`idx_report_task (tenant_id, task_id, created_at)`；FK：`fk_report_tenant_task (tenant_id, task_id) → dg_system.tasks(tenant_id, task_id)`；`ENGINE=InnoDB / utf8mb4_unicode_ci`
- 只追加语义：迁移文件 `010_w09_report.sql` 仅 `CREATE TABLE IF NOT EXISTS`，无 DELETE/DROP；`dg_ctl` 对本表 `table_privileges` 为空（仅继承 schema 级 SELECT/INSERT）

### 6.2 `knowledge_entries`（011_w09_memory）— 26 列，与 `02-design.md:583-613` 冻结 DDL 逐字一致

- 列序/类型/可空性：26 列全部一致（含 `environment_fingerprint_digest CHAR(64) NULL`、`revision INT`、4 个 JSON 列、`reviewed_by/reviewed_at`、`effective_from/until`、`revoked_at/reason`、`supersedes`）
- PK：`(knowledge_id, revision)`；索引：`idx_knowledge_lookup (tenant_id, kind, status, db_engine, effective_until)`、`idx_knowledge_reviewer (tenant_id, reviewed_by)`；`ENGINE=InnoDB / utf8mb4_unicode_ci`；无 FK（与设计一致）
- 只追加语义：迁移文件仅 `CREATE TABLE IF NOT EXISTS`；`dg_ctl` 对本表 `table_privileges` 为空

### 6.3 `schema_migrations` 与授权

- `schema_migrations` 含 **`010_w09_report`、`011_w09_memory`**（applied_at 2026-11-14 15:50:47），且 001–009 均在
- `dg_ctl` schema 级权限：`ALTER, CREATE, INDEX, INSERT, REFERENCES, SELECT` —— **无 DELETE、未放宽**
- 两张新表的 `table_privileges`（UPDATE/DELETE）：**空**（未被加入可变表名单）
- 回滚脚本 `010_rollback.sql` / `011_rollback.sql` 存在，但**本轮未执行**（含 DROP，必须 `DG_ADMIN_DSN` 人工执行；见 ⑧-4）

---

## ⑦ 四条 AC 与 M03 基线对照（含 sha256 差异清单）

### 7.1 汇总行对照

| 命令 | M03 基线 | M08 收口 | 结论 |
|---|---|---|---|
| `test_worker_layering.py` | 10 passed | **10 passed** | 一致 |
| `test_worker_isolation.py` | 2 passed | **2 passed** | 一致 |
| `test_worker_runner.py` | 38 passed | **38 passed** | 一致 |
| `test_worker_process.py::test_worker_process_executes_approved_repair_once` | 1 passed, 1 warning | **1 passed, 1 warning** | 一致（warning 仍为 12 表清理降级环境项） |

→ 四条 AC（AC①入口/生命周期、AC② Gateway+一次性 grant、AC③ 三表各 1 + tool_calls=1、AC④ 装配点真实引用）**收口对照全部通过**，无回归。

### 7.2 sha256 差异清单（对照 `11-worker-ac-baseline.md` §6.3）

| 文件 | 结果 | 说明 |
|---|---|---|
| `tests/unit/test_worker_layering.py` | **SAME** | 基线测试未被动 |
| `tests/unit/test_worker_isolation.py` | **SAME** | — |
| `tests/unit/test_worker_runner.py` | **SAME** | — |
| `tests/integration/test_worker_process.py` | **SAME** | — |
| `workers/stack.py` | **SAME** ✅ | **按裁定零写入，符合预期** |
| `workers/runner.py` | **DIFF** | 预期（T06 改动面） |
| `workers/__main__.py` | **SAME** | — |
| `workers/cli.py` | **SAME** | — |
| `core/approval/service.py` | **DIFF** | 预期（T03 KD-1 改动面） |
| `core/gateway/tool_gateway.py` | **SAME** | — |
| `docker-compose.yml` | **SAME** | **compose 未动，符合预期** |
| `deploy/Dockerfile.worker` | **SAME** | — |
| `tests/conftest.py`（M03 复核时记录的锚） | **SAME** | — |

→ **差异恰好只有 `workers/runner.py`（T06）与 `core/approval/service.py`（T03）两个预期文件；`workers/stack.py` 未变（零写入裁定满足）**；其余 10 个文件全部未变。无「测试被改 / 断言被放宽」的痕迹。

---

## ⑧ 遗留项与未验证项（单列，不作为通过依据）

1. **C6 红（E-2 + E-3）**：`test_step_prepared.py -k "sweep or reconcile"` = `2 failed, 2 errors`，属环境项，非 T06 回归（见 ④）。该文件是 W03 既有用例，T06 未改；修复需改历史用例断言范围，留 W10/另立卡。
2. **E-2（`DG_ADMIN_DSN` root 1045）**：管理员凭据在本机始终不可用，清理一律降级为 warning；`test_worker_process.py` 命令 4 的 1 条 `UserWarning` 同源。
3. **E-3（控制库残留）**：`tenant_local` 未观测 `step_prepared` 由 101 累积到 102（本轮 C6 复跑 +1），历史残留跨 11-10…11-14 累积；按全局纪律**未 DELETE**，如实登记。
4. **迁移回滚未执行**：`rollback/010_rollback.sql`、`011_rollback.sql` 含 DROP，必须 `DG_ADMIN_DSN` 人工执行；本轮**未执行、也不得执行**。回滚会使已交付 `report_id` 失效（设计 §8.5），需发布说明登记。
5. **compose 未动 / NV-1 容器运行态未验证**：`docker-compose.yml` hash 未变；本机 Docker daemon 不可用，Worker 容器运行态 `NOT_VERIFIED`，按 `10-lead-rulings.md` §2-5 挂 W11。
6. **`KD-W09-03`（内部通道 PII）**：`StepPrepared.request` 与 `TrajectoryRecorder`（含 `usage`）不接管 `REDACTED`/`REVIEW_REQUIRED` 改写，非敏感键名下的 PII 可能原样落盘；W09 不替换审计链路，`02-design.md:67` 已登记为已知限制，W10 再议。
7. **`usage` 键名命中 R12 → 训练样本恒 `REDACTED`**：精确口径为 `usage.prompt_tokens` / `usage.completion_tokens` 的**键名含 "token"**，命中 R12 敏感键词表（`SENSITIVE_KEY_MARKERS` 含 `token`），故含计数的训练样本恒为 `REDACTED`（`core/report/samples.py:216,235`）。**`REDACTED` 是「可放行」状态**，样本仍可交付；该口径由 `test_w09_redaction_samples.py:411-434` 专门锁定，不是缺陷、不得为让样本 CLEAN 而改闸门。
8. **报告组件未接生产装配**：**原登记**：`ReportBuilder` / `build_report` 仅出现在 `core/report/{builder,validate}.py` 与文档中，**`apps/`、`workers/` 无任何引用** → T03 报告组件已在组件级/真实库验证，但**未接入 API/Worker 生产装配链路**；AC-12 的「生产装配链路」未验证。**2026-11-18 复核：已关闭**，证据：`workers/report_assembly.py:35` 已 import `ReportBuilder`、`:193` 实际装配，`workers/stack.py:54` import `TaskReportPublisher` / `build_report_publisher`、`:307` 构造、`:339` 挂入 `WorkerStack`；真实库链路用例 `tests/integration/test_w09_report_publish_chain.py`（`test_publish_chain_persists_one_snapshot_and_one_event`、`test_publish_without_evidence_still_writes_one_unknown_snapshot`）2 passed。原登记保留作审计链，本条不作为通过依据。
9. **AC-06 业务侧端到端未验证**：仅验证到闸门③④的单元级调用计数；真实 Agent/导出链路的业务编排未验证（承接 `06-qa-report.md` §11.6 口径）。
10. **KD-5 两份 W07 冻结件仍缺**（`05-freeze.md`、`06-conformance-audit.md`）：**原登记**：本轮不补，按已知偏离登记，关闭条件挂 W11（`10-lead-rulings.md` §2-6/§3-Q3）。**2026-11-18 复核：已关闭**，证据：`docs/w07/05-freeze.md`（16556 字节）与 `docs/w07/06-conformance-audit.md`（18824 字节）已于 2026-11-17 23:13 落盘、文件存在且内容可读；当前 `git status` 为 `??`（未跟踪、未提交），属版本控制状态而非缺件。原登记保留作审计链，本条不作为通过依据。
11. **PG 5432**：未涉及、未验证；本轮所有定向用例均不依赖 PG。
12. **M01 改后行为**：`test_evidence_service.py` 现 `6 passed / 0 errors`；但环境凭据未修、历史残留未清，该结果**不等于**环境已恢复（见 ⑨-3）。

---

## ⑨ 结论口径与表述禁令自查

1. **W08**：写「**有条件放行**」（`docs/w08/07-gate-review.md:10`）。KD-1…KD-5 的修复完成**不等于** W08 条件 C-1…C-4 全部关闭。
2. **W07**：写「**有条件放行；门禁通过：否**」（`docs/w07/07-gate-review.md:11,20`）；两份历史冻结件仍缺。
3. **环境项**：6 个 teardown errors 与 101 条残留按环境项登记，保留固定表述「**用例本体 6 passed、6 个 teardown errors 为环境缺陷**」（历史基线）；M01 改后 `test_evidence_service.py` 为 `6 passed / 0 errors`，**仅表示 teardown 环境报错不再出现**，不代表凭据已修、残留已清、整体回归通过。
4. **W09**：只写「**T03–T06 已实现并通过本轮验收，W09 门禁未评**」，**不写** W09 整体通过/已实现/已收口。
5. 本文件未使用 `10-lead-rulings.md` §6 与 `03-task-cards.md:735` 列举的禁止措辞（写入后已 grep 自检）。

---

## ⑩ 读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅** `docs/w09/12-w09-qa-closeout.md` |
| **QA** 修改 `.py` / SQL / 配置 / 既有 docs | **0**（未触碰 `docs/w09/02-design.md`、`03-task-cards.md`、`06-qa-report.md` 三份既有 `M` 文档，也未触碰工程师的 12 份改动） |
| 手工 INSERT / UPDATE / DELETE / TRUNCATE / ALTER / rollback | **0**（未改账号 / `.env` / 服务） |
| 测试执行固有写库（透明披露） | 各集成用例按设计只 INSERT，`test_worker_process` 的用例 finally 恢复 LAB 目标行；C6 复跑按用例设计调用 `sweep()`，会对未观测行写 `task_events` 并推进 `tasks.status`（与工程师此前实跑同源），并使未观测残留 **101→102**。无任何清理型 DELETE。 |
| git commit / add / checkout / reset | **0** |
| 无路径全量 pytest / `--timeout=` | **0**（`tests/unit` 为定向目录；DB 定向用外层 `timeout 180`） |
| 放宽断言 / 删用例 / 加 skip / xfail | **0** |
| 与其他成员直连通信 | **0**（仅回传 team-lead） |
