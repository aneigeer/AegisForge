# W10 → W11 门禁评审（Benchmark 与应用观测）

| 项 | 值 |
|---|---|
| 评审件编号 | `docs/w10/05-w10-gate-review.md` |
| 评审人 | 严过关（Edward）· QA 工程师｜团队 `software-dbguardian-w09-closeout2` |
| 评审性质 | **门禁评审**（不是定向验收；`docs/w10/04-w10-qa-closeout.md` 给的是「定向验收通过、W10 门禁未评」，性质不同） |
| 时点锚 | `HEAD = 2850eca326850eec491e9159ec7033a85807d080`；全部改动**未提交**（`git diff --name-only` = 25、`git status --short` = 121 = 25 `M` + 96 `??`） |
| 解释器 / 现场 | `.venv/Scripts/python.exe` = Python 3.14.7；`D:/DBGuaredian` |
| 判据来源 | `docs/w10/01-prd.md` **§6 `:152-167`**（4 条门禁 + 未通过处置 + 完成定义）、§5 AC 表 `:129-150` |
| 上游口径 | W07「**有条件放行；门禁通过：否**」；W08「**有条件放行**」；W09「**有条件放行｜门禁通过：否**」（本轮同批出件 `docs/w09/15-w09-gate-review.md`）；W10 门禁此前未评，本件为首次正式评审 |
| 唯一写入 | **本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs（含 `docs/w10/01-prd.md`、`02-design.md`、`03-task-cards.md`、`04-w10-qa-closeout.md`、`metrics-definition.md`）；未改 `docs/w12/known-limitations.*`；未改数据库；未 `git commit/add/checkout/reset` |

---

## ① 执行摘要

**门禁结论：`有条件放行`｜门禁通过：否。**

判据一句话：PRD §6 的四条门禁判据中 **3 条满足、1 条部分满足**（G-3「Baseline 报告完成」的归因能力只在 `total_cases = 0` 的空样本上取过证），且 `docs/w10/01-prd.md:167` 的**完成定义「交付物已提交到仓库」不满足**（零 commit）——存在未满足项，不能判「通过」。

**为什么不是「未通过」**：
- `G-1 Benchmark 可重放`、`G-2 指标口径冻结`、`G-4 数据泄漏检查` 三条**判据全部满足**，且有我的独立实跑与只读复算双证据；
- `G-3` 的缺口被 PRD **自己明文豁免**：`:164`「模型环境仍 `BLOCKED_MODEL_ENV_MISSING` 时，**不得**为凑门禁而填写任何未实测指标；按「如实标注 + 登记延期」处理，**不影响其余门禁项判定**」。这是「环境阻塞 ≠ 实现失败」的裁定，不应据此判未通过；
- 无失败用例、无写面回退、无冻结资产被改。

**为什么不能判「通过」**：
1. 完成定义「交付物已提交到仓库」实测为零提交；
2. `G-3` 的「报告可区分正确拒绝 / 能力失败 / 诊断错误 / 环境故障」四类归因，在真实样本上是 `0 + 0 + 0 + 0 == 0`（`total_cases = 0`、14 个指标分母全 0，我独立只读复核，见 §3.2）——**分类器结构可判，判别力未被任何真实样本验证过**；
3. `L-2`（`docs/w10/02-design.md` §10.3 与实测闭集不一致）为设计文档事实偏差，且涉及 `EvidenceService.create` 的降级状态闭集——这是 W10 遥测 Evidence 落库路径的判据之一，未处置前不宜判「通过」。

**「定向验收通过」≠「门禁通过」**：`docs/w10/04-w10-qa-closeout.md:13` 明写「**W10 门禁：未评**。本报告只给「定向验收通过 / 不通过」，不替主理人签门禁」；其结论行 `:212` 亦为「W10 定向验收：通过。W10 门禁：未评（由主理人裁定）」。本件不继承其结论。

