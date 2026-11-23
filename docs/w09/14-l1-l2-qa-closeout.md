# W09 收口增量 L1 + L2 复验报告

- 执行人：严过关（Yan）· QA 工程师｜语言：简体中文｜本轮任务：L1 + L2 收口复验
- 现场：`D:/DBGuaredian`，`git HEAD = 2850eca`（全部改动**未提交**），`.venv/Scripts/python.exe` = Python 3.14.7
- 依据：`docs/w09/13-l1-l2-design.md`（§3 L1 / §4 L2 / §5 精确改动 / §6 验收 / §7 风险）、`docs/w09/10-lead-rulings.md`（裁定台账）、`docs/w09/11-worker-ac-baseline.md`（sha256 锚 §6.3）、`docs/w09/12-w09-qa-closeout.md`（上轮口径）
- 方法：**不采信工程师回传数字，A/B/C 三组全部自己串行实跑**；`-o addopts="" -q -p no:cacheprovider`；需 DB 加外层 `timeout 180`（整文件 `timeout 420`）；**不写 `--timeout=`**；未跑无路径全量 pytest
- 唯一写入文件：**本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs；未手工 INSERT/UPDATE/DELETE/TRUNCATE/ALTER；未执行 rollback；未改账号 / `.env`；未 `git commit/add/checkout/reset`

---

## ① 执行摘要（verdict + 智能路由）

**Verdict：L1 / L2 已实现并通过定向验收，W09 门禁未评。**

- L1：`test_step_prepared.py` C6 子集 `2 passed, 18 deselected`、整文件 `20 passed`（**0 errors**）；新增单测 16 条、新增真实库集成 2 条全过；冻结的三份 T06/W08 文件数字（10/35/38）**一条未变**。
- L2：`workers/report_assembly.py` 静态守护**零命中**；发布器单测 12 条、运行器接线 14 条、真实库发布链路 2 条全过；`RunReport.clean` 判据 AST **逐字相同**、`workers/cli.py` sha256 **SAME**（退出码 0/1/2 与判据逐字未变）；真实装配 `publisher.redaction is stack.redaction == True`。
- 汇总回归：`tests/unit` 独立复跑 **1336 passed**（与预期一致）。
- 四个 W08 基线测试文件 sha256 **全部 SAME**；`workers/stack.py` 变更为 L2 显式授权项（DIFF 已登记）。

**智能路由判定：`NoOne`（无源码 Bug、无测试代码 Bug 需自修）。**
- 未发现源码缺陷证据：不需要回派 Engineer。
- 未发现断言被**放宽 / 删除 / 用例被跳过**：不需要 QA 自修。
- **口径偏差 D-1：已裁定「接受偏差」，依据 `docs/w09/10-lead-rulings.md` §7 E-1**，定性「非断言放宽、非源码缺陷」 —— L1 的断言并非全部逐字不变：**42 条测试函数体断言中 40 条逐字不变、2 条**（`test_older_than_threshold_filters_recent_prepared` 的 `:267/:268`）左操作数增加 `task_id=` 实参（查询范围租户级→任务级），**比较算符与右操作数逐字未变、断言条数 2→2 未变**；模块级断言 43→44，唯一新增一条在 `created_tasks` 夹具内（只读 `get_task` 核对）。不路由 Engineer、QA 不改 `.py`、不拆分 assert 行、不回写设计档。详见 ③-4 / ③-5 / ③-6。
- **口径禁令**：本文件此后**不得再表述为「断言文本逐字不变」**，须用 ③-5 主理人统一口径原文。

**一句话**：L1/L2 的定向验收全部通过，两处工程师自报的 L2 偏离经独立取证均成立且可接受；L1 那 2 条内联在 assert 里的查询收窄已由主理人裁定接受（非放宽、非源码缺陷），按 ③-5 精确口径登记。

---

## ② 逐命令原文汇总行（A / B / C 分组）

> 全部命令在 `cd /d/DBGuaredian` 下**逐条串行**执行（Python 3.14.7 并发会 access violation，本轮零并发）。每条均带外层 `timeout`，未使用 `--timeout=`。

### A. L1（任务级核对能力 + 用例收窄 + 夹具去噪）

```
##### A1 tests/integration/test_step_prepared.py -k "sweep or reconcile"
$ timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_step_prepared.py -k "sweep or reconcile" -o addopts="" -q -p no:cacheprovider
..                                                                       [100%]
2 passed, 18 deselected in 5.18s
EXIT=0

##### A2 tests/integration/test_step_prepared.py（整文件）
$ timeout 420 .venv/Scripts/python.exe -m pytest tests/integration/test_step_prepared.py -o addopts="" -q -p no:cacheprovider
....................                                                     [100%]
20 passed in 50.70s
EXIT=0

##### A3 tests/unit/test_w09_sweep_task_scope.py（新增）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_sweep_task_scope.py -o addopts="" -q -p no:cacheprovider
................                                                         [100%]
16 passed in 0.20s
EXIT=0

##### A4 tests/integration/test_w09_sweep_task_scope.py（新增，真实库）
$ timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_w09_sweep_task_scope.py -o addopts="" -q -p no:cacheprovider -rs
..                                                                       [100%]
2 passed in 6.63s
EXIT=0

##### A5 tests/unit/test_w09_sweep_scanned.py（T06 冻结）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_sweep_scanned.py -o addopts="" -q -p no:cacheprovider
..........                                                               [100%]
10 passed in 0.14s
EXIT=0

##### A6 tests/unit/test_worker_scheduler.py（W08 冻结）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_worker_scheduler.py -o addopts="" -q -p no:cacheprovider
...................................                                      [100%]
35 passed in 0.47s
EXIT=0

##### A7 tests/unit/test_worker_runner.py（W08 冻结）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_worker_runner.py -o addopts="" -q -p no:cacheprovider
......................................                                   [100%]
38 passed in 0.70s
EXIT=0
```

### B. L2（报告接 Worker 生产装配）

