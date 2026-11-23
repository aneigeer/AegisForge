-- W09 T03：结构化报告快照（只追加）。
--
-- 只追加语义：report_snapshots 只靠 scripts/bootstrap_mysql.py 的
-- CONTROL_BASE_GRANTS（GRANT SELECT, INSERT ON dg_system.*）取得权限。
-- dg_ctl 对本表既无 UPDATE 也无 DELETE，且对 schema_migrations 无 DELETE。
-- 本迁移不含任何 DELETE 或 DROP 语句，可由 scripts/migrate.py 自动执行。
-- 配套回滚脚本 rollback/010_rollback.sql 含 DROP，必须由 DG_ADMIN_DSN 人工执行，
-- 绝不纳入 scripts/migrate.py 自动流程，也不要在本次验收中执行它。
--
-- 注意：split_statements 按分号切分且不去行内注释，本文件注释不得含字面量分号。
--
-- 快照只追加：报告可由证据重建（report_version 会变），但已交付的对外编号
-- report_id 在回滚后会失效，须在发布说明登记（设计 §8.5）。

CREATE TABLE IF NOT EXISTS dg_system.report_snapshots (
    report_id              VARCHAR(64) PRIMARY KEY,
    tenant_id              VARCHAR(64) NOT NULL,
    task_id                VARCHAR(64) NOT NULL,
    report_version         VARCHAR(32) NOT NULL,
    fact_status            VARCHAR(16) NOT NULL,
    action_state           VARCHAR(24) NOT NULL,
    verification_state     VARCHAR(16) NOT NULL,
    verification_result_id VARCHAR(64) NULL,
    evidence_ids_json      JSON NOT NULL,
    missing_evidence_json  JSON NOT NULL,
    body_json              JSON NOT NULL,
    redaction_version      VARCHAR(32) NOT NULL,
    degraded               TINYINT(1) NOT NULL,
    created_at             DATETIME(6) NOT NULL,
    KEY idx_report_task (tenant_id, task_id, created_at),
    CONSTRAINT fk_report_tenant_task FOREIGN KEY (tenant_id, task_id)
        REFERENCES dg_system.tasks (tenant_id, task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
