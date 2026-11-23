# W12 增量 PRD｜Alpha 验收与发布（把功能、安全、可复现性和限制一起交付，而不是只做一个演示）

> 本文是 V2.2 12 周计划的第 12 周**增量需求基线**，供架构设计与实现使用。
> 口径约定：§4「现状」引用的 `file:line` 均为**已核实的既存文件**；§4/§5「验收位置 / 测试位置」中的测试文件为**本周待新建**，路径按 `tests/unit/`、`tests/integration/`、`docs/w12/` 既有命名约定给出，非既存文件。二者不得混读。

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.13/3.14 + FastAPI + MySQL 8 控制库 + MySQL LAB 目标库 + PostgreSQL MCP 只读（**延续 V2.2 既有栈；不新增前端、不新增第三方依赖，沿用 `requirements.lock.txt`**） |
| Project Name | `dbguardian_w12_alpha_acceptance_release` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W12_Alpha 验收与发布.docx` §一–§六；`项目规划/项目规划2.2/DBGuardian_12周研发计划_总览与门禁.docx` §二/§三/§四/§五/§六；上游 `docs/w11/01-prd.md`（RC 冻结与安全红线）、`docs/w10/01-prd.md`（指标冻结/Baseline）、`docs/w09/01-prd.md`（脱敏/报告/记忆）、`docs/w08/01-prd.md`、`docs/w08/02-design.md`、`docs/w08/08-drill-report.md`、`docs/w07/07-gate-review.md` GR-0；代码基线（已核实）`apps/api/main.py`、`deploy/`、`docker-compose.yml`、`database/migrations/`、`scripts/`、`docs/adr/`、`README.md`、`.env.example` |
| 前置门禁 | **依赖 W11 门禁**（W11→W12 三条：安全负面集通过 / 状态恢复·跨租户·备份恢复均有报告 / RC 冻结与端到端回归，见 `docs/w11/01-prd.md` §6），以及 W11 `publish_decision`（`ALPHA_ALLOWED` / `TECH_PREVIEW_ONLY`）。W09、W10、W11 门禁**均尚未裁定**；**根依赖 W08 门禁仍「尚在复核、未裁定」**（`docs/w08/06-qa-report.md`、`docs/w08/07-gate-review.md` 均未落盘；W07 门禁为 GR-0「有条件放行」）。**不得**写成「W08/W09/W10/W11 已通过」 |
| 前置依赖（环境阻塞，如实登记） | ① PostgreSQL 5432 拒绝连接（`docs/w08/08-drill-report.md:165`）；② `DG_ADMIN_DSN`（root）认证失败 1045、只追加表无 `DELETE` 授权（`docs/w08/08-drill-report.md:166`、`docs/w07/07-gate-review.md:111`）；③ 控制库历史任务堆积（主理人任务书登记 830+ 行，逐条见 `docs/w07/worker-residue.md`）；④ 容器运行态仍 `NOT_VERIFIED`（`docs/w07/07-gate-review.md:78` NV-1）；⑤ 模型环境 `BLOCKED_MODEL_ENV_MISSING`、`results.json` 仍 `NOT_RUN`（`benchmarks/baseline_v0/run_baseline.py:140-145/:344`）。以上均写入**已知限制清单**，不因发布而消失 |
| 原始需求复述 | 任务书「本周目标」原句：「完成 DBGuardian Alpha 验收、部署包、演示流程、模型 Baseline 报告、接口/安全/数据/Benchmark 文档与已知限制清单。」 |

## 1. 产品目标

**一句话**：把 W01–W11 的交付物收口为「**以冻结测试集和各类报告为准、可复现部署、限制清单诚实、按 W11 安全裁定决定发 Alpha 还是技术预览**」的一次发布，而不是一场演示。

三个正交目标（判定口径均为可验证事实）：

| # | 目标 | 判定口径（可机器验证） |
|---|---|---|
| G1 | **功能验收以冻结集为准** | 任务创建/查询/暂停/恢复/取消 + 事件与工具调用可追溯；PG 与 MySQL LAB 分别连接、引擎/版本/能力/权限识别与证据缺失提示；三类只读诊断报告含 `evidence_id`/支持证据/反证/未知项/适用范围；至少一个引擎完成经人工审批的有限 LAB 修复并经独立 Verifier 验证，另一引擎无写闭环时**保持只读**。逐条落到测试断言，不以「演示通过」代替 |
| G2 | **安全可靠性逐条复核** | W11 安全负面集与恢复报告逐条复核；任何未审批/篡改/过期/重放/环境变化的 R2 操作被拒；`R3/R4` 不进 Agent 执行器、`R4` 不注册工具；未知写结果进 `NEEDS_RECONCILIATION`/`MANUAL_INTERVENTION` 不自动重试；审计失败时 R2 `fail closed`；敏感数据不进模型上下文/普通日志/训练集；知识条目审核后才进正式索引 |
| G3 | **交付物完整、限制诚实** | 部署包、OpenAPI 3.1/DTO/Tool JSON Schema/错误码/状态机合同/迁移说明、威胁模型/RBAC/风险表/审批/审计/脱敏/Fault Manifest/Oracle 隔离/数据切分报告、冻结 Baseline 版本与 Benchmark 结果、已知限制清单逐项存在；发布对象的 `commit`/镜像 digest/制品 hash 有记录；对未过门禁能力明确标 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`；**不承诺**生产自动写入等六类能力（清单见 §2） |

