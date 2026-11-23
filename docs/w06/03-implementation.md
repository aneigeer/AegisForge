# W06 实现说明与偏差清单

- 基线 `d2ceae8`（W05 全量 660）→ 本文对应提交：`089f926`（T01–T03）、`a22662a`（T04）、T05 提交（见 git log）
- 需求依据：`docs/w06/01-prd.md`、`docs/w06/02-design.md`；冻结件：`docs/w06/05-freeze.md`
- 本环境限制：PostgreSQL LAB（`127.0.0.1:55432`）未运行且 Docker daemon 不可用，因此全量测试有 **21 项 PG 用例失败 + 4 项 skip**，均为环境原因；MySQL 侧全绿。

## 1. 交付结构

| 层 | 模块 | 职责 |
|---|---|---|
| 规范化 | `core/security/canonical.py` | JCS 受约束子集 + SHA-256，**所有摘要的唯一入口** |
| 审批领域 | `core/approval/{models,digest,prerequisites,repository,service}.py` | 状态白名单、`ApprovalGrant`（seal）、摘要构建、前置条件探测、仓储、创建/裁决/撤销/过期/消费 |
| 审计 | `core/audit/{integrity,service,worm}.py`、`core/audit/events.py` | 链校验、查询与 fail closed 追加、归档端口、事件类型 |
| 执行证据 | `core/execution/{step_records,reconciler}.py` | Prepared/Observed 只 INSERT 仓储、未观测调用的收敛 |
| 数据 | `database/migrations/006_w06_approval_audit.sql`(+rollback)、`database/mysql/transaction.py` | 四张表 + 事件过滤列、显式事务 |
| 执行链 | `core/tools/executor.py`、`core/gateway/tool_gateway.py`、`core/tasks/service.py`、`core/worker/worker.py` | grant 唯一入口、8b/8c 步、UNKNOWN_OUTCOME 收敛 |
| API / CLI | `apps/api/routes/{approvals,audit}.py`、`apps/api/{deps,main}.py`、`scripts/audit_verify.py` | 6+2 端点、装配、链巡检 |

## 2. 关键实现选择（非偏差，用于复核）

1. **审计真源唯一**：新增表只有"证据"（consumption/prepared/observed），事件仍只进 `task_events`；
   `event_hash()` 九字段输入未改，W03/W05 的链断言全部保持。
2. **摘要唯一入口**：`action/prerequisites/分通道` 摘要全部经 `canonical_digest`；
   `core/gateway/envelope.py::compute_args_digest`（`sort_keys` + `default=str`）**不复用**。
3. **一次性凭证**：`ApprovalGrant` 为 frozen dataclass，`grant_seal` 是模块私有哨兵，
   外部实例化必抛 `ValueError`；`ToolExecutor._precheck` 只接受该类型。
4. **单次消费**：控制库单事务 `SELECT … FOR UPDATE` + `INSERT approval_consumptions`
   （`approval_id`/`nonce_hash` 双唯一键）+ 条件 `UPDATE … WHERE status='APPROVED'`。
   N=8 并发实测恰好 1 成功。
5. **审计 fail closed**：两个强制写入点（`AuditService.emit_required`、
   `StepRecordRepository.insert_prepared`）失败即阻断，并写 `AUDIT_WRITE_FAILED` 告警。
6. **审计过滤列来源**：`tenant_id` 取 `tasks.tenant_id`，`actor_user_id` 只取服务端 Principal，
   **不从 payload 兜底**（见 05-freeze.md A.3）。

## 3. 与设计的偏差清单

### D1. 审批披露字段：风险等级/补偿方案/验证计划未持久化（PRD §4 vs 数据模型）

- **现象**：PRD §4 要求批准页展示"风险等级（含风险因子）/补偿方案/验证计划"；
  设计 §2.2 的 `approvals` 列清单里没有对应列，`action_descriptor` 的字段集又是封闭的
  （ADR-0005 §2，多一个字段就报错），因此这三项在 W06 **没有事实来源**。
