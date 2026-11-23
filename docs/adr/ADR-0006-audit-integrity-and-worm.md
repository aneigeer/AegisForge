# ADR-0006 审计完整性与 WORM 存储选型

- 状态：Proposed（本地追加式实现已定；WORM 部分仅为**候选方案**，未实测）
- 日期：2026-11-09
- 相关：V2.2 W06 D3｜`docs/w06/01-prd.md` P0-11…P0-15、T5/T6/T7/T11｜`docs/w06/00-task-book.md` D3
- 实现位置：`core/audit/{events,integrity,service,worm}.py`、`database/migrations/006_w06_approval_audit.sql`、`scripts/bootstrap_mysql.py`

## 背景

W06 要求审批、策略决策、执行与未知结果全部落入"只可追加、可校验完整性"的账本，且"普通业务组件不能覆盖/删除"。同时总纲要求对 Alpha 阶段 WORM/Object Lock 给出选型，但**不得把未实测方案标为已具备合规能力**。

本机 Docker daemon 不可用，无法部署对象存储或容器级不可变存储；无第三方合规评估预算。

## 决策

### 1. 单一事件链，不建第二套真相

审计记录落在**既有 `dg_system.task_events`**（已有 `(task_id, seq)` 唯一键、`previous_event_hash` + `event_hash` 哈希链、`trace_id`，且 `dg_ctl` 无 UPDATE/DELETE 授权）。

本轮只增两列用于查询：`tenant_id`、`actor_user_id`；审计专有字段（`action_digest`、`policy_version`、`decision`、`result`、`integrity_ref`、`actor`、`trace_id`）全部写入 `payload_json`，因此 `event_hash()` 计算式**不需要修改**，W03/W05 的哈希链断言全部保持有效。

**不新建 `audit_events` 表**。理由见 `02-design.md` §1.3：审批必须有 `task_id`，若按全局 seq 另建一条链，同一审批事件会同时出现在两条链上，形成互相矛盾的事件真相。这条是硬约束。

### 2. 追加式在数据库层保证（不靠应用自律）

| 表 | `dg_ctl` 授权 | 说明 |
|---|---|---|
| `dg_system.task_events` | `SELECT, INSERT`（现状，无 UPDATE/DELETE） | 事件账本 |
| `dg_system.evidence` | `SELECT, INSERT`（现状） | 证据 |
| `dg_system.approval_consumptions` | `SELECT, INSERT` | 消费台账（本轮新增） |
| `dg_system.step_prepared` | `SELECT, INSERT` | 执行前证据（本轮新增） |
| `dg_system.step_observed` | `SELECT, INSERT` | 执行后证据（本轮新增） |
| `dg_system.approvals` | `SELECT, INSERT, **UPDATE**`（**无 DELETE**） | 审批是状态表，历史由 `task_events` 承担 |

`scripts/bootstrap_mysql.py` 的 `CONTROL_WRITE_GRANTS` 只为 `approvals` 追加 `UPDATE`；其余新表**不出现**在授权列表里。

负向测试（P0-12/T5）必须真实执行并断言数据库拒绝：
`UPDATE`/`DELETE` 四张只追加表 → `DatabasePermissionError`（MySQL 1142），沿用 `tests/integration/test_w03_integrity.py` 的写法。测试不得 mock，必须是 `dg_ctl` 真实连接。

### 3. 完整性校验：哈希链 + 可定位断裂

`core/audit/integrity.py::verify_event_chain(events)`：
1. 按 `seq` 升序逐条重算 `event_hash`（用与 `core/tasks/repository.py::event_hash` **完全相同**的输入集合与序列化方式）；
2. 校验 `previous_event_hash` 等于上一条的 `event_hash`（`seq=0` 必须为 `None`）；
3. 校验 `seq` 连续（`0,1,2,…` 无空洞）。

返回 `ChainVerification(ok, checked, broken_at_seq, affected_range, reason)`：
- 通过 → `ok=True`；
- 篡改 payload → 该条重算不符，`broken_at_seq` 指向它；
- 删除中间一条 → `seq` 空洞 + 后继 `previous_event_hash` 不符，`affected_range` 给出 `(断裂前最后有效 seq, 链尾 seq)`。

**不允许只返回"失败"两字**（P0-13/T6）。API `POST /v1/audit/integrity/verify` 与 CLI `scripts/audit_verify.py` 输出同一结构。

### 4. 审计写入失败 → R2 fail closed

落点（单点，不做多处重复判断）：
1. `AuditService.emit_required(event)` — 包装 `insert_event`，任何异常转 `AuditUnavailableError`（`ErrorCode.AUDIT_UNAVAILABLE`，HTTP 503）；
2. `StepRecordRepository.insert_prepared(record)` — 写操作前的**强制**持久化点。

