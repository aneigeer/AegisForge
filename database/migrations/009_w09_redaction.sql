-- W09 T02：脱敏原文锚点列 + 隔离区（只追加）。
--
-- 只追加语义：`evidence` 与 `redaction_quarantine` 只靠 scripts/bootstrap_mysql.py
-- 的 CONTROL_BASE_GRANTS（GRANT SELECT, INSERT ON dg_system.*）取得权限；
-- dg_ctl 对这两张表既无 UPDATE 也无 DELETE，且对 schema_migrations 无 DELETE。
-- 本迁移不含任何 DELETE / DROP 语句，可由 scripts/migrate.py 自动执行。
-- 配套回滚脚本 rollback/009_rollback.sql 含 DROP，必须由 DG_ADMIN_DSN 人工执行，
-- 绝不纳入 scripts/migrate.py 自动流程，也不要执行它。
--
-- 注意：split_statements 按 ";" 切分且不去行内注释，本文件不得含字面量分号。
--
-- 隔离区**没有任何原文列**：只存哈希（payload_hash）、类别（detected_categories）、
-- 定位（unresolved_spans_json 里的 json_path/start/end）、blockers 与版本号。
-- 原文一旦入库不可撤回（只追加 + 无 DELETE 授权），因此这里刻意不设 payload 列。

ALTER TABLE dg_system.evidence
    ADD COLUMN source_payload_hash CHAR(64) NULL AFTER payload_hash;

CREATE TABLE IF NOT EXISTS dg_system.redaction_quarantine (
    quarantine_id          VARCHAR(64) PRIMARY KEY,
    tenant_id              VARCHAR(64) NOT NULL,
    task_id                VARCHAR(64) NULL,
    channel                VARCHAR(32) NOT NULL,
    status                 VARCHAR(24) NOT NULL,
    payload_hash           CHAR(64) NOT NULL,
    detected_categories    JSON NOT NULL,
    unresolved_spans_json  JSON NOT NULL,
    blockers               JSON NOT NULL,
    redaction_version      VARCHAR(32) NOT NULL,
    created_at             DATETIME(6) NOT NULL,
    KEY idx_quarantine_task (tenant_id, task_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
