# W11 → W12 门禁评审（安全回归与长稳）

| 项 | 值 |
|---|---|
| 评审件编号 | `docs/w11/06-w11-gate-review.md` |
| 评审人 | 严过关（Edward）· QA 工程师｜团队 `software-dbguardian-w09-closeout2` |
| 评审性质 | **门禁评审**（不是定向验收；`docs/w11/05-w11-qa-closeout.md:5` 明写「**W11 门禁状态：未评**——门禁由主理人裁定，本文件不代签」） |
| 时点锚 | `HEAD = 2850eca326850eec491e9159ec7033a85807d080`；全部改动**未提交**（`git diff --name-only` = 25、`git status --short` = 121 = 25 `M` + 96 `??`） |
| 解释器 / 现场 | `.venv/Scripts/python.exe` = Python 3.14.7；`D:/DBGuaredian` |
| 判据来源 | `docs/w11/01-prd.md` **§6 `:150-165`**（3 条门禁 + 未通过处置 + 完成定义）、§5 AC 表 `:126-148` |
| 上游口径 | W07「**有条件放行；门禁通过：否**」；W08「**有条件放行**」；W09「**有条件放行｜门禁通过：否**」；W10「**有条件放行｜门禁通过：否**」（本轮同批出件）；W11 门禁此前未评，本件为首次正式评审 |
| 唯一写入 | **本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs（含 `docs/w11/01-prd.md`、`02-design.md`、`03-task-cards.md`、`05-w11-qa-closeout.md`、`backup-restore-report.md`、`fault-injection-report.md`、`limitations.json`、`rc-freeze.md`）；未改 `docs/w12/known-limitations.*`；未改数据库；未 `git commit/add/checkout/reset` |

---

## ① 执行摘要

**门禁结论：`有条件放行`｜门禁通过：否。**

判据一句话：PRD §6 的三条门禁判据**全部为「部分满足」**（每条都有一条 AC 停在 `PARTIAL`），且 `docs/w11/01-prd.md:165` 的**完成定义「交付物已提交到仓库」不满足**（零 commit）——存在多项未满足，不能判「通过」。

**为什么不是「未通过」**：
- **无安全红线失败**：我实算 `publish_decision()`（真实代码路径）→ `decision = TECH_PREVIEW_ONLY`，`failed_test_ids = ()`、`red_line_failures = ()`。也就是说**没有任何一条负面集/并发/故障注入用例判红**；`TECH_PREVIEW_ONLY` 是由 5 个**环境阻塞**（`environment_blocks`）触发的完备性守卫，按 PRD `:162` 属「如实登记」而非「红线失败」。
- **负面集本体通过**：`E-16 test_w11_security_negative.py` 我实跑 `32 passed`；冻结清单 29 条（`version=negative-set-v1`）。
- 无删减 P0 安全门禁：`test_w11_gate_inventory.py` 的 9 项 `present is True`（含在 `tests/unit` 1803 全量内，0 failed）。
- 环境项按 PRD `:162`/`:163` 如实登记，不判 FAIL。

**为什么不能判「通过」**：
1. 完成定义「交付物已提交到仓库」实测为零提交；
2. `G-1` 的 AC-03「拒绝不改数据」在 29 条负面集中只有 **6/29** 是非空验证，其余 23 条（G1–G3）用哨兵归属、前后计数恒 `0 == 0`（空真）——`docs/w11/05-w11-qa-closeout.md:66` 2-L 已独立认定；
3. `G-2` 的 AC-12「备份实际恢复并校验四类计数逐值相等」中 4 类有 3 类是 `0 == 0`，且设计 §8.2 明列的 `approval_consumptions`、`step_prepared` **未纳入快照**（KD-W11-01/02 仍在册）；**长稳未实跑**（我实算 `SoakReport.status = PLANNED`、`measured_sample_size = None`）；
4. `G-3` 的「完整端到端回归」中 PG 段 `NOT_EXECUTED`（`ENV-PG5432`）——「双引擎只读诊断」只完成了 MySQL 一侧。

