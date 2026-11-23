# W09 → W10 门禁评审（报告、脱敏与记忆）

| 项 | 值 |
|---|---|
| 评审件编号 | `docs/w09/15-w09-gate-review.md` |
| 评审人 | 严过关（Edward）· QA 工程师｜团队 `software-dbguardian-w09-closeout2` |
| 评审性质 | **门禁评审**（不是定向验收；与前序 `12-w09-qa-closeout.md`、`14-l1-l2-qa-closeout.md` 的「定向验收收口」性质不同） |
| 时点锚 | `HEAD = 2850eca326850eec491e9159ec7033a85807d080`；全部改动**未提交**（`git diff --name-only` = 25、`git status --short` = 121 = 25 `M` + 96 `??`） |
| 解释器 / 现场 | `.venv/Scripts/python.exe` = Python 3.14.7；`D:/DBGuaredian`；Windows / Git Bash |
| 判据来源 | `docs/w09/01-prd.md` **§6 `:155-167`**（5 条门禁 + 未通过处置 + 完成定义）、§5 AC 表 `:129-151` |
| 上游口径 | W07「**有条件放行；门禁通过：否**」；W08「**有条件放行**」（`docs/w08/07-gate-review.md:10` GR-0）；W09 门禁**此前未评**，本件为首次正式评审 |
| 唯一写入 | **本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs（含 `docs/w09/02-design.md`、`03-task-cards.md`、`06-qa-report.md`、`10-lead-rulings.md`）；未改 `docs/w12/known-limitations.*`；未改数据库；未 `git commit/add/checkout/reset` |

---

## ① 执行摘要

**门禁结论：`有条件放行`｜门禁通过：否。**

判据一句话：PRD §6 的五条门禁判据中 **4 条满足、1 条部分满足**（G-1 的 AC-06 只在闸门/装配层与 pipeline 集成层取证），且 PRD §6 `:167` 的**完成定义「交付物已提交到仓库」不满足**（`HEAD` 仍为 `2850eca`、25 个 tracked 修改 + 96 个未跟踪文件、零 commit）——存在未满足项，故不能判「通过」。

**为什么不是「未通过」**：五条门禁判据无一条被判 `不满足`；全部 21 条 AC 均有已执行证据；W08 点名承接项 AC-20 已绿。缺的是**流程闭环（提交锚点）**与**一条 AC 的取证层级**，两者均可判定、可关闭。

**为什么不能判「通过」**：
1. `docs/w09/01-prd.md:167` 明文要求「交付物已提交到仓库」，实测为零提交；
2. `AC-06`（模型输入前强制闸门）在真实 Agent 业务编排链路无端到端用例，只有闸门③④单元级 + pipeline 集成级；
3. `KD-W09-03`（内部通道 PII 未经 `REDACTED` 改写）仍在册未裁定。

**「定向验收通过」≠「门禁通过」**：前序两件收口报告（`12-`/`14-`）判的是「T03–T06 / L1+L2 已实现并通过定向验收」，并**明确写了「W09 门禁未评」**（`12-w09-qa-closeout.md:13,357`、`14-l1-l2-qa-closeout.md:13`）。本件是首次门禁评审，不继承其结论。

---

## ② 门禁判据逐条对账

> 逐条取自 `docs/w09/01-prd.md` §6（`:155-160`），另加 `:167` 的完成定义。

### G-1｜敏感数据门禁通过（`01-prd.md:156`）— **部分满足**

