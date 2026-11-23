# 开发规范（V2.2）

## 目录职责

| 目录 | 职责 |
| --- | --- |
| `apps/api` | HTTP 入口、DTO、路由；不直接实现安全判定 |
| `apps/web` | 展示层（后续周次） |
| `core` | 配置、错误、任务、证据、策略、审计等核心逻辑 |
| `adapters` | PostgreSQL/MySQL 适配层与固定工具实现 |
| `workers` | 后台任务/执行 Worker（W05 起） |
| `database` | 连接层、迁移、初始化脚本 |
| `deploy` | Compose、镜像、网络拓扑、环境 Manifest |
| `benchmarks` / `datasets` | 评测与数据集资产（W10 起） |
| `docs` | ADR、架构、接口与验收记录 |
| `tests` | 单元/集成/安全回归测试 |

## 依赖方向

`apps -> core -> adapters/database`；`adapters/database` 不得反向依赖 `apps`。模型层只能输出建议，不得直接执行工具或接触数据库凭据。

## 提交与测试

- 分支：`main` 保持可运行；功能分支按 `feat/wXX-*` 命名。
- 测试：`.\.venv\Scripts\python.exe -m pytest -q`；本地 CI：`powershell -ExecutionPolicy Bypass -File scripts/ci_check.ps1`。
- 未验证代码不得标记完成；集成测试依赖 LAB 时，缺失环境必须显式记录为 SKIPPED/NOT_VERIFIED。
- 禁止提交 `.env`、真实 DSN、Token、私钥、生产连接串。

## 双引擎约束

- 所有工具与证据必须标注 `db_engine`；不得把 PG 能力假设套用到 MySQL，反之亦然。
- 能力不足返回 `PARTIAL/DENIED/NOT_SUPPORTED/UNKNOWN`，不得伪造成 `SUCCESS`。
- 只读账号在数据库层必须无写权限；应用层只读开关只是第二道防线。

## 周门禁

每周结束必须提交：交付物、测试命令与结果、未通过项、风险/ADR、下一周依赖确认。未通过门禁不得进入依赖它的下一周。
