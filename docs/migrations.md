# 控制库迁移说明（W03/W05/W06/W07/W08/W09）

## 版本

| 版本 | 内容 |
| --- | --- |
| `001_harness_core` | tasks/task_steps/tool_calls/task_events/checkpoints/schema_migrations |
| `002_v22_evidence` | tenants/users/memberships/environments、tasks 并发/预算字段、事件 seq/hash、evidence 表 |
| `003_evidence_redaction_hash` | evidence.redacted_payload_hash |
| `004_tenant_composite_fk` | 跨表租户归属复合唯一键与外键 |
| `005_w05_identity_policy` | users 认证字段、tasks.created_by、auth_sessions、worker_leases、execution_envelopes |
| `006_w06_approval_audit` | task_events 增 tenant_id/actor_user_id、approvals、approval_consumptions、step_prepared、step_observed |
| `007_w07_disclosure` | approvals 增披露快照四列（disclosure_json/disclosure_digest/disclosure_version/disclosed_at）与索引 idx_approvals_disclosure_digest |
| `008_w08_verification` | `verification_results`（独立验证结果，只追加）+ `approvals.compensation_of` 关联列与唯一键 `uq_approvals_compensation_of` |
| `009_w09_redaction` | `evidence.source_payload_hash`（脱敏原文锚点）+ `redaction_quarantine`（隔离区，只追加，不含原文列） |
| `010_w09_report` | `report_snapshots`（结构化报告快照，只追加） |
| `011_w09_memory` | `knowledge_entries`（知识条目，只追加；撤销/过期/冲突以追加 revision+1 表达） |
| `012_p1_byok_model_providers` | `tenant_model_providers`（P1-BYOK 租户模型供应商；api_key 以 Fernet 密文存储，仅回显掩码） |
| `013_p1_byok_provider_versions` | 供应商配置改**只追加版本**：增 `root_id`/`revision`/`status` 与索引；控制库账号 `dg_ctl` 只有 SELECT/INSERT，故改/删/激活均写新 revision |
| `014_p3_knowledge_confirmations` | 知识条目增 `confirmations_json`（两级确认：stage/确认人/时间/内容摘要）与 `content_digest`；只加列不改历史行 |
| `015_p5_eval_platform` | 评测中台四表：`eval_suites` / `eval_runs` / `eval_case_results` / `eval_comparisons`（只追加；metrics 只存实测值，缺测为 NULL） |
| `016_p4_oidc_subject_mapping` | `users` 增 `oidc_issuer`/`oidc_subject` 与唯一键：仅做身份映射，角色仍只来自 `memberships` |
| `017_p6_readonly_tool_dispatch` | `readonly_tool_requests` / `readonly_tool_responses`（只追加，API 经控制库派发只读工具给 Worker 执行，解除 API 对 `lab_net` 依赖） |

正向文件共 **17 支（001–017）**，回滚脚本共 **10 支**（`rollback/002_004_rollback.sql` 覆盖 002–004，
另有 `005`–`011`、`017` 等各一支；`014` 只增列，回滚可人工 `DROP COLUMN`），逐支清单见下文「回滚」。

`005_w05_identity_policy` 的具体变更：

- `dg_system.users`：新增 `password_hash`、`password_updated_at`、`auth_version`（默认 1）、`updated_at`。
- `dg_system.tasks`：新增 `created_by`（默认 `user_local`）与 `(tenant_id, created_by)` 索引，用于所有者校验。
- `dg_system.auth_sessions`：会话 Token 只存 SHA-256 摘要（`token_hash` 唯一），含租户、状态、有效期、撤销与轮换来源。
- `dg_system.worker_leases`：任务级租约；生成列 `active_task_id` + 唯一键保证每任务最多一个 `ACTIVE` 租约，`fencing_token` 单调递增。
- `dg_system.execution_envelopes`：执行信封只追加；`nonce_hash` 唯一，作为重放防护。