失败行为：
- R2 请求：工具**未被调用**（`StepPrepared` 未落盘即不发起执行），返回 `ToolResult(DENIED, code=AUDIT_UNAVAILABLE)`；
- 告警：`logger.error("审计写入失败，R2 已阻断", extra={"alert": "AUDIT_WRITE_FAILED", ...})` + best-effort 写 `AuditEventType.AUDIT_WRITE_FAILED` 事件（若账本本身不可写，只剩结构化日志——这是已知降级，见限制 5）。
- **不引入新告警基础设施**（无 Prometheus/告警通道）；沿用 `core/logging` 与现有事件表。

### 5. WORM / Object Lock：候选方案（未实测）

**明确口径（对外表述必须照抄）**：本节只列出候选方案与预留接口，**本轮未做任何实测，不具备任何 WORM 合规认证（无 SEC 17a-4、无 ISO 27001 相关认证、无第三方评估）**。当前实现是"**本地追加式 + 数据库授权边界内的不可改写**"，不是 WORM，不得表述为"已具备 WORM 能力"。

候选方案（按推荐顺序，均**未实测**）：

| # | 方案 | 机制 | 优点 | 已知代价/风险 | 状态 |
|---|---|---|---|---|---|
| A | AWS S3 Object Lock（COMPLIANCE 模式，法定保留期） | 对象级不可删改 + 保留期由服务端强制 | 业界最成熟，证据链清晰 | 需云依赖与合规评估；保留期内连 root 也不能删，误配不可逆 | 未实测 |
| B | MinIO / Ceph RGW 的 Object Lock（自建 S3 兼容） | 同上，自托管 | 无云锁定，可本地部署 | 需容器/对象存储；本机 Docker 不可用，无法验证 | 未实测 |
| C | 只追加文件系统 + 定期外部时间戳锚定 | append-only 目录 + 周期性哈希根写入外部不可变介质（如 WORM 光盘、第三方时间戳服务） | 依赖最少，可先落地 | 只保证"篡改可发现"，不保证"不可改写"；锚定本身需人工流程 | 未实测 |
| D | 第三方审计/SIEM 托管（不可变索引） | 外部系统接收并固化事件 | 与 A/B 互补，查询能力强 | 数据出境与合规问题；引入外部依赖 | 未实测 |
| E | 区块链/公链锚定 | 哈希上链 | 第三方可验证 | 性能、合规、运维成本高，与本阶段规模不匹配 | 已否决 |

**本轮实现（`core/audit/worm.py`）**：
```
AuditArchivePort (Protocol)
  - append(records: Sequence[dict]) -> ArchiveRef
  - verify(ref: ArchiveRef) -> bool
LocalArchiveSink        # 本地追加文件 + 每批 sha256 链，可在无 Docker 环境验证
ObjectStorageArchiveSink  # 预留实现；未配置时 __init__ 抛 NotImplementedError，
                          # 且不得被默认装配（避免"看起来支持"的假象）
```
接口预留的目的：W07 之后接入 A/B 时**不需要改审计业务代码**。当前默认装配只有 `LocalArchiveSink`。

### 6. 控制库账号不拆分（本轮）

**原始设想**是 `dg_ctl` 继续持有 `dg_system.*` 的 `CREATE, DROP, ALTER, INDEX, REFERENCES`，即"即使不授予新表的 `UPDATE/DELETE`，同一账号仍可 `DROP TABLE`"。**实现已收窄**：`DROP` 已从 `dg_ctl` 移除（见下"已知限制 2"），`ALTER TABLE … DROP COLUMN` 仍可能，残余口子从"整表删除"降级为"列级结构变更"。

主理人决策：本轮不拆分账号，**登记为已知残余风险，作为 W11 安全回归待办**。缓解措施（本轮已做）：
- 新表对 `dg_ctl` 只授 `SELECT/INSERT`，并有负测证明 UPDATE/DELETE 被拒；
- `ddl` 使用与运行时同账号这一点在 `05-freeze.md` 与测试报告中显式写明，**不得在对外材料中宣称"应用账号在权限层面完全无法改写历史"**；
- 正确的表述是"**应用运行路径**无法 `UPDATE/DELETE` 审计记录；同一账号已无 `DROP`，但仍可用 `ALTER` 破坏表结构，该风险已登记待 W11 拆分只写账号"。对外**不得**表述为"账号层面已完全无法改写历史"。

## 备选方案与取舍

- **新建 `audit_events` 表（全局链）**：查询模型更"纯"，但会产生两套事件真相，且需要迁移 `task_events` 全部历史或在两表间做同步。否决。
- **只靠应用层禁止 UPDATE/DELETE**：无法抵御误写、SQL 注入与运维脚本。否决；必须数据库授权兜底。
- **用 MySQL 触发器阻止 UPDATE/DELETE**：`dg_ctl` 有 DDL 权限可随时删触发器，且触发器本身需要高权创建。不作为主防线；不采用。
- **审计同步双写（DB + 对象存储）**：写放大与一致性复杂，且对象存储当前不可用。改为"预留 `AuditArchivePort`，默认只写 DB"，归档在 W07+ 接入。
- **审计写入失败时降级为"先执行后补审计"**：直接违反 P0-14。否决。
- **引入 Prometheus/Alertmanager 做告警**：违反"不引入新基础设施"。改用结构化日志 + 事件表。