## 2. 本周边界（先划死，防止范围蔓延）

| 边界 | 内容 |
|---|---|
| 做什么 | ① 功能验收清单；② 安全与可靠性验收（逐条复核 W11）；③ 部署包与可复现演示；④ 文档、模型与限制清单；⑤ RC 最终构建/安装·启动·停止·升级·回滚检查、Alpha 验收报告、发布与复盘 |
| 写能力 | **不扩大写能力**。唯一 R2 动作仍是 `mysql_lab_update_order_note` / `1.0`（`adapters/mysql/lab_repair.py`；`docs/w08/02-design.md` §2.3 列其 `TOOL_NAME`/`ALLOWED_*`/`tool_version="1.0"`/三条断言为零改动）；演示的审批修复复用该动作，不新增工具 |
| 引擎写路径 | **不做第二引擎写路径、不做 PostgreSQL 写路径**；PG 保持 MCP 只读。另一引擎（PG）如无写闭环**保持只读**，不得为演示临时开写 |
| 第三方依赖 | **不引入新的第三方依赖**；部署与演示复用既有 `deploy/`、`docker-compose.yml`、`scripts/`；OpenAPI 由 FastAPI 既有能力（`apps/api/main.py:91-93`）导出，不引入新文档框架 |
| 安全机制 | **不绕过审批 / 证据 / 审计 / 验证机制**；演示**使用隔离 LAB，不连接生产数据库**；不得为「展示效果」临时放宽风险、审批、只读约束或采信模型自报成功 |
| 如实返回状态 | 未通过门禁的能力标 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`；PG 侧受 5432 阻塞的演示段标 `NOT_EXECUTED`；`results.json` 未真实运行保持 `NOT_RUN`/`scores=null`（`benchmarks/baseline_v0/run_baseline.py:342-354`），报告**不写「预计/估计」充当实测**；RPO/RTO 未实测标「建议目标」（W11 AC-13） |
| 前端 | **不新增前端交付项**；演示以 API/CLI + 结构化报告为准 |
| 训练（非主线） | **确认训练收益而非推进训练**：如 SFT 无可重复提升，**保留未微调 Baseline**；DPO / 14B / RL **推迟至 Alpha 后**（任务书 §二 D5 / §五、《总览》§五）。不得因发布压力强行升级模型 |
| 不承诺能力（写入限制清单） | 生产自动写入、通用故障自动修复、所有场景必然诊断成功、完整死锁/索引评估、跨系统 Exactly Once、网络级隔离已验证（`docs/w07/07-gate-review.md:101` KD-1 为账号级隔离）。**不把建议、部分证据或未经验证的修复表述为成功** |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 主理人（发布决策） | 作为主理人，我要验收以冻结测试集与功能/安全/恢复报告为准，而不是演示效果；若 W11 裁定 `TECH_PREVIEW_ONLY`，发布物必须自动降级为技术预览并列出限制，我不会在「按时发 Alpha」和「删安全门禁」之间被逼选择。 |
| U2 | 交付 / 运维工程师 | 作为交付工程师，我要拿到 Compose、网络隔离、`.env.example`、Secret 注入说明、PG/MySQL LAB 镜像、初始化脚本与迁移，并能完成安装/启动/停止/升级/回滚五项检查，记录 commit、镜像 digest 与制品 hash。 |
| U3 | 安全评审 | 作为安全评审，我要逐条复核 W11 负面集与恢复报告，确认 R3/R4 不进执行器、只读角色无法被模型/SQL 注释/MCP 参数/UI 绕过、未知结果不盲目重试、审计失败 R2 fail closed，这样我才认可发布包的安全口径。 |
| U4 | 审计员 / 客户 | 作为审计员，我要一份明确的已知限制清单（含生产自动写入不支持、PG 5432 未验证、容器运行态 NOT_VERIFIED、Baseline 未上机、控制库残留未清理、账号级非网络级隔离），且未过门禁能力带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` 标记。 |
| U5 | 值班 SRE（演示执行者） | 作为演示执行者，我要一份隔离 LAB 的端到端脚本：双引擎只读诊断、单引擎审批修复、Verifier 独立验证、未知结果恢复演练，且脚本不连接生产库、不把建议写成已修复。 |
| U6 | 评测 / 文档工程师 | 作为文档工程师，我要交付 OpenAPI 3.1、Pydantic DTO、Tool JSON Schema、错误码、状态机合同、迁移说明与冻结 Baseline 报告；若未微调，明确写明「未微调」，不把 `NOT_RUN` 包装成基线成绩。 |

## 4. 需求池

