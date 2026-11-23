# W06 冻结件

> 本文件是 W06 的**冻结证据**（P0-21）。A/B 两节由架构侧给出（审计事件 Schema 与字段迁移约束、人工核对与处置），C–F 由 T05 补齐：审批协议（字段/状态白名单/错误码/API）、digest 冻结向量、故障演练报告、审计观察项 A.3/A.4 的处置结果。任何改动必须先升 `action_digest_version` 并在本文件重新冻结。

## A. 审计事件 Schema 与字段迁移约束

### A.1 W06 新增事件类型（`core/audit/events.py`，12 个）
`APPROVAL_REQUESTED`、`APPROVAL_APPROVED`、`APPROVAL_REJECTED`、`APPROVAL_REVOKED`、`APPROVAL_EXPIRED`、`APPROVAL_CONSUMED`、`STEP_PREPARED`、`STEP_OBSERVED`、`RECONCILIATION_REQUIRED`、`AUDIT_WRITE_FAILED`、`AUDIT_INTEGRITY_FAILED`、`TOOL_CALL_UNKNOWN_OUTCOME`。既有事件类型不变。

> 批次 1 补记：`TOOL_CALL_UNKNOWN_OUTCOME` 用于把"写操作结果未知"与 `TOOL_CALL_FAILED` 区分开（原先误映射到 FAILED，导致 `payload.status=unknown_outcome` 与事件类型自相矛盾）。该类型只影响枚举取值，**不进入 `event_hash()` 九字段、不改变任何摘要**，因此无需升 `action_digest_version`；`step_observed` 的写入落点见 `core/tools/executor.py::_observe_step`（结果确定才落盘，结果未知保持未观测，由 Reconciler 收敛）。

### A.2 `task_events` 的不可变边界（迁移时不得触碰）
- **哈希输入九字段固定**：`task_id`/`run_id`/`tool_call_id`/`event_type`/`payload`/`created_at`/`seq`/`previous_event_hash`，其序列化方式在 `core/tasks/repository.py::event_hash()` 内固化。**任何新增列都不得进入该函数**，否则全仓历史链校验立即失效（W03 起全部事件、含既有 660 项基线断言）。
- **payload 一律先脱敏**：`redact_payload()` 不得绕过；`SENSITIVE_KEYS` 命中即 `<redacted>`，单串上限 2000、条目上限 50。
- **审计专有字段走 payload**：`action_digest`、`policy_version`、`decision`、`result`、`integrity_ref`、`actor`、`trace_id` 全部在 `payload_json` 内，不进列。

### A.3 过滤列的字段迁移约束（O1）
`task_events.tenant_id` / `actor_user_id` 是**索引过滤列**，不是哈希输入。约束：
1. **只允许显式传参**。`insert_event(..., tenant_id=..., actor_user_id=...)` 是唯一合法来源；**禁止**从 `event.payload` 兜底取同名键。理由：兜底会把"payload 内容"与"索引列"隐式耦合，一旦某条事件的 payload 出现客户端可控的 `tenant_id`/`actor_user_id`，租户过滤列即可被污染，而哈希链无法发现（这两列不进哈希）。**状态：T04（`a22662a`）已移除 `repository.py::_payload_str` 兜底**，本条转为长期约束，新增事件时不得恢复兜底写法。
2. **新增过滤列原则上同期 backfill，但审计表不得为回填开 UPDATE**（本条已按偏差 D3 收窄）。新增审计过滤列时，若可以在**同一迁移内**用非审计账号回填则回填；若回填需要给审计表授予 `UPDATE`（当前 `dg_ctl` 无此权限，且"审计表只追加"优先），则**放弃回填**、允许历史行为 `NULL`，并把查询口径固定为"以 `tasks.tenant_id` JOIN 为权威隔离路径"。已落地的 006 即属后者：`task_events.tenant_id/actor_user_id` 历史行为 `NULL`，查询必须 JOIN `tasks`；W06 之后写入的行两列必有值。**不得**为了补齐历史行去申请 `UPDATE` 授权（见 §F 偏差 D3）。
3. **不得为过滤列建唯一键**。审计表只追加，唯一性只由 `(task_id, seq)` 保证。
4. **历史事件 tenant 来源**：`task_events` 无独立租户事实，回填以 `tasks.tenant_id` 为唯一权威（`uq_tasks_tenant` 保证 join 唯一）。

