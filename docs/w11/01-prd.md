# W11 增量 PRD｜安全回归与长稳（用负面测试和故障注入证明系统在异常条件下会停下来）

> 本文是 V2.2 12 周计划的第 11 周**增量需求基线**，供架构设计与实现使用。
> 口径约定：§4「现状」引用的 `file:line` 均为**已核实的既存文件**；§4/§5「验收位置 / 测试位置」中的测试文件为**本周待新建**，路径按 `tests/unit/`、`tests/integration/`、`docs/w11/` 既有命名约定给出，非既存文件。二者不得混读。

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.13/3.14 + FastAPI + MySQL 8 控制库 + MySQL LAB 目标库 + PostgreSQL MCP 只读（**延续 V2.2 既有栈；不新增前端、不新增第三方依赖，沿用 `requirements.lock.txt`**） |
| Project Name | `dbguardian_w11_security_regression_soak` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W11_安全回归与长稳.docx` §一–§六；`项目规划/项目规划2.2/DBGuardian_12周研发计划_总览与门禁.docx` §三/§四/§五/§六；上游 `docs/w10/01-prd.md`（Benchmark 指标冻结）、`docs/w09/01-prd.md`、`docs/w08/01-prd.md`、`docs/w08/02-design.md`、`docs/w08/08-drill-report.md`、`docs/w07/07-gate-review.md`；代码基线（已核实）`core/security/policy.py`、`core/gateway/leases.py`、`core/audit/service.py`、`core/tools/executor.py`、`benchmarks/faults/injector.py`、`core/config.py`、`database/migrations/` | 
| 前置门禁 | **依赖 W10 门禁**（W10→W11 四条：Benchmark 可重放 / 指标口径冻结 / Baseline 报告完成 / 数据泄漏检查通过，见 `docs/w10/01-prd.md` §6）。W09、W10 门禁**均尚未裁定**；**根依赖 W08 门禁仍「尚在复核、未裁定」**（`docs/w08/06-qa-report.md`、`docs/w08/07-gate-review.md` 均未落盘；W07 门禁为 GR-0「有条件放行」）。**不得**写成「W08/W09/W10 已通过」 |
| 前置依赖（环境阻塞，如实登记） | ① PostgreSQL 5432 拒绝连接（`docs/w08/08-drill-report.md:165`）；② `DG_ADMIN_DSN`（root）认证失败 1045、只追加表无 `DELETE` 授权（`docs/w08/08-drill-report.md:166`、`docs/w07/07-gate-review.md:111`）；③ 控制库历史任务堆积（主理人任务书登记 830+ 行，逐条见 `docs/w07/worker-residue.md`）。**备份恢复演练不得把「环境可清零」当前提；PG 侧用例如实记 `NOT_EXECUTED`，不修环境、不冒充通过** |
| 原始需求复述 | 任务书「本周目标」原句：「完成安全负面集、跨租户与并发安全回归、审计/Evidence 故障演练、备份恢复、性能与长稳测试；冻结 Alpha Release Candidate。」 |

## 1. 产品目标

**一句话**：用**冻结的负面测试集 + 故障注入 + 实测备份恢复**证明「越权/未审批/篡改/重放/跨租户/只读写入会停、旧 Worker 与重复消息不产生重复副作用、审计或 Evidence 不可用时 R2 fail closed」，并据此冻结一个**可复现、带实测 RPO/RTO** 的 Release Candidate。

三个正交目标（判定口径均为可验证事实）：

| # | 目标 | 判定口径（可机器验证） |
|---|---|---|
| G1 | **红线恒阻断** | 冻结安全负面集中每条用例都有 `test_id` / `expected_deny_reason` / `actual` / `evidence`；越权、未审批、审批篡改·过期·撤销·重放、`nonce` 重复、`action_digest` 不匹配、环境指纹变化、`R3/R4`、生产环境写请求、只读账号写入、SQL 注释/MCP 参数绕过**逐条被拒且目标数据不变**。**100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全**（任务书 D2 原文） |
| G2 | **异常会停、恢复可核对** | 六种注入（Evidence Store / Audit Writer / 数据库断连 / Model Service / Worker 崩溃 / 控制库短暂故障）下：审计不可用时 R2 `fail closed`、R0 按策略处理并告警；恢复后 `task_events.seq` 连续、审批消费状态/审计完整性/Evidence 引用/`state_version` 可核对；所有未决写操作先 `reconciliation`，**不按旧任务状态自动重放**（`tool_calls` 增量 0、无 `TOOL_CALL_RETRIED`） |
| G3 | **RC 可复现、RPO/RTO 实测** | RC 冻结清单逐项有值（commit、数据库镜像 digest、Adapter/Tool/Policy/Prompt/Model 版本、测试集版本）；备份恢复报告**区分目标值与实测值**（`RPO ≤ 15 分钟`、`RTO ≤ 4 小时` 仅为 Alpha 建议目标，须实测确认，不得写成已验证 SLA）；长稳报告给出**样本量与运行时长**，不用短时 smoke test 冒充 |

## 2. 本周边界（先划死，防止范围蔓延）

| 边界 | 内容 |
|---|---|
| 做什么 | ① 安全负面测试集 + 回归报告；② 跨租户 / 审批 / Worker 并发回归；③ 六类故障注入 + 审计/Evidence 可用性验证；④ 控制库与 Evidence/Object Store 备份恢复演练 + 实测 RPO/RTO + 性能长稳 + 告警清单；⑤ RC 冻结 + 一次完整端到端回归 + 安全/可靠性/性能/恢复/成本报告 |
| 写能力 | **不扩大写能力**。唯一 R2 动作仍是 `mysql_lab_update_order_note` / `1.0`；负面集只做「诱导被拒」验证，不新增任何可写工具 |
| 引擎写路径 | **不做第二引擎写路径、不做 PostgreSQL 写路径**；PG 保持 MCP 只读（`docs/w07/07-gate-review.md:90` NV-3 属设计范围外，非缺陷） |
| 第三方依赖 | **不引入新的第三方依赖**；故障注入复用 `benchmarks/faults/injector.py` 与 W08 既有接缝（`docs/w08/02-design.md:506`「不为测试加生产分支」），不新起监控/调度服务 |
| 安全机制 | **不删除、不绕过任何 P0 安全门禁**：环境隔离、最小权限、固定只读工具、服务端风险计算、人工审批、唯一 Tool Gateway、Task State Machine、lease/fencing、重放保护、`action_digest` 绑定、审批原子消费、Evidence 归属校验、追加式 Audit、`UNKNOWN_OUTCOME` 核对、独立 Verifier、恢复、跨租户负测、备份恢复、安全负面集、Benchmark 基线（任务书 §五、《总览》§四）。**不得为按期发布而删除安全门禁** |
| 如实返回状态 | 遇到不支持 / 权限不足 / 结果未知必须如实返回：审计失败 → R2 阻断而非「先执行后补审计」（`docs/adr/ADR-0006-audit-integrity-and-worm.md:105` 已否决该路径）；证据不足 → `MANUAL_INTERVENTION`；PG 用例受环境阻塞 → `NOT_EXECUTED`；RPO/RTO 未实测 → 标「建议目标」，不写成 SLA |
| 前端 | **不新增前端交付项**；报告以版本化文档 + 结构化结果为准 |
| 训练 | **本周不涉及模型训练**；SFT/微调仅在 W10 Baseline 报告发现可定位短板后按条件决定，DPO / 14B / RL 推迟至 Alpha 后（《总览》§五） |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 安全评审 | 作为安全评审，我要冻结负面集里每条诱导（未授权 SQL/Shell/文件/网络、未注册工具、伪造风险字段、只读账号写入、SQL 注释/MCP 参数绕过、审批篡改/过期/撤销/重放/nonce 重复/digest 不符/环境指纹变化、R3/R4、生产写请求、任意 host/port、SSRF/不可信重定向）都有 `test_id`、预期拒绝原因、实际结果与证据，这样我认可「100%」的准确含义是「当前冻结集通过」。 |
| U2 | 值班 SRE | 作为值班 SRE，我要看到审计写入失败时 R2 直接停下、R0 按策略处理并告警，且恢复后事件序列连续、审批消费状态与 Evidence 引用可核对，这样我敢在审计短暂不可用时让系统继续跑只读诊断。 |
| U3 | 审计员 | 作为审计员，我要备份恢复演练**实际恢复**并校验 `task_events`/审批/`step_*`/Evidence/审计完整性，而不是只验证备份文件存在；报告要区分 RPO/RTO 的目标值与实测值。 |
| U4 | 平台工程师 | 作为平台工程师，我要旧 Worker、过期 lease、错误 `fencing_token`、双 Worker 并发、状态版本冲突、重复消息、审批消费并发、Gateway 失联各跑一遍，且每种都证明没有重复副作用。 |
| U5 | 主理人（发布决策） | 作为主理人，我要 RC 冻结清单逐项有值、一次完整端到端回归通过；**任一安全红线未过就停止 Alpha 发布、只发明确标注限制的技术预览**，而不是让我在「按时发布」和「删门禁」之间选。 |
| U6 | 运维 | 作为运维，我要长稳报告写明样本量与运行时长、告警覆盖 lease 丢失/审计失败/Evidence 不可用/数据库异常/模型降级/备份失败，且不用短时 smoke test 冒充长稳。 |

## 4. 需求池

> **P0 为 W11→W12 门禁必需，不得降级、不得延期**。每条 P0 给出 `[D#]`、已核实现状、可实施要求、**验收位置（测试文件路径 + 断言形式）**。
> `file:line` 现状引用已用 Grep/Read 核实；「当前无该能力」以全仓检索无代码实现为依据（`RPO`/`RTO`/`backup`/`restore`/`ReleaseCandidate` 仅命中 `docs/w07/08-pm-ruling.md:150` 未来项描述）。

### P0（Must have，6 条）

- **P0-W11-01** `[D1]` **安全负面测试集 + 逐条证据。**
  - 现状（已核实）：`core/security/policy.py:28` `_RISK_LEVELS=("R0","R1","R2","R3","R4")`、`:146` `PolicyEngine.decide`、`:216` `R4_NOT_EXECUTABLE`、`:223` `R3_AGENT_EXECUTION_FORBIDDEN`、`:230-235` `R2_REQUIRES_APPROVAL`、`:280` `_deny`；`core/models/gateway.py:226-239` 校验「工具未注册 / 风险等级不在允许范围 / 写工具未获授权」（默认 `allow_write=False`，`:179`）；`tests/integration/test_w07_rejection_matrix.py` 已有 W07 拒绝矩阵、`tests/integration/test_w07_disclosure_integrity.py` 已有披露完整性用例。**全仓无覆盖 W11 D1 全部诱导面的冻结负面集、无 `test_id`/`expected_deny_reason`/`actual`/`evidence` 四元组结构**。
  - 要求：
    1. 覆盖四组诱导：① 未授权 SQL/Shell/文件/网络、未注册工具、伪造风险字段、只读账号写入、SQL 注释与 MCP 参数绕过；② 审批篡改、过期、撤销、重放、`nonce` 重复、`action_digest` 不匹配、环境指纹变化；③ `R3/R4`、生产环境写请求、任意 host/port、DNS rebinding/SSRF、不可信重定向；④ 跨租户读取任务/Evidence/Approval/Report/Environment/Audit（与 D2 交叠）。
    2. 每条用例落 `test_id`、`expected_deny_reason`、`actual`、`evidence`（结构化，可机器比对）；拒绝时**目标数据不变**。
    3. 报告明示「100% 仅代表当前冻结测试集通过，不代表数学意义绝对安全」；任一红线失败 → 触发 §6 停止发布处置。
  - **验收位置**：`tests/integration/test_w11_security_negative.py`（新建，按组参数化）——① 每条用例 `actual.denied is True` 且 `actual.deny_reason == expected_deny_reason`；② 四元组字段非空率 ==100%；③ 每条用例前后目标行/表计数不变；④ 冻结集用例数与设计清单逐条相等（不缺项）。`tests/unit/test_w11_negative_manifest.py`（新建）——负面集 manifest 的 `test_id` 集合无重复、版本号非空。

- **P0-W11-02** `[D2]` **跨租户 / 审批 / Worker 并发回归。**
  - 现状（已核实）：`core/config.py:96-97` `worker_lease_ttl_s`/`worker_heartbeat_interval_s`、`:119` `worker_max_concurrency=1`（`ge=1,le=1`，注释「多并发留给 W08 之后的独立评审」）；`tests/integration/test_w08_crash_matrix.py` 7 类注入已实测（`docs/w08/08-drill-report.md:92-100`），含 `FENCING_TOKEN_STALE`（`:97`）、`WORKER_CONFLICT`（`:98`）、重复消息（`:99`）、PAUSE/CANCEL（`:100`）；`core/gateway/leases.py` 已有租约/fencing 实现。`docs/w08/08-drill-report.md:64-70` 已登记调度事件挂在伪任务 `system:worker_scheduler`、其 `tenant_id` 为 NULL 导致 `AuditService.query(tenant_id=...)` 查不到——跨租户回归须覆盖该已知边界。
  - 要求：
    1. 跨租户读取任务、Evidence、Approval、Report、Environment、Audit 六类对象 → 对象级授权拒绝；含伪任务 `system:worker_scheduler` 的租户过滤口径。
    2. 旧 Worker、lease 过期、错误 `fencing_token`、双 Worker 并发、状态版本冲突、重复消息、审批消费并发、`nonce` 重放、审计写入失败、Gateway 失联逐类独立用例。
    3. 所有安全红线失败都阻止发布；**零重复副作用**：`tool_calls` 增量 ≤1、目标行变化 ≤1 次、`task_events.seq` 连续、无 `TOOL_CALL_RETRIED`。
  - **验收位置**：`tests/integration/test_w11_cross_tenant.py`（新建）——六类对象 × 跨租户各 1 例，`OBJECT_AUTH_DENIED` 且返回集为空。`tests/integration/test_w11_concurrency.py`（新建，10 类独立用例）——每例：`tool_calls_delta <= 1`、`target_row_changes <= 1`、`task_events.seq == list(range(n))`、`"TOOL_CALL_RETRIED" not in event_types`；旧 Worker → `FENCING_TOKEN_STALE`；双 Worker → `WORKER_CONFLICT`。

- **P0-W11-03** `[D3]` **六类故障注入 + 审计/Evidence 可用性 + 恢复核对。**
  - 现状（已核实）：`core/audit/service.py:240` 有 best-effort 追加（仅告警降级路径）；`docs/adr/ADR-0006-audit-integrity-and-worm.md:62` 明确「审计写入失败，R2 已阻断」+ `AUDIT_WRITE_FAILED`、`:105` **否决**「先执行后补审计」、`:129` 登记「账本不可写时告警只剩结构化日志」；`core/tools/executor.py:452` 回执写不进 → 降级 `UNKNOWN_OUTCOME`、`:495` 证据缺失统一降级「结果未知」；`benchmarks/faults/injector.py:157` `inject()` / `:243` `reset()` / `:308` `FaultSession` / `:42` `FaultInjectionError` / `:58` `InjectionResult` 为注入器现状；`core/recovery/controller.py`（W08）与 `core/execution/reconciler.py`（W06）已有 UNKNOWN 核对与不重放语义。**全仓无 D3 六类「服务级」故障注入矩阵与恢复后完整性校验报告**。
  - 要求：
    1. 注入六种：Evidence Store 不可用、Audit Writer 失败、数据库断连、Model Service 不可用、Worker 崩溃、控制库短暂故障；复用既有接缝，**不为测试加生产分支**（`docs/w08/02-design.md:506`）。
    2. 验证 **R2 在审计不可用时 fail closed**；R0 读取按策略处理并告警。
    3. 恢复后校验：`task_events` 连续性、审批消费状态、审计完整性、Evidence 引用、`state_version`。
    4. 所有未决写操作恢复后先 `reconciliation`，**不根据旧任务状态自动重放**；`tool_calls` 增量 0。
  - **验收位置**：`tests/integration/test_w11_fault_injection.py`（新建，6 类独立用例）——① Audit Writer 失败 + R2 请求 → `AUDIT_WRITE_FAILED` 且 `tool_calls_delta == 0`、目标行不变；② R0 读取 → 按策略返回并 `alert_emitted is True`；③ 每类恢复后 `task_events.seq == list(range(n))`、`state_version` 单调、`Evidence` 引用可解析；④ 全程 `"TOOL_CALL_RETRIED" not in event_types`。`docs/w11/fault-injection-report.md`（新建）——六类各列「可自动恢复 vs 必须人工介入」边界（延续 `docs/w08/08-drill-report.md:102-110` 写法）。

- **P0-W11-04** `[D4]` **备份恢复实测 RPO/RTO + 性能长稳 + 告警清单。**
  - 现状（已核实）：`database/migrations/001_harness_core.sql` … `008_w08_verification.sql` 及 `rollback/` 已存在（Glob 确认 8 个正向 + 5 个回滚）；`scripts/migrate.py` 为迁移入口；`deploy/environment-manifest.example.json`、`docs/migrations.md` 为部署/迁移文档基线；`core/verification/perf.py` 有标准库 P50/P95（`docs/w08/02-design.md:465`）。**全仓无备份/恢复脚本、无 RPO/RTO 记录、无长稳报告、无告警清单实现**（Grep 仅命中 `docs/w07/08-pm-ruling.md:150` 把「备份恢复实测 RPO/RTO」列为未来项）。
  - 要求：
    1. 执行控制库、Evidence/Object Store 的**备份与实际恢复**演练；恢复后校验事件、审批、Evidence 与审计完整性（**不得只验证文件存在**）。
    2. 记录**实测** RPO/RTO；`RPO ≤ 15 分钟`、`RTO ≤ 4 小时` 仅作为 **Alpha 建议目标**，报告字段须分列 `target_rpo_minutes` / `measured_rpo_minutes` / `target_rto_hours` / `measured_rto_hours`，未实测写 `null` 并标「建议目标，未验证 SLA」。
    3. 并发任务、工具超时、数据库查询、模型 TTFT/生成时延、任务 P95、预算消耗、错误率测试；长稳须给出**样本量与运行时长**，不用短时 smoke test 冒充。
    4. 告警覆盖：lease 丢失、审计失败、Evidence 不可用、数据库异常、模型降级、备份失败。
  - **验收位置**：`tests/integration/test_w11_backup_restore.py`（新建）——① 备份后写入 N 条事件/审批/Evidence/审计 → 恢复 → 四类对象计数与恢复前逐值相等；② `measured_rpo_minutes is None` 时报告 `sla_claim is False`；③ 建议目标值与实测值分列且互不覆盖。`tests/unit/test_w11_soak_report.py`（新建）——报告 `sample_size` / `duration_s` 均 >0 且 `smoke_test is False`；告警清单 6 项集合相等。`docs/w11/backup-restore-report.md`（新建）——实测值与目标值分表。

- **P0-W11-05** `[D5]` **Release Candidate 冻结 + 完整端到端回归 + 安全红线处置。**
  - 现状（已核实）：`benchmarks/baseline_v0/run_baseline.py:20-34` 已有版本字段集（`harness_version`/`prompt_version`/`tool_schema_version`/`inference_config_version`/`testset_version`）；`docs/migrations.md` + `database/migrations/` 为迁移版本权威来源（`docs/w07/07-gate-review.md:104` KD-4 明确「环境清单不入库，版本权威来源是迁移 + `docs/migrations.md`」）；`deploy/Dockerfile.api`、`deploy/Dockerfile.worker` 存在但**容器运行态仍 `NOT_VERIFIED`**（`docs/w07/07-gate-review.md:78` NV-1）。**全仓无 RC 冻结清单、无 W11 端到端回归报告**。
  - 要求：
    1. 冻结代码 commit、数据库镜像 digest、Adapter/Tool/Policy/Prompt/Model 版本、测试集版本，逐项有值（缺失项记 `UNKNOWN` 并登记，不臆造）。
    2. 完成一次完整端到端回归：双引擎只读诊断、单引擎审批修复、Verifier、未知结果核对；PG 侧受 5432 阻塞部分记 `NOT_EXECUTED`。
    3. 整理安全、可靠性、性能、恢复、成本报告；记录未通过项及延期方案。
    4. **若安全红线未通过 → 停止 Alpha 发布，仅标记技术预览并列明限制**（任务书 D5 / §六原文）。
  - **验收位置**：`tests/unit/test_w11_rc_manifest.py`（新建）——① RC 清单必填键集合逐项存在且非空；② 缺项 → `value == "UNKNOWN"` 且 `registered_issue is True`；③ 版本号与 `benchmarks/baseline_v0/baseline_manifest.json` / `docs/migrations.md` 一致。`tests/integration/test_w11_e2e_regression.py`（新建）——双引擎只读诊断 + 单引擎审批修复 + Verifier + UNKNOWN 核对四段各自断言；PG 段 `status == "NOT_EXECUTED"` 时不判 FAIL。`docs/w11/rc-freeze.md`（新建）——冻结清单 + 未通过项延期方案。

- **P0-W11-06** `[D1/D2 补强]` **「红线失败即阻止发布」的机器判定。**
  - 现状（已核实）：《总览》§四「安全负面集任一红线失败都阻止完整 Alpha 发布；只能发布明确标注限制的技术预览」；任务书 W11 §五「为了按期发布而删除安全门禁」列为主要风险。**当前无把负面集结果自动映射为「可发布 / 仅技术预览」判定的实现**。
  - 要求：
    1. 汇总 P0-W11-01 负面集、P0-W11-02 并发回归、P0-W11-03 故障注入三份结果，输出 `publish_decision ∈ {ALPHA_ALLOWED, TECH_PREVIEW_ONLY}`。
    2. 判定为纯函数：任一红线 `denied is False` 或「重复副作用」断言失败 → `TECH_PREVIEW_ONLY`；全部通过才 `ALPHA_ALLOWED`；**不得**由人工开关覆盖。
    3. `TECH_PREVIEW_ONLY` 时自动生成限制清单（含 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` 标记），供 W12 发布使用。
  - **验收位置**：`tests/unit/test_w11_publish_decision.py`（新建）——① 全绿 → `ALPHA_ALLOWED`；② 注入 1 条红线失败 → `TECH_PREVIEW_ONLY` 且 `failed_test_ids` 含该 id；③ 断言判定函数签名无 `force_allow`/`override` 形参；④ 限制清单自动生成且标记集合含 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`。

### P1（本周期内可做，不进门禁）

- **P1-W11-01** `[D4]` 指标趋势看板的只读数据导出（无前端）。**AC**：`tests/unit/test_w11_trend_export.py`（新建）导出字段集合冻结。
- **P1-W11-02** `[D3]` 故障注入的 `FaultSession` 与 W11 六类服务的适配封装（不改 `injector.py` 内部）。**AC**：`tests/unit/test_w11_injector_adapter.py`（新建）六类各有 1 个适配器且不新增第三方依赖。
- **P1-W11-03** `[D2]` 审批消费并发的 nonce 重放压力参数化。**AC**：`tests/integration/test_w11_nonce_replay.py`（新建）100 次重放全部 `APPROVAL_ALREADY_CONSUMED`。

### P2（明确延期，不进 W11 门禁）

- **P2-W11-01** 真实 WORM/对象锁接入与 `dg_ctl` 账号拆分（`docs/w08/01-prd.md` P2 已登记）。
- **P2-W11-02** 网络分区/磁盘满等超出 D3 六类清单的注入（**是否扩展待主理人拍板**）、多级/会签审批。
- **P2-W11-03** 跨系统 Exactly Once 承诺（明确不承诺）。

## 5. 验收标准 AC（可机器验证）

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | 负面集四元组完整 | `tests/integration/test_w11_security_negative.py`（新建） | 每条 `test_id`/`expected_deny_reason`/`actual`/`evidence` 非空率 ==100% |
| AC-02 | 诱导逐条被拒 | 同上 | 每条 `actual.denied is True` 且 `deny_reason == expected_deny_reason`；用例数 == 冻结清单数 |
| AC-03 | 拒绝不改数据 | 同上 | 每条用例前后目标行/表计数相等（`==`） |
| AC-04 | 负面集 manifest 唯一 | `tests/unit/test_w11_negative_manifest.py`（新建） | `len(test_ids) == len(set(test_ids))`；版本号非空 |
| AC-05 | 跨租户对象级授权 | `tests/integration/test_w11_cross_tenant.py`（新建） | 六类对象 × 跨租户 → `OBJECT_AUTH_DENIED` 且返回集为空；含伪任务 `system:worker_scheduler` 边界 |
| AC-06 | 并发零重复副作用 | `tests/integration/test_w11_concurrency.py`（新建，10 类） | 每例 `tool_calls_delta<=1`、`target_row_changes<=1`、`seq==list(range(n))`、无 `TOOL_CALL_RETRIED` |
| AC-07 | fencing / 冲突拒绝 | 同上 | 旧 Worker → `FENCING_TOKEN_STALE`；双 Worker → `WORKER_CONFLICT` |
| AC-08 | 审计失败 R2 fail closed | `tests/integration/test_w11_fault_injection.py`（新建，6 类） | Audit Writer 失败 + R2 → `AUDIT_WRITE_FAILED` 且 `tool_calls_delta==0`、目标行不变 |
| AC-09 | R0 按策略并告警 | 同上 | R0 读取在审计失败下按策略返回且 `alert_emitted is True` |
| AC-10 | 恢复后完整性 | 同上 | 六类恢复后 `seq==list(range(n))`、`state_version` 单调、Evidence 引用可解析 |
| AC-11 | 恢复不重放 | 同上 | 全程无 `TOOL_CALL_RETRIED`；`tool_calls_delta==0` |
| AC-12 | 备份实际恢复并校验 | `tests/integration/test_w11_backup_restore.py`（新建） | 恢复后事件/审批/Evidence/审计四类计数与恢复前逐值相等 |
| AC-13 | RPO/RTO 目标与实测分列 | 同上 + `docs/w11/backup-restore-report.md`（新建） | `measured_* is None` → `sla_claim is False`；目标值字段与实测值字段互不覆盖 |
| AC-14 | 长稳不冒充 | `tests/unit/test_w11_soak_report.py`（新建） | `sample_size>0`、`duration_s>0`、`smoke_test is False`；告警清单 6 项集合相等 |
| AC-15 | RC 清单逐项有值 | `tests/unit/test_w11_rc_manifest.py`（新建） | 必填键集合全存在；缺项 → `value=="UNKNOWN"` 且 `registered_issue is True`；版本与 manifest/migrations 一致 |
| AC-16 | 端到端四段回归 | `tests/integration/test_w11_e2e_regression.py`（新建） | 双引擎只读 / 单引擎审批修复 / Verifier / UNKNOWN 核对四段各自断言；PG 段 `NOT_EXECUTED` 不判 FAIL |
| AC-17 | 红线失败即阻止发布 | `tests/unit/test_w11_publish_decision.py`（新建） | 全绿 → `ALPHA_ALLOWED`；注入 1 条失败 → `TECH_PREVIEW_ONLY` 且 `failed_test_ids` 含该 id |
| AC-18 | 判定不可人工覆盖 | 同上 | 签名无 `force_allow`/`override` 形参；限制清单自动含 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` |
| AC-19 | 写面与引擎不回退 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py`（既存） | PG 工具集 7 个全 `read`；write 工具仍 `LAB+mysql` 单一；`core/security/policy.py:216/223` 的 R4/R3 拒绝仍生效 |
| AC-20 | 安全门禁未删除 | `tests/unit/test_w11_gate_inventory.py`（新建） | 冻结的 P0 门禁清单（环境隔离/只读/服务端风险/审批/Gateway/恢复/Evidence/Verifier/安全测试）逐项 `present is True` |
| AC-21 | 上游指标口径未回退 | `tests/unit/test_w10_metrics.py`（既存，W10 门禁） | 指标分母/排除规则断言仍绿；W11 报告引用同一冻结版本 |

## 6. 周末门禁与完成定义

**W11 → W12 门禁（浓缩任务书 §六，逐条可判）**：
1. **安全负面集通过**：AC-01～AC-04、AC-17～AC-20 全绿；冻结负面集中越权、未审批、审批篡改/重放、跨租户、只读写入等红线请求**全部阻断**。
2. **状态恢复、跨租户、备份恢复均有报告**：AC-05～AC-14 全绿；旧 Worker、过期 lease、错误 `fencing_token`、双 Worker 并发、重复消息不导致重复副作用；审计失败时 R2 停止；备份**实际恢复**并校验完整性；RPO/RTO 报告区分目标值与实测值。
3. **RC 冻结与端到端回归**：AC-15～AC-16、AC-21 全绿；冻结清单逐项有值，完整端到端回归（双引擎只读诊断、单引擎审批修复、Verifier、未知结果核对）通过。

**未通过处置（按任务书 §六 / D5 / 《总览》§四，写死）**：
- **任一安全红线失败 → 停止 Alpha 发布，仅可标记「技术预览」并列明限制**（`NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN`）；**不得**删除或放宽任何 P0 安全门禁来换发布；`publish_decision` 由纯函数判定，人工不可覆盖（AC-18）。
- **不满足则不标记完整 Alpha**：`docs/w12/` 发布材料只能按 `TECH_PREVIEW_ONLY` 口径书写。
- 门禁任一项未过 → **先修复 W11 前置模块，不并行堆叠 W12 功能**；W12 不得在「红线未过」时准备完整 Alpha 包。
- **W10 门禁未过**（指标未冻结 / Baseline 未复现 / 数据泄漏未查）→ 先回 W10，W11 报告不得引用未冻结口径；**W09 门禁未过** → 脱敏相关恢复/审计校验暂停；**W08 门禁复核最终未通过**（当前尚在复核）→ 沿用 `docs/w08/` 门禁件处置，W11 不得替代上游门禁证据。
- 模型环境仍 `BLOCKED_MODEL_ENV_MISSING` / PG 5432 仍拒绝连接时，相关用例记 `NOT_EXECUTED` 并归因为环境故障，**不判 FAIL、不冒充通过、不修环境**；该事实须在 RC 冻结清单与交接说明登记。
- 已知环境阻塞（PG 5432 / admin 1045 / 只追加表残留）不因本周工作消失，须如实登记，不得表述为「环境已可复现清零」（`docs/w07/07-gate-review.md:135`）。

**本周完成定义（任务书 §六）**：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

## 7. 待确认问题 Q

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | **W08 门禁仍「尚在复核、未裁定」**（`docs/w08/06-qa-report.md`、`07-gate-review.md` 未落盘），且 W09/W10 门禁均未裁定，W11 能否先做？ | **建议**：先做离线可判的工作（负面集 manifest、并发用例骨架、RC 清单结构、故障注入适配封装）；**不宣布 W11 开工、不写「上游已通过」**；门禁签署等上游件落盘后补做 | 主理人 + QA |
| Q2 | 已知环境阻塞（PG 5432 拒绝连接 / `DG_ADMIN_DSN` root 1045 / 控制库 830+ 历史行、只追加表无 DELETE）如何影响 W11？ | **建议**：PG 侧只读/跨引擎用例记 `NOT_EXECUTED` 并归因为环境故障，不判 FAIL、不修环境；备份恢复演练**不以「环境可清零」为前提**，改用「备份→追加 N 条→恢复→校验计数」的可控构造；清理前提仍以 `docs/w07/worker-residue.md` 为准 | 主理人 |
| Q3 | `RPO ≤ 15 分钟`、`RTO ≤ 4 小时` 是「Alpha 建议目标」还是门禁硬阈值？ | **建议**：**仅建议目标，不作 W11 门禁硬阈值**；门禁判「报告区分目标值与实测值 + 实际恢复并校验完整性」（AC-12/13）；绝对值是否冻结为 SLA 由主理人依据实测拍板，本周不把建议值写成已验证 SLA | 主理人 |
| Q4 | 长稳的样本量与运行时长定多少？ | **建议**：由架构师在设计给出可复现的具体值（如 `sample_size`/`duration_s` 常量并写进报告），AC 只断言「>0 且 `smoke_test is False`」；**不用短时 smoke test 冒充长稳**（任务书 §五）；具体数值待架构师确认 | 架构师 + 主理人 |
| Q5 | 故障注入是否扩展到 D3 六类之外（网络分区 / 磁盘满，`docs/w08/08-drill-report.md:167`；W08 崩溃矩阵 7 类之外）？ | **建议**：本周严格按任务书 D3 **六类**（Evidence/Audit/DB/Model/Worker/控制库短暂故障），不擅自扩大；是否扩展**待主理人拍板**（W10 Q8 已登记同一问题） | 主理人 |
| Q6 | 备份对象「Evidence/Object Store」当前是否有独立对象存储？ | **建议**：按现状如实登记——若仅控制库表，则报告写「控制库 + Evidence 表（无独立 Object Store，标 `NOT_SUPPORTED`）」，**不得编造对象存储路径**；落 `docs/w11/backup-restore-report.md` | 架构师 |
| Q7 | RC 冻结的「数据库镜像 digest」在容器运行态 `NOT_VERIFIED`（`docs/w07/07-gate-review.md:78` NV-1）时怎么填？ | **建议**：填 `UNKNOWN` 并登记为问题（AC-15），**不臆造 digest**；容器运行态实跑落 **W12 D3/D5**，与 NV-1 关闭绑定 | 主理人 |
| Q8 | `publish_decision=TECH_PREVIEW_ONLY` 时对 W12 的约束如何传递？ | **建议**：由 `docs/w11/rc-freeze.md` 输出结构化限制清单，W12 PRD §0/§6 直接引用；W12 不得在无 W11 `ALPHA_ALLOWED` 时标记完整 Alpha | 主理人 |

## 8. 上游遗留映射表

> 任务书所指「`docs/w07/07-gate-review.md` §6 遗留项」：该文件**当前版本 §6 为「对外表述禁令」**（`:129-137`），遗留项实际分布在 **§5「W08 前置项」、§3「未验证项 NV」、§4「已知偏离 KD」、§4.3「残留事实」**。本表按**真实章节位置**列出属于本周（W11）承接的部分；`docs/w08/08-drill-report.md` §5「未执行项」按原编号列出。W09/W10 已承接项不重复实现，仅标注依赖。

### 8.1 W07 门禁复核遗留（`docs/w07/07-gate-review.md`）

| # | 遗留项（真实位置） | 本周（W11）落点 / 后续落点 |
|---|---|---|
| L1 | §5 W08-5 / §3.1 NV-1：容器运行态（build + `compose up worker`）**NOT_VERIFIED**（「没跑过，不是跑不了」） | **本周承接为长稳前提**：P0-W11-04 长稳/性能、P0-W11-05 RC 清单把镜像 digest 记 `UNKNOWN`（Q7）；真正关闭 NV-1 落 **W12 D3/D5** 部署包/RC 实跑 |
| L2 | §3.2 NV-2：POSIX `SIGTERM`/`SIGINT` 真实信号路径未验证（Windows `TerminateProcess`） | **本周承接**：P0-W11-03「Worker 崩溃」注入 + P0-W11-04 长稳/优雅停机告警；如实标注「真实信号路径未验证」不冒充通过 |
| L3 | §3.2 NV-4：`test_worker_gateway.py` 本机 admin 1045 未跑通 | **本周承接**：P0-W11-02 并发回归、§0/Q2 登记为环境阻塞，如实归因不修 |
| L4 | §4 KD-1：`api` 双网、隔离为账号级非网络级；不得写「摘 api 的 lab_net」 | 落 **W12 D3/D4** 部署文档如实标注；本周负面集「任意 host/port / SSRF」不得借「网络级隔离」假设 |
| L5 | §4 KD-2：`tool_gateway` 占位服务与实现相反（`docker-compose.yml:156-162`） | W08 `P1-W08-01` 已承接，状态待 W08 交接确认；**W12 D4** 文档复核 |
| L6 | §4 KD-3 / KD-4 / §4.3：环境非「可复现清零」；`environment-manifest.*.json` 不入库；**7 条残留**不得表述为「已清理」 | **本周承接**：P0-W11-04 备份恢复不以环境可清零为前提、§0/§6/Q2 登记；**W12 D4/D5** 发布说明保留该事实 |
| L7 | §4 KD-5：W07 冻结四件套缺 `05-freeze.md`/`06-conformance-audit.md`（已核实不存在） | W11 的 RC 冻结以 `benchmarks/baseline_v0/` + `docs/migrations.md` + 迁移文件为版本权威（P0-W11-05），**不借用 W07 冻结四件套**；W12 D4 文档完整性复核 |
| L8 | §6「对外表述禁令」本身（`:129-137`） | **贯穿 W11–W12**；本周 §0/§6 已按「有条件放行 / NOT_VERIFIED / 未清理 / 只读复核」口径书写 |

### 8.2 W08 演练报告未执行项（`docs/w08/08-drill-report.md` §5）

| # | 未执行项（§5 原编号） | 本周（W11）落点 / 后续落点 |
|---|---|---|
| M1 | §1.3 第 4 条（`:76-78`）：`scanned` 口径，真实扫描总数需 W09 扩展 `sweep()` | 已由 **W09** 承接（`docs/w09/01-prd.md` P0-W09-06 / AC-20）；本周 P0-W11-03 恢复核对引用 `scanned_total`，不重复实现 |
| M2 | §5：`--once` 退出码未断言固定值（`:164`，库内历史未收敛候选导致退出码为 1） | **本周直接承接**：P0-W11-04 长稳/回归脚本明确样本量与运行时长；退出码仍按 `(0,1)` 口径如实记录，不把环境状态当断言 |
| M3 | §5：PostgreSQL 相关用例未执行（`:165`，5432 拒绝连接） | **本周直接承接**：P0-W11-05 端到端回归 PG 段记 `NOT_EXECUTED`、P0-W11-01/02 跨引擎用例归因为环境故障（§0/Q2） |
| M4 | §5：控制库残留清理部分未执行（`:166`，admin 1045、只追加表无 DELETE） | **本周直接承接**：P0-W11-04 备份恢复改用可控构造、§0/§6/Q2 登记；W12 限制清单（与 L6 合并） |
| M5 | §5：7 类注入之外的网络分区/磁盘满未执行（`:167`，不在 D5 清单） | **本周 Q5 登记「是否扩展待主理人拍板」**；本周严格按 D3 六类，不擅自扩大（与 W10 Q8 同一问题） |

> 本表为「如实登记 + 给落点」，**不代表本周已修复或已验证**。W11 对 L1/L2/L3/L6、M2/M3/M4 承担实现或归因责任，其余为跨周承接登记。
