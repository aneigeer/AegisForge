-- P1-BYOK-T1 修正：控制库账号 dg_ctl **只有 SELECT/INSERT**（无 UPDATE/DELETE），
-- 因此供应商配置改为**只追加版本**：每次改/删/激活都 INSERT 一条 revision+1，
-- 读路径取每个 root_id 的最新 revision。
--
-- 本迁移：去掉按 name 的唯一约束（同一供应商会有多条 revision），
-- 增加 root_id / revision / status 三列与查询索引。
-- 不含 DROP TABLE / DELETE；DROP INDEX 属正向结构变更（dg_ctl 有 ALTER 权限）。

ALTER TABLE dg_system.tenant_model_providers
    DROP INDEX uq_tmp_tenant_name;

ALTER TABLE dg_system.tenant_model_providers
    ADD COLUMN root_id CHAR(36) NOT NULL DEFAULT '' AFTER provider_id,
    ADD COLUMN revision INT NOT NULL DEFAULT 1 AFTER root_id,
    ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'INACTIVE' AFTER active;

ALTER TABLE dg_system.tenant_model_providers
    ADD INDEX idx_tmp_tenant_root (tenant_id, root_id, revision);