### A.4 消费拒绝的账本记录（已收敛，T04 `a22662a`）
设计 §1.4 要求"审批消费失败必须有账本记录"。已落地为**两个语义不同的事件，不可互相替代**：
- **`APPROVAL_CONSUME_DENIED`**（`core/audit/events.py:72`）：仅在 service 层落一次（`core/approval/service.py:886` → `_emit_consume_denied`），覆盖**摘要/绑定不符**类拒绝，payload 带 `channel` 用于定位；写入用 `try_emit`，失败只降级为 `logger.error(alert="AUDIT_WRITE_FAILED")`，**拒绝照常生效**。
- **`GATEWAY_EXECUTION_DENIED`**（`core/gateway/tool_gateway.py:419-430`）：Gateway 对**所有**消费失败（含状态类拒绝 `PENDING/REJECTED/EXPIRED/REVOKED/CONSUMED`）写此事件。

**冻结查询口径（必须照此写，否则会漏）**：要枚举"所有消费拒绝"，必须同时查 `event_types=["APPROVAL_CONSUME_DENIED", "GATEWAY_EXECUTION_DENIED"]`，并按 `channel`（前者）与 `deny_code`（后者）区分拒绝原因。状态类拒绝**不会**产生 `APPROVAL_CONSUME_DENIED`——其可追溯性由状态迁移事件（`APPROVAL_EXPIRED/REVOKED/REJECTED/CONSUMED`）与 `GATEWAY_EXECUTION_DENIED` 共同保证，属既定设计，非缺陷。

## B. 人工核对与处置

### B.1 中间态清单

| 中间态 | 判定方法 | 处置 | 禁止 |
|---|---|---|---|
| **审批已消费、`APPROVAL_CONSUMED` 事件缺失**（O2） | `approval_consumptions` 有行 且 `task_events` 中该 `approval_id` 无 `APPROVAL_CONSUMED` | 以 `approval_consumptions` 行为权威（`consumed_at`/`consumed_by`/`action_digest`）；在 `06-conformance-audit.md` 同类清单里登记一条人工说明，附 `audit_events` 查询导出 | 不得 `UPDATE approvals` 回退状态；不得伪造补写事件（会破坏链） |
| **审批已消费、目标库操作未发生**（O3） | `approval_consumptions` 有行 且 `step_observed` 无对应行 或 `outcome='DENIED'` | 走 `Reconciler.sweep()` → 任务转 `NEEDS_RECONCILIATION`；确认确实未执行后，转 `MANUAL_INTERVENTION` 由人工决定是否**重新发起一张新审批**（新 `approval_id` + 新 `nonce`） | 不得复用 `approval_id`；不得把"审批已耗"当作"操作已执行" |
| **已 prepared、无 observed**（T9） | `step_prepared` 有行、`step_observed` 无行 | `Reconciler.sweep()` 收敛为 `NEEDS_RECONCILIATION` 或 `MANUAL_INTERVENTION` | **不得重放工具**（`Reconciler` 结构上不持有注册表） |
| **审批 APPROVED 且未消费但已过期** | `status='APPROVED'` 且 `now >= expires_at` | `expire_due()` 落 `EXPIRED` 并写 `APPROVAL_EXPIRED` 事件 | 不得在消费路径"顺手"把状态改成 `CONSUMED` |

