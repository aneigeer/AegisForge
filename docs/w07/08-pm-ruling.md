# W07 门禁书面裁定｜R-1/R-2/R-3 关闭定档与「有条件放行」

| 项 | 值 |
|---|---|
| 裁定人 | 产品经理 许清楚 |
| 裁定日期 | 2026-11-12（与 `docs/w07/02-design.md:846` 附 C 补录同日） |
| 触发来源 | `docs/w07/07-gate-review.md`（架构师同步落盘，结构 §1–§7，结论「有条件放行」）；QA 最终复核 2026-11-11 23:56 |
| 裁定对象 | `docs/w06/08-pm-ruling.md` 登记的显式接受风险 **R-1 / R-2 / R-3** 的关闭判定，以及 W07 门禁的放行定档 |
| 适用边界 | **只对 W07 收口交付件定性**；本文件为新增且**仅此一个文件**，不改代码、不改 compose、不改他人文档；不修改 `docs/w06/05-freeze.md` 与 W06 冻结向量、不升 `action_digest_version` |
| 结论 | **R-1 按 `:58/:64` 视为闭合；R-2 书面义务在本签署件达成；R-3 达成；W07 门禁「有条件放行」（QA 2026-11-11 23:56 复核确认），须满足 §3 的 C-1…C-4 四条放行条件** |

> 说明：`docs/w07/07-gate-review.md` 已于本裁定落盘期间由架构师同步产出（**工作区版本 160 行**，§1–§7，GR-0 唯一结论行在 `:11`：「**有条件放行（QA 最终复核确认，2026-11-11 23:56）**」，文字未变；**tag `w07-close` 初建时所含为 157 行早一版，见 §3 C-4**）。本裁定与其口径一致，且**不修改该文件**；GR-0 的放行条件状态以落盘时刻为准，凡涉及该文件具体行号的表述以其落盘版本为准。

---

## 1. 事实基础（已核实，非推断）

