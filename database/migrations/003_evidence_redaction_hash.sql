-- W03 补充：脱敏载荷哈希（原始/脱敏分层校验）。
ALTER TABLE dg_system.evidence
    ADD COLUMN redacted_payload_hash CHAR(64) NULL AFTER payload_hash;
