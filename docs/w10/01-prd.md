# W10 增量 PRD｜Benchmark 与应用观测（建立可复现评测，让性能、诊断质量和模型收益有统一口径）

> 本文是 V2.2 12 周计划的第 10 周**增量需求基线**，供架构设计与实现使用。
> 口径约定：§4「现状」引用的 `file:line` 均为**已核实的既存文件**；§4/§5「验收位置 / 测试位置」中的测试文件为**本周待新建**，路径按 `tests/unit/`、`tests/integration/`、`benchmarks/` 既有命名约定给出，非既存文件。二者不得混读。

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.13/3.14 + FastAPI + MySQL 8 控制库 + MySQL LAB 目标库 + PostgreSQL MCP 只读（**延续 V2.2 既有栈；不新增前端、不新增第三方依赖，沿用 `requirements.lock.txt`**） |
| Project Name | `dbguardian_w10_benchmark_telemetry` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W10_Benchmark 与应用观测.docx` §一–§六；`项目规划/项目规划2.2/DBGuardian_12周研发计划_总览与门禁.docx` §二/§三/§四/§五/§六；上游 `docs/w09/01-prd.md`（本周脱敏/报告门禁）、`docs/w08/01-prd.md`、`docs/w08/02-design.md`、`docs/w08/08-drill-report.md`、`docs/w07/07-gate-review.md`；代码基线（已核实）`benchmarks/baseline_v0/run_baseline.py` 及 6 个冻结资产、`benchmarks/faults/`、`core/evidence/models.py`、`core/verification/perf.py`、`core/context/builder.py` |
| 前置门禁 | **依赖 W09 门禁**（W9→W10 四条：敏感数据门禁通过 / 报告证据引用有效 / 知识可审核·过期·撤销·冲突处理 / 降级不绕过策略，见 `docs/w09/01-prd.md` §6）。W09 门禁**尚未裁定**；**根依赖 W08 门禁仍「尚在复核、未裁定」**：W08 T01–T06 代码已实现、单元 `891 passed / 0 failed`（`docs/w08/08-drill-report.md:16`），但 `docs/w08/06-qa-report.md`、`docs/w08/07-gate-review.md` **均未落盘**。**不得**写成「W08/W09 已通过」 |
| 前置依赖（环境阻塞，如实登记） | ① PostgreSQL 5432 拒绝连接（`docs/w08/08-drill-report.md:165`）；② `DG_ADMIN_DSN`（root）认证失败 1045、只追加表无 `DELETE` 授权（`docs/w08/08-drill-report.md:166`、`docs/w07/07-gate-review.md:111`）；③ 控制库历史任务堆积（主理人任务书登记 830+ 行，逐条见 `docs/w07/worker-residue.md`）。**本周评测不依赖 PG 写路径、不依赖 admin 清理；PG 侧只读用例按环境阻塞如实记 `UNKNOWN`/`NOT_EXECUTED`，不冒充通过** |
| 原始需求复述 | 任务书「本周目标」原句：「完成 Benchmark Engine 的最小可用版本、评测统计与故障矩阵扩展；接入最小 AppTelemetryAdapter；建立数据集质量与切分报告。」 |

## 1. 产品目标

**一句话**：把 W09「可解释、已脱敏的报告」变成「**口径冻结、可重放、可分组统计的评测体系**」，并在不采集用户数据、不引入新依赖的前提下补上应用侧连接池观测的空缺。

三个正交目标（判定口径均为可验证事实）：

| # | 目标 | 判定口径（可机器验证） |
|---|---|---|
| G1 | **评测可重放** | 同一 `Run Manifest`（`suite_version`/`model_version`/`prompt·tool·schema 版本`/环境镜像 digest/`seed`/`budget`/起止时间）连续两次运行，冻结测试集逐 case 的样本数、`seed` 集合、工具调用序列 digest 一致；`results.json` 的 `status` 由 `NOT_RUN` 迁移到真实值**只由评测端写入**，Agent 侧结构上无法写 Oracle |
| G2 | **指标口径冻结、不编造** | 每项指标有版本化定义：分子、分母、排除规则、环境故障/注入失败/无可观测故障的处置；`results.status != "RUNNING"` 时 `scores`/`metrics` 必须为 `null`（延续 `benchmarks/baseline_v0/run_baseline.py:342-354`）；报告能区分「正确拒绝 / 能力失败 / 诊断错误 / 环境故障」四类，不把所有失败归因于模型 |
| G3 | **数据不泄漏、观测不越界** | `group split` 按 `engine`/`fault_family`/`seed`/`workload_group` 分组，训练·验证·测试交集为空；Oracle 四标志 `oracle_visible_to_agent`/`oracle_visible_to_model`/`oracle_in_logs`/`oracle_in_training_samples` 全为 `false`（`run_baseline.py:281-288`）；`AppTelemetryAdapter` 采集字段白名单不含业务请求正文/用户数据，指标缺失时输出「应用池状态未确认」而非用数据库连接数代替 |

## 2. 本周边界（先划死，防止范围蔓延）

| 边界 | 内容 |
|---|---|
| 做什么 | ① `Benchmark Run Manifest` + 独立 `benchmark-runner` + 任务重放/样本保存/取消/失败原因 + 测试集冻结；② 指标定义与统计脚本（含分组报告、Bootstrap）；③ 最小 `AppTelemetryAdapter` + Evidence 字段；④ 数据集清洗/去重/脱敏/许可/标签/manifest 与 `group split`、token 统计；⑤ 冻结 Harness 上运行**未微调 Baseline** 的评测报告、失败归因、以及**条件性**的 SFT 格式小试跑 |
| 写能力 | **不扩大写能力**。唯一 R2 动作仍是 `mysql_lab_update_order_note` / `1.0`；`benchmark-runner` 只读评测，不注册写工具、不给 Agent 执行器新端点 |
| 引擎写路径 | **不做第二引擎写路径、不做 PostgreSQL 写路径**；PG 保持 MCP 只读。PG 只读诊断用例因 5432 拒绝连接记 `NOT_EXECUTED`（`docs/w08/08-drill-report.md:165`），不修环境、不冒充通过 |
| 第三方依赖 | **不引入新的第三方依赖**：P50/P95/Bootstrap 用标准库（延续 `docs/w08/02-design.md:465`「P50/P95 用标准库实现（排序 + 索引法）」）；`docs/w07/02-design.md:725` 已明确不引入 `prometheus_client`/`psutil`/`apscheduler`；`AppTelemetryAdapter` 走**人工导入快照**或只读解析既有指标文本，不新起 exporter 服务 |
| 安全机制 | **不绕过审批 / 证据 / 审计 / 验证机制**：评测端只能在 Agent 任务**结束后**读 Oracle；脱敏在模型上下文/Evidence 之前执行（W09 门禁）；`benchmark-runner` 不得复用生产审批/执行凭据；故障注入复用 `benchmarks/faults/injector.py` 既有接缝，不新开生产分支 |
| 如实返回状态 | 应用池指标缺失 → 「应用池状态未确认 / `UNKNOWN`」，**不得**用数据库连接数代替（`core/evidence/models.py:56-58` 的 `collection_status`/`quality`/`completeness` 口径）；环境故障/注入失败/无可观测故障须单列，不计入模型失败；重复 `seed` 不视为独立样本 |
| 前端 | **不新增前端交付项**；分组报告以版本化文档 + 结构化结果为准 |
| 训练（非主线） | **模型训练不是本周主线**。SFT 仅在 W10 Baseline 评测报告**发现可定位模型短板**后，做 10–50 条**数据格式/保存加载/断点恢复**小试跑；不得挤占 Gateway、审批、Verifier、恢复等 P0 资源；训练版本必须与 Baseline 用同一 Harness/测试集；无可重复收益则保留 Baseline，**不进入 DPO / 14B / RL**（《总览》§四/§五、W10 任务书 §二 D5）。14B 完整训练延期到 Alpha 之后 |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 评测工程师 | 作为评测工程师，我要用一份带 `suite_version`/镜像 digest/`seed`/各版本号的 Run Manifest 重放整轮评测，两次跑出的 case 集、seed 集和工具序列一致，这样指标差异才归因得到具体变化。 |
| U2 | 数据工程师 | 作为数据工程师，我要训练/验证/测试按 fault family、实例、seed、SQL workload group 做 group split，且 Oracle 真值只在评测端可读、不出现在日志或训练样本里，这样我能证明没有同源模板泄漏。 |
| U3 | 值班 SRE | 作为值班 SRE，我要报告能区分「正确拒绝 / 能力失败 / 诊断错误 / 环境故障」，并单列环境故障与注入失败，这样我不会把 PG 连不上算成模型诊断错。 |
| U4 | 安全 / 合规评审 | 作为安全评审，我要 `AppTelemetryAdapter` 只采集连接数、等待线程、acquire 超时/时延等池指标（带来源/采样时间/聚合窗口/exporter 版本），不采集业务请求正文或用户数据；没有应用侧指标时明确输出「应用池状态未确认」。 |
| U5 | 主理人（模型收益决策） | 作为主理人，我要 Baseline 报告给出分组指标、置信区间/统计方法、成本与失败案例，并把失败归因到工具/上下文/数据/Prompt/模型容量；只有在已定位模型短板且小试跑有可重复收益时才考虑 SFT，否则保留未微调 Baseline。 |
| U6 | 审计员 | 作为审计员，我要每次评测的取消、失败原因、样本结果与测试集版本都可追溯，且 `results.json` 未真实运行前保持 `NOT_RUN`/`scores=null`，不出现编造的基线成绩。 |

## 4. 需求池

> **P0 为 W10→W11 门禁必需，不得降级、不得延期**。每条 P0 给出 `[D#]`、已核实现状、可实施要求、**验收位置（测试文件路径 + 断言形式）**。
> `file:line` 现状引用已用 Grep/Read 核实；「当前无该能力」以全仓检索无代码实现为依据（`BenchmarkRunner`/`RunManifest`/`AppTelemetryAdapter`/`task_success_rate` 等仅命中 `docs/w07/08-pm-ruling.md:150` 未来项描述）。

### P0（Must have，6 条）

- **P0-W10-01** `[D1]` **`Benchmark Run Manifest` + 独立 `benchmark-runner` + 重放/取消/冻结。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:20-34` 已有 `REQUIRED_MANIFEST_FIELDS`（`manifest_version`/`wbs`/`model_name`/`model_commit`/`tokenizer_commit`/`prompt_version`/`tool_schema_version`/`harness_version`/`inference_config_version`/`testset_version`/`created_at`/`status`/`notes`），`:135` `validate_manifest` 强制 `status=BLOCKED_MODEL_ENV_MISSING`（`:140`）、`model_commit`/`tokenizer_commit` 为 `null`（`:142-145`）、`harness_version=44e4f9d`（`:146`）；`benchmarks/baseline_v0/` 下 6 个冻结资产 + `README.md` 已存在；`tests/unit/test_baseline_manifest.py` 已存在。**但这是 W04「资产校验 + dry-run 规划」脚本（`:1-7`：不下载模型/不加载权重/不训练/不联网/不产生指标），全仓无 `BenchmarkRunner` / 运行期 `Run Manifest` / 重放与样本保存实现**。
  - 要求：
    1. `Run Manifest` 含：`suite_version`、`model_version`、`prompt/tool/schema 版本`、环境镜像 digest、`seed`、`budget`、起止时间；版本号取值与 `baseline_manifest.json` 冻结字段一致，不得运行期新造。
    2. 建立独立 `benchmark-runner` 与「故障注入 / 评测」权限分离；**Agent 任务结束后 Evaluation Engine 才能读 Oracle**（与 `run_baseline.py:71` `ORACLE_PROMPT_FORBIDDEN`、`run_baseline.py:332-334` Oracle 不对模型可见同向）。
    3. 任务重放、样本结果保存、取消与失败原因记录；冻结测试集版本；数据集标签区分 `VERIFIED`/`REVIEWED`/`GENERATED`/`REJECTED`/`QUARANTINED`。
    4. Runner 只读评测：不注册写工具、不新增 Agent `execute`/`consume` 端点。
  - **验收位置**：`tests/unit/test_w10_run_manifest.py`（新建）——① Manifest 缺 `seed`/`env_image_digest`/`budget` 任一 → `ValidationError`；② 版本号与 `baseline_manifest.json` 逐字段相等；③ `RunManifest` `extra="forbid"`。`tests/integration/test_w10_runner_replay.py`（新建）——同一 Manifest 连跑 2 次：case 集、`{seed}`、工具序列 digest 三个集合各大小 ==1；取消 → `status=CANCELLED` 且 `cancel_reason` 非空；Runner 装配对象不持有 `ToolGateway`/`ApprovalService` 实例（签名检查）。

- **P0-W10-02** `[D2]` **指标定义、统计脚本与分组报告。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:59-69` 已有 `METRIC_KEYS`（用于「manifest 不得含指标」校验，`:150-152`）、`:342-354` `validate_results` 强制 `status=NOT_RUN`/`scores=null`/`metrics=null`（不得编造指标）；`core/verification/perf.py` 已有标准库 `percentile`（`docs/w08/02-design.md:465` 排序索引法）。**全仓无 `task_success_rate` / `unsafe_action_block_rate` / `rca_top1` / `correct_abstention_rate` / `false_repair_rate` / Bootstrap / 分组统计实现**。
  - 要求：
    1. 实现 8 项：`task_success_rate`、`unsafe_action_block_rate`、`schema_valid_rate`、`rca_top1`、`correct_abstention_rate`、`false_diagnosis_rate`、`false_repair_rate`、`recovery_success_rate`；补充 Evidence Coverage、Verified Task Success、False Repair Rate、Regression Rate、Recovery Success、Unsafe Action Escape Rate、任务 P95、Cost per Verified Task。
    2. 每项指标版本化写入：分子、分母、**排除规则**、环境故障/注入失败/无可观测故障的处置；四类结果（正确拒绝 / 能力失败 / 诊断错误 / 环境故障）可分别计数。
    3. 按 `db_engine × scenario × fault_seed` 分组；**重复 seed 不视为独立样本**，采用分层/聚类 Bootstrap（标准库实现，不引 numpy/scipy）。
    4. `results.status` 未到终态前 `scores`/`metrics` 恒为 `null`（延续 `run_baseline.py:344-349`）。
  - **验收位置**：`tests/unit/test_w10_metrics.py`（新建）——① 给定 10 case 含 2 环境故障 + 1 注入失败，各指标分母按排除规则逐值相等；② 重复 `seed` 3 次 → 有效样本数只计 1；③ `results.status=NOT_RUN` → `scores is None and metrics is None`；④ 四类归因计数之和 == 总 case 数。`tests/unit/test_w10_bootstrap.py`（新建）——固定序列的 Bootstrap 置信区间可复现（同 `seed` 两次结果相等）；`percentile([])` → `REQUEST_INVALID`（复用 W08 口径）。

- **P0-W10-03** `[D3]` **最小 `AppTelemetryAdapter` + Evidence 字段。**
  - 现状（已核实）：`core/evidence/models.py:50-51` `db_version`/`adapter_version`、`:53-55` `time_window_start`/`time_window_end`/`observed_at`、`:56-58` `collection_status`/`quality`/`completeness` 可作观测字段口径；`docs/w07/02-design.md:725` 明确不引入 `prometheus_client`/`psutil`/`apscheduler`。**全仓无 `AppTelemetryAdapter` / 连接池观测 / Prometheus 或快照导入实现**。
  - 要求：
    1. 按最小权限接入：人工导入快照优先，或只读解析既有指标端点文本；首批适配 HikariCP / PgBouncer 连接池；**不新起 exporter 服务、不新增第三方依赖**。
    2. 采集白名单：`active/idle connections`、`pending threads`、`acquire timeout`、`acquire latency P95`、`pool_max_size`、`application_error_rate`；**不采集业务请求正文或用户数据**。
    3. Evidence 记录来源、采样时间、聚合窗口、exporter 版本（映射到 `core/evidence/models.py:50-58` 字段）；采集结果须先过 W09 脱敏闸门。
    4. 没有应用侧指标 → 输出「应用池状态未确认」/ `UNKNOWN`，**不得**用数据库连接数代替。
  - **验收位置**：`tests/unit/test_w10_telemetry_adapter.py`（新建）——① 采集字段集合 == 6 项白名单；② 快照含 `request_body`/`user_id` 字段 → 被剔除且不进 Evidence；③ 缺失快照 → `collection_status is UNKNOWN` 且 `message == "应用池状态未确认"`；④ 用数据库连接数冒充池指标 → `ValidationError`。`tests/integration/test_w10_telemetry_evidence.py`（新建）——Evidence 行 `source_tool`/`observed_at`/`time_window_*`/`adapter_version` 均非空且经脱敏。

- **P0-W10-04** `[D4]` **数据集质量、`group split` 与 token 统计。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:226-289` `validate_testset` 已强制 `split.strategy="group_split"`（`:271`）、`group_keys` 含 `engine`/`fault_family`/`seed`/`workload_group`（`:273-276`）、`oracle_isolation` 四标志全 `false`（`:277-288`）、`repetitions_per_seed=3`（`:259`）、`status=NOT_GENERATED`（`:261`）；`benchmarks/faults/manifests/` 下 6 个故障清单、`benchmarks/faults/oracle/` 下 6 个 Oracle 文件已隔离存放。`docs/w09/01-prd.md` 的脱敏闸门是本周数据集清洗前置。**全仓无数据清洗/去重/许可检查/标签审核流程、无 token 统计实现**。
  - 要求：
    1. 建立数据清洗、去重、脱敏、许可检查、标签审核、manifest 生成流程；标签沿用 `VERIFIED`/`REVIEWED`/`GENERATED`/`REJECTED`/`QUARANTINED`，来源或许可不明样本隔离。
    2. 训练/验证/测试按 `fault_family`、实例、`seed`、`SQL workload group` 做 `group split`，三组交集为空；调参只用开发/验证集，最终测试集冻结。
    3. 目标先积累 100–300 条人工核验、可重放核心轨迹；**条数不等于有效训练规模**，优先证据与断言质量。
    4. 用目标 tokenizer 统计 token P50/P95、截断比例、`assistant loss mask`、`packing` 边界、工具 Schema 覆盖率；tokenizer 版本从冻结 manifest 读，未冻结记 `UNKNOWN`，**不臆造统计值**。
  - **验收位置**：`tests/unit/test_w10_group_split.py`（新建）——① 构造同源模板跨三组 → 三组交集为空且 `leakage_count == 0`；② 许可不明样本 → `label == QUARANTINED` 且不进任何集合；③ `oracle_in_training_samples is False` 且训练样本文本不含 Oracle 真值。`tests/unit/test_w10_token_stats.py`（新建）——固定样本的 token P50/P95 与预期逐值相等；tokenizer 未冻结 → `tokenizer_version == "UNKNOWN"` 且统计字段为 `null`。

- **P0-W10-05** `[D5]` **Baseline 评测报告 + 失败归因 + 条件性 SFT 小试跑。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:383-439` `build_dry_run_plan` 明确「不下载模型/不加载权重/不训练/不联网」，`:430-438` `planned_steps_after_unblock` 列出上机后 7 步；`baseline_manifest.json`/`inference_config.json` 当前 `status=BLOCKED_MODEL_ENV_MISSING`、模型版本为 `null`（`:140-145/:303-313`）；`results.json` 当前 `NOT_RUN`（`:344`）。**全仓无真实 Baseline 评测报告、无失败归因、无 SFT 试跑记录**。
  - 要求：
    1. 在冻结 Harness/测试集上运行**未微调 Baseline**，输出分组指标、置信区间/统计方法、成本与失败案例；报告区分「正确拒绝 / 能力失败 / 诊断错误 / 环境故障」。
    2. 失败归因到工具、上下文、数据、Prompt 或模型容量，**不把所有失败归因于模型**；PG 侧因 5432 阻塞的用例如实标 `NOT_EXECUTED`/环境故障，不计入模型失败。
    3. **条件性 SFT 小试跑**：仅当已定位模型短板时，做 10–50 条数据格式/保存加载/断点恢复验证；训练版本与 Baseline 用同一 Harness/测试集；**不挤占 Gateway/审批/Verifier/恢复 P0 资源**；无可重复收益则保留 Baseline，**不进入 DPO / 14B / RL**。
    4. 模型环境仍不可用时（`BLOCKED_MODEL_ENV_MISSING`），报告如实标注「Baseline 未上机、指标未测得」，**不得填写任何编造指标**（`run_baseline.py:342-354`）。
  - **验收位置**：`tests/unit/test_w10_baseline_report.py`（新建）——① 报告四类归因计数之和 == case 总数；② `model_commit is None` 时 `metrics is None` 且 `blocked_reason` 非空；③ 环境故障用例 `attribution == "ENVIRONMENT"`，不计入 `false_diagnosis_rate` 分子。`tests/unit/test_w10_sft_gate.py`（新建）——① `shortcoming_located is False` → `sft_allowed is False`；② 允许时 `sample_count in range(10,51)` 且 `harness_version`/`testset_version` 与 Baseline 相等；③ 断言 `dpo_allowed`/`rl_allowed`/`full_14b_allowed` 恒为 `False`。

- **P0-W10-06** `[D1/D4 补强]` **Ground Truth Oracle 隔离与「不编造指标」红线校验。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:71` `ORACLE_PROMPT_FORBIDDEN = ("expected_root_cause","fault_seed")`、`:162-164` `validate_prompt` 禁止 prompt 含 Oracle 真值字段、`:277-288` `oracle_isolation` 四 `false`、`:332-334` `oracle_visible_to_model is False`；`benchmarks/faults/oracle/*.json` 6 个文件独立存放；`core/context/builder.py:39-70` `build()` 是模型上下文唯一装配点（W09 脱敏闸门后）。
  - 要求：
    1. 评测端之外任何组件（Agent/模型/日志/训练样本）不得读到 Oracle：以 `oracle` 文件路径与 `fault_seed` 字段做静态与运行期双重检查。
    2. Agent 任务结束前，Runner 不得读取 Oracle 目录（时序断言）；`fault_seed` 不得进入 prompt/上下文/训练样本。
    3. 任何未真实测得的指标保持 `null`；报告不得出现「预计 / 估计值」充当实测值。
  - **验收位置**：`tests/unit/test_w10_oracle_isolation.py`（新建）——① 扫描所有 prompt/上下文/训练样本文本 → 不含 `expected_root_cause`/`fault_seed`；② `oracle_isolation` 四标志全 `False` 断言；③ 构造 `fault_seed` 泄漏样本 → `ORACLE_LEAK_DETECTED`。`tests/integration/test_w10_oracle_timing.py`（新建）——Agent 任务状态未到终态时读 Oracle → `ORACLE_ACCESS_DENIED`；终态后可读一次且留审计。

### P1（本周期内可做，不进门禁）

- **P1-W10-01** `[D2]` 故障矩阵扩展的只读清单（追加 `fault family` 元数据，不改 6 个既有 manifest）。**AC**：`tests/unit/test_w10_fault_matrix.py`（新建）新增元数据可被 `validate_testset` 兼容，既存 6 组合校验仍绿。
- **P1-W10-02** `[D3]` 快照导入的 manifest（来源/时间窗/exporter 版本）版本化。**AC**：`tests/unit/test_w10_snapshot_manifest.py`（新建）字段冻结校验。
- **P1-W10-03** `[D2]` `Cost per Verified Task` 的口径文档草稿（数值不填）。**AC**：文档存在且分母定义明确、指标值为 `null`。

### P2（明确延期，不进 W10 门禁）

- **P2-W10-01** DPO / RL / GRPO / 14B 完整训练与复杂多 Agent（《总览》§五优先裁剪顺序）。
- **P2-W10-02** `AppTelemetry` 深度集成与实时 exporter、知识关系网络、高级 UI。
- **P2-W10-03** 更多故障场景（超出 W11 D3 六类注入清单的部分需主理人拍板）。

## 5. 验收标准 AC（可机器验证）

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | Run Manifest 字段完整 | `tests/unit/test_w10_run_manifest.py`（新建） | 缺 `seed`/`env_image_digest`/`budget` 任一 → `ValidationError`；版本字段与 `baseline_manifest.json` 相等 |
| AC-02 | Manifest 封闭 | 同上 | `RunManifest` 注入多余字段 → `ValidationError`（`extra="forbid"`） |
| AC-03 | Runner 可重放 | `tests/integration/test_w10_runner_replay.py`（新建） | 同一 Manifest 连跑 2 次：`{case_ids}`/`{seeds}`/`{tool_sequence_digest}` 集合大小各 ==1 |
| AC-04 | 取消与失败原因 | 同上 | 取消 → `status=CANCELLED` 且 `cancel_reason` 非空；失败 → `failure_reason` 非空 |
| AC-05 | Runner 只读 | 同上 | Runner 装配签名无 `ToolGateway`/`ApprovalService` 形参；`registry.write_tools() == []` |
| AC-06 | 指标分母与排除规则 | `tests/unit/test_w10_metrics.py`（新建） | 10 case（2 环境故障 + 1 注入失败）→ 逐指标分母按冻结规则逐值相等 |
| AC-07 | 重复 seed 不独立计数 | 同上 | 同 `seed` 重复 3 次 → `effective_samples` 只 +1 |
| AC-08 | 不编造指标 | 同上 | `status=NOT_RUN` → `scores is None and metrics is None`；`status=RUNNING` 同样为 `None` |
| AC-09 | 四类归因可区分 | 同上 | `correct_abstention + capability_failure + diagnosis_error + environment_fault == total_cases` |
| AC-10 | Bootstrap 可复现 | `tests/unit/test_w10_bootstrap.py`（新建） | 同 `seed` 两次置信区间相等；`percentile([])` → `REQUEST_INVALID` |
| AC-11 | 遥测字段白名单 | `tests/unit/test_w10_telemetry_adapter.py`（新建） | 采集字段集合 == 6 项白名单；含 `request_body`/`user_id` 被剔除 |
| AC-12 | 池状态缺失不冒充 | 同上 | 无快照 → `collection_status is UNKNOWN` 且 `message == "应用池状态未确认"`；用 DB 连接数代替 → `ValidationError` |
| AC-13 | 遥测 Evidence 字段 | `tests/integration/test_w10_telemetry_evidence.py`（新建） | `source_tool`/`observed_at`/`time_window_*`/`adapter_version` 均非空且经 W09 脱敏闸门 |
| AC-14 | group split 无泄漏 | `tests/unit/test_w10_group_split.py`（新建） | 同源模板跨三组 → 交集为空且 `leakage_count == 0`；许可不明 → `QUARANTINED` 不进集合 |
| AC-15 | Oracle 不入训练样本 | 同上 + `tests/unit/test_w10_oracle_isolation.py`（新建） | `oracle_in_training_samples is False`；样本文本不含 `expected_root_cause`/`fault_seed` |
| AC-16 | token 统计不臆造 | `tests/unit/test_w10_token_stats.py`（新建） | 固定样本 P50/P95 逐值相等；tokenizer 未冻结 → `tokenizer_version=="UNKNOWN"` 且统计为 `null` |
| AC-17 | Baseline 报告诚实 | `tests/unit/test_w10_baseline_report.py`（新建） | `model_commit is None` → `metrics is None` 且 `blocked_reason` 非空；环境故障不计入 `false_diagnosis_rate` |
| AC-18 | SFT 条件闸门 | `tests/unit/test_w10_sft_gate.py`（新建） | `shortcoming_located is False` → `sft_allowed is False`；允许时 `10<=sample_count<=50` 且 Harness/测试集版本相等；`dpo_allowed`/`rl_allowed`/`full_14b_allowed` 恒 `False` |
| AC-19 | Oracle 时序隔离 | `tests/integration/test_w10_oracle_timing.py`（新建） | 任务未到终态读 Oracle → `ORACLE_ACCESS_DENIED`；终态后可读 1 次且留审计 |
| AC-20 | 写面与引擎不回退 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py`（既存） | PG 工具集仍 7 个全 `read`；write 工具仍 `LAB+mysql` 单一；无第二写动作 |
| AC-21 | W09 脱敏不被绕过 | `tests/integration/test_w09_redaction_pipeline.py`（既存，W09 门禁） | Runner 消费的样本在 Evidence/模型输入前 `status in {CLEAN,REDACTED}`；数据集清洗出口无未脱敏样本 |

## 6. 周末门禁与完成定义

**W10 → W11 门禁（浓缩任务书 §六，逐条可判）**：
1. **Benchmark 可重放**：AC-01～AC-05 全绿；冻结测试集与 Runner 可重复运行，Ground Truth 只由评测端读取（AC-19/AC-15）。
2. **指标口径冻结**：AC-06～AC-10 全绿；指标公式、分母、排除规则和统计方法写入版本化文档。
3. **Baseline 报告完成**：AC-17～AC-18 全绿；报告可区分正确拒绝、能力失败、诊断错误和环境故障；模型环境未解阻时如实标注「指标未测得」，不编造。
4. **数据泄漏检查通过**：AC-11～AC-16、AC-21 全绿；应用池数据缺失时明确 `UNKNOWN`，已采集指标带来源和时间窗口。

**未通过处置**：
- 门禁任一项未过 → **先修复 W10 前置模块，不并行堆叠 W11 功能**；W11 安全回归与长稳不得在「指标口径未冻结 / Baseline 未复现 / 数据泄漏未查」的前提下开始。
- **W09 门禁未过**（脱敏闸门未闭合）→ 本周所有数据集/训练样本清洗与评测**暂停**，先回 W09 修脱敏，不得消费未脱敏样本（数据泄漏红线）。
- **W08 门禁复核最终未通过**（当前尚在复核）→ 沿用 `docs/w08/` 门禁件处置缩小修复范围或仅发布诊断预览，W10 门禁不得替代上游门禁证据。
- 模型环境仍 `BLOCKED_MODEL_ENV_MISSING` 时，**不得**为凑门禁而填写任何未实测指标；按「如实标注 + 登记延期」处理，不影响其余门禁项判定，但须在交接说明列明。
- 已知环境阻塞（PG 5432 / admin 1045 / 只追加表残留）不因本周工作消失，须如实登记，不得表述为「环境已可复现清零」（`docs/w07/07-gate-review.md:135`）。

**本周完成定义（任务书 §六）**：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

## 7. 待确认问题 Q

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | **W08 门禁仍「尚在复核、未裁定」**（`docs/w08/06-qa-report.md`、`07-gate-review.md` 未落盘），且 **W09 门禁未裁定**，W10 能否先做？ | **建议**：先做不消费生产数据、不触碰写路径的工作（Manifest 模型、指标口径文档、Oracle 隔离静态检查、group split 工具）；**不宣布 W10 开工、不写「上游已通过」**；门禁签署等上游件落盘后补做 | 主理人 + QA |
| Q2 | 已知环境阻塞（PG 5432 拒绝连接 / `DG_ADMIN_DSN` root 1045 / 控制库 830+ 历史行、只追加表无 DELETE）如何影响 W10？ | **建议**：PG 侧只读诊断评测用例记 `NOT_EXECUTED` 并归因为环境故障（不计入模型失败）；评测不依赖 admin 清理；清理前提以 `docs/w07/worker-residue.md` 为准，不为「好清理」放宽 `dg_ctl` | 主理人 |
| Q3 | 模型环境仍 `BLOCKED_MODEL_ENV_MISSING`（`run_baseline.py:140-145/:303-313`，`model_commit`/`tokenizer_commit` 为 `null`），Baseline 报告如何算「完成」？ | **建议**：门禁判「Baseline 报告完成」= 报告结构、口径、归因、失败案例、环境阻塞标注齐全；**实测指标缺席如实标 `null`**，不以填写估计值充当完成；是否解阻 GPU/模型环境由主理人拍板，本周不擅自下载模型/联网 | 主理人 |
| Q4 | `AppTelemetryAdapter` 接 Prometheus 还是人工导入快照？`docs/w07/02-design.md:725` 已明确不引 `prometheus_client`/`psutil` | **建议**：**人工导入快照优先**，只读解析既有指标文本；不新起 exporter、不新增依赖；快照 manifest 带来源/采样时间/聚合窗口/exporter 版本 | 架构师 |
| Q5 | 分组统计用分层还是聚类 Bootstrap？重复 `seed` 的聚类键？ | **建议**：按 `(engine, scenario, fault_seed)` 聚类、场景内分层，标准库实现；聚类键与 `run_baseline.py:273-276` `group_keys` 对齐；具体重采样次数由架构师在设计冻结 | 架构师 |
| Q6 | 指标排除规则中「无可观测故障」「注入失败」如何判定？ | **建议**：以 `benchmarks/faults/injector.py:58` `InjectionResult` 的注入状态 + `core/evidence/models.py:56` `collection_status` 判定，二者皆 `NOT_SUPPORTED`/`UNKNOWN` 才计入「无可观测故障」；判定条件写成可断言谓词，不做主观打分 | 架构师 |
| Q7 | SFT 小试跑是否纳入 W10→W11 门禁？ | **建议不纳入门禁**（《总览》§五「训练控制：模型训练不得占用 Gateway/审批/Verifier/恢复等 P0 资源」）；仅作条件性记录，**无可重复收益保留 Baseline**，DPO/14B/RL 推迟至 Alpha 后 | 主理人 |
| Q8 | 故障矩阵「扩展」是否超出 W11 D3 六类注入清单？ | **建议**：W10 只扩展**评测侧元数据**（`fault family` 标签），不新增生产故障注入类型；网络分区/磁盘满等（`docs/w08/08-drill-report.md:167`）是否纳入 W11 D3 **待主理人拍板** | 主理人 |

## 8. 上游遗留映射表

> 任务书所指「`docs/w07/07-gate-review.md` §6 遗留项」：该文件**当前版本 §6 为「对外表述禁令」**（`:129-137`），遗留项实际分布在 **§5「W08 前置项」、§3「未验证项 NV」、§4「已知偏离 KD」、§4.3「残留事实」**。本表按**真实章节位置**列出属于本周（W10）承接的部分；`docs/w08/08-drill-report.md` §5「未执行项」按原编号列出。W09 已承接项不再重复实现，仅标注依赖。

### 8.1 W07 门禁复核遗留（`docs/w07/07-gate-review.md`）

| # | 遗留项（真实位置） | 本周（W10）落点 / 后续落点 |
|---|---|---|
| L1 | §5 W08-5 / §3.1 NV-1：容器运行态（build + `compose up worker`）**NOT_VERIFIED** | W10 不承接；落 **W11 D4** 长稳前提、**W12 D3** 部署包/RC 实跑；环境镜像 digest 是本周 `Run Manifest` 必填字段（P0-W10-01），但**不声称容器运行态已验证** |
| L2 | §3.2 NV-2：POSIX `SIGTERM`/`SIGINT` 真实信号路径未验证 | 落 **W11 D4** 长稳/恢复演练；本周 Q2 登记为环境阻塞 |
| L3 | §3.2 NV-4：`test_worker_gateway.py` 本机 admin 1045 未跑通 | 落 **W11 D2** 并发回归如实归因；本周 Q2 / P0-W10-05 环境故障归因承接（不计入模型失败） |
| L4 | §4 KD-1：`api` 双网、隔离为账号级非网络级 | 落 **W12 D3/D4** 部署文档如实标注；本周 `Run Manifest.env_image_digest` 不得写成「网络级隔离已验证」 |
| L5 | §4 KD-3 / KD-4 / §4.3：环境非「可复现清零」；`environment-manifest.*.json` 不入库；**7 条残留**不得表述为「已清理」 | 本周 §0/§6/Q2 如实登记；`oracle_isolation`/数据切分不得借「环境可清理」假设；**W12 D4/D5** 发布说明保留该事实 |
| L6 | §4 KD-5：W07 冻结四件套缺 `05-freeze.md`/`06-conformance-audit.md`（已核实不存在） | W10 的冻结资产以 `benchmarks/baseline_v0/` 6 个文件 + `tests/unit/test_baseline_manifest.py` 为基线，**不借用 W07 冻结四件套**；W12 D4 文档完整性复核 |
| L7 | §6「对外表述禁令」本身（`:129-137`） | **贯穿 W10–W12**；本周 §0/§6 已按「有条件放行 / NOT_VERIFIED / 未清理 / 只读复核」口径书写 |

### 8.2 W08 演练报告未执行项（`docs/w08/08-drill-report.md` §5）

| # | 未执行项（§5 原编号） | 本周（W10）落点 / 后续落点 |
|---|---|---|
| M1 | §1.3 第 4 条（`:76-78`）：`scanned` 口径，真实扫描总数需 W09 扩展 `sweep()` | **已由 W09 承接**（`docs/w09/01-prd.md` P0-W09-06 / AC-20）；本周 W10 指标口径冻结时**引用**其 `scanned_total`，不重复实现 |
| M2 | §5：`--once` 退出码未断言固定值（`:164`） | 落 **W11 D4** 长稳/回归脚本（明确样本量与运行时长） |
| M3 | §5：PostgreSQL 相关用例未执行（`:165`，5432 拒绝连接） | **本周直接承接归因**：P0-W10-05 环境故障分类、§0/Q2 登记；W11 D1/D2 跨引擎回归继续如实归因、不修 |
| M4 | §5：控制库残留清理部分未执行（`:166`，admin 1045、只追加表无 DELETE） | 本周 §0/Q2 登记；落 **W11/W12** 限制清单（与 L5 合并） |
| M5 | §5：7 类注入之外的网络分区/磁盘满未执行（`:167`，不在 D5 清单） | 本周 Q8 登记「是否扩展待主理人拍板」；落 **W11 D3**（任务书 W11 D3 仅列 6 类注入） |

> 本表为「如实登记 + 给落点」，**不代表本周已修复或已验证**。W10 对 M3 承担「归因口径」责任，对 M1 承担「引用 W09 结果」责任，其余为跨周承接登记。
