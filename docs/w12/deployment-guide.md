# W12 部署指南（T03 / AC-10 / AC-11）

> 本文件是 **W12 对外材料**：只描述**既有**部署件与**实测到的状态**，不引入新部署编排、
> 不改 `deploy/**` / `docker-compose.yml` / `.env.example` / `scripts/**`。
> 口径：`docs/w12/02-design.md` §9.1、`docs/w12/03-task-cards.md` T03、
> `docs/w10-w12-lead-rulings.md` §4（W12-Q2/Q3）。
>
> **状态口径**：配置层（文件/清单/口令占位）已逐项实测；**运行态**（容器 build/up/down、
> 升级演练、回滚）本轮**未执行**，一律 `NOT_EXECUTED` + 原因，**不判 FAIL**；
> 网络级隔离只有账号级证据（KD-1），**不**声称网络隔离已通过运行态验证（`ENV-NV1`）。

## 1. 拓扑与命名（KD-2 重点）

`docker-compose.yml`（`name: dbguardian`）的服务与镜像 tag：

| 服务 | 镜像 / 构建 | 网络 | 说明 |
|---|---|---|---|
| `control_db` | `mysql:8.0.46` | `control_net` | 控制库 `dg_system`（任务/审批/审计/证据） |
| `mysql_lab` | `mysql:8.0.46` | `lab_net` | MySQL LAB `dg_lab`（唯一写目标） |
| `pg_lab` | `postgres:16.4` | `lab_net` | PostgreSQL LAB `dg_lab_pg`（**只读**第二引擎） |
| `api` | `deploy/Dockerfile.api` | `control_net` + `lab_net` | FastAPI 控制面（过渡态仍接 `lab_net`，见 `docker-compose.yml:109-111`） |
| `worker` | `deploy/Dockerfile.worker` | `control_net` + `lab_net` | 独立 Worker 进程外壳（唯一执行链），`DG_MODEL_PROVIDER=disabled` |
| `tool_gateway` | `python:3.14-slim` + 打印占位 | `control_net` + `lab_net` | **遗留占位**（`profiles: ["future"]`，`docker-compose.yml:155-162`） |
| `model_service` | `ollama/ollama:0.5.13` | `model_net` | 可选（`profiles: ["model"]`），Worker **不接入** `model_net` |

**KD-2 命名核对（必须按此理解：网关不是独立容器，也不存在 `api` 到网关容器的调用链）**

- Tool Gateway **不是独立容器**：`core/gateway/tool_gateway.py::ToolGateway` 由
  `workers/stack.py::build_worker_stack()` 与 Worker **同进程装配**（执行链唯一入口在进程内）；
- `docker-compose.yml` 里的 `tool_gateway` 服务是 W01 遗留**占位**（`profiles: ["future"]`，
  命令只是打印 `tool_gateway placeholder; W05 implemented`），不代表运行期存在网关容器；
- 因此"验签/策略/审批消费/STEP_PREPARED 门禁"都发生在 **worker 进程内**，
  `DG_GATEWAY_SIGNING_KEY` 必须在 worker 侧显式注入（dev 默认是进程内随机值，跨容器验签必败）。

## 2. 网络隔离（配置层）

三条自定义网络（`docker-compose.yml:177-183`，均带显式 `name:`）：

| 网络 | 实际名 | 挂载服务 |
|---|---|---|
| `control_net` | `dbguardian_control_net` | `control_db`、`api`、`worker`（+ 占位 `tool_gateway`） |
| `lab_net` | `dbguardian_lab_net` | `mysql_lab`、`pg_lab`、`api`、`worker`（+ 占位 `tool_gateway`） |
| `model_net` | `dbguardian_model_net` | `model_service` |

- **Worker 绝不接入 `model_net`**（`docker-compose.yml:129-131` 注释与 `networks:` 段）：模型侧拿不到数据库凭据；
- `api` 处于**过渡态**（同时挂 `control_net` 与 `lab_net`），W05 引入 Tool Gateway 后应收敛为只留 `control_net`——这是**已登记的配置现状**，不是本轮改动项；
- 隔离的**账号级**依据见 `docs/w07/07-gate-review.md`（KD-1：账号级隔离已实测，网络级未验证）；
- **运行态网络隔离状态**：`container_runtime_status = NOT_VERIFIED`（`ENV-NV1`），容器运行时本轮未启动。

## 3. `.env.example` 与 Secret 注入

- `.env.example` 只放占位：全部口令/密钥/DSN 口令段均为 `CHANGE_ME_*`
  （`DG_RO_PASSWORD=CHANGE_ME_RO`、`DG_CONTROL_DSN=mysql://dg_ctl:CHANGE_ME_CONTROL@...` 等）；
  W12 采集器实测 `env_example_has_real_secret = False`（`core/release/deploy.py::env_example_has_real_secret`）。
- 真实值**只**来自运行环境的 Secret 注入（本机 `.env`、CI Secret 或 compose 的环境变量），
  **不得**写回仓库；`.env` 不入库。
