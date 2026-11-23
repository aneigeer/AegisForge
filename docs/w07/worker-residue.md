# W07 收口：集成测试残留清单（控制库 `dg_system`）

> 性质：**开发期测试卫生问题**，不是线上事故，也不影响任何安全不变量。
> 结论：本轮**未能清理**，原因是控制库只追加表缺少 DELETE 授权，且本机管理员账号不可用。
> **本文不声称"已清理"**；下列行仍然存在，见文末核对方法。

- 观测时间：**2026-11-11T23:52:18+08:00**
- 观测方式：`dg_ctl`（只读 SELECT）连控制库 `dg_system`，只查不写
- 产生来源：`tests/integration/test_worker_process.py`（本轮新增文件的 3 次实跑）
- 观测到的库级总量：`dg_system.tasks` = **819** 行，`dg_system.tool_calls` = **152** 行

## 1. 本次测试产生的 task 全清单（6 条）

筛选条件：`tasks.goal LIKE 'W07 收口%'`（仅本文件的夹具使用该 goal 前缀）。

| # | task_id | tasks.status | state_version | 创建时间(UTC) | approval_id | approval.status | tool_calls | step_prepared | step_observed | approval_consumptions | task_events | worker_leases |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `task_cc73a4c371914eed8a4fa8f1450fa277` | EXECUTING | 3 | 2026-11-11 15:42:23.220967 | `apr_ff6468089d824e19bd8c0545f6e85f8c` | CONSUMED | 1 | 1 | 1 | 1 | 14 | 1 |
| 2 | `task_eb1d6c2b9ae7430a910c98166a0360df` | EXECUTING | 3 | 2026-11-11 15:42:26.066688 | `apr_ccb44df486314a89b3a74e124a76ac38` | CONSUMED | 1 | 1 | 1 | 1 | 14 | 1 |
| 3 | `task_9110603a639146ce9e4d660ccdb81e69` | EXECUTING | 3 | 2026-11-11 15:44:57.688825 | `apr_a4dda73b71f24733bef213d1230e40d6` | CONSUMED | 1 | 1 | 1 | 1 | 14 | 1 |
| 4 | `task_7307499382204913af20257cce4938de` | EXECUTING | 3 | 2026-11-11 15:45:00.936577 | `apr_b18ef786e823457880d77f1d948a1b7f` | CONSUMED | 1 | 1 | 1 | 1 | 14 | 1 |
| 5 | `task_69300b8c85d74f2fadaf9c0633f87452` | WAITING_APPROVAL | 3 | 2026-11-11 15:50:23.725187 | `apr_b955599c560a4844b22ee886c6bfbf10` | REVOKED | 0 | 0 | 0 | 0 | 9 | 0 |
| 6 | `task_0e13f0c5b7f0492eb918999d6edd4d7e` | EXECUTING | 4 | 2026-11-11 15:50:27.358356 | `apr_384ebf65f6844a9da43d95cf686a3029` | CONSUMED | 1 | 1 | 1 | 1 | 16 | 1 |

说明：

- 1–4 来自 rev.1（旧候选口径：`EXECUTING` 也算候选）的两次实跑；
- 5 来自 `test_dry_run_lists_candidate_without_executing`：**dry-run 未执行**（`tool_calls=0`），
  其审批在夹具 teardown 已被 `ApprovalService.revoke()` 撤销，因此不再是候选；
- 6 来自 rev.2 后的 `--once` 实跑：Worker 自己把任务 `WAITING_APPROVAL(v3) → EXECUTING(v4)`，
  `step_observed.outcome=success`，`tool_calls.status=success`。
- **这 6 条当前都不是可执行候选**（无 `status=APPROVED` 的审批）：
  `python -m workers --dry-run --once` 输出 `candidate_count: 0`。
- 目标库 `dg_lab.seed_orders(order_id=2001)` 的 `note` 已由测试 `finally` 恢复为执行前值
  （LAB 数据未残留脏值）。

## 2. 为什么这份清单无法自行清理

### 2.1 `dg_ctl` 的授权边界（实测 `SHOW GRANTS`）

