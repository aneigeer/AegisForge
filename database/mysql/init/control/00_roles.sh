#!/bin/bash
# 控制库 dg_system 角色（在 02_harness_core.sql 建表之后执行）。
# 事件/证据表只授予 SELECT + INSERT，保证数据库级只追加。
set -euo pipefail

: "${DG_CONTROL_PASSWORD:?DG_CONTROL_PASSWORD is required}"

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" --default-character-set=utf8mb4 <<-EOSQL
CREATE USER IF NOT EXISTS 'dg_ctl'@'%' IDENTIFIED BY '${DG_CONTROL_PASSWORD}';
ALTER USER 'dg_ctl'@'%' IDENTIFIED BY '${DG_CONTROL_PASSWORD}';

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_ctl'@'%';
GRANT SELECT, INSERT ON dg_system.* TO 'dg_ctl'@'%';
GRANT CREATE, DROP, ALTER, INDEX, REFERENCES ON dg_system.* TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.tasks TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.task_steps TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.tool_calls TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.checkpoints TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.tenants TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.users TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.memberships TO 'dg_ctl'@'%';
GRANT UPDATE, DELETE ON dg_system.environments TO 'dg_ctl'@'%';

FLUSH PRIVILEGES;
EOSQL

echo "[init] MySQL control roles created: dg_ctl (append-only events/evidence)"
