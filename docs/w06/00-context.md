# W06 实施上下文（主理人整理，供团队参考）

- 整理时间：2026-11-09
- 基线：`d2ceae8`（W05 已提交），全量 660 passed（Python 3.14.7，双 LAB）
- 当前周次：W06 审批事务与追加式审计；下一门禁 W6 → W7（单引擎沙箱修复）

## 一、不可违反的既有约束

| 约束 | 现状 |
|---|---|
| 运行时 | Python 3.14.7，`requires-python >=3.14,<3.15` |
| 依赖 | 以 `requirements.lock.txt` 冻结；新增第三方依赖需说明理由并更新锁文件 |
| 数据库 | 控制库 MySQL 8.0.46，schema `dg_system`；目标库 MySQL 8 + PostgreSQL 12 LAB |
| 控制库账号 | 应用运行时只用 `dg_ctl`：有 DDL 与 SELECT/INSERT，`task_events`/`evidence` **无 UPDATE/DELETE** |
| 环境 | 本机 Docker daemon 不可用，容器网络不能作为验证手段 |
| 语言 | 代码注释与文档使用简体中文；标识符用英文 |

## 二、可直接复用的既有能力

| 模块 | 路径 | 现状 |
|---|---|---|
| 策略引擎 | `core/security/policy.py` | `PolicyEngine.decide()` 对 R2 一律返回 `allowed=False`、`reason="R2_REQUIRES_APPROVAL_W06_CLOSED"`、`required_approval=True` |
| 身份与角色 | `core/security/roles.py`、`models.py` | `Approver` 有 `approval:decide`；`Operator` 有 `approval:create`；`Administrator` 有 `approval:create/read` 但**没有** `approval:decide`（职责分离，不得破坏） |
| 唯一执行入口 | `core/gateway/tool_gateway.py` | `execute()` 已实现验签 → nonce 落库 → 租约校验 → 状态版本 → 工具版本 → 参数摘要 → 策略判定 → 审计 → Executor |
| 执行器 | `core/tools/executor.py` | `_precheck()` 对 R2 要求 `ApprovalDecision`，否则 `APPROVAL_REQUIRED`；R3/R4 恒拒绝 |
| 审批占位模型 | `core/tasks/models.py::ApprovalDecision` | 仅 `approved: bool` 等字段，**未绑定 digest、环境、策略版本**，W06 要替换 |
| 事件哈希链 | `core/tasks/repository.py::insert_event` | `(task_id, seq)` 唯一 + `previous_event_hash` + `event_hash`，撞唯一键重试；数据库层无 UPDATE/DELETE |
| 执行信封 | `core/gateway/envelope.py` | HMAC-SHA256 签名、`args_digest`、nonce、有效期 |
| 迁移 | `database/migrations/001`–`005` | `scripts/migrate.py` 执行，`schema_migrations` 记录版本，`rollback/` 放回滚脚本 |
| 错误码 | `core/errors.py::ErrorCode` | 已有 `APPROVAL_REQUIRED`、`STATE_CONFLICT`、`NONCE_REPLAY` 等 |
| 审计事件类型 | `core/audit/events.py::AuditEventType` | 已有 `POLICY_DECISION`、`GATEWAY_EXECUTION_ALLOWED/DENIED` 等 |
| 测试夹具 | `tests/conftest.py` | `control_client`、`admin_client`、`pg_ro_client` 等；集成测试缺配置时 skip |

## 三、W06 必须补上的缺口（读代码确认）

1. 控制库**没有** `approvals`、`audit_events` 表；现有审计写的是 `task_events`。
2. 没有 RFC 8785 JCS 规范化实现，也没有 `action_digest` / `prerequisites_digest`。
3. `ApprovalDecision` 是内存布尔值，**无法证明审批绑定到某一次具体操作**。
4. 没有审批 API（创建/查询/批准/拒绝/撤销）。
5. 没有 `StepPrepared` / `StepObserved` 持久化，也没有 `UNKNOWN_OUTCOME → NEEDS_RECONCILIATION` 的落地路径。
6. 没有审计完整性校验入口（无 `audit:read` API，无 CLI）。
7. 没有 WORM 选型 ADR。

## 四、验收硬指标（摘自任务书，逐条可测）

- 审批绑定 `action_digest` + 环境指纹 + 策略版本；任一关键字段变化使审批失效。
- 并发消费只有一个成功，其余 `APPROVAL_ALREADY_CONSUMED`。
- 重放、过期、撤销、参数篡改全部拒绝。
- 审计事件只追加；普通业务组件不能 UPDATE/DELETE；完整性校验能发现篡改。
- 审计写入失败时 R2 fail closed 并告警。
- `UNKNOWN_OUTCOME` 必须转 `NEEDS_RECONCILIATION`，不得自动重试非幂等操作。
- 不得宣称 Exactly Once；控制库与目标库无分布式事务。

## 五、文档产出约定

| 阶段 | 产出路径 |
|---|---|
| 需求 | `docs/w06/01-prd.md` |
| 架构与任务分解 | `docs/w06/02-design.md` |
| 实施说明 | `docs/w06/03-implementation.md` |
| 测试报告 | `docs/w06/04-test-report.md` |
| ADR | `docs/adr/ADR-0005-*.md`、`ADR-0006-*.md` |

## 六、验证命令

```powershell
# 迁移
.\.venv\Scripts\python.exe scripts\migrate.py

# 单元测试（快，无需数据库）
.\.venv\Scripts\python.exe -m pytest -o addopts="" --assert=plain tests\unit -q

# 全量（需双 LAB，约 5 分钟）
.\.venv\Scripts\python.exe -m pytest -o addopts="" --assert=plain
```