- **处置**：API 详情返回 `disclosure` 九节，缺的三节显式 `provided=false` +
  `reason=NOT_PERSISTED_IN_W06`（绝不折叠、绝不编造）；`missing_required_fields` 只覆盖
  记录内可校验的绑定字段，`approve()` 据此阻止批准。
- **后果与建议**：这三节目前**不阻断批准**（因为没有数据可校验），W07 应在工具契约里
  增加 `risk/compensation/verification` 声明并随 `action_digest_version` 升级纳入摘要。
  该缺口已在 `05-freeze.md` C.5 登记。

### D2. 事务上下文管理器放在 `database/mysql/transaction.py` 而非 `MysqlClient` 方法

- **原因**：`database/mysql/connection.py` 的**原始字节**被 W04 故障资产哈希覆盖
  （`core/faults/store.py::_HASH_DEPENDENCIES` 含该文件）。改它会让 6 个已冻结 Manifest 判为
  "fault_script_hash 过期"，`test_fault_manifest.py` + `test_fault_assertions.py` 共 36 项直接变红。
- **处置**：`transaction(client)` / `execute_in(client, conn, …)` / `fetch_all_in(client, conn, …)`
  以模块函数提供，语义与设计一致（`BEGIN → yield → COMMIT/ROLLBACK`，且必须复用同一个连接）；
  `connection.py` 保持零改动。
- **代价**：调用方写 `transaction(client)` 而不是 `client.transaction()`；已在模块 docstring 说明。

### D3. `task_events.tenant_id`/`actor_user_id` 历史行未 backfill

- **原因**：`dg_ctl` 对审计表没有 UPDATE 权限（这正是 P0-12 要的），而迁移必须以 `dg_ctl` 执行；
  用管理员回填等于给审计表开"可改写"的口子。
- **处置**：历史行保持 `NULL`；审计查询**以 `tasks.tenant_id` JOIN 为唯一隔离路径**，
  W06 之后写入的行两个过滤列均有值。登记为已知偏差（`05-freeze.md` F 节）。

### D4. `prerequisites_digest` 冻结值重新生成

- **原因**：ADR-0005 §5 的 `a15af786…` 未给出那三项 check 的 `observed` 内容，不可复现。
- **处置**：按"字段集封闭 + 内容明确"重新冻结一组可复现的三项 PASS 输入
  （值 `c5b4b817…`，见 `05-freeze.md` D.3）。`action_digest` 基准与 5 条失效子向量
  与 ADR **逐字节一致**，未受影响。

### D5. "改参数 → channel=target_objects" 的实现口径

- **现象**：由于目标对象由 `extract_target_objects(definition, parameters)` 从参数抽取，
  **改参数时 `parameters_digest` 也会变**，按设计 §5 第 8 项的检查顺序
  （parameters → target_objects → action）会先报 `channel=parameters`。
- **处置**：`target_objects` 通道用于定位"参数没变、目标映射变了"的情形：
  ① 抽取规则原地漂移（`name_field` 改名、`tool_version` 不变）→ `channel=target_objects`
  + `reason=EXTRACTED_TARGETS_EMPTY`；② 库内快照被改 → `channel=action`
  + `reason=DESCRIPTOR_DIGEST_MISMATCH`（消费前校验 `canonical_digest(descriptor) == action_digest`）。
  两条用例：`test_extraction_rule_drift_blocks_execution_without_tool_call`、
  `test_snapshot_tamper_is_located`。

### D6. `Auditor` 不能读审批详情（保持 PRD §6 权限矩阵）

- **现象**：`roles.py` 中 Auditor 只有 `audit:read`，没有 `approval:read`。
- **处置**：不新增权限（PRD T3 明确不得为角色扩权），Auditor 通过 `/v1/audit/events`
  证明"发生了什么"；`GET /v1/approvals/{id}` 对其返回 403。已落测。

### D7. 非 R2 写操作的 Prepared 证据

- **现象**：`_prepare_step` 仅在携带 `ApprovalGrant`（即 R2 已批准路径）时强制落盘；
  无 grant 的写操作（仅存在于测试桩，R0/R1）只记 warning 不落盘。
