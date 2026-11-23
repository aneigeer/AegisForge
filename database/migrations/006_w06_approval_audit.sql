-- W06 V2.2：审批事务与追加式审计（设计 §2）。
-- 审计真相只有一份：审批/审计/核对事件全部落既有 task_events，
-- 因此本迁移只给 task_events 增加两个过滤列与两个索引，哈希链列与计算式不动。
-- 新增的三张只追加表（approval_consumptions / step_prepared / step_observed）
-- 是"证据"而非第二套事件流，对 dg_ctl 只授予 SELECT/INSERT（见 scripts/bootstrap_mysql.py）。

-- ---------------------------------------------------------------------------
-- 2.1 task_events：租户/操作者过滤列（哈希链列不动）
-- ---------------------------------------------------------------------------
ALTER TABLE dg_system.task_events
    ADD COLUMN tenant_id VARCHAR(64) NULL,
    ADD COLUMN actor_user_id VARCHAR(64) NULL,
    ADD KEY idx_events_tenant_created (tenant_id, created_at),
    ADD KEY idx_events_actor (actor_user_id);

-- ---------------------------------------------------------------------------
-- 2.2 approvals：审批状态表（可变；历史由 task_events 承担）
-- 分通道摘要（parameters/target_objects）用于精确定位"是参数变了还是目标变了"，
-- 整体 action_digest 仍是唯一门禁。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.approvals (
    approval_id                 VARCHAR(64) PRIMARY KEY,
    tenant_id                   VARCHAR(64) NOT NULL,
    task_id                     VARCHAR(64) NOT NULL,
    step_id                     VARCHAR(64) NULL,
    environment_id              VARCHAR(64) NOT NULL,
    db_engine                   VARCHAR(16) NOT NULL,
    environment_fingerprint     CHAR(64) NOT NULL,
    environment_fingerprint_json JSON NOT NULL,
    action_digest               CHAR(64) NOT NULL,
    parameters_digest           CHAR(64) NOT NULL,
    target_objects_digest       CHAR(64) NOT NULL,
    prerequisites_digest        CHAR(64) NOT NULL,
    prerequisites_json          JSON NOT NULL,
    action_descriptor_json      JSON NOT NULL,
    policy_version              VARCHAR(64) NOT NULL,
    tool_name                   VARCHAR(64) NOT NULL,
    tool_version                VARCHAR(32) NOT NULL,
    requested_by                VARCHAR(64) NOT NULL,
    requested_session_id        VARCHAR(64) NULL,
    approver_id                 VARCHAR(64) NULL,
    approver_session_id         VARCHAR(64) NULL,
    authn_context_json          JSON NOT NULL,
    nonce_hash                  CHAR(64) NOT NULL,
    status                      VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    decision_reason             VARCHAR(512) NULL,
    operator_note               VARCHAR(2000) NULL,
    created_at                  DATETIME(6) NOT NULL,
    expires_at                  DATETIME(6) NOT NULL,
    decided_at                  DATETIME(6) NULL,
    consumed_at                 DATETIME(6) NULL,
    consumed_by                 VARCHAR(64) NULL,
    revoked_at                  DATETIME(6) NULL,
    revoked_by                  VARCHAR(64) NULL,
    UNIQUE KEY uq_approvals_nonce (nonce_hash),
    KEY idx_approvals_tenant_task (tenant_id, task_id, step_id, status),
    KEY idx_approvals_status_expires (status, expires_at),
    KEY idx_approvals_digest (tenant_id, action_digest),
    CONSTRAINT fk_approvals_tenant_task FOREIGN KEY (tenant_id, task_id)
        REFERENCES dg_system.tasks (tenant_id, task_id),
    CONSTRAINT fk_approvals_tenant_environment FOREIGN KEY (tenant_id, environment_id)
        REFERENCES dg_system.environments (tenant_id, environment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 2.3 approval_consumptions：消费台账（写一次）
-- approval_id 与 nonce_hash 双唯一约束 => 单次消费由数据库保证，
-- 不依赖应用层"先查后写"（任何重复消费必然撞唯一键）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.approval_consumptions (
    consumption_id VARCHAR(64) PRIMARY KEY,
    approval_id    VARCHAR(64) NOT NULL,
    tenant_id      VARCHAR(64) NOT NULL,
    task_id        VARCHAR(64) NOT NULL,
    tool_call_id   VARCHAR(64) NULL,
    envelope_id    VARCHAR(64) NULL,
    nonce_hash     CHAR(64) NOT NULL,
    action_digest  CHAR(64) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    consumed_by    VARCHAR(64) NOT NULL,
    consumed_at    DATETIME(6) NOT NULL,
    UNIQUE KEY uq_consumption_approval (approval_id),
    UNIQUE KEY uq_consumption_nonce (nonce_hash),
    KEY idx_consumption_task (tenant_id, task_id),
    CONSTRAINT fk_consumption_approval FOREIGN KEY (approval_id)
        REFERENCES dg_system.approvals (approval_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 2.4 step_prepared：执行前证据（写一次）
-- uq_prepared_call(tool_call_id) 保证同一 tool_call 只有一条 prepared 记录。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.step_prepared (
    prepared_id     VARCHAR(64) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    task_id         VARCHAR(64) NOT NULL,
    run_id          VARCHAR(64) NOT NULL,
    step_id         VARCHAR(64) NULL,
    tool_call_id    VARCHAR(64) NOT NULL,
    envelope_id     VARCHAR(64) NULL,
    approval_id     VARCHAR(64) NULL,
    tool_name       VARCHAR(64) NOT NULL,
    tool_version    VARCHAR(32) NOT NULL,
    action_digest   CHAR(64) NULL,
    idempotency_key VARCHAR(128) NULL,
    policy_version  VARCHAR(64) NOT NULL,
    environment_id  VARCHAR(64) NOT NULL,
    request_json    JSON NOT NULL,
    prepared_at     DATETIME(6) NOT NULL,
    UNIQUE KEY uq_prepared_call (tool_call_id),
    KEY idx_prepared_task (tenant_id, task_id, prepared_at),
    KEY idx_prepared_approval (approval_id),
    CONSTRAINT fk_prepared_tenant_task FOREIGN KEY (tenant_id, task_id)
        REFERENCES dg_system.tasks (tenant_id, task_id),
    CONSTRAINT fk_prepared_envelope FOREIGN KEY (envelope_id)
        REFERENCES dg_system.execution_envelopes (envelope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 2.5 step_observed：执行后证据（写一次）
-- 刻意不含 reconciliation_state：否则核对需要 UPDATE 本表。
-- 核对结论写 task_events + tasks.status。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.step_observed (
    observed_id    VARCHAR(64) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL,
    task_id        VARCHAR(64) NOT NULL,
    run_id         VARCHAR(64) NOT NULL,
    step_id        VARCHAR(64) NULL,
    tool_call_id   VARCHAR(64) NOT NULL,
    tool_name      VARCHAR(64) NOT NULL,
    outcome        VARCHAR(32) NOT NULL,
    result_json    JSON NULL,
    error_code     VARCHAR(64) NULL,
    started_at     DATETIME(6) NOT NULL,
    finished_at    DATETIME(6) NULL,
    duration_ms    INT NULL,
    observed_at    DATETIME(6) NOT NULL,
    UNIQUE KEY uq_observed_call (tool_call_id),
    KEY idx_observed_task (tenant_id, task_id, observed_at),
    CONSTRAINT fk_observed_tenant_task FOREIGN KEY (tenant_id, task_id)
        REFERENCES dg_system.tasks (tenant_id, task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
