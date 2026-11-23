# W06 增量 PRD｜审批事务与追加式审计

修订：2026-11-09 v6（P0-03b 改为 `list[TargetObjectSpec]`，语义改为"契约声明抽取规则、对象值由服务端从参数抽取"，摘要通道改为独立校验通道。v5 §7 三项定稿；v4 A3 收口去掉 DROP；v3 回填主理人 3 项决策、T9 稳定性；v2 按架构裁定修正 4 处表述）

- 项目名：`dbguardian_w06_approval_audit`
- Language：简体中文（标识符与路径用英文）
- 技术栈：沿用既有（Python 3.14.7 + FastAPI + Pydantic v2 + 控制库 MySQL 8.0.46），**不新增第三方依赖**
- 基线：`d2ceae8`（W05 全量 660 passed）；本周只做增量，不重做 W05
- 上游依据：`docs/w06/00-task-book.md`（最高优先级）、`docs/w06/00-context.md`、ADR-0004、`docs/security/identity-and-policy.md`

**原始需求复述**：把"人工同意"从内存里的一个布尔值，升级为**绑定到某一次具体操作、只能消费一次、可被独立验证**的授权凭证；同时把所有关键决策与执行证据改为只可追加、可校验完整性的审计记录；并且承认跨库无分布式事务，结果未知时必须如实上报而不是重试掩盖。

## 1. 产品目标

**一句话质变**：W06 交付后，R2 写操作从"任何情况下都被 `R2_REQUIRES_APPROVAL_W06_CLOSED` 恒定拒绝"（现状见 `core/security/policy.py` 第 230–238 行）变为"在 LAB 环境下，凭一张绑定唯一 `action_digest` 的一次性审批，可执行**恰好一个**受控写操作，且该操作的过程与结果可被第三方独立复核"。

三个正交目标：

1. **可授权且不可滥用**：审批不能被抽象批准、不能被换参数复用、不能被两个人同时用掉、不能过期后偷偷生效。
2. **可追溯且不可抵赖**：审批、策略决策、执行、未知结果全部落入只追加账本；应用运行账号在**表级权限层面**无法改写审计历史，篡改可被发现并定位（DDL 残余风险见 §9）。
3. **不撒谎**：结果未知即 `UNKNOWN_OUTCOME` → `NEEDS_RECONCILIATION`；不自动重试非幂等操作；不宣称 Exactly Once。

## 2. 角色与用户故事

| # | 角色 | 触发条件 | 期望结果 | 失败时的系统行为 |
|---|---|---|---|---|
| U1 | Operator | 某 R2 工具在 LAB 被策略判为"需审批" | 创建审批单，拿到 `approval_id` 与被批准的摘要原文，可随时查询状态 | 缺 `approval:create`→`FORBIDDEN`；环境非 LAB→拒绝创建（不得为 PROD 建立任何待批单）；参数无法规范化→`REQUEST_INVALID` |
| U2 | Approver | 打开待批审批单 | 看到第 4 节全部字段后可批准或拒绝；批准后该单只能被消费一次 | 无 `approval:decide`→`FORBIDDEN`；单已过期/已撤销/已消费→显式状态错误码，不得静默改为成功；字段缺失→禁止批准 |
| U3 | Administrator | 查看审批列表/详情 | 可见全部审批与审计记录 | 尝试裁决→`FORBIDDEN`（职责分离，现状 `roles.py` 已保证 Administrator 无 `approval:decide`，**W06 不得为此新增该权限**） |
| U4 | Auditor | 事后需要证明"发生了什么" | 按任务/审批/时间查审计事件，并能触发完整性校验得到"通过"或"在 seq=N 处断裂" | 无 `audit:read`→`FORBIDDEN`；校验失败→返回断裂位置与受影响区间，不得返回仅"失败"两字 |
| U5 | ServiceWorker | 拿到租约与信封，准备执行已批准步骤 | 消费审批成功→执行；失败→不进入执行 | 消费失败（已消费/过期/摘要不符/策略版本不符）→不调用工具，返回结构化拒绝 |
| U6 | Operator | 发现审批对象有误，想撤回 | 调用撤销，审批立即不可消费 | 已消费的审批不可撤销→`STATE_CONFLICT`，并记录撤销尝试的审计事件 |

## 3. 需求池

