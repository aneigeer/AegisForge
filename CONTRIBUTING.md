# 贡献指南

感谢参与 AegisForge。提交前请先阅读 [README](README.md)、[SECURITY.md](SECURITY.md) 与 [docs/dev-guidelines.md](docs/dev-guidelines.md)。

## 开发环境

- Python **3.14**、Git、PowerShell（Windows 桌面端构建）
- 推荐使用 `uv` 管理虚拟环境

```powershell
uv venv .venv --python 3.14
uv pip install -e ".[dev]" --python .venv\Scripts\python.exe
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m apps.desktop.launcher --prepare-only
```

数据库相关改动需要 MySQL 8 / PostgreSQL 16 LAB：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_lab.ps1
```

## 提交前检查

```powershell
# 核心模块与桌面桥接
.\.venv\Scripts\python.exe -m pytest tests/unit/test_ten_core_modules.py tests/unit/test_desktop_launcher.py -o addopts="" -q -p no:cacheprovider

# 全量单元测试与密钥扫描
powershell -ExecutionPolicy Bypass -File scripts/ci_check.ps1
```

涉及 `pyproject.toml`、`docker-compose.yml`、`deploy/` 或 `.env.example` 的改动，还需运行：

```powershell
.\.venv\Scripts\python.exe scripts/w12_release.py
```

## 提交规范

使用 Conventional Commits：

```text
feat(platform): 新增 ...
fix(desktop): 修复 ...
docs: 更新 ...
test: 补充 ...
chore(security): ...
```

一次提交只做一件事；不要混入格式化全仓库的改动。

## Pull Request

1. Fork 仓库并从 `main` 创建分支；
2. 保持变更聚焦，附测试证据与安全影响说明；
3. 确保 CI 通过、无新增告警；
4. 在 PR 描述中关联 Issue（如有）。

## 安全红线

- **禁止**提交 `.env`、真实 DSN、Token、私钥、客户数据或未脱敏日志；
- 示例配置只能使用 `CHANGE_ME` 占位；
- 不得移除或弱化强制 Rollback、会话锁超时、审批单次消费等安全不变量；
- 新增依赖必须确认许可证兼容（Apache-2.0），并更新 `requirements.lock.txt`；
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私密报告，不要提交公开 Issue。

## 许可证

提交贡献即表示你同意以 [Apache License 2.0](LICENSE) 授权你的贡献，并确认你拥有相应权利。本项目不使用 CLA。
