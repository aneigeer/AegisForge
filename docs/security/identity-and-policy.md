# 身份认证与策略底座（W05）

- 适用：DBGuardian V2.2 W05
- 策略版本：`policy_version=w05-v1`（默认值，可由配置覆盖并原样返回）
- 代码：`core/auth/`、`core/security/`、`core/gateway/`、`core/worker/`、`apps/api/security.py`
- 迁移：`database/migrations/005_w05_identity_policy.sql`

本文只描述当前实现；未在运行态验证的项在文末明确标记。

## 1. 认证模式

配置项 `DG_AUTH_MODE` 的取值为 `dev_local` 或 `token`，默认 `dev_local`；`DG_ENV` 取值为 `dev`、`test`、`prod`。

| 模式 | 适用环境 | 身份来源 | 说明 |
| --- | --- | --- | --- |
| `dev_local` | 仅非生产 | 控制库固定本地主体 `user_local` | 服务端构造 Principal，完全忽略请求的 `Authorization` 头；租户与角色来自 `memberships` |
| `token` | 生产必须使用 | `Authorization: Bearer <token>` | 登录签发不透明会话 Token，服务端按 `auth_sessions` 解析 Principal |

- `Settings` 在 `env=prod` 时校验：`auth_mode` 必须为 `token`，且必须配置非空的 `DG_GATEWAY_SIGNING_KEY`；同时 `auth_token_ttl_s` 不得超过 `auth_token_max_ttl_s`。
- `AuthService.dev_principal()` 再次校验 `auth_mode == "dev_local"` 且 `env != "prod"`；否则抛 `ConfigError`。
- `dev_local` 主体不存在、已停用、无成员关系，或存在多个租户且未配置 `default_tenant_id` 时，均 fail closed。
- API 认证依赖只从服务端配置与凭据构造 Principal：缺失认证信息返回 `AUTH_REQUIRED`，Bearer 格式非法或 Token 无效返回 `AUTH_INVALID`，Token 过期返回 `AUTH_EXPIRED`。不读取 `X-Role`、`X-Tenant` 等请求头。

## 2. 密码

- 算法：标准库 `hashlib.scrypt`，固定 `n=2**14`（16384）、`r=8`、`p=1`、`dklen=32`。
- 盐：每次哈希生成 16 字节随机盐（`secrets.token_bytes`），同一密码两次哈希结果不同。
- 存储格式：`scrypt$16384$8$1$<salt_b64>$<digest_b64>`；参数、盐长度或摘要长度不符时严格判为非法。
- 校验：使用 `hmac.compare_digest` 恒定时间比较；哈希缺失、格式非法或参数不符时返回 `False`，并执行一次固定 dummy scrypt 计算以减小失败路径的时序差异。
- 明文密码不落库、不写日志、不在响应中回显。未使用明文、MD5/SHA 弱摘要或“盐+单次哈希”。
- `scripts/bootstrap_auth.py` 只从环境变量 `DG_AUTH_BOOTSTRAP_PASSWORD` 或交互式 `getpass` 读取密码；不提供默认密码，空密码直接退出。租户必须已存在，角色名严格解析，未知角色拒绝。

## 3. Token 与会话

- 生成：`dg_` 前缀 + `secrets.token_urlsafe(32)`。
- 落库：`auth_sessions.token_hash` 只保存原始 Token 的 SHA-256 十六进制摘要（64 字符），并有唯一键；仓储层不接触明文 Token。
- 有效期：登录与刷新均按 `DG_AUTH_TOKEN_TTL_S` 计算（默认 3600 秒），上限由 `DG_AUTH_TOKEN_MAX_TTL_S` 约束（默认 86400 秒）。
- 会话状态：`ACTIVE` / `REVOKED`。认证时校验会话存在、状态为 `ACTIVE`、未过期、用户仍为 `ACTIVE`，且用户仍属于会话租户；成功后更新 `last_used_at`。
- 登出：将 `ACTIVE` 会话置为 `REVOKED` 并写 `revoked_at`；重复登出返回未撤销，不报错。
- 刷新：条件撤销旧会话后插入新会话，新会话记录 `rotated_from`；旧 Token 立即失效，新 Token 与旧 Token 不同。
- `dev_local` 模式没有会话可撤销或轮换：无 Bearer 时登出返回 `logged_out=false`，刷新返回 `AUTH_REQUIRED`。
- 认证日志只记录 `user_id`、`session_id`；异常 `details` 只包含 `user_id`、`tenant_id`、`session_id` 等安全字段。`LoginResult.token` 的 `repr=False`，`token_fingerprint()` 只保留前 8 位。任何日志/响应不得输出完整 Token 或密码。