```
##### B8 tests/unit/test_w09_report_assembly.py（新增）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_report_assembly.py -o addopts="" -q -p no:cacheprovider
............                                                             [100%]
12 passed in 0.33s
EXIT=0

##### B9 tests/unit/test_worker_report_hook.py（新增）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_worker_report_hook.py -o addopts="" -q -p no:cacheprovider
..............                                                           [100%]
14 passed in 0.31s
EXIT=0

##### B10 tests/unit/test_worker_layering.py（W08 冻结，builders == ["stack.py"]）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_worker_layering.py -o addopts="" -q -p no:cacheprovider
..........                                                               [100%]
10 passed in 0.12s
EXIT=0

##### B11 tests/unit/test_worker_isolation.py（W08 冻结）
$ .venv/Scripts/python.exe -m pytest tests/unit/test_worker_isolation.py -o addopts="" -q -p no:cacheprovider
..                                                                       [100%]
2 passed in 0.09s
EXIT=0

##### B12 tests/integration/test_w09_report_publish_chain.py（新增，真实库）
$ timeout 180 .venv/Scripts/python.exe -m pytest tests/integration/test_w09_report_publish_chain.py -o addopts="" -q -p no:cacheprovider -rs
..                                                                       [100%]
2 passed in 4.68s
EXIT=0
（-rs 无 skip 行 → 两条用例真实连库执行，未被 _require_lab 的 pytest.skip 兜底放过）

##### B13 tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once
$ timeout 180 .venv/Scripts/python.exe -m pytest "tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once" -o addopts="" -q -p no:cacheprovider
.                                                                        [100%]
============================== warnings summary ===============================
tests/integration/test_worker_process.py::test_worker_process_executes_approved_repair_once
  D:\DBGuaredian\tests\integration\test_worker_process.py:237: UserWarning: 控制库清理不完整（需可用的 DG_ADMIN_DSN）：task_id=task_0342d500d1ba4deab5425a79a947afd2 未清理表=approval_consumptions,step_observed,step_prepared,tool_calls,execution_envelopes,approvals,task_events,task_steps,worker_leases,checkpoints,evidence,tasks
    warnings.warn(
1 passed, 1 warning in 45.36s
EXIT=0
```

### C. 汇总回归

```
##### C tests/unit（定向目录，按 10-lead-rulings.md §2-4 允许）
$ .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider
........................................................................ [ 70%]
........................................................................ [ 75%]
........................................................................ [ 80%]
........................................................................ [ 86%]
........................................................................ [ 91%]
........................................................................ [ 97%]
........................................                                 [100%]
1336 passed in 13.97s
EXIT=0
```

### 汇总表

| 组 | 命令 | 原文汇总行 | 期望 | 一致 |
|---|---|---|---|---|
| A1 | `test_step_prepared.py -k "sweep or reconcile"` | `2 passed, 18 deselected in 5.18s` | 2 passed / 0 errors | ✅ |
| A2 | `test_step_prepared.py`（整文件） | `20 passed in 50.70s` | 20 passed / 0 errors | ✅ |
| A3 | `test_w09_sweep_task_scope.py`（unit） | `16 passed in 0.20s` | 16 | ✅ |
| A4 | `test_w09_sweep_task_scope.py`（integration） | `2 passed in 6.63s` | 2 | ✅ |
| A5 | `test_w09_sweep_scanned.py` | `10 passed in 0.14s` | 10（冻结） | ✅ |
| A6 | `test_worker_scheduler.py` | `35 passed in 0.47s` | 35（冻结） | ✅ |
| A7 | `test_worker_runner.py` | `38 passed in 0.70s` | 38（冻结） | ✅ |
| B8 | `test_w09_report_assembly.py` | `12 passed in 0.33s` | 12 | ✅ |
| B9 | `test_worker_report_hook.py` | `14 passed in 0.31s` | 14 | ✅ |
| B10 | `test_worker_layering.py` | `10 passed in 0.12s` | 10（冻结） | ✅ |
| B11 | `test_worker_isolation.py` | `2 passed in 0.09s` | 2（冻结） | ✅ |
| B12 | `test_w09_report_publish_chain.py` | `2 passed in 4.68s` | 2 | ✅ |
| B13 | `test_worker_process.py::..._repair_once` | `1 passed, 1 warning in 45.36s` | 1 passed / 1 warning（环境项） | ✅ |
| C | `tests/unit` | `1336 passed in 13.97s` | 1336 | ✅ |

**唯一非绿项**：B13 的 1 条 `UserWarning` —— `DG_ADMIN_DSN` root 1045 导致的 teardown 清理降级（`test_worker_process.py:228-241` 的 `except DBGuardianError → warnings.warn`），**不是 failed / 不是 error / 不是断言失败**，为环境项，残留未 DELETE。

---

## ③ L1 断言取证（40/42 逐字不变 + 2/42 左操作数增加 `task_id=`）

### 3.1 取证方法与工具

`git show HEAD:tests/integration/test_step_prepared.py` 导出到工作区**外**的临时目录（`%LOCALAPPDATA%\Temp`，不落工作区、不入 git），与工作区文件做 **AST 逐函数断言抽取 + `ast.unparse` 文本比对**；node id 用 `--collect-only -q`（改后真实 pytest）+ HEAD 侧 AST 推导（改前无法直接 collect）。

### 3.2 结论表

| 检查项 | HEAD | 改后 | 判定 |
|---|---|---|---|
| 顶层 `test_*` 函数 | 11 个 | 11 个 | **数量一致** |
| 函数名与顺序 | — | — | **逐字一致（`TEST_NAMES_IDENTICAL: True`）** |
| `--collect-only -q` | 20（AST 推导：10 个独立 + `test_crash_after_prepared_is_recovered_without_replay[0..9]`） | **20 tests collected** | **一致**（`CRASH_ITERATIONS = 10` 两侧均未变） |
| 参数化范围 | `range(CRASH_ITERATIONS)` = 10 | 同 | **未缩小** |
| 测试函数体内断言总数 | **42** | **42** | **一致** |
| 逐函数断言条数 | 14 / 3 / 3 / 2 / 0 / 0 / 1 / 1 / 0 / 5 / 13 | 同 | **逐函数一致** |
| 模块级断言总数 | **43** | **44（+1）** | **+1 且唯一新增位于 `created_tasks` 夹具内** |
| 新增的那 1 条断言 | — | `repository.get_task(task.task_id, tenant_id=task.tenant_id).task_id == task.task_id` | 只读核对（`control_client`），与 `10-lead-rulings.md` §4 对 M01 的同法裁定一致 |
| `admin_client` 是否仍被本文件引用 | 是（11 张表 DELETE） | **否** | 环境项 `ENV-1045` 的触发面消除 |
| `_CLEANUP_TABLES` 常量 | 存在 | **保留（仅加注释"本轮起不再执行"）** | 未删除历史清理清单 |
| `skip` / `xfail` | 0 | **0** | 未新增 |
| 9 处调用点收窄 | 9 处租户级（`sweep` ×5、`find_unobserved` ×4） | 9 处任务级（`sweep_task` ×5、`find_unobserved + task_id` ×4） | **9/9 全部收窄，无遗留租户级调用** |

### 3.3 逐函数断言文本比对（关键）