> P0 全部为 W6→W7 门禁必需，**不得降级或延期**。`[D1]`–`[D4]` 标注对应任务书交付物，`[A1]`–`[A5]` 标注对应任务书第四节验收标准。

**A. 审批数据模型与操作摘要**

- **P0-01** `[D1]` 审批记录承载任务书 D1 列举的全部字段：`tenant/task/step/environment`、`environment_fingerprint`、`db_engine`、`action_digest`、`prerequisites_digest`、`policy_version`、`tool/version`、`requested_by`、`approver_id`、`authn_context`、`expires_at`、`nonce_hash`、`status`。字段不得以"可从别处推导"为由省略。
- **P0-02** `[D1]` 实现规范化算法并落 ADR-0005：计算 SHA-256 前先做规范化，产出 `action_digest` 与 `prerequisites_digest`；必须提供冻结测试向量（同一逻辑操作多次计算摘要必须一致，键顺序/空白差异不得改变摘要）。**规范化范围经决策为 RFC 8785 JCS 的受约束子集**：UTF-16 码元键序、紧凑分隔符、非 ASCII 不转义；数值只接受 `int`（`|v| ≤ 2^53−1`）与十进制字符串，`float`/`Decimal`/`datetime` **一律报错**，不得降级为字符串或四舍五入。子集内必须与 JCS 逐字节一致；ADR-0005 与所有对外文档**不得宣称完整 JCS 实现**；完整合规需松绑依赖冻结，本轮不做。
- **P0-03** `[D1]` 摘要必须纳入：`tenant`、`environment`、`environment_fingerprint`、`db_engine`、`tool_name`+`tool_version`、`normalized_operation`、`target_objects`、`parameters`、`policy_version`。`environment_fingerprint` 的事实来源为 `environments` 表的 `db_engine`/`db_version`/`image_digest`/`config_version`/`network_id`，不得凭空生成。
- **P0-03b** `[D1]` `target_objects` 由工具契约声明、由服务端抽取，客户端**不可提交**（现状 `ToolDefinition` 无此字段且 `extra="forbid"`，`policy.py` 的 `target_objects` 入参从未被任何工具填充）。落地形态与 `docs/w06/02-design.md` §2.8 / §11.1 一致：**① 签名为 `target_objects: list[TargetObjectSpec] = Field(default_factory=list)`；② spec 只声明抽取规则（`object_type`/`database_field`/`name_field`/`schema_field`/`keys_field`/`required`），对象值由 `extract_target_objects()` 从**已规范化参数**抽取，客户端不可提交 `target_objects`；③ 既有 10 个只读工具不声明即默认空列表，构造点零改动；④ fail closed 两道——`model_validator` 中 `side_effect=="write"` 且为空即抛错（只读工具空列表合法），`ApprovalService.create()` 再校验抽取结果非空，否则 `APPROVAL_PREREQUISITE_FAILED`（`reason=TARGET_OBJECTS_UNDECLARED`）**。本周只有测试桩工具声明 spec；桩工具**不得进入生产注册表**（`adapters/registry.py`、`build_observer_registry` 均不可注册）。
  **摘要与通道（必须按此实现）**：对象值来自**本次** `parameters`，故 `target_objects` **不随 `tool.version` 冻结**——"这次到底动了哪张表/哪些行"由 `channel=target_objects` 这一**独立校验通道**判定（消费时先比 `target_objects_digest`，可在不改参数的情况下定位目标对象不符）；而修改**抽取规则**本身会经 `tool_version` 变更使旧审批失效。两个通道都要各自有测试证据，不得只测其一。
- **P0-04** `[A1]` 消费时**重新计算**摘要与指纹并与审批记录比对；不得只信任登记时刻写入的值。

**B. 审批生命周期与失效**