### B.2 两条硬规则
1. **状态与事件不得互相替代**：`approvals.status` 是当前态，`task_events` 是历史；出现不一致时**以事件链为审计权威、以状态表为运行态权威**，两者都要保留，不得为了"看起来一致"去改其中一方。
2. **人工补录只允许追加**：任何人工核对结论都必须以**新增事件**（`MANUAL_INTERVENTION` / 说明性 payload）落地，不得修改既有行；`dg_ctl` 对 `task_events` 无 UPDATE/DELETE，这条由数据库兜底，不得申请授权绕过。

## C. 审批协议冻结（T05 补齐）

### C.1 审批字段（`dg_system.approvals`，全部字段均为必需披露项）

| 分组 | 字段 | 说明 |
|---|---|---|
| 归属 | `approval_id`/`tenant_id`/`task_id`/`step_id`/`environment_id`/`db_engine` | 跨租户访问按 404 处理 |
| 环境绑定 | `environment_fingerprint`(摘要) + `environment_fingerprint_json`(原文) | 事实来源唯一：`dg_system.environments` 行 |
| 操作绑定 | `action_digest`/`parameters_digest`/`target_objects_digest` | 分通道摘要用于定位"哪一项变了" |
| 前置条件 | `prerequisites_digest` + `prerequisites_json` | 只含服务端探测事实，人工输入不进入摘要 |
| 契约 | `policy_version`/`tool_name`/`tool_version` | 工具版本漂移即失效 |
| 身份 | `requested_by`/`requested_session_id`/`approver_id`/`approver_session_id`/`authn_context_json` | 申请人 ≠ 审批人（含同一用户不同会话） |
| 一次性 | `nonce_hash`（UNIQUE） | 消费台账另有 `approval_id` + `nonce_hash` 双唯一键 |
| 状态 | `status`/`decision_reason`/`operator_note` | `operator_note` **不参与**任何摘要 |
| 时间 | `created_at`/`expires_at`/`decided_at`/`consumed_at`/`consumed_by`/`revoked_at`/`revoked_by` | 有效期比较只用服务端时间 |

### C.2 状态迁移白名单（P0-05，`core/approval/models.py::ALLOWED_APPROVAL_TRANSITIONS`）

- `PENDING → APPROVED | REJECTED | EXPIRED | REVOKED`
- `APPROVED → CONSUMED | EXPIRED | REVOKED`
- `REJECTED`/`EXPIRED`/`REVOKED`/`CONSUMED` 为终态（无出边）
- 非法迁移（含同态重复裁决）→ `STATE_CONFLICT`；`APPROVED` 不可回退为 `PENDING`
- 撤销：申请人本人或具备 `approval:decide` 的角色；`CONSUMED` 不可撤销

### C.3 稳定错误码（消费/裁决）

| 场景 | 错误码 | HTTP |
|---|---|---|
| 审批不存在（含跨租户） | `APPROVAL_NOT_FOUND` | 404 |
| 仍为 `PENDING` | `APPROVAL_NOT_APPROVED` | 409 |
| 已拒绝 / 已过期 / 已撤销 / 已消费 | `APPROVAL_REJECTED` / `APPROVAL_EXPIRED` / `APPROVAL_REVOKED` / `APPROVAL_ALREADY_CONSUMED` | 409 |
| 摘要不符（`details.channel` 定位） | `APPROVAL_DIGEST_MISMATCH` | 409 |
| 前置条件未全 PASS / 写工具抽不出目标对象 | `APPROVAL_PREREQUISITE_FAILED` | 422 |
| 审计账本不可写（fail closed） | `AUDIT_UNAVAILABLE` | 503 |
| 职责分离（申请人自批） | `FORBIDDEN` + `details.reason=SEGREGATION_OF_DUTY` | 403 |
| 需要审批但未携带 `approval_id` | `APPROVAL_REQUIRED` | 403 |

### C.4 消费九项校验与 `channel` 取值

