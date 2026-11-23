-- P6-T1：API -> Worker 只读诊断工具派发队列（只追加，无 UPDATE/DELETE）。
-- 使 API 容器无需接入 lab_net：API 只把只读工具请求写入控制库，
-- 独立 Worker（仅 control_net + lab_net、无 model_net）领跑并回写脱敏后的结果。

CREATE TABLE IF NOT EXISTS dg_system.readonly_tool_requests (
    request_id      VARCHAR(64)  NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    task_id         VARCHAR(64)  NOT NULL,
    engine          VARCHAR(32)  NOT NULL DEFAULT 'mysql',
    tool_name       VARCHAR(128) NOT NULL,
    params_json     JSON         NOT NULL,
    requested_by    VARCHAR(128) NOT NULL,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (request_id),
    KEY idx_ro_dispatch_created (created_at),
    KEY idx_ro_dispatch_task (tenant_id, task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dg_system.readonly_tool_responses (
    request_id        VARCHAR(64)  NOT NULL,
    worker_id         VARCHAR(128) NOT NULL,
    status            VARCHAR(32)  NOT NULL,
    result_json       JSON         NOT NULL,
    redaction_version VARCHAR(64)  NULL,
    completed_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (request_id),
    KEY idx_ro_response_completed (completed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
