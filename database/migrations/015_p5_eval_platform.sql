-- P5-T1：评测中台持久化（套件 / 运行 / 逐例结果 / 候选对比）。
--
-- 只追加：运行与逐例结果一旦写入不再修改（`dg_ctl` 本就无 UPDATE/DELETE）。
-- 不存模型 prompt 原文、不存数据库凭据；metrics 只存**实测**数值（缺测为 NULL）。

CREATE TABLE IF NOT EXISTS dg_system.eval_suites (
    suite_id         VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(64)  NOT NULL,
    suite_version    VARCHAR(64)  NOT NULL,
    kind             VARCHAR(16)  NOT NULL,
    case_count       INT          NOT NULL,
    digest           CHAR(64)     NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    PRIMARY KEY (suite_id),
    KEY idx_eval_suites_tenant (tenant_id, suite_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS dg_system.eval_runs (
    run_id           VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(64)  NOT NULL,
    suite_id         VARCHAR(64)  NOT NULL,
    candidate_label  VARCHAR(64)  NOT NULL,
    provider_type    VARCHAR(32)  NOT NULL,
    model_name       VARCHAR(128) NOT NULL,
    wiki_snapshot    VARCHAR(64)  NULL,
    status           VARCHAR(16)  NOT NULL,
    metrics_json     JSON         NOT NULL,
    notes            VARCHAR(512) NULL,
    created_at       DATETIME(6)  NOT NULL,
    PRIMARY KEY (run_id),
    KEY idx_eval_runs_tenant (tenant_id, created_at),
    CONSTRAINT fk_eval_runs_suite FOREIGN KEY (suite_id)
        REFERENCES dg_system.eval_suites (suite_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS dg_system.eval_case_results (
    run_id           VARCHAR(64)  NOT NULL,
    case_id          VARCHAR(64)  NOT NULL,
    status           VARCHAR(16)  NOT NULL,
    latency_ms       INT          NULL,
    tokens           INT          NULL,
    outcome_json     JSON         NULL,
    created_at       DATETIME(6)  NOT NULL,
    PRIMARY KEY (run_id, case_id),
    CONSTRAINT fk_eval_case_run FOREIGN KEY (run_id)
        REFERENCES dg_system.eval_runs (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS dg_system.eval_comparisons (
    comparison_id    VARCHAR(64)  NOT NULL,
    tenant_id        VARCHAR(64)  NOT NULL,
    baseline_run_id  VARCHAR(64)  NOT NULL,
    candidate_run_id VARCHAR(64)  NOT NULL,
    deltas_json      JSON         NOT NULL,
    verdict          VARCHAR(16)  NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    PRIMARY KEY (comparison_id),
    KEY idx_eval_comparisons_tenant (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