## 后果

- 审计查询与完整性校验都按 `task_id` 分链；跨任务的"全局顺序"不存在（也没有全局 seq）。跨任务审计视图只能按 `created_at` 排序，**这不是一条可链式校验的链**——必须在 API 文档中写明，避免误认为全局链。
- `task_events` 增列后，`insert_event` 的 `MAX(seq)` 查询会走 `idx_events_task_seq`，`tenant_id`/`actor_user_id` 只用于过滤，无热点写冲突（现有 3 次重试足够）。
- `approvals` 允许 UPDATE 意味着审批状态可被 `dg_ctl` 改写——但每次状态迁移都必须同时追加 `task_events`，因此**篡改状态会被事件链比对暴露**（`05-freeze.md` 要求提供该比对用例）。
- `ObjectStorageArchiveSink` 是空壳。任何"已接入对象存储"的说法都不成立。

## 验证方式

- `tests/integration/test_audit_service.py`：篡改单条事件（用 `admin_client` 绕过授权）→ `verify` 返回 `ok=False` 且 `broken_at_seq` 精确；删除中间一条 → `affected_range` 正确。
- `tests/integration/test_w03_integrity.py`（扩写）：`dg_ctl` 对 `approval_consumptions`/`step_prepared`/`step_observed`/`task_events` 执行 UPDATE/DELETE → 全部 `DatabasePermissionError`。
- `tests/integration/test_approval_consume.py`：注入 `DatabaseQueryError` 使审计不可写 → R2 工具未被调用、返回 `AUDIT_UNAVAILABLE`、`AUDIT_WRITE_FAILED` 日志出现。
- `scripts/audit_verify.py`：对真实库跑一遍，输出 `ok=True` 与 `checked` 计数，作为 T6 的人工核对证据。
- 授权语句人工核对：`SHOW GRANTS FOR 'dg_ctl'@'%'` 输出附在测试报告中（T5 的"人工核对授权语句"部分）。

## 已知限制与未完成项

1. **不具备 WORM 能力**：无对象锁、无保留期强制、无第三方合规认证。当前只是"数据库授权边界内的追加式 + 哈希链可校验"。
2. **`dg_ctl` 仍持部分 DDL（W06 已收窄）**：`scripts/bootstrap_mysql.py` 已把 `dg_ctl` 的 DDL 收窄为 `CREATE, ALTER, INDEX, REFERENCES`，**移除了 `DROP`**；因此"应用运行账号直接删除审计/证据表"这条路径已关闭，回滚与任何 `DROP`/`RENAME` 必须走 `DG_ADMIN_DSN` 人工运维路径（见 `docs/migrations.md`）。**残余风险**：同一账号仍可 `ALTER TABLE ... DROP COLUMN`、`RENAME`（若后续授予）与修改表结构，故"结构级不可改写"仍未达成；账号拆分（只写账号 + 只读账号）仍登记为 W11 待办，本轮不做。
3. **无全局事件链**：链的边界是 `task_id`；跨任务无法做单链校验。
4. **`ObjectStorageArchiveSink` 未实现**：仅有协议与占位，默认不装配。
5. **审计不可写时的告警只剩结构化日志**：若账本本身不可写，`AUDIT_WRITE_FAILED` 事件也写不进去，告警退化为 `logger.error`。这是无外部告警通道的必然降级，已在此登记。
6. **完整性校验是"事后可发现"而非"事前防篡改"**：哈希链本身不阻止写入者删除链尾（只删除尾部而不改中间，校验仍会通过，因为链是自洽的）。缓解：`LocalArchiveSink` 定期把链根写入独立位置；该锚定机制本轮未实测，仅接口预留。
7. **`task_events` 无分区与归档策略**：长期增长未处理，属 W07+。
8. **"审批已消费但消费事件缺失"的可能窗口**：`ApprovalService.consume()` 在控制库事务 `COMMIT` **之后**才写 `APPROVAL_CONSUMED` 审计事件（`core/approval/service.py`）。若该写入失败，审批状态已是 `CONSUMED`、`approval_consumptions` 已有台账行，但事件链里缺这条事件，调用方收到 `AuditUnavailableError`、工具未执行。**这不是 fail-closed 违规**（工具确实未被调用），但会造成"状态与事件不一致"。**本轮不改代码**：强行把事件写入塞进同一事务会把 `insert_event` 的 3 次唯一键重试逻辑拖进事务边界，重试期间的 `COMMIT`/`ROLLBACK` 语义与行锁持有时间都会失控，风险与收益不成正比。**处置方式**：以 `approval_consumptions` 行为权威（`consumed_at`/`consumed_by`/`action_digest` 均在台账里），按 `docs/w06/05-freeze.md` §B.1 的人工核对流程登记一条追加式说明；**禁止** `UPDATE approvals` 回退状态，**禁止**伪造补写事件（会破坏哈希链）。W07 重新评估是否改为"先写事件再提交"的两阶段写法。
