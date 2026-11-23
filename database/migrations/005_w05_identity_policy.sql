-- W05 V2.2：本地身份认证、会话 Token、Worker 租约与执行信封。
-- 只追加/可撤销表优先；密码哈希不接受明文或不加盐摘要。

-- ---------------------------------------------------------------------------
-- 用户认证字段：成熟密码哈希（scrypt）+ 认证版本（用于全局吊销）
-- ---------------------------------------------------------------------------
ALTER TABLE dg_system.users
    ADD COLUMN password_hash VARCHAR(255) NULL,
    ADD COLUMN password_updated_at DATETIME(6) NULL,
    ADD COLUMN auth_version INT NOT NULL DEFAULT 1,
    ADD COLUMN updated_at DATETIME(6) NULL;

-- ---------------------------------------------------------------------------
-- 任务所有者：tenant_id + object ownership 双重校验
-- ---------------------------------------------------------------------------
ALTER TABLE dg_system.tasks
    ADD COLUMN created_by VARCHAR(64) NOT NULL DEFAULT 'user_local',
    ADD KEY idx_tasks_tenant_owner (tenant_id, created_by);

-- ---------------------------------------------------------------------------
-- 认证会话：Token 只存 SHA-256 摘要；可过期、撤销、轮换
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.auth_sessions (
    session_id   VARCHAR(64) PRIMARY KEY,
    user_id      VARCHAR(64) NOT NULL,
    tenant_id    VARCHAR(64) NOT NULL,
    token_hash   CHAR(64) NOT NULL,
    auth_method  VARCHAR(32) NOT NULL,
    status       VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at   DATETIME(6) NOT NULL,
    expires_at   DATETIME(6) NOT NULL,
    last_used_at DATETIME(6) NULL,
    revoked_at   DATETIME(6) NULL,
    rotated_from VARCHAR(64) NULL,
    UNIQUE KEY uq_auth_sessions_token (token_hash),
    KEY idx_auth_sessions_user (user_id, status),
    KEY idx_auth_sessions_expires (expires_at),
    CONSTRAINT fk_auth_sessions_user FOREIGN KEY (user_id) REFERENCES dg_system.users(user_id),
    CONSTRAINT fk_auth_sessions_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- Worker 租约：每个任务最多一个 ACTIVE 租约（生成列 + 唯一键）；fencing 单调递增
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.worker_leases (
    lease_id     VARCHAR(64) PRIMARY KEY,
    task_id      VARCHAR(64) NOT NULL,
    tenant_id    VARCHAR(64) NOT NULL,
    worker_id    VARCHAR(64) NOT NULL,
    environment_id VARCHAR(64) NOT NULL,
    fencing_token BIGINT NOT NULL,
    status       VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    issued_at    DATETIME(6) NOT NULL,
    heartbeat_at DATETIME(6) NOT NULL,
    expires_at   DATETIME(6) NOT NULL,
    released_at  DATETIME(6) NULL,
    active_task_id VARCHAR(64) GENERATED ALWAYS AS (
        CASE WHEN status = 'ACTIVE' THEN task_id ELSE NULL END
    ) STORED,
    UNIQUE KEY uq_worker_leases_active_task (active_task_id),
    KEY idx_worker_leases_task (task_id, fencing_token),
    KEY idx_worker_leases_worker (worker_id, status),
    CONSTRAINT fk_worker_leases_task FOREIGN KEY (task_id) REFERENCES dg_system.tasks(task_id),
    CONSTRAINT fk_worker_leases_tenant FOREIGN KEY (tenant_id) REFERENCES dg_system.tenants(tenant_id),
    CONSTRAINT fk_worker_leases_environment FOREIGN KEY (environment_id) REFERENCES dg_system.environments(environment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- 执行信封：nonce_hash 唯一 => 重放必然失败；签名只由 Gateway 校验
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dg_system.execution_envelopes (
    envelope_id  VARCHAR(64) PRIMARY KEY,
    task_id      VARCHAR(64) NOT NULL,
    step_id      VARCHAR(64) NULL,
    worker_id    VARCHAR(64) NOT NULL,
    lease_id     VARCHAR(64) NOT NULL,
    fencing_token BIGINT NOT NULL,
    state_version INT NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    actor_user_id VARCHAR(64) NOT NULL,
    tool_name    VARCHAR(64) NOT NULL,
    tool_version VARCHAR(32) NOT NULL,
    environment_id VARCHAR(64) NOT NULL,
    args_digest  CHAR(64) NOT NULL,
    nonce_hash   CHAR(64) NOT NULL,
    issued_at    DATETIME(6) NOT NULL,
    expires_at   DATETIME(6) NOT NULL,
    signature    CHAR(64) NOT NULL,
    created_at   DATETIME(6) NOT NULL,
    UNIQUE KEY uq_envelopes_nonce (nonce_hash),
    KEY idx_envelopes_task (task_id, created_at),
    KEY idx_envelopes_lease (lease_id),
    CONSTRAINT fk_envelopes_task FOREIGN KEY (task_id) REFERENCES dg_system.tasks(task_id),
    CONSTRAINT fk_envelopes_lease FOREIGN KEY (lease_id) REFERENCES dg_system.worker_leases(lease_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