> **P0 为最终 Alpha 门禁必需，不得降级、不得延期**。每条 P0 给出 `[D#]`、已核实现状、可实施要求、**验收位置（测试文件路径 + 断言形式）**。
> `file:line` 现状引用已用 Grep/Read 核实；「当前无该能力」以全仓检索无对应交付物为依据（`docs/w12/` 目录本周前不存在；OpenAPI 独立交付文档、Alpha 验收报告、已知限制清单、发布版本标签均无既存实现）。

### P0（Must have，6 条）

- **P0-W12-01** `[D1]` **功能验收清单（以冻结测试集为准）。**
  - 现状（已核实）：`core/tasks/state_machine.py`、`core/tasks/service.py`、`apps/api/routes/tasks.py` 已有任务生命周期；`core/evidence/models.py:102-111` 有 `ReportFacts`（`supporting_evidence_ids`/`contradicting_evidence_ids`/`missing_evidence`）；`benchmarks/faults/manifests/` 6 个故障清单覆盖 `mysql/postgresql × slow_query/connection_pressure/lock_wait`（`benchmarks/baseline_v0/run_baseline.py:46-48`）；`core/verification/verifier.py`（W08）为独立验证入口；`adapters/mysql/lab_repair.py` 为唯一 R2 动作。**全仓无 W12 功能验收清单、无「双引擎分别连接 + 能力/权限识别 + 证据缺失提示」的验收件**。
  - 要求：
    1. 验收任务创建、查询、暂停、恢复、取消；确认事件与工具调用可追溯。
    2. PG 与 MySQL LAB 分别连接，验收引擎/版本/能力/权限识别与**证据缺失提示**；PG 侧受 5432 阻塞段记 `NOT_EXECUTED`（不判 FAIL、不冒充通过）。
    3. 慢查询、连接压力、锁等待三类只读诊断报告含 `evidence_id`、支持证据、反证、未知项、适用范围（W09 `ReportFacts` 口径）。
    4. 至少一个引擎完成经人工审批的有限 LAB 修复与独立 Verifier 验证；另一引擎无写闭环时**保持只读**。
  - **验收位置**：`tests/integration/test_w12_functional_acceptance.py`（新建）——① 五态生命周期各 1 例且 `task_events` 可追溯；② 三类诊断报告五字段（evidence_id/支持/反证/未知/适用范围）非空率 ==100%；③ 审批修复闭环 `verification_result.verdict == PASS` 且 `tool_calls` 计数 ==1；④ PG 段 `status=="NOT_EXECUTED"` 时该段不判 FAIL；⑤ 第二引擎 `write_tools() == []`。

- **P0-W12-02** `[D2]` **安全与可靠性验收（逐条复核 W11）。**
  - 现状（已核实）：`core/security/policy.py:216` `R4_NOT_EXECUTABLE`、`:223` `R3_AGENT_EXECUTION_FORBIDDEN`、`:230-235` `R2_REQUIRES_APPROVAL`；`core/models/gateway.py:226-239` 工具注册/风险/写授权校验；`core/tools/executor.py:452/:495` 回执缺失→`UNKNOWN_OUTCOME`、证据缺失→结果未知；`docs/adr/ADR-0006-audit-integrity-and-worm.md:62/:105` 审计失败 R2 阻断；`docs/w11/01-prd.md`（W11 负面集与恢复报告为本周输入，尚未落盘）；`docs/w09/01-prd.md`（脱敏/知识审核门禁）。
  - 要求：
    1. 逐条复核 W11 安全负面集与恢复报告；任何未审批、篡改、过期、重放或环境变化的 R2 操作**必须被拒绝**。
    2. 确认 `R3/R4` 不进入 Agent 执行器、`R4` 不注册工具；PG/MySQL 只读角色无法被模型、SQL 注释、MCP 参数或 UI 绕过。
    3. 确认未知写结果进入 `NEEDS_RECONCILIATION`/`MANUAL_INTERVENTION` 不自动重试；审计失败时 R2 `fail closed`。
    4. 确认敏感数据不进模型上下文、普通日志、训练集；知识条目经审核后才进正式索引（W09 AC-13）。
  - **验收位置**：`tests/integration/test_w12_security_acceptance.py`（新建）——① W11 负面集每条复核结果 `denied is True`；② `R4` 在 `registry.names()` 中不存在、`R3` 执行请求 → `R3_AGENT_EXECUTION_FORBIDDEN`；③ 四类绕过（模型/SQL 注释/MCP 参数/UI）各 1 例 → 只读角色写入被拒；④ UNKNOWN → `NEEDS_RECONCILIATION` 且无 `TOOL_CALL_RETRIED`；⑤ 审计失败 + R2 → `tool_calls_delta==0`。

