-- P1-BYOK-T1：租户级模型供应商注册表（每租户可配多家，**同时仅一家 active**）。
--
-- 安全口径（INV-09）：
--   * api_key_cipher 存 Fernet 密文（`DG_SECRET_MASTER_KEY` 派生），**绝不存明文**；
--   * api_key_preview 只保留 `sk-***1234` 形态的掩码，供列表/详情回显；
--   * base_url 的 SSRF 校验在应用层（core/models/ssrf.py）执行，本表不做网络判定，
--     但把 `ssrf_checked_version` 记下来，便于审计"这条配置过了哪版校验"。
--
-- 本迁移不含 DELETE / DROP，可由 scripts/migrate.py 自动执行；
-- 回滚脚本 rollback/012_rollback.sql 含 DROP，须人工执行。

CREATE TABLE IF NOT EXISTS dg_system.tenant_model_providers (
    provider_id            CHAR(36)     NOT NULL,
    tenant_id              VARCHAR(64)  NOT NULL,
    name                   VARCHAR(128) NOT NULL,
    provider_type          VARCHAR(32)  NOT NULL,
    base_url               VARCHAR(512) NOT NULL,
    api_key_cipher         TEXT         NOT NULL,
    api_key_preview        VARCHAR(64)  NOT NULL,
    role_models_json       JSON         NOT NULL,
    timeout_s              INT          NOT NULL DEFAULT 60,
    stream                 TINYINT(1)   NOT NULL DEFAULT 0,
    active                 TINYINT(1)   NOT NULL DEFAULT 0,
    ssrf_checked_version   VARCHAR(16)  NOT NULL DEFAULT 'ssrf-v1',
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME(6)  NOT NULL,
    PRIMARY KEY (provider_id),
    UNIQUE KEY uq_tmp_tenant_name (tenant_id, name),
    KEY idx_tmp_tenant_active (tenant_id, active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