---

## ② 门禁判据逐条对账

> 逐条取自 `docs/w10/01-prd.md` §6（`:154-158`），另加 `:167` 的完成定义。

### G-1｜Benchmark 可重放（`01-prd.md:155`）— **满足**

> 原文：AC-01～AC-05 全绿；冻结测试集与 Runner 可重复运行，Ground Truth 只由评测端读取（AC-19/AC-15）。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-01 | Manifest 字段完整（缺 `seed`/`env_image_digest`/`budget` → `ValidationError`） | `tests/unit/test_w10_run_manifest.py` | 含在 E-1（`tests/unit` 1803 全量）0 failed | 满足 |
| AC-02 | Manifest 封闭（`extra="forbid"`） | 同上 | 同上 | 满足 |
| AC-03 | Runner 可重放（`{case_ids}`/`{seeds}`/`{tool_sequence_digest}` 各 ==1） | `tests/integration/test_w10_runner_replay.py` | **E-15 `8 passed in 0.30s` exit 0** | 满足 |
| AC-04 | 取消 / 失败原因非空 | 同上 | E-15 覆盖 | 满足 |
| AC-05 | Runner 只读（签名无 `ToolGateway`/`ApprovalService`；`registry.write_tools() == []`） | 同上 + unit | E-15 + E-1 | 满足 |
| AC-19 | Oracle 时序隔离（未终态 → `ORACLE_ACCESS_DENIED`；终态可读 1 次 + 审计） | `tests/integration/test_w10_oracle_timing.py` | **E-14 `2 passed in 2.02s` exit 0（真实库，非 skip）** | 满足 |
| AC-15 | Oracle 不入训练样本（`oracle_in_training_samples is False`） | `test_w10_group_split.py` + `test_w10_oracle_isolation.py` | E-1 0 failed | 满足 |

**附加独立取证**：冻结资产未被 W10 改动 —— `git diff --name-only` 过滤 `benchmarks/baseline_v0/`、`benchmarks/faults/`、`benchmarks/w10/` → **命中 0**。

**G-1 结论**：`满足`。

### G-2｜指标口径冻结（`01-prd.md:156`）— **满足**

> 原文：AC-06～AC-10 全绿；指标公式、分母、排除规则和统计方法写入版本化文档。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-06 | 指标分母与排除规则逐值相等 | `tests/unit/test_w10_metrics.py` | E-1 0 failed（前序件单跑 `21 passed`） | 满足 |
| AC-07 | 重复 seed 不独立计数 | 同上 | E-1 0 failed | 满足 |
| AC-08 | 不编造指标（`NOT_RUN`/`RUNNING` → `None`） | 同上 + manifest | E-1 0 failed | 满足 |
| AC-09 | 四类归因之和 == 总 case 数 | 同上 | E-1 0 failed | 满足 |
| AC-10 | Bootstrap 可复现 + `percentile([])` → `REQUEST_INVALID` | `tests/unit/test_w10_bootstrap.py` | E-1 0 failed | 满足 |

**版本化文档独立复核**（只读）：

```
benchmarks/w10/metrics-v1.json  ->  version = "metrics-v1"   definitions = 14
docs/w10/metrics-definition.md  ->  存在（11 496 B，14 个指标逐字定义）
```

→ 口径文档与冻结件**版本与条数一致**（14 = 14）。

**G-2 结论**：`满足`。

### G-3｜Baseline 报告完成（`01-prd.md:157`）— **部分满足**

> 原文：AC-17～AC-18 全绿；报告可区分正确拒绝、能力失败、诊断错误和环境故障；模型环境未解阻时如实标注「指标未测得」，不编造。

