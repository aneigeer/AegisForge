# W6 → W7 门禁复核（文档与冻结件）

- 复核人：架构师｜依据：任务书 §6、PRD §8 T10、`docs/w06/02-design.md`
- 输入：`05-freeze.md`（206 行）、`06-conformance-audit.md`（125 行）、`03-implementation.md`、ADR-0005/0006、`089f926`+`a22662a`+`6176430`
- 方式：只读代码与文档；**未跑测试**（QA 并行中，避免数据库争用）。表中"独立复算"指用 `core/approval/digest.py` 现场重算，不依赖测试。

## 结论：**有条件放行**

功能与安全门禁本身已过（二轮审计三条 PASS、四件套齐备且关键摘要可独立复现），但存在 **1 条需要产品经理书面裁定的 P0 合规偏差**（见 §5）。该偏差被工程师自行判为"不阻断批准"，超出工程师权限范围，必须由 PM 定档；不得以"技术实现困难"直接放过。

## 1. P0-21 冻结件四件套

| # | 冻结件 | 判定 | 证据 |
|---|---|---|---|
| 1 | 审批协议（状态机 / 9 项消费校验 / 错误码表 / API 契约） | **齐** | `05-freeze.md:44-107`（C.1 字段表、C.2 状态白名单、C.3 错误码 8 行、C.4 九项校验与 `channel` 取值、C.5 API 契约）；与代码一致：`core/approval/models.py:47-63`（白名单）、`core/errors.py:40-50`（9 个错误码） |
| 2 | digest 向量（`action_digest` / `prerequisites_digest` / 分通道 / 反向量） | **齐且可复现** | `05-freeze.md:109-160`；**独立复算**：`BASE_CANONICAL_TEXT` 的 SHA-256 = `f31f1537…`（与 §D.2 一致）；按 §D.3 输入重算 `prerequisites_digest` = `c5b4b817…`（与冻结值逐字节一致，且倒序输入同值，验证排序无关）；反向量见 `tests/unit/test_canonical.py:129` |
| 3 | 审计事件 Schema（字段 / 类型枚举 / 脱敏） | **齐** | `05-freeze.md:5-28`（A.1 11 个新类型、A.2 哈希输入九字段与脱敏规则、A.3 过滤列约束、A.4 消费拒绝两个事件的口径） |
| 4 | 故障演练报告 | **齐（含 T9 十次数据）** | `05-freeze.md:162-191`（E1–E10 十项、T5 四断言、T9 10/10 与启动抖动 retry-once 说明、`audit_verify.py` 人工核对结果） |

**引用真实性抽验**：冻结件引用的 14 个测试函数名，我逐个 `grep` 全部存在（含 `test_concurrent_consume_has_exactly_one_winner`、`test_audit_unavailable_blocks_r2_before_tool_call`、`test_control_account_cannot_drop_tables`、`test_crash_after_prepared_is_recovered_without_replay[0..9]` 等），**无编造引用**。

## 2. `05-freeze.md` 的"未完成"表述

**已清除**。文件头现为"本文件是 W06 的**冻结证据**（P0-21）…任何改动必须先升 `action_digest_version`"（`05-freeze.md:3`），全文 `grep "不完整|待补齐|待填写|尚未|未完成|TODO"` **无命中**。此前的 44 行"不完整件"状态已解除，不构成自相矛盾。

## 3. ADR 与实现一致性

| 项 | 一致性 | 处置 |
|---|---|---|
| ADR-0005 `action_digest` 基准与 5 条失效子向量 | **一致**（`f31f1537…`、`9c25c50a…`、`0b164e1a…`、`215e72a9…`、`a9156994…`、`ed21be51…` 与 freeze §D.2 逐条相同） | 无需改 |
| ADR-0005 `prerequisites_digest` | **原先不一致**：ADR 写 `a15af786…`，freeze §D.3 为 `c5b4b817…` | **以 freeze 为准**（ADR 未记录三项 check 的 `observed`，不可复现，不满足冻结要求）。**已由我改** `ADR-0005:173`：标注 `a15af786…` 作废并指向 freeze §D.3 |
| ADR-0006 "已移除 `DROP`" 与 `scripts/bootstrap_mysql.py` | **一致**（ADR-0006:126 ↔ `scripts/bootstrap_mysql.py:49` 为 `GRANT CREATE, ALTER, INDEX, REFERENCES`，无 `DROP`） | 无需改 |
| `05-freeze.md` C.4 "拒绝一律先落 `APPROVAL_CONSUME_DENIED`" 与 A.4 | **原先自相矛盾**（状态类拒绝不落该事件） | **已由我改**：C.4 改为"摘要/绑定类拒绝先落…；状态类由状态迁移事件 + Gateway 事件承担" |

两处文档修正均属"消除自相矛盾"，未改任何实现与测试。

## 4. PRD 22 条 P0 可追溯性（抽查 6 条）

