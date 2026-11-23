# ADR-0003 LAB 版本矩阵

- 状态：Accepted（本地开发）/ 正式镜像待冻结
- 日期：2026-11-07
- 相关：V2.2 W01

## 背景

V2.2 要求锁定镜像 tag/digest、记录版本矩阵，并在环境指纹变化时暂停/重新探测。

## 决策

| 环境 | 引擎 | 版本 | 端口 | 状态 |
| --- | --- | --- | --- | --- |
| Compose 候选 | PostgreSQL | `postgres:16.4` | 55432（宿主 127.0.0.1） | 文件已定义；镜像 digest NOT_VERIFIED |
| Compose 候选 | MySQL | `mysql:8.0.46` | 3307（宿主 127.0.0.1） | 文件已定义；镜像 digest NOT_VERIFIED |
| 本机开发 | PostgreSQL | 12.18（EOL） | 55432 | AVAILABLE，仅本地验证 |
| 本机开发 | MySQL | 8.0.46 | 3306 | AVAILABLE |

1. 正式 LAB 必须使用 Compose 固定 tag，并在首次拉取后记录 digest 到 Environment Manifest。
2. 本机 PostgreSQL 12.18 不用于性能/统计能力结论；涉及 `pg_stat_statements` 的项必须显式返回 NOT_SUPPORTED/PARTIAL。
3. 环境指纹变化（版本、端口、镜像 digest、能力）时，任务必须暂停并重新探测。

## 验证方式

- `scripts/environment_manifest.py` 生成两份 Manifest；
- `database/postgres/capabilities.py` 返回显式能力状态；
- Docker 可用后补做 `docker compose config` 与镜像 digest 记录。

## 已知限制

- 本机 Docker daemon 不可用，PG16.4 镜像未拉取、digest 未记录；
- PG12 已 EOL，仅作为开发替身。
