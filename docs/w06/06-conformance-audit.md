# W06 T01–T03 符合性审计（只读）

- 审计对象：`089f926`（W06-T01~T03: 审批数据基座、审计服务与 Prepared/Observed）
- 审计依据：`docs/w06/02-design.md`（含 §11 增量）、`docs/adr/ADR-0005`、`docs/adr/ADR-0006`、`docs/w06/01-prd.md`
- 审计方式：只读代码/迁移/测试，未修改任何文件；实跑 `tests/unit/test_{canonical,action_digest,approval_lifecycle,audit_integrity}.py` = **179 passed（0.34s）**
- 判定口径：`PASS` 符合；`PARTIAL` 实现正确但存在偏差或属后续任务未接线；`FAIL` 与设计不符且需返工

## 结论总表

| # | 审计项 | 结论 | 证据（文件:行号） | 不通过/偏差时的修复要求 |
|---|---|---|---|---|
| 1 | 事件哈希链未被污染 | **PASS** | `core/tasks/repository.py:74-98` 的 `event_hash()` 函数体与 `d2ceae8` 逐字节一致（`diff` 为空）；diff 仅落在 `insert_event` 签名 `:409-416` 与 INSERT 列 `:466-484` | 无。可选加固见观察 O1 |
| 2 | 事务真在同一个连接上 | **PASS**（文档需同步） | `database/mysql/transaction.py:29-51`（单 `conn`、`begin/commit/rollback/close`；只读拒绝 `:31-35`）；`core/approval/service.py:610` 单一 `with transaction(...) as conn`；三条语句均传同一 `conn`：`repository.py:252/294/324` | 设计 §3/§4 写的是"给 `MysqlClient` 加 `transaction()`"，实现改为新模块 `database/mysql/transaction.py:1-12`（理由：`connection.py` 原始字节被 W04 故障资产哈希覆盖）。**我修订设计文档即可**，不改代码 |
| 3 | 单次消费的数据库保证 | **PASS**（1 处偏差） | 双唯一键 `006_w06_approval_audit.sql:65-66`；条件 UPDATE 带 `AND status='APPROVED'` 且返回 affected = `repository.py:324-336`，校验 `!=1` = `service.py:653-661`；1062→`ALREADY_CONSUMED` = `service.py:643-652` | 无阻断。偏差：行锁内才发现过期时抛错导致事务回滚，`EXPIRED` **未落库**（`service.py:622-631` 注释称由 `expire_due` 兜底），与设计 §5 第 3 项"先落状态再报错"不完全一致 → T05 补一条断言：并发场景下过期审批最终状态必须是 `EXPIRED`，不能长期停留 `APPROVED` |
| 4 | 审批凭证不可伪造 | **FAIL**（T04 计划内，非本提交缺陷） | seal 正确：`core/approval/models.py:129` `_GRANT_SEAL`、`:153-157` `__post_init__` 校验、`:191-193` `issue_grant` 唯一入口。但 `ApprovalDecision` **未删除**，共 8 处：`core/tasks/models.py:48`、`core/tasks/service.py:12,380`、`core/tools/executor.py:29,99,215`、`tests/integration/test_executor.py:14,321` | T04 必须删净（`grep -rn "ApprovalDecision"` 归零）并把 `_precheck` 的 R2 分支改为只认 `ApprovalGrant`。当前**不可利用**（`policy.py:230-238` 仍对 R2 恒 `allowed=False`，T01 未改 policy），故不阻断 T04 开工，但**阻断 W06 提交** |
| 5 | 摘要实现与 ADR-0005 一致 | **PASS**（1 处待补文档） | UTF-16 键序 `core/security/canonical.py:80-89,150-160`；紧凑分隔 `:157,171`；非 ASCII 不转义 `:73-76`；拒绝 float `:110-115` / Decimal `:116-121` / datetime,date,time `:122-127` / bytes,bytearray,memoryview `:128-133` / set,frozenset `:134-139` / 非 str 键 `:143-149` / 孤立代理项 `:56-63` / `\|int\|>2^53-1` `:91-100`；未知类型报错不降级 `:146-152` | `tuple` 被当数组接受（`canonical.py:141-144`），ADR-0005 的表未列该类型 → **我补 ADR 一行**，不改代码 |
| 6 | 消费时重算而非信任快照 | **FAIL**（最高优先级） | 指纹重算 ✅ `service.py:531-540`；parameters 重算 ✅ `:559`；policy/tool 版本 ✅ `:542-556`；prerequisites 重算 ✅ `:595-608`；**target_objects 未重算** ❌ `service.py:560` `targets = approval.target_objects`（取自 `action_descriptor_json` 快照） | 详见下方 §A，**T04 前必须修** |
| 7 | 权限收口 | **PARTIAL**（安全增强 + 文档漂移） | `approvals` 仅 `UPDATE` 无 `DELETE` = `scripts/bootstrap_mysql.py:66`；三张只追加表不在列表 = `:60-67`；`dg_ctl` DDL 已无 `DROP` = `:53`；006 键/外键与设计 §2 一致 = `006:41-49,65-66,99,117,127` | ① `DROP` 已移除（`docs/migrations.md:35`），**超出** ADR-0006「已知限制 2」的原记载 → **我修订 ADR-0006**；② 需确认正向迁移不依赖 DROP：已核 `001`–`006` 无 `DROP`/`RENAME`，仅回滚脚本需要（`docs/migrations.md:72-74`）✅；③ `step_observed.finished_at` 允许 NULL（`006:122`），设计 §2.5 写 NOT NULL → **我在设计里标注为允许 NULL**（未完成时无 finished_at，更稳） |
| 8 | StepPrepared 门禁顺序 | **PARTIAL**（未接线） | fail closed 语义正确：`core/execution/step_records.py:160-179`（失败→`AuditUnavailableError`+`_alert`）、`:161-171`（1062→`StateConflictError`）；`client.execute()` 独立自动提交连接 → 崩溃后确已提交 ✅；`Reconciler` 构造不接收 registry = `reconciler.py:63-71` → 结构上不可能重放 ✅ | **`insert_prepared` 目前无任何调用方**：全仓除 `step_records.py` 自身，仅 `reconciler.py:18,65` 引用其类型。执行链（`ToolExecutor`/`ToolGateway`）尚未接线，"落库早于任何工具调用"**尚未成立** → T04 交付项 |
| 9 | 未知结果语义 | **PARTIAL**（未实现） | `_should_retry` 未被放宽 ✅ `executor.py:383-395`（`write→False`、`not idempotent→False`） | 写操作 `TIMEOUT→UNKNOWN_OUTCOME` **未实现**（`_invoke` 超时仍返回 `TIMEOUT` = `executor.py:347-359`；`:551` 只是 W02 遗留的事件映射条目）；`UNKNOWN_OUTCOME→NEEDS_RECONCILIATION` **未实现**（`core/tasks/service.py` 本提交未改）→ T04 交付项 |