- **P0-W12-03** `[D3]` **部署包与可复现演示。**
  - 现状（已核实）：`deploy/Dockerfile.api`、`deploy/Dockerfile.worker`、`deploy/init/pg_lab_bootstrap.sh`、`deploy/network/README.md`、`deploy/environment-manifest.example.json` 已存在（Glob 确认）；`docker-compose.yml:102`（api `DG_MODEL_PROVIDER`）、`:125`（worker 绝不接入 `model_net`）、`:145`（worker `disabled`）、`:156-162`（`tool_gateway` 占位，KD-2）；`scripts/migrate.py`、`scripts/bootstrap_mysql.py`、`scripts/bootstrap_pg_lab.py`、`scripts/environment_manifest.py` 已存在；`database/migrations/001..008` + `rollback/` 已存在；**容器运行态仍 `NOT_VERIFIED`**（`docs/w07/07-gate-review.md:78` NV-1，`:77` 仅 `docker compose config` 层实测通过）。
  - 要求：
    1. 整理 Compose、网络隔离、`.env.example`、Secret 注入说明、PG/MySQL LAB 镜像、初始化脚本和迁移；整理 API/Worker/Web、Policy、Gateway、PG MCP、MySQL Adapter、Verifier、Benchmark Runner 的部署说明。
    2. 准备演示脚本：双引擎只读诊断、单引擎审批修复、Verifier 独立验证、未知结果恢复演练；**使用隔离 LAB，不连接生产数据库**。
    3. RC 最终构建 + 安装/启动/停止/升级/回滚五项检查，记录 `commit`、镜像 digest、制品 hash（缺失项记 `UNKNOWN` 并登记，不臆造）。
    4. 容器运行态实跑（NV-1 关闭路径）：`docker build -f deploy/Dockerfile.worker` + `compose up worker` + 容器内 `--self-check` 退出码 0；**未跑通则如实保持 `NOT_VERIFIED` 并写入限制清单**，不得表述为「容器网络隔离已验证」。
  - **验收位置**：`tests/integration/test_w12_deploy_package.py`（新建）——① 部署清单必填文件集合逐项 `exists is True`；② `.env.example` 不含真实口令（`SecretStr` 占位）；③ 五项检查各 1 条结果（PASS / `NOT_EXECUTED` + 原因）。`tests/integration/test_w12_demo_script.py`（新建）——① 演示脚本四段各 1 例且 `lab_only is True`、`production_dsn_referenced is False`；② 演示报告不出现「已修复」措辞描述「建议」。`docs/w12/deployment-guide.md`（新建）。

- **P0-W12-04** `[D4]` **文档、模型与已知限制清单。**
  - 现状（已核实）：`apps/api/main.py:91-93` FastAPI `create_app()` 带 `title="DBGuardian API"`、`version=__version__`（OpenAPI 自动生成基础）；`docs/adr/ADR-0005`（canonicalization/action digest）、`ADR-0006`（audit integrity & WORM）、`ADR-0007`（single-engine lab repair）、`ADR-0001`–`0004` 已存在；`docs/baseline_v0.md`、`docs/capability-matrix.md`、`docs/diagnosis_flows.md`、`docs/security/w05-security-tests.md`、`docs/security/identity-and-policy.md`、`docs/migrations.md`、`docs/cleanup-inventory.md`、`docs/dev-guidelines.md` 已存在；`README.md:124` 已有验收状态表；`.env.example:33-36` 说明 `model_provider` 三态；`benchmarks/baseline_v0/results.json` 当前 `NOT_RUN`（`run_baseline.py:344`）。**全仓无 OpenAPI 3.1 独立交付件、无冻结 Baseline 发布报告、无已知限制清单**。
  - 要求：
    1. 交付 OpenAPI 3.1、Pydantic DTO、Tool JSON Schema、错误码、状态机合同、数据库迁移说明。
    2. 交付威胁模型、RBAC 矩阵、风险表、审批协议、审计与脱敏策略、Fault Manifest、Oracle 隔离、数据切分报告。
    3. 交付冻结 Baseline 的模型版本、Prompt/Tool/Schema/Harness、推理配置与 Benchmark 结果；**若未微调，明确写明「未微调」**；`NOT_RUN` 不得包装成基线成绩。
    4. 列出**不承诺能力**：生产自动写入、通用故障自动修复、所有场景必然诊断成功、完整死锁/索引评估、跨系统 Exactly Once、网络级隔离已验证；对未过门禁能力标 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`。
  - **验收位置**：`tests/unit/test_w12_doc_inventory.py`（新建）——① 四类文档清单（接口合同 / 安全材料 / 数据与 Benchmark / 限制清单）逐项 `exists is True`；② OpenAPI 导出 `openapi` 主版本为 `3.1`；③ `results.status=="NOT_RUN"` → 文档 `baseline_score is None` 且含「未微调 / 指标未测得」措辞；④ 六类不承诺能力逐项在限制清单中且带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` 标记。`docs/w12/known-limitations.md`（新建）。