| # | 事实 | 证据位置 | 复核方式 |
|---|---|---|---|
| F1 | `docs/w06/08-pm-ruling.md:48` 原文「转为闭合必须同时满足下列 5 条，**缺一即视为 W07 门禁不通过**」——「缺一」的后果落在**门禁**，不是「R-1 未闭合」 | `docs/w06/08-pm-ruling.md:48` | 本裁定逐字核对 |
| F2 | `:58` 附则「若 C1–C4 达成但 W07 其他 P0 未达成，视为『**本条已闭合**、W07 门禁因其他项不通过』——**不得反向把本条重新判为未闭合**」；`:64` R-1 行「**W07 门禁：C1–C4 全部达成即关闭**」 | `docs/w06/08-pm-ruling.md:58,64` | 本裁定逐字核对 |
| F3 | 三条风险各有独立关闭条件，不得打包判：R-1 = C1–C4（`:64`）；R-2 = P1-W07-01 + 「**必须在 W07 门禁复核中显式列出，不得静默**」（`:65`）；R-3 = 「W07 设计需明确该口径」（`:66`） | `docs/w06/08-pm-ruling.md:64,65,66` | 本裁定逐字核对 |
| F4 | **C1** 实际覆盖：`tests/integration/test_approval_api.py:221 test_operator_creates_approval_with_full_disclosure`，`:253,257` 断言响应不含 `NOT_PERSISTED_IN_W06`；`apps/api/routes/approvals.py:87` 已将 reason 改为 `LEGACY_ROW_WITHOUT_DISCLOSURE` | 同上路径 | 函数名与行号核验通过 |
| F5 | **C2** 实际覆盖：`tests/integration/test_w07_disclosure_integrity.py:64/92/121/141`（创建时快照、契约变更后不可改、篡改读出、消费前篡改检出）+ `tests/unit/test_disclosure_snapshot.py:241,253,266,299,325,381`（列不可变、迁移/插入/锁内复核、API 读快照） | 同上路径 | 函数名与行号核验通过 |
| F6 | **C3** 实际覆盖：`tests/integration/test_w07_disclosure_integrity.py:191 test_approve_blocked_when_disclosure_section_missing`，`:192/:209` 断言 `DISCLOSURE_INCOMPLETE` | 同上路径 | 函数名与行号核验通过 |
| F7 | **C4** 实际覆盖：`tests/unit/test_canonical.py:68 test_frozen_vector_is_byte_exact` + `database/migrations/007_w07_disclosure.sql` + `database/migrations/rollback/007_rollback.sql`；W06 `action_digest` 冻结向量逐字节未变 | 同上路径 + `docs/w07/02-design.md:6,19,264` | 文件/函数存在性与口径核验通过 |
| F8 | 裁定 `:52-56` 点名的 **4 个验证函数中 3 个名字 / 所在文件与实际不符**（C1、C2 的函数名不存在；C3 的函数落在别的文件；仅 C4 完全对得上） | 对照 F4–F7 与 `docs/w06/08-pm-ruling.md:52-56` | 逐项比对 |
| F9 | **R-2 事实来源已补**：`apps/api/routes/approvals.py:90-93,114-125` 从服务端快照 `impact` 取 `reversible` / `affects_other_tasks` 并列出 `missing` | `apps/api/routes/approvals.py:90-93,114-125` | 本裁定复核 |
| F10 | **R-3 口径已明确**：`docs/w07/02-design.md:19`「不能进 `action_digest`…输入字段集不变、版本不变」、`:264`「与 `action_digest` 解耦…纳入需升版并重算全部向量」 | `docs/w07/02-design.md:19,264` | 本裁定复核 |
| F11 | 独立 Worker 载体已落地：`python -m workers` 为可独立启动进程，`workers/stack.py:217/:229` 把 `ToolGateway` 与 `Worker` 同进程装配，**不给 API 加端点**，`core/worker/*` 零改动 | team-lead 中转 + `docs/w07/02-design.md:870` 交叉引用 | 团队前序结论，非本裁定独立跑测 |
| F12 | 架构师 rev.2 修复真实缺陷：原 `WAITING_APPROVAL` 无 `NEEDS_RECONCILIATION` 出边；现 Worker 执行前先 `transition(WAITING_APPROVAL→EXECUTING)`、重读 task 取新 `state_version` 再构造信封 | team-lead 中转的架构师 rev.2 结论 | 团队前序结论，非本裁定独立复核 |
| F13 | QA 最终复核（2026-11-11 23:56）结论「**有条件放行**」，四条 AC 证据链完整：AC① 载体可运行、AC② 唯一装配点、AC③ 参数还原 fail-closed、AC④ **只读数据复核通过**（`task_0e13f0c5…` 四表各 1 行、`EXECUTING/v4`、`CONSUMED`、LAB 无脏值）。**AC④ 是只读核验工程师实跑产物，不是 QA 独立复跑两个写库用例** | QA 复核结论（team-lead 中转）；`docs/w07/worker-residue.md:23,30-35` 交叉印证 | 中转，非本裁定独立复跑 |
| F14 | `docs/w07/02-design.md:840-887` 已落「附 C｜未验证项（NOT_VERIFIED）与已知偏离」，含 NV-1..NV-4、KD-1..KD-3 与 §C.5 对外表述禁令 | `docs/w07/02-design.md:840-887` | 本裁定复核 |
| F15 | 控制库残留：`docs/w07/worker-residue.md` 登记本轮 6 条 W07 task；`dg_ctl` 对只追加表无 DELETE、本机 admin 账号 MySQL 1045 不可用；文件明确「**不声称已清理**」 | `docs/w07/worker-residue.md:1-5,39-64,110` | 本裁定复核 |
| F16 | 「摘 `api` 的 `lab_net`」会打断 `POST /v1/approvals`：`apps/api/deps.py:119-132` 的 `MysqlPrepareProbe` 连 LAB MySQL，D5 固定 SQL / D2 前置条件 / 披露 `missing` 均依赖它 | `docs/w07/02-design.md:862-864` | 本裁定复核 |
| F17 | `docs/w06/07-gate-review.md` §6 的 8 条遗留项可归类，均为持续跟踪项 | `docs/w06/07-gate-review.md:59-68` | 本裁定复核 |