`tenant`/存在性 → `status` → `expires_at` → 环境一致性 → 环境指纹 → `policy_version` →
`tool_name`+`tool_version` → 参数/目标对象/整体摘要 → 前置条件 → 原子消费。

`APPROVAL_DIGEST_MISMATCH.details.channel` ∈
`{environment, environment_fingerprint, policy_version, tool_version, parameters, target_objects, action, prerequisites}`；
另有 `reason` 细分：`EXTRACTED_TARGETS_EMPTY`（写操作抽不出目标对象）、
`DESCRIPTOR_DIGEST_MISMATCH`（库内展示快照无法复算出 `action_digest`）。
**摘要/绑定类拒绝先落 `APPROVAL_CONSUME_DENIED` 事件**（payload 含 approval_id/task_id/channel/
deny_code/actor_user_id/trace_id），事件写失败只降级为告警日志、拒绝照常生效。**状态类拒绝**
（`PENDING/REJECTED/EXPIRED/REVOKED/CONSUMED`）不落该事件，由状态迁移事件 + Gateway 的
`GATEWAY_EXECUTION_DENIED` 共同承担；枚举"所有消费拒绝"须查两类事件，详见 §A.4。

### C.5 API 契约（PRD §6）

| 方法 | 路径 | 权限 |
|---|---|---|
| POST | `/v1/approvals` | `approval:create` |
| GET | `/v1/approvals`、`/v1/approvals/{id}` | `approval:read` |
| POST | `/v1/approvals/{id}/approve`、`/reject` | `approval:decide` |
| POST | `/v1/approvals/{id}/revoke` | 申请人本人或 `approval:decide`（服务层复核） |
| GET | `/v1/audit/events` | `audit:read` |
| POST | `/v1/audit/integrity/verify` | `audit:read` |

详情响应必须含 `disclosure`（PRD §4 九节全部出现）与 `missing_required_fields`；
W06 数据模型无法提供的三节（风险等级/补偿方案/验证计划）显式标 `provided=false` +
`reason=NOT_PERSISTED_IN_W06`，**不得**折叠隐藏或凭空填充（见 03-implementation.md 偏差 D1）。
Auditor 只有 `audit:read`：可查审计链，不能读审批详情（PRD §6 权限矩阵，W06 未新增权限）。

## D. digest 冻结向量（逐条对应测试函数）

### D.1 规范化向量（ADR-0005 §5，`core/security/canonical.py`）

| # | SHA-256 | 测试 |
|---|---|---|
| V1 | `38b5d9a6be58441e40ba736c50ec9ac004336e9818b647f78fabfd53c64cd302` | `test_canonical.py::test_frozen_vector_is_byte_exact[V1]` |
| V2 | `8f975e8f28effd0f00d336b5792cb8cb01a599141d4da2f630250db2c63ba0f9` | 同上 `[V2]` |
| V3 | `5749ea9be5c94fa197da09e996a67e12a9e7e5db1fc720521112e98bce2bc0bc` | 同上 `[V3]` |
| V4 | `b7b2401ddca2165824e98c61890c0aaec470258d3119dd265d02be9438bf47e6` | 同上 `[V4]` |
| V5 | `d0044d5452bc6cacd54894da533ae405d6d3d8063f9bc4ba7546a6d84faafc8f` | 同上 `[V5]` |
| V6 | `a0a5f63d039042c56ec693c51152ba4676dd17162b6a6f06d447d6b84b0a7b61` | 同上 `[V6]` |

规范串原文见 `tests/unit/test_canonical.py::FROZEN_VECTORS`（逐字节常量）。
反向量（必须抛 `CanonicalizationError`，绝不降级）：`1.5`、`NaN`、`Decimal("1.0")`、`b"a"`、
`{1,2}`、非 `str` 键、孤立代理项、`9007199254740992`
→ `test_canonical.py::test_rejected_values_raise_canonicalization_error`。

### D.2 完整 `action_digest` 基准（`core/approval/digest.py`）

