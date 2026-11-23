# W12 接口合同（T04 / AC-13）

> 本文件是 **W12 对外材料**：只**引用**既有契约件与 `docs/w12/openapi.json` 的实测导出，
> **不新增** API 端点、**不改** `apps/api/main.py`、**不引**文档框架。
> 口径：`docs/w12/02-design.md` §9.3/§9.4、`docs/w12/03-task-cards.md` T04。
>
> 生成方式：`core/release/docs.py::export_openapi_31()` →
> `apps.api.main.create_app().openapi()`（函数内 import，只读）。

## 1. OpenAPI（实测）

| 项 | 实测值 |
|---|---|
| `openapi` | **`3.1.0`**（主版本 `3.1`，由 `FastAPI 0.162.0` 原生产出） |
| `info.title` | `DBGuardian API` |
| `info.version` | `0.1.0` |
| `paths` 数量 | **31** |
| tags | `adapters` / `agent` / `approvals` / `audit` / `auth` / `tasks` / `v1` |
| 导出件 | `docs/w12/openapi.json`（只读导出，不参与运行期） |

- 导出即**同一份** `create_app().openapi()`，因此合同与运行期不会出现第二个真值；
- 代码中原本没有 `app.openapi()` 调用点，本轮**不新增**端点，只新增**导出函数**。

## 2. DTO 与数据模型（源码即合同）

| 主题 | 合同载体 |
|---|---|
| 任务 / 步骤 / 工具调用 / 事件 / 检查点 | `core/tasks/models.py`（`extra="forbid"`） |
| 工具输出统一结构 | `core/tools/contracts.py::ToolResult` |
| 工具元数据与三节披露 | `core/tools/contracts.py::ToolDefinition` / `CompensationSpec` / `VerificationAssertion` |
| 审批披露与摘要 | `core/approval/disclosure.py`、`core/approval/digest.py` |
| 验证结论 | `core/verification/models.py`（`verdict ∈ {PASS, FAIL, UNKNOWN}`，无自然语言字段） |
| 证据与报告事实 | `core/evidence/models.py`（`ReportFacts`：`:121-130`，D-03 实测行号） |
| 诊断假设 | `core/diagnosis/models.py::Hypothesis`（支持/反证/缺失/范围/置信度带） |
| 发布与限制清单 | `core/release/publish.py`（T05）/ `core/release/docs.py`（T04） |

## 3. 错误码（封闭枚举）

`core/errors.py::ErrorCode` 为闭集：`CONFIG_INVALID` / `REQUEST_INVALID` / `DB_AUTH_FAILED` /
`DB_UNAVAILABLE` / `DB_TIMEOUT` / `DB_PERMISSION_DENIED` / `DB_QUERY_ERROR` / `POLICY_DENIED` /
`AUTH_REQUIRED` / `AUTH_INVALID` / `AUTH_EXPIRED` / `FORBIDDEN` / `ENVELOPE_INVALID` /
`NONCE_REPLAY` / `WORKER_LEASE_INVALID` / `FENCING_TOKEN_STALE` / `WORKER_CONFLICT` /
`TOOL_NOT_FOUND` / `TASK_NOT_FOUND` / `STATE_INVALID` / `STATE_CONFLICT` / `EVIDENCE_INVALID` /
`TENANT_INVALID` / `IDEMPOTENCY_CONFLICT` / `APPROVAL_*` 系列 / `AUDIT_UNAVAILABLE` /
`MODEL_*` 系列 / `PLAN_INVALID` / `BUDGET_EXCEEDED` / `LOOP_DETECTED` / `NO_PROGRESS` /
`INTERNAL` / `REDACTION_BLOCKED` / `REDACTION_REVIEW_REQUIRED` / `EVIDENCE_REF_INVALID` /
`KNOWLEDGE_INVALID` / `ORACLE_ACCESS_DENIED` / `ORACLE_LEAK_DETECTED` / `BENCHMARK_INVALID`。

- **W12 零生产枚举改动**：本轮**不新增**任何错误码（`core/errors.py` 未改）；
- 策略拒绝码（非 `ErrorCode`）见 `core/security/policy.py`：`R4_NOT_EXECUTABLE`（`:218`）、
  `R3_AGENT_EXECUTION_FORBIDDEN`（`:225`）、`R2_REQUIRES_APPROVAL`（`:230-238`）。

## 4. 状态机合同

`core/tasks/state_machine.py` 定义合法迁移闭集（`allowed_targets()` / `ensure_transition()`）；
外部只能经 `TaskService.transition()` 迁移，越权迁移抛 `STATE_INVALID` / `STATE_CONFLICT`。

关键不可逆/半不可逆语义：

- `EXECUTING` 中断后**不自动续跑**，转 `NEEDS_RECONCILIATION`（超时/中断 ≠ 失败）；
- 工具结果 `UNKNOWN_OUTCOME` 转 `NEEDS_RECONCILIATION`，**绝不**自动重试（P0-18）；
- `CANCEL_REQUESTED` 与 `CANCELLED` 分离：取消意图与取消结果不同态。

## 5. Tool JSON Schema 与写边界

- 每个工具以 `ToolDefinition` 声明 `input_schema` / `output_schema` / `risk_level` /
  `side_effect` / `permissions` / `allowed_environments` / `operation_class`；
- **写动作只有 1 个**：`mysql_lab_update_order_note` / `1.0`（`adapters/mysql/lab_repair.py`，
  LAB、R2、单行、`target_objects` + `compensation` + 3 条 `verification_plan` 齐备）；
- 第二引擎（PostgreSQL / MCP）**只读**：`[d for d in registry.definitions() if d.side_effect=="write"] == []`
  （`write_tools()` 不存在，D-06）。

## 6. 迁移说明（**PARTIAL**）

- 版本权威：`database/migrations/*.sql` 实测 **11 个正向（`001`…`011`）+ 8 个回滚**；
- `docs/migrations.md` 的版本表**止于 `007`**，且 `deploy/environment-manifest.example.json`
  的 `migration_version="001_harness_core"` 亦滞后 → 文档完整性标 `PARTIAL`（`DOC-PARTIAL`）；
- **不声称**迁移说明齐全；W12 **零 DDL、零迁移、不执行回滚**，本轮未修改 `docs/migrations.md`。

## 7. 边界声明

- 本文件与 `docs/w12/openapi.json` 均**不**含业务请求正文、SQL 原文、参数值或真实口令；
- 演示与验收的产物分离（AC-20）：演示脚本件见 `docs/w12/demo-script.md`，
  **不进入**任何 `ac_result` 判定输入。
