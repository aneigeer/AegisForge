# W07 实施期符合性审计（对 tag `w07-close` 快照，只读 / 追溯补记）

> ## 追溯补记声明（必读）
>
> - **补记日期**：2026-11-17｜**性质**：追溯补记件（KD-5 第二件）。W07 收口（2026-11-12）与本件相隔 5 天，冻结时刻 `T0 = 2026-11-11T23:52:44+08:00` 相距 **6 个日历日**。
> - **本件不是冻结时刻证据**：2026-11-17 补写的审计**不可能**等于当时产出的原件；**本件不声称 W07 在冻结时刻已有该件**。
> - **原裁决**：`docs/w07/07-gate-review.md:105` 登记 KD-5「两件不存在」+「本轮不补、W08 首日承接」；W08 未承接（全仓仅 W06 有 `05-freeze` / `06-conformance-audit` 两类文件），故本次为**超期追溯补记**。
> - **本件的结论不改变 W07 门禁定档**：W07 口径固定为「**有条件放行；门禁通过：否**」（`docs/w07/07-gate-review.md:11`）。

## 审计对象与依据

| 项 | 值 |
|---|---|
| 审计对象 | tag `w07-close` 的**快照**：`git rev-parse w07-close` → `c8a739ad9ff1dabdcf4908e353bb11baacd30075`（`2026-11-12 14:14:01 +0800`，提交信息 `W07-close: 门禁件 §7 锚点校准与 P6 后表述修正`） |
| 读取方式 | **一律** `git show w07-close:<path>`（下文所有行号均指 tag 内该文件的行号）。**不使用工作区内容**：补记时工作区已含 W08–W12 改动（73 文件 / `+13,832/−176` 行，见 `docs/w07/05-freeze.md` §3.3），拿它审计等于把后来者当成 W07 |
| 审计依据 | `docs/w07/02-design.md`（设计 + 附 C）、`docs/w07/01-prd.md`（AC-01..AC-15）、`docs/w07/00-action-spec.md`、`docs/adr/ADR-0007-single-engine-lab-repair.md`、`docs/w07/07-gate-review.md`、`docs/w07/08-pm-ruling.md`、`docs/w06/05-freeze.md`（向量基准） |
| 审计方式 | **只读**：`git show` / `git grep` / `git ls-tree` / `sha256sum`。**未修改任何文件**，**未执行任何测试**，**未连接任何数据库**，**未执行 `git add/commit/checkout/reset`** |
| 判定口径 | `PASS` 实现与设计一致（或一致性成立且限定的口径已写明）；`PARTIAL` 实现主体正确但存在偏离/未接线/口径需收窄；`FAIL` 与设计不符且需返工；`NOT_AVAILABLE` 本次只读审计无法取得证据（写明原因，不猜） |

## 审计边界（本件**不**做什么）

1. **不重跑任何用例**：W07 的实跑数字（`775 passed / 0 failed`、全仓收集 `1121`）唯一来源是 `docs/w07/07-gate-review.md:39-40`（QA 实测），本件**不复制、不背书、不改写**；下文引用的测试均为「**tag 内存在 + 断言内容**」的静态证据，**不代表本件执行过**。
2. **不复核 GR-1..GR-4**：四条 AC 的门禁判定归 `docs/w07/07-gate-review.md` §2；本件按**设计 AC 与代码实现的对齐关系**独立立项。
3. **不裁决放行**：本件任何 `PASS` 都不构成放行依据（`07-gate-review.md` §6 的对外表述禁令对本件同样生效）。
4. **不连库、不写库**：控制库的实际 schema / 事件链 / 残留数据一律不查询（本机 `DG_ADMIN_DSN` 1045 已知不可用，见 `07-gate-review.md:111`），相关项登记为 `NOT_AVAILABLE`（§B）。
5. **不重复 GR-3 的参数还原核对**：`workers/discovery.py:160-193`（fail closed + `RECONCILIATION_REQUIRED` + `action="DO_NOT_EXECUTE"`）已由 `07-gate-review.md:53` 覆盖，本件不另立审计项。

---

## 结论总表

> 11 项逐条审计。**无 `FAIL`；无 `PARTIAL`；4 项附带限定口径或 `NOT_AVAILABLE` 旁注，均在「证据」列写明。**

