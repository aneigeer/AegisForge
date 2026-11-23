-- 回滚 W09 T03：删结构化报告快照表。非幂等。
--
-- 【必须由 DG_ADMIN_DSN 人工执行】【本次验收不要执行它】
-- 本脚本含 DROP TABLE 与最后一步 DELETE FROM schema_migrations，
-- 需要 DROP 与 DELETE 权限；而应用迁移账号 dg_ctl 对 dg_system 只有
-- SELECT/INSERT/CREATE/ALTER/INDEX/REFERENCES（见 scripts/bootstrap_mysql.py），
-- 既无 DELETE 也无 DROP。
-- 因此本脚本不得纳入 scripts/migrate.py 自动流程，只能由运维用管理员账号手工执行：
--     mysql --defaults-extra-file=<admin.cnf> < database/migrations/rollback/010_rollback.sql
-- 回滚后必须重跑 python scripts/migrate.py 才能回到 010（回滚脚本不自动重放）。
--
-- 【回滚前必读】设计 §8.5：报告可由证据重建，但 report_version 会变，
-- **已交付的对外编号 report_id 会失效**，须在发布说明登记。
-- 本脚本只做结构回滚，不删任何历史报告行（DROP TABLE 会连同快照行一起移除，
-- 因此在执行前须确认这些快照不再需要对外引用）。

DROP TABLE IF EXISTS dg_system.report_snapshots;

DELETE FROM dg_system.schema_migrations WHERE version = '010_w09_report';