- Worker 侧必须显式注入 `DG_GATEWAY_SIGNING_KEY`（compose 默认 `CHANGE_ME_WORKER_SIGNING_KEY`
  仅为占位，dev 下进程内随机值不能跨容器复用）。
- `DG_ADMIN_DSN` 属管理员账号，本机实测认证失败（`ENV-1045`）：**本轮不使用**该账号做任何清理或回滚。

## 4. 初始化与迁移

| 步骤 | 命令（既有脚本，本轮未改） | 实测状态 |
|---|---|---|
| 控制库 + MySQL LAB 引导 | `python scripts/bootstrap_mysql.py` | 配置层件在（`required_files` 逐项 True）；本轮未重跑 |
| PG LAB 引导 | `python scripts/bootstrap_pg_lab.py` | 件在；PG 运行态未验证（`ENV-PG5432`） |
| 环境事实清单 | `python scripts/environment_manifest.py` | 件在；`deploy/environment-manifest.example.json` 的 `image_digest="NOT_VERIFIED"` 保持原样 |
| 迁移执行 | `python scripts/migrate.py` | 件在；本轮**不执行**任何迁移 |
| 部署四段检查 | 见 `scripts/start_dev.ps1` / `start_lab.ps1` | 本轮未实跑 |

- 迁移版本权威：`database/migrations/*.sql` 实测 **11 个正向（001…011）+ 8 个回滚**；
  `docs/migrations.md` 的版本表**止于 007** → 文档完整性标 `PARTIAL`（`DOC-PARTIAL` 限制项），
  本轮**不改** `docs/migrations.md`。
- **W12 零 DDL、零迁移**：本条只读说明既有迁移，不新增、不改写、不执行回滚。
- `deploy/environment-manifest.mysql.json` / `.postgresql.json` 被仓库忽略策略排除，
  **不得**用 `git add -f` 强行入库（KD-4）。

## 5. 五项检查（AC-11，逐条如实登记）

采集函数：`core/release/deploy.py::collect_deploy_package(root)`；判定只有两档
`PASS` / `NOT_EXECUTED`（未执行必须给原因，**不判 FAIL**）。

| check_id | 状态 | 原因 |
|---|---|---|
| `install` | `NOT_EXECUTED` | `ENV-NV1`：本轮不实跑 `docker build` / 镜像安装（容器不作硬门槛，W12-Q3） |
| `start` | `NOT_EXECUTED` | `ENV-NV1`：本轮不实跑 `compose up`（运行态 `NOT_VERIFIED`） |
| `stop` | `NOT_EXECUTED` | `ENV-NV1`：本轮不实跑 `compose down`（无运行态可停） |
| `upgrade` | `NOT_EXECUTED` | `DG_ADMIN_DSN` 不可用（`ENV-1045`）：本轮不实跑升级演练 |
| `rollback` | `NOT_EXECUTED` | 本轮不执行 rollback（含 `DROP`，须人工 admin；W12 不执行回滚） |

**制品记录（禁止编造，W12-Q2）**

- `commit`：`git rev-parse HEAD` → 本轮实测 `2850eca32685…`（权威始终是命令输出，**不写死**）；
- `image_digest`：`UNKNOWN`（容器运行态未验证）→ 已登记 `W12-Q2/ENV-NV1`；
- `artifact_hash`：由 **11 项必填文件的内容**做 SHA-256（同一输入必得同一输出），
  任一件缺失即 `UNKNOWN` + 登记；
- `container_runtime_status`：`NOT_VERIFIED`；
- `nv1_is_hard_gate`：`False`（`ClassVar`，不是模型字段，无法被数据覆盖）。

## 6. 必填文件清单（AC-10，逐项 `exists is True`）

| 路径 | 本轮实测 |
|---|---|
| `deploy/Dockerfile.api` | 存在 |
| `deploy/Dockerfile.worker` | 存在 |
| `deploy/environment-manifest.example.json` | 存在 |
| `deploy/init/pg_lab_bootstrap.sh` | 存在 |
| `deploy/network/README.md` | 存在 |
| `docker-compose.yml` | 存在 |
| `.env.example` | 存在 |
| `scripts/migrate.py` | 存在 |
| `scripts/bootstrap_mysql.py` | 存在 |
| `scripts/bootstrap_pg_lab.py` | 存在 |
| `scripts/environment_manifest.py` | 存在 |

## 7. 复用与边界

- 演示与自检**只复用**既有接缝：`scripts/demo_agent_run.py`（Agent 多轮只读诊断）、
  `adapters/lab_assembly.py`（唯一写入口装配）、`workers/stack.py`（组合根）；
- **不新增** API 端点、Web 控制台、第三方依赖；
- 写动作仍只有 `mysql_lab_update_order_note` / `1.0`（LAB、R2、单行、需一次性审批）；
- 已知偏离见 `docs/w12/known-limitations.md`（含已关闭偏离 `KD-W12-01`（原 `FAILED`，2026-11-17 修复后 `PASSED`）
  与 `KD-W12-02`（原「全表扫描 + 无 `table_stats`」输入的 `UnboundLocalError`，2026-11-19 修复并补回归后关闭）；
  当前在册 3 条为 `KD-W11-01/02/03`）。
