-- W03 Harness Core：任务状态、步骤、工具调用、事件与检查点。
-- 由 scripts/migrate.py 以 dg_rw 账号执行；dg_system 库由 bootstrap 创建。

CREATE TABLE IF NOT EXISTS dg_system.schema_migrations (
    version     VARCHAR(128) PRIMARY KEY,
    applied_at  DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.tasks (
    task_id           VARCHAR(64) PRIMARY KEY,
    run_id            VARCHAR(64) NOT NULL,
    goal              TEXT NOT NULL,
    status            VARCHAR(32) NOT NULL,
    risk_level        VARCHAR(8) NOT NULL DEFAULT 'R0',
    cancel_requested  TINYINT(1) NOT NULL DEFAULT 0,
    pause_requested   TINYINT(1) NOT NULL DEFAULT 0,
    error_code        VARCHAR(64) NULL,
    error_message     TEXT NULL,
    created_at        DATETIME(6) NOT NULL,
    updated_at        DATETIME(6) NOT NULL,
    finished_at       DATETIME(6) NULL,
    KEY idx_tasks_status (status),
    KEY idx_tasks_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.task_steps (
    step_id       VARCHAR(64) PRIMARY KEY,
    task_id       VARCHAR(64) NOT NULL,
    run_id        VARCHAR(64) NOT NULL,
    step_index    INT NOT NULL,
    title         VARCHAR(255) NOT NULL,
    tool_name     VARCHAR(64) NULL,
    params_json   JSON NULL,
    status        VARCHAR(32) NOT NULL,
    created_at    DATETIME(6) NOT NULL,
    updated_at    DATETIME(6) NOT NULL,
    UNIQUE KEY uq_steps_task_index (task_id, step_index),
    KEY idx_steps_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.tool_calls (
    tool_call_id      VARCHAR(64) PRIMARY KEY,
    task_id           VARCHAR(64) NOT NULL,
    run_id            VARCHAR(64) NOT NULL,
    step_id           VARCHAR(64) NULL,
    tool_name         VARCHAR(64) NOT NULL,
    idempotency_key   VARCHAR(128) NULL,
    status            VARCHAR(32) NOT NULL,
    attempt           INT NOT NULL DEFAULT 1,
    request_json      JSON NULL,
    result_json       JSON NULL,
    error_code        VARCHAR(64) NULL,
    error_message     TEXT NULL,
    started_at        DATETIME(6) NOT NULL,
    finished_at       DATETIME(6) NULL,
    duration_ms       DOUBLE NULL,
    UNIQUE KEY uq_tool_calls_idempotency (task_id, idempotency_key),
    KEY idx_tool_calls_task (task_id),
    KEY idx_tool_calls_tool (tool_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.task_events (
    event_id      VARCHAR(64) PRIMARY KEY,
    task_id       VARCHAR(64) NOT NULL,
    run_id        VARCHAR(64) NOT NULL,
    tool_call_id  VARCHAR(64) NULL,
    event_type    VARCHAR(64) NOT NULL,
    payload_json  JSON NULL,
    created_at    DATETIME(6) NOT NULL,
    KEY idx_events_task (task_id, created_at),
    KEY idx_events_type (event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dg_system.checkpoints (
    checkpoint_id  VARCHAR(64) PRIMARY KEY,
    task_id        VARCHAR(64) NOT NULL,
    run_id         VARCHAR(64) NOT NULL,
    sequence       INT NOT NULL,
    state_json     JSON NOT NULL,
    created_at     DATETIME(6) NOT NULL,
    UNIQUE KEY uq_checkpoints_task_sequence (task_id, sequence),
    KEY idx_checkpoints_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