---

## 2. 裁定

### 2.1 R-1 / R-2 / R-3 按各自关闭条件分别判，不打包成一句

| 风险 | 裁定写的关闭条件 | 本轮实际 | 判定 |
|---|---|---|---|
| **R-1** | `C1–C4 全部达成即关闭`（`:64`）；`:58` 附则明定「若 C1–C4 达成但 W07 其他 P0 未达成，视为**本条已闭合**…**不得反向把本条重新判为未闭合**」 | C1–C4 实质均有等价覆盖（见 §2.2：C4 完全对得上；C1/C2/C3 指定测试名或所在文件对不上但等价测试在） | **按 `:58/:64` 视为闭合**。此前差的只是书面核验与 PM 关闭签署归档，由本签署件 §2.2 / §4 补齐。**C5 属 W07 门禁线，不得拿来判 R-1** |
| **R-2** | `W07 P1-W07-01` 补服务端事实来源；「若不达成，登记至 W08，但**必须在 W07 门禁复核中显式列出，不得静默**」（`:65`） | 事实来源已补（F9）；`docs/w07/` 原只有 `00/01/02`，无门禁复核文件可「显式列出」 | **书面义务在本签署件达成**——§2.1 本行与 §4 风险清单即为「显式列出」，不再静默。实质缓解与书面义务**均已达成** |
| **R-3** | 「W07 设计需明确该口径」（`:66`） | `docs/w07/02-design.md:19,264` 已明确三节不进 `action_digest`、字段集与版本不变（F10） | **达成**。设计文档已满足，无需额外签署 |

> **禁止两种极端写法**：
> - ❌「C1–C5 均未达成 / W06 遗留未解决」——会被 F4–F10 的代码与文档证据反驳；
> - ❌「R-1 未闭合，因 C5 不成立」——会被 `:58` 附则与 `:64` 直接反驳。
>
> 三项均未造成真实写入或数据损害（沿用 `docs/w06/08-pm-ruling.md:68` 定性）。

### 2.2 C1–C4 按实际测试名逐条登记（本次签署核心）

| 条件 | 裁定 `:52-56` 点名的验证方式 | 实际存在的验证（本裁定据实登记） | 判定 |
|---|---|---|---|
| **C1** 三节 `provided=true` 且不再出现 `reason=NOT_PERSISTED_IN_W06` | `tests/integration/test_approval_api.py::test_disclosure_three_sections_persisted`（**函数名不存在**） | `tests/integration/test_approval_api.py:221 test_operator_creates_approval_with_full_disclosure`（`:253,257` 断言无 `NOT_PERSISTED_IN_W06`；`apps/api/routes/approvals.py:87` 已改 `LEGACY_ROW_WITHOUT_DISCLOSURE`） | 实质达成 |
| **C2** 三节来自服务端事实来源、创建时快照持久化、批准后不可改 | `test_disclosure_snapshot_is_immutable_after_approve`（**函数名不存在**） | `tests/integration/test_w07_disclosure_integrity.py:64/92/121/141` + `tests/unit/test_disclosure_snapshot.py:241,253,266,299,325,381` | 实质达成 |
| **C3** 三节纳入必需项、缺失时批准被拒且 `missing_fields` 明确列出 | `tests/integration/test_approval_service.py::test_approve_blocked_when_disclosure_section_missing`（**函数存在但落在别的文件**） | `tests/integration/test_w07_disclosure_integrity.py:191`（`DISCLOSURE_INCOMPLETE`，`:192/:209`） | 实质达成 |
| **C4** 新迁移含回滚脚本、W06 冻结向量逐字节未变 | `tests/unit/test_canonical.py::test_frozen_vector_is_byte_exact`、`test_action_digest.py` 全绿（**完全对得上**） | `tests/unit/test_canonical.py:68 test_frozen_vector_is_byte_exact` + `database/migrations/007_w07_disclosure.sql` + `database/migrations/rollback/007_rollback.sql`；`docs/w07/02-design.md:6,19,264` 确认 `action_digest_version` 未升、字段集未改 | **达成** |

