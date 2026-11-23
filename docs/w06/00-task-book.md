# W06｜审批事务与追加式审计（任务书原文）

> 来源：`项目规划/项目规划2.2/DBGuardian_W06_审批事务与追加式审计.docx`
> 依据：《DBGuardian 完整研发总纲 V2.2｜双数据库工程版》总纲 §7、§9–§10、§15、§25
> 主题：把"人工同意"变成绑定具体动作、只能消费一次的可验证授权

## 本周目标

完成 Approval Schema、action_digest 规范化、审批原子消费、nonce 防重放、追加式审计与 UNKNOWN_OUTCOME 持久化，为 W07 单引擎 LAB 修复开放受控前提。

## 前置依赖

W05 认证、RBAC、Risk Resolver、Gateway、lease/fencing 已通过。若任一安全负测失败，冻结写工具。

## 一、本周执行范围

本周重点：完成 Approval Schema、action_digest 规范化、审批原子消费、nonce 防重放、追加式审计与 UNKNOWN_OUTCOME 持久化。

边界提醒：本周未列入交付的能力不得为了赶进度绕过权限、审批、证据、审计或验证机制；遇到不支持/权限不足/结果未知，必须如实返回状态。

## 二、五日执行清单

### D1｜Approval 数据模型与操作摘要

- 实现 Approval：tenant/task/step/environment、environment_fingerprint、db_engine、action_digest、prerequisites_digest、policy_version、tool/version、requested_by、approver_id、authn_context、expires_at、nonce_hash、status。
- 审批页面/接口必须展示具体操作、目标对象、影响、风险、前置条件、补偿方案、验证计划和有效期。
- 定义规范化算法 ADR；按总纲建议使用 RFC 8785 JCS（或经 ADR 确认的等价方案）后计算 SHA-256。
- 将 tenant、environment、environment_fingerprint、engine、tool/version、normalized_operation、target_objects、parameters、policy_version 纳入 digest。

### D2｜审批原子消费与失效规则

- 实现审批通过、拒绝、过期、撤销、消费状态；审批只能绑定一个具体 action_digest。
- 在控制库事务内检查 APPROVED、expires_at、nonce 未消费、digest 匹配，再条件更新为 CONSUMED。
- 为 approval_id/nonce_hash 建唯一约束；并发请求只能有一个消费成功，其余返回 APPROVAL_ALREADY_CONSUMED。
- 参数、对象、环境、策略版本或工具版本变化时旧审批失效；Approver 与 Operator 分离。

### D3｜Audit Service 与完整性

- 实现追加式 AuditEvent：actor、tenant、task、step、action_digest、policy_version、decision、result、timestamp、trace_id、integrity_ref。
- 普通 API/Worker 无 UPDATE/DELETE 审计记录权限；实现哈希链或等价完整性校验。
- 选定 Alpha WORM/Object Lock 候选方案并形成 ADR；可先实现本地/对象存储接口，不把未实测方案标成已具备合规能力。
- 审计写入失败时 R2 写操作 fail closed，并生成告警。

### D4｜Prepared/Observed 与未知结果

- 写操作前持久化 StepPrepared；执行后尽快写 StepObserved，保存请求、响应、开始/结束时间和结果状态。
- 明确 SUCCESS、PARTIAL、DENIED、TIMEOUT、UNKNOWN_OUTCOME；TIMEOUT/响应丢失不能简单解释为失败。
- UNKNOWN_OUTCOME 必须转 NEEDS_RECONCILIATION；不得自动重试非幂等操作。
- 恢复时按 action_digest、幂等键、目标对象状态、审计事件和数据库日志核对；证据不足转 MANUAL_INTERVENTION。

### D5｜审批/审计并发与故障测试

- 测试审批参数被修改、目标环境变化、审批过期、撤销、重放、nonce 重复、并发双消费。
- 测试 Worker 在 StepPrepared 后崩溃、数据库已执行但响应丢失、审计存储不可用等情况。
- 验证审计失败会阻断 R2；审批消费失败不会进入执行；已消费审批不可再次使用。
- 冻结审批协议、digest 测试向量、审计事件 Schema 与故障演练报告。

## 三、本周交付物

- Approval Schema、规范化/action_digest/prerequisites_digest 实现与 ADR。
- 审批 API、单次消费事务、nonce 唯一约束、撤销/过期规则。
- Audit Service、追加式事件、完整性校验与 WORM 接口/选型 ADR。
- StepPrepared/StepObserved/UNKNOWN_OUTCOME 持久化与故障恢复测试。

## 四、测试与验收标准

- 审批绑定具体 action_digest、环境指纹和策略版本；任何关键字段变化使审批失效。
- 并发消费只有一个成功；重放、过期、撤销和参数篡改均被拒绝。
- 审计事件追加写、普通业务组件不能覆盖/删除；完整性检查可发现篡改。
- 写操作审计失败时 R2 fail closed；结果未知时进入 NEEDS_RECONCILIATION。
- 完成 W6 → W7 门禁：认证/RBAC、Gateway、风险、审批指纹、审计与 fencing 测试稳定。

## 五、主要风险与处理

- 把审批做成简单布尔值：必须绑定 action_digest、环境和前置条件，不能批准一个抽象的"修复"。
- 把跨库操作宣称 Exactly Once：控制库与目标库没有天然分布式原子事务，采用幂等键、事件、核对和停止策略。
- 将补偿称作回滚：补偿是独立操作，仍需策略和必要审批。

## 六、周末门禁与完成定义

W6 → W7：所有审批重放/并发消费/参数变更失效测试通过；审计不可用时 R2 被阻断；未知结果不触发盲目重试。

本周完成定义：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；不得仅以"代码已写完"作为完成标准。