| # | 审计项 | 结论 | 证据（`git show w07-close:<path>` 行号） | 不通过/偏差时的修复要求 |
|---|---|---|---|---|
| 1 | 事件哈希链未被污染（`event_hash()` 输入集不变） | **PASS** | `core/tasks/repository.py:66-89`：`event_hash()` 显式关键字参数 8 个、规范化字典 8 键（`task_id`/`run_id`/`tool_call_id`/`event_type`/`payload`/`created_at`/`seq`/`previous_event_hash`），`sort_keys=True` + `sha256`；`insert_event` `:401-447` 中 `tenant_id`/`actor_user_id` 只入列、**不入哈希**（docstring `:409-417` 明确禁止 payload 兜底）。**该文件自 `6176430`（W06-T05）后无提交**（`git log -1 -- core/tasks/repository.py`），即 W07 未触碰哈希输入 | 无。参见观察 O-1（「九字段」表述与实现的 8 键差异） |
| 2 | Worker 与 Gateway **同进程装配**（生产装配点唯一） | **PASS**（限定口径） | `workers/stack.py:188` `build_lab_stack(`→`:217` `gateway = ToolGateway(`→`:229` `worker = Worker(`，同一函数内装配、无跨进程端点；`git grep "ToolGateway(" w07-close -- workers/` 命中**仅** `workers/stack.py:217`；`tests/unit/test_worker_layering.py:83-89` `test_stack_is_the_only_module_building_the_gateway` 断言 `builders == ["stack.py"]` | 无。**口径必须限定为「`workers/` 内唯一 + 生产运行时装配点唯一，测试夹具另计」**——`adapters/lab_assembly.py:72` 是 `build_lab_stack` 定义处，测试里亦有多处 `ToolGateway(` 构造；写成「全仓唯一」不成立（同 `07-gate-review.md:56`） |
| 3 | Reconciler **结构上无法重放**（不持有 registry） | **PASS** | `core/execution/reconciler.py:1-8` 模块 docstring「**不持有任何工具注册表**，因此结构上不可能触发工具重放」；`:15-21` 导入清单只含审计/错误/证据仓储/任务模型/状态机，**无 registry、无执行器**；`:63-71` `__init__(step_records, task_repository, audit)` 三个依赖，无工具入口；`:73-82` `sweep()` 只扫「已 prepared 未 observed」并写事件/状态，`_reconcile_one` 不写 `step_observed` | 无 |
| 4 | `StepPrepared` 落库**早于**工具调用（门禁顺序 + D7 收紧） | **PASS** | `core/tools/executor.py:161-171` 先 `_prepare_step(...)`（内部 `:417-435` `insert_prepared(...)`），`:173` 才发「开始执行」事件、`:174` 落 tool_call，**首次**工具调用在 `:215` `self._invoke(...)`；D7：`:400-411` 写操作无 `ApprovalGrant` 即抛 `APPROVAL_REQUIRED` **且不落 `step_prepared`**（docstring `:393-399` 明确「删除 W06 的 warning 旁路」）；`_observe_step` 只在 `prepared` 为真时补（`:238-250`） | 无 |
| 5 | `PrepareProbe` **无 SQL 文本入参** | **PASS** | `core/approval/prerequisites.py:186-189` `PrepareProbePort.observe(self, request: PrepareRequest)` 唯一方法、唯一参数；`adapters/mysql/prepare_probe.py:1-11` 模块硬规则（无任何接受 SQL 文本的入口）；SQL 全部模块级常量 `:43-96`，标识符仅服务端常量 `ALLOWED_TARGET_DATABASES/ALLOWED_TARGET_TABLES`；`PREPARE_PROBE_SQL` `:102-111` 共 **8 条**常量，覆盖设计 `02-design.md:149-157` 的 **7 组**固定语句（第 ⑨ 组含 `SELECT CURRENT_USER…` 与 `SHOW GRANTS` 两条）；参数一律 `%s` 绑定。测试：`tests/unit/test_prerequisites_w07.py:343-357`（签名只允许 `request`；`PrepareRequest` 字段集 == `{environment_id, order_id, target_database, target_table}`，禁 `sql/query/statement/text`）、`:360-369`（`PrerequisiteProbe` 无接受 SQL 的公开方法）、`:384`（非白名单表名被拒） | 无。**对外口径应写「7 组 / 8 条」**（观察 O-2） |
| 6 | 披露快照三节判定（`verify_disclosure`） | **PASS** | `core/approval/disclosure.py:37` `DISCLOSURE_VERSION="w07-v1"`、`:226-236` `missing_disclosure_sections()`（三节缺失即列出）；`verify_disclosure()` `:238-288` 四类失败（`DIGEST_MISSING` / `KEYS_MISMATCH` / `NOT_CANONICAL` / `DIGEST_MISMATCH`）全部 `STATE_CONFLICT + reason=DISCLOSURE_TAMPERED`；三处调用面：写入侧 `ApprovalRepository.insert` 断言摘要非空（依据 `007_w07_disclosure.sql:6-7` 注释 + `tests/unit/test_disclosure_snapshot.py:266` `test_insert_requires_disclosure_digest`）、**读侧** `core/approval/repository.py:505`（`_to_approval` 内重算）、**消费锁内** `core/approval/service.py:813` → `:917-959` `_verify_locked_disclosure`；批准门禁 `service.py:353-386`（`disclosure.*` 缺失 → `DisclosureIncompleteError`，`reason=DISCLOSURE_INCOMPLETE`，`:368-382`） | 无 |
| 7 | `action_digest` 冻结值与 W06 基准一致、版本未升 | **PASS** | `core/approval/digest.py:24` `ACTION_DIGEST_VERSION = "w06-v1"`（**未升版**）、`:28` `DESCRIPTOR_KEYS` 未改；`tests/unit/test_action_digest.py:34` `BASE_ACTION_DIGEST = "f31f153767a028a758bb3ef02fb9322422e8fc0ff77c287ad459893f7a8d5bc2"`、`:90` `test_base_action_descriptor_is_byte_exact`，与 `docs/w06/05-freeze.md:135` **逐字节相同**；三节不进 `action_descriptor` 的设计约束见 `02-design.md:19/:788` | 无。`_CHECK_SOURCES` 增 `target_probe`（`digest.py:492`）为**增量允许值**，既有取值与摘要形态不变（`02-design.md:192` 预登记，观察 O-4） |
| 8 | `007` 迁移与回滚 | **PASS**（文件级） | `database/migrations/007_w07_disclosure.sql:9-14` 四列 + `idx_approvals_disclosure_digest`，注释 `:1-7` 说明「为什么允许 NULL」（W06 历史行不可回填，NOT NULL 只能造假）；`rollback/007_rollback.sql:12-19` 删索引/四列 + `DELETE … schema_migrations`，`:1-10` 明确**须以 `DG_ADMIN_DSN` 人工执行、不得纳入自动流程**；`docs/migrations.md:13` 版本表已登记、`:82` 回滚说明齐备 | 无。**控制库内该迁移是否已实际生效 = `NOT_AVAILABLE`**（本件不连库，见 §B N-1），不得据此宣称「已迁移」 |
| 9 | worker 网络收窄（配置层） | **PASS**（仅配置层静态事实） | `docker-compose.yml:151-153` worker `networks: [control_net, lab_net]`（**不含 `model_net`**）、`:145` `DG_MODEL_PROVIDER: disabled`、`:123-128` 注释声明「绝不接入 model_net」、`:130-132` `dockerfile: deploy/Dockerfile.worker`；CI 层断言 `tests/unit/test_worker_layering.py:145-151`（worker 块含 `control_net`/`lab_net`、不含 `model_net`、`DG_MODEL_PROVIDER: disabled`、`DG_GATEWAY_SIGNING_KEY` 在块内） | 无。**不得写成「容器网络隔离已验证」**：镜像构建与容器起栈/隔离运行态仍 `NOT_VERIFIED`（NV-1 三段式，`07-gate-review.md:76-81`）。另见观察 O-3（`tool_gateway` 占位仍挂 `lab_net`） |
| 10 | 未知结果语义（`UNKNOWN_OUTCOME` → `NEEDS_RECONCILIATION`，禁自动重试） | **PASS** | `core/tools/executor.py:537-548` 写操作 `TIMEOUT` → `UNKNOWN_OUTCOME`（并加 `write_timeout_outcome_unknown` 告警，**不解释为失败**）；事件映射 `:826` `UNKNOWN_OUTCOME → TOOL_CALL_UNKNOWN_OUTCOME`；重试闸 `:658-669`（`side_effect == "write"` → `False`，先于一切状态判断）；收敛 `core/gateway/tool_gateway.py:413-451` `_converge_unknown_outcome`（`can_transition` 门禁 + `error_code="UNKNOWN_OUTCOME"`）；任务侧 `core/tasks/service.py:401-415` 同向迁移；结果未知时**刻意不落** `step_observed`（`executor.py:238-250` 只在 `prepared` 且结果确定时补，docstring `:239-240`） | 无 |
| 11 | 审计链 `seq` 连续性 | **PASS**（代码 + DB 约束层） | `core/tasks/repository.py:422-447`：每轮 `SELECT MAX(seq)`→`seq = 0 if None else max+1`，同时取 `ORDER BY seq DESC LIMIT 1` 的 `event_hash` 作为 `previous_event_hash`，唯一键冲突时重算重试（`:33` `_EVENT_INSERT_ATTEMPTS = 3`，`:492` 记录 attempts）；DB 侧唯一键 `uq_events_task_seq (task_id, seq)` 见 `database/migrations/002_v22_evidence.sql:106-108`（列定义 `:80-83`、回填 `:85-104`） | 无。**历史链逐条复算未执行**（`NOT_AVAILABLE`，§B N-2） |