## 应用

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_mysql.py   # 初始化 + 迁移（幂等）
.\.venv\Scripts\python.exe scripts\migrate.py            # 仅迁移
```

迁移由 `dg_ctl` 执行；该账号有 `SELECT/INSERT` 全库权限与 `CREATE/ALTER/INDEX/REFERENCES`，
但 `task_events`/`evidence` **没有 UPDATE/DELETE**（数据库级只追加）。

### 账号与 DDL 边界（W06 起）

- 正向迁移**只使用 `CREATE` / `ALTER` / `INDEX`**（`dg_ctl` 已具备），因此 `scripts/migrate.py` 仍以 `dg_ctl` 运行。
- `dg_ctl` 的 DDL 授权已移除 `DROP`：**任何需要 `DROP`/`RENAME` 的迁移，以及全部回滚脚本，必须用 `DG_ADMIN_DSN`（管理员）执行**，属人工运维路径，不纳入自动迁移。这样"应用运行账号无法删除审计/证据表"不再只靠自律。

W05 的权限边界：

- `dg_system.auth_sessions`、`dg_system.worker_leases` 仅额外授予 **UPDATE**，没有 DELETE；会话撤销/轮换与租约心跳只做条件更新。
- `dg_system.execution_envelopes` 只有基础 `SELECT/INSERT`，**只追加，无 UPDATE/DELETE**；`nonce_hash` 唯一键由数据库保证。

W06 的权限边界（`006_w06_approval_audit`）：

- `dg_system.approvals` 额外授予 **UPDATE**（条件更新状态列），**没有 DELETE**；审批历史由 `task_events` 承担。
- `dg_system.approval_consumptions`、`dg_system.step_prepared`、`dg_system.step_observed` **只有基础 `SELECT/INSERT`**，不出现在 `CONTROL_WRITE_GRANTS` 中：单次消费由 `uq_consumption_approval`/`uq_consumption_nonce` 双唯一键保证，执行证据写一次不改写。
- 负向测试：`tests/integration/test_audit_service.py`、`tests/integration/test_step_prepared.py` 断言 `dg_ctl` 对上述只追加表执行 `UPDATE`/`DELETE` 必然 `DatabasePermissionError`（MySQL 1142）。
- 残余风险（已登记 W11）：`dg_ctl` 仍是执行迁移的同一账号，历史上曾持 DDL；W06 已移除其 `DROP`，但表结构变更能力与运行时账号未彻底拆分。

W07 的权限边界（`007_w07_disclosure`）：

- 披露快照四列（`disclosure_json` / `disclosure_digest` / `disclosure_version` / `disclosed_at`）**没有**新增任何授权：与 `approvals` 其余列共用既有的 `UPDATE` 授权。
- 因此"三节写入后不被改写"**不靠数据库授权**，而是三层应用层约束（见 `docs/w07/02-design.md` §3.3）：
  1. `ApprovalRepository._MUTABLE_COLUMNS` 不含这四个列（`transition()` 收到即 `REQUEST_INVALID`）；
  2. `_to_approval()` 读出后用 `canonical_digest(disclosure_json)` 与 `disclosure_digest` 列比对（不一致即 `STATE_CONFLICT`/`DISCLOSURE_TAMPERED`）；
  3. `consume()` 在行锁内二次复核同一比对（TOCTOU 保护）。
- 残留：同时改写 `disclosure_json` 与 `disclosure_digest` 的攻击代码层检测不到，与"同时改写 `action_descriptor_json` 与 `action_digest`"同构，属 W06 已接受同类风险，由 W11 的 WORM/账号拆分承接。
- 写入不变量：`ApprovalRepository.insert()` 断言 `disclosure_digest` 非空，因此**新行不可能为 NULL**；历史行允许 NULL，在 `approve()` 被 `DISCLOSURE_INCOMPLETE` 阻断（fail closed），在 `GET` 显式标 `provided=false` / `reason=LEGACY_ROW_WITHOUT_DISCLOSURE`。

W08–W09 的权限边界（`008_w08_verification` / `009_w09_redaction` / `010_w09_report` / `011_w09_memory`）：

- 四支正向迁移**只含 `CREATE TABLE` / `ALTER TABLE ... ADD COLUMN` / `ADD UNIQUE KEY`**，不含 `DELETE` / `DROP`，
  因此仍由 `scripts/migrate.py`（`dg_ctl`）自动执行；对应回滚脚本含 `DROP`，只能由 `DG_ADMIN_DSN` 人工执行。
- 新增的四张表 `verification_results` / `redaction_quarantine` / `report_snapshots` / `knowledge_entries`
  **只有基础授权**（`GRANT SELECT, INSERT ON dg_system.*` 覆盖），不在 `CONTROL_WRITE_GRANTS` 中：
  `dg_ctl` 对它们既无 `UPDATE` 也无 `DELETE`，只追加语义由数据库授权兜底。
- `redaction_quarantine` **没有任何原文列**：只存 `payload_hash`、`detected_categories`、定位信息
  （`unresolved_spans_json`）与 `blockers`，原文不入库（只追加且无 `DELETE`，一旦入库不可撤回）。
- `knowledge_entries` 以 `(knowledge_id, revision)` 为主键，撤销/过期/冲突一律追加 `revision+1`，原行不改；
  `report_snapshots` 以 `report_id` 为主键，报告可由证据重建（`report_version` 会变），但已交付的对外编号
  在回滚后失效，须在发布说明登记（`docs/w09/02-design.md` §8.5）。
- `approvals.compensation_of` 只新增列与唯一键，未新增授权：补偿审批与常规审批共用既有 `UPDATE` 权限。

## 已有数据库兼容

- 002 对历史 `task_events` 的 SQL 回填哈希无法复现运行时“规范化 JSON + UTC ISO”算法；
  对 002 之前已存在事件的库，执行一次：

```powershell
.\.venv\Scripts\python.exe scripts\repair_event_chain.py
```

该脚本使用 `DG_ADMIN_DSN`（管理员），重算 seq 链的 `previous_event_hash`/`event_hash`。
新库（无历史事件）无需执行。

- 002 将旧任务统一归入 `tenant_local` / `env_mysql_local`；旧任务如需别的归属，必须在迁移前
  人工映射，迁移脚本不做业务归属推断。

## 回滚

MySQL DDL 不回滚数据。回滚脚本：

- `database/migrations/rollback/002_004_rollback.sql`：回滚 002–004。
- `database/migrations/rollback/005_rollback.sql`：回滚 005，删除 `execution_envelopes`、`worker_leases`、`auth_sessions`，移除 `tasks.created_by` 及 users 的认证字段，并删除 `schema_migrations` 中的 `005_w05_identity_policy` 记录。
- `database/migrations/rollback/006_rollback.sql`：回滚 006，按逆 FK 顺序删除 `step_observed`/`step_prepared`/`approval_consumptions`/`approvals`，移除 `task_events` 的 `tenant_id`/`actor_user_id` 与两个索引，并删除版本记录。
- `database/migrations/rollback/007_rollback.sql`：回滚 007，删除 `approvals` 的 `idx_approvals_disclosure_digest` 与披露快照四列，并删除版本记录。**只写不自动执行**，须用 `DG_ADMIN_DSN` 人工执行。
- `database/migrations/rollback/008_rollback.sql`：回滚 008，删除 `approvals.compensation_of` 与 `uq_approvals_compensation_of`，再删除 `verification_results`，并删除版本记录。
- `database/migrations/rollback/009_rollback.sql`：回滚 009，删除 `redaction_quarantine` 与 `evidence.source_payload_hash`，并删除版本记录。
- `database/migrations/rollback/010_rollback.sql`：回滚 010，删除 `report_snapshots`，并删除版本记录。
- `database/migrations/rollback/011_rollback.sql`：回滚 011，删除 `knowledge_entries`，并删除版本记录。回滚前须先导出全量 revision（知识含人工审核结论）。

008–011 的回滚脚本与 005–007 同规格：含 `DROP`，**只写不自动执行**，须用 `DG_ADMIN_DSN` 人工执行。
回滚顺序为逆序：011 → 010 → 009 → 008 → 007 → 006 → 005 → 002–004。

回滚脚本含 `DROP` 且最后一步 `DELETE FROM schema_migrations` 需要 DELETE 权限
（`dg_ctl` 对 `schema_migrations` 只有 `SELECT/INSERT`），因此**必须用 `DG_ADMIN_DSN` 执行**。
回滚脚本非幂等：重复执行 `DROP KEY` 会报 1091，回滚后请重新执行 `scripts/migrate.py`。

回滚仅用于可丢弃的本地 LAB；执行前必须停写、备份认证会话与租约/审计数据，并由人工确认。生产/共享环境不做自动回滚。回滚顺序需先 011→008、再 007→005，最后 002–004。

## 已知限制

- 迁移文件不是逐语句幂等的；`schema_migrations` 防止重复执行，但半途失败后需要人工修复。
- 复合外键保证 task/evidence 的 tenant+environment 归属一致；应用层仍保留显式校验作为第二道防线。
