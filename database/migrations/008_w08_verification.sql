-- W08：独立验证结果（只追加）+ 补偿关联列（供 T04 使用）。
-- 只追加语义：verification_results 只靠 scripts/bootstrap_mysql.py 的
-- CONTROL_BASE_GRANTS（GRANT SELECT, INSERT ON dg_system.*）取得权限；
-- dg_ctl 对该表既无 UPDATE 也无 DELETE，且对 schema_migrations 无 DELETE。
-- 本迁移不含任何 DELETE / DROP 语句，可由 scripts/migrate.py 自动执行。
-- 配套回滚脚本 rollback/008_rollback.sql 含 DROP / DELETE，必须由
-- DG_ADMIN_DSN 人工执行，绝不纳入 scripts/migrate.py 自动流程，也不要执行它。

CREATE TABLE IF NOT EXISTS dg_system.verification_results (
    result_id         VARCHAR(64) PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL,
    task_id           VARCHAR(64) NOT NULL,
    tool_call_id      VARCHAR(64) NOT NULL,
    plan_digest       CHAR(64) NOT NULL,
    verdict           VARCHAR(16) NOT NULL,
    outcomes_json     JSON NOT NULL,
    disposition       VARCHAR(32) NULL,
    approval_required TINYINT(1) NOT NULL,
    claimed_success   TINYINT(1) NOT NULL,
    collected_at      DATETIME(6) NOT NULL,
    KEY idx_verification_results_call (tenant_id, tool_call_id),
    KEY idx_verification_results_task (tenant_id, task_id),
    CONSTRAINT fk_verification_results_tenant_task FOREIGN KEY (tenant_id, task_id)
        REFERENCES dg_system.tasks (tenant_id, task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE dg_system.approvals
    ADD COLUMN compensation_of VARCHAR(64) NULL,
    ADD UNIQUE KEY uq_approvals_compensation_of (compensation_of);
