# DBGuardian

PostgreSQL 16 + MySQL 8 双引擎企业级数据库诊断、受控沙箱修复与两级人工确认自进化 Agent。

## 核心企业级能力总览（W01–W12 + Enterprise P1–P6）

- **安全与执行底座（W01–W09）**：15 状态机、追加式哈希链审计与证据链、RBAC 与租户隔离、一次性审批消费（摘要绑定 + 原子单次消费）、独立 Worker 载体与 `ToolGateway`（租约 / fencing / pre-flight 预检 / 爆炸半径拦截）、全链路 `RedactionGate` 脱敏防线、崩溃恢复与核对扫描。
- **企业 BYOK 模型平台（P1）**：`/v1/model-providers` 租户级多模型接入、Fernet 加密存储与掩码回显、SSRF 防护、Token/调用配额、三档结构化协议自适应降级（`json_schema → json_object → prompt_json`）与流式 SSE 墙钟超时中断。
- **双引擎与受控修复扩展（P2）**：MySQL 8 + PostgreSQL 16（含 `pg_stat_statements`）双引擎只读诊断、3 个受控 R2 修复动作与补偿闭环、外部基准转换契约（`DiagDBench` / `D-Bot` / `BIRD-CRITIC-1` / `BenchBase`）与 `scripts/import_external_corpus.py` 离线导入。
- **两级不同人确认知识自我进化 + 赛博朋克调度台（P3）**：3-Gate 两级人工确认（机器预检 + 阶段 1/2 强制不同主体确认 + 摘要防篡改 + 一键撤销）、LLM Wiki 编译（草稿恒 `PROPOSED` 必经脱敏）+ 8 道硬过滤 Hybrid 检索注入 `AgentLoop`、单页控制台 `/console`（径向弧形工作流可视化、长按边框进度确认/删除与微震反馈、太阳/月亮圆心扩散双主题切换）。
- **企业基础设施（P4）**：OIDC / SSO（RS256 + JWKS 缓存 + 角色只认控制库 DB）、S3 / MinIO Object Lock 合规 WORM 归档（`S3ObjectLockArchiveSink` + `docker-compose.yml` `worm` profile + `scripts/audit_verify.py`）、无外部依赖 Prometheus `/metrics` 导出与进程内脱敏 Span。
- **评测中台与金标回流（P5）**：`/v1/eval` 持久化评测记录与候选同向对比（含 `/v1/eval/runs/execute` 控制台计划触发）、`core/benchmark/golden.py` 脱敏去重切分与二次复检。
- **容器网络真隔离与多 Worker 压测驱动（P6）**：迁移 `017` 建立只追加只读派发队列（`DG_AGENT_EXECUTION_MODE=remote_worker`），`docker-compose.yml` 中 `api` 已彻底移除 `lab_net`；实机完成 `dbguardian-api` / `dbguardian-worker` 镜像构建（非 root `uid=10001`）、跨网段隔离实测（`scripts/verify_docker_runtime.ps1`：`lab_net` 可达 `pg_lab:5432`，`control_net` 与 `model_net` 均 `BLOCKED`）及多 Worker 故障转移驱动（`scripts/run_soak.py`）。

## 当前阶段状态

| 阶段 | 主题 | 状态 |
| --- | --- | --- |
| W01–W12 | 安全控制底座、双引擎、脱敏、Benchmark、安全回归与受限发布 | 已入库；发布档位 `TECH_PREVIEW`（详见 [docs/w12/known-limitations.json](docs/w12/known-limitations.json)） |
| P1 (T1/T2 + BYOK) | 真实 LLM 接入、AGENT 基线、租户 BYOK 供应商 + SSRF + 配额 + 协议降级 | 已完成并入库 |
| P2 (T1/T1B/T2/T3/T4) | PG16 解堵、外部基准契约/离线导入、W11 备份与负集补齐、pre-flight 与 R2 扩展 | 已完成并入库 |
| P3 (T1/T2/T3) | 3-Gate 两级人工确认、LLM Wiki + Hybrid RAG 接线、赛博朋克 `/console` 控制台 | 已完成 |
| P4 (T1/T2/T3) | OIDC/SSO、S3/MinIO Object Lock WORM 归档、Prometheus `/metrics` + Span | 已完成 |
| P5 (T1/T2) | 评测中台（含控制台触发）与金标数据集导出 | 已完成 |
| P6 (T1/T2) | API→Worker 只读派发队列、移除 `api` 的 `lab_net`、实机镜像构建与跨网隔离验证、多 Worker 压测驱动 | 代码、镜像构建、跨网隔离负测与短时故障转移压测已通过；3600s 完整长稳需按需在独立环境执行 `scripts/run_soak.py` |

