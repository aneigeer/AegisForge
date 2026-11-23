-- 回滚 P1-BYOK-T1：删除租户模型供应商表。非幂等。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】【验收流程不要执行它】
-- 含 DROP TABLE 与 DELETE FROM schema_migrations：应用迁移账号 dg_ctl 无 DROP/DELETE 权限。
--
-- 【回滚前必读】本表只存供应商配置密文与掩码，**不含**对话内容；
-- 但删除后 `active` 供应商消失，相关租户的模型调用会退回全局 DG_MODEL_* 配置，
-- 需运维确认后再执行。

DROP TABLE IF EXISTS dg_system.tenant_model_providers;
DELETE FROM dg_system.schema_migrations WHERE version = '012';
