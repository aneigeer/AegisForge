-- W03 补充：跨表租户归属复合唯一键与外键。
-- 目的：数据库层保证 task/evidence 的 tenant 与 environment 归属一致，
-- 而不只依赖应用层校验。

ALTER TABLE dg_system.environments
    ADD UNIQUE KEY uq_environments_tenant (tenant_id, environment_id);

ALTER TABLE dg_system.tasks
    ADD UNIQUE KEY uq_tasks_tenant (tenant_id, task_id);

ALTER TABLE dg_system.tasks
    ADD CONSTRAINT fk_tasks_tenant_environment
    FOREIGN KEY (tenant_id, environment_id)
    REFERENCES dg_system.environments(tenant_id, environment_id);

ALTER TABLE dg_system.evidence
    ADD CONSTRAINT fk_evidence_tenant_task
    FOREIGN KEY (tenant_id, task_id)
    REFERENCES dg_system.tasks(tenant_id, task_id);

ALTER TABLE dg_system.evidence
    ADD CONSTRAINT fk_evidence_tenant_environment
    FOREIGN KEY (tenant_id, environment_id)
    REFERENCES dg_system.environments(tenant_id, environment_id);
