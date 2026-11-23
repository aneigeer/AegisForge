# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