| P0 | 实现 | 测试 |
|---|---|---|
| **P0-02** 规范化 + 冻结向量 | `core/security/canonical.py`（`canonical_json` / `canonical_digest`）、ADR-0005 | `test_canonical.py:68 test_frozen_vector_is_byte_exact`、`:129 test_rejected_values_raise_canonicalization_error` |
| **P0-04** 消费时重算摘要与指纹 | `core/approval/service.py:531-540`（指纹重算）、`:585-608`（参数/目标/整体重算） | `test_approval_consume.py:564 test_extraction_rule_drift_blocks_execution_without_tool_call`、`test_approval_service.py:456 test_environment_change_is_located` |
| **P0-06** 原子单次消费 | `service.py:610-661`（`transaction` + `SELECT…FOR UPDATE` + 台账 `INSERT` + 条件 `UPDATE`）、`repository.py:324-336`（`WHERE status='APPROVED'`，返回 affected） | `test_concurrent_consume_has_exactly_one_winner` |
| **P0-12** 不能 UPDATE/DELETE 审计 | `scripts/bootstrap_mysql.py:49`（无 `DROP`）、`:66`（`approvals` 仅 `UPDATE`）；三张只追加表不在 `CONTROL_WRITE_GRANTS` | `test_task_events_remains_append_only`、`test_approvals_allows_update_but_not_delete`、`test_control_account_cannot_drop_tables` |
| **P0-14** 审计失败 fail closed | `core/execution/step_records.py:160-179`（`insert_prepared` 失败 → `AuditUnavailableError`）、`core/tools/executor.py:395`（执行前落盘）、`:433`（R2 走 `emit_required`）、`:204`（`AUDIT_UNAVAILABLE` 拒绝） | `test_audit_unavailable_blocks_r2_before_tool_call`、`test_prepared_write_failure_blocks_execution` |
| **P0-18** 未知结果语义 | `executor.py:449-455`（写超时 → `UNKNOWN_OUTCOME` + `write_timeout_outcome_unknown`）、`:555-563`（`_should_retry` 对 write / 非幂等返回 False，未放宽）、`core/tasks/service.py:401-411`（转 `NEEDS_RECONCILIATION`） | `test_executor.py::test_write_timeout_becomes_unknown_outcome_and_needs_reconciliation` |

**6/6 链路完整，无缺链。**

## 5. 放行条件：PRD §4 硬规则① 的偏差须由 PM 裁定（唯一卡点）

- **现象**：PRD §4 要求「风险等级 / 补偿方案 / 验证计划」在批准前必须可见，且"任一必需字段缺失…**阻止批准**（fail closed）"。实现中这三节**没有数据来源**（`ToolDefinition` 未声明、`approvals` 无对应列、`action_descriptor` 字段集封闭），API 显式返回 `provided=false` + `reason=NOT_PERSISTED_IN_W06`（`apps/api/routes/approvals.py:94-117`），但 **`missing_required_fields()` 把它们排除在必需项之外**（`core/approval/models.py:372-400` 只列 14 个已持久化字段），因此 **不阻断批准**（`core/approval/service.py:313-320` 只在 `missing` 非空时拦）。
- **判定**：这一条**不满足 PRD §4 硬规则①**（任务书 D1 也要求披露"风险/补偿方案/验证计划"）。工程师自行判为"不阻断"，属越权定档。
- **两条可选路径（需 PM 书面选择其一，任一即可放行）**：
  1. **补实现**：`approvals` 加三列 + `ToolDefinition` 加三项声明，由服务端从契约填充并纳入披露（**不必**进 digest，PRD P0-03 的 digest 字段集未含这三项，故无需升 `action_digest_version`）；成本中等，可留在 W06 内或转 W07。
  2. **改 PRD 并登记 W07**：明确"W06 阶段这三节以 `provided=false` 呈现且不阻断批准"，同时把"批准人在缺少补偿/验证计划时可批准 R2 写操作"列为显式接受的风险。
- 我的建议：**路径 2 + W07 立即承接补实现**。理由：W06 的核心命题是"审批绑到具体操作"，`action_digest`/指纹/前置条件已达成；补偿与验证计划属于"修复方案质量"，与 W07 的单引擎修复范围天然重合，硬塞进 W06 会拖出门禁窗口。

## 6. W07 必须承接的遗留项（8 条）

1. **D1**：风险等级/补偿方案/验证计划三节持久化 + 纳入披露（本复核 §5 的条件项）。
2. **D7**：无 grant 的写操作只记 warning 不落 `step_prepared`；若 W07 引入 R1 写工具，必须收紧为"任何 write 都必须落盘"。
3. **T9 测试脆弱性**：漂移用例通过 `gateway._registry._tools[...] =`（`# noqa: SLF001`）注入契约，依赖私有属性；建议提供 `ToolRegistry.replace()` 或测试注入点，否则该用例可能静默失效。
4. **WORM 未实现**：`ObjectStorageArchiveSink` 构造即 `NotImplementedError`、默认不装配；无对象锁/保留期/合规认证（ADR-0006 已知限制 1）。
5. **`dg_ctl` 账号未拆分**：已移除 `DROP`，但仍可 `ALTER TABLE … DROP COLUMN` 破坏表结构（ADR-0006 已知限制 2，登记 W11）。
6. **审计历史行 `tenant_id`/`actor_user_id` 为 `NULL`**：因审计表不得为回填开 `UPDATE`；查询以 `tasks.tenant_id` JOIN 为权威路径（D3、freeze §A.3/F）。
7. **T9 启动抖动 retry-once**：进程级用例遇启动抖动会换新 ID 重试**一次启动**；W07 应补连续多轮稳定性数据，避免长期用重试掩盖环境问题（freeze §E）。
8. **`Auditor` 无法读审批详情**（D6）：与 PRD §6 权限矩阵一致，但 U4"按审批维度取证"只能靠 `/v1/audit/events` 间接达成，W07 复核是否需要只读审批视图。

## 7. 复核结论汇总

| 核对项 | 结论 |
|---|---|
| 四件套 | **齐**，关键摘要**可独立复现**，引用测试**全部真实存在** |
| `05-freeze.md` 残留表述 | **已清除**，无自相矛盾 |
| ADR ↔ 实现 | ADR-0006 **一致**；ADR-0005 的 `prerequisites_digest` 曾不一致，**已以 freeze 为准修正**；freeze C.4 与 A.4 矛盾**已修正** |
| P0 抽查 6 条 | **6/6 链路完整** |
| 门禁 | **有条件放行** —— 唯一条件是 §5 由 PM 书面裁定；其余不构成阻断 |