**10 / 11 个测试函数的断言文本逐字相同（`SAME`）**，唯一 `DIFF` 是：

```
DIFF test_older_than_threshold_filters_recent_prepared: HEAD=2 CUR=2
   - step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=60) == []
   + step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=60, task_id=task.task_id) == []
   - len(step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=0)) == 1
   + len(step_records.find_unobserved(tenant_id=task.tenant_id, older_than_s=0, task_id=task.task_id)) == 1
```

**成因（客观事实）**：该用例把 `find_unobserved(...)` **内联在 assert 表达式里**（设计 §2.2④ 表格 `:241` / `:242` 两行就是这两条）。只要把扫描范围收窄，assert 行文本**必然**改变——这与另外 7 处（调用在独立语句上、assert 在下一行）在形态上不同。

### 3.4 判定：硬保护「逐字」在字面上不成立（D-1，**已裁定：接受偏差，依据 `10-lead-rulings.md` §7 E-1**）

| 维度 | 结论 |
|---|---|
| 是否**放宽**断言 | **否**。比较算符 `==` 与右操作数 `== []` / `== 1` 逐字未变；断言条数 2→2 未变 |
| 是否**删除**断言 / 用例 | **否**。42→42，用例 20→20，无 skip/xfail |
| 是否**改比较对象** | **左操作数（被测查询）的扫描范围**由「租户全量」收窄为「本任务」，**判据（`== []` / `== 1`）与右操作数未变** |
| 被测命题是否变 | **否**。命题仍是「本用例那条 prepared 行被 `older_than_s=60` 过滤掉 / 用 `older_than_s=0` 恰好命中 1 条」 |
| 与设计文档一致性 | **不一致（设计文档事实性错误）**：`13-l1-l2-design.md` §2.2④ 把 `:241`/`:242` 的「断言是否变」标为「否」；§3.3 的「测试函数体内断言数 HEAD = 改后（逐字）」同此不准确 |
| 我的判定（QA 独立） | **不构成"为让测试变绿而放宽断言"，不是源码缺陷**；不建议路由 Engineer，QA 也不自行改动 |
| 主理人裁定（终态） | **接受该偏差，定性「非断言放宽、非源码缺陷」**（`10-lead-rulings.md` §7 **E-1**）；不路由 Engineer、QA 不改 `.py`、不拆分 assert 行、不回写设计档 |
| 若要求绝对逐字（假想方案） | 唯一替代是拆成「先赋值再断言」（`u60 = ...; assert u60 == []`），但那同样重写 assert 所在行、改动测试结构，**已裁定不做** |

### 3.5 统一口径（`docs/w09/10-lead-rulings.md` §7 **E-1**，本文件此后**唯一**可用的表述，原文照录）

> L1 未改断言语义——42 条测试函数体断言中 **40 条逐字不变**；**2 条**（`test_older_than_threshold_filters_recent_prepared` 的 `:267/:268`）左操作数增加 `task_id=` 实参（查询范围租户级→任务级），**比较算符与右操作数逐字未变、断言条数 2→2 未变**；模块级断言 43→44，唯一新增一条在 `created_tasks` 夹具内（只读 `get_task` 核对）。

> 口径来源：`docs/w09/10-lead-rulings.md` §7 **E-1**（该节「效力高于此前任务书中的口头措辞」）。

**口径禁令（主理人明令）**：本文件及后续任何引用，**不得再表述为「断言文本逐字不变」**；须用上述「40/42 逐字不变 + 2/42 左操作数增加 `task_id=` 实参」的精确口径。

### 3.6 必须登记的三条事实（主理人指定，对应 §7 E-1 / E-2 / E-3）

| # | 事实 | 定性 | 裁定条目 |
|---|---|---|---|
| ① | 设计 `docs/w09/13-l1-l2-design.md` §2.2④ 把 `:241/:242` 标为「断言是否变 = 否」是**事实错误**（该两行是 assert 内联调用，收窄必然改 assert 行）；§3.3 的「测试函数体内断言数 HEAD = 改后（逐字）」表述不准确 | **设计文档缺陷，不是实现缺陷**（本轮不改设计档，双记录） | **E-2** |
| ② | `:267/:268` 改后为 **110 / 113 字符**，超本仓 **100 字符**惯例（HEAD 版仅 `:117` 一行本就超）；环境**未安装 ruff / flake8**，无法机器化 lint | **已知风格偏差，不修**（拆行会扩大 diff 且无门禁收益） | **E-3** |
| ③ | 主理人任务书原措辞「测试函数体内断言文本逐字不变」**过严、实际不可达、与内联调用的现实冲突** | **责任在主理人，不计入工程师违规** | **E-1** |

**处置边界（主理人明令，本轮已遵守）**：不改设计文档、不改任何 `.py`、不拆分 assert 行。

### 3.7 补充机器取证（我自己跑的，非引用工程师结论）

| 取证 | 结果 |
|---|---|
| `inspect.signature(Reconciler.sweep).parameters` | `['self','tenant_id','older_than_s']` → **精确等于冻结值** |
| `inspect.signature(Reconciler.sweep_detailed).parameters` | `['self','tenant_id','older_than_s']` → **精确等于冻结值** |
| `inspect.signature(Reconciler.sweep_task).parameters` | `['self','task_id','tenant_id','older_than_s']` → **等于设计 §3.1 指定值** |
| `sweep` / `sweep_detailed` 默认值 | `tenant_id=None, older_than_s=60.0` → **逐字未变** |
| `StepRecordRepository.find_unobserved` 形参表 | `['self','tenant_id','older_than_s','limit','offset','task_id']`（新增项在**末尾**且默认 `None`） |
| **`task_id=None` 时 SQL 是否与 HEAD 逐字一致** | **`IDENTICAL TO HEAD: True`**（拼装后字符串逐字节相同），`params` 顺序 `tenant→cutoff→limit→offset` 一致、`max_rows=limit` 保留 |
| `task_id` 非空时 SQL | 追加 `AND p.task_id = %s`，`params` 顺序 `tenant→cutoff→task→limit→offset` 一致 |
| `Reconciler._reconcile_one` AST | **`SAME`**（判定逻辑零改动） |
| `Reconciler._target_status` / `_record` / `_tenants_with_prepared` / `__init__` AST | **全 `SAME`** |
| `Reconciler.sweep` AST | `DIFF`（T06 已交付的「委托 `sweep_detailed()`」，上轮已验；签名未变） |

---

## ④ L2 两处偏离的独立最终判定

### 4.1 偏离一：`getattr(result, "tool_call_id", None)` —— **成立且可接受，真实链路不构成漏洞**

