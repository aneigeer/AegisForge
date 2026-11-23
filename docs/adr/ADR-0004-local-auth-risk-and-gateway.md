# ADR-0004 本地认证、集中策略与 Tool Gateway

- 状态：Accepted（本机开发；待独立验收）
- 日期：2026-11-08
- 相关：V2.2 W05

## 背景

W05 要求建立身份认证与策略底座：Principal 只能由服务端构造；RBAC、租户与对象级授权必须可验证；风险由服务端解析；工具执行必须有唯一入口；W05 不开放写操作。Alpha 阶段没有外部 IdP、审批系统与独立 Worker 容器，且本机 Docker daemon 不可用。

## 决策

1. Alpha 使用**本地账户 + 不透明随机 Token**（`dg_` + `token_urlsafe(32)`），不采用 JWT。数据库只保存 Token 的 SHA-256 摘要；支持过期、登出撤销与刷新轮换（旧 Token 立即失效）。
2. 密码使用标准库 **scrypt**：`n=2^14`、`r=8`、`p=1`、`dklen=32`、16 字节随机盐，校验用 `hmac.compare_digest`；不存储明文或弱摘要。`bootstrap_auth.py` 不提供默认密码。
3. 认证模式 `dev_local` 仅用于非生产；生产必须使用 `token`，且配置校验强制要求 `DG_GATEWAY_SIGNING_KEY`。客户端提交的 `role`/`tenant` 不参与授权。
4. **Risk Resolver 与 Policy Engine 集中于服务端**：风险取工具契约、环境事实、操作类别与副作用的下限最大值；模型风险提示只升不降；`PROD` 一律拒绝；缺策略或未知类别 fail closed；策略版本 `w05-v1`。
5. **Tool Gateway + 租约/fencing 为唯一 Worker 执行入口**：Worker 只持有签名密钥与 `LeasePort`/`GatewayPort`，不导入数据库或适配层；Gateway 负责验签、有效期、nonce、租约、权威任务状态、工具版本、参数摘要与策略判定后，才调用 `ToolExecutor`。
6. **R2 写操作保持关闭**：即使具备 `adapter:invoke`，R2 决策仍为拒绝且 `required_approval=true`，等待 W06 接入审批事务后再开放。

## 备选方案与取舍

- **JWT / 无状态会话**：可减少数据库查询，但撤销、轮换、租户/角色变更与强制下线需要额外黑名单或版本机制，且 Token 体积与泄露风险更高。选择不透明 Token + `auth_sessions`，以换取即时撤销与可审计。
- **Argon2id**：抗 GPU 能力更强，但需要新增第三方依赖与构建约束；Alpha 阶段用标准库 scrypt 满足要求，并通过固定参数与存储格式约束避免弱化。
- **审批前开放 R2 写操作**：实现更快，但违反 W05 风险闭锁与职责分离，且没有审批事务/WORM 审计时无法证明可追溯性。选择拒绝并保留 `required_approval=true`。
- **Worker 直连控制库/目标库**：链路短，但扩大凭据暴露面，绕过集中策略与租约 fencing，无法形成单一执行入口。选择 Worker 无 DB、经 Gateway 执行。
- **独立 Gateway/Worker 容器**：更接近生产拓扑，但当前 Docker 不可用，先以进程内组件与端口协议落地，容器化留待部署验证。

## 后果

- 每次 Token 认证需要查询控制库；控制库不可用时认证、策略、租约与执行均 fail closed。
- 所有 Worker 工具调用必须经过 Gateway，增加一次信封签名/验签与租约校验开销，但换来统一策略、审计与重放防护。
- 角色矩阵显式分离系统管理与审批：`Administrator` 没有 `approval:decide`，也没有目标库直写权限。
- 租约只保证“每任务至多一个 ACTIVE 租约 + 单调 fencing token”，不声称 Exactly Once 或分布式事务。
- 进程内 Gateway 与 API legacy 工具路由并存；legacy 路由已加 PolicyEngine 守卫，但不是完整的租约/信封执行链。

## 验证方式

- 单元测试：`test_auth_passwords.py`、`test_auth_tokens.py`、`test_api_principal.py`、`test_authz.py`、`test_policy_engine.py`、`test_envelope.py`、`test_worker_isolation.py`、`test_model_isolation.py`。
- 集成测试：`test_auth_service.py`、`test_auth_api.py`、`test_rbac_api.py`、`test_worker_gateway.py`。
- 全量基线：`654 passed, 1 warning`（Python 3.14.7，`--assert=plain`，临时启动 PostgreSQL LAB）；W05 专项 146 项。详见 [docs/security/w05-security-tests.md](../security/w05-security-tests.md)。
- 静态检查：`docker compose config`；Worker/模型包的禁用依赖扫描；`model_service` 网络声明断言。

## 已知限制与未完成项

- Docker daemon 不可用，容器网络拓扑与运行态 `NOT_VERIFIED`；模型服务实际不可达 `lab_net` 的容器级证据尚未采集。
- `api` 在 Compose 中仍保留 `lab_net` 过渡挂载，尚未移除并复测。
- Tool Gateway 与租约服务当前为进程内装配，独立 Worker 容器/独立 Gateway 进程未部署。
- 审批事务、审批绑定与 WORM 审计留待 W06；R2 写操作继续保持关闭。
- `users.auth_version` 已落库但尚未在 Token 认证路径中比对，当前吊销依赖 `auth_sessions.status`。
- Tool Gateway 构造环境事实时固定 `tier=LAB`；PROD 规则由单元测试覆盖，运行时 PROD 环境读取尚未接入。