- **理由**：生产环境不存在 R0/R1 写工具（R3/R4 恒拒绝，R2 必须审批），因此"写操作必须有
  Prepared 证据"在真实路径上成立；该分支保留是为了不让 W03 的既有测试桩被迫引入审批。
- **风险**：若 W07 引入 R1 写工具，必须同步收紧为"任何 write 都必须落盘"。

### D8. `ToolDefinition` 的 write 门禁加在 T04

- 设计把 `preconditions`/`target_objects` 列在 T04 的 `contracts.py` 变更里，实际拆成两步：
  T01–T03 只加字段（不破坏既有测试），T04 加 `model_validator` 并同步修正 6 处 write 构造点
  （架构师清单的 5 处 + `test_planner_schema.py::_StubTool`，后者用参数注入 `side_effect`，
  静态 grep 看不见）。每次提交都保持全绿。

## 4. 未完成项 / 已知限制（如实登记）

1. **不具备 WORM**：只有"数据库授权边界内的追加式 + 哈希链可校验"，无对象锁/保留期/合规认证；
   `ObjectStorageArchiveSink` 是占位（构造即 `NotImplementedError`，默认不装配）。
2. **`dg_ctl` 仍持 DDL**（已移除 `DROP`，但保留 `CREATE/ALTER/INDEX`）：W11 拆出只写/只读账号。
3. **无全局事件链**：链边界是 `task_id`；跨任务查询只能按 `created_at` 排序，不可链式校验
   （API 响应里用 `chain_scope` 显式标注）。
4. **不宣称 Exactly Once**：控制库事务与目标库写入是两笔独立事务；只保证"每张审批至多消费一次"，
   兜底是幂等键 + `action_digest` 核对 + 停止策略。
5. **真实写工具缺失**：本周所有真实工具均为只读 observer，R2 证据来自测试桩；
   真实写工具的 `target_objects` 抽取与风险/补偿/验证声明属 W07。
6. **`Reconciler` 只做状态收敛**：不读目标库、不判断"操作是否真的发生"，证据不足转
   `MANUAL_INTERVENTION`；跨库核对需要 W07 的只读目标库探测。

## 5. 验收映射（PRD §8）

| # | 断言 | 证据 |
|---|---|---|
| T1 | 审批绑定 `action_digest`/指纹/策略版本，详情与重算一致 | `test_approval_api.py::test_operator_creates_approval_with_full_disclosure`、`test_approval_service.py::test_create_returns_full_disclosure` |
| T2 | 六类关键字段变化使审批失效 | `test_approval_consume.py`（parameters/target_objects/tool_version）+ `test_approval_service.py`（environment/fingerprint/policy_version） |
| T3 | 并发只有一个成功 | `test_concurrent_consume_has_exactly_one_winner`（N=8） |
| T4 | 重放/过期/撤销/篡改被拒且无工具调用 | `test_approval_consume.py` 4 项 + `test_approval_service.py` 撤销/拒绝/过期 |
| T5 | 只追加与授权边界 | `test_audit_service.py` 三条 + `test_step_prepared.py::test_prepared_and_observed_tables_reject_update_and_delete` |
| T6 | 完整性可定位 | `test_audit_integrity.py`（18 项）+ `test_audit_service.py` 篡改/删除定位 |
| T7 | 审计失败 R2 fail closed | `test_audit_unavailable_blocks_r2_before_tool_call`、`test_prepared_write_failure_blocks_execution` |
| T8 | 未知结果进 `NEEDS_RECONCILIATION` | `test_write_timeout_becomes_unknown_outcome_and_needs_reconciliation` |
| T9 | Prepared 后崩溃可恢复 | `test_crash_after_prepared_is_recovered_without_replay[0..9]`（10/10，真实子进程 kill） |
| T10 | 全量回归 | 638 unit + 902 total passed（21 PG 环境失败，见文首） |
| T11 | 不宣称 Exactly Once | `grep -r "Exactly Once" docs/` 无肯定性表述；本文 §4.4 明确否定 |

## 6. 代码审计后的加固记录（2026-11-10，未提交）