**① 真实 `ToolResult` 是否必有 `tool_call_id`？**
- `core/tools/contracts.py:43-66`：`class ToolResult(BaseModel)`，字段 `tool_call_id: str | None = None`（`:50`）。**属性恒存在**（pydantic 字段，默认 `None`）。
- 实测：`hasattr(r, "tool_call_id") == True`；`getattr(r, "tool_call_id", "SENTINEL") == r.tool_call_id` → **对真实实例 `getattr` 与直取完全等价**。默认值实测为 `None`。
- 因此"必有该字段"成立，但"必有非空值"**不成立**（契约允许 `None`）。真实链路里 `None` 的出现点已被穷举：
  - `core/gateway/tool_gateway.py:291 / 363 / 404 / 490 / 571` 中，`tool_call_id=None` 出现在 **DENIED 分支的 `ToolResult`**（`:363` / `:404`）与 `emit` / `consume` 的**普通参数**上；
  - `:404` 的 `executor.execute(..., tool_call_id=None, ...)` 让执行器**自生成** id：`core/tools/executor.py:121` `call_id = tool_call_id or context.tool_call_id or new_id("call")` → **恒非空 `call_*`**；
  - 结论：**`EXECUTED` / `FAILED` / `UNKNOWN_OUTCOME` 三态的 `tool_call_id` 恒为非空 `call_*`**；唯一 `None` 的 `DENIED` 结果**不触发发布**（见 ④-③）。
- 工程师的解释（历史替身 `_FakeWorker.submit()` 返回 `SimpleNamespace(status=...)`）**实测属实**：`tests/unit/test_worker_runner.py:282` `return SimpleNamespace(status=self.status)`；直取 `.tool_call_id` 会抛 `AttributeError`（已实测），会让 `test_worker_runner.py` 的 38 条全红。

**② 该 `getattr` 是否会让真实链路出现「有 outcome 但 tool_call_id 为 None → 静默跳过发布」的漏洞？**
- **不会，且不存在这一实现**：`workers/runner.py::_publish_report` **不以 `tool_call_id` 作为门槛**，读到 `None` 时仍然调用 `reporter.publish(...)`（源码 + 探针双向确认）。
- 触发条件本身也不可达：`if outcome in (OUTCOME_EXECUTED, OUTCOME_FAILED, OUTCOME_UNKNOWN)` 与「三态 id 恒非空」互斥补集。

**③ 若 `tool_call_id` 为 `None`（当前仅替身可达），实际会发生什么？（我做的探针）**
- 探针（复用发布器的假端口，内存内、不落盘）：`publish(task, tool_call_id=None, succeeded=True)` →
  `generated=True`、`failed=False`、`REPORT_GENERATED` 已发、`verifications.calls == [('tenant_local', None)]`。
- 真实仓储层：`VerificationResultRepository.list_by_tool_call(tenant, None)` 走 `WHERE ... AND tool_call_id = %s`，`= NULL` 恒不命中 → 返回 `[]` → `latest_for_task` 返回 `None` → 报告**照常生成**，但**验证锚静默缺失**。
- 判定：这不是"静默跳过发布"，而是"**锚点静默降级**"（报告仍产出，`verification_state` 少一个事实来源）。**当前链路不可达**（三态 id 恒非空）。属可接受的设计余量，但**建议 W10 补一条 fail-soft 守卫**：`tool_call_id` 为空时打 `alert` 明确标注"验证锚缺失"，而不是靠 SQL 的 `= NULL` 静默返回空。**登记为观察项 O-1，不构成本轮缺陷、不路由 Engineer。**

**④ 最终判定（回答主理人指定问题）**

> **问题**：`getattr(result, "tool_call_id", None)` 在**真实链路**上是否会形成「有 outcome 但 `tool_call_id` 为 `None` → 静默跳过发布」的漏洞？
>
> **结论：不会。该漏洞形态在真实链路上不存在（既非"跳过"、也非"可达"）。**

| 判定维度 | 结论 | 证据 |
|---|---|---|
| `getattr` 与直取是否有生产差异 | **零差异**。`ToolResult` 是 pydantic 模型，`tool_call_id` 为声明字段（`:50`），属性恒存在 | `hasattr → True`；`getattr(r,...,"SENTINEL") == r.tool_call_id → True` |
| 是否会"静默**跳过**发布" | **不会**。`_publish_report` **没有**任何以 `tool_call_id` 为条件的 return/continue；读到 `None` 仍调用 `publish` | `workers/runner.py::_publish_report` 源码逐行核对 + 探针（`tool_call_id=None` → 仍 `generated=True`） |
| 真实链路能否出现「三态 + id=None」 | **不能**。执行器自生成且优先复用：`call_id = tool_call_id or context.tool_call_id or new_id("call")`（`core/tools/executor.py:121`）；`ToolResult` 构造点 `:441/482/547/714/753/789/874/909` 全部传 `call_id` / `context.tool_call_id` | 源码逐行核对 |
| 唯一 `tool_call_id=None` 的结果 | 是 `DENIED`（`tool_gateway.py:363` / `:404` 的 `_denied()`），而 `DENIED` **不在发布触发集合**内（只触发 `EXECUTED/FAILED/UNKNOWN`） | 触发条件 `if outcome in (...)` 与 `_classify()` 映射 |
| **影响面（假设最坏情形真的发生）** | 影响面**极小且可观测**：报告仍会生成并落库，仅 `verification_result_id` 缺失（`WHERE tool_call_id = NULL` 恒不命中 → `None`），**不会**改写任务状态、**不会**影响 `outcome`/退出码/`clean`、**不会**产生错误报告结论（`FactStatus`/`ActionState` 由 `StepPort` + 证据推导，验证只是其中一个来源） | 探针 + `core/verification/repository.py:49-54` SQL 语义 |
| 是否本轮缺陷 | **不是**。属"契约允许但链路不可达"的余量；**登记观察项 O-1**，建议 W10 补 fail-soft 告警（空 id 时打 `alert` 标注"验证锚缺失"，而非依赖 `= NULL` 静默返回空） | — |

**判定汇总**：偏离一**成立（工程师的处理必要）且可接受**；`getattr` 对真实 `ToolResult` 与直取严格等价（零生产差异），仅为兼容历史替身；**未引入任何"静默跳过发布"路径**，也未引入"有 outcome 却不发布"的分支。

### 4.2 偏离二：`ReportOutcome` 多 `report_id` 字段 —— **成立，设计内部不一致，补字段合理，默认 `None` 不破坏语义**

**设计原文两节（我逐字核对）**

`13-l1-l2-design.md` **§4.4**（`:433-437`）：