> 原文：AC-01～AC-08、AC-19 全绿（脱敏在进入模型上下文与 Evidence 持久化前执行；导出/报告/训练集再次扫描；无法可靠脱敏被隔离或阻断）。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-01 | 12 类敏感样本全命中 | `tests/unit/test_w09_redaction_engine.py` | 含在 `tests/unit` 1803 全量内（E-1），0 failed | 满足 |
| AC-02 | 脱敏确定性（5 轮 count/version/hash 集合大小 ==1） | 同上 | 同上 | 满足 |
| AC-03 | 不可靠脱敏不冒充（`!= REDACTED`） | 同上 | 同上 | 满足 |
| AC-04 | 哈希口径 + `extra="forbid"` | 同上 | 同上 | 满足 |
| AC-05 | 持久化前闸门（`BLOCKED` → 0 新增行） | `tests/integration/test_w09_redaction_pipeline.py` | **E-12 `4 passed`（真实库，非 skip）** | 满足 |
| AC-06 | 模型输入前强制闸门 + `REDACTION_BLOCKED` | 同上 + `test_w09_redaction_wiring.py` | E-12 `4 passed`（pipeline 层）；`wiring` 30 例在 E-1 内 | **部分满足**（见下） |
| AC-07 | 导出/训练集再扫描 | 同上 + `test_w09_report_export.py` + `test_w09_redaction_samples.py` | 均在 E-1 内 0 failed | 满足 |
| AC-08 | 证据行与事件 `redaction_version` 一致 | `test_w09_redaction_pipeline.py` | **E-12 `4 passed`（真实库）** | 满足 |
| AC-19 | 五类样本三断言 + 版本非空 | `tests/unit/test_w09_redaction_samples.py` | 在 E-1 内 0 failed | 满足 |

**AC-06 判「部分满足」的依据**：AC-06 的文字要求「未脱敏载荷**渲染前**被拒」，当前证据是①闸门③④的单元级调用计数（`test_w09_redaction_wiring.py`）、②`test_w09_redaction_pipeline.py` 的 pipeline 集成（4 passed，真实库）。**没有**一条用例走真实 Agent 编排（`core/agent/loop.py` → 模型 `complete` → `user_content` 渲染）的完整业务链路。前序收口件亦同口径登记为「业务侧端到端未验证」（`12-w09-qa-closeout.md:345` ⑧-9）。

**G-1 结论**：`部分满足` —— 8/9 条 AC 满足，AC-06 取证层级不足。

### G-2｜报告证据引用有效（`01-prd.md:157`）— **满足**

> 原文：AC-09～AC-12 全绿（每个事实结论引用有效 `evidence_id`，含支持证据、反证、未知项、执行状态、验证状态和适用范围）。

| AC | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|
| AC-09 | `tests/unit/test_w09_report_schema.py` | 在 E-1 内 0 failed | 满足 |
| AC-10 | 同上 | 在 E-1 内 0 failed | 满足 |
| AC-11 | 同上 | 在 E-1 内 0 failed | 满足 |
| AC-12 | `tests/integration/test_w09_report_evidence_ref.py` | 在 E-1 内 0 failed（组件级断言） | 满足 |

**附加独立取证（关掉前序收口件 ⑧-8 的遗留）**：前序 `12-w09-qa-closeout.md:344` 登记「`ReportBuilder` / `build_report` 仅出现在 `core/report/`，`apps/`、`workers/` 无任何引用 → 未接入生产装配链路」。**该事实现已不成立**，我用全仓静态扫描取证：

```
HITS: workers\report_assembly.py: build_report, ReportBuilder
      workers\stack.py:          build_report
      core\report\builder.py, core\report\validate.py: ReportBuilder
```

`apps/` 仍无引用（报告由 Worker 侧发布，不经 API 装配），这与 L2 设计（`docs/w09/13-l1-l2-design.md` §4）一致。且我实跑了真实库发布链路：

```
E-13  tests/integration/test_w09_report_publish_chain.py → 2 passed in 3.04s  exit 0
```

→ **AC-12 的生产装配链路已闭合**，不构成 G-2 的缺口。

**G-2 结论**：`满足`。

### G-3｜知识可审核、过期、撤销和冲突处理（`01-prd.md:158`）— **满足**

