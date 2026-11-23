-- 本地 LAB 回滚脚本：002+003+004。仅用于可丢弃的控制库。
-- 执行前：停止 API/Worker、备份 dg_system、由人工确认。
-- 顺序：先删复合/单列外键与索引，再删列/表。

ALTER TABLE dg_system.evidence
    DROP FOREIGN KEY fk_evidence_tenant_task,
    DROP FOREIGN KEY fk_evidence_tenant_environment,
    DROP FOREIGN KEY fk_evidence_task,
    DROP FOREIGN KEY fk_evidence_environment,
    DROP FOREIGN KEY fk_evidence_tenant;

DROP TABLE IF EXISTS dg_system.evidence;

ALTER TABLE dg_system.tasks
    DROP FOREIGN KEY fk_tasks_tenant_environment,
    DROP FOREIGN KEY fk_tasks_environment,
    DROP FOREIGN KEY fk_tasks_tenant,
    DROP INDEX uq_tasks_tenant,
    DROP INDEX idx_tasks_tenant_status,
    DROP INDEX idx_tasks_environment,
    DROP COLUMN tenant_id,
    DROP COLUMN environment_id,
    DROP COLUMN state_version,
    DROP COLUMN deadline_at,
    DROP COLUMN max_steps,
    DROP COLUMN max_tool_calls,
    DROP COLUMN max_tokens,
    DROP COLUMN replan_count;

ALTER TABLE dg_system.task_events
    DROP INDEX uq_events_task_seq,
    DROP INDEX idx_events_task_seq,
    DROP COLUMN seq,
    DROP COLUMN previous_event_hash,
    DROP COLUMN event_hash,
    DROP COLUMN trace_id;

DROP TABLE IF EXISTS dg_system.memberships;
DROP TABLE IF EXISTS dg_system.users;
DROP TABLE IF EXISTS dg_system.environments;
DROP TABLE IF EXISTS dg_system.tenants;

-- 回滚后允许 migrate 重新应用 002–004。
DELETE FROM dg_system.schema_migrations
WHERE version IN ('002_v22_evidence', '003_evidence_redaction_hash', '004_tenant_composite_fk');
