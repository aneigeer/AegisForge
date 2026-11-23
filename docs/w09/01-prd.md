# W09 增量 PRD｜报告、脱敏与记忆（把诊断结果变成可解释、敏感数据可控的报告）

> 本文是 V2.2 12 周计划的第 9 周**增量需求基线**，供架构设计与实现使用。
> 口径约定：§4「现状」引用的 `file:line` 均为**已核实的既存文件**；§4/§5「验收位置 / 测试位置」中的测试文件为**本周待新建**，路径按 `tests/unit/`、`tests/integration/` 既有命名约定给出，非既存文件。二者不得混读。

## 0. 项目信息

| 项 | 值 |
|---|---|
| Language | 中文 |
| Programming Language | Python 3.13/3.14 + FastAPI + MySQL 8 控制库 + MySQL LAB 目标库 + PostgreSQL MCP 只读（**延续 V2.2 既有栈；不新增前端、不新增第三方依赖，沿用 `requirements.lock.txt`**） |
| Project Name | `dbguardian_w09_report_redaction_memory` |
| 依据 | `项目规划/项目规划2.2/DBGuardian_W09_报告、脱敏与记忆.docx` §一–§六；`项目规划/项目规划2.2/DBGuardian_12周研发计划_总览与门禁.docx` §二/§四/§五/§六；上游 `docs/w08/01-prd.md`、`docs/w08/02-design.md`、`docs/w08/08-drill-report.md`、`docs/w07/07-gate-review.md` GR-0；代码基线（已核实）`core/evidence/models.py`、`core/context/builder.py`、`core/audit/events.py`、`core/logging.py`、`core/config.py`、`core/models/gateway.py`、`database/migrations/003_evidence_redaction_hash.sql` |
| 前置门禁 | **W08 门禁尚在复核、未裁定**：W08 T01–T06 代码已实现、单元回归 `891 passed / 0 failed`（`docs/w08/08-drill-report.md:16`），但 QA 最终全量回归与 W8→W9 门禁复核进行中，`docs/w08/06-qa-report.md` 与 `docs/w08/07-gate-review.md` **均未落盘**（`docs/w08/` 现有仅 `01-prd.md`/`02-design.md`/`03-task-cards.md`/`08-drill-report.md`）。**不得**写成「W08 已通过」。W07 门禁结论为「有条件放行（GR-0）」，未验证项与已知偏离见 §8 |
| 前置依赖（环境阻塞，如实登记） | ① PostgreSQL 5432 拒绝连接（`docs/w08/08-drill-report.md:165`）；② `DG_ADMIN_DSN`（root）认证失败 1045、只追加表无 `DELETE` 授权无法清理（`docs/w08/08-drill-report.md:166`、`docs/w07/07-gate-review.md:111`）；③ 控制库存在历史任务堆积（主理人任务书登记 830+ 行；逐条残留见 `docs/w07/worker-residue.md`，实测待核对候选 72 条见 `docs/w08/08-drill-report.md:193`）。**本周不依赖 PG 写路径、不依赖 admin 清理、不以「环境可清零」为前提** |
| 原始需求复述 | 任务书「本周目标」原句：「实现 DataRedactionEngine、结构化诊断报告、五类记忆的最小可用实现与 Memory Conflict Resolver；完善模型不可用/输出异常时的规则化降级。」 |

## 1. 产品目标

**一句话**：把 W08「能被独立验证的一次写」向上游与下游延伸为「**进入模型/证据/报告/记忆的每一份数据都先脱敏、每个事实结论都绑得到 evidence_id、每条知识都可审核可撤销可过期、模型不可用时能安全降级**」。

三个正交目标（判定口径均为可验证事实）：

| # | 目标 | 判定口径（可机器验证） |
|---|---|---|
| G1 | **敏感数据不越界** | 任一含敏感样本（见 AC-01 样本集）进入「模型上下文 / Evidence 持久化 / 报告 / CSV·JSON 导出 / 训练样本」五条通道前，脱敏函数返回 `status != PASSTHROUGH`；无法可靠脱敏时返回 `REVIEW_REQUIRED` 或 `BLOCKED` 且该载荷**不进入**下游通道（以通道入口断言 `redaction_result.status` 为准） |
| G2 | **报告不造假** | 报告内每个「事实结论」字段的 `evidence_id` 必须能在 Evidence Store 中解析到同 `tenant_id`/`task_id`/`environment_id` 的记录；引用失效/缺失/状态为 `PARTIAL`·`STALE`·`DENIED`·`UNKNOWN` 时报告对应项标为该状态，**不呈现为已确认事实**；「建议」「等待审批」「已执行」「结果未知」「已验证」五态互不等价、不可由前者推导后者 |
| G3 | **知识不越权** | 五类记忆中只有 `Semantic` 类经 `reviewed_by` + `validation_level` + 有效期校验后才可进入正式索引；冲突组条目在重新验证前 `status=CONFLICTED` 且**不参与**工具授权决策；记忆对象**结构上不持有** tool registry / gateway / DSN（签名检查），即无法直接触发工具调用 |

## 2. 本周边界（先划死，防止范围蔓延）