- 运行时：**Python 3.14.7**（容器镜像 `python:3.14-slim-bookworm` + `uv:0.6.14`），依赖锁 `requirements.lock.txt`，数据库迁移共 **17 支（`001`–`017`）**，OpenAPI **3.1.0（46 paths）**。
- 数据库：**MySQL 8.0.46** 与 **PostgreSQL 16.4**（`pg_stat_statements` 已启用）。
- 控制台入口：启动 API 后访问 `http://127.0.0.1:8000/console/`（启用 BYOK 供应商管理请在 `.env` 配置 `DG_SECRET_MASTER_KEY`）。

## 工程结构

```
apps/api/             FastAPI 入口与路由
apps/web/             前端（后续周次）
core/                 配置、错误、日志、任务、工具、审计、模型/Agent（1.1 资产）
adapters/             PostgreSQL/MySQL 适配层（W02）
workers/              Worker（W05）
database/             MySQL/PostgreSQL 连接、角色、schema 与迁移
deploy/               Compose 镜像、网络拓扑、环境 Manifest、PG LAB 数据目录
benchmarks/ datasets/ 评测与数据集（W10）
docs/                 ADR、开发规范、环境指纹 Schema
scripts/              启动、初始化、迁移、CI 检查
tests/                单元 / 集成测试
```

## 快速启动

前置：MySQL 8 服务已运行；PostgreSQL 12 二进制可用（本机路径见 ADR-0003）。

```powershell
# 1. 依赖（Python 3.14.7）
uv venv .venv --python 3.14
uv pip install -e ".[dev]" --python .venv\Scripts\python.exe

# 2. 配置（.env 已被 gitignore）
Copy-Item .env.example .env
# 填写 DG_ADMIN_DSN / DG_RO_PASSWORD / DG_RW_PASSWORD / DG_CONTROL_PASSWORD
# 以及 DG_PG_ADMIN_DSN / DG_PG_RO_PASSWORD / DG_PG_RW_PASSWORD

# 3. 一条命令检查并初始化双 LAB（幂等）
powershell -ExecutionPolicy Bypass -File scripts/start_lab.ps1

# 4. 启动 API
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/healthz/db     # MySQL 只读身份
Invoke-RestMethod http://127.0.0.1:8000/healthz/pg     # PostgreSQL 只读身份
```

Docker 方式（本机 daemon 不可用时只能做静态校验）：

```powershell
docker compose config
docker compose up -d control_db mysql_lab pg_lab api
```

## 双引擎权限模型

| 账号 | 权限 | 用途 |
| --- | --- | --- |
| `dg_ro` | 仅 `SELECT ON dg_lab.*`、`SELECT ON performance_schema.*`、`PROCESS ON *.*` | MySQL 只读诊断 |
| `dg_rw` | 仅 `dg_lab.*` 的受控 DML/DDL | MySQL 沙箱故障注入/修复 |
| `dg_ctl` | 仅 `dg_system.*` 的读写 | 控制库任务/事件/证据/审计 |
| `dg_ro_pg` | 仅 `SELECT`（PG 12 LAB 已验证写入被拒） | PostgreSQL 只读诊断 |
| `dg_rw_pg` | 仅隔离 LAB 内的受控写（W07 起使用） | PostgreSQL 沙箱修复 |