## §A 审计项 6 详情：`target_objects` 未在消费侧重算

**现象**：`service.py:560` 直接取审批记录里的快照 `approval.target_objects`（源自 `action_descriptor_json`，见 `models.py:279-281`），随后 `:561-577` 用它构造 descriptor 并重算 `targets_digest` 与 `action_digest`。而 `extract_target_objects()` 只在**创建侧**被调用（`service.py:166`），消费侧完全没有调用——两侧不对称，正是设计反复点名的分叉点。

**为什么是 FAIL 而不是"无影响"**：
1. `target_objects_digest` 恒等于登记值，设计 §5 第 8 项要求的"先分通道定位"在该通道上失去意义。
2. `tests/integration/test_approval_service.py:366-379` 的"目标对象定位"用例，实际验证的是**库内快照被篡改**（用 `control_client` 直接 `JSON_SET`），而不是"从本次请求参数重算"——它通过的理由与设计意图相反。
3. **存在真实缺口**：若工具契约的抽取规则被原地改动（例如 `name_field` 由 `table` 改为 `target_table`）而 `tool_version` 未同步提升，则同一份 `parameters` 会映射到**不同**目标对象，而 `parameters_digest`（参数未变）与 `target_objects_digest`（取快照）**都会通过** → 审批授权的对象与实际被改动的对象不一致。`action_digest` 同样被快照污染，无法兜住。这正是 W06 声称要检出的"契约与请求不一致"类缺陷。
4. 反向确认：单纯篡改**参数**仍是安全的（`parameters_digest` 会拦住），所以**不构成越权**——安全影响低，符合性影响高。
5. `tests/integration/test_approval_service.py:529-531` 的"write 工具空目标对象被拒"只覆盖创建侧；六类失效里"目标对象"这一类在消费侧实际未真实测到。