**「定向验收通过」≠「门禁通过」**：`05-w11-qa-closeout.md:5` 与 `:174`（路由段）明确本件只给「定向验收」，门禁未评。本件不继承其结论。

---

## ② 门禁判据逐条对账

> 逐条取自 `docs/w11/01-prd.md` §6（`:152-155`），另加 `:165` 的完成定义。

### G-1｜安全负面集通过（`01-prd.md:153`）— **部分满足**

> 原文：AC-01～AC-04、AC-17～AC-20 全绿；冻结负面集中越权、未审批、审批篡改/重放、跨租户、只读写入等红线请求**全部阻断**。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-01 | 四元组完整（`test_id`/`expected_deny_reason`/`actual`/`evidence` 非空率 100%） | `tests/integration/test_w11_security_negative.py` | **E-16 `32 passed in 2.59s` exit 0（真实库）** | 满足 |
| AC-02 | 诱导逐条被拒、用例数 == 冻结清单数 29 | 同上 + `test_w11_negative_manifest.py` | E-16；manifest 在 E-1 内 0 failed | 满足 |
| AC-03 | 拒绝不改数据（前后计数相等） | 同上 | E-16 绿，但**证据强度不足**：G1–G3（23/29）用哨兵归属，前后恒 `0 == 0` | **部分满足** |
| AC-04 | manifest `test_id` 无重复、版本非空 | `tests/unit/test_w11_negative_manifest.py` | E-1 0 failed | 满足 |
| AC-17 | 红线失败即阻止发布（全绿 → `ALPHA_ALLOWED`；注入 1 条失败 → `TECH_PREVIEW_ONLY`） | `tests/unit/test_w11_publish_decision.py` | E-1 0 failed；**我实算**真实 `publish_decision()` → `TECH_PREVIEW_ONLY`、`failed_test_ids=()` | 满足 |
| AC-18 | 判定不可人工覆盖（签名无 `force_allow`/`override`） | 同上 | **我实取 `inspect.signature` = `['negative','concurrency','faults','environment_blocks']`**，无任何 `force_*`/`allow`/`override` | 满足 |
| AC-19 | 写面与引擎不回退 | `tests/unit/test_write_tool_gate.py` + `test_tools_contracts.py`（既存） | **E-8 `11 passed`（6 + 5）exit 0** | 满足 |
| AC-20 | 冻结 P0 门禁清单 9 项 `present is True` | `tests/unit/test_w11_gate_inventory.py` | E-1 0 failed | 满足 |

**AC-03 判「部分满足」的依据（我自己复核的口径）**：`docs/w11/05-w11-qa-closeout.md:66` 记录「G1–G3（23/29）用哨兵归属 `w11-neg-scope:<test_id>` / `w11-neg-tenant:<test_id>`，永不命中 → 前后均 0；仅 G4（6/29）计真实对象」。这意味着「拒绝不改数据」这条红线在 **79% 的用例上是结构性空真**：`0 - 0 == 0` 恒成立，无法区分「正确拒绝」与「拒绝后照样改了数据」。

**G-1 结论**：`部分满足`（AC-03 证据强度）。红线本体未失败。

### G-2｜状态恢复、跨租户、备份恢复均有报告（`01-prd.md:154`）— **部分满足**