- **P0-W12-05** `[D5]` **发布与复盘（按 W11 裁定决定 Alpha / 技术预览）。**
  - 现状（已核实）：`docs/w11/01-prd.md` P0-W11-06 定义 `publish_decision ∈ {ALPHA_ALLOWED, TECH_PREVIEW_ONLY}` 纯函数判定（尚未落盘/裁定）；《总览》§四「安全负面集任一红线失败都阻止完整 Alpha 发布；只能发布明确标注限制的技术预览」；W12 任务书 §六最终门禁原文。**全仓无 Alpha 验收报告、无发布版本标签、无复盘记录**。
  - 要求：
    1. 生成 Alpha 验收报告：功能、安全、可靠性、性能、模型收益、成本、已知问题和延期项。
    2. **确认训练收益**：如 SFT 无可重复提升，保留未微调 Baseline；DPO/14B/RL 推迟至 Alpha 后；不得因发布压力强行升级模型。
    3. 发布 Alpha 包**或**技术预览包：发布类型严格由 W11 `publish_decision` 决定；`TECH_PREVIEW_ONLY` 时**不得**出现「完整 Alpha」字样，且限制清单随包发布。
    4. RC 最终构建记录 commit / 镜像 digest / 制品 hash；对未通过门禁的能力明确标记 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`。
  - **验收位置**：`tests/unit/test_w12_publish_gate.py`（新建）——① `publish_decision=="TECH_PREVIEW_ONLY"` → `release_type=="TECH_PREVIEW"` 且 `release_name` 不含 `Alpha`（大小写不敏感）；② `ALPHA_ALLOWED` → `release_type=="ALPHA"`；③ 断言 `release_type` 只由 `publish_decision` 派生、签名无 `force_alpha`/`override` 形参；④ 验收报告七类章节（功能/安全/可靠性/性能/模型收益/成本/已知问题）齐全。`docs/w12/alpha-acceptance-report.md`（新建）。

- **P0-W12-06** `[D4/D5 补强]` **「演示 ≠ 验收」与「限制不可删除」的机器校验。**
  - 现状（已核实）：W12 任务书 §五列三项主要风险：「把演示通过等同于产品验收」「训练未达预期仍强行升级模型」「某项安全门禁未过但仍标 Alpha」；`docs/w07/07-gate-review.md:129-137` §6 对外表述禁令（禁「W07 已收口 / 门禁通过 / 放行」「容器网络隔离已验证」「环境已清理」「QA 独立实跑通过」等）。
  - 要求：
    1. 验收报告必须引用冻结测试集与 W11/W10/W09 门禁证据；**不得**以「演示脚本跑通」作为任一 AC 的通过依据。
    2. 限制清单由 W11 `publish_decision` 与已知环境阻塞（§0 五项）自动生成基础项，**不可人工删除**；人工只能追加。
    3. 全部对外材料遵守 `docs/w07/07-gate-review.md:129-137` 表述禁令：W07 结论写「有条件放行」、容器写「配置层已实测 / 运行态 `NOT_VERIFIED`」、残留写「存在 7 条已登记残留」、QA 写「只读数据复核通过（依赖工程师实跑产物）」。
  - **验收位置**：`tests/unit/test_w12_claims.py`（新建）——① 扫描 `docs/w12/` 全部文本 → 不含禁用表述集合（`已收口`/`门禁通过`/`容器网络隔离已验证`/`已清理`/`QA 独立实跑通过`）；② 验收报告 `evidence_refs` 集合含 W11/W10/W09 门禁件路径且非空；③ 限制清单基础项集合为自动项超集（人工删除任一项 → `LIMITATION_REMOVED` 错误）；④ `demo_passed` 字段不出现在任何 `ac_result` 判定输入中。

### P1（本周期内可做，不进门禁）

- **P1-W12-01** `[D3]` 演示脚本的一键 `scripts/demo_alpha.py` 封装（只读调用既有脚本）。**AC**：`tests/unit/test_w12_demo_cli.py`（新建）`--lab-only` 为默认且不可关闭。
- **P1-W12-02** `[D4]` 错误码表与状态机合同的机器生成（从既有 `core/errors.py`/`core/tasks/state_machine.py` 导出）。**AC**：`tests/unit/test_w12_contract_export.py`（新建）导出条目数与源码枚举数相等。
- **P1-W12-03** `[D5]` 升级/回滚检查的 dry-run 记录归档。**AC**：`docs/w12/upgrade-rollback-check.md`（新建）存在且五项检查结果逐条有值或 `NOT_EXECUTED`。

### P2（明确延期，不进 W12 门禁）

- **P2-W12-01** DPO / 14B / RL 与复杂多 Agent（《总览》§五）、高级 UI、更多故障场景。
- **P2-W12-02** 生产环境部署（**明确不承诺**，Alpha 仅隔离 LAB）；真实 WORM/对象锁接入与 `dg_ctl` 账号拆分（W11 P2 延续）。
- **P2-W12-03** 跨系统 Exactly Once。

## 5. 验收标准 AC（可机器验证）

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | 任务生命周期可追溯 | `tests/integration/test_w12_functional_acceptance.py`（新建） | 创建/查询/暂停/恢复/取消五态各 1 例；每例 `task_events` 可查询且 `seq` 连续 |
| AC-02 | 三类诊断报告字段 | 同上 | 三类报告 `evidence_id`/`supporting_evidence_ids`/`contradicting_evidence_ids`/`missing_evidence`/`scope` 非空率 ==100% |
| AC-03 | 审批修复闭环独立验证 | 同上 | `verification_result.verdict == PASS`；`tool_calls == 1`；目标行变化 ==1 次 |
| AC-04 | 第二引擎保持只读 | 同上 | 无写闭环引擎 `write_tools() == []`；PG 段 `NOT_EXECUTED` 不判 FAIL |
| AC-05 | W11 负面集逐条复核 | `tests/integration/test_w12_security_acceptance.py`（新建） | 每条 `denied is True`；复核结果数与 W11 冻结集数相等 |
| AC-06 | R3/R4 不进执行器 | 同上 | `"R4" not in registry.risk_levels_registered`；R3 执行请求 → `R3_AGENT_EXECUTION_FORBIDDEN` |
| AC-07 | 只读角色四类绕过被拒 | 同上 | 模型 / SQL 注释 / MCP 参数 / UI 各 1 例 → 写入被拒且目标行不变 |
| AC-08 | 未知结果不重试 + fail closed | 同上 | UNKNOWN → `NEEDS_RECONCILIATION`、无 `TOOL_CALL_RETRIED`；审计失败 + R2 → `tool_calls_delta==0` |
| AC-09 | 敏感数据不越界 | 同上 + `tests/integration/test_w09_redaction_pipeline.py`（既存，W09 门禁） | 模型上下文/普通日志/训练集三处不含敏感样本；知识未审核 `is_indexable is False` |
| AC-10 | 部署清单完整 | `tests/integration/test_w12_deploy_package.py`（新建） | 必填文件集合逐项 `exists is True`；`.env.example` 不含真实口令 |
| AC-11 | 五项检查与制品记录 | 同上 | 安装/启动/停止/升级/回滚各 1 条结果；`commit`/`image_digest`/`artifact_hash` 有值或 `UNKNOWN` + 登记 |
| AC-12 | 演示隔离且措辞诚实 | `tests/integration/test_w12_demo_script.py`（新建） | `lab_only is True`、`production_dsn_referenced is False`；演示报告不出现「已修复」描述「建议」 |
| AC-13 | 文档清单齐全 | `tests/unit/test_w12_doc_inventory.py`（新建） | 四类文档逐项 `exists is True`；OpenAPI 主版本 `== "3.1"` |
| AC-14 | Baseline 不包装 | 同上 | `results.status=="NOT_RUN"` → `baseline_score is None` 且文档含「未微调 / 指标未测得」 |
| AC-15 | 不承诺能力清单 | 同上 | 六类不承诺能力逐项在限制清单且带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` 标记 |
| AC-16 | 发布类型由裁定派生 | `tests/unit/test_w12_publish_gate.py`（新建） | `TECH_PREVIEW_ONLY` → `release_type=="TECH_PREVIEW"` 且名称不含 `Alpha`；`ALPHA_ALLOWED` → `ALPHA` |
| AC-17 | 判定不可人工覆盖 | 同上 | 签名无 `force_alpha`/`override`；验收报告七类章节齐全 |
| AC-18 | 训练收益诚实 | 同上 | `sft_reproducible_gain is False` → `model == baseline` 且 `dpo/14b/rl` 全 `deferred is True` |
| AC-19 | 表述禁令 | `tests/unit/test_w12_claims.py`（新建） | `docs/w12/` 全文不含 5 条禁用表述集合 |
| AC-20 | 演示不等于验收 | 同上 | `evidence_refs` 含 W11/W10/W09 门禁件路径且非空；`demo_passed` 不在任何 `ac_result` 输入中 |
| AC-21 | 限制清单不可删 | 同上 | 自动基础项集合为超集；人工删除任一项 → `LIMITATION_REMOVED` |
| AC-22 | 写面与引擎不回退 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py`（既存） | PG 工具集 7 个全 `read`；write 工具仍 `LAB+mysql` 单一；无第二写动作 |

## 6. 周末门禁与完成定义

**最终 Alpha 门禁（浓缩任务书 §六 / 《总览》§二 W12 行，逐条可判）**：
1. **双引擎只读诊断稳定**：AC-01～AC-04 全绿（PG 侧受 5432 阻塞段按 `NOT_EXECUTED` 如实登记，不冒充通过）。
2. **至少一类受审批的 LAB 修复闭环通过独立验证**：AC-03 全绿；另一引擎未完成写闭环时保持只读（AC-04）。
3. **安全负面集、状态恢复、跨租户和备份恢复有报告**：AC-05～AC-09、AC-19～AC-22 全绿；逐条复核 W11 报告，任一未审批/篡改/过期/重放/环境变化的 R2 被拒。
4. **交付物完整、限制诚实**：AC-10～AC-18、AC-20～AC-21 全绿；源代码、部署、接口合同、安全材料、数据、Benchmark、模型版本与验收报告齐全；明确 Alpha 不支持生产自动写入；不把建议、部分证据或未经验证的修复表述为成功。

**未通过处置（按任务书 §六 / §五 / 《总览》§四，写死）**：
- **否则发布技术预览，不标记完整 Alpha**：`publish_decision == TECH_PREVIEW_ONLY`（W11 P0-W11-06）或上述任一条未满足 → `release_type == TECH_PREVIEW`，限制清单随包发布，`release_name` 不含「Alpha」（AC-16）。
- **某项安全门禁未通过但仍标 Alpha → 降级为技术预览并列出限制，不得删除安全要求**（任务书 §五）。`release_type` 只由 `publish_decision` 派生，人工不可覆盖（AC-17）。
- **不得把演示通过等同于产品验收**：以冻结测试集、功能/安全/恢复报告为准（AC-20）；不得为展示效果临时放宽风险、审批、只读约束或连接生产库。
- **训练未达预期仍强行升级模型 → 保留 Baseline**，DPO/14B/RL 推迟至 Alpha 后（AC-18）。
- **W11 门禁未过** → 先回 W11 修红线，W12 不得准备完整 Alpha 包；**W10 门禁未过** → 指标/Baseline 文档不得引用未冻结口径；**W09 门禁未过** → 脱敏/知识审核相关验收暂停；**W08 门禁复核最终未通过**（当前尚在复核）→ 沿用 `docs/w08/` 门禁件处置缩小修复范围或仅发布诊断预览，W12 不得替代上游门禁证据。
- 已知环境阻塞（PG 5432 / admin 1045 / 控制库残留 / 容器运行态 `NOT_VERIFIED` / 模型环境 `BLOCKED_MODEL_ENV_MISSING`）**全部写入已知限制清单**，不因发布而消失；不得表述为「环境已可复现清零」「容器网络隔离已验证」（`docs/w07/07-gate-review.md:129-137`）。

**本周完成定义（任务书 §六）**：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

## 7. 待确认问题 Q

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | **W08 门禁仍「尚在复核、未裁定」**（`docs/w08/06-qa-report.md`、`07-gate-review.md` 未落盘），且 W09/W10/W11 门禁均未裁定，W12 能否先做？ | **建议**：先做不依赖裁定的工作（部署清单整理、文档骨架、限制清单结构、演示脚本隔离检查）；**不宣布 W12 开工、不预写 Alpha 验收结论、不预写 `publish_decision`**；最终门禁签署等全部上游件落盘后补做 | 主理人 + QA |
| Q2 | 已知环境阻塞（PG 5432 / `DG_ADMIN_DSN` root 1045 / 控制库 830+ 历史行、只追加表无 DELETE / 容器运行态 `NOT_VERIFIED` / 模型 `BLOCKED_MODEL_ENV_MISSING`）如何影响发布？ | **建议**：五项**全部写入 `docs/w12/known-limitations.md`** 并带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` 标记；PG 演示段 `NOT_EXECUTED`；`baseline_score` 保持 `null`；容器 digest 记 `UNKNOWN`（Q3）。不为发布而修环境或放宽 `dg_ctl` | 主理人 |
| Q3 | NV-1 容器运行态（`docs/w07/07-gate-review.md:78`）本周是否必须实跑关闭？ | **建议**：列入 P0-W12-03 的实跑路径（`docker build` + `compose up worker` + `--self-check`）；**未跑通则如实保持 `NOT_VERIFIED` 并写限制清单**，不表述为「容器网络隔离已验证」；是否把「NV-1 关闭」设为发布硬门槛由主理人拍板 | 主理人 |
| Q4 | W11 `publish_decision` 尚未落盘/裁定，W12 发布类型如何定？ | **建议**：发布类型**只由 W11 纯函数裁定派生**（AC-16/17），W12 不自行判断；若裁定缺失，默认按 `TECH_PREVIEW_ONLY` 处理并登记为问题，不默认发 Alpha | 主理人 |
| Q5 | 冻结 Baseline 仍 `NOT_RUN`（`benchmarks/baseline_v0/run_baseline.py:344`）、未微调，模型收益章节怎么写？ | **建议**：明确写「未微调 Baseline / 指标未测得（模型环境 `BLOCKED_MODEL_ENV_MISSING`）」，`baseline_score is None`；**不填任何估计值**；训练收益按 W10 D5 条件判定，无可重复提升保留 Baseline | 主理人 |
| Q6 | 版本标签 / 制品 hash / 镜像 digest 的取值来源？ | **建议**：以 `git rev-parse`（不写死 hash）+ `docs/migrations.md` + 迁移文件为版本权威（`docs/w07/07-gate-review.md:104` KD-4：环境清单不入库）；缺失项记 `UNKNOWN` 并登记，不臆造 digest | 架构师 |
| Q7 | 演示是否需要 Web 控制台？ | **建议不需要**：延续 W06/W07/W08/W09/W10/W11「无前端交付项」界定，演示走 API/CLI + 结构化报告；高级 UI 属《总览》§五延期项 | 主理人 |
| Q8 | 限制清单的自动基础项与人工追加项如何区分？ | **建议**：自动基础项由 `publish_decision` + §0 五项环境阻塞生成（不可删，AC-21）；人工只能追加且须带 `added_by`/`reason`；删除自动项 → `LIMITATION_REMOVED` | 架构师 |