## 4. 角色权限矩阵

角色与权限在 `core/security/roles.py` 中静态定义；`Principal` 只由服务端构造，未显式提供权限时按角色集合计算，请求体中的 `role` 不参与授权。

| 角色 | 权限集合（精确值） |
| --- | --- |
| `Viewer` | `task:read`、`evidence:read`、`report:read`、`environment:read` |
| `Operator` | `task:read`、`task:create`、`task:control`、`task:run`、`evidence:read`、`report:read`、`report:write`、`adapter:read`、`adapter:invoke`、`approval:create`、`environment:read` |
| `Approver` | `task:read`、`evidence:read`、`report:read`、`approval:read`、`approval:decide`、`environment:read` |
| `Administrator` | `task:read`、`task:create`、`task:control`、`task:run`、`evidence:read`、`report:read`、`report:write`、`adapter:read`、`adapter:invoke`、`approval:create`、`approval:read`、`audit:read`、`environment:read`、`environment:manage`、`user:manage` |
| `ServiceWorker` | `task:read`、`worker:lease`、`gateway:execute` |
| `Auditor` | `task:read`、`evidence:read`、`report:read`、`audit:read`、`environment:read` |

边界说明：

- `Administrator` **没有** `approval:decide`，审批职责与系统管理职责分离。
- RBAC 不授予任何目标库直写权限。`Operator`/`Administrator` 的 `adapter:invoke` 只表示可请求工具执行；实际是否允许由 Policy Engine 判定，W05 的 R2 写操作一律拒绝。
- `ServiceWorker` 只用于 Gateway 内部执行链，只有任务读取、租约与 Gateway 执行权限，没有审批、管理或 `adapter:invoke`。
- 角色解析严格：未知角色拒绝；控制库中出现未知角色时跳过，不静默降权或提权。`Principal.roles` 不能为空。

## 5. 租户与对象级授权

- `tenant_id` 只来自服务端构造的 `Principal.tenant_id`。`/v1/tasks` 的请求 DTO 虽接收 `tenant_id` 兼容字段，但该字段不参与归属或授权；与 Principal 不一致时只记录告警，任务仍写入 Principal 租户。
- 创建任务时校验环境属于当前租户；任务归属写入 `tenant_id` 与 `created_by`。
- 跨租户任务在 API 加载时按 `tenant_id` 过滤，统一返回 `TASK_NOT_FOUND`（404），不泄露任务存在性；覆盖查询、事件、证据与暂停等入口。服务层 `ensure_tenant()` 的跨租户错误使用 403，作为内部第二道防线。
- 任务控制（暂停/恢复/取消）需要 `task:control` 权限，且必须是任务所有者或 `Administrator`；同租户非所有者返回 `FORBIDDEN`。
- `/v1` 创建任务 DTO 使用 `extra="forbid"`，客户端提交 `role`、`approver_id` 等未定义字段返回 422（`REQUEST_INVALID`）。
- `Principal` 模型 `frozen=True`、`extra="forbid"`，不携带任何凭据或 Token。

## 6. Risk Resolver 与 Policy Engine

### 6.1 风险解析

服务端风险解析器按以下顺序取最大值：

```
effective_risk = max(
    tool.risk_level,
    tool.risk_floor,
    操作类别下限,
    副作用下限,
    模型风险提示（仅可抬升）
)
```

- 操作类别下限：`observe → R0`、`diagnose → R0`、`experiment → R1`、`repair → R2`。
- 副作用下限：`none → R0`、`read → R0`、`write → R2`。
- `PROD` 环境：风险至少抬升到 `R2`，随后由 Policy Engine 一律拒绝。
- 模型风险提示：仅接受合法 `R0`–`R4`；低于解析风险的提示忽略并记入 `notes`，等于不改变，高于则抬升。非法提示忽略并记录。
- 未知 `operation_class` 或未知 `side_effect` 记入 `notes`，由 Policy Engine 直接拒绝，绝不按默认放行。