PG 12 无 `SET SESSION TRANSACTION READ ONLY` 等 MySQL 等价能力，只读由账号权限与应用层只读校验共同保证；PG 16 LAB 引入后需重新探测能力。

## 配置与脱敏

- 环境变量前缀 `DG_`，DSN 使用 `SecretStr`；日志/异常递归脱敏，不输出 DSN 密码。
- `.env` 永不进入 Git；`.env.example` 只含 `CHANGE_ME` 占位。
- 统一错误码见 `core/errors.py`；API 输出 `{status, error:{code,message,details}}`。
- 控制库凭据与目标库凭据分离；应用运行时只使用 `dg_ctl`，管理员凭据仅初始化脚本使用。

## 测试

```powershell
# 全量（需 MySQL + PostgreSQL LAB；本机 Python 3.14.7 使用 --assert=plain）
.\.venv\Scripts\python.exe -m pytest -q --assert=plain

# 本地 CI（编译、单测、Compose 静态校验、密钥扫描）
powershell -ExecutionPolicy Bypass -File scripts/ci_check.ps1
```

W01–W05 全量基线（2026-11-08，Python 3.14.7，`--assert=plain`）：

```
654 passed, 1 warning
```

覆盖：配置/日志/错误、MySQL 只读与沙箱权限、MySQL 观察工具、任务状态机/Executor/审计、PostgreSQL DSN/连接/只读权限/能力探测、双引擎健康检查、环境 Manifest Schema、Agent Loop 与 API 端到端、W05 本地认证/RBAC/策略/租约/执行信封/Tool Gateway 与静态隔离负测。W05 专项共 146 项（含参数化），详见 [docs/security/w05-security-tests.md](docs/security/w05-security-tests.md)；独立验收将复核该基线。

## W02 双引擎工具合同与适配器

- 统一 `ToolStatus`：`success / partial / error / timeout / denied / not_supported / unknown_outcome`；`ToolResult` 增加 `observed_at`、`truncated`、`capability_ref`、`source_metadata`。
- 双引擎各 7 个固定只读工具：`get_connection_snapshot`、`get_query_stats`、`get_lock_wait_edges`、`get_lock_details`、`get_transaction_metadata`、`explain_query`、`get_table_stats`。
- MySQL：`adapters/mysql/adapter.py`，复用原生 SQL 观察工具，额外实现 `information_schema.innodb_trx` 事务元数据。
- PostgreSQL：`adapters/postgres_mcp/`，最小 MCP JSON-RPC stdio Server/Client；PG12 无 `pg_stat_statements` 时返回 `NOT_SUPPORTED` + warning，不伪造数据。
- `adapters/registry.py` 把固定工具接入 `ToolRegistry`，`apps/api/routes/adapters.py` 提供 `GET/POST /adapters/{engine}/tools`，实际经 `ToolExecutor` 执行。
- 能力差异、截断/超时/行数约束见 [docs/capability-matrix.md](docs/capability-matrix.md)。

### W02 验收记录

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 每引擎 ≥6 个固定只读工具、版本/Schema 可追踪 | 通过 | 两引擎各 7 个；`tests/unit/test_capability_matrix.py` |
| 能力状态五态与不伪造 | 通过 | PG12 `pg_stat_statements` → NOT_SUPPORTED；MySQL capability probe → AVAILABLE/PARTIAL/DENIED |
| 超时/行数/权限/错误语义；截断不静默 | 通过 | 双引擎 `max_rows=limit+1`，截断降级 PARTIAL + warning；`test_adapter_registry.py` |
| 只读账号不能写、不能任意 SQL/host/port | 通过 | 双引擎写操作被拒；EXPLAIN 仅单条 SELECT/WITH，禁 ANALYZE/多语句 |
| 统一 DatabaseAdapter 与 Registry/Executor 接线 | 通过 | `PostgresMcpAdapter`、`MySQLAdapter`、`build_adapter_registry`、`/adapters` API |
| 越界负测 | 通过 | 未知工具、非法参数、非法表名、UPDATE/ANALYZE、多语句均拒绝 |

