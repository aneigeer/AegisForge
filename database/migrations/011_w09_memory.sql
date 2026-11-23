-- W09 T04：知识条目（只追加，撤销/过期以追加新 revision 表达）。
--
-- 只追加语义：knowledge_entries 只靠 scripts/bootstrap_mysql.py 的
-- CONTROL_BASE_GRANTS（GRANT SELECT, INSERT ON dg_system.*）取得权限。
-- dg_ctl 对本表既无 UPDATE 也无 DELETE，且对 schema_migrations 无 DELETE。
-- 本迁移不含任何 DELETE 或 DROP 语句，可由 scripts/migrate.py 自动执行。
-- 配套回滚脚本 rollback/011_rollback.sql 含 DROP，必须由 DG_ADMIN_DSN 人工执行，
-- 绝不纳入 scripts/migrate.py 自动流程，也不要在本次验收中执行它。
--
-- 注意：split_statements 先去掉整行注释再按分号切分，本文件注释不得引入 SQL 语句。
--
-- 版本链：主键 (knowledge_id, revision)。撤销/过期/冲突都追加 revision+1，
-- 原行不改；知识含人工审核结论，回滚前必须先导出全量 revision（设计 §8.5）。

CREATE TABLE IF NOT EXISTS dg_system.knowledge_entries (
    knowledge_id                 VARCHAR(64) NOT NULL,
    revision                     INT NOT NULL,
    tenant_id                    VARCHAR(64) NOT NULL,
    kind                         VARCHAR(16) NOT NULL,
    status                       VARCHAR(16) NOT NULL,
    db_engine                    VARCHAR(16) NULL,
    version_min                  VARCHAR(64) NULL,
    version_max                  VARCHAR(64) NULL,
    adapter_version              VARCHAR(64) NULL,
    capability_scope_json        JSON NULL,
    environment_fingerprint_digest CHAR(64) NULL,
    risk_scope_json              JSON NULL,
    evidence_ids_json            JSON NULL,
    test_case_ids_json           JSON NULL,
    validation_level             VARCHAR(16) NOT NULL,
    reviewed_by                  VARCHAR(64) NULL,
    reviewed_at                  DATETIME(6) NULL,
    effective_from               DATETIME(6) NOT NULL,
    effective_until              DATETIME(6) NOT NULL,
    revoked_at                   DATETIME(6) NULL,
    revoked_reason               VARCHAR(255) NULL,
    source_license               VARCHAR(64) NOT NULL,
    supersedes                   VARCHAR(64) NULL,
    content_json                 JSON NOT NULL,
    redaction_version            VARCHAR(32) NOT NULL,
    created_at                   DATETIME(6) NOT NULL,
    PRIMARY KEY (knowledge_id, revision),
    KEY idx_knowledge_lookup (tenant_id, kind, status, db_engine, effective_until),
    KEY idx_knowledge_reviewer (tenant_id, reviewed_by)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
