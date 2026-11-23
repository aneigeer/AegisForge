# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [3.0.0] - 2026-11-23

多智能体协作（Multi-Agent SecOps）与多业务域插件化架构。

### Added

- 业务域插件 SDK：`DomainProbe` / `DomainMasker` / `DomainSandboxHarness` / `DomainRollbackExecutor` 四大契约，装配期完整性校验 fail closed。
- 确定性 StateGraph 状态机：节点预算、节点访问上限、`max_self_correction_rounds = 3` 图级别硬熔断、HITL 挂起快照与精确续跑。
- 五类核心 Agent：总控调度、研判定级、领域专家（DB / K8s / Host / AppSec）、Critic 红队审查、自主进化蒸馏。
- Critic 对抗自愈闭环：沙箱打回 -> 结构化失败码 -> 专家自愈重提交 -> 放行/超限转人工，超限事件写入 WORM。
- 多模态隐私网关：SQL AST / K8s YAML / 源码与 HTTP / 自由文本按格式路由，复用 Fernet 信封加密映射与结构校验。
- 业务域插件：database（复用 M02~M06）、k8s（Manifest 审计 + vCluster 沙箱 + 声明式回退）、host（基线 + MicroVM + 逆向 Playbook）、appsec（SAST/SCA + 代码 Patch + 联合 DB 加固）。
- 统一沙箱调度器：瞬时容器 / 温备池 / vCluster / MicroVM / 流量回放，配额隔离与 TTL 强制回收。
- 跨领域共享记忆库：租户隔离、跨域检索、入库拒密、资产拓扑与变更窗口沉淀。
- 跨栈 SARIF v2.1.0：同一报告内同时承载 `db://` 与 `file://#L` 位置。
- 报告 API：`POST /v1/secops/sarif`（只读渲染、不落盘、拒绝 `evidence` 字段）与 `GET /v1/secops/plugins`（插件能力矩阵）。
- 桌面端新增多 Agent 角色徽章与 Critic 自愈轮次可视化（`AegisForge.exe`）。

### Security

- 插件不持有任何凭据；MCP 工具参数禁止携带原始证据/凭据，审计副本按密钥名打码。
- 生产写动作只能经 `DomainRollbackExecutor`（HITL 审批摘要 + 超时熔断 + 回滚计划就绪）。
- 记忆库拒绝含明文凭据的内容；AUDITOR 角色只读。

## [2.0.0] - 2026-11-23

首个公开的技术预览版本（TECH_PREVIEW）。

### Added

- 四层十模块架构：交互编排、检测验证、AI 与进化、企业治理。
- 原生 Windows 桌面工作台 `AegisForge.exe`：8 节点 S 型 DAG、4-Tab 检视器、长按边框蓄力审批、1.4s 圆心扩散与 0ms 极速排障双模式。
- SQL AST 节点级双向可逆脱敏：映射表 Fernet 信封加密、按 `workflow_run_id` 持久化、还原前后语法校验、闭环销毁。
- 双轨验证 Harness：瞬时容器 / 温备池分层池化、PoC 消误报、Dry-Run、240 条业务 SQL 回放、生产只读长事务预检。
- 会话级锁超时强制注入、0~5 分钟实时熔断自动回滚、24~72 小时 SIEM 延迟观察与一键紧急恢复。
- WORM SHA-256 哈希链审计、SARIF v2.1.0 导出、CMDB / SIEM 集成接口。
- 可开关自主进化引擎：Shadow Mode、规则蒸馏、Git 风格版本回滚。

### Security

- 生产凭据与脱敏映射零明文落盘；桌面端零凭据缓存。
- 缺少 Rollback 脚本禁止进入沙箱与生产；长事务阻塞自动挂起变更窗口。
- 公开前完成历史清理与密钥扫描，仓库不含 `.env` 与真实凭据。

### Known limitations

- 容器运行态与部分外部集成为 `NOT_VERIFIED`，详见 [docs/w12/known-limitations.md](docs/w12/known-limitations.md)。
- 本项目为技术预览，不建议直接用于生产环境。