> **登记说明**：裁定 `:52-56` 点名的 **4 个验证函数中 3 个名字 / 所在文件与实际不符**，此处按实测替换。替换只更正「验证方式」的登记口径，**不改 C1–C4 的实质要求，不改 W06 已冻结向量，不升 `action_digest_version`**。

### 2.3 W07 门禁失败原因不得归咎 W06 遗留

`:58` 禁止「反向把本条重新判为未闭合」，因此 W07 门禁的阻断原因**必须落在本周自身项**，不能回头算到 W06 头上。`docs/w06/07-gate-review.md` §6 的 8 条遗留项归类如下，**均为持续跟踪项，不构成 W07 门禁阻断项**：

| 类别 | 条目 | 性质 |
|---|---|---|
| 已在 W07 实质解决 | 三节披露持久化并纳入披露（D1）；无 grant 的写操作收紧为「任何 write 必须落 `step_prepared`」（D7）；影响面 `reversible` / `affects_other_tasks` 服务端事实来源（P1-W07-01） | 已闭环，见 §2.1 / §2.2 |
| 显式 P2 / 登记 W11 | 真实 WORM/Object Lock 接入与第三方合规评估；`dg_ctl` 账号拆分与 `ALTER TABLE … DROP COLUMN` 收口 | 按 `docs/w07/01-prd.md` P2 与 ADR-0006 既定延期 |
| 已冻结的接受口径 | 审计历史行 `tenant_id` 为 `NULL`（查询以 `JOIN tasks` 为权威路径）；T9 启动抖动 retry-once（需连续多轮稳定性数据） | W06 已定档，不重开 |
| 其余跟踪项 | `ToolRegistry.replace()` 测试注入点（消除私有属性注入）；Auditor 只读审批详情视图（建议 W08+） | 不阻断门禁 |

**W07 原三条阻断项的本轮状态**：

| 阻断项 | 本轮状态 |
|---|---|
| ① 独立 Worker 载体未落地 | **已补齐**（F11：`python -m workers`，Gateway 与 Worker 同进程装配，不新增 API 端点） |
| ② 无门禁复核 / 冻结件 | **已补齐**（本签署件 + 架构师同步落盘的 `docs/w07/07-gate-review.md`） |
| ③ 四条最小可测 AC 全不成立 | **QA 确认达成**（F13）；其中 AC④ 为**只读核验工程师实跑产物，不是 QA 独立复跑两个写库用例**，不得表述为「QA 已独立复跑」 |

### 2.4 签署结论

**W07 门禁有条件放行**（QA 2026-11-11 23:56 复核确认）。放行以 §3 的 C-1…C-4 四条 precondition 为前提；**在 C-4 的 commit/tag 完成前不得写「W07 已收口」**（见 §6）。

本结论**只定档 W07 门禁**，不代替 W12 最终 Alpha 门禁；按《总览》§四与《W12》，当前仍只能发布明确标注限制的技术预览。

---

## 3. 裁定生效条件（W07 门禁放行条件，可查证）