| AC | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|
| AC-13 | `tests/unit/test_w09_memory_models.py` | 在 E-1 内 0 failed | 满足 |
| AC-14 | 同上 + `tests/integration/test_w09_conflict_resolver.py` | unit 在 E-1；集成件在 1803 之外的 integration 层，前序收口件 `3 passed` | 满足 |
| AC-15 | `tests/unit/test_w09_memory_models.py`（`inspect.signature` 无 `tool_registry/gateway/dsn/rw_dsn`） | 在 E-1 内 0 failed | 满足 |

**G-3 结论**：`满足`。

### G-4｜降级不绕过策略（`01-prd.md:159`）— **满足**

| AC | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|
| AC-16 | `tests/unit/test_w09_model_degradation.py` | 在 E-1 内 0 failed | 满足 |
| AC-17 | 同上（`retry_count == 1`、`adapter.complete` 调用 2 次） | 在 E-1 内 0 failed | 满足 |
| AC-18 | `tests/integration/test_w09_degrade_audit.py` | **E-11 `2 passed in 1.63s` exit 0（真实库）** | 满足 |
| AC-21 | `tests/unit/test_write_tool_gate.py` + `test_tools_contracts.py`（既存） | **E-8 `11 passed`（= 6 + 5）exit 0** | 满足 |

**G-4 结论**：`满足`。写面与引擎未回退（PG 工具集 7 个全 `read`、write 工具仍 `LAB+mysql` 单一）。

### G-5｜W08 点名承接项：`sweep()` 真实扫描总数可观测（`01-prd.md:160`）— **满足**

> 原文：AC-20 全绿；未绿则 `docs/w08/` 的 W8→W9 交接件不得标注该项已完成（`docs/w08/08-drill-report.md:76-78` §1.3 第 4 条）。

| AC | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|
| AC-20 | `tests/unit/test_w09_sweep_scanned.py` + `tests/unit/test_worker_scheduler.py`（既存扩展） | **E-7 `10 passed in 0.08s`** + **E-6 `35 passed in 0.25s`**，均 exit 0 | 满足 |

**G-5 结论**：`满足`。`scanned_total` 与 `conclusions` 已分离计数，W08 W8→W9 交接件可标注该项完成。

### G-6｜本周完成定义（`01-prd.md:167`）— **不满足**

> 原文：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

| 子条件 | 实测 | 判定 |
|---|---|---|
| 交付物已**提交到仓库** | `git rev-parse HEAD` = `2850eca…`（与 W08 收尾同一提交）；`git diff --name-only` = 25、`git status --short` = 121（25 `M` + 96 `??`）；**零 commit** | **不满足** |
| 关键测试有自动化结果/可复现记录 | E-1…E-17 共 17 条串行实跑命令全部 exit 0，原文汇总行见 ③ | 满足 |
| 未通过项已登记并说明影响 | `12-w09-qa-closeout.md` §⑧ 12 条 + `14-l1-l2-qa-closeout.md` 遗留段 | 满足 |

**G-6 结论**：`不满足`（唯一原因是「未提交」）。

### 小结表

| # | 判据（PRD 行号） | 判定 |
|---|---|---|
| G-1 | 敏感数据门禁通过（`:156`） | **部分满足** |
| G-2 | 报告证据引用有效（`:157`） | 满足 |
| G-3 | 知识可审核/过期/撤销/冲突（`:158`） | 满足 |
| G-4 | 降级不绕过策略（`:159`） | 满足 |
| G-5 | `sweep()` 扫描总数可观测（`:160`） | 满足 |
| G-6 | 完成定义：交付物已提交到仓库（`:167`） | **不满足** |

---

## ③ 独立取证

### 3.1 我自己跑的数字（不采信任务书数字、不采信前序收口件数字）

