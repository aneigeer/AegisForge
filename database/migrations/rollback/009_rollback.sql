-- 回滚 W09 T02：删隔离区表 + 删 evidence.source_payload_hash 列。非幂等。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】【本次验收不要执行它】
-- 本脚本含 DROP TABLE / DROP COLUMN 与最后一步 DELETE FROM schema_migrations，
-- 需要 DROP 与 DELETE 权限；而应用迁移账号 dg_ctl 对 dg_system 只有
-- SELECT/INSERT/CREATE/ALTER/INDEX/REFERENCES（见 scripts/bootstrap_mysql.py），
-- 既无 DELETE 也无 DROP。
-- 因此本脚本不得纳入 scripts/migrate.py 自动流程，只能由运维用管理员账号手工执行：
--     mysql --defaults-extra-file=<admin.cnf> < database/migrations/rollback/009_rollback.sql
-- 回滚后必须重跑 python scripts/migrate.py 才能回到 009（回滚脚本不自动重放）。
--
-- 【回滚前必读】source_payload_hash 是原文**唯一锚点**：原文不落库，该列一旦
-- DROP 即无法重算，"哪份原文对应哪行证据"的可追溯性永久丢失。
-- 因此回滚前必须先导出该列（例如
--     mysqldump --no-create-info --columns=evidence_id,source_payload_hash dg_system evidence
-- ），确认离线留档后再执行本脚本。
--
-- 反序回滚：先删表（后建），再删列（先加）。

DROP TABLE IF EXISTS dg_system.redaction_quarantine;

ALTER TABLE dg_system.evidence
    DROP COLUMN source_payload_hash;

DELETE FROM dg_system.schema_migrations WHERE version = '009_w09_redaction';
