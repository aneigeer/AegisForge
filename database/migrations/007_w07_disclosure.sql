-- W07：审批披露三节的创建时刻快照（风险等级 / 补偿方案 / 验证计划）。
-- 与 action_digest 解耦：W06 冻结向量字段集不含三节，纳入需升版并重算全部向量。
-- 完整性由两件事共同保证：
--   1) disclosure_digest = canonical_digest(disclosure_json)，读/批/消费三处重算比对；
--   2) repository 的 UPDATE 列白名单不含这四个列（应用层约束，不依赖触发器）。
-- 为什么允许 NULL：W06 已存在的 PENDING/终态行无法回填真实内容，
-- NOT NULL DEFAULT 只能造假。新行由 ApprovalRepository.insert() 断言 digest 非空兜住。

ALTER TABLE dg_system.approvals
    ADD COLUMN disclosure_json    JSON        NULL AFTER action_descriptor_json,
    ADD COLUMN disclosure_digest  CHAR(64)    NULL AFTER disclosure_json,
    ADD COLUMN disclosure_version VARCHAR(16) NULL AFTER disclosure_digest,
    ADD COLUMN disclosed_at       DATETIME(6) NULL AFTER disclosure_version,
    ADD KEY idx_approvals_disclosure_digest (tenant_id, disclosure_digest);