**修复要求（T04 前）**
- `service.py:560-561` 改为：`targets = normalize_target_objects(extract_target_objects(definition, parameters))`；当 `definition.side_effect == "write"` 且抽取为空时，按 `APPROVAL_DIGEST_MISMATCH(channel="target_objects")` 拒绝（创建侧已有的 `TARGET_OBJECTS_UNDECLARED` 不变）。
- 新增断言（否则修复不可验）：构造"抽取规则原地变动、`tool_version` 不变、`parameters` 不变"的用例，断言 `channel == "target_objects"` 且工具未被调用。
- 保留现有 `test_target_objects_change_is_located`，但改名为"快照篡改定位"，避免与"请求侧重算"混淆。

## 附加观察（不阻断，但需登记）

- **O1**：`repository.py:66-73` 的 `_payload_str` 允许从 `event.payload` 兜底取 `tenant_id`/`actor_user_id` 写入过滤列。当前所有 payload 的同名字段均为服务端生成，但这是一条"payload → 索引列"的隐式耦合；建议 T05 改为**仅接受显式传参**，或至少在函数 docstring 里禁止客户端可控 payload 使用这两个键。
- **O2**：`service.py:663-679` 的 `APPROVAL_CONSUMED` 审计事件在 `COMMIT` **之后**写出。若该写入失败，审批已 `CONSUMED` 而调用方收到 `AuditUnavailableError`、工具未执行 → 形成"审批已耗、操作未发生"。不违反 fail-closed（工具确实没跑），但 `05-freeze.md` 必须写明该状态的人工处置路径，或 T04 把该事件挪进事务（代价：审计失败会回滚消费，需权衡）。
- **O3**：`_emit` 在消费成功后需要 `_repository.get()` 重读（`service.py:662`），即一次额外读；不影响正确性，仅记录。
- **O4**：本次提交把 T03 提前到 T02 之前（`core/approval/service.py` 依赖 `AuditService`）。任务顺序与设计 §6 不同但依赖方向合法，且提交信息已说明；建议在 `03-implementation.md` 里同步该顺序变更，避免文档与提交对不上。

## T04 前必须修复的阻断项

1. **审计项 6**：`service.py:560` 的 `targets` 必须改为从本次 `parameters` 重算（含 §A 的新增断言）。**唯一阻断项**。
2. 审计项 4（`ApprovalDecision` 删除 + `_precheck` 收口）与审计项 8/9（StepPrepared 接线、`UNKNOWN_OUTCOME` 映射）属 T04 交付内容，**不要求在 T04 前完成，但必须在 W06 提交前完成**；本审计把它们登记为"已知未完成"，不得在测试报告里表现为已通过。

## 连带的文档处置（已完成 / 已移交）

