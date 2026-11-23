# workers/ — 独立 Worker 进程（组合根）

本目录是 **W07 收口**的产物：把"已批准但无法执行"的修复链在**生产运行态**打通。

## 分层契约（不要混用）

| 层 | 目录 | 允许做的事 | 禁止做的事 |
|---|---|---|---|
| 领域层 | `core/worker/` | 持有签名密钥、`GatewayPort`/`LeasePort`，构造并签名执行信封 | import 数据库适配层、持有 DSN、裸 SQL |
| 组合根 | `workers/`（本目录） | 持有 DSN、import `adapters`、装配控制库/LAB 客户端与 `ToolGateway` | 绕过 `build_lab_stack()` 自行注册写工具；写裸 SQL |

反向守护：`tests/unit/test_worker_isolation.py` 扫描 `core/worker/` 的禁用串；
正向守护：`tests/unit/test_worker_layering.py` 断言本目录**必须**引用
`adapters/lab_assembly.py::build_lab_stack` 与 `core/gateway/tool_gateway.py::ToolGateway`。
两者任一被破坏都会让"唯一写入口"变成口头约定。

## 为什么 Gateway 与 Worker 同进程

`ToolGateway.execute()` 已经是执行链的唯一入口（验签 → nonce 防重放 → 租约/fencing
校验 → 策略决策 → 消费一次性审批 → `STEP_PREPARED` 门禁 → Executor）。跨进程再包
一层协议只会让"谁验签"变模糊，还会引入新的信任边界；`ToolGateway` 与
`GatewayPort` 的方法签名本来就一致，可以直接注入（见 `core/worker/client.py`）。

## 模块

| 文件 | 职责 |
|---|---|
| `stack.py` | `build_worker_stack()`：唯一装配点（控制库 → `build_lab_stack()` → `ToolGateway` → `Worker`），并暴露 `TaskService` 用于受控状态迁移 |
| `discovery.py` | `discover_candidates()` + 参数还原（`approval.step_id` → `task_steps.params_json`，失败即 fail closed） |
| `runner.py` | `WorkerRunner`：poll → **transition → 重读 state_version** → acquire lease → submit → release；不重试、不抢任务 |
| `heartbeat.py` | `LeaseHeartbeat`：daemon 线程续租，失败置 `lost=True` 并停止提交 |
| `cli.py` | `argparse` 入口 + 退出码 |

## 候选与状态迁移（rev.2，硬约束）

- **候选只取 `WAITING_APPROVAL`**：`ApprovalService.approve()` 完全不改任务状态（所有
  `transition` 都作用在 approvals 行上），所以"已批准待执行"的真实形态就是
  `WAITING_APPROVAL`。已经在 `EXECUTING` 的任务说明别人迁移过它（可能在途），不再当候选。
- **执行前必须先迁移**：`runner._ensure_executing()` 先 `WAITING_APPROVAL → EXECUTING`，
  再**重新读取**任务拿新的 `state_version`，最后才建信封。顺序反了必被 Gateway 第 5 步
  判 `StateConflictError`。
- **为什么不能省**：`ToolGateway` 只校验 `state_version`/`environment_id`，不看 status，
  所以"不迁移也能执行成功"；但 `_converge_unknown_outcome()` 用
  `can_transition(current.status, NEEDS_RECONCILIATION)` 做门禁，而状态机里
  `WAITING_APPROVAL` **没有**到 `NEEDS_RECONCILIATION` 的边（只有 `EXECUTING` 有）。
  漏了这步，一旦结果未知，任务会静默卡在 `WAITING_APPROVAL` 且永不被标记待核对。
- **审批与 `state_version` 解耦**：`consume()` 的九项校验不含 `state_version`，所以先
  涨版本再消费不会触发摘要不符。
- `pause_requested` / `cancel_requested` 只是标记、不改状态，候选与执行前各拦一次。

## 崩溃恢复边界（不重试、不补偿、不复核）

若进程在 "审批已 `CONSUMED`" 之后、"`step_observed` 写入" 之前崩溃，控制库会呈现：

- `approvals.status = CONSUMED`（终态，无出边）、
- `tasks.status = EXECUTING`、
- `step_prepared` 有行、`step_observed` 无行。

此时**发现阶段永远不会再选中它**（候选只查 `status=APPROVED` 的审批，已消费的审批
不会再出现），这正是 P0-18「禁止自动重试」要求的形态。Worker **不做补偿、不做重放、
不做状态回滚**；这条「已 Prepared 未 Observed」的落账由 **W08 Reconciler** 统一负责。

