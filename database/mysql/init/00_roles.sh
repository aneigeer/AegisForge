#!/bin/bash
# MySQL 8 LAB 初始化：最小权限账号（Docker 首次启动时执行）。
# 仅创建 dg_lab 业务库；控制库 dg_system 由独立 control_db 容器负责。
set -euo pipefail

: "${DG_RO_PASSWORD:?DG_RO_PASSWORD is required}"
: "${DG_RW_PASSWORD:?DG_RW_PASSWORD is required}"

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" --default-character-set=utf8mb4 <<-EOSQL
CREATE DATABASE IF NOT EXISTS dg_lab CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'dg_ro'@'%' IDENTIFIED BY '${DG_RO_PASSWORD}';
ALTER USER 'dg_ro'@'%' IDENTIFIED BY '${DG_RO_PASSWORD}';

CREATE USER IF NOT EXISTS 'dg_rw'@'%' IDENTIFIED BY '${DG_RW_PASSWORD}';
ALTER USER 'dg_rw'@'%' IDENTIFIED BY '${DG_RW_PASSWORD}';

-- 只读诊断账号：先清空历史授权，再只授予读取权限
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_ro'@'%';
GRANT SELECT ON dg_lab.* TO 'dg_ro'@'%';
GRANT SELECT ON performance_schema.* TO 'dg_ro'@'%';
-- PROCESS 为只读实例级权限：用于 processlist / innodb_trx 观察，不允许写入
GRANT PROCESS ON *.* TO 'dg_ro'@'%';

-- 隔离沙箱写账号：仅限 dg_lab 业务表，不授予控制库权限
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'dg_rw'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, REFERENCES,
      CREATE TEMPORARY TABLES, LOCK TABLES ON dg_lab.* TO 'dg_rw'@'%';

FLUSH PRIVILEGES;
EOSQL

echo "[init] MySQL LAB roles created: dg_ro (SELECT only), dg_rw (dg_lab sandbox only)"