```python
@dataclass(frozen=True)
class ReportOutcome:
    generated: bool
    failed: bool = False
    skipped_reason: str | None = None
```

`13-l1-l2-design.md` **§5.3**（`:548`）要求：

> 成功 `self._reports_generated += 1` 并 `logger.info("报告已生成", extra={task_id, report_id})`

同文 **§4.8**（`:514`）也要求：

> 3. `logger.info("报告已生成")` 与 `report_id`（可用 grep 对账）。

**判定**

| 问题 | 结论 |
|---|---|
| §4.4 与 §5.3 是否真的冲突 | **是**。§4.4 的 dataclass 不暴露 `report_id`，而 §5.3 / §4.8 都要求 runner 日志打出 `report_id` 用于对账 → 设计内部不一致，工程师的说法**属实** |
| 补字段是否合理 | **合理且必要**。否则 runner 只能靠 `getattr(outcome, "report_id", None)` 打一个恒为 `None` 的日志，等于 §4.8 第 3 层可见性补偿**落空** |
| 默认 `None` 是否破坏既有语义 | **不破坏**。① 字段追加在**末尾**，位置参数构造 `ReportOutcome(True, False, None)` 仍成立；② 全仓 4 处构造**全部用关键字实参**（`workers/report_assembly.py:189/222/284`、`tests/unit/test_worker_report_hook.py:142/382/401`），实测无位置式调用；③ `generated` / `failed` / `skipped_reason` 三者互斥语义未变（失败与跳过分支的 `report_id` 均为 `None`，与"未落库"事实一致） |
| 是否有副作用 | 无。`report_id` 只在**成功落库**分支赋值（`report.report_id`），并只用于 `logger.info` 的 `extra`；不参与任何判定 |
| 结论 | **补字段是设计 §5.3 的必然要求，实现正确；建议由架构师在下一版设计档回写 `report_id` 字段，使 §4.4 与 §5.3 一致** |

> 附注：§5.3 的调用点是 `tool_call_id=result.tool_call_id`（**直取**），工程师落地时改成 `getattr(...)` —— 这是偏离一对偏离二的引用，已在 ④-1 判定为可接受。

**最终判定**：偏离二**成立**——§4.4 与 §5.3 / §4.8 的冲突是**设计文档内部不一致**（设计缺陷），补 `report_id: str | None = None` 是 §5.3 的**唯一可实现路径**，实现正确、默认值不破坏任何既有语义（关键字实参、字段追加末尾、三态互斥不变）。建议由架构师在下一版设计档把 `report_id` 回写进 §4.4；本轮按禁令**不改设计文档**。

---

## ⑤ L2「不阻塞、不改语义」取证

### 5.1 `RunReport.clean` 判据逐字未变（AST 取证）

```
RunReport.clean 函数体 AST identical (HEAD vs CUR): True
HEAD: self.denied == 0 and self.skipped == 0 and (self.failed == 0) and (self.unknown_outcome == 0)
CUR : self.denied == 0 and self.skipped == 0 and (self.failed == 0) and (self.unknown_outcome == 0)
```

- `reports_generated` / `report_failed` **不出现在 `clean` 表达式里**（AST 比对结论 + 源码比对）。
- 与 W09 T06 引入 `reconcile_failed` / `reconcile_scanned_total` 同法：只新增字段，不改判据。
- `merged()` 与 `to_public_dict()` 同步新增两项（源码核对）。

### 5.2 `workers/cli.py` 零改动 + 退出码取值与判据逐字不变

| 检查项 | 结果 |
|---|---|
| `git status --porcelain workers/cli.py` | **空**（未改） |
| `git diff HEAD -- workers/cli.py` | **空** |
| sha256 vs 基线 `c117d9cb…ec407` | **SAME** → 文件逐字节未变，退出码与判据必然逐字不变 |
| `EXIT_OK / EXIT_OUTSTANDING / EXIT_CONFIG_ERROR` | `0 / 1 / 2`（探针实测） |
| 判据原文 | `return EXIT_OK if report.clean and outstanding == 0 else EXIT_OUTSTANDING`（`:242` / `:252`），未变 |

### 5.3 `_publish_report` 是否真「全包 try/except 且绝不 raise」

**源码事实**（`workers/runner.py`）：
- `reporter is None` → 立即 `return`（未装配即零行为变化，连日志都不打）；
- `reporter.publish(...)` 包在 `try`，`except Exception as exc` → `self._report_failed += 1` + `logger.error(alert=REPORT_FAILED, reason_code="REPORT_PUBLISH_RAISED", code/error_type/message_digest)` + `return`；
- `publish` 自己返回 `ReportOutcome(failed=True)` 的路径 → `report_failed += 1` + `alert=REPORT_FAILED, reason_code="REPORT_PUBLISH_FAILED"`（**不重复抛**）；
- 成功 → `reports_generated += 1` + `logger.info("报告已生成", extra={task_id, report_id, tool_call_id})`；
- 三种分支**都不改写 `outcome`、不 return 值、不触碰 task 状态**。

**边界探针（我自己跑，明确边界而非夸大）**

| 注入异常 | 结果 |
|---|---|
| `RuntimeError` | **已吞**，`report_failed=1` |
| `ValueError` | **已吞**，`report_failed=1` |
| `SystemExit(3)` | **向上抛出**（`except Exception` 不覆盖 `BaseException`） |
| `KeyboardInterrupt` | **向上抛出** |

判定：与设计 §5.3 原文「全包 `try/except Exception`」**一致**；`BaseException`（关机/中断信号）不被吞是**正确的**——吞掉 `KeyboardInterrupt`/`SystemExit` 会让 Worker 无法正常停机。登记为**边界说明（B-1）**，不是缺陷。
残余理论风险：`except` 分支内调用了 `canonical_digest({...})`（两个 `str` 入参），若它自身抛错则会穿出 `_publish_report`；实测不可达，登记为**理论风险 B-2**。

### 5.4 独立探针：报告失败/成功对 `RunReport` 的影响（不采信工程师的 14 条单测）

我自建最小替身（内存内，不连库）跑同一候选，与「无发布器」基线逐键对比：

| 注入情形 | `failed` | `clean` | `reports_generated` | `report_failed` | 与基线**差异键** |
|---|---|---|---|---|---|
| 无发布器（基线） | 0 | True | 0 | 0 | — |
| `publish` 抛异常 | **0** | **True** | 0 | **1** | **仅 `report_failed`** |
| `publish` 返回 `failed=True` | **0** | **True** | 0 | **1** | **仅 `report_failed`** |
| `publish` 成功 | **0** | **True** | **1** | 0 | **仅 `reports_generated`** |