- **P0-05** `[D2]` 状态集合：`PENDING / APPROVED / REJECTED / EXPIRED / REVOKED / CONSUMED`。仅允许：`PENDING→APPROVED|REJECTED|EXPIRED|REVOKED`；`APPROVED→CONSUMED|EXPIRED|REVOKED`；其余终态不可迁出。非法迁移→`STATE_CONFLICT`。
- **P0-06** `[D2][A2]` 原子单次消费：在控制库单事务内依次校验"状态为 APPROVED、未过期、nonce 未消费、四类指纹均匹配"，再条件更新为 `CONSUMED`；并发请求只有一个成功，其余返回 `APPROVAL_ALREADY_CONSUMED`。
- **P0-07** `[D2][A2]` "只能消费一次"必须由**数据库约束**保证，不能靠应用层先查后写：由 `approval_consumptions.approval_id` 与 `approval_consumptions.nonce_hash` **两个 UNIQUE 约束共同保证**，任何重复消费必然撞唯一键失败，不依赖应用层判断。
- **P0-08** `[A1]` 失效触发集，任一变化使既有审批失效：`parameters`、`target_objects`、`environment`、`environment_fingerprint`、`policy_version`、`tool_version`、过期、撤销、已消费。失效必须显式体现为状态或消费拒绝原因，不得静默忽略。
- **P0-09** `[D2]` 职责分离：`approver_id` 不得等于 `requested_by`（含同一用户以不同会话操作的情形）；违反→`FORBIDDEN` 且写审计。
- **P0-10** `[D2]` 审批 API：创建/查询/批准/拒绝/撤销均可用，且全部写入审批审计事件（谁、何时、对哪张单、从什么状态到什么状态）。**拒绝必须携带理由（必填），批准理由可选**（已决策）。撤销权限为申请人本人 + Approver，`Administrator` 不可撤销，`CONSUMED` 不可撤销。

**C. 审计服务与完整性**

- **P0-11** `[D3]` 追加式审计事件包含：`actor`、`tenant`、`task`、`step`、`action_digest`、`policy_version`、`decision`、`result`、`timestamp`、`trace_id`、`integrity_ref`。审计历史**统一落在 `task_events`**（已有 `(task_id, seq)` 唯一 + `previous_event_hash` + `event_hash` 哈希链，见 `core/tasks/repository.py::insert_event`），不另建可更新审计表。沿用既有脱敏规则（`core/audit/events.py::redact_payload`），不得把 Token、DSN、密码写入。
  **`tenant` 字段的实现约束**（架构设计已含 `tenant_id VARCHAR(64) NULL` + `idx_events_tenant_created`）：① 新事件由 `insert_event` 写入 `tenant_id`；② **历史行 `tenant_id` 为 `NULL`**——迁移以 `dg_ctl` 执行，而 `dg_ctl` 对 `task_events` 无 UPDATE 权限，回填做不到；③ 因此审计查询的租户过滤**必须**走 `JOIN tasks ON tasks.task_id = task_events.task_id WHERE tasks.tenant_id = ?`，**禁止**直接 `WHERE task_events.tenant_id = ?`，否则历史事件会凭空消失。该列的价值在索引与未来 RLS，不是当期查询主路径。