| 事项 | 处置 | 状态 |
|---|---|---|
| 设计 §3/§4 `transaction()` 落点、§2.5 `finished_at` 允许 NULL | 由我改 `docs/w06/02-design.md` | **已完成**（378 行） |
| ADR-0005 规则表补 `tuple` | 由我改 `docs/adr/ADR-0005` | **已完成**（210 行） |
| ADR-0006 已知限制 2 改为"已移除 DROP" | 由我改 `docs/adr/ADR-0006` | **已完成** |
| **O1** payload→索引列隐式耦合 | 写入 `docs/w06/05-freeze.md` §A.3 作为字段迁移约束；T05 移除 payload 兜底或加硬断言 | **已登记**（freeze §A.3） |
| **O2** `APPROVAL_CONSUMED` 事件在 `COMMIT` 之后 | 主理人裁定**不改代码**（塞进事务会拖入 `insert_event` 重试逻辑）；已写入 `ADR-0006` 已知限制 8，含"审批已消费但消费事件缺失"窗口与人工处置 | **已登记**（ADR-0006 限制 8） |
| **O3** "审批已耗但操作未发生"处置路径 | 写入 `docs/w06/05-freeze.md` §B.1 中间态清单 | **已登记**（freeze §B.1） |
| **O4** T03 先于 T02 的实现顺序 | `03-implementation.md` 由工程师产出，本审计只做移交，**不代写** | **已移交 T05** |
| 阻断项（审计项 6 `target_objects` 重算） | 主理人已单独下发给工程师，随 T04 修复并补断言；架构师**不修改** `core/approval/service.py` | **已移交 T04** |

### 新增发现（本轮补记，交 T04 前处置）

**`ApprovalService._mismatch()` 不写审计事件**（`core/approval/service.py:822`），且 `AuditEventType` 中**没有** `APPROVAL_CONSUME_DENIED`。设计 §1.4 要求"审批消费失败必须有账本记录"，当前完全依赖 T04 的 Gateway 落 `GATEWAY_EXECUTION_DENIED`。若 T04 未覆盖，则"消费被拒"只会留下一条 error 日志、没有事件链记录 —— 这会让 T4「重放/过期/撤销/参数篡改均被拒绝」的"且不产生工具调用记录"与"拒绝可追溯"两条断言出现缺口。**要求**：T04 二选一并落测（① Gateway 写 `GATEWAY_EXECUTION_DENIED`；② 新增 `APPROVAL_CONSUME_DENIED` 并在 `consume()` 内写）。已同步登记到 `docs/w06/05-freeze.md` §A.4。

**§11.1 的回归清单漏了第 4 个文件，且我提的"静态门禁"不足以兜住它。** 我在 §11.1 里断言"5 个既有构造点（3 个测试文件）"，实际是 **6 处（4 个文件）**——漏了 `tests/unit/test_planner_schema.py::_StubTool`。漏因很具体：该桩工具的 `side_effect` 是**函数参数注入**（`side_effect=side_effect`），不是字面量，所以我提的静态门禁 `grep -rn 'side_effect="write"'` **看不见它**。工程师在 T04 里已自行补上（`git diff` 可见该文件新增 `TargetObjectSpec` 与条件声明）。

**由此修订 §11.1 的门禁设计**（原设计需修正，不只是这一处）：
1. 静态 grep 的匹配面从 `side_effect="write"` 扩大为 `side_effect=`（捕获变量注入/默认值/枚举），命中的每一处都要求人工确认是否为 write 路径——**静态检查只用于列清单，不作为"证明无遗漏"的依据**。
2. 真正的兜底改为**运行时门禁**：新增一条测试，遍历 `build_observer_registry().definitions()` **与各测试桩注册表**中所有 `side_effect == "write"` 的 `ToolDefinition`，断言 `target_objects` 非空；再加一条"构造侧"断言，直接 `ToolDefinition(name=..., side_effect="write")`（不带 `target_objects`）必须抛 `ValueError`。
3. 结论：**"只读工具不受影响"的三条断言本身没错，但"受影响构造点已穷尽"这个结论不能由 grep 得出**，只能由"全量测试跑绿 + 运行时枚举断言"得出。后续同类契约变更的评估都按此口径写。


## 二轮审计：范围与判定口径（结论待 T04 提交后填写）

**前提**：本轮范围由主理人收窄为下述三条，**不扩大**；其余 T04 改动只做导入/编译级扫查，不逐行。