| 子判据 | 实测 | 判定 |
|---|---|---|
| AC-17 全绿（`model_commit is None` → `metrics is None` + `blocked_reason` 非空；环境故障不计入 `false_diagnosis_rate`） | `tests/unit/test_w10_baseline_report.py` 在 E-1 内 0 failed | 满足 |
| AC-18 全绿（SFT 条件闸门；`dpo`/`rl`/`14b` 恒 `False`） | `tests/unit/test_w10_sft_gate.py` 在 E-1 内 0 failed | 满足 |
| 「如实标注『指标未测得』，不编造」 | 见下方只读复算：`status=BLOCKED`、`metrics=None`、`blocked_reason` 非空、14 分母全 0 | 满足 |
| **「报告可区分正确拒绝 / 能力失败 / 诊断错误 / 环境故障」** | `total_cases = 0`；四类归因之和 `0 == 0`（恒真）；**无任何真实样本经过分类器** | **部分满足** |

**我做的只读复算（不连库、不写任何文件）** —— `benchmarks/w10/baseline_report.json`：

```
status              = BLOCKED
model_commit        = None
tokenizer_commit    = None
metrics             = None
confidence_intervals= None
cost                = None
total_cases         = 0
blocked_reason      = BLOCKED_MODEL_ENV_MISSING：模型环境不可用，Baseline 未上机，
                      模型侧实测指标未测得，一律 null（ENV-MODEL）
metric_denominators = 14 entries, all_zero = True
```

→ **诚实性成立**（没有任何估计值冒充实测；`null` 就是 `null`，14 个分母如实保留为 `int 0`）。
→ **判别力未证**：四类归因之和 `0 == 0` 是空真。分类器的"可区分"能力只有在至少一条真实 case 进入时才被考验过。

**G-3 结论**：`部分满足`。按 PRD `:164` 的明文豁免，**该缺口不使门禁判未通过**，但必须在交接说明列明（`docs/w10/04-w10-qa-closeout.md` §八 `ENV-MODEL` 已列，满足该要求）。

### G-4｜数据泄漏检查通过（`01-prd.md:158`）— **满足**

> 原文：AC-11～AC-16、AC-21 全绿；应用池数据缺失时明确 `UNKNOWN`，已采集指标带来源和时间窗口。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-11 | 遥测字段白名单 6 项（`request_body`/`user_id` 被剔除） | `tests/unit/test_w10_telemetry_adapter.py` | E-1 0 failed | 满足 |
| AC-12 | 池状态缺失不冒充（`UNKNOWN` +「应用池状态未确认」） | 同上 | E-1 0 failed | 满足 |
| AC-13 | 遥测 Evidence 字段非空且经 W09 脱敏闸门 | `tests/integration/test_w10_telemetry_evidence.py` | 前序件单跑 `2 passed`（真实库非 skip）；本件未重跑（时间预算，非判据缺口） | 满足 |
| AC-14 | group split 无泄漏（交集空 + `leakage_count == 0`） | `tests/unit/test_w10_group_split.py` | E-1 0 failed | 满足 |
| AC-15 | Oracle 不入训练样本 | 同上 + oracle_isolation | E-1 0 failed | 满足 |
| AC-16 | token 统计不臆造 | `tests/unit/test_w10_token_stats.py` | E-1 0 failed | 满足 |
| AC-21 | W09 脱敏不被绕过 | `tests/integration/test_w09_redaction_pipeline.py`（既存） | **E-12 `4 passed in 2.58s` exit 0（真实库）** | 满足 |

**G-4 结论**：`满足`。

### G-5｜本周完成定义（`01-prd.md:167`）— **不满足**

> 原文：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

| 子条件 | 实测 | 判定 |
|---|---|---|
| 交付物已**提交到仓库** | `HEAD` = `2850eca…`（与 W08 收尾同一提交）；`git diff --name-only` = 25、`git status --short` = 121（25 `M` + 96 `??`）；W10 全部新文件为 `??` 未跟踪；**零 commit** | **不满足** |
| 关键测试有自动化结果/可复现记录 | E-1…E-17 共 17 条串行实跑 exit 0 | 满足 |
| 未通过项已登记并说明影响 | `04-w10-qa-closeout.md` §七（L-1…L-6）+ §八（5 项 `ENV-*`） | 满足 |