| 边界 | 内容 |
|---|---|
| 做什么 | ① 统一脱敏入口 `DataRedactionEngine` + `RedactionResult`；② 脱敏接入 Adapter 结果 / Evidence Store / Context Builder / 模型输入 / 报告 / 导出 / 训练样本生成；③ 结构化报告模板 + `evidence_id` 引用校验 + Markdown/HTML 安全渲染；④ 五类记忆 + `KnowledgeEntry` + `Memory Conflict Resolver`；⑤ 模型不可用/输出异常的规则化降级；⑥ 脱敏回归样本集、知识撤销/过期/冲突测试、报告样例 |
| 写能力 | **不扩大写能力**。全仓唯一 R2 动作仍是 `mysql_lab_update_order_note` / `1.0`（`adapters/mysql/lab_repair.py`），本周不新增写工具、不改其 `TOOL_NAME`/`ALLOWED_*`/三条断言 |
| 引擎写路径 | **不做第二引擎写路径**、**不做 PostgreSQL 写路径**；PG 保持 MCP 只读（延续 `docs/w07/07-gate-review.md:90` NV-3：写工具只在 `LAB + mysql` 注册，属设计范围外） |
| 第三方依赖 | **不引入新的第三方依赖**；沿用 `requirements.lock.txt`。脱敏正则、报告渲染、冲突过滤一律用标准库；`docs/w07/02-design.md:725` 已明确不引入 `apscheduler`/`prometheus_client`/`psutil`，本周延续 |
| 安全机制 | **不绕过审批 / 证据 / 审计 / 验证机制**：脱敏不得替代 Evidence 归属校验、不得替代审批、`redact_payload` 审计链路（`core/audit/events.py:122`）保持不变；报告不得把「建议」写成「已修复」；记忆不得授权执行 |
| 如实返回状态 | 遇到**不支持 / 权限不足 / 结果未知**必须如实返回状态：脱敏不可靠 → `REVIEW_REQUIRED`/`BLOCKED`；证据引用失效 → `STALE`/`UNKNOWN`；应用池指标缺失（W10 前置）→ `UNKNOWN`；知识无匹配 → 空集 + `NOT_FOUND`，不得以相似度硬凑；模型不可用 → 暂停新规划并降级，不伪造结果 |
| 前端 | **不新增前端交付项**（延续 W06/W07/W08 界定）；报告以结构化对象 + 纯文本/安全渲染产物为准 |
| 训练 | 本周**不做模型训练**；SFT/微调仅在 W10 Baseline 报告发现可定位短板后按条件决定，14B 完整训练延期到 Alpha 之后（见《总览》§五优先裁剪顺序） |

## 3. 用户故事

| # | 角色 | 故事 |
|---|---|---|
| U1 | 审计员（Auditor） | 作为审计员，我要看到每份报告里每个事实结论引用的 `evidence_id` 都能回溯到同租户/任务/环境的原始证据，且能区分「支持证据/反证/未知项」，这样我能独立复核结论有没有被拔高。 |
| U2 | 数据合规 / 安全评审 | 作为安全评审，我要证明手机号、邮箱、身份证、连接串、Token、私钥等敏感数据在**进入模型上下文与 Evidence 持久化之前**就被处理，而不是只在报告显示时打码；无法可靠脱敏的内容被隔离而不是流入训练集。 |
| U3 | 审批人（Approver） | 作为审批人，我要报告严格区分「建议」「等待审批」「已执行」「结果未知」「已验证」，绝不把「建议修复」呈现成「已修复」，这样我批准的动作与报告口径一致。 |
| U4 | 运维 / 知识管理员 | 作为运维，我要给每条知识条目登记引擎/版本范围、Adapter 版本、Evidence IDs、审核人、有效期与撤销时间；过期或冲突的知识自动退出可用集，并保留版本与撤销原因供追溯。 |
| U5 | 值班 SRE | 作为值班 SRE，我要在模型服务不可用或输出非法时，系统暂停新规划、基于已有 Evidence 生成规则化简报、最多做一次修复性重试，且全程不绕过策略、不访问数据库凭据。 |
| U6 | 平台工程师 | 作为平台工程师，我要 `Reconciler.sweep()` 的真实扫描总数能被单独观测（W08 演练报告 §1.3 第 4 条点名 W09 承接），这样长稳阶段的核对指标不是被截断的「产出结论条数」。 |

## 4. 需求池

> **P0 为 W9→W10 门禁必需，不得降级、不得延期**。每条 P0 给出 `[D#]`（任务书五日执行清单）、已核实的现状、可实施要求、**验收位置（测试文件路径 + 断言形式）**。
> `file:line` 现状引用已用 Grep/Read 核实；「当前无该能力」以全仓检索无代码实现为依据（仅 `docs/w07/08-pm-ruling.md:150` 把「报告/脱敏/记忆」列为未来项）。

### P0（Must have，6 条）