| # | 放行条件 | 验收标准 | 责任方 |
|---|---|---|---|
| **C-1** | **sha256 清单落盘 / 采信 QA 实测哈希** | 产出一份版本化 sha256 清单（代码 + 迁移 + 冻结件），或在 `docs/w07/07-gate-review.md` 中直接采信 QA 2026-11-11 23:56 的实测哈希并逐项登记；清单须与工作区逐字节一致 | 主理人 / QA |
| **C-2** | **NV-1 更正为三段式** | 必须写成「**Docker daemon 29.7.2 可用；`docker compose config` exit 0；仅容器运行态未验证**」，并明确「**是『没跑过』不是『跑不了』**」。**`docs/w07/02-design.md:852` 已为三段式表格行、`:857` 为三段式对外表述并含禁令句，二者均非失实断言**；仅 `workers/README.md:87` 为失实断言，**已解冻由工程师同批更正提交**（T0 冻结就此项解冻，范围仅 NV-1 段）；**本签署件不修改他人文档** | 架构师 / 文档负责人 |
| **C-3** | **残留 7 条如实声明，不得表述「已清理」** | 残留 **7 条（6 条 W07 本轮 + 1 条历史）** 如实登记；6 条明细见 `docs/w07/worker-residue.md`，第 7 条历史残留须补登记。凡验收材料出现「环境可复现 / 可清理 / 已清理」，必须同时引用 `worker-residue.md` 与 `docs/w07/02-design.md:874-878`（KD-3） | 主理人 / 工程师 |
| **C-4** | **提交并打 tag `w07-close` 作为版本锚点** | 将本签署件、`07-gate-review.md`、附 C、sha256 清单与 W07 代码一并提交，打 tag `w07-close`；记录 commit 与 tag 哈希。**提交范围排除 `.workbuddy/` 与 `项目规划/`；采用逐路径 add（禁 `-A`/`-u`/目录形式）；最终版经追加提交入库后 `w07-close` 前移** | 主理人 |

> **状态对齐**（`docs/w07/07-gate-review.md` GR-0 放行条件表，落盘时刻）：**C-1 已满足**（§7 已落盘 7 个文件完整 64 位 sha256，架构师本地复算前缀与 QA 实测逐字一致，以 §7 为新基准）；**C-2 已满足**（§3.1 三段式定稿；`02-design.md:852/:857` 已为三段式与禁令句、均非失实断言，仅 `workers/README.md:87` 待工程师同批更正提交）；**C-3 已满足**（§4.3 如实登记 7 条残留，发布说明须引用 §4.3 与 `docs/w07/worker-residue.md`）；**C-4 待闭合于 tag 前移**：`d937b77` 与 `w07-close` 已存在，但 tag 内门禁件为 **157 行早一版**；门禁件最终版（160 行）与签署件（本件，169 行）将随追加提交入库并前移 `w07-close`，**在此之前依门禁件 §7 不得称证据链闭合**；追加提交由 team-lead 在本件落盘后执行。
>
> 四条均为**放行 precondition**；**C-1/C-2/C-3 已满足；C-4 于 `w07-close` 前移后闭合**；§6 对外表述禁令在 tag 前移前继续有效。

### 3.1 已知偏离登记（不阻断有条件放行）

> 以下两项由工程师提交前预检发现、经主理人裁决，属**如实登记的已知偏离**：**不构成 W07 门禁的第四条阻断项**，也**不改变「有条件放行」结论**；本节上方的 4 个放行条件（**C-1 sha256 / C-2 NV-1 三段式 / C-3 残留 7 条 / C-4 commit + tag `w07-close`**）**保持不变**。

| # | 已知偏离 | 事实（已核实） | 裁决 | 版本权威来源 / 承接 |
|---|---|---|---|---|
| **KD-4** | **环境清单不入库** | `deploy/environment-manifest.mysql.json` / `.postgresql.json` 被 `.gitignore:29` 忽略且从未跟踪（`git ls-files` 返回空）；本轮已补 `migration_version=007_w07_disclosure`，但**不会进入 tag `w07-close`**；两文件不含 DSN / 口令，`image_digest` 仍为 `null`，`status=LOCAL_DEV_NOT_VERIFIED` | **尊重既有忽略策略，不执行 `git add -f`**；发布说明**不得写「环境指纹已完整入库」** | 版本权威来源为 `database/migrations/007_w07_disclosure.sql` + `database/migrations/rollback/007_rollback.sql` + `docs/migrations.md`（三者均入库），不以两份被忽略的 JSON 为锚 |
| **KD-5** | **W07 冻结件缺失** | `docs/w07/05-freeze.md` 与 `docs/w07/06-conformance-audit.md` 不存在；`docs/w07/` 现只有 `00/01/02`、`worker-residue.md`、`07-gate-review.md` 与本签署件 `08-pm-ruling.md`（W06 有该两件，W07 无） | **本轮不补**，登记为已知偏离并**列入 W08 首日承接**；**不声称 W07 已有独立冻结四件套** | W07 冻结向量以 `docs/w06/05-freeze.md` 的 `action_digest` 基准（W06 冻结值逐字节未变，C4 已验）与 `007` 迁移为锚 |