> 原文：AC-05～AC-14 全绿；旧 Worker、过期 lease、错误 `fencing_token`、双 Worker 并发、重复消息不导致重复副作用；审计失败时 R2 停止；备份**实际恢复**并校验完整性；RPO/RTO 报告区分目标值与实测值。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-05 | 六类对象 × 跨租户 → `OBJECT_AUTH_DENIED` 且返回集为空 | `tests/integration/test_w11_cross_tenant.py` | 前序件 `16 passed`（真实库）；本件未重跑（时间预算） | 满足 |
| AC-06 | 并发零重复副作用（10 类） | `test_w11_concurrency.py` | 前序件 `19 passed`；本件未重跑 | 满足 |
| AC-07 | 旧 Worker → `FENCING_TOKEN_STALE`；双 Worker → `WORKER_CONFLICT` | 同上 | 同 AC-06 | 满足 |
| AC-08 | 审计失败 + R2 → `AUDIT_WRITE_FAILED`、`tool_calls_delta == 0` | `tests/integration/test_w11_fault_injection.py` | **E-17 `16 passed in 3.86s` exit 0（真实库）** | 满足 |
| AC-09 | R0 按策略返回且 `alert_emitted is True` | 同上 | E-17 覆盖 | 满足 |
| AC-10 | 六类恢复后 `seq` 连续、`state_version` 单调、Evidence 可解析 | 同上 | E-17 覆盖 | 满足 |
| AC-11 | 恢复不重放（无 `TOOL_CALL_RETRIED`、`tool_calls_delta == 0`） | 同上 | E-17 覆盖 | 满足 |
| AC-12 | 备份**实际恢复**并校验四类计数逐值相等 | `tests/integration/test_w11_backup_restore.py` | 前序件 `22 passed`；**证据强度不足**：4 类中 3 类为 `0 == 0`，且漏 2 张设计列明的表 | **部分满足** |
| AC-13 | RPO/RTO 目标与实测分列；`measured_* is None` → `sla_claim is False` | 同上 + `docs/w11/backup-restore-report.md` | 前序件：`measured_* = null`、`sla_claim = False`；我只读核对报告含 `restore_mode`/`physical_restore`/`sla_claim`/`NOT_SUPPORTED` 四类字段 | 满足 |
| AC-14 | 长稳 `sample_size>0`/`duration_s>0`/`smoke_test is False`；告警 6 项集合相等 | `tests/unit/test_w11_soak_report.py` | E-1 0 failed；**我实算 `build_soak_report(measured=None)`** → `status=PLANNED / sample_size=200 / duration_s=3600 / smoke_test=False / measured_*=None / p50=None / p95=None` | 满足（判据是「不冒充」，非「必须跑长稳」） |

**AC-12 判「部分满足」的依据**：AC-12 的字面要求是「恢复后事件/审批/Evidence/审计四类计数与恢复前逐值相等」。实跑绿，但：
- 4 类中只有 `task_events`（快照 1 行）是非空证据，其余 3 类都是 `0 == 0`；
- `BACKUP_OBJECT_TABLES` 覆盖 `task_events / approvals / evidence / step_observed`，**未含**设计 §8.2 明列的 `approval_consumptions` 与 `step_prepared`；
- `audit_chain` 这个逻辑类名映射到 `step_observed`（物理表正确、逻辑类名与设计不符）。

以上三条均已在 `docs/w11/backup-restore-report.md:124-133` 登记为 `KD-W11-01` / `KD-W11-02`，并被主理人裁定为「只登记、不改逻辑、移交 W12」（`docs/w10-w12-lead-rulings.md` §13.3）。

**附加（长稳）**：长稳**未实跑**（`status=PLANNED`，`measured_*=None`）。这**不违反** AC-14（AC-14 只要求「不冒充」），但使「W11 = 安全回归**与长稳**」这一周主题只完成了一半。我按 `NOT_SUPPORTED` 登记（§④ U-4），不作为门禁未通过的独立理由。

**G-2 结论**：`部分满足`。

### G-3｜RC 冻结与端到端回归（`01-prd.md:155`）— **部分满足**

> 原文：AC-15～AC-16、AC-21 全绿；冻结清单逐项有值，完整端到端回归（双引擎只读诊断、单引擎审批修复、Verifier、未知结果核对）通过。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-15 | RC 清单逐项有值；缺项 → `UNKNOWN` + 登记；版本与 manifest/迁移一致 | `tests/unit/test_w11_rc_manifest.py` | E-1 0 failed；**我独立只读复核 `benchmarks/w11/rc_manifest.json`**：`commit = 2850eca…`（与 `git rev-parse HEAD` 逐字相等）、`docs_migrations_lag = True`、`db_image_digest = "UNKNOWN"`、`model_version = None`、16 个必填键齐 | 满足 |
| AC-16 | 端到端四段各自断言；PG 段 `NOT_EXECUTED` 不判 FAIL | `tests/integration/test_w11_e2e_regression.py` | 前序件 `5 passed`（S2/S3/S4 `PASSED`，S1 的 PG 段 `NOT_EXECUTED` 且显式 `!= FAILED`） | **部分满足** |
| AC-21 | 上游指标口径未回退 | `tests/unit/test_w10_metrics.py`（既存 W10 门禁） | E-1 0 failed；`benchmarks/w10/metrics-v1.json` 我独立只读核对 → `version="metrics-v1"`、`definitions=14` | 满足 |

