# W08 增量 PRD｜独立验证与恢复（单引擎 R2 修复闭环的安全收口）

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.14.7 + FastAPI + MySQL 8 控制库（延续既有栈；**本周无前端交付项**） |
| Project Name | `dbguardian_w08_independent_verification` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W08_独立验证与恢复.docx` §一–§六；`docs/w07/01-prd.md`（Q7 承接项）、`docs/w07/00-action-spec.md` §6、`docs/w07/02-design.md:338-352`（三条验证断言）；`docs/w07/07-gate-review.md` GR-0；**`tag w07-close`（以 `git rev-parse w07-close` 取锚，当前 `c8a739ad`；不写死 hash）**；Q1–Q8 裁定见 `docs/w08/02-design.md` |
| 前置门禁 | W07 门禁「有条件放行」（QA 2026-11-11 23:56），C-1…C-4 已于 `tag w07-close` 锚点对齐；本 PRD 只在既有唯一 R2 动作之上增加**验证与恢复**，不扩大执行能力 |
| 原始需求复述 | 「不让模型自报成功；写操作未知时先核对，绝不盲目重试。」门禁判定：至少一个修复闭环通过独立验证；未知结果恢复可靠；取消/暂停与 Worker 故障演练通过；未通过则缩小修复范围或仅发布诊断预览。 |

## 1. 产品目标

**一句话**：把 W07「能安全执行一次写」升级为「**能独立证明这次写是否真的达成目标**，并在结果未知时先核对、永不盲目重试」。

三个正交目标：

| # | 目标 | 判定口径 |
|---|---|---|
| G1 | **不自证** | 修复结论只来自 Verifier 的断言结果；模型/Agent 的「修复成功」文本不进入任何判定分支（构造上无法传入） |
| G2 | **异路径** | 关键断言至少一条的采集路径**不同于执行路径**（执行走 `dg_rw` 写连接；验证走 `dg_ro` 只读连接或独立只读回读端口） |
| G3 | **停得住** | 结果未知 → `NEEDS_RECONCILIATION`；证据不足 → `MANUAL_INTERVENTION`；断言不满足 → 不得 `COMPLETED`；恢复过程 `tool_calls` 与目标行副作用均不增加 |

## 2. 本周边界（先划死，防止范围蔓延）

| 边界 | 内容 |
|---|---|
| 执行能力 | **不新增**任何写工具、**不做**第二种写动作、**不做** PostgreSQL 写路径；只验证既有 `mysql_lab_update_order_note` / `1.0` |
| 判定来源 | **不采信模型自报**：Verifier 只接受结构化断言 + 采集端口返回的实测值，不接受自然语言结论 |
| 交互面 | **不给 API 加 execute / consume 端点**；验证与恢复由 Worker 进程内驱动，读侧如需暴露走只读查询（P1） |
| 网络 | **不摘 `api` 的 `lab_net`**（`apps/api/deps.py:118-132` 的 `MysqlPrepareProbe` 依赖它，摘网即打断 `POST /v1/approvals`）；迁出评估仅出 ADR |
| 依赖 | **不引入新的第三方依赖**；沿用 `requirements.lock.txt` |
| 前端 | 本周无前端交付项；验证结果以结构化 API/审计事件为准 |
| 补偿 | 补偿是**独立 R2 动作**，需独立审批与审计；**不实现**「自动补偿 / 自动回滚」 |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 值班 SRE | 作为值班 SRE，我要看到「哪条断言、用什么工具、在什么时间窗、实测值是多少、为什么判 PASS/FAIL/UNKNOWN」，这样我不会被一句「已修复」糊弄，也不会在缺数据时误判成功。 |
| U2 | 审批人（Approver） | 作为审批人，我要在批准前就知道「执行后会用哪几条断言、从哪条路径核验」，这样我批准的是一个**可被独立验证**的动作。 |
| U3 | 审计员（Auditor） | 作为审计员，我要能按 `tool_call_id` / `action_digest` 复原「验证计划是什么、实际采到什么、判定是什么、补偿是否另走审批」，这样我能独立复核闭环是否真的闭合。 |
| U4 | 安全评审 | 作为安全评审，我要看到「结果未知时系统不会自动重试、不会把 UNKNOWN 当 PASS、补偿不复用原审批」的自动化证据，这样我才认可写入能力仍被钉在一个格子里。 |
| U5 | 平台工程师 | 作为平台工程师，我要在 Worker 崩溃/响应丢失/重复消息后，看到任务被收敛到确定状态（`NEEDS_RECONCILIATION` 或 `MANUAL_INTERVENTION`）且事件序列连续，这样我敢把 Worker 放进无人值守的调度。 |

## 4. 需求池

> P0 全部为 W8→W9 门禁必需，**不得降级、不得延期**；每条 P0 均给出**可机器验证**的测试位置与断言形式。`[D1]`–`[D5]` 标注对应任务书交付项。

### P0（Must have，8 条）

- **P0-W08-01** `[D1]` **Verification Plan/Result 数据模型 + W07 三条断言自动比对。**
  - 现状（已核实）：`core/tools/contracts.py:94-117` 已定义 `VerificationAssertion`（`assertion_id` / `metric` / `expected` / `tolerance` / `source ∈ {step_observed, target_readback, audit_event}`）、`MIN_VERIFICATION_ASSERTIONS = 3`、`REQUIRED_VERIFICATION_SOURCES = ("step_observed","target_readback")`，并在 `:102` 注明「W07 只披露，不执行比对，供 W08 Verifier 解析」；审批披露快照已持久化 `verification_plan.assertions`（`core/approval/disclosure.py:120-151`），W07 实际冻结了三条断言（`docs/w07/02-design.md:344-348`）：`affected_rows=1`、`target_note_final=$params.note`、`snapshot_comparable=before==$params.expected_note && after==$params.note`。
  - 要求：
    1. `VerificationPlan` 从**审批创建时刻的快照**读取（不得实时重算、不得接受客户端提交），条数 <3 或缺 `step_observed`/`target_readback` 来源 → 构造失败（fail closed）。
    2. `VerificationResult` 逐条断言记录：`assertion_id`、`verdict ∈ {PASS, FAIL, UNKNOWN}`、`observed`（实测值，非布尔）、`source`、`collected_at`、`evidence_ref`（`step_observed.observed_id` 或回读快照 id）；整体 `verdict` 取三者最差（任一 FAIL → FAIL；无 FAIL 但有 UNKNOWN → UNKNOWN）。
    3. `expected` 中的 `$params.*` 必须由**服务端**从已消费审批的 `action_descriptor.parameters` 解析；未解析出的占位符 → 该断言 `UNKNOWN`（不得按字面量比较）。
    4. 缺失 `step_observed` / 回读失败 / 超时 → 对应断言 `UNKNOWN`，**不得** `PASS`；`UNKNOWN` 不得被汇总为 `PASS`。
    5. **入口推进（A3b）**：`EXECUTING → VERIFYING` 由 `Verifier` 入口负责推进——`core/tasks/state_machine.py:69-77` 允许该跳，但当前**无任何调用方**（`core/tasks/service.py:171` 只是 `_IN_FLIGHT_STATUSES` 集合成员，不是转换点），W07 执行成功后任务实际停在 `EXECUTING`；若已处于 `VERIFYING` 则跳过。Verifier 内部异常 → 落 `NEEDS_RECONCILIATION` 并发 `VERIFICATION_FAILED(reason=VERIFIER_ERROR)`，**不得**卡在 `EXECUTING`。
  - **可机器验证**：`tests/unit/test_verification_plan.py`（新建）——① `VerificationPlan.from_disclosure` 对 2 条断言抛 `DISCLOSURE_INCOMPLETE`；② `$params.note` / `$params.expected_note` / 未知占位 `$params.x` 三例解析结果分别为值、值、`UNKNOWN`；③ `{"PASS","PASS","UNKNOWN"} → UNKNOWN`、`{"PASS","FAIL","UNKNOWN"} → FAIL`；④ `Verifier.__init__` / `verify()` 的签名中**不存在** `model_output` / `llm` / `narrative` 形参（`inspect.signature` 断言，证明不自证）；⑤ **入口推进**：任务处于 `EXECUTING` 时调用 `verify()` → `status == VERIFYING`；已处于 `VERIFYING` 时再次调用不重复推进（`state_version` 不变）；`verify()` 内部抛错 → `status == NEEDS_RECONCILIATION` 且 `task_events` 含 `VERIFICATION_FAILED(reason=VERIFIER_ERROR)`。

- **P0-W08-02** `[D1]` **异路径采集：至少一条关键断言不走执行路径。**
  - 要求：
    1. `target_readback` 类断言必须用**只读连接**（`dg_ro`，`read_only=True`）在**执行事务之外**重新查询目标行；不得用 `step_observed.result` 或执行连接的回执替代。
    2. `step_observed` 类断言读取的是**只追加证据表**（`dg_system.step_observed`），与写路径的数据库连接不同。
    3. 采集端口只允许**固定只读方法**（如 `read_note(order_id)`），**禁止任意 SQL 入参**（延续 W07 `TargetProbePort` 口径）。
  - **可机器验证**：`tests/integration/test_w08_verifier_readback.py`（新建）——① 捕获 Verifier 使用的 client，断言其 `read_only is True` 且 `id(client) != id(执行 client)`；② 人为把 `step_observed.result` 改成错误值，`target_readback` 断言仍按**实际库值**判定（证明未复用执行回执）；③ `ReadbackPort` 无接受 SQL 字符串的方法（签名检查）+ 传入含 `;`/注释的入参被拒。

- **P0-W08-03** `[D2]` **功能 + 性能 + 副作用验证；收益不足或断言不满足即不得宣称成功。**
  - 要求：
    1. 功能：修复前后对照故障断言、对象状态、业务数据一致性、错误率；每条都要有 `observed`。
    2. 性能：固定 workload + 预热 + 重复测量（默认 `N=5`，可配置），报告 **P50 / P95** 与耗时分布；Buffers/CPU/IO 可用则记录，缺失标 `UNKNOWN`。
    3. 副作用：写入开销、存储增长、锁等待、长事务、资源预算；任一超阈值 → `FAIL`（阈值缺省时标 `UNKNOWN`，不判 `PASS`）。
    4. **性能收益不足**（P95 未改善或改善在噪声内）→ 报告字段 `claimed_success=False`，且不得把任务推进 `COMPLETED`。
    5. Verifier 自身须有**模拟失败**测试：断言不满足时拒绝 `COMPLETED`（任务停在 `VERIFYING`→`NEEDS_RECONCILIATION` / `MANUAL_INTERVENTION` / `FAILED`，具体出边由架构师在设计中定，**不得** `COMPLETED`）。
  - **可机器验证**：`tests/unit/test_verification_perf.py`（新建）——① 给定固定序列 `[10,20,30,40,50]`，P50/P95 与预期逐值相等（含 `N=1` 退化为该值、`N=0` 抛 `REQUEST_INVALID`）；② `performance_gain_insufficient=True` 时 `result.verdict != PASS` 且 `report.claimed_success is False`；③ 副作用断言构造超阈值 → `FAIL`。`tests/integration/test_w08_verifier_rejects.py`（新建）——注入 FAIL 断言后 `task.status is not TaskStatus.COMPLETED`，且 `task_events` 含 `VERIFICATION_FAILED`；注入 UNKNOWN 后同样不 `COMPLETED` 且落 `NEEDS_RECONCILIATION` 或 `MANUAL_INTERVENTION`。

- **P0-W08-04** `[D3]` **补偿是独立动作：独立 digest、独立审批、独立审计，不叫「回滚」。**
  - 要求：
    1. 补偿动作（把 `note` 写回 `before`）必须重新计算 `action_digest`（参数含 `compensation_of=<原 approval_id>`，nonce 不同）；`compensation.action_digest != original.action_digest`。
    2. **不得复用原 `approval_id`**；用原审批驱动补偿必须被拒（`APPROVAL_ALREADY_CONSUMED` 或 `APPROVAL_DIGEST_MISMATCH`）。
    3. 补偿须走完整链：`Prepare → Approve → Gateway 复核 → 消费 → StepPrepared → 执行 → StepObserved → 审计`；缺任一环不得执行。
    4. 契约与报告字段必须标注 `is_rollback=false`；**不得**在任何面向人的字段里把补偿描述为「回滚 / rollback / 安全回滚」。
  - **可机器验证**：`tests/unit/test_compensation_contract.py`（新建）——① 同一 `before` 值构造两次补偿，`action_digest` 相等（确定性）；② 与原动作的 `action_digest` 必不相等；③ `compensation.is_rollback is False`、`label` 不含 `rollback`（大小写不敏感）；④ 未带新审批的补偿调用 → `APPROVAL_REQUIRED`。`tests/integration/test_w08_compensation.py`（新建）——用原 `approval_id` 驱动补偿 → 拒绝且目标行不变；正常补偿 → 审计含 `COMPENSATION_REQUESTED` / `COMPENSATION_APPROVED` / `COMPENSATION_EXECUTED` 各 1 条。

- **P0-W08-05** `[D3]` **结果处置三分法；UNKNOWN 不得自动补偿。**
  - 要求：每条验证结果必须给出 `disposition ∈ {SAFE_TO_COMPENSATE, HUMAN_ADVICE_ONLY, MANUAL_INTERVENTION}`，且满足矩阵：`FAIL + 证据充分 → SAFE_TO_COMPENSATE`；`FAIL + 证据不足 → HUMAN_ADVICE_ONLY`；`UNKNOWN → MANUAL_INTERVENTION`（或 `NEEDS_RECONCILIATION` 先行核对）；`UNKNOWN → SAFE_TO_COMPENSATE` **必须被拒绝**（不得自动补偿）。
  - **可机器验证**：`tests/unit/test_disposition_policy.py`（新建）——3×3 矩阵逐格断言允许/拒绝；`resolve_disposition(verdict="UNKNOWN", evidence_sufficient=True)` 抛 `POLICY_DENIED`；任何 `SAFE_TO_COMPENSATE` 结果必须携带 `approval_required=True`。

- **P0-W08-06** `[D4]` **Agent Recovery Controller（循环检测 / 反证关闭 / 一次重试 / 核对 / 不重放）。**
  - 现状（已核实）：`core/agent/loop.py:90-91` 已有 `max_duplicate_calls=2`、`no_progress_threshold=2`，检测点在 `:189`、`:278`；**但那是 W04 Agent Loop 的规划期循环检测，不是 W08 的恢复器**；`core/execution/reconciler.py` 是 W06 P0-18/19 的「已 Prepared 未 Observed」收敛。W08 必须在二者之上实现**恢复语义**，不得把既有组件算作已实现。
  - 要求：
    1. 重复工具调用：同一 `call_key` 第 `max_duplicate_calls` 次 → 停止并发 `AGENT_LOOP_DETECTED`，payload 含 `call_key` / 次数 / 已获得的 evidence 列表。
    2. 无信息增益：连续 `no_progress_threshold` 次 evidence digest 相同 → `AGENT_NO_PROGRESS`，必须给出**人可读的停止解释**（字段非空）。
    3. 假设被反证：`Hypothesis.status` 置 `REFUTED`，历史保留（不得物理删除），并触发重规划（`plan_version` +1）。
    4. 模型输出非法：最多一次修复性重试；第二次仍非法 → `MODEL_OUTPUT_INVALID` 终止，**不得**第三次。
    5. `UNKNOWN_OUTCOME` → `NEEDS_RECONCILIATION`，按 `action_digest` / `idempotency_key` / `target_objects` / DB 日志 / 审计核对；证据不足 → `MANUAL_INTERVENTION`。
    6. 恢复后**不得按旧任务状态重放写**：`Reconciler` 结构上不持有 tool registry（`core/execution/reconciler.py:1-8` 已如此设计，须保持），且恢复前后 `tool_calls` 计数不变。
  - **可机器验证**：`tests/unit/test_recovery_controller.py`（新建）——① 同一 `call_key` 第 1 次不触发、第 2 次触发 `AGENT_LOOP_DETECTED`（边界值）；② 连续 2 次相同 digest → `AGENT_NO_PROGRESS` 且 `explanation` 非空；③ 非法输出重试计数 == 1（第 2 次非法即终止）；④ `REFUTED` 后 `plan_version` 递增且历史 hypothesis 仍可查。`tests/integration/test_w08_recovery_e2e.py`（新建）——UNKNOWN 后 `task.status == NEEDS_RECONCILIATION`；恢复流程前后 `COUNT(tool_calls)` 不变；证据不足用例落 `MANUAL_INTERVENTION`。

- **P0-W08-07** `[D4]` **Reconciler 接入生产调度（W07 交接 #1）。**
  - 现状（已核实）：`core/execution/reconciler.py:73 sweep(tenant_id=None, older_than_s=60.0)` 已实现分页收敛与 `NEEDS_RECONCILIATION` / `MANUAL_INTERVENTION` 判定，但全仓**只有测试调用**（`tests/integration/test_step_prepared.py:155/181/210/384/534`、`test_w06_acceptance.py:996/1049`），**无生产调度入口**。
  - 要求：在 `WorkerRunner`（`workers/runner.py:223 run_once` / `:252 run_forever`）中按可配置周期调用 `sweep`；`RunReport` 增加 `reconciled` 计数（与 `executed`/`denied`/`failed` 并列）；`sweep` 抛错**不得**终止 Worker 主循环，须记 `RECONCILIATION_SWEPT` / `RECONCILIATION_FAILED` 事件并继续。
  - **可机器验证**：`tests/unit/test_worker_scheduler.py`（新建）——`run_once()` 中 `sweep` 被调用恰好 1 次（mock 计数）；`sweep` 抛 `DB_UNAVAILABLE` 时 `run_once` 返回 `clean=False` 且循环不中断；配置项 `DG_RECONCILE_INTERVAL_S` / `DG_RECONCILE_OLDER_THAN_S` 被读取（默认 60/60）。`tests/integration/test_worker_process.py`（扩展）——构造 `prepared without observed` + `python -m workers --once` → 任务转 `NEEDS_RECONCILIATION` 且 `tool_calls` 计数不增。

- **P0-W08-08** `[D5]` **崩溃 / 取消 / 竞态演练矩阵（6 类注入 + PAUSE/CANCEL）。**
  - 要求：每类独立用例，各自的期望终态与「零重复副作用」断言：① `StepPrepared` 后 Worker 崩溃；② DB 已执行但响应丢失；③ 连接中断；④ 租约过期；⑤ 双 Worker 并发；⑥ 重复消息投递；⑦ `PAUSE` / `CANCEL` 与运行中 SQL 竞态。
  - **可机器验证**：`tests/integration/test_w08_crash_matrix.py`（新建，7 个独立用例）——每类必须断言：`tool_calls` 增量 ≤1；目标行 `note` 只变一次或未变；`task_events.seq` 连续无洞；旧 Worker 得 `FENCING_TOKEN_STALE`；`PAUSE` 不改变正在执行语句的状态、`CANCEL` 经 `CANCEL_REQUESTED` 落到 `CANCELLED` 或 `NEEDS_RECONCILIATION`；全程无 `TOOL_CALL_RETRIED`（非幂等写）。另需 `docs/w08/*-drill-report.md` 恢复演练报告，列明「可自动恢复 vs 必须人工介入」的边界。

### P1（本周期内可做）

- **P1-W08-01** `[交接#2]` **KD-2 处置**：`docker-compose.yml:155-162` 的 `tool_gateway` 占位服务与实现相反（Gateway 实际在 `workers/stack.py:217/:229` 与 Worker 同进程装配）。二选一：删除，或改名 `gateway_legacy_placeholder` 并加注释指向 `docs/w07/02-design.md` §4.2。**AC**：`docker compose config` 输出中该服务不存在或已改名且带说明；`tests/unit/test_worker_layering.py` 的「gateway 只在 `workers/stack.py` 装配」断言仍通过。
- **P1-W08-02** `[交接#3]` **prepare probe 迁出评估（需 ADR）**：评估把 `MysqlPrepareProbe` 从 `apps/api/deps.py:118-132` 迁出，使 `api` 可只留 `control_net`。**AC**：新增 ADR（含替代探针设计与「迁移前不得摘网」结论）；**PRD 明确：不得把「摘 api 的 `lab_net`」写成需求**，现有 `POST /v1/approvals` 行为不得回归（`tests/integration/test_approval_api.py` 全绿）。
- **P1-W08-03** `[交接#4]` **Docker 容器运行态实跑（补偿 NV-1）**：`docker build -f deploy/Dockerfile.worker` + `docker compose up worker` 实跑一次。**AC**：构建与启动日志归档；容器内 `python -m workers --self-check` 退出码 0；`docker inspect` 证明 worker 只挂 `control_net` + `lab_net`、不含 `model_net`。
- **P1-W08-04** `[交接#5]` **W07 冻结件补齐**：补 `docs/w07/05-freeze.md` 与 `06-conformance-audit.md`。**AC**：两份文件存在，冻结件四件套（审批协议 / digest 向量 / 审计 Schema / 故障演练报告）可追溯至 `7d284dc`；符合性审计表逐项 PASS/PARTIAL/FAIL 且有证据位置。
- **P1-W08-05** **验证确定性**：同一 `tool_call_id` 重复执行 `verify()` ≥10 轮，判定结果必须 100% 一致（`observed` 值可因数据变化不同，但 `verdict` 不得抖动）。**AC**：`tests/unit/test_verification_determinism.py`（新建）10 轮 `verdict` 集合大小为 1。

### P2（明确延期，不进 W08 门禁）

只读验证查询 API（`GET /v1/tasks/{id}/verification`，待 W09/W10 前端与报告需要时）；验证结果长期趋势与 Benchmark 集成（W10）；多级/会签补偿审批（W09+）；真实 WORM/对象锁接入与 `dg_ctl` 账号拆分（W11）；知识化「哪种修复容易验证」的经验沉淀（W09 Memory）。

## 5. 验收标准（可机器验证）

> 每条判据必须落到「测试文件 + 具体断言」，不接受「全链可复现」这类松标准。

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | 三条断言自动比对 | `tests/unit/test_verification_plan.py`（新建） | `[a.assertion_id for a in plan] == ["affected_rows","target_note_final","snapshot_comparable"]`；每条 `verdict ∈ {PASS,FAIL,UNKNOWN}` 且带 `observed` 与 `source` |
| AC-02 | `$params` 由服务端解析、未知占位不猜 | 同上 | `$params.note`→目标值；`$params.expected_note`→基线值；`$params.x`→`UNKNOWN`（且不抛未捕获异常） |
| AC-03 | 汇总语义不注水 | 同上 | `{"PASS","PASS","UNKNOWN"}→UNKNOWN`；`{"PASS","FAIL","UNKNOWN"}→FAIL`；`{"UNKNOWN","UNKNOWN","UNKNOWN"}→UNKNOWN` |
| AC-04 | Verifier 不自证 | 同上 | `inspect.signature(Verifier.verify)` 形参集合中无 `model_output`/`llm`/`narrative`；向 `VerificationPlan` 注入模型文本字段 → `ValidationError`（`extra="forbid"`） |
| AC-05 | 异路径采集 | `tests/integration/test_w08_verifier_readback.py`（新建） | Verifier client `read_only is True` 且 `id != id(执行 client)`；篡改 `step_observed.result` 不改变 `target_readback` 判定；`ReadbackPort` 无 SQL 字符串入参 |
| AC-06 | 性能统计正确 | `tests/unit/test_verification_perf.py`（新建） | `[10,20,30,40,50]` 的 P50==30、P95==50；`N=1` 退化；`N=0` 抛 `REQUEST_INVALID` |
| AC-07 | 收益不足不宣称成功 | 同上 | `performance_gain_insufficient=True` → `verdict != PASS` 且 `claimed_success is False` |
| AC-08 | 断言不满足不得 COMPLETED | `tests/integration/test_w08_verifier_rejects.py`（新建） | FAIL 与 UNKNOWN 两例均 `task.status is not COMPLETED`；事件含 `VERIFICATION_FAILED`；UNKNOWN 落 `NEEDS_RECONCILIATION` 或 `MANUAL_INTERVENTION` |
| AC-09 | 缺失数据不判通过 | `tests/unit/test_verification_plan.py` | 删除 `step_observed` 行 → `affected_rows` 与 `snapshot_comparable` 均为 `UNKNOWN`；整体 `verdict == UNKNOWN` |
| AC-10 | 补偿独立授权 | `tests/unit/test_compensation_contract.py` + `tests/integration/test_w08_compensation.py`（新建） | `compensation.action_digest != original.action_digest`；复用原 `approval_id` → 拒绝且目标行不变；`is_rollback is False`；三项 `COMPENSATION_*` 事件各 1 条 |
| AC-11 | 补偿不称「回滚」 | 同上 | 所有面向人字段（`label`/`description`/`report_text`）不含 `rollback`（大小写不敏感） |
| AC-12 | 处置矩阵 | `tests/unit/test_disposition_policy.py`（新建） | 9 格逐格断言；`UNKNOWN→SAFE_TO_COMPENSATE` 抛 `POLICY_DENIED`；`SAFE_TO_COMPENSATE` 必带 `approval_required=True` |
| AC-13 | 循环检测边界 | `tests/unit/test_recovery_controller.py`（新建） | 同一 `call_key` 第 1 次不触发、第 2 次触发 `AGENT_LOOP_DETECTED`；连续 2 次相同 digest → `AGENT_NO_PROGRESS` 且 `explanation` 非空 |
| AC-14 | 非法输出只重试一次 | 同上 | 非法输出第 1 次触发 1 次重试、第 2 次终止 `MODEL_OUTPUT_INVALID`；`retry_count == 1` |
| AC-15 | 反证关闭且保留历史 | 同上 | `Hypothesis.status == REFUTED`；`plan_version` 递增 1；原 hypothesis 仍可查（不物理删除） |
| AC-16 | UNKNOWN 收敛不重放 | `tests/integration/test_w08_recovery_e2e.py`（新建） | `status == NEEDS_RECONCILIATION`；恢复前后 `COUNT(tool_calls)` 相等；证据不足用例 `status == MANUAL_INTERVENTION` |
| AC-17 | Reconciler 进生产调度 | `tests/unit/test_worker_scheduler.py`（新建）+ `tests/integration/test_worker_process.py`（扩展） | `run_once()` 内 `sweep` 调用次数 ==1；`sweep` 抛错时 `clean=False` 且循环不中断；`--once` 后任务转 `NEEDS_RECONCILIATION` 且 `tool_calls` 增量 0 |
| AC-18 | 崩溃矩阵零重复副作用 | `tests/integration/test_w08_crash_matrix.py`（新建，7 用例） | 每例：`tool_calls` 增量 ≤1、目标行 `note` 变化次数 ≤1、`task_events.seq` 连续、旧 Worker `FENCING_TOKEN_STALE`、无 `TOOL_CALL_RETRIED` |
| AC-19 | PAUSE/CANCEL 语义 | 同上（第 7 例） | `PAUSE` 后运行中语句状态不变（不伪造「已停止」）；`CANCEL` 经 `CANCEL_REQUESTED` 落 `CANCELLED` 或 `NEEDS_RECONCILIATION` |
| AC-20 | 引擎与写面不回退 | `tests/unit/test_tools_contracts.py` + `tests/unit/test_write_tool_gate.py` | PG 工具集仍 7 个且全 `side_effect=="read"`；生产装配点 write 工具集合仍为空或仅 `LAB+mysql` 单一工具；不存在第二个写动作 |
| AC-21 | 验证确定性 | `tests/unit/test_verification_determinism.py`（新建） | 同一 `tool_call_id` 10 轮 `verdict` 集合大小 ==1 |

## 6. 待确认问题

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | Verifier 的采集路径选型：复用 `dg_ro` 现有 client，还是新建独立 `ReadbackPort`（走固定只读 SQL 模板）？ | **建议独立 `ReadbackPort`**，内部复用 `dg_ro` 连接；理由：与 `TargetProbePort` 同型、可签名检查「不接受任意 SQL」，且与执行 client 对象分离可被断言（AC-05） | 架构师（需写入设计） |
| Q2 | `VerificationResult` 存哪里：新表 `verification_results`（含回滚脚本），还是只写 `task_events` payload？ | **建议新表 + 事件引用**；理由：需要按 `tool_call_id` 查询、需要索引与租户过滤；仅事件 payload 难以稳定查询。表必须只追加（`dg_ctl` 无 UPDATE/DELETE） | 架构师 |
| Q3 | 验证失败后的任务出边：`FAILED` / `NEEDS_RECONCILIATION` / `MANUAL_INTERVENTION` 如何分派？ | **建议**：`FAIL` 且证据充分 → `FAILED`（可进补偿流程）；`FAIL` 但证据不足 → `MANUAL_INTERVENTION`；`UNKNOWN` → `NEEDS_RECONCILIATION`。**任一情形都不得 `COMPLETED`** | 架构师 + 主理人 |
| Q4 | 性能 P50/P95 是否作为门禁判据？ | **建议不作为绝对值门禁**：只记录 + 「收益不足不宣称成功」；绝对阈值需实测后由 W11 冻结（沿用「阈值必须实测」原则） | 主理人 |
| Q5 | 补偿审批复用 W07 单动作模板，还是新建模板？ | **建议复用同一模板 + 新增 `compensation_of`（指向原 `approval_id`）与 `is_rollback=false`**；不新建第二套审批协议 | 架构师 |
| Q6 | `sweep` 的调度频率与 `older_than_s` 默认值？ | **建议**配置项 `DG_RECONCILE_INTERVAL_S`（默认 60）、`DG_RECONCILE_OLDER_THAN_S`（默认 60）；`sweep` 必须可被 `--once` 触发一次（便于 CI 断言） | 架构师 |
| Q7 | 新增审计事件类型清单是否需一并冻结？ | **建议**新增并冻结：`VERIFICATION_PLANNED` / `VERIFICATION_RESULT` / `VERIFICATION_FAILED`、`COMPENSATION_REQUESTED` / `COMPENSATION_APPROVED` / `COMPENSATION_EXECUTED` / `COMPENSATION_DENIED`、`RECONCILIATION_SWEPT` / `RECONCILIATION_FAILED`、`AGENT_RECOVERY_STARTED` / `AGENT_RECOVERY_STOPPED`；命名由架构师统一 | 架构师 |
| Q8 | P1 四项（KD-2 / probe ADR / 容器实跑 / W07 冻结件）是否纳入 W8→W9 门禁？ | **建议不纳入门禁**，但必须在 W08 交接报告逐条列出状态；未完成项须登记 W09 或 W11 | 主理人 |

## 7. 明确不做（防范围蔓延）

| 不做 | 理由 |
|---|---|
| PostgreSQL 写路径、第二种写动作、第二个引擎开写 | 直接违反 W07 §一/§四与《总览》§五；W08 只验证既有唯一动作 |
| 给 API 加 `execute` / `consume` 端点 | 执行入口有意留在独立 Worker（`docs/w06/02-design.md:163,302`）；加端点等于新开攻击面 |
| 摘 `api` 的 `lab_net` | `apps/api/deps.py:118-132` 的 `MysqlPrepareProbe` 依赖 LAB 网络，摘网即打断 `POST /v1/approvals`；只做迁出评估（P1-W08-02） |
| 自动补偿 / 自动「回滚」 | 补偿是独立 R2 动作，必须人工审批；承诺自动补偿等于承诺跨库事务 |
| 采信模型或 Agent 的自然语言结论 | 违背 W08 核心命题；Verifier 构造上不接受该输入 |
| 前端 / 审批侧 UI | 本周无前端交付项（延续 W06/W07 界定） |
| 引入新的第三方依赖 | 沿用 `requirements.lock.txt`；任何新增需 ADR |
| 宣称 Exactly Once 或「修复必然成功」 | 跨库无分布式事务；只保证「一张审批至多消费一次」+「验证可复核」 |
| 把 P50/P95 绝对值写成 SLA | 阈值需实测冻结（W11），本周只记录 |

## 8. W07 → W08 交接落点

| W07 交接项 | 落点 |
|---|---|
| 1. Reconciler 落账（`sweep()` 仅测试调用） | **P0-W08-07**（生产调度 + `RunReport.reconciled` + AC-17） |
| 2. KD-2 `tool_gateway` 占位服务与实现相反 | **P1-W08-01**（删除或改名 + 说明；不阻断门禁） |
| 3. prepare probe 迁出评估（`MysqlPrepareProbe` 依赖 LAB 网络） | **P1-W08-02**（仅 ADR；**不摘网**，见 §7） |
| 4. Docker 容器运行态实跑（NV-1 补偿） | **P1-W08-03**（build + up + `--self-check` 0） |
| 5. W07 冻结件缺件（`05-freeze.md` / `06-conformance-audit.md`） | **P1-W08-04**（补齐并可追溯至 `7d284dc`） |
| 6. W07 PRD Q7「执行后自动比对验证计划」 | **P0-W08-01**（三条断言自动比对，AC-01～AC-03） |
| 7. W06 遗留「Auditor 无法读审批详情」 | 本周不扩权限矩阵；审计员经 `/v1/audit/events` + `verification_results` 取证（P2 再议只读视图） |

## 9. 交付物与门禁

| 交付物 | 形态 |
|---|---|
| Verifier 断言框架 | `core/verification/*`（`plan.py` / `verifier.py` / `readback.py` / `perf.py`）+ 单测 |
| Verification Plan/Result | 数据模型 + 持久化表（Q2）+ 按 `tool_call_id` 查询 |
| 补偿流程 | 独立动作契约 + 审批串联 + 三项审计事件 |
| Agent Recovery Controller | `core/recovery/*` + 停止解释字段 + 反证关闭 |
| Reconciler 调度 | `WorkerRunner` 集成 + `RunReport.reconciled` |
| 演练报告 | `docs/w08/*-drill-report.md`（7 类注入 + 边界说明） |

**W8 → W9 门禁（逐条可判）**：
1. 至少一个修复闭环通过独立验证（AC-01～AC-09 全绿，且验证报告范围与声明一致）；
2. 未知结果恢复可靠（AC-13～AC-17 全绿，`tool_calls` 零增长）；
3. 取消/暂停与 Worker 故障演练通过（AC-18/AC-19 全绿，7 类用例各自独立可定位）；
4. 补偿独立授权（AC-10～AC-12 全绿）。

未满足则按任务书缩小修复范围或仅发布诊断预览，**不得**以「验证器已写」替代门禁证据。
