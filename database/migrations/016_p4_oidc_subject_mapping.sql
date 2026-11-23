-- P4-T1：OIDC 外部主体 → 本地用户的映射（角色仍**只**来自 memberships）。
--
-- 只加列 + 唯一键；不允许把角色/租户写进 token 就能提权：
--   * oidc_issuer / oidc_subject 仅是"身份映射"，不携带任何授权语义；
--   * 授权一律由 dg_system.memberships 决定（与 W05 口径一致）。
--
-- 不含 UPDATE / DELETE / DROP，可由 scripts/migrate.py 自动执行。

ALTER TABLE dg_system.users
    ADD COLUMN oidc_issuer VARCHAR(255) NULL AFTER status,
    ADD COLUMN oidc_subject VARCHAR(255) NULL AFTER oidc_issuer;

ALTER TABLE dg_system.users
    ADD UNIQUE KEY uq_users_oidc_subject (oidc_issuer, oidc_subject);