规范串逐字节等于 `tests/unit/test_action_digest.py::BASE_CANONICAL_TEXT`（由实现生成后冻结）。

- `action_digest` = `f31f153767a028a758bb3ef02fb9322422e8fc0ff77c287ad459893f7a8d5bc2`
  （`test_action_digest.py::test_base_action_descriptor_is_byte_exact`）
- `parameters_digest` = `195322569497093bcfc59f0025575f18a24d24c163878f02f0ae83d71e45c904`
- `target_objects_digest` = `6f14bf084071058b294d76e9ea2f372822d8f637c6bd64ef4287471d7eb3eb59`

失效子向量（`test_invalidation_vector_matches_frozen_digest`）：

| 变更 | 摘要 |
|---|---|
| `parameters.expected_rows: 1 → 2` | `9c25c50aabfa54ee11dc9dbe1515f4c662bf72a11041fb3a381b1c2da69c1b04` |
| `policy_version: w06-v1 → w06-v2` | `0b164e1a34ffff0817def520daebd45d021a099fec3b2c04b392e7ef344943ee` |
| `environment_fingerprint.config_version: w03-local → w06-local` | `215e72a91cdf7d1b10b055d1956c60bed6ae7c9df67f624b2a77269393254277` |
| `target_objects[0].name: orders → orders_archive` | `a91569943a0f4353cb2abaae60e1f6401bca6b671a08605d957434cae6bd3710` |
| `tool.version: 1.0 → 1.1` | `ed21be519c2828991119ef5abb4a560d4c5f0c834ec4667ba201cf69c1d27974` |

### D.3 `prerequisites_digest` 冻结输入（W06 新增）

ADR-0005 §5 给出 `a15af786…`，但**未记录那三项 check 的 `observed` 内容**，因此不可复现。
W06 按"字段集封闭 + 内容明确"重新冻结一组可复现的三项 PASS 输入（`action_digest` 为 D.2 基准）：

| check_id | status | observed | source |
|---|---|---|---|
| `environment_fingerprint` | PASS | `{"environment_id":"env_mysql_local","fingerprint_completeness":"PARTIAL"}` | `control_db` |
| `policy_version_matches` | PASS | `{"policy_version":"w06-v1"}` | `server_probe` |
| `tool_version_matches` | PASS | `{"tool_name":"mysql_update_rows","tool_version":"1.0"}` | `control_db` |

- `prerequisites_digest` = `c5b4b8175724de5a55a16772a1bffc6da2276950f69c37d695aa0e78bd64d0dc`
- `checks` 按 `check_id` 的 UTF-16 码元序排序，列举顺序不影响摘要
  （`test_prerequisites_digest_is_order_independent_and_bound_to_action`）。
- 人工输入（`operator_note`）不进入该摘要。

## E. 故障演练报告（本机 LAB，2026-11-09）

