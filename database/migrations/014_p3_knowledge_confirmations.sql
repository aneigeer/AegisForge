-- P3-T1：知识条目的**两级人工确认**（3-Gate N-of-M）落库字段。
--
-- 只追加语义不变：确认是新的 revision（revision+1），本迁移只加列，不改历史行。
--   confirmations_json：Confirmation[]，每条含 stage(1|2)/confirmed_by/confirmed_at/content_digest
--   content_digest：该 revision 的内容摘要；stage-1 之后内容若变化（新 revision），
--                   旧确认因摘要不匹配而自动失效（服务层拒绝晋升 INDEXED）。
--
-- 不含 UPDATE / DELETE / DROP，可由 scripts/migrate.py 自动执行。

ALTER TABLE dg_system.knowledge_entries
    ADD COLUMN confirmations_json JSON NULL AFTER reviewed_at,
    ADD COLUMN content_digest CHAR(64) NULL AFTER confirmations_json;
