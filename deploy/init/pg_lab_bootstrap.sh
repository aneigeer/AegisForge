#!/bin/sh
# Docker 初始化：在 postgres 官方镜像的首次启动阶段创建只读角色并应用 schema。
# 依赖环境变量 DG_RO_PASSWORD / DG_RW_PASSWORD（Compose 注入）。
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ro_password="$DG_RO_PASSWORD" -v rw_password="$DG_RW_PASSWORD" \
  -f /opt/dg/00_roles.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -f /opt/dg/02_schema.sql

# P2-T1：pg_stat_statements 由 compose 的 shared_preload_libraries 预加载，
# 这里只负责装扩展（未预加载时该语句会失败——那正是需要暴露的配置错误）。
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