**AC-16 判「部分满足」的依据**：PRD 的门禁措辞是「**完整**端到端回归（**双引擎**只读诊断、单引擎审批修复、Verifier、未知结果核对）通过」。实测 S1（双引擎只读诊断）的 MySQL 侧 `PASSED`，但 **PG 侧 `NOT_EXECUTED`**（`ENV-PG5432`）。按 PRD `:162`「PG 5432 仍拒绝连接时，相关用例记 `NOT_EXECUTED` 并归因为环境故障，**不判 FAIL**」——所以**不构成 FAIL，但也不构成「双引擎」已跑通的证据**。这是"部分满足"而不是"满足"。

**G-3 结论**：`部分满足`。

### G-4｜本周完成定义（`01-prd.md:165`）— **不满足**

| 子条件 | 实测 | 判定 |
|---|---|---|
| 交付物已**提交到仓库** | `HEAD` = `2850eca…`；25 `M` + 96 `??`；W11 全部新文件（`core/assurance/*`、`benchmarks/w11/*`、`tests/**/test_w11_*`、`docs/w11/*`）为 `??` 未跟踪；**零 commit** | **不满足** |
| 关键测试有自动化结果/可复现记录 | E-1…E-17 共 17 条串行实跑 exit 0 | 满足 |
| 未通过项已登记并说明影响 | `05-w11-qa-closeout.md` §4（9 项 `L-*`）+ §5（5 项 `ENV-*`）+ §11.8 | 满足 |

**附（前序件已过期的事实）**：`05-w11-qa-closeout.md:4,209` 记录「`git diff --name-only` = 20、`git status --short` = 93/94」。本轮实测为 **25 / 121**（W12 与收尾轮继续改动）。时点差异，非违规（全程零 `git add`）。

**G-4 结论**：`不满足`（唯一原因是「未提交」）。

### 小结表

| # | 判据（PRD 行号） | 判定 |
|---|---|---|
| G-1 | 安全负面集通过（`:153`） | **部分满足**（AC-03 证据强度） |
| G-2 | 状态恢复/跨租户/备份恢复均有报告（`:154`） | **部分满足**（AC-12 空真 + 漏桶；长稳未实跑） |
| G-3 | RC 冻结与端到端回归（`:155`） | **部分满足**（AC-16 的 PG 段 `NOT_EXECUTED`） |
| G-4 | 完成定义：交付物已提交到仓库（`:165`） | **不满足** |

---

## ③ 独立取证

### 3.1 我自己跑的数字