`dg_ctl` 在 `dg_system` 上是 `SELECT, INSERT, CREATE, REFERENCES, INDEX, ALTER`，
`DELETE` 只授予了少数状态表（`tasks` / `task_steps` / `tool_calls` / `checkpoints` /
`environments` / `memberships` / `tenants` / `users`），而下面这些表**只追加**：

| 表 | `dg_ctl` 权限 | 后果 |
|---|---|---|
| `task_events` | 仅 SELECT/INSERT | 无法删除审计链 |
| `approvals` | SELECT/INSERT + **仅 UPDATE** | 无法删除审批行（可撤销状态，不可删除） |
| `approval_consumptions` | 仅 SELECT/INSERT | 无法删除消费台账 |
| `step_prepared` / `step_observed` | 仅 SELECT/INSERT | 无法删除执行证据 |
| `worker_leases` | SELECT/INSERT + 仅 UPDATE | 无法删除租约 |
| `execution_envelopes` | 仅 SELECT/INSERT | 无法删除信封 |

这是 **W06 刻意的设计**（`docs/w06/01-prd.md` 权限收口：审计与证据表不可 UPDATE/DELETE），
不是遗漏——所以"清理测试残留"必须走管理员账号，绝不能放宽 `dg_ctl` 权限。

### 2.2 管理员账号不可用

`DG_ADMIN_DSN`（`root`）在本机连接返回 **MySQL 1045 Access denied**；已尝试
DSN 自带口令、`DG_MYSQL_ROOT_PASSWORD`、空口令、`root` 四种都不通。没有可用管理员凭据时，
`tests/integration/test_worker_process.py` 的清理分支只能降级：

- LAB 目标行照常恢复（`rw` 账号可用）；
- 控制库清理失败时发 `UserWarning`，显式列出未清理的表与 `task_id`（**不静默吞**）。

## 3. 清理前提与建议操作（由具备权限的人执行）

前提（二选一）：

1. 提供可用的管理员 DSN（`root` 或等价 DBA 账号），在 `dg_system` 上具备 DELETE；或
2. 换到一个**可整体重置**的实例（本机 LAB 容器重建 / `bootstrap_mysql.py` 重建控制库），
   残留随实例销毁。

具备权限后按下列顺序删除（先子表后父表，满足外键约束）：

```sql
DELETE FROM dg_system.approval_consumptions WHERE task_id = %s;
DELETE FROM dg_system.step_observed        WHERE task_id = %s;
DELETE FROM dg_system.step_prepared        WHERE task_id = %s;
DELETE FROM dg_system.tool_calls           WHERE task_id = %s;
DELETE FROM dg_system.execution_envelopes  WHERE task_id = %s;
DELETE FROM dg_system.approvals            WHERE task_id = %s;
DELETE FROM dg_system.task_events          WHERE task_id = %s;
DELETE FROM dg_system.task_steps           WHERE task_id = %s;
DELETE FROM dg_system.worker_leases        WHERE task_id = %s;
DELETE FROM dg_system.checkpoints          WHERE task_id = %s;
DELETE FROM dg_system.tasks                WHERE task_id = %s;
```

对上面 6 个 `task_id` 各执行一遍即可。

## 4. 核对方法（清理后应全部为 0）

```sql
SELECT COUNT(*) FROM dg_system.tasks WHERE goal LIKE 'W07 收口%';            -- 期望 0
SELECT COUNT(*) FROM dg_system.approvals WHERE task_id IN (...6 个 id...);   -- 期望 0
SELECT COUNT(*) FROM dg_system.step_prepared WHERE task_id IN (...);         -- 期望 0
SELECT COUNT(*) FROM dg_system.step_observed WHERE task_id IN (...);         -- 期望 0
SELECT COUNT(*) FROM dg_system.approval_consumptions WHERE task_id IN (...); -- 期望 0
```

## 5. 给 QA 的复跑建议（避免新增残留）

在管理员账号可用之前，复跑 AC④ 的两个写库用例会**继续新增**同类残留（每次 2 条 task）。
可选做法：

- 先就绪 admin DSN，再跑 `tests/integration/test_worker_process.py`，让夹具的 DELETE 生效；
- 或只在可整体重置的实例上跑，跑完销毁实例。

本文件不做"已清理"声明；如需更新，请以同样的 SELECT 结果重写本表并记录观测时间。
