# W12 安全材料索引（T04 / AC-13）

> 本文件是 **W12 对外材料**：把分散在 W05–W11 的**既有**安全材料集中索引，
> 只**引用**、不复制、不改写（`docs/w07/**`–`docs/w11/**` 一律只读）。
> 口径：`docs/w12/02-design.md` §6、`docs/w12/03-task-cards.md` T04、
> `docs/w10-w12-lead-rulings.md` §13/§14。
>
> 状态口径：材料**存在性**逐项实测；材料**完整性**如有滞后则标 `PARTIAL` 并写明缺项，
> **不声称齐全**；环境类结论按 `NOT_EXECUTED` / `NOT_VERIFIED` 处置，**不判 FAIL**。

## 1. 威胁模型与边界

| 主题 | 既有材料 |
|---|---|
| 威胁模型 / 信任边界 | `docs/adr/ADR-0001-runtime-and-dual-engine.md`、`docs/adr/ADR-0004`、`docs/security/identity-and-policy.md` |
| 依赖与运行边界 | `docs/adr/ADR-0003`（镜像与冻结）、`docs/adr/ADR-0006`（审计账本） |
| 账号级隔离（非网络级） | `docs/w07/07-gate-review.md`（KD-1：账号级已实测；网络级未验证，登记为不承诺项） |

- 六类**不承诺**能力已逐条写入 `docs/w12/known-limitations.json`（`NP-*` 六项），
  其中「网络级隔离已验证」标 `NOT_SUPPORTED`：账号级证据不能替代网络级验证。

## 2. RBAC 与身份

| 主题 | 既有材料 / 载体 |
|---|---|
| 角色与权限闭集 | `core/security/roles.py`（`Role` / `Permission`） |
| 主体与认证态 | `core/security/models.py`（`Principal`、`AuthMethod`） |
| 授权判定 | `core/security/authz.py`（`ensure_permission` / `ensure_task_access` / `require_owner_control`） |
| 安全测试材料 | `docs/security/w05-security-tests.md` |

- 租户归属**只**取服务端 `Principal`（客户端提交的 `tenant_id` 被忽略并留痕），
  见 `apps/api/routes/v1.py`（W12 AC-07 的 UI 绕过实测即以此为准）。

## 3. 风险分级与策略拒绝

| 风险级 | 处置 |
|---|---|
| `R0`/`R1` | LAB 内只读 / 受限观测；`R1` 额外要求 Operator 角色 |
| `R2` | **必须**一次性审批授权（`ApprovalService.consume()` 的 `ApprovalGrant`），策略层永不放行 |
| `R3` | **禁止** Agent 执行（`R3_AGENT_EXECUTION_FORBIDDEN`，`core/security/policy.py:225`） |
| `R4` | **禁止**执行（`R4_NOT_EXECUTABLE`，`core/security/policy.py:218`） |

- 执行器侧同口径双保险：`core/tools/executor.py::_precheck` 对 `R3`/`R4` 直接拒绝；
- 注册表内**不得**出现 `R4` 定义（W12 AC-06 等价断言 `not any(d.risk_level == "R4" ...)`）。

## 4. 审批与披露

| 主题 | 既有材料 / 载体 |
|---|---|
| 审批生命周期（请求/批准/消费/撤销） | `core/approval/service.py`、`core/approval/models.py` |
| 三节披露（风险/补偿/验证计划） | `core/approval/disclosure.py` |
| 动作摘要与目标对象抽取 | `core/approval/digest.py` |
| 状态语义（已执行/失败/结果未知） | `core/approval/state_semantics.py` |

- 审批摘要 `action_digest` 覆盖参数与目标对象；`prerequisites_digest` 覆盖九类前置条件；
- 审批**消费即一次性**，重放/篡改/过期/撤销均有稳定拒绝码（见 `docs/w07/**`）。

## 5. 审计与脱敏

| 主题 | 既有材料 / 载体 |
|---|---|
| 追加式审计账本 + 哈希链 | `core/audit/service.py`（`emit_required`/`try_emit`）、`core/audit/integrity.py` |
| 链校验与截断口径 | `core/audit/service.py::verify`（`CHAIN_TRUNCATED` 不静默报绿） |
| 脱敏引擎与闸门 | `core/redaction/engine.py`、`core/redaction/gate.py`、`core/redaction/quarantine.py` |
| 敏感类别与红黑名单 | `core/redaction/models.py`（`RedactionChannel` 七通道）、`core/redaction/rules.py` |
| 回归门禁 | `tests/integration/test_w09_redaction_pipeline.py`（4 passed，W12 未改断言） |

- R2 执行链的关键事件**必须**走 `emit_required`（写失败即阻断执行），**不得**接 `try_emit`
  （W11 D-03 口径；W12 AC-08 已实测方向：`emit_required_raised=True`、`try_emit_swallowed=False`）；
- 三处越界面（模型上下文 / 工具结果 / 训练集）在 `MODEL_INPUT`、`TOOL_RESULT`、`TRAINING_SAMPLE`
  通道下均无敏感样本残留（W12 AC-09 实测）。

## 6. Fault Manifest 与 Oracle 隔离

| 主题 | 既有材料 |
|---|---|
| 故障场景冻结清单（六场景） | `benchmarks/faults/manifests/{mysql,pg}_{slow_query,connection_pressure,lock_wait}.json` |
| 故障注入与断言 | `benchmarks/faults/**`（**冻结资产，本轮只读**） |
| Oracle 隔离（评测不得读答案） | `docs/w10/02-design.md`、`core/benchmark/**`（`ORACLE_ACCESS_DENIED` / `ORACLE_LEAK_DETECTED`） |
| 数据切分（train/eval 不交叉） | `docs/w10/metrics-definition.md`、`benchmarks/w10/metrics-v1.json` |

- 演示与诊断**只**走隔离 LAB；演示件 `lab_only=True`、`production_dsn_referenced=False`。

## 7. 已知偏离（`origin=MANUAL`，只登记）

详见 `docs/w12/known-limitations.md` 第 3 节。与安全直接相关的两条：

| 编号 | 内容 | 影响面 |
|---|---|---|
| `KD-W12-01` | **已关闭**（2026-11-17）：AC-03 独立验证结论 `PASSED`（原 `FAILED`；`core/verification/plan.py` 读取端形状归一：优先 `result["data"]`、回落顶层） | 历史口径：「验收结论不得表述为 verification PASS」曾适用于关闭前；关闭后按实测 `PASSED` 记录。发布类型仍由 W11 发布决定派生（当前 `TECH_PREVIEW`），不因 AC-03 变化 |
| `KD-W11-01`/`02`/`03` | W11 备份恢复口径偏离（桶命名 / 覆盖漏项 / 哨兵归属） | 备份恢复能力口径以 W11 报告为准，W12 不改其实现 |

## 8. 边界声明

- 本文件**不**收录任何口令、DSN、SQL 原文或请求正文；
- 环境阻塞五项（`ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL`）未解，
  相关能力按 `NOT_EXECUTED` / `NOT_VERIFIED` 记录，环境**未**清零；
- 本文件不改变 W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09/W10/W11「门禁未评」的既有口径。