纪律同前：`.venv/Scripts/python.exe`、`-o addopts="" -q -p no:cacheprovider`、**全程串行零并发**、需 DB 加外层 `timeout 180`、**不用 `--timeout=`**、仓库外全新 `--basetemp`。

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| E-1 | `pytest tests/unit`（全量） | `1803 passed in 24.87s` | 0 |
| E-16 | `timeout 180 pytest tests/integration/test_w11_security_negative.py` | `32 passed in 2.59s` | 0 |
| E-17 | `timeout 180 pytest tests/integration/test_w11_fault_injection.py` | `16 passed in 3.86s` | 0 |
| E-8 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py` | `11 passed in 0.18s` | 0 |
| E-6 | `pytest tests/unit/test_worker_scheduler.py` | `35 passed in 0.25s` | 0 |
| E-5 | `pytest tests/unit/test_worker_runner.py` | `38 passed in 0.50s` | 0 |
| E-12 | `timeout 180 pytest tests/integration/test_w09_redaction_pipeline.py` | `4 passed in 2.58s` | 0 |

**与任务书期望比对**：`tests/unit` 1803 → 一致；`test_w11_security_negative.py` 与 `test_w11_fault_injection.py` 二选一/抽跑 → 我两个都跑了（32 / 16），与任务书设计的"抽 1–2 个"一致且超额。

### 3.2 只读实算（不是重跑工程师命令）

| 取证项 | 方法 | 我的实测值 |
|---|---|---|
| `publish_decision()` 结论 | 直接调真实函数，喂真实 `ENVIRONMENT_BLOCK_IDS` | `decision = TECH_PREVIEW_ONLY`；`failed_test_ids = ()`；`red_line_failures = ()`；`environment_blocks = ('ENV-1045','ENV-MODEL','ENV-NV1','ENV-PG5432','ENV-RESIDUE')` |
| `publish_decision` 签名 | `inspect.signature` | `['negative', 'concurrency', 'faults', 'environment_blocks']` —— **无 `force_*` / `allow` / `override`** |
| `build_soak_report(measured=None)` | 直接调真实函数 | `status=PLANNED`、`sample_size=200`、`duration_s=3600`、`smoke_test=False`、`measured_sample_size=None`、`measured_duration_s=None`、`p50_ms=None`、`p95_ms=None` |
| `benchmarks/w11/rc_manifest.json` | 只读 JSON | `commit = 2850eca326850eec491e9159ec7033a85807d080`（== `git rev-parse HEAD`）、`docs_migrations_lag = True`、`db_image_digest = "UNKNOWN"`、`model_version = None` |
| `docs/w11/limitations.json`（只读引用） | 只读 JSON | `items = 6`，**auto = 6**（W11 侧 6 个自动项，被 W12 的 13 个自动项包含） |
| `benchmarks/w10/metrics-v1.json` | 只读 JSON | `version="metrics-v1"`、`definitions=14`（AC-21 上游口径未回退） |
| `docs/migrations.md` | 只读 | 仍止于 007（`has_007=True`、`has_011=False`）—— 与 `docs_migrations_lag=True` **自洽** |

**关键结论**：`TECH_PREVIEW_ONLY` 的成因是**环境阻塞（5 项）**，不是**红线失败（0 项）**。这两者不能混写——把前者说成「安全红线未过」是夸大；把后者说成「安全已通过」是掩盖。本件按 PRD `:158`/`:162` 的原始语义分开登记。

### 3.3 与前序 QA 收口件的数字对账

| 项 | `05-w11-qa-closeout.md` | 本轮实测 | 是否一致 | 说明 |
|---|---|---|---|---|
| `tests/unit` | R1 `1721`（`:76`）、R2 `1731`（`:196`） | 1803 | 数字不同 | 时点差异：W12（+70）、收尾轮（+2）在其后落地；**无回归** |
| `test_w11_security_negative.py` | `32 passed`（`:80`） | **E-16 `32 passed`** | **一致** | — |
| `test_w11_fault_injection.py` | `16 passed`（`:83`） | **E-17 `16 passed`** | **一致** | — |
| AC-19 既存两文件 | `11 passed`（`:78`） | **E-8 `11 passed`** | **一致** | — |
| `publish_decision` 真算 | `TECH_PREVIEW_ONLY`、`failed_test_ids=()`、5 blocks（`:245`） | **我独立实算逐项一致** | **一致** | 双向独立取证 |
| `SoakReport` 计划值/实测分离 | `status=PLANNED`、`measured_*=None`（`:63`、`:106`） | **我独立实算逐项一致** | **一致** | — |
| `docs_migrations_lag` | `docs/w11/rc-freeze.md:22` 与 `:50` 记 `True`（D-06：`docs/migrations.md` 止于 007） | **我实读 `rc_manifest.json` → `True`；实读 `docs/migrations.md` → 不含 `010`/`011`** | **一致** | — |
| `ENV-RESIDUE` 规模 | R1 `78319` → R2 `79313`（`:141`、`:346`） | 本轮未重测（不做 `COUNT`，避免新增写与时间开销） | 未复核 | 登记为未复核项（§④ U-7） |
| tracked-modified 计数 | `20`（`:4`、`:209`） | **25** | **不一致（时点差异）** | — |
| 文件计数 | 实测 31 个新文件（`:111`） | 未重数 | 未复核 | 非判据 |

### 3.4 取证方法学（如实披露）

同一批次的两次取证副作用，同时影响本件，必须披露：

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| **M-1** | `tests/unit` 首轮 `1 failed, 1802 passed`，唯一红 `tests/unit/test_w11_rc_manifest.py::test_git_rev_parse_returns_none_outside_repo`（`assert '2850eca…' is None`） | 我首轮把 `--basetemp` 设在**仓库内** `D:/DBGuaredian/.qa_tmp_base`；该用例断言「在**非 git 仓库**目录上 `git_rev_parse()` 返回 `None`」，tmp_path 落在仓库内时 `git rev-parse` 必然成功 | **我的取证副作用，不是 W11 实现缺陷**。改用仓库外 basetemp 后 `1803 passed / 0 failed`。临时目录与中转文件已删除，`git status --short` 复归 121 |
| **M-2** | W12 七件首轮 `106 passed, 14 errors` | 复用已存在的 `--basetemp` 导致 tmp_path 冲突 | 改全新 basetemp 后 `120 passed` |

> M-1 的落点恰是 **W11 的测试文件**，若不披露会被误读为「W11 有红项」。此处明确：**W11 无红项**，红是我自己的取证方式造成的，且改法后同命令全绿。

---

> **写入后复核**：4 份评审件全部落盘后，我复跑了 `tests/unit` → `1803 passed / exit 0`、W12 七件 → `120 passed / exit 0`，并复核 `scan_claims()` 仍 `ok=True` / `violations=()`（本类评审件不在 `STRICT_SCAN_FILES` 的 11 件内）。结论：新增评审件**不改变**任何既有校验结果，本件的取证数字在写入前后一致。

## ④ 未闭合项

> 标记口径：`NOT_SUPPORTED` / `PARTIAL` / `UNKNOWN`。环境阻塞**不作为放行条件**。

### 4.1 对应 `docs/w12/known-limitations.json` 的项（只读引用，未改）

| `item_id` | marker | 与 W11 的关系 | 一致性核对 |
|---|---|---|---|
| `ENV-PG5432` | `NOT_SUPPORTED` | 直接导致 AC-16 的 PG 段 `NOT_EXECUTED` | 一致（`rc-freeze.md`/`known-limitations` 双向） |
| `ENV-1045` | `NOT_SUPPORTED` | 物理库级恢复不可用 → `restore_mode="LOGICAL_SHADOW"`、`physical_restore="NOT_SUPPORTED"` | 一致（`backup-restore-report.md` 只读核对命中） |
| `ENV-RESIDUE` | `PARTIAL` | 只追加、不清理；所有计数按 `task_id`/`run_id` 过滤 | 一致 |
| `ENV-NV1` | `UNKNOWN` | `db_image_digest="UNKNOWN"`、`container_runtime_status="NOT_VERIFIED"` | 一致（我实读 `rc_manifest.json`） |
| `ENV-MODEL` | `UNKNOWN` | `model_version=None` + `ISSUE-MODEL-VERSION` | 一致（我实读 `rc_manifest.json`） |
| `DECISION-TECH-PREVIEW` | `PARTIAL` | **本件实算值即此**：`TECH_PREVIEW_ONLY` | 一致 |
| `KD-W11-01` | `PARTIAL` | `audit_chain` 桶命名与设计 §8.2 / W08 语义不符 | 一致（仍在册） |
| `KD-W11-02` | `PARTIAL` | 快照未含 `approval_consumptions` / `step_prepared` | 一致（仍在册） |
| `KD-W11-03` | `PARTIAL` | G1–G3 哨兵归属错位 | 一致（仍在册） |
| `DOC-PARTIAL` | `PARTIAL` | `docs/migrations.md` 止于 007 | 一致 |

### 4.2 W11 自身遗留（逐条标标记）

| 编号 | 内容 | 标记 | 来源 |
|---|---|---|---|
| U-1 | **交付物未提交**（完成定义未满足） | `PARTIAL` | 本件 §② G-4 |
| U-2 | AC-03：29 条负面集中 23 条（G1–G3）为哨兵空真，「拒绝不改数据」仅 6/29 非空验证（`KD-W11-03`） | `PARTIAL` | `05-…md:66`（2-L）；`backup-restore-report.md:124-133` |
| U-3 | AC-12：4 类快照 3 类为 `0 == 0`；`approval_consumptions` / `step_prepared` 未纳入（`KD-W11-01`/`KD-W11-02`）；`audit_chain` 逻辑类名映射到 `step_observed` | `PARTIAL` | 同上 |
| U-4 | **长稳未实跑**：`status=PLANNED`、`measured_*=None`、`p50/p95=None`（我实算复核） | `NOT_SUPPORTED` | 本件 §3.2；`05-…md:125` |
| U-5 | 端到端回归 PG 段 `NOT_EXECUTED`（`ENV-PG5432`）→ 「双引擎」只完成 MySQL 侧 | `PARTIAL` | 本件 §② G-3 |
| U-6 | `L-6`：`LimitationRemoved` 在 pydantic 校验器内抛出后被包成 `ValidationError`，`code="LIMITATION_REMOVED"` 无法按异常类型捕获 | `PARTIAL` | `05-…md:104` |
| U-7 | `IN_PROCESS_BYPASS`-1：`object.__setattr__` 可改写 frozen `PublishDecision.decision`（进程内恶意绕过，**不在威胁模型内**） | `NOT_SUPPORTED`（威胁模型外，按裁定只登记） | `05-…md` §11.2、§10 表 |
| U-8 | `CONTENT_DEPTH_LIMIT`-1：`_frozen_evidence_available` 只校验 `definitions` 长度 14，不校验元素 → 14 个空对象仍判 `True` | `PARTIAL` | `05-…md` §11.1 |
| U-9 | `ENV-PY314-AST-CRASH`：`tests/unit` 曾 4 次尝试中 2 次 `exit 139`（`ast.py copy_location`） | `NOT_SUPPORTED`（环境层） | `05-…md` §11.5 |
| U-10 | `ENV-RESIDUE` 精确行数本轮**未复核**（不做 `COUNT`，避免新增连库开销；只登记「增长」这一事实方向） | `UNKNOWN` | 本件 §3.3 |
| U-11 | 跑 pytest 时若 tmp 目录设在仓库内，`test_w11_rc_manifest.py::test_git_rev_parse_returns_none_outside_repo` 恒红 | `PARTIAL`（运维提示） | 本件 §3.4 M-1 |

---

## ⑤ 放行条件（每条可判定、可关闭）

| # | 条件 | 关闭判据（可机器核对） | 责任 |
|---|---|---|---|
| **C-W11-1** | 提交一次 commit，把 W11 交付物（`core/assurance/**`、`benchmarks/w11/**`、`tests/**/test_w11_*`、`docs/w11/**`）入库并记录 `commit` 哈希 | `git rev-parse HEAD != 2850eca…` 且 `git status --short` 对上述路径为空 | 主理人 / 工程师 |
| **C-W11-2** | `KD-W11-01` + `KD-W11-02` 关闭：备份桶补 `approval_consumptions` + `step_prepared`（覆盖设计 §8.2 的 5 张表），且 `audit_chain` 逻辑类名改回 `task_events`（哈希链本体） | `BACKUP_OBJECT_TABLES` 覆盖 5 张表；AC-12 四类计数**全部非空**（不再有 `0 == 0`）；报告内不再出现把 `step_observed` 标为 `audit_chain` 的表述 | 工程师 |
| **C-W11-3** | `KD-W11-03` 关闭：G1–G3 的 23 条负面集用例改挂真实对象（去掉哨兵归属的空真） | `test_w11_security_negative.py` 中 G1–G3 的 `before/after` 计数**非全零**且逐条相等；用例数仍 == 29 | 工程师 |
| **C-W11-4** | 长稳实跑：1 小时长稳并回填实测值 | `SoakReport.status == "COMPLETED"`、`measured_sample_size > 0`、`measured_duration_s > 0`、`p50_ms`/`p95_ms` 非 `None` | 工程师 |
| **C-W11-5** | 端到端回归的 PG 段：`ENV-PG5432` 解阻（由主理人拍板）后跑 S1 的 PG 分支 | `test_w11_e2e_regression.py` 的 S1 中 PG 段 `status != NOT_EXECUTED` | 主理人 / 工程师 |
| **C-W11-6** | `U-8`（`CONTENT_DEPTH_LIMIT`）：`_frozen_evidence_available` 增加元素级校验（14 项均须含指标名且非空） | 14 个空对象 → `False`（当前为 `True`） | 工程师 |

> `ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL` **不列入**放行条件（PRD `:162`/`:163`：环境项 `NOT_EXECUTED`，不判 FAIL、不冒充通过、不修环境）。C-W11-5 是「解阻后补测」的后续迭代条件。
> **`RPO ≤ 15 min` / `RTO ≤ 4 h` 仍为「建议目标，未验证 SLA」**（PRD `:173` Q3 明确不作门禁硬阈值；我实读报告 `sla_claim=False`、`measured_*=null`）。

---

## ⑥ 口径自查（禁用表述）

| 禁用表述 | 本件是否出现 | 说明 |
|---|---|---|
| 「W11 门禁通过 / 已收口」 | **否** | 结论为「`有条件放行`｜门禁通过：**否**」，附 6 条未关闭条件 |
| 「容器网络隔离已验证」 | **否** | 只写 `container_runtime_status="NOT_VERIFIED"`、`db_image_digest="UNKNOWN"` |
| 「环境已清理 / 已可复现清零」 | **否** | `ENV-RESIDUE` 只追加不清理 |
| 「QA 独立实跑通过」 | **否** | 逐条给原文汇总行 + exit code；并披露 M-1（W11 测试文件上的红是我自己的 basetemp 造成） |
| 「回归全绿」 | **否** | 只给原文汇总行；如实登记 `ENV-PY314-AST-CRASH` 曾 `exit 139`（U-9） |
| 「全部可自动恢复」 | **否** | 六类边界 `AUTO 3 / MANUAL 3`（`AUDIT_WRITER`/`DB_DISCONNECT`/`WORKER_CRASH` = MANUAL），照引前序件 |
| 「已验证 SLA」 | **否** | 写「建议目标，未验证 SLA」 |
| 「安全红线未过」（作为本件结论） | **否** | 明确区分：**红线失败 0 条**（`failed_test_ids=()`），`TECH_PREVIEW_ONLY` 由 **5 项环境阻塞**触发 |
| 「Alpha 就绪 / Alpha 通过」 | **否** | 只写 `TECH_PREVIEW_ONLY`，发布档位见 `docs/w12/07-w12-gate-review.md` |

---

## ⑦ 读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅**本文件 `docs/w11/06-w11-gate-review.md` |
| 修改 `.py` / SQL / 配置 / 既有 docs | **0**（`docs/w11/` 既有 7 件全部未触碰，含 `limitations.json` 与 `rc-freeze.md`；`docs/w12/known-limitations.*` 未触碰） |
| 手工 `INSERT/UPDATE/DELETE/TRUNCATE/ALTER` / rollback | **0**（未碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`） |
| 新增测试用例 | **0** |
| `git commit / add / checkout / reset` | **0** |
| 无路径全量 pytest / `--timeout=` | **0** |
| 临时产物 | 全部在 `%LOCALAPPDATA%/Temp/`（仓库外）；我一度在仓库内建的 `.qa_tmp_base` 与 `runs/.qa_unit_full.txt` 已删除，`git status --short` 复归 121 |

---

*严过关（Edward）｜QA 工程师｜`software-dbguardian-w09-closeout2`｜时点锚 `HEAD = 2850eca`，全部改动未提交*