## 8. 上游遗留映射表

> 任务书所指「`docs/w07/07-gate-review.md` §6 遗留项」：该文件**当前版本 §6 为「对外表述禁令」**（`:129-137`），遗留项实际分布在 **§5「W08 前置项」、§3「未验证项 NV」、§4「已知偏离 KD」、§4.3「残留事实」**。本表按**真实章节位置**列出属于本周（W12）承接的部分；`docs/w08/08-drill-report.md` §5「未执行项」按原编号列出。W09/W10/W11 已承接项不重复实现，仅标注依赖或最终复核责任。

### 8.1 W07 门禁复核遗留（`docs/w07/07-gate-review.md`）

| # | 遗留项（真实位置） | 本周（W12）落点 |
|---|---|---|
| L1 | §5 W08-5 / §3.1 NV-1：容器运行态（build + `compose up worker`）**NOT_VERIFIED**（「没跑过，不是跑不了」） | **本周最终承接**：P0-W12-03 实跑路径 + Q3；未跑通则 `docs/w12/known-limitations.md` 保持 `NOT_VERIFIED`，RC 镜像 digest 记 `UNKNOWN`（W11 Q7） |
| L2 | §3.2 NV-2：POSIX `SIGTERM`/`SIGINT` 真实信号路径未验证 | W11 P0-W11-03/04 已承接；**本周**：限制清单如实标注，发布说明不写「优雅停机已全平台验证」 |
| L3 | §3.2 NV-4：`test_worker_gateway.py` 本机 admin 1045 未跑通 | W11 已承接；**本周**：§0/Q2 写入限制清单（admin 1045） |
| L4 | §4 KD-1：`api` 双网、隔离为**账号级非网络级**，不得写「摘 api 的 lab_net」 | **本周**：P0-W12-04 不承诺能力清单含「网络级隔离已验证」为 `NOT_SUPPORTED`；部署文档如实标注（AC-15） |
| L5 | §4 KD-2：`tool_gateway` 占位服务与实现相反（`docker-compose.yml:156-162`） | **本周**：`docs/w12/deployment-guide.md` 复核命名一致性，指向 `workers/stack.py` 同进程装配事实；不误写成「api → gateway 容器」 |
| L6 | §4 KD-3 / KD-4 / §4.3：环境非「可复现清零」；`environment-manifest.*.json` 不入库；**7 条残留**（6 条 W07 + 1 条历史行）不得表述为「已清理」 | **本周最终承接**：`docs/w12/known-limitations.md` 保留「存在 7 条已登记残留」+ `docs/w07/worker-residue.md` 引用；发布说明遵守表述禁令；版本权威取迁移 + `docs/migrations.md`（`:104`） |
| L7 | §4 KD-5：W07 冻结四件套缺 `05-freeze.md`/`06-conformance-audit.md`（已核实不存在） | W08 `P1-W08-04` 已承接，状态待 W08 交接确认；**本周**：`tests/unit/test_w12_doc_inventory.py` 文档完整性复核时逐项核对，缺件如实登记为 `PARTIAL`，不声称「四件套齐全」 |
| L8 | §6「对外表述禁令」本身（`:129-137`）：禁「W07 已收口/门禁通过/放行」「容器网络隔离已验证」「环境已清理」「QA 独立实跑通过」等 | **本周直接承接为机器校验**：P0-W12-06 / AC-19 扫描 `docs/w12/` 全文禁用表述集合；W07 结论一律写「有条件放行（QA 最终复核确认，2026-11-11 23:56）」 |