**附**：`04-w10-qa-closeout.md:112` 的 CORR-02 事实（「`git diff --name-only` 仍为 19」）亦已过期 —— 本轮实测为 **25**（W11/W12/收尾轮继续改动 tracked 文件）。这是时点差异，不是违规（全程零 `git add`）。

**G-5 结论**：`不满足`（唯一原因是「未提交」）。

### 小结表

| # | 判据（PRD 行号） | 判定 |
|---|---|---|
| G-1 | Benchmark 可重放（`:155`） | 满足 |
| G-2 | 指标口径冻结（`:156`） | 满足 |
| G-3 | Baseline 报告完成（`:157`） | **部分满足** |
| G-4 | 数据泄漏检查通过（`:158`） | 满足 |
| G-5 | 完成定义：交付物已提交到仓库（`:167`） | **不满足** |

---

## ③ 独立取证

### 3.1 我自己跑的数字

纪律同 W09：`.venv/Scripts/python.exe`、`-o addopts="" -q -p no:cacheprovider`、**全程串行零并发**、需 DB 加外层 `timeout 180`、**不用 `--timeout=`**、每次运行仓库外全新 `--basetemp`。

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| E-1 | `pytest tests/unit`（全量） | `1803 passed in 24.87s` | 0 |
| E-14 | `timeout 180 pytest tests/integration/test_w10_oracle_timing.py` | `2 passed in 2.02s` | 0 |
| E-15 | `timeout 180 pytest tests/integration/test_w10_runner_replay.py` | `8 passed in 0.30s` | 0 |
| E-12 | `timeout 180 pytest tests/integration/test_w09_redaction_pipeline.py`（AC-21 既存） | `4 passed in 2.58s` | 0 |
| E-8 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py`（AC-20 既存） | `11 passed in 0.18s` | 0 |
| E-10 | `pytest tests/unit/test_baseline_manifest.py` | `9 passed in 0.07s` | 0 |
| E-9 | `pytest tests/unit/test_verification_perf.py` | `10 passed in 0.11s` | 0 |

**与任务书期望比对**：`tests/unit` = 1803 → **一致**；`test_w10_oracle_timing.py` = 2 → **一致**；`test_w10_runner_replay.py` = 8 → **一致**；`test_baseline_manifest.py` = 9 → **一致**；`test_verification_perf.py` = 10 → **一致**；既存两文件 6 + 5 = 11 → **一致**。

**任务书勘误（1 条）**：任务书写「W10 `test_w10_report_publish_chain.py`(2)」——该文件**不存在**；仓库内的是 `tests/integration/test_w09_report_publish_chain.py`（2 passed，见 `docs/w09/15-w09-gate-review.md` E-13）。不影响 W10 判定。

### 3.2 与前序 QA 收口件的数字对账

| 项 | `04-w10-qa-closeout.md` | 本轮实测 | 是否一致 | 说明 |
|---|---|---|---|---|
| `tests/unit` 全量 | `1582 passed`（`:125`，C1-RETRY） | 1803 | 数字不同 | 时点差异：W11（+149）、W12（+70）、收尾轮（+2）在其后落地；**无回归** |
| AC-20 既存两文件 | `11 passed`（`:126` C2） | **E-8 `11 passed`** | **一致** | — |
| AC-21 既存集成 | `4 passed`（`:133` C9，DB 非 skip） | **E-12 `4 passed`** | **一致** | — |
| `test_w10_oracle_timing.py` | `2 passed`（`:131` C7） | **E-14 `2 passed`** | **一致** | — |
| `test_w10_runner_replay.py` | `8 passed`（`:130` C6） | **E-15 `8 passed`** | **一致** | — |
| `test_w10_metrics.py`（AC-21 上游口径） | `21 passed`（`:129` C5） | 在 E-1 内 0 failed | 一致 | 本次未单跑（已含于全量） |
| `baseline_report.json` 诚实性 | EP-7：`metrics/ci/cost` 全 `None`、14 分母全 0（`:96`） | **我独立只读复算逐项一致** | **一致** | 双向独立取证 |
| tracked-modified 计数 | `19`（`:112` CORR-02） | **25** | **不一致（时点差异）** | W11/W12/收尾轮继续改动 tracked 文件；全程零 `git add`，CORR-02 的结论（不得为凑数 `git add`）仍成立 |

**一处前序件已过期的事实**：

| 编号 | 前序件表述 | 本轮实测 | 结论 |
|---|---|---|---|
| Y-1 | `04-w10-qa-closeout.md:148` L-1「`git diff --check` **exit 2**，唯一告警 `docs/w09/06-qa-report.md:624: new blank line at EOF`」 | `git diff --check` **exit 0**；`docs/w09/06-qa-report.md` 末字节为单个 `\n`（`od -c` 复核） | **已关闭**（收尾轮 B3 只删了空行、未动文字，符合 R18-02） |

### 3.3 取证方法学（如实披露）

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| **M-1** | `tests/unit` 首轮 `1 failed, 1802 passed`，唯一红 `test_w11_rc_manifest.py::test_git_rev_parse_returns_none_outside_repo` | 我首轮把 `--basetemp` 设在**仓库内**，该用例要求 tmp_path 不在 git 仓库内 | **我的取证副作用**，非实现缺陷；改仓库外 basetemp 后 `1803 passed`（E-1）。临时目录已删除 |
| **M-2** | 复用已存在的 `--basetemp` 时出现 14 个 error（W12 七件首轮） | tmp_path 目录冲突 | **我的取证副作用**；改为每次全新 basetemp 后 `120 passed` |

> **写入后复核**：4 份评审件全部落盘后，我复跑了 `tests/unit` → `1803 passed / exit 0`、W12 七件 → `120 passed / exit 0`，并复核 `scan_claims()` 仍 `ok=True` / `violations=()`（本类评审件不在 `STRICT_SCAN_FILES` 的 11 件内）。结论：新增评审件**不改变**任何既有校验结果，本件的取证数字在写入前后一致。

### 3.4 冻结资产与只读边界（我核对的）

| 边界项 | 方法 | 结果 |
|---|---|---|
| `benchmarks/baseline_v0/**`、`benchmarks/faults/**`、`benchmarks/w10/**` 零修改 | `git diff --name-only` 路径过滤 | **命中 0** |
| `docs/migrations.md` 是否被补到 011 | 只读读文件 | **仍止于 007**（`has_007=True`、`has_011=False`、`has_010=False`，93 行）→ 与 R18-01 裁定一致 |
| `benchmarks/w10/metrics-v1.json` 版本与条数 | 只读 JSON 解析 | `version="metrics-v1"`、`definitions=14` |

> 本件**未**执行 `core/benchmark plan` 双跑字节比对（`04-…md` C11）与 `baseline_report` 的 `model_validate` 走查（`04-…md` C12）——我已改用更直接的只读字段复算（§② G-3），结论同向，但**不宣称复制了那两项取证**。

---

## ④ 未闭合项

> 标记口径：`NOT_SUPPORTED` / `PARTIAL` / `UNKNOWN`。环境阻塞**不作为放行条件**。

### 4.1 对应 `docs/w12/known-limitations.json` 的项（只读引用，未改）

| `item_id` | marker | 与 W10 的关系 | 一致性核对 |
|---|---|---|---|
| `ENV-PG5432` | `NOT_SUPPORTED` | PG 侧评测 case 记 `NOT_EXECUTED` 并归 `ENVIRONMENT_FAULT`，不计入模型失败（PRD `:174` Q2） | 一致 |
| `ENV-1045` | `NOT_SUPPORTED` | 清理不可用 → 集成用例只按本任务 id 断言 | 一致 |
| `ENV-RESIDUE` | `PARTIAL` | 连库用例继续累加残留 | 一致 |
| `ENV-NV1` | `UNKNOWN` | `env_image_digest` 取不到 → `RunManifest.from_baseline()` 落 `UNKNOWN` | 一致 |
| `ENV-MODEL` | `UNKNOWN` | **直接决定 G-3 的实测指标缺席** | 一致（本件 §② G-3 已逐项复算） |
| `DOC-PARTIAL` | `PARTIAL` | `docs/migrations.md` 止于 007 | 一致 |
| `KD-W12-02` | `UNKNOWN` | 慢查询分析器输入形状缺口（W12 承接，非 W10 判据） | 一致 |

### 4.2 W10 自身遗留（逐条标标记）

| 编号 | 内容 | 标记 | 来源 |
|---|---|---|---|
| V-1 | **交付物未提交**（完成定义未满足） | `PARTIAL` | 本件 §② G-5 |
| V-2 | Baseline 零真实样本：`total_cases=0`、14 分母全 0 → 四类归因判别力未被验证 | `PARTIAL` | 本件 §3.2；PRD `:157`/`:164` |
| V-3 | `L-2`：`docs/w10/02-design.md` §10.3 称 `UNKNOWN` 会被 `EvidenceService.create` 拒；实测 `core/evidence/service.py:23-29` 的 `_DEGRADED_STATUSES = {DENIED, TIMEOUT, NOT_SUPPORTED}` **不含 `UNKNOWN`** | `PARTIAL`（设计文档事实偏差，未改生产码） | `04-…md:114`、`:149` L-2 |
| V-4 | `L-4`：`cases.jsonl` 是运行期产物（`core/benchmark/runner.py:680`），仓库内 `find -name '*.jsonl'` 零命中 | `PARTIAL`（载体性质登记） | `04-…md:151` L-4 |
| V-5 | `L-5`：`fault_seed` 在 `suite_manifest.json`（6 处）与 `metrics-v1.json` notes 中命中 `ORACLE_FORBIDDEN_FIELDS`；已界定为「冻结评测清单内的公开 `data_seed`」，泄红约束不含清单本身 | `PARTIAL`（边界登记） | `04-…md:152` L-5 |
| V-6 | `L-3` / `ENV-PY314-AST-CRASH`：`pytest tests/unit` 曾在 pytest assertion-rewrite 层 `exit 139`，隔离重跑即恢复 | `NOT_SUPPORTED`（环境层） | `04-…md:150` L-3 |
| V-7 | SFT / DPO / RL / 全量 14B 全部推迟（W10-Q7 明确不入门禁） | `NOT_SUPPORTED`（按裁定不入本周门禁） | PRD `:179` Q7；AC-18 |
| V-8 | `L-1`（`git diff --check` exit 2 / W09 EOF 空行）——**已关闭** | 已关闭 | 本件 §3.2 Y-1 |
| V-9 | 跑 pytest 时若 tmp 目录设在仓库内，`test_git_rev_parse_returns_none_outside_repo` 恒红 | `PARTIAL`（运维提示） | 本件 §3.3 M-1 |

---

## ⑤ 放行条件（每条可判定、可关闭）

| # | 条件 | 关闭判据（可机器核对） | 责任 |
|---|---|---|---|
| **C-W10-1** | 提交一次 commit，把 W10 交付物（`core/benchmark/**`、`benchmarks/w10/**`、`tests/**/test_w10_*`、`docs/w10/**`、`docs/w10/metrics-definition.md`）入库并记录 `commit` 哈希 | `git rev-parse HEAD != 2850eca…` 且 `git status --short` 对上述路径为空 | 主理人 / 工程师 |
| **C-W10-2** | `L-2` 设计文档事实偏差处置：把 `docs/w10/02-design.md` §10.3 的 `_DEGRADED_STATUSES` 描述改为实测闭集 `{DENIED, TIMEOUT, NOT_SUPPORTED}`，或在裁定台账出勘误条目 | `grep -n "_DEGRADED_STATUSES" docs/w10/02-design.md` 与 `core/evidence/service.py:23` 一致 **或** 台账新增勘误裁定 | 架构师 / 主理人 |
| **C-W10-3** | Baseline 实测补跑（依赖 `ENV-MODEL` 解阻，由主理人拍板是否解阻）：解阻后重跑 `core.benchmark` 并回填**实测**指标 | `benchmarks/w10/baseline_report.json` 的 `status != "BLOCKED"`、`metrics` 非 `None`、`metric_denominators` 不全为 0、`total_cases > 0` | 主理人 |
| **C-W10-4** | 四类归因判别力取证：至少 1 条真实 case（含 1 条环境故障 case）走完 `attribute` 分类器，并断言四类之和 == 总数且各类非空 | 新增/扩展用例绿，且不再依赖 `0 == 0` 的空真 | 工程师 |

> `ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL` **不列入**放行条件（PRD `:164`、`:174`、`:165` 明文：环境阻塞如实登记，不判 FAIL、不影响其余门禁项判定）。C-W10-3 是「解阻后补测」的**后续迭代条件**，不是本次放行的否决项。

---

## ⑥ 口径自查（禁用表述）

| 禁用表述 | 本件是否出现 | 说明 |
|---|---|---|
| 「W10 门禁通过 / 已收口」 | **否** | 结论为「`有条件放行`｜门禁通过：**否**」，附 4 条未关闭条件 |
| 「容器网络隔离已验证」 | **否** | 写「配置层已实测 / 运行态 `NOT_VERIFIED`」 |
| 「环境已清理 / 已可复现清零」 | **否** | 只写 `ENV-RESIDUE` 不清理 |
| 「QA 独立实跑通过」 | **否** | 逐条给原文汇总行 + exit code，并披露 M-1/M-2 |
| 「回归全绿」 | **否** | 只给原文汇总行；并如实登记 `ENV-PY314-AST-CRASH` 曾 `exit 139` 的历史（V-6） |
| 「Baseline 已完成」作为**实测完成** | **否** | 只写「报告结构与口径完成；实测指标 `null`、`total_cases=0`」 |
| 「W07 / W08 / W09 已通过」 | **否** | 分别引「有条件放行；门禁通过：否」/「有条件放行」/「有条件放行｜门禁通过：否」 |
| 「Alpha 就绪」 | **否** | SFT/DPO/RL/14B 一律写「推迟至 Alpha 之后」 |

---

## ⑦ 读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅**本文件 `docs/w10/05-w10-gate-review.md` |
| 修改 `.py` / SQL / 配置 / 既有 docs | **0**（`docs/w10/01-prd.md`、`02-design.md`、`03-task-cards.md`、`04-w10-qa-closeout.md`、`metrics-definition.md` 均未触碰；`docs/w12/known-limitations.*` 未触碰） |
| 手工 `INSERT/UPDATE/DELETE/TRUNCATE/ALTER` / rollback | **0** |
| 新增测试用例 | **0** |
| `git commit / add / checkout / reset` | **0** |
| 无路径全量 pytest / `--timeout=` | **0** |
| 临时产物 | 全部在 `%LOCALAPPDATA%/Temp/`（仓库外）；我一度在仓库内建的 `.qa_tmp_base` 与 `runs/.qa_unit_full.txt` 已删除，`git status --short` 复归 121 |

---

*严过关（Edward）｜QA 工程师｜`software-dbguardian-w09-closeout2`｜时点锚 `HEAD = 2850eca`，全部改动未提交*