**总表结论**：11/11 `PASS`，其中 #2 / #8 / #9 / #11 附带限定口径或旁注 `NOT_AVAILABLE`；**无 `FAIL`、无 `PARTIAL`**。本结论**不改变** W07「有条件放行；门禁通过：否」。

---

## §A 逐项详情（仅对限定口径与旁注展开，PASS 项不重述）

### A.1 #2 「同进程装配」的证据边界

`builders == ["stack.py"]` 的检查范围是 `_py_sources(_WORKERS_DIR)`（`tests/unit/test_worker_layering.py:83-89`），**只覆盖 `workers/` 目录**。全仓其他位置的 `ToolGateway(` 构造分布在测试与 `adapters/lab_assembly.py`（`build_lab_stack` 的定义处）——这**不构成第二写入口**，因为生产运行时入口只有 `workers/stack.py` 一个函数。审计结论按此口径成立；若有人把「全仓唯一」写进对外材料，则**该表述本身不成立**。

### A.2 #5 「7 条」与「8 条」的对账

| 设计 `02-design.md:149-157` | 实现 `prepare_probe.py` 常量 | 条数 |
|---|---|---|
| ② `target_object_signature` | `_SQL_TARGET_SIGNATURE`（`:44-50`） | 1 |
| ③ `table_size` | `_SQL_TABLE_SIZE`（`:53-57`） | 1 |
| ④ `disk_headroom` | `_SQL_DATADIR`（`:60`）+ 本机 `shutil.disk_usage` | 1 |
| ⑤ `blocking_locks` | `_SQL_BLOCKING_LOCKS`（`:63-68`） | 1 |
| ⑥ `long_transactions` | `_SQL_LONG_TRANSACTIONS`（`:71-74`） | 1 |
| ⑦ `server_load` | `_SQL_SERVER_LOAD`（`:77-85`） | 1 |
| ⑨ `db_identity_privilege` | `_SQL_IDENTITY`（`:90-93`）+ `_SQL_SHOW_GRANTS`（`:96`） | 2 |
| **合计** | `PREPARE_PROBE_SQL`（`:102-111`） | **7 组 / 8 条** |