→ `failed` / `clean` / `executed` / `denied` / `skipped` / `unknown_outcome` / `reconciled` / `reconcile_failed` / `reconcile_scanned_total` **全部与基线逐字相同**；只有两个新计数键变化。**「失败不阻塞执行结论」成立。**

### 5.5 「额外验证（工程师未做）」：`--once` JSON 是否真把两键带出

- 源码：`workers/cli.py:115-118` `_report_payload()` → `payload.update(report.to_public_dict())`，随后 `_print(...)`。
- 装配冒烟（**不连库、不发 HTTP**，直接调用 `_report_payload`）：

```
payload keys: ['clean','cycles','denied','discovered','event','executed','failed','ok',
               'outstanding','reconcile_failed','reconcile_scanned_total','reconciled',
               'report_failed','reports_generated','skipped','unknown_outcome']
reports_generated: 2   report_failed: 3
JSON 可直接序列化: True
```

- 退出码判据冒烟（同一脚本）：

| `RunReport` | `clean` | 退出码 |
|---|---|---|
| `RunReport()` | True | **0** |
| `RunReport(failed=1)` | False | **1** |
| `RunReport(report_failed=99)` | **True** | **0** |
| `RunReport(reports_generated=99)` | **True** | **0** |

→ **两键确实进入 `--once` 的 JSON**；且**报告计数不改变退出码**（Q3 裁定「不并入 `failed`」已落实）。

### 5.6 真实库只读证明（发布不改任务状态）

`tests/integration/test_w09_report_publish_chain.py::test_publish_chain_persists_one_snapshot_and_one_event` 在真实控制库上断言（我实跑 `2 passed`）：

- `report_snapshots` 本任务**恰 1 行**；重复 `publish` 后仍为 1 行（幂等 `ALREADY_GENERATED`）；
- `task_events` 中 `REPORT_GENERATED` **恰 1 条**，payload 含 `fact_status` / `action_state` / `verification_state` / `degraded`；
- `redaction_version == worker_stack.redaction.version == "redaction-v2"`；
- **发布前后 `tasks.status` 与 `state_version` 完全未变**（只读证明）。

---

## ⑥ 静态守护与 sha256 差异清单

### 6.1 `workers/report_assembly.py` 静态扫描（**全文含 docstring**）

| 守护项 | 期望 | 实测 |
|---|---|---|
| `ToolGateway(` | 不出现 | **不存在** |
| `ToolRegistry()` | 不出现 | **不存在** |
| `pymysql` / `psycopg` import | 不出现 | **不存在**（文件内 10 条 import 全部为 `core.*` + `__future__` / `dataclasses` / `typing`） |
| 裸 SQL `SELECT … FROM` | 不出现 | **不存在** |
| 裸 SQL `INSERT INTO` | 不出现 | **不存在** |
| 裸 SQL `UPDATE dg_system` | 不出现 | **不存在** |
| 裸 SQL `DELETE FROM` | 不出现 | **不存在** |
| 裸 SQL `CREATE TABLE` | 不出现 | **不存在** |

### 6.2 `workers/stack.py` 的「唯一闸门实例」

| 检查项 | 实测 |
|---|---|
| 全文 `RedactionGate(` 出现次数 | **1**（`:306` `redaction_gate = RedactionGate(DataRedactionEngine(), audit=audit)`） |
| 同一局部名喂两个槽位 | `gate=redaction_gate`（`:312`）与 `redaction=redaction_gate`（`:338`）**均为同一局部名** |
| `workers/` 侧 `ToolRegistry()` | **0** |
| `ToolGateway(` | **1**（仍只在 `workers/stack.py`） |
| `build_lab_stack(` | 1 处真实调用（`:238`）+ 1 处 docstring（`:13`） |
| **真实装配 `publisher.redaction is stack.redaction`** | **True**（我写的一次性只读探针 + 集成用例 `:127` 双向确认） |
| 真实装配其它事实 | `stack.redaction is None → False`；`report_publisher is None → False`；`redaction.version == "redaction-v2"`；`publisher._tenant_id == stack.tenant_id == "tenant_local"` |
| `WorkerStack.redaction` 槽位是否有消费者按 `None` 分支 | grep 全仓 `workers/` / `core/` / `apps/` / `adapters/`：**无**（槽位只被写入 + 被 `is` 断言） |

### 6.3 sha256 差异清单（对照 `11-worker-ac-baseline.md` §6.3）

| 文件 | 基线锚 | 本轮实测 | 结果 | 说明 |
|---|---|---|---|---|
| `tests/unit/test_worker_layering.py` | `dd41be5c…97262` | `dd41be5c…97262` | **SAME** | 基线测试未被动 |
| `tests/unit/test_worker_isolation.py` | `ac47d3e4…bad02d` | `ac47d3e4…bad02d` | **SAME** | — |
| `tests/unit/test_worker_runner.py` | `7a222efe…184fdd` | `7a222efe…184fdd` | **SAME** | **L2 未改冻结单测** |
| `tests/integration/test_worker_process.py` | `5a2c69bf…cd494b` | `5a2c69bf…cd494b` | **SAME** | — |
| `workers/stack.py` | `a09dfbf5…be6227` | **`b4b67193…c97f`** | **DIFF** | **L2 显式授权改动**（第 11 步装配 + `report_publisher` 字段 + docstring 第 5 条）。按 `11-worker-ac-baseline.md:206`「hash 变化须立即回报」登记 |
| `workers/runner.py` | `fb31a6c0…72f8` | **`aa92e7a1…2b2c9`** | **DIFF** | 预期（W09 T06 + W10 L2 两批改动面） |
| `workers/__main__.py` | `fdc182af…0ff7` | `fdc182af…0ff7` | **SAME** | — |
| `workers/cli.py` | `c117d9cb…ec407` | `c117d9cb…ec407` | **SAME** | **退出码与判据逐字不变的文件级证明** |
| `core/approval/service.py` | `b56bb8b3…dd91` | **`4b260779…9aee`** | **DIFF** | 预期（W09 T03 KD-1 改动面，上轮已登记） |
| `core/gateway/tool_gateway.py` | `75fa3985…455f` | `75fa3985…455f` | **SAME** | — |
| `docker-compose.yml` | `72b400fb…bf` | `72b400fb…bf` | **SAME** | **compose 未动** |
| `deploy/Dockerfile.worker` | `80e60caa…d9bf` | `80e60caa…d9bf` | **SAME** | — |
| `tests/conftest.py` | （未在 §6.3 列锚；上轮记为 SAME） | `462182be…e1ce`，`git status` **无 M** | **SAME** | — |