1. **`service.py` 的 targets 重算**：`ApprovalService.consume()` 是否已把 `targets = approval.target_objects` 改为 `normalize_target_objects(extract_target_objects(definition, parameters))`（即消费侧与创建侧对称，均以本次请求参数为来源）。
2. **新断言的真实覆盖度**。**判定关键（原样引用主理人裁定的验收标准）：测试里有没有真的构造"两个 spec 不同、`tool_version` 相同"的 definition——只改 `action_descriptor_json` 的旧用例不算。**
3. **消费拒绝的审计落点**：`APPROVAL_CONSUME_DENIED` 是否**只在 service 层落一次**（不重复、不遗漏），且 Gateway 的 `GATEWAY_EXECUTION_DENIED` **仍然存在**（两者不能互相替代）。

**结论：待填写。** 本文件在结论为空时**不得**被当作二轮审计已完成；二轮结论填入下方空白后，才可从"已知项清单"升级为"QA 验收证据"。

> 二轮结论（T04 `a22662a` / T05 `6176430`）：**三条全部 PASS，无剩余阻断项。**

### 二轮逐项判定

| # | 审计项 | 结论 | 证据（文件:行号） |
|---|---|---|---|
| 1 | `service.py` 的 targets 是否真的重算 | **PASS** | `core/approval/service.py:585-588` `extracted_targets = normalize_target_objects(extract_target_objects(definition, parameters))`；写工具空抽取在 `:589-596` **先**被 `EXTRACTED_TARGETS_EMPTY` 拒绝；回落 `:598` 位于其后 |
| 2 | 新断言的真实覆盖度 | **PASS**（两个子分支都覆盖） | 漂移用例 `tests/integration/test_approval_consume.py:564-602`；非空漂移用例 `:544-561`；旧用例改名 `tests/integration/test_approval_service.py:431-452` |
| 3 | `APPROVAL_CONSUME_DENIED` 落点与唯一性 | **PASS**（1 处覆盖建议） | 事件类型 `core/audit/events.py:72`；落点 `core/approval/service.py:886-888` → `_emit_consume_denied` `:906-947`；Gateway 保留 `core/gateway/tool_gateway.py:419-430` |

### 1. targets 重算：PASS，且**回落分支不会重新打开缺口**（结论）

- 重算：`service.py:585-588`，与创建侧 `:166` 使用同一对函数（`extract_target_objects` + `normalize_target_objects`），两侧已对称。
- **回落分支判定（主理人指定要结论）**：`:589-596` 的写工具空抽取拒绝**先于** `:598` 的 `targets = extracted_targets or approval.target_objects`，因此**写工具永远到不了回落**——只有当 `definition.side_effect != "write"` 时回落才生效，符合"回落仅对读工具/显式目标生效"的可接受条件。
- **我额外验证了回落是否可能被"绕过"**：唯一能让写工具走回落的办法是把契约的 `side_effect` 原地从 `write` 改成非写。这种情况**仍被拦住**，因为 `side_effect` 参与 `normalized_operation`（`core/approval/digest.py:361`），漂移会改变 descriptor → `action_digest` 比对失败（`service.py` 第 8 项末段）→ `channel=action` 拒绝。即：`side_effect` 漂移由 `channel=action` 兜住，目标对象漂移由 `channel=target_objects` 兜住，两条通道互补，回落不构成新缺口。
- 结论：**回落可接受，无缺口。**

### 2. 新断言的真实覆盖度：PASS（判定标准原样适用）

判定标准：**测试里有没有真的构造"两个 spec 不同、`tool_version` 相同"的 definition——只改 `action_descriptor_json` 的旧用例不算。** 逐条核对：