已知限制：PG12 无 `pg_stat_statements`（PG16 待 Compose 验证）；>500 行真实事务样本未构造，截断语义由受控路径验证；适配器 API 的认证/租户隔离于 W05 接入（本记录保留 W02 当时状态）。

### W03 验收记录

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 租户/环境/成员/证据表、复合租户外键、迁移版本与回滚 | 通过 | `002_v22_evidence.sql`、`003`、`004`、`docs/migrations.md`、rollback 脚本 |
| 15 状态、非法转换拒绝、PAUSE/CANCEL 语义 | 通过 | `test_state_machine.py`；取消事件顺序 `TASK_CANCEL_REQUESTED → TASK_CANCELLED` |
| 事件 seq 唯一、哈希链、只追加、游标 | 通过 | `test_task_evidence_chain.py`、`test_w03_integrity.py`；`dg_ctl` 无事件/证据 UPDATE/DELETE |
| state_version 乐观并发 | 通过 | 旧版本 transition/resume 返回 `STATE_CONFLICT`，不静默覆盖 |
| Evidence 字段与归属校验 | 通过 | `test_evidence_service.py`；跨租户/任务/环境与 ReportFacts 引用拒绝 |
| `/v1` API、统一错误、trace_id、cursor、OpenAPI 3.1 | 通过 | `test_api_v1.py`；base64url 游标、400 非法游标、`X-Trace-Id` |
| 历史事件链修复 | 通过 | `scripts/repair_event_chain.py`；42/42 旧事件重算一致 |

已知限制：`/v1/tasks` 游标为 `created_at + task_id` keyset；证据恢复时效默认 3600 秒，可通过 `DG_RESUME_EVIDENCE_MAX_AGE_S` 调整；迁移非逐语句幂等，半途失败需人工修复。

### W04 验收记录

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 6 个 Fault Manifest、注入/重置、哈希覆盖运行参数 | 通过 | `benchmarks/faults/manifests/`、`injector.py`、`compute_fault_script_hash` |
| PG/MySQL 三场景只读诊断可重放 | 通过 | `test_fault_injection.py`、`test_diagnosis_flows.py`；能力缺失保留 PARTIAL/NOT_SUPPORTED |
| Hypothesis 支持/反证/缺失/置信度/适用范围 | 通过 | `core/diagnosis/models.py`、`analyzers.py`；证据 ID 可追溯 |
| Runner 经 Executor 写 tool_calls 与审计事件 | 通过 | `test_runner_writes_tool_calls_and_audit_events`（tool_calls=4，事件与 evidence 关联） |
| Oracle 真值不进入 Agent/Evidence/日志/任务目标 | 通过 | 独立验收 grep + 实跑检索；Oracle 仅存 `benchmarks/faults/oracle/` |
| 最小真值断言可执行 | 通过 | `benchmarks/faults/assertions.py`；6 个 Manifest 断言注册与空观测 fail closed |
| Baseline v0 冻结、无训练/无伪造指标 | 通过（模型环境缺失） | `benchmarks/baseline_v0/`；`status=BLOCKED_MODEL_ENV_MISSING`、`scores=null`、dry-run 不联网 |

已知限制：PG12 无 `pg_stat_statements`，慢查询以 EXPLAIN 为主；`scenario`/`verification_assertions` 尚未纳入资产哈希；`verify_assertions` 尚未接入 `fault_lab verify` CLI（P2）。