- **P0-W09-01** `[D1]` **`DataRedactionEngine` + `RedactionResult` 统一脱敏入口。**
  - 现状（已核实）：仅有分散的轻量脱敏——`core/logging.py:27` `redact_text` 用两条正则 `_DSN_PATTERN`（`:12-14`）、`_PASSWORD_PATTERN`（`:15-17`）屏蔽 DSN 口令与 password/secret/token 字段；`core/audit/events.py:122` `redact_payload` 按键名归一化子串遮蔽为 `<redacted>`（`:138-139`）+ 类型兜底（`:155`）+ 三项上限（`:38` `MAX_AUDIT_ITEMS=50`）。**全仓无 `DataRedactionEngine` / `RedactionResult` / `redaction_version` 规则版本化实现**；`core/evidence/models.py:61` 已有 `redaction_version: str = "none"` 字段但当前恒为 `none`。
  - 要求：
    1. 单一入口覆盖 12 类：SQL 字面量、手机号、邮箱、身份证号、姓名/业务标识、连接串、用户名密码、Token、API Key、私钥、证书、日志字段；每类有**版本化规则**与可枚举的 `detected_categories`。
    2. `RedactionResult` 至少含：`payload_redacted`、`redaction_version`、`detected_categories`、`redaction_count`、`unresolved_sensitive_spans`、`payload_hash`、`status ∈ {CLEAN, REDACTED, REVIEW_REQUIRED, BLOCKED}`；`payload_hash` 必须对**脱敏前**载荷计算（与 `core/evidence/models.py:59` `payload_hash` 语义一致），脱敏后另存 `redacted_payload_hash`（`:60`，迁移见 `database/migrations/003_evidence_redaction_hash.sql:2-3`）。
    3. 无法可靠脱敏（规则未覆盖、嵌套结构超深度、二进制/未知编码）→ `REVIEW_REQUIRED` 或 `BLOCKED`，**不得**返回 `REDACTED` 冒充已处理。
    4. 规则必须覆盖多语言、SQL 注释、`\` 转义、连接串、嵌套 JSON 五类回归样本；`status` 判定与 `redaction_count` 对同一输入确定性可复现。
  - **验收位置**：`tests/unit/test_w09_redaction_engine.py`（新建）——① 12 类样本逐类 `detected_categories` 含对应类别且 `payload_redacted` 不含原敏感串；② 同一输入连跑 5 次 `redaction_count` / `redaction_version` / `payload_hash` 集合大小各为 1；③ 构造规则未覆盖的敏感片段 → `status in {"REVIEW_REQUIRED","BLOCKED"}`；④ `payload_hash == sha256(脱敏前 canonical)` 且 `!= sha256(payload_redacted)`；⑤ `RedactionResult` 注入多余字段 → `ValidationError`（`extra="forbid"`）。

- **P0-W09-02** `[D2]` **全链路脱敏接入：Adapter 结果 → Evidence 持久化 → Context Builder → 模型输入 → 报告 → CSV/JSON 导出 → 训练样本生成。**
  - 现状（已核实）：`core/context/builder.py:84` `summarize_result` 已用 `redact_text(canonical)` 先脱敏再截断（`:85-86`），`:95` 对 warnings 脱敏，`:102` `render_messages` 构造模型输入；`core/tools/executor.py:428/468/520/717/743/779` 与 `core/agent/trajectory.py:78/86` 已调用 `redact_payload`；`core/evidence/models.py:63-64` 已有 `payload` / `redacted_payload` 双字段。**但这些是各自调用 `redact_text`/`redact_payload`，不是统一 `DataRedactionEngine` 出口，也没有「未脱敏不得持久化/不得进模型」的强制闸门**。
  - 要求：
    1. 在 Evidence 持久化（`core/evidence/service.py`）与模型输入（`core/context/builder.py:102` 之前）两处设**强制闸门**：入口断言 `RedactionResult.status in {CLEAN, REDACTED}`，否则拒绝写入/拒绝渲染并落审计事件；`REVIEW_REQUIRED`/`BLOCKED` 载荷隔离，不进入模型、训练集、导出。
    2. 报告、CSV/JSON 导出、训练样本生成三处**再次扫描**（不是复用上游结果），防止二次拼接引入敏感串。
    3. 原始载荷确需保留时须有明确授权标记、加密、最小访问与保留期限字段；普通日志（`core/logging.py`）**不得**输出原始敏感数据。
    4. 验证脱敏前后 `payload_hash` / `redaction_version` 与审计引用的对应关系（`core/evidence/models.py:59-61` / `:64`）。
  - **验收位置**：`tests/integration/test_w09_redaction_pipeline.py`（新建）——① 在 Evidence 持久化入口注入 `status=BLOCKED` → 抛领域错误且 `evidence` 表无新增行；② 在 `render_messages` 前注入未脱敏载荷 → 模型输入中不含原串且审计含 `REDACTION_BLOCKED`；③ 导出端点对已脱敏报告再拼入手机号 → 导出文件不含该号；④ `task_events` 中 `redaction_version` 与 Evidence 行一致。

- **P0-W09-03** `[D3]` **结构化报告模板 + `evidence_id` 引用校验 + 状态五分 + 安全渲染。**
  - 现状（已核实）：`core/evidence/models.py:102-111` 已定义 `ReportFacts`（`supporting_evidence_ids` / `contradicting_evidence_ids` / `missing_evidence`）、`:94-99` `EvidenceRef`（`supports` / `contradicts`）、`:17-23` `CollectionStatus`（含 `PARTIAL`/`DENIED`/`NOT_SUPPORTED`/`UNKNOWN`）；`benchmarks/baseline_v0/run_baseline.py:73-86` `PROMPT_REQUIRED_TERMS` 已要求模型输出含 `evidence_id`/`supporting_evidence_ids`/`contradicting_evidence_ids`/`missing_evidence`/`confidence_band`。**全仓无报告模板实现、无 `evidence_id` 归属解析校验、无「建议/已执行/已验证」状态机字段**。
  - 要求：
    1. 报告模板字段：任务/数据库/版本/环境指纹、结论与置信等级、支持证据、反证、未知项、建议、执行状态、验证状态、适用范围；状态严格区分「建议 / 未执行 / 等待审批 / 已执行 / 结果未知 / 已验证」，禁止把建议写成已修复。
    2. 每个事实结论必须引用有效 `evidence_id`：解析时校验同 `tenant_id`/`task_id`/`environment_id`（`core/evidence/models.py:43-48`），失效/缺失标 `STALE`/`UNKNOWN`；`CollectionStatus ∈ {PARTIAL, DENIED, UNKNOWN, NOT_SUPPORTED}` 的证据不得作为「已确认事实」；应用指标缺失须显式标注 `UNKNOWN`。
    3. 模型生成的 Markdown/HTML 做纯文本化或安全渲染（转义/白名单标签），不得原样输出可执行内容。
  - **验收位置**：`tests/unit/test_w09_report_schema.py`（新建）——① 报告模型中「建议」字段注入「已修复」措辞 → `ValidationError`；② 引用 `evidence_id` 与报告 `tenant_id` 不一致 → `EVIDENCE_REF_INVALID`；③ `CollectionStatus=PARTIAL` 的证据被引用时结论 `status != CONFIRMED`；④ 六态枚举两两不等价断言。`tests/integration/test_w09_report_evidence_ref.py`（新建）——构造含 `<script>` 的模型输出 → 渲染结果不含 `<script>`；失效 `evidence_id` → 报告含 `STALE` 且 `missing_evidence` 非空。

- **P0-W09-04** `[D4]` **五类记忆 + `KnowledgeEntry` + `Memory Conflict Resolver`。**
  - 现状（已核实）：**全仓无 `KnowledgeEntry` / `ConflictResolver` / 五类记忆实现**（Grep 仅命中 `docs/w07/08-pm-ruling.md:150` 未来项描述）；`core/config.py:36` `database_id`、`:44` `pg_database_id`、`:98` `policy_version` 可作为 Environment 类记忆的版本来源；`core/evidence/models.py:50-51` `db_version`/`adapter_version` 可作引擎/版本范围口径。
  - 要求：
    1. 建立 `Working` / `Episodic` / `Semantic` / `Procedural` / `Environment` 五类：任务工作态、任务时间线、已验证知识、SOP/工具约束、环境身份与能力；五类枚举封闭。
    2. `KnowledgeEntry` 含：数据库引擎/版本范围、Adapter 版本、`evidence_ids`、测试用例、`validation_level`、`reviewed_by`、有效期、撤销时间、来源许可；未审核条目不得进入正式索引。
    3. `Memory Conflict Resolver` **先硬过滤**（引擎 / 主版本 / Adapter / 能力 / 环境 / 风险 / 状态 / 有效期）**再比较**证据等级与独立性；冲突条目进入冲突组、标 `CONFLICTED` 并要求当前工具重新验证；撤销/过期保留版本与原因；**记忆只能提示，不得授权执行**（对象不持有 registry/gateway/DSN，签名检查）。
  - **验收位置**：`tests/unit/test_w09_memory_models.py`（新建）——① 五类枚举集合大小 ==5 且 `extra="forbid"`；② `KnowledgeEntry` 缺 `reviewed_by` 或已过期 → `is_indexable is False`；③ 硬过滤：引擎 `postgresql` 的知识在 `mysql` 任务上不返回（不因向量/文本相似度命中）；④ 冲突两条 → 均 `status=CONFLICTED` 且不出现在可用集；⑤ `inspect.signature` 断言记忆对象无 `tool_registry`/`gateway`/`dsn` 形参。`tests/integration/test_w09_conflict_resolver.py`（新建）——撤销后条目仍可按 `knowledge_id` 查到且带 `revoked_reason`。

- **P0-W09-05** `[D5]` **模型不可用 / 输出异常的规则化降级。**
  - 现状（已核实）：`core/config.py:66` `model_provider: Literal["fake","openai_compat","disabled"]="fake"`、`:70` `model_timeout_s`；`core/models/gateway.py:105` `generate_structured` 对非法 JSON/结构抛 `ModelOutputInvalidError`（`:128/:160`）、`:173` `tool_call` 默认 `allow_write=False`（`:179`）；`core/recovery/controller.py` 已实现「非法输出最多一次修复性重试」与 `UNKNOWN` 核对（W08 AC-14/AC-16）；`adapters/mysql/adapter.py:158`、`database/mysql/capabilities.py:1` 已有「能力缺失时明确降级、不返回假数据」的既有范例。**全仓无「模型不可用时暂停新规划 + 基于已有 Evidence 生成规则化简报 + 质量回归路由回前一验证版本」实现**。
  - 要求：
    1. `model_provider=disabled` 或调用超时/失败 → 暂停新规划，允许基于已有 Evidence 生成规则化简报；简报字段须标 `degraded=true` 与 `degrade_reason`，不得声称「模型已诊断」。
    2. 结构化输出非法：最多一次修复性重试（复用 `core/recovery/controller.py` 的一次重试语义）；第二次仍非法 → 终止，不得第三次。
    3. 模型质量回归 → 路由回前一验证版本（版本标识来自冻结的 `model_version` / `prompt_version`，见 `benchmarks/baseline_v0/run_baseline.py:20-34` 字段集）；模型不得访问数据库凭据或直接执行工具（延续 `core/models/gateway.py:179` `allow_write=False`）。
    4. 降级全程不绕过策略、审批、审计、验证；降级事件可审计。
  - **验收位置**：`tests/unit/test_w09_model_degradation.py`（新建）——① `model_provider=disabled` → `planning_suspended is True` 且简报 `degraded is True`；② 连续两次非法输出 → `retry_count == 1` 且终止，**无第 3 次调用**（mock 计数）；③ 降级简报的 `model_used is False`；④ `inspect.signature` 断言模型输入构造不接收 `ro_dsn`/`rw_dsn`/`control_dsn`。`tests/integration/test_w09_degrade_audit.py`（新建）——降级路径下 `task_events` 含 `MODEL_DEGRADED` 且 `tool_calls` 增量 0。

- **P0-W09-06** `[D1/D2 补强]` **脱敏回归样本集 + 阻断效果测试 + `sweep()` 真实扫描总数可观测（W08 点名承接）。**
  - 现状（已核实）：`tests/unit/test_audit_events.py:62/70/83/94/99/108/118/134` 已有 `redact_payload` 的键名遮蔽与截断单测，可作为回归基线；`docs/w08/08-drill-report.md:76-78` §1.3 第 4 条明确「`scanned = len(results)` 是产出结论条数，不是全表扫描行数；**真实扫描总数需 W09 扩展 `sweep()` 的返回值**」；`workers/runner.py` 的 `RECONCILIATION_SWEPT` 事件当前 payload 取自 `sweep()` 返回值（`docs/w08/08-drill-report.md:37-39`）。
  - 要求：
    1. 建立版本化脱敏回归样本集：多语言、SQL 注释、转义、连接串、嵌套 JSON 五类，逐类「原始含敏感 / 脱敏后不含 / `detected_categories` 正确」三断言；样本集带 `redaction_version`。
    2. 测试敏感数据进入模型/报告/训练集的**阻断效果**（与 P0-W09-02 闸门联动）。
    3. 扩展 `Reconciler.sweep()` 返回值，分离 `scanned_total`（真实扫描行数）与 `conclusions`（产出结论条数）；`RECONCILIATION_SWEPT` 事件的 `scanned` 口径由架构师在设计裁定，但**不得**再把 `len(results)` 当作全表扫描数；改动不得破坏 `docs/w08/08-drill-report.md:59` 既有 `test_sweep_success_emits_reconciliation_swept` 断言。
    4. 整理报告样例、脱敏规则版本与记忆维护说明（文档落点 `docs/w09/`，本周仅产出 PRD 之外的说明由实现阶段补齐）。
  - **验收位置**：`tests/unit/test_w09_redaction_samples.py`（新建）——五类样本各 1 例三断言；样本集 `redaction_version` 非空且与引擎一致。`tests/unit/test_w09_sweep_scanned.py`（新建）——构造 5 行候选仅 2 行产出结论 → `scanned_total == 5` 且 `len(conclusions) == 2`；`tests/unit/test_worker_scheduler.py`（既存，扩展）原 `scanned=2` 断言按新口径更新且仍绿。

### P1（本周期内可做，不进门禁）

- **P1-W09-01** `[D3]` 报告导出为 CSV/JSON 的字段白名单与列头固定化。**AC**：`tests/unit/test_w09_report_export.py`（新建）导出列名集合与冻结清单逐字相等。
- **P1-W09-02** `[D4]` 知识条目的 `evidence_ids` 批量归属校验工具。**AC**：`tests/unit/test_w09_knowledge_refs.py`（新建）跨租户 `evidence_id` 被拒。
- **P1-W09-03** `[D2]` 原始载荷保留的授权/加密/期限字段模型（不启用持久化）。**AC**：模型字段存在且默认 `retention_allowed is False`。

### P2（明确延期，不进 W09 门禁）

- **P2-W09-01** 向量相似度检索与知识关系网络（《总览》§五「可收窄」）。
- **P2-W09-02** 只读报告查询 API `GET /v1/tasks/{id}/report`（`docs/w08/01-prd.md` P2 已登记，待前端与报告需要时）。
- **P2-W09-03** 真实 WORM/对象锁接入（`docs/w08/01-prd.md` P2 已登记 W11）与多级/会签知识审批。

## 5. 验收标准 AC（可机器验证）

> 每条落到「测试文件 + 具体断言」，不接受「全链路可复现」「脱敏良好」这类松标准。测试文件均为本周待新建（或既存扩展），已在「测试位置」列注明。

| # | 判据 | 测试位置 | 关键断言 |
|---|---|---|---|
| AC-01 | 12 类敏感样本全命中 | `tests/unit/test_w09_redaction_engine.py`（新建） | 12 类逐类 `category in detected_categories` 且 `raw_secret not in payload_redacted` |
| AC-02 | 脱敏确定性 | 同上 | 同一输入 5 轮：`{redaction_count}`/`{redaction_version}`/`{payload_hash}` 集合大小各 ==1 |
| AC-03 | 不可靠脱敏不冒充 | 同上 | 规则未覆盖样本 → `status in {"REVIEW_REQUIRED","BLOCKED"}`，且 `!= "REDACTED"` |
| AC-04 | 哈希口径正确 | 同上 | `payload_hash == sha256(canonical(原始))`；`payload_hash != sha256(payload_redacted)`；模型 `extra="forbid"` |
| AC-05 | 持久化前强制闸门 | `tests/integration/test_w09_redaction_pipeline.py`（新建） | 注入 `status=BLOCKED` → 抛领域错误且 `evidence` 表新增行数 ==0 |
| AC-06 | 模型输入前强制闸门 | 同上 | 未脱敏载荷渲染前被拒；`user_content` 不含原串；`task_events` 含 `REDACTION_BLOCKED` |
| AC-07 | 导出/训练集再扫描 | 同上 | 导出后再拼手机号 → 导出文件不含该号；训练样本生成器出口同样不含 |
| AC-08 | 审计引用一致 | 同上 | Evidence 行 `redaction_version` 与对应 `task_events.payload.redaction_version` 相等 |
| AC-09 | 引用归属校验 | `tests/unit/test_w09_report_schema.py`（新建） | 跨 `tenant_id` 的 `evidence_id` → `EVIDENCE_REF_INVALID`；六态枚举两两不等价 |
| AC-10 | 弱证据不升格 | 同上 | `CollectionStatus=PARTIAL`/`DENIED`/`UNKNOWN` 被引用 → 结论 `status != CONFIRMED` |
| AC-11 | 报告措辞不可混写 | 同上 | 「建议」字段含「已修复」→ `ValidationError`；「等待审批」≠「已执行」 |
| AC-12 | Markdown/HTML 安全渲染 | `tests/integration/test_w09_report_evidence_ref.py`（新建） | 模型输出含 `<script>` → 渲染结果不含 `<script>`；失效 `evidence_id` → 报告含 `STALE` 且 `missing_evidence` 非空 |
| AC-13 | 五类记忆与不可索引 | `tests/unit/test_w09_memory_models.py`（新建） | 五类枚举大小 ==5；缺 `reviewed_by` 或已过期 → `is_indexable is False` |
| AC-14 | 冲突先硬过滤 | 同上 + `tests/integration/test_w09_conflict_resolver.py`（新建） | `postgresql` 知识在 `mysql` 任务返回空集；冲突两条均 `CONFLICTED` 且不进可用集；撤销后可按 `knowledge_id` 查且带 `revoked_reason` |
| AC-15 | 记忆不授权执行 | `tests/unit/test_w09_memory_models.py` | `inspect.signature` 中无 `tool_registry`/`gateway`/`dsn`/`rw_dsn` 形参 |
| AC-16 | 模型降级语义 | `tests/unit/test_w09_model_degradation.py`（新建） | `disabled` → `planning_suspended is True` 且简报 `degraded is True`、`model_used is False` |
| AC-17 | 非法输出只重试一次 | 同上 | 连续两次非法 → `retry_count == 1`，`adapter.complete` 调用次数 ==2（无第 3 次） |
| AC-18 | 降级不触凭据/不重放 | `tests/integration/test_w09_degrade_audit.py`（新建） | 模型输入构造签名不含 DSN 形参；降级路径 `tool_calls` 增量 ==0；`task_events` 含 `MODEL_DEGRADED` |
| AC-19 | 脱敏回归样本集 | `tests/unit/test_w09_redaction_samples.py`（新建） | 五类样本各「原始含/脱敏后不含/类别正确」三断言全绿；样本 `redaction_version` 非空 |
| AC-20 | `sweep()` 扫描总数可观测 | `tests/unit/test_w09_sweep_scanned.py`（新建）+ `tests/unit/test_worker_scheduler.py`（既存扩展） | 5 行候选 2 行结论 → `scanned_total == 5` 且 `len(conclusions) == 2`；既存 `test_sweep_success_emits_reconciliation_swept` 仍绿 |
| AC-21 | 写面与引擎不回退 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py`（既存） | PG 工具集仍 7 个且全 `side_effect=="read"`；生产装配 write 工具集合仍为 `LAB+mysql` 单一工具；无第二个写动作 |