---

## 4. 显式接受风险清单（本轮定档）

| # | 风险 | 裁定关闭条件 | 本轮判定 | 剩余事项 |
|---|---|---|---|---|
| **R-1** | 批准人可在缺少补偿方案与验证计划时批准 R2 写操作 | C1–C4 全部达成即关闭（`:64`）+ `:58` 不得反向重开 | **闭合** | 书面核验与 PM 关闭签署归档由本签署件 §2.2 / 本节完成 |
| **R-2** | 「影响面」中 `reversible` / `affects_other_tasks` 恒为 `null` | P1-W07-01 + 「必须在 W07 门禁复核中显式列出，不得静默」（`:65`） | **实质缓解 + 书面义务均达成**（本签署件 §2.1 本行与本节即为显式列出） | 无 |
| **R-3** | 三节内容不参与 `action_digest`，篡改三节无法由摘要检出 | 「W07 设计需明确该口径」（`:66`） | **达成**（`docs/w07/02-design.md:19,264`） | 无；减轻手段为创建时快照 + 批准后不可改（C2） |

**本轮新增登记**（不属于 R-1/R-2/R-3，但须随交付登记、不得静默）：

| 编号 | 事项 | 统一口径 |
|---|---|---|
| NV-1..NV-4 | 未验证项 | 按 `docs/w07/02-design.md:840-857`；**NV-1 依 §3 C-2 更正**，其余不得表述为「已验证」 |
| KD-1 | `api` 维持双网 | 当前隔离边界是账号级（`dg_ro`/`dg_rw`/`dg_ctl`），不是网络级；**不得**把「摘 api 的 `lab_net`」写成 W08 前置项（F16） |
| KD-2 | `tool_gateway` 占位服务保留 | 按既定指令保留、**非遗漏**；W08 二选一处置（`docs/w07/02-design.md:866-872`） |
| KD-3 | 控制库残留无法清理 | 见 `docs/w07/worker-residue.md`；不得表述「已清理」，`dg_ctl` 权限不得为清理而放宽 |
| KD-4 | 环境清单不入库 | 见 §3.1；尊重既有忽略策略、不执行 `git add -f`；发布说明**不得写「环境指纹已完整入库」** |
| KD-5 | W07 冻结四件套缺件 | 见 §3.1；本轮不补、**W08 首日承接**；**不声称 W07 已有独立冻结四件套** |
| AC④ 复核性质 | 只读核验工程师实跑产物 | **不是 QA 独立复跑两个写库用例**，不得表述为「QA 已独立复跑」 |

---

## 5. 对 W08 的一句话交接

**W08 承接 Reconciler 落账、KD-2 处置、prepare probe 迁出 API 的 ADR 评估与 Docker 容器运行态实跑；但不得把「摘 `api` 的 `lab_net`」列为前置项。**