### W05 验收记录

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 本地认证与 Principal（dev_local / token、角色只来自控制库、客户端不可覆盖） | 本地实测通过（待独立验收） | `core/auth/service.py`、`apps/api/security.py`、`tests/unit/test_api_principal.py`、`tests/integration/test_auth_service.py`、`test_auth_api.py` |
| RBAC、租户与对象级授权（跨租户 404、任务控制需所有者或 Administrator） | 本地实测通过（待独立验收） | `core/security/roles.py`、`authz.py`、`tests/unit/test_authz.py`、`tests/integration/test_rbac_api.py` |
| Risk Resolver / Policy Engine（风险下限、模型提示只升不降、缺策略 fail closed） | 本地实测通过（待独立验收） | `core/security/policy.py`、`tests/unit/test_policy_engine.py`、`test_rbac_api.py::test_r2_tool_call_is_denied_by_policy` |
| Worker lease / fencing / ExecutionEnvelope / Tool Gateway | 本地实测通过（待独立验收） | `core/gateway/{leases,envelope,tool_gateway}.py`、`tests/unit/test_envelope.py`、`tests/integration/test_worker_gateway.py` |
| 唯一执行链与安全负测（Worker 无 DB/适配层导入、模型不接 lab_net、nonce 重放/旧租约/参数篡改被拒） | 本地实测通过（待独立验收） | `core/worker/worker.py`、`tests/unit/test_worker_isolation.py`、`test_model_isolation.py`、`tests/integration/test_worker_gateway.py` |
| R2 保持关闭（拒绝且 `required_approval=true`，等待 W06 审批闭环） | 本地实测通过（待独立验收） | `policy.py`（`R2_REQUIRES_APPROVAL_W06_CLOSED`）、`tests/unit/test_policy_engine.py`、`tests/integration/test_rbac_api.py` |

已知限制：

- 本机 Docker daemon 不可用，网络拓扑与容器运行态为 `NOT_VERIFIED`；仅执行静态隔离负测与 `docker compose config` 检查。
- W05 的 Tool Gateway 目前为进程内组件（当前由集成测试装配），API 运行时仍使用带 PolicyEngine 守卫的 legacy 执行路径；独立 Worker 容器与网络切分留待后续部署验证。
- `docker-compose.yml` 中 `api` 仍保留 `lab_net` 过渡挂载，尚未移除并做运行态复测。
- 审批事务与 WORM 审计留待 W06；R2 写操作在 W06 审批闭环完成前保持关闭。
- 租约/fencing 提供“每任务至多一个 ACTIVE 租约”与单调 token，不声称 Exactly Once 或分布式事务。

## W01 验收记录

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 仓库目录、ADR、贡献/开发规范、锁文件、`.env.example` | 通过 | `docs/adr/`、`CONTRIBUTING.md`、`docs/dev-guidelines.md` |
| 一条命令启动/初始化双 LAB | 本机通过（MySQL 服务 + PG 55432） | `scripts/start_lab.ps1` 实测退出码 0；控制库与 PG schema 幂等初始化 |
| 双引擎独立账号与只读权限 | 通过 | `tests/integration/test_db_permissions.py`（MySQL 11 项）、`test_pg_connection.py`（PG 6 项） |
| 控制库与目标库凭据分离 | 通过 | `dg_ctl` 仅 `dg_system`；`tests/integration/test_executor.py` 改用控制账号 |
| Compose + 网络隔离运行验证 | **因环境缺失无法验证** | 本机 Docker daemon 不可用；`docker compose config` 静态校验通过，连通性负测未执行 |
| 环境版本/digest/迁移/配置版本记录 | 部分通过 | Manifest 含版本、迁移版本、配置版本；镜像 digest 为 `NOT_VERIFIED` |
| CI 可重复运行 | 本地通过；远程未触发 | `scripts/ci_check.ps1` 全绿；`.github/workflows/ci.yml` 已定义 |

已知限制：

- Docker Desktop 管道不存在，容器健康检查、网络负测、重建/数据卷复现均未验证。
- 本机 PostgreSQL 12.18 已 EOL，仅作开发替身；Compose 候选 16.4 镜像 digest 未记录。
- W01 暂无 Tool Gateway；API 暂接 `lab_net` 为过渡态，W05 前必须移除并改为唯一 Gateway。
- 旧 1.1 的 W01–W03 代码（MySQL 观察工具、Executor、任务表）可复用；旧 W04 Model Gateway/Planner/Agent Loop 已提交为 `1604aa6`，不纳入 V2.2 验收。
