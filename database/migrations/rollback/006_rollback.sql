-- W06 回滚：逆 FK 顺序删除四张表 → 移除 task_events 两列两索引 → 删除版本记录。
-- 注意：
--   1) 回滚会丢弃审批与 Prepared/Observed 证据，仅限开发/LAB 环境使用；
--   2) 最后一条 DELETE 必须由**具备 DELETE 权限的管理员账号**执行：
--      `dg_ctl` 对 dg_system.* 只有 SELECT/INSERT 与逐表 UPDATE，没有 schema_migrations
--      的 DELETE（这是"只可追加"授权边界的直接结果，不是遗漏）；
--   3) 本脚本非幂等（重复执行 DROP KEY 会报 1091），回滚后请重新执行 scripts/migrate.py。

DROP TABLE IF EXISTS dg_system.step_observed;

DROP TABLE IF EXISTS dg_system.step_prepared;

DROP TABLE IF EXISTS dg_system.approval_consumptions;

DROP TABLE IF EXISTS dg_system.approvals;

ALTER TABLE dg_system.task_events
    DROP KEY idx_events_actor,
    DROP KEY idx_events_tenant_created,
    DROP COLUMN actor_user_id,
    DROP COLUMN tenant_id;

DELETE FROM dg_system.schema_migrations WHERE version = '006_w06_approval_audit';