### 6.2 决策顺序

`PolicyEngine.decide()` 是唯一策略出口，输出 `PolicyDecision`（`allowed`、`effective_risk`、`reason`、`policy_version`、`required_approval`、`deny_code`、`risk`、`details`）。

1. 缺少工具策略定义：`R4` + `POLICY_MISSING`，拒绝。
2. 未知操作类别：`UNKNOWN_OPERATION_CLASS`，拒绝。
3. 未知副作用：`UNKNOWN_SIDE_EFFECT`，拒绝。
4. `PROD` 目标：`PROD_TARGET_NOT_ALLOWED`，一律拒绝。
5. 环境不在工具允许列表且不是 `*`：`ENVIRONMENT_NOT_ALLOWED`，拒绝。
6. `R4`：`R4_NOT_EXECUTABLE`，拒绝。
7. `R3`：`R3_AGENT_EXECUTION_FORBIDDEN`，拒绝。
8. `R2`：`R2_REQUIRES_APPROVAL_W06_CLOSED`，拒绝且 `required_approval=true`；W05 不执行写操作，等待 W06 审批闭环。
9. 缺少 `adapter:invoke`：`MISSING_ADAPTER_INVOKE`，拒绝。`Viewer`、`Approver`、`Auditor`、`ServiceWorker` 因此不能执行 R0 工具。
10. `R1`：必须为 `LAB`，且必须为 `Operator` 或 `Administrator`；否则 `R1_LAB_ONLY` / `R1_REQUIRES_OPERATOR_ROLE`，拒绝。
11. 其余情况（`R0`，以及在 LAB 下满足条件的 `R1`）允许，`reason=ALLOWED`。

### 6.3 版本化映射（`w05-v1`）

| 风险 | 典型来源 | W05 决策 |
| --- | --- | --- |
| `R0` | `observe`/`diagnose`，无副作用或只读 | 具备 `adapter:invoke` 且非 PROD 时允许 |
| `R1` | `experiment`，或显式 `risk_level=R1` | 仅 LAB + `Operator`/`Administrator` 允许 |
| `R2` | `repair`、`write` 副作用、`risk_floor=R2`、模型提示抬升 | 拒绝，`required_approval=true` |
| `R3` | 工具契约或模型提示 | 拒绝 |
| `R4` | 工具契约、模型提示、缺少策略定义 | 拒绝 |

`policy_version` 由 `DG_POLICY_VERSION` 或 `PolicyEngine(policy_version=...)` 提供，默认 `w05-v1`，并在审计与 API 错误 details 中原样返回。全部拒绝路径 fail closed。Tool Gateway 当前从任务构造 `EnvironmentContext` 时固定 `tier=LAB`；PROD 规则已实现并由单元测试覆盖，运行态 PROD 环境事实读取尚未接入。

## 7. 唯一执行链

Worker 侧只持有端口与签名密钥，不持有 DSN、不导入 `database`/`adapters`：

```
Worker（无 DB / 无适配层导入）
  ├─ LeasePort：acquire / heartbeat / release（部署时可替换为远程租约服务）
  └─ GatewayPort.execute(envelope, params)
       └─ ToolGateway.execute
            ├─ verify_envelope：HMAC-SHA256、issued/expires、时钟偏差、nonce 完整性
            ├─ execution_envelopes INSERT：nonce_hash 唯一，重放必然失败
            ├─ LeaseService.verify：lease_id / task_id / worker_id / fencing_token / 过期
            ├─ 任务读取（按租约租户过滤）+ state_version + environment_id 一致性
            ├─ 注册表工具存在 + tool_version + args_digest 一致性
            ├─ 服务端 Worker 主体与请求者主体构造；租户一致性校验
            ├─ PolicyEngine.decide（服务端环境事实 + 模型风险提示）
            └─ ToolExecutor.execute → ToolRegistry 工具 → Adapter / MCP
```