## 6. 周末门禁与完成定义

**W9 → W10 门禁（浓缩任务书 §六，逐条可判）**：
1. 敏感数据门禁通过：AC-01～AC-08、AC-19 全绿（脱敏在进入模型上下文与 Evidence 持久化前执行；导出/报告/训练集再次扫描；无法可靠脱敏被隔离或阻断）。
2. 报告证据引用有效：AC-09～AC-12 全绿（每个事实结论引用有效 `evidence_id`，含支持证据、反证、未知项、执行状态、验证状态和适用范围）。
3. 知识可审核、过期、撤销和冲突处理：AC-13～AC-15 全绿。
4. 降级不绕过策略：AC-16～AC-18、AC-21 全绿。
5. W08 点名承接项：`sweep()` 真实扫描总数可观测（AC-20）全绿；未绿则 `docs/w08/` 的 W8→W9 交接件不得标注该项已完成（`docs/w08/08-drill-report.md:76-78` §1.3 第 4 条）。

**未通过处置**：
- 门禁任一项未过 → **先修复 W09 前置模块，不并行堆叠 W10 功能**（《总览》§六「若前置门禁未通过，先修复前置模块」）；W10 的 Benchmark/数据集工作不得在脱敏闸门未闭合前消费未脱敏样本。
- 若 W08 门禁复核最终未通过（当前尚在复核），**先按 `docs/w08/` 门禁件的处置缩小修复范围或仅发布诊断预览**，W09 门禁不得替代 W08 门禁证据。
- 已知环境阻塞（PG 5432 / admin 1045 / 只追加表残留）不因本周工作而消失，须在交接说明中如实登记，不得表述为「环境已可复现清零」（`docs/w07/07-gate-review.md:135` 表述禁令）。