- `tests/integration/test_approval_consume.py:564` `test_extraction_rule_drift_blocks_execution_without_tool_call`：把注册表内的 spec 换成 `name_field="target_table"`（`_definition` 默认 `version="1.0"`，`tests/integration/test_approval_consume.py:94-120`，**tool_version 未变**），参数用 `dict(PARAMS)` **未变**，走**真实 Gateway 全链路**；断言 `channel=target_objects` + `reason=EXTRACTED_TARGETS_EMPTY` + 桩工具调用计数 `0` + `tool_calls` 0。**符合判定标准。**
- `:544` `test_target_object_rule_change_is_reported_as_target_objects_channel` 覆盖我原以为缺失的**第二个子分支**：`keys_field=None` 使同一份参数抽出 `keys=[]`，即"抽取结果非空但集合不同"，断言 `channel=target_objects`（不是 `EXTRACTED_TARGETS_EMPTY`）。**两个子分支齐全。**
- 旧用例已改名 `tests/integration/test_approval_service.py:431` `test_snapshot_tamper_is_located`，docstring 明确声明"不是抽取规则变更用例"，并断言 `channel=action` + `reason=DESCRIPTOR_DIGEST_MISMATCH`（因为快照被改后无法复算出登记的 `action_digest`，先命中 `action` 通道——这个断言变更与 T04 新增的"展示全文与摘要同源"校验一致，**不是**为了让测试变绿而放宽）。旧名 `test_target_objects_change_is_located` 全仓已不存在。

### 3. `APPROVAL_CONSUME_DENIED`：PASS（一处覆盖建议，非阻断）

- 事件类型：`core/audit/events.py:72`。
- 落点唯一：全仓 `_emit_consume_denied` 仅 `core/approval/service.py:886` 一处调用（由 `_mismatch()` 在抛错前触发），Gateway 不再重复写该类型。
- 拒绝优先：`service.py:931-947` 用 `self._audit.try_emit(...)` 返回布尔，失败仅 `logger.error(alert="AUDIT_WRITE_FAILED")`，**拒绝照常生效**，既不变 500 也不放行 ✅。
- Gateway 的 `GATEWAY_EXECUTION_DENIED` 保留：`core/gateway/tool_gateway.py:419-430` 的 `_denied()` 对**所有**消费失败（含状态类拒绝）写该事件，与 service 层的 `APPROVAL_CONSUME_DENIED` 语义不同、不互相替代 ✅。
- 断言核对：`tests/integration/test_approval_consume.py:603-632` `test_consume_denial_is_audited_with_channel` 真查了 `task_events`（经 `audit.query(event_types=["APPROVAL_CONSUME_DENIED"])`），断言 `len==1`、`channel=parameters`、`deny_code`、`actor_user_id`、`tool_calls` 计数 0 ✅。
- **覆盖建议（不阻断）**：`_mismatch()` 只覆盖"摘要/绑定不符"类拒绝；**状态类拒绝**（`PENDING/REJECTED/EXPIRED/REVOKED/CONSUMED`，经 `error_for_status()`）不落 `APPROVAL_CONSUME_DENIED`。可追溯性没有丢失（状态迁移在发生时就写了 `APPROVAL_EXPIRED/REVOKED/REJECTED/CONSUMED`，且 Gateway 必写 `GATEWAY_EXECUTION_DENIED`），但"查所有消费拒绝"需要同时查两类事件。建议 T05/`05-freeze.md` 里把这条查询口径写明，**不要求改代码**。

### 二轮剩余阻断项

**无。** 首轮的阻断项（`service.py:560` 快照回落）已修复并经上述三项复核确认。

### 二轮补充观察（均不阻断）
- 漂移用例通过 `gateway._registry._tools[TOOL_NAME] = drifted_tool`（`# noqa: SLF001`）直接改私有属性注入漂移契约。能证明问题，但依赖私有实现；若 `ToolRegistry` 后续加缓存/校验，该用例会静默失效。建议 T05 之后提供 `ToolRegistry.replace()` 或测试专用注入点。
- 未跑 `tests/integration/test_approval_consume.py`：主理人正在跑全量，避免数据库争用；本轮三条结论均基于代码级证据。已跑 `tests/unit` = **638 passed（4.52s）**，覆盖运行时门禁 `tests/unit/test_write_tool_gate.py`（6 条：直接构造抛错/显式空抛错/声明后接受/读工具允许/注册表拒绝/生产注册表 write 工具均已声明）。