| # | 演练 | 结论 | 证据（测试） |
|---|---|---|---|
| E1 | 并发消费同一审批（N=8） | 恰好 1 成功、7 个 `APPROVAL_ALREADY_CONSUMED`，台账仅 1 行 | `test_approval_consume.py::test_concurrent_consume_has_exactly_one_winner` |
| E2 | 无 `approval_id` 的 R2 | `APPROVAL_REQUIRED`、`tool_calls` 计数 0 | `test_approval_consume.py::test_r2_without_approval_id_is_denied_without_tool_calls` |
| E3 | 过期 / 撤销 / 重复消费 / 参数篡改 | 全部拒绝且无工具调用；拒绝落 `APPROVAL_CONSUME_DENIED` | `test_approval_consume.py`（4 项）+ `test_consume_denial_is_audited_with_channel` |
| E4 | 抽取规则原地漂移（`name_field` 改名、版本不变） | 消费被拒、`channel=target_objects`、`reason=EXTRACTED_TARGETS_EMPTY`、工具未调用 | `test_extraction_rule_drift_blocks_execution_without_tool_call` |
| E5 | 审计不可用（`emit_required` 失败） | 抛 `AUDIT_UNAVAILABLE`，工具未调用 | `test_audit_unavailable_blocks_r2_before_tool_call` |
| E6 | `step_prepared` 落盘失败 | 返回 `AUDIT_UNAVAILABLE`，工具未调用 | `test_prepared_write_failure_blocks_execution` |
| E7 | 写操作超时 | `TIMEOUT → UNKNOWN_OUTCOME → NEEDS_RECONCILIATION`，无 `TOOL_CALL_RETRIED` | `test_executor.py::test_write_timeout_becomes_unknown_outcome_and_needs_reconciliation` |
| E8 | 崩溃恢复（T9，子进程 `kill`） | **10/10 成功** | `test_step_prepared.py::test_crash_after_prepared_is_recovered_without_replay[0..9]` |
| E9 | 只追加授权负测（T5 四断言） | 见下表 | `test_audit_service.py` 三条用例 |
| E10 | 篡改/删除事件 | `verify` 返回 `ok=False` 且 `broken_at_seq` 精确、`affected_range` 正确 | `test_audit_service.py::test_verify_detects_tampered_event_with_precise_seq`、`test_verify_locates_deleted_middle_event` |

T5 四断言逐条：① `task_events` 的 UPDATE 被拒（`test_task_events_remains_append_only`）；
② `task_events` DELETE 被拒（同上）且 `approvals` DELETE 被拒
（`test_approvals_allows_update_but_not_delete`）；③ `task_events` 的 `DROP TABLE` 被拒且表仍在
（`test_control_account_cannot_drop_tables`，用临时表探测同名权限，审计表本身不动）；
④ `approvals` 的 UPDATE 仍可用（`test_approvals_allows_update_but_not_delete`）。

T9 十次稳定性：**10/10 通过**（`test_step_prepared.py -k crash`，单轮 ~29s）。
每个用例都是真实 `subprocess.Popen` 子进程：落盘 `step_prepared` 后阻塞 → 父进程轮询控制库
确认已提交 → `proc.kill()`（Windows 为 TerminateProcess）→ `Reconciler.sweep()` 收敛。
若子进程未在 60s 内提交，会**换新 ID 重试一次启动**（只重试启动、不重试执行），失败时把子进程
stdout/stderr 打进断言，用于区分"启动抖动"与"产品缺陷"。

命令行人工核对：`python scripts/audit_verify.py --limit 3` → 3 个任务全部 `OK`
（checked=71/171/793），退出码 0；输出结构与 `POST /v1/audit/integrity/verify` 一致。

## F. A.3 / A.4 处置结果回填

- **A.3 第 1 条（已处置）**：`insert_event()` 的 payload 兜底**已移除**，只接受显式
  `tenant_id`/`actor_user_id`。`AuditService.emit_required(event, actor_user_id=...)` 的
  `tenant_id` 一律取控制库事实 `tasks.tenant_id`，`actor_user_id` 只来自服务端 Principal；
  协调器以 `system:reconciler` 记录。
- **A.3 第 2 条（无法满足，登记偏差 D3）**：历史行**无法 backfill**——`dg_ctl` 对审计表没有
  UPDATE 权限，而"迁移用 dg_ctl 执行"本身是冻结约束；用管理员回填等于给审计表开改写口子。
  处置：历史行保持 `NULL`；审计查询**始终以 `tasks.tenant_id` JOIN 为权威**（唯一隔离路径，
  不是运行时兜底）；W06 之后写入的行两个过滤列均有值。
- **A.4（已处置，采用方案 ②）**：新增 `AuditEventType.APPROVAL_CONSUME_DENIED`，在
  `ApprovalService._mismatch()` 抛错前写事件，只落一次；Gateway 的
  `GATEWAY_EXECUTION_DENIED` 保留（语义不同：执行链拒绝 vs 消费拒绝）。