## 启动方式

```bash
# 本仓库必须使用 venv 内的 Python 3.14（PATH 里的 3.13 不满足 requires-python）
./.venv/Scripts/python.exe -m workers --help
./.venv/Scripts/python.exe -m workers --self-check      # 装配 + 连接自检，输出 JSON
./.venv/Scripts/python.exe -m workers --dry-run --once  # 只列候选，不执行
./.venv/Scripts/python.exe -m workers --once            # 处理一轮
./.venv/Scripts/python.exe -m workers                   # 常驻轮询
```

常驻模式会注册 SIGINT/SIGTERM → `runner.request_stop()`：**POSIX 下**收到信号后在
当前任务收敛后退出（退出码 0/1）。**本机 Windows 未验证真实信号路径**——Windows
没有 POSIX 信号，`Popen.terminate()` 走 `TerminateProcess`，只能验证到"handler 接线"
与"`request_stop()` 语义"两层（见下节 NOT_VERIFIED）。

退出码：`0` 干净退出；`1` 有在途未收敛（被他人持锁/拒绝/失败/结果未知）；`2` 配置或自检失败。

## 未验证项（NOT_VERIFIED）

以下四项**未完整**取得运行态证据（逐条以各条的「已实测 / 仍未验证」分量为准），不得在验收材料里**整体**表述为"已验证"：

| 编号 | 未验证项 | 硬原因 | 已做的替代验证 |
|---|---|---|---|
| NV-1 | Docker 镜像构建、`compose` 起栈、容器网络隔离**运行态** | ① **已实测通过**：Docker daemon **29.7.2** 在运行；`docker compose config` **exit 0**；worker 仅挂 `control_net` + `lab_net`、**不含** `model_net`、`DG_MODEL_PROVIDER=disabled`。<br>② **仍未验证**：镜像构建（`docker build -f deploy/Dockerfile.worker`）与容器起栈 / 网络隔离**运行态**（`docker ps -q` 为 **0 容器**）。<br>③ **定性**：**没跑过，不是跑不了**——静态装配可验证，运行态证据尚缺。 | `deploy/Dockerfile.worker` 静态断言（`CMD ["python","-m","workers"]`、`COPY workers/adapters`、`USER dguser`、uid 10001、无 EXPOSE）；`docker-compose.yml` 用 `docker compose config`（exit 0）+ PyYAML 解析 + 断言 worker 块只含 `control_net`/`lab_net`、**不含** `model_net`、`DG_MODEL_PROVIDER: disabled` |
| NV-2 | POSIX `SIGTERM`/`SIGINT` 优雅停机（真实信号路径） | Windows 无 POSIX 信号；`Popen.terminate()` 是 `TerminateProcess`（实测 rc=1、stdout 未 flush），不能代表 SIGTERM | `tests/unit/test_worker_runner.py::test_install_signal_handlers_forwards_to_request_stop`（handler 接线）；`::test_runner_request_stop_prevents_new_work`、`::test_run_forever_stops_after_request_stop`（停止语义） |
| NV-3 | PostgreSQL 引擎执行路径 | 本轮**刻意不接**：写工具只在 `LAB + mysql` 注册（`adapters/lab_assembly.py::is_lab_mysql_target`），Worker 侧不读 `DG_PG_*` | 无（属设计范围外，非缺陷）；PG 只读能力仍由既有 W01/W07 适配层测试覆盖 |
| NV-4 | `tests/integration/test_worker_gateway.py`（既有用例） | 本机 `DG_ADMIN_DSN`(root) 认证失败（MySQL 1045），其 autouse 清理夹具在 setup 阶段即报错 | 无（未声称通过）；该文件与本次改动无交集（gateway/leases/config 校验逻辑未改） |

补充：控制库只追加表（`task_events`/`approvals`/`approval_consumptions`/`step_prepared`/
`step_observed`/`worker_leases`/`execution_envelopes`）`dg_ctl` 无 DELETE 权限，本机
admin 账号不可用，因此集成测试留下的 task 行无法清理；清单见 `docs/w07/worker-residue.md`。

## 运维注意

- **绝不能跑两个 Worker 抢同一任务**：租约与 fencing token 会兜住（第二个必然
  `WORKER_CONFLICT`），但那是兜底不是设计意图；
- **拆容器时 `DG_GATEWAY_SIGNING_KEY` 必须显式配置**：dev 下同一个进程内的
  Worker 与 Gateway 共享临时随机密钥，跨进程/跨重启会验签失败；
- 日志不含参数原文、DSN、token（`core/logging.py` 统一脱敏）。
