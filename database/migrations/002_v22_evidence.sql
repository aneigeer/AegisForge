-- W03 V2.2：租户/环境、任务并发版本、追加式事件链、Evidence 证据表。

-- ---------------------------------------------------------------------------
-- 租户 / 成员 / 环境
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.tenants (
    tenant_id   VARCHAR(64) PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    status      VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at  DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.users (
    user_id     VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(255) NOT NULL,
    status      VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at  DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.memberships (
    tenant_id   VARCHAR(64) NOT NULL,
    user_id     VARCHAR(64) NOT NULL,
    role        VARCHAR(32) NOT NULL,
    created_at  DATETIME(6) NOT NULL,
    PRIMARY KEY (tenant_id, user_id, role),
    KEY idx_memberships_user (user_id),
    CONSTRAINT fk_memberships_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id),
    CONSTRAINT fk_memberships_user FOREIGN KEY (user_id) REFERENCES dg_system.users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.environments (
    environment_id VARCHAR(64) PRIMARY KEY,
    tenant_id      VARCHAR(64) NOT NULL,
    db_engine      VARCHAR(16) NOT NULL,
    db_version     VARCHAR(64) NOT NULL,
    network_id     VARCHAR(64) NOT NULL,
    image_digest   VARCHAR(128) NULL,
    config_version VARCHAR(64) NOT NULL,
    status         VARCHAR(32) NOT NULL DEFAULT 'LAB',
    created_at     DATETIME(6) NOT NULL,
    KEY idx_environments_tenant (tenant_id),
    CONSTRAINT fk_environments_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO dg_system.tenants (tenant_id, name, status, created_at)
VALUES ('tenant_local', 'Local Alpha Tenant', 'ACTIVE', UTC_TIMESTAMP(6));

INSERT IGNORE INTO dg_system.users (user_id, display_name, status, created_at)
VALUES ('user_local', 'Local Operator', 'ACTIVE', UTC_TIMESTAMP(6));

INSERT IGNORE INTO dg_system.memberships (tenant_id, user_id, role, created_at)
VALUES ('tenant_local', 'user_local', 'Administrator', UTC_TIMESTAMP(6));

INSERT IGNORE INTO dg_system.environments
    (environment_id, tenant_id, db_engine, db_version, network_id, image_digest, config_version, status, created_at)
VALUES
    ('env_mysql_local', 'tenant_local', 'mysql', '8.0.46', 'lab_net', NULL, 'w03-local', 'LAB', UTC_TIMESTAMP(6)),
    ('env_pg_local', 'tenant_local', 'postgresql', '12.18', 'lab_net', NULL, 'w03-local', 'LAB', UTC_TIMESTAMP(6));

-- ---------------------------------------------------------------------------
-- 任务：租户/环境归属、state_version、截止时间与预算
-- ---------------------------------------------------------------------------
ALTER TABLE dg_system.tasks
    ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_local',
    ADD COLUMN environment_id VARCHAR(64) NOT NULL DEFAULT 'env_mysql_local',
    ADD COLUMN state_version INT NOT NULL DEFAULT 1,
    ADD COLUMN deadline_at DATETIME(6) NULL,
    ADD COLUMN max_steps INT NOT NULL DEFAULT 8,
    ADD COLUMN max_tool_calls INT NOT NULL DEFAULT 10,
    ADD COLUMN max_tokens INT NOT NULL DEFAULT 20000,
    ADD COLUMN replan_count INT NOT NULL DEFAULT 0,
    ADD KEY idx_tasks_tenant_status (tenant_id, status),
    ADD KEY idx_tasks_environment (environment_id),
    ADD CONSTRAINT fk_tasks_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id),
    ADD CONSTRAINT fk_tasks_environment FOREIGN KEY (environment_id) REFERENCES dg_system.environments(environment_id);

-- ---------------------------------------------------------------------------
-- 事件：task_id + seq 唯一、previous_event_hash 哈希链、trace_id、cursor 读取
-- ---------------------------------------------------------------------------
ALTER TABLE dg_system.task_events ADD COLUMN seq INT NULL;
ALTER TABLE dg_system.task_events ADD COLUMN previous_event_hash CHAR(64) NULL;
ALTER TABLE dg_system.task_events ADD COLUMN event_hash CHAR(64) NULL;
ALTER TABLE dg_system.task_events ADD COLUMN trace_id VARCHAR(64) NULL;

UPDATE dg_system.task_events e
JOIN (
    SELECT event_id,
           ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at ASC, event_id ASC) - 1 AS seq_no
    FROM dg_system.task_events
) ranked ON ranked.event_id = e.event_id
SET e.seq = ranked.seq_no;

ALTER TABLE dg_system.task_events MODIFY COLUMN seq INT NOT NULL;

UPDATE dg_system.task_events e
SET e.event_hash = SHA2(CONCAT(e.task_id, '|', e.seq, '|', COALESCE(e.payload_json, '')), 256);

UPDATE dg_system.task_events e
JOIN (
    SELECT event_id,
           LAG(event_hash) OVER (PARTITION BY task_id ORDER BY seq ASC) AS prev_hash
    FROM dg_system.task_events
) chain ON chain.event_id = e.event_id
SET e.previous_event_hash = chain.prev_hash;

ALTER TABLE dg_system.task_events
    ADD UNIQUE KEY uq_events_task_seq (task_id, seq),
    ADD KEY idx_events_task_seq (task_id, seq);

-- ---------------------------------------------------------------------------
-- Evidence：来源、时间窗、质量、payload_hash、脱敏版本与归属
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.evidence (
    evidence_id          VARCHAR(64) PRIMARY KEY,
    tenant_id            VARCHAR(64) NOT NULL,
    task_id              VARCHAR(64) NOT NULL,
    step_id              VARCHAR(64) NULL,
    tool_call_id         VARCHAR(64) NULL,
    environment_id       VARCHAR(64) NOT NULL,
    db_engine            VARCHAR(16) NOT NULL,
    db_version           VARCHAR(64) NOT NULL,
    adapter_version      VARCHAR(64) NOT NULL,
    source_tool          VARCHAR(64) NOT NULL,
    observed_at          DATETIME(6) NOT NULL,
    time_window_start    DATETIME(6) NULL,
    time_window_end      DATETIME(6) NULL,
    collection_status    VARCHAR(32) NOT NULL,
    quality              VARCHAR(32) NOT NULL,
    completeness         VARCHAR(32) NOT NULL,
    payload_hash         CHAR(64) NOT NULL,
    redaction_version    VARCHAR(32) NOT NULL,
    reproducible         TINYINT(1) NOT NULL DEFAULT 0,
    payload_json         JSON NOT NULL,
    redacted_payload_json JSON NULL,
    created_at           DATETIME(6) NOT NULL,
    KEY idx_evidence_tenant_task (tenant_id, task_id),
    KEY idx_evidence_environment (environment_id),
    KEY idx_evidence_observed_at (observed_at),
    CONSTRAINT fk_evidence_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id),
    CONSTRAINT fk_evidence_task FOREIGN KEY (task_id) REFERENCES dg_system.tasks(task_id),
    CONSTRAINT fk_evidence_environment FOREIGN KEY (environment_id) REFERENCES dg_system.environments(environment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