**本周完成定义（任务书 §六）**：交付物已提交到仓库；关键测试有自动化结果或可复现记录；未通过项已登记为问题并说明影响；**不得仅以「代码已写完」作为完成标准**。

## 7. 待确认问题 Q

| # | 问题 | 我的建议 | 需要谁定 |
|---|---|---|---|
| Q1 | **W08 门禁未复核**：`docs/w08/06-qa-report.md` 与 `07-gate-review.md` 均未落盘，W09 是否可先启动设计与非门禁项？ | **建议**：先产出架构设计与脱敏回归样本集（不触碰写路径、不消费生产数据），但**不宣布 W09 开工、不以「W08 已通过」为前提**；W09 门禁签署须等 W08 门禁件落盘后补做 | 主理人 + QA |
| Q2 | 已知环境阻塞（PG 5432 拒绝连接 / `DG_ADMIN_DSN` root 1045 / 控制库 830+ 历史行、只追加表无 DELETE）本周如何处理？ | **建议**：如实登记为「环境阻塞，非本周修复项」；W09 全部测试用离线样本与 mock，`sweep()` 扫描口径用构造数据断言；清理前提与逐条清单仍以 `docs/w07/worker-residue.md` 为准，不为「好清理」放宽 `dg_ctl` | 主理人 |
| Q3 | `RedactionResult.status` 的 `REVIEW_REQUIRED` 与 `BLOCKED` 边界、以及哪些类别必须 `BLOCKED`（如私钥/证书）？ | **建议**：`BLOCKED` = 私钥/证书/完整连接串/未能解析的二进制；`REVIEW_REQUIRED` = 规则命中但不确定能否安全替换的姓名/业务标识；二者都**不进入**模型/训练集。具体类别清单由架构师在设计冻结 | 架构师 |
| Q4 | 报告「六态」（建议/未执行/等待审批/已执行/结果未知/已验证）与 W08 `VerificationResult.verdict`（PASS/FAIL/UNKNOWN）如何映射？ | **建议**：报告「已验证」只接受 `verdict=PASS` 的 `VerificationResult` 引用；`UNKNOWN` 一律映射「结果未知」，**不得**映射「已修复」；映射表写进版本化文档 | 架构师 |
| Q5 | 五类记忆的存储形态：新建控制库表（只追加、需 009 迁移）还是复用 `evidence`/`task_events`？ | **建议**：新建 `knowledge_entries` 只追加表 + 撤销用「追加一条 `REVOKED` 版本」而非 `UPDATE`（延续 `dg_ctl` 无 UPDATE/DELETE 约束，见 `docs/adr/ADR-0006-audit-integrity-and-worm.md`）；需 009 迁移与 rollback | 架构师 |
| Q6 | `Reconciler.sweep()` 返回值扩展（W08 §1.3 第 4 条点名 W09）是否改变 `RECONCILIATION_SWEPT` 事件 payload 的冻结字段？ | **建议**：只**新增** `scanned_total` 字段，`scanned`/`converged` 语义保持不变并升级事件 payload 版本；`docs/w08/08-drill-report.md:59` 既存断言须回归通过（字段只增不改） | 架构师 |
| Q7 | 模型降级「路由回前一验证版本」的「前一版本」从哪取？ | **建议**：从冻结的 `benchmarks/baseline_v0/baseline_manifest.json` 的 `model_commit`/`prompt_version`/`tool_schema_version`（字段见 `run_baseline.py:20-34`）读取；当前这些值多为 `null`/`BLOCKED_MODEL_ENV_MISSING`（`:140-145`），**降级目标版本待架构师确认**，不得凭空指定模型版本 | 架构师 |
| Q8 | 本周是否引入 `docs/w09/` 下的报告样例/规则版本/记忆维护说明文档？ | **建议**：由实现阶段随代码产出（非 PRD 范畴），命名 `docs/w09/02-design.md` 之外的说明件；本周 PRD 不预写结论 | 主理人 |