→ **四个 W08 基线测试文件 + `workers/cli.py` + `__main__.py` + `tool_gateway.py` + compose + Dockerfile 全部 SAME**；差异仅 `workers/stack.py`（L2 授权）、`workers/runner.py`（T06+L2）、`core/approval/service.py`（T03），**三个预期文件，无「测试被改 / 断言被放宽」痕迹**。

---

## ⑦ 冻结数字对账表

| 冻结项 | 期望 | 本轮实测 | 结论 |
|---|---|---|---|
| `tests/unit/test_w09_sweep_scanned.py` | 10 | **10 passed** | ✅ 未变 |
| `tests/unit/test_worker_scheduler.py` | 35 | **35 passed** | ✅ 未变 |
| `tests/unit/test_worker_runner.py` | 38 | **38 passed** | ✅ 未变 |
| `tests/unit/test_worker_layering.py` | 10（`builders == ["stack.py"]`） | **10 passed** | ✅ 未变 |
| `tests/unit/test_worker_isolation.py` | 2 | **2 passed** | ✅ 未变 |
| `tests/unit` 全量 | 1336 | **1336 passed** | ✅ 一致 |
| `workers/cli.py` sha256 | 不变 | **SAME** | ✅ |
| 四个基线测试文件 sha256 | 全部 SAME | **全部 SAME** | ✅ |
| `sweep` / `sweep_detailed` 签名 | 逐字不变 | **逐字不变** | ✅ |
| `_reconcile_one` AST | 零改动 | **SAME** | ✅ |
| `RunReport.clean` 判据 | 逐字不变 | **AST 相同** | ✅ |
| `test_step_prepared.py` 用例数 | 20 | **20 collected / 20 passed** | ✅ |
| 测试函数体内断言总数 | 不变 | **42 → 42** | ✅ |
| 模块断言总数 | +1 且在夹具内 | **43 → 44，唯一新增在 `created_tasks`** | ✅ |
| 断言文本是否**全部**逐字不变 | 硬保护项（原措辞，已裁定过严） | **不是全部**：40/42 逐字不变；2/42 左操作数增加 `task_id=` 实参（`:267/:268`，算符与右操作数未变、条数 2→2 未变） | ✅ 已裁定接受（§7 E-1） |

---

## ⑧ 遗留与未验证（单列，不作为通过依据）

> **编号消歧（务必区分）**：`docs/w09/10-lead-rulings.md` §7 的 **E-1 / E-2 / E-3** 指「执行期口径勘误」（断言表述失效 / 设计文档事实错误 / 风格偏差），**不是**环境项编号。上一轮 `12-w09-qa-closeout.md` §⑧ 曾用 `E-2` 指 `DG_ADMIN_DSN` root 1045、`E-3` 指控制库残留；本文件起改用 **`ENV-1045`** / **`ENV-RESIDUE`** 指代环境项，避免与 §7 的 E-1/E-2/E-3 混用。

1. **C6 口径（本轮已改）**：`test_step_prepared.py` 现为 `2 passed, 18 deselected / 20 passed`、**0 errors**。该结果**仅表示「残留污染不再导致该文件用例失败」**——**残留实测仍在增长**：我本轮只读 SELECT 实测控制库 **`tenant_local` 未观测 140 行**（上轮 QA 报告记录为 102）、`step_prepared` **276 行** / `step_observed` **136 行**。**变绿来自扫描范围收窄（租户级→本任务），不是残留消失**；残留**未清、也未被本卡清理**，`DG_ADMIN_DSN` root 1045 **未修**。且**本轮连库跑本身贡献了 102→140 这部分增量**（各集成用例只 INSERT，见 ⑩）。
2. **ENV-1045（`DG_ADMIN_DSN` root 1045）**：管理员凭据在本机始终不可用；B13 的 1 条 `UserWarning` 同源。本轮未尝试修复、未改账号、未改 `.env`。
3. **ENV-RESIDUE（控制库残留）**：`tenant_local` 未观测行由 **102** 增至 **140**（跨多轮集成用例只 INSERT，其中本轮连库跑贡献了一部分）。按全局纪律**未 DELETE / 未 UPDATE / 未 TRUNCATE**；本轮测试执行自身产生的写入已在 ⑩ 透明披露。
4. **KD-W09-03（内部通道 PII）**：`StepPrepared.request` 与 `TrajectoryRecorder`（含 `usage`）不接管 `REDACTED`/`REVIEW_REQUIRED` 改写，非敏感键名下的 PII 仍可能原样落盘；沿用 `02-design.md:67` 已知限制，W10 再议。
5. **compose / NV-1**：`docker-compose.yml` sha256 **SAME**（未动）；本机 Docker daemon 不可用，Worker 容器运行态 `NOT_VERIFIED`，按 `10-lead-rulings.md` §2-5 挂 W11；`docker compose config` **未执行**（会回显 `.env` 变量值，本轮只读边界不引入）。
6. **R6（幂等竞态）**：两 Worker 进程同任务同时 `publish` 会各插一行（PK 是 `report_id`，不报错）。执行侧已由租约独占 + 审批一次性 + `worker_max_concurrency=1` 封死；**未加分布式锁**（会引入新写面），维持理论风险。
7. **报告组件已接装配，但「验证后重生成」仍缺**：`publish()` 的幂等守卫是「该任务已有快照即跳过（`ALREADY_GENERATED`）」，没有 `report_version` 递增、没有重生成入口。`core/report/**` 本轮零改动（禁改），因此「先验证、再生成一版新报告」必须另立卡（属 W11 `Verifier` 生产接线范畴）。
8. **R2（「快照已落、`REPORT_GENERATED` 未发」窗口不可自愈）**：builder 内部顺序是「先 `reports.insert` 再 `emit_required`」；若发事件失败，快照已存在 → 幂等守卫会永久跳过重试，该任务永久缺 `REPORT_GENERATED`。本轮**不改 builder**，如实登记为已知限制（建议 W11 用 outbox / 先事件后快照修）。
9. **R1 口径（无证据仍生成 `UNKNOWN`）**：`worker` 执行链不写 `evidence` 行，因此实际生产中多数报告会是 `fact_status=UNKNOWN`；本轮按 Q5 默认「生成」并在单测/集成用例里把该事实钉死（`test_publish_without_evidence_still_generates_unknown_fact`、`test_publish_without_evidence_still_writes_one_unknown_snapshot`）。若主理人改为「无证据不生成」，需同步改这两条用例。
10. **观察项 O-1**（④-1 提出）：`tool_call_id` 为空时发布器无守卫，会「生成报告但验证锚静默缺失」。当前链路不可达（三态 id 恒非空），建议 W10 补 fail-soft 告警。
11. **边界项 B-1 / B-2**（⑤-3 提出）：`except Exception` 不覆盖 `BaseException`（符合设计原文，且不吞关机信号）；`except` 分支内的 `canonical_digest` 理论可抛（实测不可达）。
12. **可观测性缺口（`_execute` 的失败计数不对称，设计已明示）**：`worker.submit()` 抛 `DBGuardianError` 的路径返回 `OUTCOME_FAILED` 但**不发布报告**（无 `ToolResult` / 无 `tool_call_id`），因此该路径 **`reports_generated=0` 且 `report_failed=0`** —— 与「未装配发布器」在计数上**不可区分**，运维单看 `--once` JSON 无法判断是"没装"还是"提交失败没得发"。设计 §4.1 注已显式接受此取舍（不伪造 `tool_call_id`）；登记为**可观测性缺口**，不影响执行语义，建议 W10 增一个独立计数或 reason 码。
13. **A.8 中未列入本轮命令的用例仍未实跑**（沿用上轮边界）：`test_help_lists_worker_flags`、`test_self_check_returns_zero_when_stack_is_available`、`test_dry_run_lists_candidate_without_executing`、`test_w07_rejection_matrix.py`、`test_approval_consume.py`；AC② 的六类拒绝矩阵 / N=8 并发消费分支未由本轮覆盖。
14. **PG 5432**：未涉及、未验证；本轮全部定向往返只打 MySQL 控制库 + LAB。
15. **D-1（口径偏差）——已裁定（`10-lead-rulings.md` §7 E-1），非遗留**：L1 有 **2/42** 条断言因「调用内联在 assert 中」而左操作数增加 `task_id=` 实参；**比较算符与右操作数逐字未变、断言条数 2→2 未变**。裁定定性「非断言放宽、非源码缺陷」，不路由 Engineer、不修 `.py`、不拆分 assert 行、不回写设计档；登记于 ③-4 / ③-5 / ③-6。**本条不构成待办项**，仅为可追溯留痕。
16. **设计档待回写（非本轮范围）**：`13-l1-l2-design.md` §2.2④（`:241/:242` 判定为「否」）与 §3.3（「断言数 HEAD = 改后（逐字）」）表述不准确，且 §4.4 的 `ReportOutcome` 缺 `report_id` 与 §5.3/§4.8 不一致。两处均为**设计文档缺陷**，建议由架构师在下一版设计档回写；本轮按禁令**不改设计文档**。