| # | 交接项 | 要点 |
|---|---|---|
| 1 | **Reconciler 落账** | 「已 Prepared 未 Observed」的收敛与 `MANUAL_INTERVENTION` 边界。`core/execution/reconciler.py` 属 W06 P0-18/19，W08 须在此基础上做独立验证与恢复闭环，不得把该组件算作 W08 已实现 |
| 2 | **KD-2 处置** | `docker-compose.yml` 的 `tool_gateway` 占位服务二选一：删除，或改名 `gateway_legacy_placeholder` 并附 `docs/w07/02-design.md:866-872` 说明 |
| 3 | **评估 prepare probe 迁出 API（需 ADR）** | `apps/api/deps.py:119-132` 的 `MysqlPrepareProbe` 使 `api` 依赖 `lab_net`；`docs/w07/02-design.md:862-864` 已论证照做即打断 `POST /v1/approvals`。W08 只做方案评估与 ADR，**在无替代探针前不摘网** |
| 4 | **补 Docker 容器运行态实跑（NV-1）** | 镜像构建、`compose` 起栈、容器网络隔离；实跑前维持 `NOT_VERIFIED` 口径，并按 §3 C-2 更正 NV-1 表述 |
| 5 | W08–W12 主体 | 独立 Verifier、Recovery Controller、报告/脱敏/记忆、Benchmark、安全回归、备份恢复实测 RPO/RTO、Alpha 验收仍按 V2.2 任务书推进；本裁定不预判其进度 |
| 6 | **KD-5 首日承接（W07 冻结四件套缺失）** | `docs/w07/05-freeze.md` 与 `06-conformance-audit.md` 本轮不补（见 §3.1 KD-5）；W08 首日承接补齐，**不得声称 W07 已有独立冻结四件套**，冻结向量仍以 `docs/w06/05-freeze.md` 的 `action_digest` 基准与 `007` 迁移为锚 |

**明确不含**：「摘 `api` 的 `lab_net`」——理由见 F16 与 KD-1。

---

## 6. 附：本裁定的效力与边界

| 项 | 说明 |
|---|---|
| 是否改变 W06 门禁结论 | **不改变**。R-1 按 `docs/w06/08-pm-ruling.md:58/:64` 视为闭合；W06 的事实判定、路径 2 定档、22 条 P0 追溯结论一律保持原状 |
| 是否修改代码 / compose / 他人文档 | **否**。本文件为新增且**仅此一个文件**；不动代码、不动 `docker-compose.yml`、不动 `workers/README.md`、`docs/w07/02-design.md`、`docs/w07/07-gate-review.md`、`docs/w07/worker-residue.md` |
| 是否修改 `docs/w06/05-freeze.md` 与 W06 冻结向量 | **否**。`action_digest` 冻结值逐字节不变，**不升 `action_digest_version`**（与 `docs/w07/02-design.md:6,19,264` 一致） |
| **已知偏离的处置边界** | **本裁定不修改 `.gitignore`，不强制纳入被忽略文件，不改变 W06 冻结向量，不升 `action_digest_version`**。KD-4 / KD-5 见 §3.1，均为**如实登记的已知偏离**，不构成 W07 门禁的第四条阻断项，不改变「有条件放行」结论 |
| 是否放宽 C1–C4 实质要求 | **否**。§2.2 只替换「验证方式」的测试名登记，实质要求与冻结向量均未改动 |
| 是否可援引为先例 | **不可援引为先例**。本裁定仅适用 W06→W07 这一次交付交接；后续周次出现同类偏差，一律按 `docs/w06/08-pm-ruling.md:81` 在当期补齐，**不得援引 `:58` 附则反向重开已闭合项** |
| 是否等于 W12 最终 Alpha 门禁通过 | **否**。本裁定只定档 W07 门禁；按《总览》§四与《W12》最终门禁，双引擎只读诊断稳定性、至少一个修复闭环通过独立验证、四类报告齐备仍未满足，当前仍只能发布明确标注限制的技术预览 |
| **对外统一表述（C-4 完成前）** | 「**审批与披露闭环已实现并可经 API 使用；执行链已由独立 Worker 载体承载，四条 AC 达成，门禁有条件放行待锚点落定**」。**在 C-4 的 commit/tag 完成前，不得写「W07 已收口」**（亦见 `docs/w07/02-design.md:885` 对外表述禁令） |
| 生效时点 | 本裁定自签署起生效；「有条件放行」的四条 precondition 以 §3 为准。C-4 完成后由主理人更新门禁状态并回写本表的对外表述 |