## 8. 上游遗留映射表

> 任务书所指「`docs/w07/07-gate-review.md` §6 遗留项」：该文件**当前版本 §6 为「对外表述禁令」**（`:129-137`），遗留项实际分布在 **§5「W08 前置项」、§3「未验证项 NV」、§4「已知偏离 KD」、§4.3「残留事实」**。本表按**真实章节位置**列出属于 W09–W12 承接的部分，不改写编号。`docs/w08/08-drill-report.md` §5「未执行项」按原编号列出。

### 8.1 W07 门禁复核遗留（`docs/w07/07-gate-review.md`）

| # | 遗留项（真实位置） | 本周（W09）落点 / 后续落点 |
|---|---|---|
| L1 | §5 W08-5 / §3.1 NV-1：容器运行态（`docker build -f deploy/Dockerfile.worker` + `compose up worker`）**NOT_VERIFIED**（「没跑过，不是跑不了」） | W09 **不承接**（不属脱敏/报告/记忆）；落 W11 D4 长稳前提、**W12 D3 部署包/RC 实跑**；W08 已列 `P1-W08-03`，状态待 W08 交接件确认 |
| L2 | §3.2 NV-2：POSIX `SIGTERM`/`SIGINT` 真实信号路径未验证（Windows `TerminateProcess`） | 落 **W11 D4** 长稳/恢复演练；W09 Q2 登记为环境阻塞 |
| L3 | §3.2 NV-4：`tests/integration/test_worker_gateway.py` 本机 admin 1045 未跑通 | 落 **W11 D2** 跨租户/并发回归（如实归因环境，不修）；本周 Q2 登记 |
| L4 | §4 KD-1：`api` 维持双网、隔离是**账号级非网络级**；明确不得写成「摘 api 的 lab_net」 | 落 **W12 D3/D4** 部署文档如实标注；W09 不触碰 `apps/api/deps.py` |
| L5 | §4 KD-2：`tool_gateway` 占位服务与实现相反（`docker-compose.yml:156-162`） | W08 `P1-W08-01` 已承接，状态待 W08 交接确认；**W12 D4** 文档复核命名一致性 |
| L6 | §4 KD-3 / KD-4 / §4.3：环境非「可复现清零」；`environment-manifest.*.json` 不入库；**7 条残留**（6 条 W07 + 1 条历史行）不得表述为「已清理」 | 本周 §0/§6/Q2 如实登记；**W12 D4/D5** 发布说明与限制清单必须保留该事实（见 §6 表述禁令） |
| L7 | §4 KD-5：W07 冻结四件套缺 `docs/w07/05-freeze.md`、`06-conformance-audit.md`（已核实两文件不存在） | W08 `P1-W08-04` 已承接，状态待 W08 交接确认；**W12 D4** 文档完整性复核时逐项核对 |
| L8 | §6「对外表述禁令」本身（`:129-137`）：禁「W07 已收口/门禁通过」、禁「容器网络隔离已验证」、禁「环境已清理」、禁「QA 独立实跑通过」等 | **贯穿 W09–W12 所有对外材料**；本周 §0/§6 已按此口径书写 |

