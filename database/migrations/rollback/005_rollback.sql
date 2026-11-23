-- 回滚 W05 迁移（005）。
-- 仅用于本地/测试环境；生产回滚需先导出认证会话与租约审计数据。

DROP TABLE IF EXISTS dg_system.execution_envelopes;
DROP TABLE IF EXISTS dg_system.worker_leases;
DROP TABLE IF EXISTS dg_system.auth_sessions;

ALTER TABLE dg_system.tasks
    DROP KEY idx_tasks_tenant_owner,
    DROP COLUMN created_by;

ALTER TABLE dg_system.users
    DROP COLUMN updated_at,
    DROP COLUMN auth_version,
    DROP COLUMN password_updated_at,
    DROP COLUMN password_hash;

DELETE FROM dg_system.schema_migrations WHERE version = '005_w05_identity_policy';