纪律：解释器 `.venv/Scripts/python.exe`；每条均带 `-o addopts="" -q -p no:cacheprovider`；**全程串行，任意两条 pytest 不并发**；需 DB 的加外层 `timeout 180`；**未使用 `--timeout=`**；每次运行使用**仓库外全新** `--basetemp`（见 §3.3 M-1/M-2 的方法学说明）。

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| E-1 | `pytest tests/unit`（全量） | `1803 passed in 24.87s` | 0 |
| E-3 | `pytest tests/unit/test_worker_layering.py` | `10 passed in 0.06s` | 0 |
| E-4 | `pytest tests/unit/test_worker_isolation.py` | `2 passed in 0.04s` | 0 |
| E-5 | `pytest tests/unit/test_worker_runner.py` | `38 passed in 0.50s` | 0 |
| E-6 | `pytest tests/unit/test_worker_scheduler.py` | `35 passed in 0.25s` | 0 |
| E-7 | `pytest tests/unit/test_w09_sweep_scanned.py` | `10 passed in 0.08s` | 0 |
| E-8 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py` | `11 passed in 0.18s` | 0 |
| E-9 | `pytest tests/unit/test_verification_perf.py` | `10 passed in 0.11s` | 0 |
| E-10 | `pytest tests/unit/test_baseline_manifest.py` | `9 passed in 0.07s` | 0 |
| E-11 | `timeout 180 pytest tests/integration/test_w09_degrade_audit.py` | `2 passed in 1.63s` | 0 |
| E-12 | `timeout 180 pytest tests/integration/test_w09_redaction_pipeline.py` | `4 passed in 2.58s` | 0 |
| E-13 | `timeout 180 pytest tests/integration/test_w09_report_publish_chain.py` | `2 passed in 3.04s` | 0 |
| E-14 | `timeout 180 pytest tests/integration/test_w10_oracle_timing.py` | `2 passed in 2.02s` | 0 |
| E-15 | `timeout 180 pytest tests/integration/test_w10_runner_replay.py` | `8 passed in 0.30s` | 0 |
| E-16 | `timeout 180 pytest tests/integration/test_w11_security_negative.py` | `32 passed in 2.59s` | 0 |
| E-17 | `timeout 180 pytest tests/integration/test_w11_fault_injection.py` | `16 passed in 3.86s` | 0 |
| E-2 | W12 七件（`test_w12_*` 单元 3 + 集成 4） | `120 passed in 38.13s` | 0 |

**与任务书数字比对**：任务书期望 `tests/unit` = 1803 passed → **一致**（E-1）。任务书期望 W12 七件 = 120 passed → **一致**（E-2）。E-3…E-10 的 8 组不变量数字与任务书括号内的期望值**逐项一致**（10 / 2 / 38 / 35 / 10 / 11(=6+5) / 10 / 9）。

### 3.2 与前序 QA 收口件的数字对账

| 项 | 前序收口件 | 本轮实测 | 是否一致 | 说明 |
|---|---|---|---|---|
| `tests/unit`（M08 轮） | `1294 passed`（`:113`） | 1803 | 数字不同 | 时点差异：1294 →（L1/L2 +42）1336 →（W10 +246）1582 →（W11 R1 +139 / R2 +10）1731 →（W12 R1 +65 / R2 +5）1801 →（收尾轮 KD-W12-02 回归 +2）**1803**；**全部为新增用例，无回归** |
| `tests/unit`（L1/L2 轮） | `1336 passed`（`:129`） | 1803 | 同上 | 同上 |
| 四条 W07 Worker 载体 AC | 10 / 2 / 38 / 1（`:304-310`） | E-3 `10` / E-4 `2` / E-5 `38` | **一致** | 第 4 条 `test_worker_process.py::…_repair_once` 本轮未跑（单条 ~26–45 s，非本门禁判据） |
| `test_worker_scheduler.py` | 35（`:96`） | E-6 `35` | **一致** | — |
| `test_w09_sweep_scanned.py` | 10（`:63`） | E-7 `10` | **一致** | — |
| `write_tool_gate` + `tools_contracts` | 6 + 5（`:98-104`） | E-8 `11` | **一致** | — |
| `test_w09_report_publish_chain.py` | 2（`:109`） | E-13 `2` | **一致** | — |

**两处前序收口件已过期的事实（必须指出）**：

| 编号 | 前序件的表述 | 本轮实测 | 结论 |
|---|---|---|---|
| X-1 | `12-w09-qa-closeout.md:344` ⑧-8「`ReportBuilder`/`build_report` **未接入** API/Worker 生产装配链路」 | `workers/report_assembly.py`、`workers/stack.py` 均有引用；E-13 真实库发布链路 `2 passed` | **前序件已过期**（L2 已补齐）。与 `14-l1-l2-qa-closeout.md` 一致，与 `12-` 件不一致 |
| X-2 | `12-w09-qa-closeout.md:346` ⑧-10「KD-5 两份 W07 冻结件仍缺（`05-freeze.md`、`06-conformance-audit.md`）」 | 两文件**已存在**：`docs/w07/05-freeze.md`（16 556 B）、`docs/w07/06-conformance-audit.md`（18 824 B），mtime 均为 2026-11-17 23:13；但 `git status` 为 `??`（**未跟踪**） | 文件已补、**未入库**。`docs/w07/07-gate-review.md`（冻结件）仍按原样记载缺失，按 R18-01 纪律不改 |

### 3.3 取证方法学：两条**我自己的**取证副作用（如实披露）

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| **M-1** | 首轮 `pytest tests/unit` 结果为 `1 failed, 1802 passed`，唯一红为 `tests/unit/test_w11_rc_manifest.py::test_git_rev_parse_returns_none_outside_repo`（`assert '2850eca…' is None`） | 我首轮把 `--basetemp` 指到了**仓库内** `D:/DBGuaredian/.qa_tmp_base`，而该用例断言「在**非 git 仓库**目录上 `git_rev_parse()` 返回 `None`」；tmp_path 落在仓库内时 `git rev-parse` 必然成功 | **取证方法副作用，不是实现缺陷**。改用仓库外 basetemp 后 `1803 passed / 0 failed`（E-1）。临时目录与中转文件已删除，`git status --short` 复归 121 |
| **M-2** | W12 七件首轮出现 `106 passed, 14 errors` | 复用了**已存在**的 `--basetemp` 目录（被前一次运行占用），不是用例缺陷 | 改用**每次全新** basetemp 后 `120 passed`（E-2） |

> 披露理由：M-1 的失败点是一个「对运行位置敏感」的用例（`test_git_rev_parse_returns_none_outside_repo`）。它不构成本周门禁缺口，但**任何把 pytest tmp 目录设在仓库内的 CI 配置都会让它恒红**，属可执行的运维提示，登记为本件 §4.2 的 `U-9`。

> **写入后复核**：4 份评审件全部落盘后，我复跑了 `tests/unit` → `1803 passed / exit 0`、W12 七件 → `120 passed / exit 0`，并复核 `scan_claims()` 仍 `ok=True` / `violations=()`（本类评审件不在 `STRICT_SCAN_FILES` 的 11 件内）。结论：新增评审件**不改变**任何既有校验结果，本件的取证数字在写入前后一致。

### 3.4 与任务书的事实勘误（3 条）

| # | 任务书表述 | 实测 | 影响 |
|---|---|---|---|
| F-1 | `git diff --name-only = 22`、`git status --short = 118` | **25** / **121**（25 `M` + 96 `??`） | 无判定影响；仅登记 |
| F-2 | 「W10 `test_w10_report_publish_chain.py`(2)」 | 该文件**不存在**；实际为 `tests/integration/test_w09_report_publish_chain.py`（2），已按 E-13 跑 | 无判定影响 |
| F-3 | 命令写作 `.venv/Scripts/python.exe -o addopts="" -q …` | `python -o` 是解释器选项（报 `Unknown option: -o`），须写 `python -m pytest -o addopts="" …` | 无判定影响；本件全部命令均用 `-m pytest` 形式 |

---

## ④ 未闭合项

> 标记口径：`NOT_SUPPORTED` = 该能力/证据不支持；`PARTIAL` = 部分成立；`UNKNOWN` = 未取值/未测。
> **环境阻塞不作为放行条件**（按 W09/W10/W11 PRD 一致口径：环境项如实登记，**不判 FAIL、不算实现失败**）。

### 4.1 对应 `docs/w12/known-limitations.json` 的项（只读引用，未改）

| `item_id` | marker | 与 W09 的关系 | 我的一致性核对 |
|---|---|---|---|
| `ENV-PG5432` | `NOT_SUPPORTED` | W09 不依赖 PG，本周用例如实为 MySQL/离线 | 一致（E-11/E-12/E-13 均连 MySQL 控制库，非 PG） |
| `ENV-1045` | `NOT_SUPPORTED` | 管理员凭据不可用 → 清理一律降级为 warning | 一致（`12-w09-qa-closeout.md:338`） |
| `ENV-RESIDUE` | `PARTIAL` | 控制库残留未清理、随时间增长 | 一致（前序件 101→102；W11 复测 79 313 行 `task_events`） |
| `ENV-NV1` | `UNKNOWN` | 容器运行态 `NOT_VERIFIED` | 一致 |
| `ENV-MODEL` | `UNKNOWN` | 模型环境缺失 → W09 T05 降级路径的**真实模型侧**行为未实测（降级路径本身已测） | 一致 |
| `DOC-PARTIAL` | `PARTIAL` | `docs/migrations.md` 止于 007，但 W09 交付 009/010/011 三支迁移 | 一致（R18-01 明确不补） |

### 4.2 W09 自身遗留（逐条标标记）

| 编号 | 内容 | 标记 | 来源 |
|---|---|---|---|
| U-1 | **交付物未提交**（完成定义未满足） | `PARTIAL` | 本件 §② G-6 |
| U-2 | AC-06「真实 Agent 编排链路」端到端未验证 | `PARTIAL` | 本件 §② G-1；`12-…md:345` ⑧-9 |
| U-3 | `KD-W09-03`：`StepPrepared.request` 与 `TrajectoryRecorder`（含 `usage`）不接管 `REDACTED`/`REVIEW_REQUIRED`，非敏感键名下的 PII 可能原样落盘 | `PARTIAL`（4/4 一致，仍在册未裁定） | `12-…md:342` ⑧-6；`docs/w09/02-design.md:67` |
| U-4 | `usage` 键名命中 R12 → 含 token 计数的训练样本恒 `REDACTED` | `PARTIAL`（口径已锁定，非缺陷） | `12-…md:343` ⑧-7 |
| U-5 | 迁移 009/010/011 的**回滚脚本未执行**（含 `DROP`，须 `DG_ADMIN_DSN` 人工执行） | `NOT_SUPPORTED` | `12-…md:340` ⑧-4 |
| U-6 | W07 两份冻结件已落盘但**未跟踪**（X-2） | `PARTIAL` | 本件 §3.2 |
| U-7 | `test_step_prepared.py -k "sweep or reconcile"` 在 `12-…md` 轮为 `2 failed / 2 errors`（E-2 + E-3 环境项）；L1 轮已改任务级口径 → `2 passed, 18 deselected`、整文件 `20 passed / 0 errors` | `NOT_SUPPORTED`（环境项，非实现缺陷） | `12-…md` §④；`14-…md:40,46` |
| U-8 | L1 的 2 条内联断言左操作数增加 `task_id=` 实参（主理人 `E-1` 裁定「接受偏差」） | `PARTIAL`（已裁定，非放宽） | `14-…md` §3.4/§3.5 |
| U-9 | 跑 pytest 时若把 tmp 目录设在仓库内，`test_git_rev_parse_returns_none_outside_repo` 恒红（M-1） | `PARTIAL`（运维提示） | 本件 §3.3 |

---

## ⑤ 放行条件（每条可判定、可关闭）

| # | 条件 | 关闭判据（可机器核对） | 责任 |
|---|---|---|---|
| **C-W09-1** | 提交一次 commit，把 W09 交付物（`core/report/**`、`core/redaction/**`、`workers/report_assembly.py`、`tests/**/test_w09_*`、`docs/w09/**`、`database/migrations/009–011` + rollback）入库并记录 `commit` 哈希 | `git rev-parse HEAD != 2850eca…` 且 `git status --short` 对上述路径为空 | 主理人 / 工程师 |
| **C-W09-2** | AC-06 补一条**真实 Agent 编排链路**端到端用例（未脱敏 → 渲染前被拒 + `task_events` 含 `REDACTION_BLOCKED`），或由主理人书面裁定把 AC-06 的取证层级降为「闸门/装配层 + pipeline 集成层」并登记 | 新增用例绿 **或** 裁定台账新增一条可引用裁定 | 工程师 / 主理人 |
| **C-W09-3** | `KD-W09-03` 处置：判定「接受为已知限制（挂后续周）」或「实现补 `REDACTED`/`REVIEW_REQUIRED` 改写」 | 裁定台账落一条 **或** 对应实现 + 用例绿 | 主理人 |
| **C-W09-4** | 把 `docs/w07/05-freeze.md`、`docs/w07/06-conformance-audit.md` 纳入提交（X-2 / KD-5） | `git ls-files docs/w07/05-freeze.md docs/w07/06-conformance-audit.md` 输出非空 | 工程师 |

> `ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL` **不列入**放行条件：按 `docs/w09/01-prd.md:165` 与后续两周一致口径，环境阻塞如实登记即可，不判 FAIL、不算实现失败、不因本周工作而消失。

---

## ⑥ 口径自查（禁用表述）

| 禁用表述 | 本件是否出现 | 说明 |
|---|---|---|
| 「W09 已收口 / 门禁通过 / 放行」作为**肯定**结论 | **否** | 本件结论为「`有条件放行`｜门禁通过：**否**」，且附 4 条未关闭条件 |
| 「容器网络隔离已验证」 | **否** | 统一写「配置层已实测 / 运行态 `NOT_VERIFIED`」 |
| 「环境已清理 / 已可复现清零」 | **否** | 只写 `ENV-RESIDUE` 未清理、随时间增长 |
| 「QA 独立实跑通过」 | **否** | 逐条区分「我实跑」与「引用前序件」，并披露 M-1/M-2 两条我自己的取证副作用 |
| 「回归全绿」 | **否** | 只给原文汇总行 + exit code |
| 「W07 已通过」/「W08 已通过」 | **否** | W07 逐字引「有条件放行；门禁通过：否」，W08 引「有条件放行」 |
| 「Alpha 通过 / Alpha 就绪」 | **否** | 本件不涉及发布档位（见 `docs/w12/07-w12-gate-review.md`） |

---

## ⑦ 读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅**本文件 `docs/w09/15-w09-gate-review.md` |
| 修改 `.py` / SQL / 配置 / 既有 docs | **0**（`docs/w09/02-design.md`、`03-task-cards.md`、`06-qa-report.md`、`10-lead-rulings.md` 均未触碰；`docs/w12/known-limitations.*` 未触碰） |
| 手工 `INSERT/UPDATE/DELETE/TRUNCATE/ALTER` / rollback | **0**（未碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl` / 账号） |
| 新增测试用例 | **0**（本任务只评审，不补测） |
| `git commit / add / checkout / reset` | **0** |
| 无路径全量 pytest / `--timeout=` | **0**（全部为定向路径；需 DB 的用外层 `timeout`） |
| 我创建的临时产物 | `D:/DBGuaredian/.qa_tmp_base`（已删除）、`runs/.qa_unit_full.txt`（已删除，且 `runs/` 本在 `.gitignore`）、其余脚本与日志全部在 `%LOCALAPPDATA%/Temp/`（仓库外） |

---

*严过关（Edward）｜QA 工程师｜`software-dbguardian-w09-closeout2`｜时点锚 `HEAD = 2850eca`，全部改动未提交*