### 8.2 W08 演练报告未执行项（`docs/w08/08-drill-report.md` §5）

| # | 未执行项（§5 原编号） | 本周（W09）落点 / 后续落点 |
|---|---|---|
| M1 | §1.3 第 4 条（`:76-78`）：`scanned` 是「产出结论条数」，**真实扫描总数需 W09 扩展 `sweep()` 返回值** | **本周直接承接：P0-W09-06 / AC-20**（`scanned_total` 与 `conclusions` 分离） |
| M2 | §5：`--once` 进程 `outstanding_candidates` 退出码未断言固定值（`:164`） | 落 **W11 D4** 长稳/回归脚本（明确样本量与运行时长，不用短时 smoke test 冒充长稳） |
| M3 | §5：PostgreSQL 相关用例未执行（`:165`，5432 拒绝连接） | 环境阻塞，本周 §0/Q2 登记；落 **W11 D1/D2** 跨引擎回归如实归因、不修 |
| M4 | §5：控制库残留清理部分未执行（`:166`，admin 1045、只追加表无 DELETE） | 环境阻塞，本周 §0/Q2 登记；落 **W11/W12** 限制清单如实标注（与 L6 合并） |
| M5 | §5：7 类注入之外的网络分区/磁盘满未执行（`:167`，不在 D5 清单） | 落 **W11 D3**；是否扩展出 D5 清单**待主理人拍板**（任务书 W11 D3 仅列 6 类注入：Evidence/Audit/DB/Model/Worker/控制库短暂故障） |

> 本表 **L1–L8 / M1–M5** 均为「如实登记 + 给落点」，**不代表本周已修复或已验证**。W09 仅对 M1 承担实现责任，其余为跨周承接登记。