---

## ⑨ 结论口径与表述禁令自查

1. **W08**：一律写「**有条件放行**」（`docs/w08/07-gate-review.md:10`）。本文件未写「已通过 / 已收口」。
2. **W07**：一律写「**有条件放行；门禁通过：否**」（`docs/w07/07-gate-review.md:11,20`）。两份历史冻结件（`05-freeze.md`、`06-conformance-audit.md`）仍缺，按 KD-5 登记为已知偏离、关闭条件挂 W11。
3. **C6 口径**：只写「**残留污染不再导致该文件用例失败**」；**未写**「残留已清」「`DG_ADMIN_DSN` 已修」「环境已清零」。残留实测 140 行仍在，凭据仍未修。
4. **W09 口径**：只写「**L1 / L2 已实现并通过定向验收，W09 门禁未评**」；**未写**「回归全绿」「W09 已实现」「W09 已收口」「整体回归通过」。
5. **L1 断言口径（`10-lead-rulings.md` §7 E-1 唯一口径，见 ③-5）**：本文件**未将**「断言文本逐字不变」作为结论使用；一律写「**42 条中 40 条逐字不变、2 条左操作数增加 `task_id=` 实参（比较算符与右操作数逐字未变、条数 2→2 未变）、模块级 43→44 唯一新增在夹具内**」。该偏差已裁定「非断言放宽、非源码缺陷」，不路由 Engineer、不修 `.py`、不拆分 assert 行、不回写设计档。
6. 本文件未使用 `10-lead-rulings.md` §6 与 `03-task-cards.md:735` 列举的禁止措辞（写入后已逐条自查）。B13 的 1 条 `UserWarning` 全程记为**环境项**，未记为失败、也未因它未消失而推断任何环境状态。
7. 未事后补造任何 W07 / W08 冻结件；未前移 / 重建 `w07-close`；未执行任何 git 写操作。

---

## ⑩ 本轮读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅** `docs/w09/14-l1-l2-qa-closeout.md`（本文件按主理人裁定补录 ③-4/③-5/③-6 与 ④ 影响面，仍为同一文件，未新增第二个文件） |
| **QA** 修改 `.py` / SQL / 配置 / 既有 docs / `docker-compose.yml` / `.env` | **0**（`git status --porcelain` 在本轮前后完全一致：开工时 **17 项 `M` / 32 项 `??`**，收工时 **17 项 `M` / 33 项 `??`**，唯一新增项即本文件） |
| 手工 INSERT / UPDATE / DELETE / TRUNCATE / ALTER / rollback | **0** |
| 改数据库账号 / 环境变量 / 重启服务 | **0** |
| **测试执行固有写库（透明披露）** | 依各集成用例设计**只 INSERT**：`test_step_prepared.py`（20 条）、`test_w09_sweep_task_scope.py`（2 条）、`test_w09_report_publish_chain.py`（2 条）、`test_worker_process.py::..._repair_once`（1 条，用例 finally 恢复 LAB 目标行）。A2/A4/B12/B13 四次连库跑合计使 `step_prepared` / `task_events` / `report_snapshots` 行数增加；只读 SELECT 复核 `report_snapshots` 共 8 行、`tenant_local` 未观测 140 行。**无任何清理型 DELETE** |
| 只读 SELECT / 只读探针 | 4 次（残留计数 1 次、`build_worker_stack` 单实例探针 1 次、`RunReport` 失败隔离探针 1 次、`--once` JSON 冒烟 1 次）；全部内存内或不写库，**未落盘任何临时脚本** |
| git commit / add / checkout / reset | **0** |
| 无路径全量 pytest / `--timeout=` / `-n` 并发 | **0**（A–C 全部定向路径；需 DB 用 Git Bash 外层 `timeout`；全程串行单进程） |
| 放宽断言 / 删用例 / 加 skip / xfail / 缩小参数化 | **0**（`test_step_prepared.py` 的 skip/xfail 计数 0→0；`CRASH_ITERATIONS` 10→10；新增集成文件的 `pytest.skip` 仅为环境不可达兜底且本轮**未触发**） |
| 与其他成员直连通信 | **0**（仅回传 team-lead） |
