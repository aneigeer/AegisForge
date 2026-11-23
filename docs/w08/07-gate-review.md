# W8 → W9 门禁复核（独立验证与恢复）

- 复核人：主理人（代架构师门禁职责）｜日期：2026-11-13
- 依据：`docs/w08/01-prd.md`（AC-01…21）、`docs/w08/02-design.md`（§5 事件冻结 11 条、§6 任务表）、`docs/w08/06-qa-report.md`（QA 独立复核，2026-11-12 23:52–11-13 14:41）、`docs/w08/08-drill-report.md`、`docs/w07/07-gate-review.md`（上游门禁与遗留）
- 方式：读 QA 实测数字 + 主理人独立核验 schema/迁移 + 逐条比对设计冻结表；**未重跑任何写库用例**
- 结论编号：`GR-0` 总结论｜`GR-1..GR-4` 四项核对｜`C-1..C-4` 放行条件｜`KD-1..KD-5` 已知偏离｜`NV-1..NV-5` 未验证项

---

## GR-0｜结论：**有条件放行**

> 本行是唯一结论行。不得以「通过」「W08 已收口」作为独立结论用语；对外一律引用本行全文。

| 层 | 判定 |
|---|---|
| 代码完整 | **是**：T01–T06 全部落地，AC-01…21 逐条有测试证据，7 类崩溃注入全过，迁移 008 已在库 |
| 全量回归可判可信 | **否**：`pytest tests/integration` 无一次跑完（900s 仅推进 77%，13 文件未触达）；PG 5432 与 `DG_ADMIN_DSN` root 1045 环境阻塞使既有文件 38 FAILED / 135 ERROR |
| 门禁通过 | **否（有条件放行）**：接受「分层证据 + 环境阻塞豁免 + 偏差补登记」，条件见 C-1…C-4 |

**放行条件**：

| # | 条件 | 状态 |
|---|---|---|
| C-1 | 偏差 #1（`COMPENSATION_DENIED` 在执行阶段不补发）必须书面登记 | **本文 KD-1 已登记**，满足 |
| C-2 | 全量回归口径降级为「分层证据」，并写明「全量通过不可判定」 | **本文 GR-2 与 NV-1 已写明**，满足；但 W11 环境补齐后**必须重跑一次全量**，否则 W12 不得宣称「回归全绿」 |
| C-3 | 环境阻塞写入限制清单，并明确 W11 前处置 | **本文 §5 已列**；W11 门禁前必须解决或由主理人书面豁免，否则 W12 Alpha 只能发技术预览 |
| C-4 | 迁移 008 与新增表结构在库核验 | **本文 §7 已由主理人实测**，满足 |

---

## 1. GR-1｜代码与测试（QA 独立复核，非实现者自述）

| 项 | 实测 | 来源 |
|---|---|---|
| 单元 | **891 passed / 0 failed（4.46s）** | `06-qa-report.md` §1.1 |
| W08 六个集成文件 | **23 passed / 0 failed / 0 errors** | 同上 §1.4 |
| 崩溃矩阵 | `test_w08_crash_matrix.py` **7 passed（117.43s）** | 同上 AC-18 |
| `--once` 真子进程 | **2 passed（38.78s）**，任务落 `NEEDS_RECONCILIATION`、本任务 `tool_calls==0`、无 `TOOL_CALL_RETRIED` | 同上 AC-17 |
| AC 逐条 | **21 / 21 通过**（0 未通过、0 未覆盖） | 同上 §3 |
| 失败是否归因 W08 新代码 | **没有**：全量日志 38 FAILED / 135 ERROR 名单中无任何 `test_w08_*`；4 条取栈失败全部为 admin 认证或既有断言 | 同上 §2.4 |

## 2. GR-2｜全量回归口径（**降级声明**）

- 全量 `pytest tests/integration` 在 900s 内只推进到 **77%**，无 pytest 汇总行，**不构成通过**；24 文件产生结果行、13 文件未触达，后由按文件补齐。
- 因此 W08 的回归证据是 **「1 次未跑完的全量 + 13 个文件独立补齐」拼成**，不是单次全量绿灯。
- 判定：**接受其作为分层证据，但不接受任何「全量回归通过」表述**。W11 必须补一次真正跑完的全量；补跑前 W12 的 Alpha 验收不得引用「全绿」。

## 3. GR-3｜设计冻结表落实（§5 十一条事件）

| # | 事件 | 状态 | 证据 |
|---|---|---|---|
| 1–3 | `VERIFICATION_PLANNED/RESULT/FAILED` | 已发 | `core/verification/verifier.py`；`test_w08_verifier_rejects.py` 断言 PLANNED→RESULT→FAILED 顺序 |
| 4–7 | `COMPENSATION_REQUESTED/APPROVED/EXECUTED/DENIED` | 已发（DENIED 覆盖口径见 KD-1） | `core/verification/compensation.py`；`test_w08_compensation.py` 四项各 1 条 |
| 8 | `RECONCILIATION_SWEPT` | 已发（空结果轮也发） | `workers/runner.py::_sweep_once`；`test_w08_worker_process.py:223` 断言四冻结字段落库 |
| 9 | `RECONCILIATION_FAILED` | 已发（失败轮只发它，与 SWEPT 互斥） | `test_worker_scheduler.py` 抛错用例 |
| 10 | `AGENT_RECOVERY_STARTED` | 已发（幂等，一次接管一条） | `test_recovery_controller.py::test_loop_takeover_emits_started_once` |
| 11 | `AGENT_RECOVERY_STOPPED` | 已发 | T05 原 `AGENT_STOPPED` 已由 T06 替换为冻结枚举 |

**11/11 有发射点**；`AGENT_LOOP_DETECTED`/`AGENT_NO_PROGRESS` 按设计复用、未新建同类。

