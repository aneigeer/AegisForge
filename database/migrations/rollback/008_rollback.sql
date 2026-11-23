-- 回滚 W08：删 compensation_of 列/唯一键 + 删 verification_results 表。非幂等。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】【本次验收不要执行它】
-- 本脚本含 DROP TABLE / DROP COLUMN / DROP KEY 与最后一步
-- DELETE FROM schema_migrations，需要 DROP 与 DELETE 权限；而应用迁移账号
-- dg_ctl 对 dg_system 只有 SELECT/INSERT/CREATE/ALTER/INDEX/REFERENCES
-- （见 scripts/bootstrap_mysql.py），既无 DELETE 也无 DROP。
-- 因此本脚本不得纳入 scripts/migrate.py 自动流程，只能由运维用管理员账号手工执行：
--     mysql --defaults-extra-file=<admin.cnf> < database/migrations/rollback/008_rollback.sql
-- 回滚后必须重跑 python scripts/migrate.py 才能回到 008（回滚脚本不自动重放）。

ALTER TABLE dg_system.approvals
    DROP KEY uq_approvals_compensation_of,
    DROP COLUMN compensation_of;

DROP TABLE IF EXISTS dg_system.verification_results;

DELETE FROM dg_system.schema_migrations WHERE version = '008_w08_verification';