设计 `:157` 本身就把 `SHOW GRANTS` 写在同一组内，**实现与设计一致**；差异只在「7 条」的口语表述。**对外应写「7 组固定语句、8 条常量」，不得写「7 条 SQL 常量」**（观察 O-2）。

### A.3 #8 / #9 / #11 的「已核实」与「未核实」分界

| 层 | 已核实（本件） | 未核实（本件 `NOT_AVAILABLE`） |
|---|---|---|
| 迁移 007 | 文件内容、列/索引、回滚步骤、`docs/migrations.md` 登记 | 控制库 `dg_system.approvals` 是否已含四列（N-1） |
| 网络收窄 | compose 配置层（worker 块网络/环境变量/dockerfile）+ CI 断言存在 | 镜像构建、容器起栈、运行态网络隔离（N-4 / NV-1） |
| 审计链 | `seq` 取号逻辑、重试、DB 唯一键 | 控制库现有事件链逐条 `verify` 复算（N-2） |

---

## §B 本次无法核实项（`NOT_AVAILABLE`，逐条给原因，不猜）

| # | 项 | 状态 | 原因 |
|---|---|---|---|
| N-1 | 控制库 `dg_system.approvals` 是否已实际含 007 的四列与索引 | `NOT_AVAILABLE` | 本件为只读文档审计，不连库；本机 `DG_ADMIN_DSN` 返回 MySQL 1045（`07-gate-review.md:111`），`dg_ctl` 亦无 DDL 查询以外的必要授权。**迁移生效状态只能由运维以管理员账号确认** |
| N-2 | 控制库现有事件链的逐条哈希复算（`verify` / `scripts/audit_verify.py`） | `NOT_AVAILABLE` | 同上（需连库）；本件只核**代码侧不变量**与**DB 唯一键定义**。历史链核对结论归 W06 冻结口径（`docs/w06/05-freeze.md:179-194`） |
| N-3 | W07 测试四数（`775 passed / 0 failed / 0 skipped / 775 collected`）与全仓收集 `1121` | `NOT_AVAILABLE`（**不由本件产生**） | 本件未执行任何测试。该组数字的唯一来源是 QA 实测，登记于 `07-gate-review.md:39-40`；本件不复制、不背书、不改写 |
| N-4 | 容器镜像构建（`docker build -f deploy/Dockerfile.worker`）与容器起栈/隔离运行态 | `NOT_AVAILABLE` | NV-1 第 ② 段：**没跑过**，不是跑不了（`07-gate-review.md:76-81`、`02-design.md:852/:857`）。本件不做运行态操作 |
| N-5 | W07 期间的审批残留是否已清理 | `NOT_AVAILABLE`（且**事实为「未清理」**） | 7 条残留为已登记事实（`07-gate-review.md:107-112`、`docs/w07/worker-residue.md`）；本件不查询、不清理。**不得表述为「已清理」** |

