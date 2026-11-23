-- 回滚 W07：删除披露快照列与索引。非幂等，重复执行会报 1091。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】本脚本最后一步 DELETE FROM schema_migrations
-- 需要 DELETE 权限，而应用迁移账号 dg_ctl 对 dg_system.schema_migrations 只有
-- SELECT / INSERT（见 scripts/bootstrap_mysql.py）。因此本脚本不得纳入
-- scripts/migrate.py 的自动流程，必须由运维用管理员账号手工执行：
--     mysql --defaults-extra-file=<admin.cnf> < database/migrations/rollback/007_rollback.sql
-- 回滚后必须重跑 python scripts/migrate.py 才能回到 007（回滚脚本不自动重放）。
-- 已知残余风险（W06 门禁 §6-5 已登记，W11 收口）：DROP COLUMN / DROP KEY 属 ALTER
-- 权限，dg_ctl 技术上具备该能力，W07 未拆分 DDL 账号。

ALTER TABLE dg_system.approvals
    DROP KEY idx_approvals_disclosure_digest,
    DROP COLUMN disclosed_at,
    DROP COLUMN disclosure_version,
    DROP COLUMN disclosure_digest,
    DROP COLUMN disclosure_json;

DELETE FROM dg_system.schema_migrations WHERE version = '007_w07_disclosure';
