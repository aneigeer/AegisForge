-- 回滚 W09 T04：删知识条目表。非幂等。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】【本次验收不要执行它】
-- 本脚本含 DROP TABLE 与最后一步 DELETE FROM schema_migrations，
-- 需要 DROP 与 DELETE 权限；而应用迁移账号 dg_ctl 对 dg_system 只有
-- SELECT/INSERT/CREATE/ALTER/INDEX/REFERENCES（见 scripts/bootstrap_mysql.py），
-- 既无 DELETE 也无 DROP。
-- 因此本脚本不得纳入 scripts/migrate.py 自动流程，只能由运维用管理员账号手工执行：
--     mysql --defaults-extra-file=<admin.cnf> < database/migrations/rollback/011_rollback.sql
-- 回滚后必须重跑 python scripts/migrate.py 才能回到 011（回滚脚本不自动重放）。
--
-- 【回滚前必读】设计 §8.5：知识不可重建（含人工审核结论与证据等级），
-- DROP TABLE 会连同全部 revision 一起移除，回滚前必须导出全量 revision 留档。

DROP TABLE IF EXISTS dg_system.knowledge_entries;

DELETE FROM dg_system.schema_migrations WHERE version = '011_w09_memory';