### 8.2 W08 演练报告未执行项（`docs/w08/08-drill-report.md` §5）

| # | 未执行项（§5 原编号） | 本周（W12）落点 |
|---|---|---|
| M1 | §1.3 第 4 条（`:76-78`）：`scanned` 口径，真实扫描总数需 W09 扩展 `sweep()` | 已由 **W09** 承接（`docs/w09/01-prd.md` P0-W09-06 / AC-20）；本周不重复实现，验收报告引用 W09 门禁证据（AC-20） |
| M2 | §5：`--once` 退出码未断言固定值（`:164`） | W11 P0-W11-04 已承接；**本周**：升级/回滚 dry-run 记录（`P1-W12-03`）沿用「退出码 `(0,1)` 如实记录」口径，不把环境状态当断言 |
| M3 | §5：PostgreSQL 相关用例未执行（`:165`，5432 拒绝连接） | **本周直接承接**：P0-W12-01 功能验收 PG 段 `NOT_EXECUTED`、P0-W12-03 演示脚本双引擎段如实标注、§0/Q2 限制清单（AC-04） |
| M4 | §5：控制库残留清理部分未执行（`:166`，admin 1045、只追加表无 DELETE） | **本周直接承接**：限制清单（与 L6 合并）、发布说明保留残留事实 |
| M5 | §5：7 类注入之外的网络分区/磁盘满未执行（`:167`，不在 D5 清单） | W10 Q8 / W11 Q5 已登记「是否扩展待主理人拍板」；**本周**：不擅自扩大范围，作为后续路线项写入 `docs/w12/alpha-acceptance-report.md` 延期项 |

> 本表为「如实登记 + 给落点」，**不代表本周已修复或已验证**。W12 对 L1/L4/L5/L6/L7/L8、M3/M4 承担最终复核或限制清单责任，其余为跨周承接登记。
> **依赖链闭合**：W09←W08 门禁、W10←W09、W11←W10、W12←W11；任一上游门禁未裁定，本周不得宣布开工，最终 Alpha 门禁不得替代上游门禁证据。
