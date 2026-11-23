-- 回滚 P1-BYOK-T1 的版本列（012 表结构回到初始形态）。非幂等，需 DG_ADMIN_DSN 人工执行。
--
-- 【验收流程不要执行它】含 DROP INDEX / DROP COLUMN（dg_ctl 无 DROP 权限）。
-- 回滚前请先导出 tenant_model_providers 全量 revision：只追加版本一旦删列，
-- 历史配置与密文无法重建。

ALTER TABLE dg_system.tenant_model_providers DROP INDEX idx_tmp_tenant_root;
ALTER TABLE dg_system.tenant_model_providers
    DROP COLUMN status,
    DROP COLUMN revision,
    DROP COLUMN root_id;
ALTER TABLE dg_system.tenant_model_providers
    ADD UNIQUE KEY uq_tmp_tenant_name (tenant_id, name);
DELETE FROM dg_system.schema_migrations WHERE version = '013';
