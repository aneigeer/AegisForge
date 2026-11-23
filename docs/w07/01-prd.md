# W07 增量 PRD｜单引擎沙箱修复（MySQL LAB 单一 R2 动作）

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.13 + FastAPI + MySQL 8 控制库（延续既有栈；本周无前端交付项） |
| Project Name | `dbguardian_w07_single_engine_lab_repair` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W07_单引擎沙箱修复.docx` §一–§六；`docs/adr/ADR-0007-single-engine-lab-repair.md`；`docs/w07/00-action-spec.md`；`docs/w06/07-gate-review.md` §5/§6；`docs/w06/08-pm-ruling.md` |
| 前置门禁 | W06 门禁为「有条件放行」，唯一条件（PRD §4 硬规则①三节披露偏差）已由 `docs/w06/08-pm-ruling.md` 裁定为路径 2，本 PRD 是其实施载体 |
| 原始需求复述 | 「在 PostgreSQL 或 MySQL 中择一，完成一种范围有限、可预检、可审计、可独立验证的 R2 沙箱修复；另一个引擎继续只读。」门禁判定标准：至少一个引擎的一种有限 LAB 写操作可经人工审批执行，前置条件与结果可追踪；未通过则停止扩展写操作。 |

## 1. 产品目标

**一句话**：证明「一个引擎、一种有限 LAB 写操作，能经人工审批安全执行并可独立验证」——不多做第二个引擎、不多做第二种动作。

三个正交目标：

| # | 目标 | 判定口径 |
|---|---|---|
| G1 | **可执行** | `mysql_lab_update_order_note` 在 MySQL LAB 上真实改变 1 行 1 列，前后值可对照 |
| G2 | **可审批** | 批准前「风险等级 / 补偿方案 / 验证计划」三节由服务端事实填充并持久化；任一必需项缺失即阻止批准（fail closed） |
| G3 | **可追溯** | 六类非法执行全部被拒且拒因可定位；执行前基线可回放；另一引擎与 `PROD_READ_ONLY` 零写入口 |

## 2. 范围与「必须在批准前可见」的信息项

> 判定口径：审批人只看 `GET /v1/approvals/{approval_id}` 的响应即可做出批准决策，不依赖任何外部文档、聊天记录或口头说明。本节每一行都是该接口字段的硬要求。

### D1 引擎选择与动作规范

| 交付 | 文件 | 批准前必须可见的信息项 |
|---|---|---|
| 引擎选择 ADR | `docs/adr/ADR-0007-single-engine-lab-repair.md`（已存在） | 引擎名与版本（MySQL 8.0.46）、环境（仅 `LAB`）、被否决备选与否决理由、「另一引擎只读」的明确声明 |
| 动作规范 | `docs/w07/00-action-spec.md`（已存在） | 动作名 + 版本（`mysql_lab_update_order_note` / `1.0`）、**唯一 SQL 形态的可读原文**、目标白名单（`dg_lab` / `seed_orders` / `note` / `order_id`）、主键范围 2001..2200、影响面（恰好 1 行 1 列）、资源预算（1 条 UPDATE / note ≤128 / 5000 ms）、幂等键（`apr:<approval_id>`）、停止条件逐条 |
| 风险评估 | 同上 §7 | 风险等级 `R2` + 风险因子清单 + 每条风险的缓解措施 + 明确「补偿不是回滚」 |

### D2 Prepare 与前置条件

| 交付 | 批准前必须可见的信息项 |
|---|---|
| 前置条件检查集（9 类，见 P0-W07-02） | 每项 `check_id`、`status ∈ {PASS, FAIL, UNKNOWN}`、`observed`（实际观测值，非布尔标记）、`source`（`control_db` / `server_probe` / `target_probe`）、采集时间；**任一非 PASS 即拒绝创建审批** |
| 执行前基线 | `expected_note` 的原值（`null` 必须与「空字符串」区分显示）、基线采集时刻、基线所属 `environment_id` |
| 摘要输入 | `action_digest` / `prerequisites_digest` 的含义说明与参与字段清单（不得只显示哈希） |
| 失效规则 | 前置条件变化即旧审批失效、必须重新 Prepare + 重新审批 的明确文案 |

### D3 受限 Adapter 写

| 交付 | 批准前必须可见的信息项 |
|---|---|
| 执行账号与权限事实 | 执行连接使用的账号（`dg_rw`）、`read_only=false` 实测值、账号授权范围（仅 `dg_lab.*`，无跨库） |
| 参数 Schema | 封闭字段清单（`database` / `table` / `order_id` / `keys` / `note` / `expected_note`）与逐字段约束；明确「`extra=forbid`，多余字段即拒绝」 |
| Adapter 二次校验项 | 引擎版本、权限、超时、资源、目标对象，逐项列出校验内容（不接受任意 SQL / host / port / 表名 / 列名） |
| 执行前证据承诺 | `StepPrepared` 先落盘后执行、`StepObserved` 尽快落盘、审计不可用即拒绝执行 |

### D4 审批闭环与披露

| 交付 | 批准前必须可见的信息项 |
|---|---|
| 具体 diff | `before`（`expected_note` 实测值）→ `after`（`note` 目标值）逐字对照，不是「将修改一条记录」 |
| 目标 | `target_objects` 逐项展开（库 / 表 / 主键），不得只给数量 |
| 风险 | 有效风险等级 + 因子 + 模型提示是否被采纳（`effective_risk` / `factors` / `model_hint_accepted`） |
| 前置条件 | 9 类检查全文 + `prerequisites_digest` |
| 补偿 | 补偿动作描述 + 「补偿需新审批、不是回滚」标注 |
| 验证计划 | 断言清单（结构化，非「希望成功」） |
| 有效期 / 绑定 | `expires_at` + 剩余秒数；`environment_id` + 指纹、`policy_version`、四个 digest、`db_engine`、`tenant` |
| 状态语义 | 接口必须能区分「建议 / 等待审批 / 已执行 / 结果未知」四种状态，不得合并表述 |

### D5 单动作端到端验收

| 交付 | 交付件中必须可见的信息项 |
|---|---|
| 重复执行记录 | 每轮前后 `note` 实测值、`affected_rows`、耗时、`tool_call_id`、`approval_id` |
| 六类拒绝证据 | 每类的期望错误码、实际错误码、具体不符项（`channel` 或 `missing_fields`） |
| 引擎边界证据 | PG 只读工具清单（7 个，全部 `side_effect=read`）+ `PROD_READ_ONLY` 无写入口的断言结果 |
| 运行手册 | 操作步骤、前置条件核对清单、补偿与人工介入路径、结果未知时的处置流程 |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 审批人（Approver） | 作为审批人，我要在批准前看到这次操作到底改哪一行的哪个字段、从什么值改到什么值、失败后怎么补偿、成功如何验证，这样我才是在批准一个**可复算的操作**，而不是批准一个「修复」说法。 |
| U2 | 运维操作者（Operator） | 作为运维操作者，我要在 Prepare 阶段就被告知 9 类前置条件里哪一项没通过、观测值是多少，这样我不会把一张注定被拒的审批单递到审批人面前。 |
| U3 | 审计员（Auditor） | 作为审计员，我要能按 `action_digest` / `approval_id` / `tool_call_id` 复原「谁在何时批准了什么、执行前后目标对象是什么、被拒的是哪一项」，这样我能在事后独立判断这次写入是否越界。 |
| U4 | 安全评审 | 作为安全评审，我要看到六类非法执行（无审批、过期、改参数、换环境、越界目标、重复消费）全部被拒绝且有自动化测试证据，以及另一引擎与 `PROD_READ_ONLY` 在生产装配点没有任何写入口，这样我才认可「写入能力被限制在一个格子内」。 |
| U5 | 值班 SRE | 作为值班 SRE，我要在结果为未知时看到「进 `NEEDS_RECONCILIATION`、禁止自动重试、按 `before` 快照人工决策」的明确指引，这样我不会用一次盲目重试把一个未知结果变成两次写入。 |

## 4. 需求池

> P0 全部为 W7→W8 门禁必需，**不得降级、不得延期**。`[D1]`–`[D5]` 标注对应任务书交付项。

### P0（Must have，6 条）

- **P0-W07-01** `[D1][D4]` **披露三节必须有服务端事实来源并持久化，且缺失即阻止批准。**
  - 现状（已核实）：`apps/api/routes/approvals.py:93-118` 对「风险等级 / 补偿方案 / 验证计划」三节硬编码 `provided=false` + `reason=NOT_PERSISTED_IN_W06`；`core/approval/models.py:372-398` 的 `required` 字典只有 14 项，未含这三节，因此 `core/approval/service.py:312-320` 的 fail closed 不会触发。
  - 要求：
    1. 三节的事实来源必须是**服务端**，逐节指定：**风险等级** ← `core/security/policy.py::RiskResolver.resolve_risk()` 的 `RiskResolution`（`effective_risk` / `factors` / `notes` / `model_hint` / `model_hint_accepted`，在审批创建时刻计算并快照）；**补偿方案** ← 工具契约内声明的补偿定义（动作 + 是否可回滚 `is_rollback=false` + 是否需独立审批）；**验证计划** ← 工具契约内声明的断言清单（结构化：`metric` / `expected` / `tolerance`，至少覆盖 affected 行数、目标行 `note` 终值、前后快照可比对）。
    2. **不得接受客户端提交**这三节内容；请求体 `extra=forbid` 保持不变；缺失声明即创建审批失败。
    3. 三节必须在**创建时刻快照并持久化**到控制库（新增迁移，含回滚脚本），批准后不可被改写；批准/消费时读取的是快照，不是实时重算值——否则「显示的与批准的不是同一份数据」。
    4. 三节纳入 `missing_required_fields()` 必需项，缺失时 `approve()` 拒绝（`STATE_CONFLICT` + `missing_fields` 明确列出，或架构师指定更精确的稳定错误码，见 §6-Q3）。
    5. `GET /v1/approvals/{approval_id}` 三节 `provided=true`，`reason` 字段不再出现 `NOT_PERSISTED_IN_W06`。
  - **W07 门禁硬条件**：本条不达成，W07 门禁不通过（见 `docs/w06/08-pm-ruling.md` §4）。

- **P0-W07-02** `[D2]` **Prepare 前置条件扩展为 9 类，任一非 PASS 即拒绝创建审批（fail closed）。**
  - 要求检查集（`check_id` 由架构师定名，语义不得裁剪）：① 环境指纹与 `environment_id` 匹配（含 `fingerprint_completeness` 如实标注）；② 对象签名（目标行存在、唯一、当前值摘要）；③ 表大小（行数/数据长度）；④ 磁盘余量；⑤ 锁（目标行/表是否存在阻塞锁）；⑥ 长事务（是否存在长于阈值的活跃事务）；⑦ 当前负载（连接数/线程运行数等可比对指标）；⑧ 能力（工具声明的 `required_capabilities` 是否具备）；⑨ 权限（执行账号对目标对象的 DML 授权与 `read_only=false`）。
  - `status` 三值语义：`PASS` 放行；`FAIL` 拒绝；`UNKNOWN`（探针无法判定）**同样拒绝**，不得按 PASS 处理。
  - 每项必须携带 `observed` 实际值、`source`、`collected_at`；探针只允许固定检查 SQL，**禁止任意 SQL**（`TargetProbePort` 目前只有 `object_exists`，需按固定方法扩展，不得开放通用查询入参）。
  - 任一非 PASS → 创建审批失败，错误码 `APPROVAL_PREREQUISITE_FAILED`，`details` 必须含全部非 PASS 项的 `check_id` + `observed`（不是只报第一项）。
  - 客户端不可提交/覆盖任何检查结果。

- **P0-W07-03** `[D2]` **执行前基线持久化，且前置条件变化使旧审批失效。**
  - `expected_note` 必须参与 `parameters` 与 `action_digest`；`null` 与空字符串不得混同（`<=>` 语义，动作规范已定）。
  - 执行前基线（审批时刻的 `expected_note` + 执行时刻 `before.note` 实测值 + 采集时间）必须落盘可追溯：`step_prepared.request_json` 必须包含 `expected_note`，`step_observed` 必须包含 `before` 与 `after`；二者可按 `tool_call_id` 关联。
  - 前置条件变化（当前 `note` ≠ `expected_note`、环境指纹变化、工具/策略版本变化）→ 旧审批失效，拒绝执行且**不得写入任何行**；返回 `DENIED / PREREQUISITE_CHANGED` 或 `APPROVAL_DIGEST_MISMATCH` 并给出具体不符通道。
  - **收紧 W06 遗留 D7**：`side_effect=write` 的调用一律必须落 `step_prepared`，不存在「无 grant 只记 warning」的旁路。

- **P0-W07-04** `[D5]` **六类拒绝负测，每类有独立自动化用例。**
  - 六类与期望结果：无审批 → `APPROVAL_REQUIRED`；审批过期 → `APPROVAL_EXPIRED`；改参数 → `APPROVAL_DIGEST_MISMATCH`（`channel=parameters`）；换环境 → `APPROVAL_DIGEST_MISMATCH`（`channel=environment`/指纹）；越界目标 → 创建阶段 `APPROVAL_PREREQUISITE_FAILED`，执行阶段目标被替换则 `APPROVAL_DIGEST_MISMATCH`（`channel=target_objects`）、参数越界（如 `order_id=2300`）则 `POLICY_DENIED`；重复消费 → `APPROVAL_ALREADY_CONSUMED`。
  - 每类用例必须断言：目标库 `note` 值**逐字未变**、无 `step_observed` 记录、拒因可定位到具体通道或 `check_id`、审计中有对应拒绝事件。
  - 六类不得合并为一个参数化用例的「一锅断言」——每类都要能被单独定位失败。

- **P0-W07-05** `[D5]` **另一引擎（PostgreSQL）保持只读，`PROD_READ_ONLY` 零写入口。**
  - PG 侧 7 个工具（`get_connection_snapshot` / `get_query_stats` / `get_lock_wait_edges` / `get_lock_details` / `get_transaction_metadata` / `explain_query` / `get_table_stats`）全部 `side_effect=read`、`risk_level=R0`、`required_role=dg_ro`，数量与字段集合均不得变化；断言注册表内不存在 `db_engines` 含 `postgresql` 且 `side_effect=write` 的定义。
  - `PROD_READ_ONLY`（实现映射：`core/security/models.py:35` `environment_tier()` 将非 `LAB` 状态一律判为 `PROD`，未知/缺失 fail closed）——必须在**策略层**与**装配层**双重阻断：策略返回 `PROD_TARGET_NOT_ALLOWED`，且 LAB 专用写工具不得在非 LAB 装配点注册。
  - 断言方式：「列举生产装配点所有工具 → 过滤 `side_effect=write` → 集合必须为空或仅含 `allowed_environments=['LAB']` 且 `db_engines=['mysql']` 的单一工具」。

- **P0-W07-06** `[D3][D4]` **受限写工具必须真正接入唯一执行链（LAB 装配点）。**
  - 现状（已核实）：`adapters/mysql/lab_repair.py::LabOrderNoteUpdateTool` 已定义，但全仓**无任何生产装配点引用**（`adapters/registry.py`、`apps/` 均未注册），仅测试引用。
  - 要求：在 LAB 装配点注册该工具；`PROD_READ_ONLY` 装配点不注册（或注册也在策略层被拒并有测试）。执行链必须完整：`Prepare → Approve → Worker 租约 → Gateway 复核（签名/nonce/任务状态/lease/fencing/环境/审批状态/action_digest/前置条件）→ 消费审批 → StepPrepared → UPDATE → StepObserved → 审计事件`。
  - `SUCCESS` / `DENIED` / `TIMEOUT→UNKNOWN_OUTCOME` 三态都必须写审计；`UNKNOWN_OUTCOME` 必须转 `NEEDS_RECONCILIATION`，非幂等写**禁止自动重试**（不得放宽 `ToolExecutor._should_retry`）。
  - 幂等空操作（两次读之间值已达成）标 `idempotent_noop=true`，不重复计数；用同一份旧参数重复调用属 fail closed，**不算**幂等空操作。

### P1（本周期内可做）

- **P1-W07-01** `[D4]` 补齐「影响面」的 `reversible` / `affects_other_tasks` 服务端事实来源（当前 `apps/api/routes/approvals.py:88-91` 显式返回 `None` + `missing`）：本动作口径为 `reversible=true`（把 `before.note` 写回，作为独立 R2 动作）、`affects_other_tasks=false`（`note` 不参与外键/索引/约束）。
- **P1-W07-02** `[D4]` 连续多轮稳定性数据（≥10 轮干净快照重复执行，记录成功率与耗时分布），替代 W06 遗留的「T9 启动抖动 retry-once」掩盖口径。
- **P1-W07-03** 测试注入点：提供 `ToolRegistry.replace()` 或等价公开接口，消除 `gateway._registry._tools[...] =` 私有属性注入（W06 门禁 §6-3）。
- **P1-W07-04** 动作运行手册、补偿/人工介入说明、演示日志归档到 `docs/w07/`。

### P2（明确延期，不进 W07 门禁）

WORM/对象锁真实接入与合规评估；`dg_ctl` 账号拆分与 `ALTER TABLE … DROP COLUMN` 收口（W11）；DDL 类修复动作（MySQL 隐式提交 / PG INVALID 索引）；批量或多表写入；多级/会签审批；审批侧 UI 与 Auditor 只读审批视图（W06 门禁 §6-8，见 §6-Q6）；跨引擎审批模板；执行后验证计划的自动比对（建议 W08 承接）。

## 5. 验收标准（可机器验证）

> 每条判据都必须落到测试文件名 + 断言；不接受「体验良好」「基本可用」这类主观项。

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | 三节持久化且 `provided=true` | `tests/integration/test_approval_api.py`（新增 `test_disclosure_three_sections_persisted`） | `disclosure["风险等级"]["provided"] is True` 且 `effective_risk == "R2"` 且 `factors` 非空；`disclosure["补偿方案"]["provided"] is True` 且 `is_rollback is False`；`disclosure["验证计划"]["assertions"]` 长度 ≥2；三节均无 `reason=NOT_PERSISTED_IN_W06` |
| AC-02 | 三节为创建时刻快照、批准后不可改 | 同上（新增 `test_disclosure_snapshot_is_immutable_after_approve`） | 批准后对契约声明做变更（测试内替换工具定义），再 `GET` 取回的仍是旧快照值，且与 `action_digest` 同源可复算 |
| AC-03 | 缺失三节即阻止批准 | `tests/integration/test_approval_service.py`（新增 `test_approve_blocked_when_disclosure_section_missing`） | 构造声明缺失的写工具契约 → 批准抛错；`details["missing_fields"]` 含对应字段名；审批状态仍为 `PENDING` |
| AC-04 | 9 类前置条件齐备且非 PASS 即拒建 | `tests/unit/test_prerequisites_w07.py`（新建） | `{c["check_id"] for c in checks}` ⊇ 9 个 id；对每类构造 FAIL/UNKNOWN → `ApprovalService.create()` 抛 `APPROVAL_PREREQUISITE_FAILED`，`details` 含全部非 PASS 的 `check_id` |
| AC-05 | 探针不接受任意 SQL | 同上 | `TargetProbePort` 无接受 SQL 字符串的方法（签名检查）；注入含 `;`/注释的输入被拒 |
| AC-06 | 基线参与摘要 | `tests/unit/test_lab_repair.py`（扩展现有用例）/ `tests/unit/test_action_digest.py` | 仅改 `expected_note`（`"a"` → `"b"`，以及 `None` → `""`）`action_digest` 必变；两次相同输入摘要逐字节一致 |
| AC-07 | 前置条件变化 → 旧审批失效且零写入 | `tests/integration/test_w07_repair_e2e.py::test_w07_prerequisite_change_blocks_write`（已存在） | 结果 `DENIED`；`_current_note()` 与执行前逐字相等；`step_observed` 为空 |
| AC-08 | 六类拒绝矩阵 | `tests/integration/test_w07_rejection_matrix.py`（新建，6 个独立用例） | 每类：实际错误码 == 期望错误码；目标行未变；无 `STEP_OBSERVED`；审计含对应拒绝事件 |
| AC-09 | 重复消费唯一 | 同上 + `tests/unit/test_approval_lifecycle.py` | 并发两次消费只有一个成功；失败方 `APPROVAL_ALREADY_CONSUMED`；`approval_consumptions` 计数 == 1 |
| AC-10 | 端到端真实单行更新 | `tests/integration/test_w07_repair_e2e.py::test_w07_approved_repair_updates_lab_and_leaves_observed`（已存在） | `affected_rows == 1`；`_current_note()` == 目标值；`step_observed` 非空；任务非 `NEEDS_RECONCILIATION`；审计含 `APPROVAL_CONSUMED`/`STEP_PREPARED`/`TOOL_CALL_SUCCEEDED` |
| AC-11 | PG 只读不变 | `tests/unit/test_tools_contracts.py` / `tests/integration/test_adapter_registry.py`（新增断言） | PG 工具集合 == 7 个已知名称；全部 `side_effect=="read"` 且 `risk_level=="R0"`；不存在 PG 写工具 |
| AC-12 | `PROD_READ_ONLY` 零写入口 | `tests/unit/test_tools_contracts.py` + `tests/unit/test_write_tool_gate.py`（新增用例） | 对 `tier=PROD` 环境执行写工具 → `POLICY_DENIED` / `PROD_TARGET_NOT_ALLOWED`；生产装配点 write 工具集合为空或仅含 `allowed_environments==["LAB"]` 的单一工具 |
| AC-13 | 写工具已接入执行链 | `tests/integration/test_w07_repair_e2e.py` | `w07_stack["gateway"]` 内 `registry.get(TOOL_NAME)` 可命中，且 `definition.side_effect=="write"`；全链事件序列完整有序 |
| AC-14 | 未知结果不重试 | `tests/unit/test_executor.py`（沿用 W06 断言） | 写超时 → `UNKNOWN_OUTCOME`；`_should_retry` 对 write 返回 False；任务转 `NEEDS_RECONCILIATION` |
| AC-15 | 控制库账号边界不回退 | `tests/integration/test_db_permissions.py` | `dg_ctl` 对 `task_events`/`evidence` 无 UPDATE/DELETE/DROP；`approvals` 可 UPDATE 不可 DELETE |

## 6. 待确认问题

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | 三节的持久化粒度：`approvals` 加三列，还是加一列 `disclosure_json`？ | 由架构师定。PRD 只约束行为：创建时刻快照、批准后不可改、可按 `approval_id` 取回、迁移含回滚脚本 | 架构师 |
| Q2 | 三节是否纳入 `action_digest`？ | **建议不纳入**。W06 冻结的 digest 字段集不含三项，纳入需升 `action_digest_version` 并重算全部冻结向量；三节的完整性由「创建时刻快照 + 不可改」保证，而非摘要 | 架构师（需在设计中写明口径） |
| Q3 | 缺失三节时的错误码：沿用 `STATE_CONFLICT`，还是新增 `DISCLOSURE_INCOMPLETE`？ | 由架构师定。要求：稳定错误码 + `missing_fields` 明确列出 + 不与会话/状态冲突混用 | 架构师 |
| Q4 | 验证计划的结构化程度：自由文本清单，还是 `{metric, expected, tolerance}` 结构化断言？ | 建议结构化最小集（可被 W08 自动比对），至少 3 条：affected 行数、目标行 `note` 终值、前后快照可比对 | 架构师 + 主理人 |
| Q5 | 三节的声明载体：`ToolDefinition` 新增字段，还是外挂动作规范文件解析？ | 建议 `ToolDefinition` 新增字段（与既有 `preconditions` 同型），保证「服务端代码内声明、服务端填充」，不引入文档解析依赖 | 架构师 |
| Q6 | Auditor 是否需要只读审批详情视图（W06 门禁 §6-8）？ | 建议 W07 不做，登记 W08+。W07 已可通过 `/v1/audit/events` 按 `approval_id`/`action_digest` 取证；扩权限矩阵会改动 W06 冻结的 §6 接口表 | 主理人 |
| Q7 | 「执行后自动比对验证计划」是否属 W07？ | 建议不属 W07（W07 只负责生成、披露、人工比对），自动断言比对由 W08「独立验证与恢复」承接 | 主理人 |
| Q8 | `PROD_READ_ONLY` 是否需要在控制库 `environments.status` 引入该字面量？ | 建议不引入。现状 `environment_tier()` 已把非 LAB 一律判 PROD（fail closed）；新增字面量会改动 W01 冻结的环境清单 | 架构师 |

## 7. 明确不做（防范围蔓延）

| 不做 | 理由 |
|---|---|
| 第二个引擎开写、第二种写动作 | 直接违反 W07 §一与 §四；「宁可缩小为单对象单动作，也不扩大权限」 |
| 通用 SQL 写工具 / 任意 host、port、表名、列名 | 写入能力必须被白名单钉死在一个格子里 |
| DDL 类动作（索引、改表） | MySQL 隐式提交、PG INVALID 索引残留，补偿复杂度和风险超出本周 |
| 生产环境任何写入 | `PROD_READ_ONLY` 无写入口，本周不讨论例外 |
| 界面/前端 | 本周无前端交付项，披露以审批详情 API 字段完整性为准（延续 W06 §4 交付载体界定） |
| 宣称 Exactly Once 或「回滚」 | 控制库事务与目标库写入是两笔独立事务；只保证「一张审批至多消费一次」；补偿是需另审批的独立 R2 动作 |

## 8. W06 门禁遗留项 → 本 PRD 落点

| W06 门禁 §6 遗留项 | 落点 |
|---|---|
| 1. 三节持久化并纳入披露 | **P0-W07-01**（W07 门禁硬条件） |
| 2. 无 grant 的写操作只记 warning 不落 `step_prepared` | **P0-W07-03** 末条收紧为「任何 write 必须落盘」 |
| 3. T9 用例依赖 `_registry._tools` 私有属性注入 | **P1-W07-03** |
| 4. WORM 未实现 | **P2**（ADR-0006 已知限制，W11 前不作门禁） |
| 5. `dg_ctl` 仍可 `ALTER TABLE … DROP COLUMN` | **P2**，登记 W11（W07 用 `test_db_permissions.py` 防回退，AC-15） |
| 6. 审计历史行 `tenant_id` 为 `NULL` | 不做改动：查询走 `JOIN tasks` 权威路径（W06 已冻结口径）；AC-03/AC-08 的审计断言必须按 `task_id` 查，不得按 `tenant_id` 直查 `task_events` |
| 7. T9 启动抖动 retry-once | **P1-W07-02** 连续多轮稳定性数据 |
| 8. Auditor 无法读审批详情 | **§6-Q6**（建议 W08+） |
