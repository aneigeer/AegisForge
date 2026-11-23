# ADR-0001 运行时与双引擎范围

- 状态：Accepted（本机开发）
- 日期：2026-11-07
- 相关：V2.2 W01

## 背景

最新 V2.2 计划要求 PostgreSQL 与 MySQL 双引擎 LAB、控制面与 Agent 安全链路。此前 1.1 阶段曾将 PostgreSQL 全部替换为 MySQL；V2.2 重新引入双引擎。

## 决策

1. 运行时固定 Python 3.14.7（`requires-python = ">=3.14,<3.15"`），依赖以 `requirements.lock.txt` 为准。
2. Alpha 目标为双引擎：MySQL 8.0.46 与 PostgreSQL（Compose 候选 16.4；本机开发 12.18）。
3. 模型训练不在本阶段范围；旧版 W04 Model Gateway 代码作为可复用资产保留，不纳入 V2.2 验收。
4. 单元测试可在无数据库时运行；集成测试依赖本机/容器 LAB，缺失时跳过并如实标注。

## 备选方案与取舍

- 仅 MySQL：实现成本低，但不满足 V2.2 双引擎门禁，且 PG MCP/能力差异无法验证。
- 仅 PostgreSQL：与既有 MySQL 工具和种子数据冲突。
- 双引擎：满足计划，但需要额外维护 DSN、能力探测与测试矩阵。

## 后果

- 所有工具/证据必须携带 `db_engine`；跨引擎行为不得假设一致。
- 本机 PG 12.18 已 EOL，只用于本地开发；正式 LAB 必须使用 Compose 固定镜像。

## 验证方式

- `tests/unit/test_dependency_contract.py` 断言 Python/依赖；
- `tests/integration/test_pg_connection.py` 与 MySQL 权限测试；
- `scripts/ci_check.ps1` 执行编译、单测、Compose 静态校验。

## 已知限制

- 本机 Docker daemon 不可用，Compose 网络隔离未运行验证；
- 本机 PostgreSQL 12.18 与 Compose 16.4 存在版本差异。
