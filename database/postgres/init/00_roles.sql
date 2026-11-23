-- PG LAB 角色与数据库初始化模板（W01）。
-- 用法（由 scripts/bootstrap_pg_lab.py 调用）：
--   psql -v ro_password=... -v rw_password=... -f 00_roles.sql
-- 注意：不要把真实密码写入仓库。

\set ON_ERROR_STOP on
SET password_encryption = 'scram-sha-256';

SELECT format('CREATE ROLE dg_ro_pg LOGIN PASSWORD %L', :'ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dg_ro_pg')\gexec

SELECT format('ALTER ROLE dg_ro_pg PASSWORD %L', :'ro_password')\gexec

SELECT format('CREATE ROLE dg_rw_pg LOGIN PASSWORD %L', :'rw_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dg_rw_pg')\gexec

SELECT format('ALTER ROLE dg_rw_pg PASSWORD %L', :'rw_password')\gexec

SELECT 'CREATE DATABASE dg_lab_pg OWNER dg_rw_pg ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'dg_lab_pg')\gexec

-- 只读账号不允许创建对象；写入账号只用于隔离 LAB。
REVOKE CREATE ON DATABASE dg_lab_pg FROM PUBLIC;
GRANT CONNECT ON DATABASE dg_lab_pg TO dg_ro_pg;