- Gateway 只使用控制库与执行器内部依赖，不接收 DSN。
- 允许执行前写 `POLICY_DECISION` 与 `GATEWAY_EXECUTION_ALLOWED` 审计事件；拒绝写 `GATEWAY_EXECUTION_DENIED`，不调用 Executor。
- API 进程内仍存在 legacy 工具路由（`/tasks/{id}/tools/...` 与 `/adapters/{engine}/tools/...`），它们不经过租约/信封，但在调用 `ToolExecutor` 前同样执行 `PolicyEngine.decide()` 守卫。W05 的 Gateway 为进程内组件，独立 Worker 容器与网络切分留待后续部署验证。

## 8. Lease 与 fencing

- `worker_leases` 中 `active_task_id` 为数据库生成列：`status='ACTIVE'` 时取 `task_id`，否则为 `NULL`；配合唯一键保证每个任务最多一个 `ACTIVE` 租约。
- 领取：若已有未过期 `ACTIVE` 租约，返回 `WORKER_CONFLICT`；过期租约先置为 `EXPIRED` 再插入新租约。并发插入命中唯一键（MySQL 1062）同样转为冲突。
- `fencing_token = 该任务历史 MAX(fencing_token) + 1`，跨过期/释放租约仍严格递增。
- 心跳：条件更新 `status='ACTIVE' AND worker_id=? AND fencing_token=? AND expires_at>now`；不满足则拒绝，Worker 续租失败后清空本地租约，不再提交新步骤。
- 释放：同样按 `status/worker_id/fencing_token` 条件更新。
- 校验：重新读取租约，校验任务、Worker、`ACTIVE` 状态与过期时间；`fencing_token` 低于历史最大值抛 `FENCING_TOKEN_STALE`，与当前活动租约不一致也拒绝，因而旧租约、伪造的更高 token 均不能通过。
- 边界：只提供“至多一个活动租约 + 单调 fencing token”的可验证事实，不声称 Exactly Once、分布式事务或跨库原子性。

## 9. ExecutionEnvelope

信封字段：

`envelope_id`、`task_id`、`step_id`、`worker_id`、`lease_id`、`fencing_token`、`state_version`、`correlation_id`、`actor_user_id`、`tool_name`、`tool_version`、`environment_id`、`args_digest`、`nonce`、`issued_at`、`expires_at`、`model_risk_hint`、`signature`。

- `args_digest`：对工具入参做 `sort_keys=True`、紧凑分隔符、UTF-8 原文的 SHA-256；键顺序无关，网关收到参数后重新计算并比对。
- 签名：覆盖除 `signature` 外的全部字段；JSON 键排序、紧凑分隔、datetime 统一为 UTC ISO-8601；`HMAC-SHA256(server_key, canonical_payload)` 十六进制签名，使用 `hmac.compare_digest` 校验。
- 签名密钥：`DG_GATEWAY_SIGNING_KEY`；生产环境必填，开发环境未配置时使用进程内临时密钥。
- 有效期：`issued_at` 不得超前当前时间 60 秒以上；`expires_at` 必须晚于当前时间，且 `issued_at < expires_at`。配置项 `DG_GATEWAY_ENVELOPE_TTL_S` 默认 60 秒、取值上限 600 秒；`Worker.submit` 默认传入 60 秒。网关按信封自身的 `issued_at`/`expires_at` 校验，不额外限制调用方传入的 TTL。
- 防重放：`nonce` 不得为空；Gateway 落库 `nonce_hash = SHA-256(nonce)`，`execution_envelopes.nonce_hash` 唯一，重复即 `NONCE_REPLAY`，拒绝继续执行。外键失败按租约无效拒绝；其他登记失败一律 fail closed。

## 10. 已知限制与未验证

- Docker daemon 不可用：容器网络拓扑与运行态 `NOT_VERIFIED`，仅做静态隔离负测与 `docker compose config` 检查。
- `api` 在 `docker-compose.yml` 中仍挂载 `lab_net` 过渡网络，未做 W05 后的网络切分复测。
- Tool Gateway 当前为进程内组件，尚未以独立容器/独立进程部署；Worker 的 `LeasePort` 也由同进程服务实现。
- `users.auth_version` 已落库用于后续全局吊销，但当前 Token 认证路径未将其与会话快照比对；现行吊销依赖 `auth_sessions.status`。
- 审批事务与 WORM 审计留待 W06；R2 写操作保持关闭。
- 不声称 Exactly Once；控制库不可用时相关认证/策略/租约路径 fail closed。