---

## §C 附加观察（不阻断，登记备查）

| # | 观察 | 说明与建议 |
|---|---|---|
| O-1 | `event_hash()` 的「九字段」表述 | 实现为 **8 个关键字参数 / 8 键**（`core/tasks/repository.py:66-89`）；`docs/w06/05-freeze.md:13` 称「哈希输入九字段固定」但只列出 8 个名字。**不影响冻结**（该函数自 `6176430` 未变），但术语应统一为「**八键哈希输入 + 不可变约束**」，或在文档里补足第九项的准确所指。建议由文档 owner（架构侧）在下一周收口时修正，**本件不代改 W06 文档** |
| O-2 | 「7 条固定 SQL」 | 实际为 **7 组 / 8 条**常量（§A.2）。对外与验收材料应写「7 组」。 |
| O-3 | `tool_gateway` 占位服务仍挂 `lab_net` | `docker-compose.yml:156-162` 的占位块 `networks: [control_net, lab_net]`。这是 KD-2（`02-design.md:866-872`：按 team-lead 明文指令保留、`profiles: ["future"]`、默认 `up` 不启动）。**#9 的结论只覆盖 `worker` 块**，不得扩写为「compose 全栈已无 lab_net 暴露」。 |
| O-4 | `_CHECK_SOURCES` 增量 | `core/approval/digest.py:492` 由 2 项扩为 3 项（+`target_probe`）。设计 `02-design.md:192` 已预登记为增量允许值；W06 冻结向量的**输入**不变，故 `PASS`。若未来又加来源而未同步 `models.CheckSource`，会导致 `prerequisites_digest` 校验路径出现双常量不一致——建议加一条"两处常量必须相等"的断言（本轮未要求）。 |
| O-5 | 设计文档在 tag 与工作区一致 | `docs/w07/` 六个文件在 tag 与工作区**逐文件行数相同**且 `git diff --name-only w07-close -- docs/w07/` 为空 → 本件引用的设计行号对 tag 与当前工作区均成立（其余 `docs/w08`–`docs/w12` 有差异，与本件无关）。 |

---

## §D 与 `07-gate-review.md` 的关系（不得互相替代）

| 维度 | `docs/w07/07-gate-review.md` | 本件 |
|---|---|---|
| 性质 | **门禁复核**（治理判定件） | **实施期符合性审计**（实现 ↔ 设计对齐） |
| 结论 | GR-0 唯一结论行：「**有条件放行（QA 最终复核确认，2026-11-11 23:56）**」；双层口径「代码完整：是 / 门禁通过：否」 | 11 项 `PASS` + 5 项 `NOT_AVAILABLE` 登记；**不含任何放行判定** |
| 组织方式 | GR-1..GR-4（四条 AC）＋ C-1..C-4（放行条件）＋ NV/KD | 按设计 AC 与代码不变量立项（11 项），面向上游设计与 PRD |
| 时序 | 2026-11-11/12 产出，位于 `tag w07-close` **内** | 2026-11-17 补记，**不在** tag 内 |
| 替代性 | 不可被本件替代：本件不做门禁判定 | 不可被本件替代：`07-gate-review.md` 不做逐项实现对齐 |

**两件并存时的正确引用方式**：

- 问「W07 门禁状态」→ 引 `07-gate-review.md:11`（有条件放行；门禁通过：否）。
- 问「W07 实现是否符合设计」→ 引本件结论总表。
- 问「W07 冻结向量是什么」→ 引 `docs/w07/05-freeze.md` §2 + `docs/w06/05-freeze.md`。
- **禁止**用本件的 11 项 `PASS` 推导「W07 门禁通过」——两者判定维度不同，且 NV-1/KD-1..KD-5 与 7 条残留事实均未因本件消除。