- **P0-12** `[D3][A3]` **两个对象必须分开**（消除与 P0-05 的歧义）：① `approvals` 是**可更新状态表**——允许 UPDATE（以便状态迁移），**禁止 DELETE**；② "只可追加"约束适用于**审计历史**，审计历史统一落在 `task_events`，该表对运行账号**无 UPDATE/DELETE 授权**（现状 `scripts/bootstrap_mysql.py` 对 UPDATE/DELETE 逐表授权，`task_events`/`evidence` 不在其列）。审批每次状态变更必须作为**新事件追加**进 `task_events`，历史不得被覆盖。"状态表可更新"不构成对审计只追加原则的例外。
  **DROP 收口（v4）**：`dg_ctl` 的 DDL 授权必须**移除 `DROP`**，只保留 `CREATE, ALTER, INDEX, REFERENCES`——正向迁移只用 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` / 建索引，实测 001–006 均无 `DROP`，故不影响迁移；需 `DROP` 的仅有 `rollback/` 脚本，改为**用 `DG_ADMIN_DSN` 人工执行**并写入 `docs/migrations.md`。据此 A3 的结论是"**运行账号对审计表既不能 UPDATE/DELETE，也不能 DROP**"。以数据库授权为最终保障，并用负向测试断言 UPDATE、DELETE、DROP 三者确实被拒。

- **P0-13** `[D3][A3]` 完整性校验：**复用并扩展** `task_events` 既有哈希链提供校验入口，能返回"通过"或**具体断裂位置**；篡改单条记录后可被检出。不得另建第二套并行链。
- **P0-14** `[D3][A4]` 审计写入失败 → R2 写操作 **fail closed**（拒绝执行，不是"先执行后补审计"），并产生告警。此条为安全硬要求，不得表述为"建议"。
- **P0-15** `[D3]` WORM/Object Lock 选型形成 ADR-0006，只选候选方案并实现本地/对象存储接口；**不得**把未实测方案标为"已具备合规能力"。

**D. Prepared/Observed 与未知结果**

- **P0-16** `[D4]` 写操作前持久化 `StepPrepared`，执行后尽快持久化 `StepObserved`（请求、响应、开始/结束时间、结果状态）。`StepPrepared` 未落盘不得发起执行。
- **P0-17** `[D4][A4]` 结果分类明确：`SUCCESS / PARTIAL / DENIED / TIMEOUT / UNKNOWN_OUTCOME`；`TIMEOUT` 或响应丢失**不得**被解释为失败。
- **P0-18** `[D4][A4]` `UNKNOWN_OUTCOME` 必须转为 `NEEDS_RECONCILIATION`；非幂等操作**禁止自动重试**（现状 `ToolExecutor._should_retry` 已对 `write` 与 `idempotent=False` 返回 False，W06 不得放宽）。
- **P0-19** `[D4]` 核对路径：按 `action_digest`、幂等键、目标对象状态、审计事件与数据库日志核对；证据不足转 `MANUAL_INTERVENTION`。

**E. 门禁与冻结**

- **P0-20** `[A1][A5]` 审批接入唯一执行链：R2 放行条件收紧为"策略允许 + 有效审批消费成功"；现有 `ApprovalDecision` 布尔校验（`core/tools/executor.py` 第 231–243 行）必须被"绑定摘要的审批"取代，不得保留"传 `approved=True` 即通过"的旁路。
- **P0-21** `[A5]` 冻结审批协议、digest 测试向量、审计事件 Schema 与故障演练报告，并保证 W05 的认证/RBAC/Gateway/风险/fencing 测试仍全绿。

**P1（本周期内可做）**：审批列表分页与筛选；`audit:read` 的查询式 REST API 与 CLI 巡检；审批待办通知；审计链定期自检任务。

**P2（明确延期）**：真实写工具及真实 `target_objects` 抽取（W07）；测试桩工具的常态化（**桩工具不得注册进 `adapters/registry.py`，只存在于测试代码**）；真实对象锁/WORM 存储接入与第三方合规评估；多级/会签审批；跨引擎审批模板；独立 Worker 容器拓扑复测；审批侧 UI。

## 4. 审批对象的信息披露要求（安全需求）

审批人批准的不是"一次修复"，而是一个**可被精确复算的操作**。以下字段在批准前必须可见、不得折叠隐藏、不得只显示摘要哈希：

| 字段 | 要求 |
|---|---|
| 具体操作 | `tool_name`、`tool_version`、`normalized_operation` 的可读描述 + 参与摘要的规范形原文 |
| 目标对象 | `target_objects` 逐项列出（库/表/主键或行范围）；不得只给数量 |
| 影响面 | `side_effect`、操作类别、涉及对象数、是否可逆、是否影响其他任务 |
| 风险等级 | `effective_risk` + 风险因子清单 + 模型风险提示与其是否被采纳（复用 `RiskResolution.factors/notes` 既有语义） |
| 前置条件 | `prerequisites_digest` 对应的前置条件全文（数据库身份权限、备份是否存在、负载窗口等） |
| 补偿方案 | 补偿操作描述；必须明确标注**补偿不是回滚**，且补偿本身是独立操作、需独立策略与必要时独立审批 |
| 验证计划 | 执行后如何判定成功（断言清单），使 Approver 能判断"可验证"而非"希望成功" |
| 有效期 | `expires_at` 与剩余时间；过期即不可消费 |
| 绑定信息 | `environment_id` + `environment_fingerprint`、`policy_version`、`action_digest`、`db_engine`、`tenant` |

硬规则：① 任一必需字段缺失时，**阻止批准**（fail closed），不允许以空白通过；② 展示内容与 `action_digest` 必须同源生成，禁止"显示的与摘要覆盖的不是同一份数据"；③ 展示层不得输出凭据、DSN、Token。

**交付载体（已决策）**：W06 无前端交付项，本节以**审批详情 API 的字段完整性**代替页面展示要求——`GET /v1/approvals/{approval_id}` 必须返回上表全部字段；"缺字段阻止批准"的行为在 **API 层实现并测试**；**界面侧验收不作为 W06 门禁**。任务书 D1 原文为"审批页面/接口"，故本界定不构成降级，但也不得因此把本节弱化为纯文档约定。

## 5. 状态与失效规则

- 状态迁移见 P0-05。补充约束：`APPROVED` 不可回退为 `PENDING`；`approvals` 的"当前状态"可被**条件更新**（只改状态列及相关时间戳，禁 DELETE），每次迁移必须追加一条事件到 `task_events`，"谁在何时把哪张单从什么状态改到什么状态"永不覆盖。
- **有效期语义（已决策，主理人 2026-11-09）**：审批 TTL 默认 **1800s**、上限 **7200s**（配置项 `DG_APPROVAL_TTL_S` / `DG_APPROVAL_MAX_TTL_S`），执行信封 **60s**。1800s 的语义是"批准后 30 分钟内**任意时刻**可消费"，**不要求**"批准后 N 秒内开始执行"；60s 信封只约束单次执行请求的时效，两者互不替代、不得合并成一个超时。
- 失效判定必须在**消费时**重新计算：参数、目标对象、环境、环境指纹、策略版本、工具版本、当前时间（过期）、撤销标记、消费标记，九项全过才允许消费；任一不符→拒绝并写明具体不符项。
- 过期与撤销是"审批状态"，消费失败是"执行结果"，两者必须分开记录，不得合并成一个"审批无效"。

## 6. 接口形态建议（路径级，schema 由架构师定）

| 方法 | 路径 | 权限 |
|---|---|---|
| POST | `/v1/approvals` | `approval:create` |
| GET | `/v1/approvals`、`/v1/approvals/{approval_id}` | `approval:read` |
| POST | `/v1/approvals/{approval_id}/approve` | `approval:decide` |
| POST | `/v1/approvals/{approval_id}/reject` | `approval:decide` |
| POST | `/v1/approvals/{approval_id}/revoke` | 申请人本人 + 具备 `approval:decide` 的角色（Approver）；`Administrator` 不可；`CONSUMED` 不可撤销（已决策） |
| GET | `/v1/audit/events`、`POST /v1/audit/integrity/verify` | `audit:read` |

- 消费动作不对普通用户暴露 API，只发生在执行链内部；对外不提供"手动消费"入口。
- 审批相关错误必须使用稳定错误码（新增 `APPROVAL_ALREADY_CONSUMED`、`APPROVAL_EXPIRED`、`APPROVAL_DIGEST_MISMATCH` 等，命名由架构师统一），禁止用 `POLICY_DENIED` 一把梭。

## 7. 不确定项与已决策事项

**原待决三项（现已全部定稿，无剩余待决项）**：

1. ~~拒绝时是否强制填写理由~~ → **已决策（主理人 2026-11-09）：拒绝必填理由，批准可选**。
2. ~~撤销权归属~~ → **已决策（主理人 2026-11-09）：申请人本人 + 具备 `approval:decide` 的角色（Approver）可撤销；`Administrator` 不可撤销；`CONSUMED` 不可撤销**。
3. ~~同一 `step` 是否允许多张 `PENDING`~~ → **已决策（主理人 2026-11-09）：允许多张并存，但执行时必须显式携带 `approval_id` 精确校验，不提供"取任一有效审批"的模糊匹配**。

> 以上三条为团队既有口径的回填（主理人 2026-11-09 落定），保留原建议值作为实现依据；§7 现已无待决项。

> 以下三条是**主理人 2026-11-09 已裁定事项的回填**，与上面"原待决三项"不是同一批，勿混读。只回填状态与配置项明细，未改原决策内容。

| 事项 | 决策 | 落点 |
|---|---|---|
| 审批 TTL（已决策，主理人 2026-11-09） | 默认 **1800s**、上限 **7200s**，配置项 `DG_APPROVAL_TTL_S` / `DG_APPROVAL_MAX_TTL_S`；语义为"批准后 30 分钟内任意时刻可消费"，**不要求**"批准后 N 秒内开始执行"；信封仍 60s 且不替代审批有效期 | §5 |
| 控制库账号是否拆分（已决策，主理人 2026-11-09） | **本轮不拆分**。理由：拆账号需动 `scripts/migrate.py`、`bootstrap_mysql.py` 及全部部署约定，影响面超出 W06。**但按 v4 收口，`dg_ctl` 的 DDL 授权移除 `DROP`**（保留 `CREATE, ALTER, INDEX, REFERENCES`），回滚改由 `DG_ADMIN_DSN` 人工执行，"运行账号可删审计表"的口子关闭，A3 判定达成。补偿要求：**不可变新表只授 SELECT/INSERT**（`approval_consumptions`、审计历史表），`approvals` 作为状态表**另授 UPDATE、不授 DELETE**（否则状态迁移无法实现，见 P0-12）；UPDATE / DELETE / DROP 三者配负测断言被拒 | P0-12、§9、ADR-0006 |
| WORM 口径（已决策，主理人 2026-11-09） | ADR-0006 只写"**候选方案 + 本地实现 + 预留接口**"，并显式写明"**未实测、不具备任何合规认证**" | P0-15、§9 |

**已决策事项（可直接实现，若主理人有异议请驳回）**：

| 事项 | 决策 | 落点 |
|---|---|---|
| 规范化是否完整实现 RFC 8785 JCS | 否。受约束子集，值域外**报错而非降级**；不松绑依赖冻结；不得宣称完整实现 | P0-02 |
| "只能消费一次"由谁保证 | `approval_consumptions` 的 `approval_id` + `nonce_hash` 两个 UNIQUE 约束 | P0-07 |
| 审批状态表与审计历史的关系 | 两对象：`approvals` 可 UPDATE、禁 DELETE；审计历史落 `task_events`，无 UPDATE/DELETE | P0-12、§5 |
| `prerequisites_digest` 由谁生成 | **只由服务端生成**：控制面事实（环境为 LAB、引擎一致、工具已注册且版本存在、`target_objects` 格式合法、`policy_version`）+ 只读目标库对象存在性探测（`TargetProbePort`，仅固定检查、**禁止任意 SQL**）。人工备注不参与摘要 | P0-02、P0-03 |
| `image_digest` 当前为 `NULL` | 允许参与摘要并标记 `fingerprint_completeness=PARTIAL`，不阻断 LAB 审批；不得宣称"环境指纹完整" | P0-03、§9 |
| W06 是否交付真实写工具 | 否。`target_objects` 用**测试桩工具**验证（桩工具不得进 `adapters/registry.py`），真实写工具抽取属 W07 | P0-03b、P2 |
| T9 崩溃恢复的验证强度 | 定档为"子进程强制终止 + 重启对账"自动化；须稳定复现 10 次并给出失败率；不降级为人工记录或 in-process 模拟 | T9 |
| 第 4 节信息披露的交付载体 | 无前端；以审批详情 API 字段完整性替代，缺字段阻止批准在 API 层实现并测试，界面侧不作门禁 | §4 |

## 8. 验收标准映射表

| # | 任务书标准 | 可测断言（Given/When/Then） | 验证者 |
|---|---|---|---|
| T1 | 审批绑定 `action_digest`、环境指纹与策略版本 `[A1]` | Given 一张已批准审批，When 读取审批详情，Then 详情含 `action_digest`、`environment_fingerprint`、`policy_version` 且与会话中重新计算的一致 | 自动化 |
| T2 | 任何关键字段变化使审批失效 `[A1]` | Given 已批准审批，When 逐个改动参数/目标对象/环境/环境指纹/策略版本/工具版本，Then 每次消费均被拒绝并给出对应不符通道（`channel=parameters`/`target_objects`/`environment`/`environment_fingerprint`/`policy_version`/`tool_version`，六个子用例全覆盖）。目标对象子用例须**分别**证明：改本次参数致抽取对象变化 → `channel=target_objects` 可比对；改抽取规则 → 经 `tool_version` 通道失效——只测其一一律不算通过。因现网工具均未声明 spec，该子用例由**测试桩工具**（仅存在于测试代码、不进入生产注册表）提供证据 | 自动化 |
| T3 | 并发消费只有一个成功 `[A2]` | Given 一张 `APPROVED` 审批，When N 个并发消费，Then 恰好 1 个成功、其余 N-1 个返回 `APPROVAL_ALREADY_CONSUMED` | 自动化 |
| T4 | 重放、过期、撤销、参数篡改均被拒绝 `[A2]` | Given 四类构造，Then 全部拒绝且不产生工具调用记录；已消费审批再次使用必失败 | 自动化 |
| T5 | 审计只追加、普通组件不能覆盖/删除 `[A3]` | Given 应用运行账号 `dg_ctl`，When 对 `task_events` 执行 UPDATE、对 `task_events` 与 `approvals` 执行 DELETE、对 `task_events` 执行 **`DROP TABLE`**，Then 四者均在数据库层报权限错误且不产生任何行变更（`DROP` 失败后表仍存在），负测断言拒绝真实发生；同时断言对 `approvals` 的 UPDATE 仍可用（状态迁移通路未被误伤） | 自动化 + 人工核对授权语句 |
| T6 | 完整性检查可发现篡改 `[A3]` | Given 直接改一条审计记录（用高权账号绕过），When 执行完整性校验，Then 返回失败并定位到断裂的位置 | 自动化 |
| T7 | 审计失败时 R2 fail closed `[A4]` | Given 审计存储不可用，When 请求执行 R2 操作，Then 工具未被调用、返回拒绝、产生告警事件 | 自动化 |
| T8 | 结果未知进入 `NEEDS_RECONCILIATION` `[A4]` | Given 数据库已执行但响应丢失，When 执行链收敛，Then 任务状态为 `NEEDS_RECONCILIATION` 且不存在任何自动重试 | 自动化 |
| T9 | Worker 在 `StepPrepared` 后崩溃可恢复 | Given 独立子进程在落盘 `StepPrepared` 后被强制终止（`Popen.kill()`，Windows 下为 TerminateProcess，不依赖 Docker），When 重启对账流程，Then 依据核对证据得出确定结论或转 `MANUAL_INTERVENTION`，且**不发生重复执行**。**须稳定复现 10 次**并将失败率与失败形态写入测试报告；若 CI 不稳定，报告必须给出数据，**不得悄悄改为 in-process 模拟** | 自动化（子进程强制终止 + 重启 + 10 次稳定性，**已定档**） |
| T10 | W6→W7 门禁 `[A5]` | Given 全量测试套件，When 运行，Then 认证/RBAC/Gateway/风险/审批指纹/审计/fencing 全绿，无降级或跳过 | 自动化 + 人工确认报告 |
| T11 | 不宣称 Exactly Once | Given 全部 W06 文档与 API 响应，When 检索 "Exactly Once"/"精确一次"，Then 无任何肯定性宣称 | 人工 |

## 9. 明确不做（防范围蔓延）与已知残余风险

- 不开放 PROD 环境任何写操作（`PROD_TARGET_NOT_ALLOWED` 保持）。
- 不开放 R3/R4。
- 不给 `Administrator` 增加 `approval:decide`。
- 不实现跨库分布式事务、不实现"审批后自动回滚"；不交付真实写工具（W07）。
- 不在本机 Docker 不可用的前提下声称容器级 WORM 已验证；ADR-0006 只能写"候选方案"。

**已知残余风险（必须随交付一并登记，不得表述为"已完全达成"）**：

1. **管理员账号具备 DDL，属人工运维路径**（主理人 2026-11-09 裁定：不拆分账号；`dg_ctl` 的 `DROP` 已移除，A3 判定**达成**）。运行账号 `dg_ctl` 对审计表既不能 UPDATE/DELETE，也不能 `DROP`；仅 `DG_ADMIN_DSN` 可用于执行 `rollback/` 脚本与初始化，且不进运行时。残余风险限定为"人工运维账号能删表"，属正常职责分离，**不得**表述为"运行账号可删除审计记录"。
   附带要求：`docs/migrations.md` 必须写明回滚一律以 `DG_ADMIN_DSN` 人工执行；`bootstrap_mysql.py` 的 DDL 授权行改为 `GRANT CREATE, ALTER, INDEX, REFERENCES`。

2. **`image_digest` 为 `NULL`**，环境指纹为 `PARTIAL`（`fingerprint_completeness=PARTIAL`）；LAB 允许，但不得宣称"环境指纹完整"，且该字段后续补全会使既有指纹变化、旧审批失效——这是正确行为，不是缺陷。