## 4. GR-4｜已知偏离（KD）

| # | 偏离 | 影响 | 处置 |
|---|---|---|---|
| KD-1 | **执行阶段复用原审批被 Gateway 拒时，不补发 `COMPENSATION_DENIED`**（只发 `APPROVAL_ALREADY_CONSUMED`）；设计 §5 第 7 行字面要求「含复用原审批」 | AC-10 判定通过（拒绝 + 目标行不变），但事件口径与设计不完全一致；按事件检索"补偿被拒"会漏掉该路径 | **W09 修**：在 Gateway 拒绝补偿路径补发 `COMPENSATION_DENIED(reason=REUSES_ORIGINAL_APPROVAL)`；修前不得对外宣称"补偿拒绝事件完整" |
| KD-2 | `RECONCILIATION_SWEPT.scanned` = 产出结论条数，**非全表扫描行数**；`conclusions` 超 50 条被审计层截断 | AC-17 只要求事件落库与四字段存在，通过；但 `scanned` 易被误读 | W09 `P0-W09-06` 扩展 `sweep()` 返回值，补真实扫描总数（PM 已落位） |
| KD-3 | 调度事件挂伪任务 `system:worker_scheduler`，`tenant_id` 过滤列为 NULL，`AuditService.query(tenant_id=...)` 查不到 | 审计 API 无法按租户检索调度事件，只能按 `task_id` 直查 | W09 明确检索方式或补租户归属；在此之前写入运维手册限制说明 |
| KD-4 | `reconcile_interval_s` 只被校验、未被读取（调度节奏由 `worker_poll_interval_s` 决定） | 配置项与实现不一致，不影响 AC-17 | W09 明确语义或删除该配置 |
| KD-5 | `sweep` 失败计入 `RunReport.failed`（无专用计数位） | `clean=False` 语义正确，但 `failed` 含义被扩展 | W09 或 W11 增 `reconcile_failed` 计数位；当前以 `RECONCILIATION_FAILED` 事件为权威 |

## 5. 环境阻塞（C-3 登记）

| # | 阻塞 | 实测 | 影响 | 处置 |
|---|---|---|---|---|
| E-1 | PostgreSQL 5432 拒绝连接 | `ConnectionRefusedError [WinError 10061]` | PG 相关 21 条集成 FAILED；W12「双引擎只读诊断」无法验收 | W11 前启动 PG LAB 或书面豁免；未解决则 W12 只能发技术预览 |
| E-2 | `DG_ADMIN_DSN` root 1045 | `DatabaseAuthError 1045 Access denied` | 135 条 fixture teardown ERROR，测试残留无法清理 | W11 前提供可用管理员凭据 |
| E-3 | 控制库历史残留 830+ 行未完成任务，只追加表无 DELETE | `tasks` 830 行（非终态 763） | 启动恢复已被预算护栏挡住；测试数据持续堆积 | W11 备份恢复演练时一并处置；不得为此放宽只追加授权 |
| E-4 | W07 NV-1 容器运行态未跑 | `docs/w07/07-gate-review.md` §3.1 | 生产装配只在 in-process 验证 | W11 容器实跑；W12 发布前必须补 |

## 6. 未验证项（NV，不得当作通过）

| # | 项 | 原因 |
|---|---|---|
| NV-1 | 全量 integration 一次跑完 | 900s 仅 77%，13 文件未触达（后按文件补齐） |
| NV-2 | 17 条 FAILED / 135 条 ERROR 逐条根因 | QA 只对 4 条取栈；其余按分布一致性推断，未逐条证明 |
| NV-3 | AC-20 生产 registry 实跑构造 | 需连库；当前为静态核对 + unit 断言 |
| NV-4 | 容器运行态（E-4） | 未执行 |
| NV-5 | 测试写库残留清理 | `DG_ADMIN_DSN` 不可用，清理降级为 warning |

## 7. 主理人独立核验（C-4）

以下为 2026-11-13 15:0x 用 `dg_ctl` 实测 [只读 SELECT / information_schema]：

```
migrations: 001…008（含 008_w08_verification）
verification_results 列: result_id, tenant_id, task_id, tool_call_id, plan_digest,
  verdict, outcomes_json, disposition, approval_required, claimed_success, collected_at
approvals.compensation_of: IS_NULLABLE=YES, COLUMN_KEY=UNI
```

→ 迁移已应用、表结构覆盖 `VerificationResult` 全字段、`compensation_of` 可空唯一，与设计 §3 一致。

## 8. W09 必须承接（按优先级）

1. **KD-1**：Gateway 拒绝补偿路径补发 `COMPENSATION_DENIED`（P0，事件完整性）。
2. **KD-2**：`sweep()` 返回值扩展真实扫描总数，`scanned` 语义校准。
3. **KD-3/KD-4**：调度事件租户归属或检索方式定稿；`reconcile_interval_s` 语义定稿。
4. **KD-5**：`RunReport` 增核对失败计数位（可与 KD-2 同批）。
5. **E-1/E-2**：W11 前处置；W09 的脱敏/报告/记忆不得依赖 PG 或在 PG 不可用时静默降级为"通过"。

## 9. 复核结论汇总

| 核对项 | 结论 |
|---|---|
| AC 21 条 | **21/21 通过**（QA 独立复核，逐条有证据） |
| 设计冻结 11 事件 | **11/11 有发射点**；DENIED 口径偏离（KD-1） |
| 失败归因 | **无一条归因 W08 新代码** |
| 全量回归 | **不可判可信**，已降级为分层证据（C-2） |
| 迁移与表结构 | **已在库核验通过**（C-4） |
| 门禁 | **有条件放行**：C-1/C-4 已满足，C-2 已声明，C-3 挂 W11 前处置 |