只读审计发现 W06 存在 P0/P1/P2 缺陷。本节**只记录已落代码且经机器验证的改动**，未修项见 §7。

### 6.1 批次 1：执行后回执与结果闭环

| 缺陷 | 落点 |
|---|---|
| `step_observed` 在生产执行链零写入，成功写入被 Reconciler 误判 | `core/tools/executor.py::_observe_step`：结果确定才落盘；结果未知刻意保持"未观测"由 Reconciler 收敛（不改 T8 断言） |
| Gateway/Worker 链 `UNKNOWN_OUTCOME` 无即时收敛 | `core/gateway/tool_gateway.py::_converge_unknown_outcome`，同调用内迁移 `NEEDS_RECONCILIATION` 并写 `TASK_STATE_CHANGED` |
| 结果未知被记成 `TOOL_CALL_FAILED` | 新增 `AuditEventType.TOOL_CALL_UNKNOWN_OUTCOME` 并修正映射 |

### 6.2 批次 2/3：加固

- **对账漏收敛**：`find_unobserved()` 增 `offset`；`Reconciler.sweep()` 分页遍历到取完，修复未观测调用超过 50 条时被挤出窗口（这也是本机脏库下 T8 复跑失败的根因）。
- **脱敏绕过**：`redact_payload` 键名归一化子串匹配（`db_password`/`access_token` 等变体），非白名单类型一律 `<redacted>`。
- **审计可用性**：`fetch_all(max_rows=limit+1)` 修复 `limit=500` 翻页静默终止；链长达到上限返回 `ok=False` + `CHAIN_TRUNCATED`；`event_types` 限 50 且枚举白名单校验。
- **API**：`X-Trace-Id` 字符集/长度校验；`/healthz/db`、`/healthz/pg` 不再回显数据库指纹；启动恢复状态进 `/healthz.recovery_status`。
- **执行链**：写工具的连接/IO/响应丢失异常归一为 `UNKNOWN_OUTCOME`，不再误判 `ERROR`。
- **审批**：消费事务内锁后复核 10 个摘要/身份列，台账改取 `row.*`（消除 TOCTOU 分叉）；消费侧 `CanonicalizationError` 转成带 `channel` 的拒绝，保证落 `APPROVAL_CONSUME_DENIED`。
- **仓储**：`transition()` 校验状态白名单；`_json_load` 遇损坏 JSON 拒绝映射而非静默返回空值。

### 6.3 机器验证（2026-11-10）

- `tests/unit/` 全量 **642 passed**（含新增 `tests/unit/test_observed_evidence.py` 4 项）。
- `test_w06_acceptance.py -k "p016 or t8"` = **2 passed**（在 `step_prepared` 66 / `step_observed` 14 的残留库下复跑通过）。
- 导入编译、`git diff --check` 通过；W04 冻结资产哈希依赖 3 文件未改动。
- **限制**：本机 `DG_ADMIN_DSN`（root）报 1045，集成测试清理退化，残留未清；四个文件的全量集成回归长时间未返回，已终止，不声称结果。

### 6.4 仍未修复（如实登记，勿据此宣称"全部完成"）

1. `ApprovalGrant` 可经公开 `issue_grant` 在进程内伪造；`TaskService.run_tool(approval_grant=...)` 遗留旁路；`_ensure_grant_matches` 未校验 `action_digest` 与本次参数绑定。彻底修需调整既有集成测试装配。
2. `prerequisites_digest` 绑定运行时探测 `observed`，环境抖动可使已批准审批永久无法消费；涉及冻结向量 §D.3，需 PM 定档。
3. 状态迁移与审计事件未同事务（create/approve/reject/revoke），`APPROVAL_CONSUMED` 仍在 COMMIT 之后（ADR-0006 已知限制 8）。
4. 默认 `auth_mode=dev_local`、部分新事件过滤列仍为 NULL、测试断言使用 `pytest.raises(Exception)`、私有属性注入用例。
5. `D1`（风险等级/补偿方案/验证计划未持久化）仍待 PM 书面裁定门禁路径。
