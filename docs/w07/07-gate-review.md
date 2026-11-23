# W7 门禁复核（独立 Worker 载体 / 四条 AC / 冻结件）

- 复核人：架构师｜依据：W07 收口任务书、`docs/w07/01-prd.md`、`docs/w07/02-design.md` 附 C（`:840-887`）
- 输入：工程师实跑产物、**QA 最终复核（2026-11-11 23:56）**、`docs/w07/worker-residue.md`
- 方式：只读代码/文档 + 对 7 个冻结文件**本地复算 sha256**（见 §7）；**本文未独立重跑任何写库用例，QA 亦未**（见 GR-4）
- 缺口：`docs/w07/05-freeze.md` 未产出；本文为 W07 门禁复核件，PM 签署件另行出具
- 结论编号：`GR-0` 总结论｜`GR-1..GR-4` 四条 AC｜`C-1..C-4` 放行条件｜`KD-1..KD-3` 已知偏离｜`NV-1..NV-4` 未验证项

---

## GR-0｜结论：**有条件放行（QA 最终复核确认，2026-11-11 23:56）**

> 本行是唯一结论行。**不得**以「通过」「放行」「W07 已收口」作为独立结论用语；对外汇报一律引用本行全文。

**双层口径**（沿用评审阶段已收敛的表述，不得退回混写）：

| 层 | 判定 |
|---|---|
| 代码完整 | **是** —— 独立 Worker 载体、四条 AC 的实现与测试、冻结哈希均已落地 |
| 门禁通过 | **否（有条件放行）** —— 尚有 **GR-0** 的 C-1..C-4 条件、§3 的未验证项与 §4 的已知偏离；容器运行态未跑（NV-1 三段式，§3.1） |

**放行条件（C-1..C-4，状态以落盘时刻为准）**：

| # | 条件 | 状态 |
|---|---|---|
| C-1 | sha256 清单落盘或采信 QA 实测哈希作为新基准 | **已在本文满足**：§7 已落盘 7 个文件**完整 64 位**值，且架构师本地复算的 7 个前缀与 QA 实测逐字一致 → 以 §7 为新基准。（§7 中 `workers/README.md` 行同时列出 **QA 基准值**与 **P6 更正后 tag 内实际值**） |
| C-2 | NV-1 按三段式更正 | **已在本文满足**：`07-gate-review.md` §3.1 与 `docs/w07/02-design.md:846/852/857/884` 均已改为三段式；**`workers/README.md:87` 亦已由 P6 更正并随定稿追加提交入库**（tag 内 `6cdeac10…`，见 §7） |
| C-3 | 发布说明保留残留事实（**7 条**），不得表述为「已清理」 | **本文已如实登记**（§4.3）；发布说明须引用 §4.3 与 `docs/w07/worker-residue.md`，由发布环节执行 |
| C-4 | 提交一次 commit 并打 tag `w07-close` 作为可复现锚点；**提交范围排除 `.workbuddy/`（WorkBuddy 项目数据，不删也不提交）与 `项目规划/`（28 份 docx 磁盘唯一副本，单独处理）** | **条件已达成**：tag `w07-close` 已建立并随定稿提交前移（初版 `d937b77`，2026-11-12 13:51:35+08:00）；提交范围已排除 `.workbuddy/` 与 `项目规划/`；**提交次数与 add 方式以 `git log` 与 `git rev-parse w07-close` 为准，本文不写死次数与哈希**（§7） |

> C-4 的 commit/tag 已创建；**本文最终版入库且 `w07-close` 前移后，证据链即闭合**。在此之前不得称「证据链已闭合」。

---

## 1. 复核范围与测试基线（QA 实测，架构师引用而非独立重跑）

| 项 | 值 | 来源 |
|---|---|---|
| 本轮相关测试四数 | `775 passed / 0 failed / 0 skipped / 775 collected` | QA 最终复核 |
| 全仓收集 | `1121`，两次采样一致 | QA 最终复核 |
| P2-5 断言强度 | 新断言**明显强于**旧断言（非 `False → True` 式注水）；文件 mtime `22:17:16`、`git log -1`=`6176430`，**不属本次改动** | QA 最终复核 |
| 冻结 | 关键文件 mtime 均早于 `T0`；team-lead 隔夜（11-12 13:40）复算 5 个哈希与 QA 实测逐字一致 | team-lead |
| 架构师补核 | 本地复算 7 个文件完整 sha256，前缀与 QA 实测**逐字一致**（§7） | 本文 §7 |

---

## 2. 四条 AC 逐条判定

| 编号 | 判定 | 证据（文件:行 / 控制库实证） | 未验证项 | 是否可关闭 |
|---|---|---|---|---|
| GR-1 | **PASS** | `--dry-run --once` 前后三表计数不变：`tool_calls=152`、`worker_leases=188`、`approval_consumptions=95`（QA 实测）；代码侧「dry-run 只列候选、不产生 tool_call / 租约 / 审批消费」由 `tests/integration/test_worker_process.py:263` `test_dry_run_lists_candidate_without_executing`（`:264-268`）锁定；候选状态口径另见 `tests/unit/test_worker_runner.py:386` `test_candidate_task_statuses_is_waiting_approval_only` | 无 | 可关闭 |
| GR-2 | **PASS（限定口径：生产运行时装配点唯一）** | `workers/stack.py:188` `build_lab_stack(`、`:217` `gateway = ToolGateway(`；`tests/unit/test_worker_layering.py:83-89` `test_stack_is_the_only_module_building_the_gateway` 断言 `builders == ["stack.py"]`。`ToolGateway.execute()`（`core/gateway/tool_gateway.py:112-118`）与 `core/worker/client.py::GatewayPort.execute()` 签名一致 → Worker + Gateway **同进程**，无新增 API `execute`/`consume` 端点 | 无 | 可关闭 |
| GR-3 | **PASS** | 参数无法解析的失败路径真写审计事件：`workers/discovery.py:179-193` → `AuditEventType.RECONCILIATION_REQUIRED`（`:181`）+ `payload["action"]="DO_NOT_EXECUTE"`（`:190`）+ 结构化告警 `:170-178`；断言见 `tests/unit/test_worker_runner.py:571/574`，告警用例 `:606` `test_resolve_params_emits_alert_log` | 无 | 可关闭 |
| GR-4 | **只读数据复核通过（依赖工程师实跑产物）** | 见 §2.1 双层明细 | 见 §2.1 | **有条件关闭**（C-3/C-4 未全满足） |

> **GR-2 口径注**：QA 表述「`ToolGateway(` / `build_lab_stack(` 全仓唯一在 `workers/stack.py`」若按字面理解**不成立**——`tests/integration/test_w07_repair_e2e.py:95`、`test_worker_gateway.py:190`、`test_w06_acceptance.py:316`、`test_approval_consume.py:243` 也构造 `ToolGateway(`，`adapters/lab_assembly.py:72` 是 `build_lab_stack` 的定义处。正确表述是「**生产运行时装配点唯一，测试夹具另计**」。本文按此口径判 PASS，不采信字面「全仓唯一」。

### 2.1 GR-4 双层明细（不得混写）

| 层 | 内容 |
|---|---|
| **工程师实跑** | `tests/integration/test_worker_process.py` **4 passed**，含真实一次 `UPDATE` 与三表计数（`tool_calls` / `step_prepared` / `step_observed` / `approval_consumptions` 各 1 行） |
| **QA 复核** | **只读数据核验**：`task_0e13f0c5…` 四表各 1 行、`step_observed.outcome=success`、`tool_calls.status=success`、`tasks.status=EXECUTING` 且 `state_version=4`、`approvals.status=CONSUMED`、LAB `seed_orders(order_id=2001).note` 已恢复无脏值（与 `docs/w07/worker-residue.md` §1 第 6 行一致） |

**禁止**写成「QA 独立实跑通过」。QA 明确**未独立复跑**两个写库用例（只追加表无法清理）。若需独立实跑复现，须提供可清理控制库或可用 admin DSN，由 QA 复跑全 4 例。

---

## 3. 未验证项（NV，性质＝**无法验证**／未取得运行态证据）

### 3.1 NV-1 三段式更正（QA 实测，本节为定稿口径）

QA 实测：`docker info --format '{{.ServerVersion}}'` → **29.7.2**；`docker ps` → exit 0；`docker compose config` → **exit 0**。

| 段 | 事实 |
|---|---|
| ① 已实测通过 | `docker compose config` 解析成功，worker 块 `networks=['control_net','lab_net']`、`'model_net' in worker = False`、`DG_MODEL_PROVIDER='disabled'`、`dockerfile='deploy/Dockerfile.worker'`；Docker daemon 29.7.2 在运行 |
| ② 仍未验证 | **镜像构建**（`docker build -f deploy/Dockerfile.worker`）与**容器起栈 / 网络隔离运行态**（`docker ps -q` 当前 0 容器，从未起过） |
| ③ 定性 | **compose 配置层已可真实验证，容器运行态仍 `NOT_VERIFIED`——这是「没跑过」，不是「跑不了」** |

→ 同时**清除**「本机无 docker daemon / `docker compose config` 无法执行」这类失实表述。隔离不变量另有 CI 层可执行单测锚定：`tests/unit/test_worker_layering.py:145-151`（worker 块含 `control_net`/`lab_net`、不含 `model_net`、`DG_MODEL_PROVIDER: disabled`）、`:129-135`（`CMD ["python","-m","workers"]` + `COPY workers/adapters`）、`:140-142`（`"workers*"` 已入 packages）、`:111`（无裸 SQL）、`:118-121`（`core/worker` 不反向依赖组合根）。

> **`workers/README.md:87` 的 NV-1 失实表述已由 P6 更正为三段式并随定稿追加提交入库**（`sha256` 见 §7；该文件不在 T0 冻结的**代码/测试**哈希核对范围内，其冻结值不受影响）。本文与 `02-design.md` 附 C.1 以此处三段式为准。

### 3.2 NV-2 / NV-3 / NV-4

完整表述见 `docs/w07/02-design.md:848-857`（附 C.1，以 `workers/README.md:81-90` 为准本），本文不重写：

- **NV-2**：POSIX `SIGTERM`/`SIGINT` 真实信号路径未验证（Windows `Popen.terminate()` = `TerminateProcess`）；只声明 handler 接线与 `request_stop()` 语义两层。
- **NV-3**：PostgreSQL 引擎执行路径**刻意不接**（写工具只在 `LAB + mysql` 注册，`adapters/lab_assembly.py:41`），属设计范围外，**非缺陷**。
- **NV-4**：`tests/integration/test_worker_gateway.py` 既有用例本机 admin 1045 未跑通，**未声称通过**，与本次改动无交集。

---

## 4. 已知偏离（KD，性质＝**已确定的状态/配置**）

> KD 与 NV **性质不同**：NV 是「无法验证」，KD 是「已确定成立」。不得把 KD-1 并入 NV-1，否则等于用「容器没跑」掩盖一个已经静态可判定的架构事实。KD-1..KD-3 的完整论证见 `docs/w07/02-design.md:859-878`（附 C.2–C.4），本文直接引用、不重写；**KD-4 / KD-5 为提交前预检暴露、由本复核件新增登记**。

| # | 已知偏离 | 本文要点 |
|---|---|---|
| KD-1 | `api` 维持 `control_net` + `lab_net` 双网（`docker-compose.yml:112-115`） | 当前隔离边界是**账号级**（`dg_ro`/`dg_rw`/`dg_ctl` 的 DB 级授权），**不是网络级**；worker 侧网络已收窄且可静态验证。根因：`apps/api/deps.py:119-132` 用 `ro_dsn`/`rw_dsn` 构造 `MysqlPrepareProbe`（连 LAB MySQL），D5 七条固定 SQL、D2 前置条件、披露快照 `missing` 均依赖它。**不得作为 W08 前置项「摘 api 的 lab_net」**——摘网会直接打断 `POST /v1/approvals`。W08 正确动作是「评估 prepare probe 迁出 API（需 ADR）」 |
| KD-2 | `tool_gateway` 占位服务保留（`docker-compose.yml:156-162`） | **按 team-lead 在 W07 收口任务书中明文指令（「本轮不删，避免误伤」）有意保留，非实现侧遗漏**；但命名与 W07 装配事实相反：`workers/stack.py:217/:229` 同进程装配、`test_worker_layering.py:83-89` 锁死 `builders == ["stack.py"]`。危害：W08 若误读为「api → gateway 容器」而新写协议，会直接推翻 `GatewayPort` 进程内契约。本轮冻结不动，处置列入 W08 |
| KD-3 | 环境非「可复现清零」状态 | 见 §4.3 |
| KD-4 | `deploy/environment-manifest.mysql.json` / `.postgresql.json` 的 W07 `migration_version` 变更**不入库** | 两文件本轮已补 `migration_version=007_w07_disclosure`，但 `.gitignore:29` 明确忽略 `deploy/environment-manifest.*.json`（`:30` 仅放行 example），`git ls-files -- deploy/` 显示二者**从未被跟踪**，库内只有 `environment-manifest.example.json`。**裁决：尊重既有忽略策略，不用 `git add -f` 强行入仓。** W07 的 `migration_version` 变更仅存在于**本机运行时生成的环境清单**，**不纳入 `tag w07-close`**；版本权威来源是 `database/migrations/007_w07_disclosure.sql` + `rollback/007_rollback.sql`，以及 `docs/migrations.md`。两文件已确认不含 DSN/口令，`image_digest` 仍为 `null`。**发布说明不得表述为「环境指纹已完整入库」** |
| KD-5 | W07 冻结四件套缺件 | `docs/w07/05-freeze.md` 与 `06-conformance-audit.md` **不存在**（W06 有；W07 现有 `00/01/02` + `worker-residue.md` + `07-gate-review.md` + `08-pm-ruling.md`）。**裁决：本轮不补，如实登记 + W08 首日承接。** W07 冻结向量以 `docs/w06/05-freeze.md` 的 `action_digest` 基准（W06 冻结值逐字节未变，C4 已验）+ `007` 迁移为锚，**不声称 W07 已有独立冻结四件套** |

### 4.3 残留事实（C-3，如实登记，**不得表述为「已清理」**）

发布说明与任何对外材料须保留 **7 条**：`docs/w07/worker-residue.md` §1 逐条登记的 **6 条 W07 收口残留** ＋ QA 清点出的 **1 条历史行**（该历史行未列入 `worker-residue.md` 的 6 行清单，本文不臆测其 task_id）。

- 未能清理的硬原因：`dg_ctl` 对只追加表（`task_events`/`approvals`/`approval_consumptions`/`step_prepared`/`step_observed`/`worker_leases`/`execution_envelopes`）**无 `DELETE`**，本机 `DG_ADMIN_DSN` 返回 MySQL 1045；这是 W06 权限收口的**刻意设计**，不得为「好清理」而放宽 `dg_ctl`。
- 清理前提、逐条清单与核对 SQL 见 `docs/w07/worker-residue.md`；LAB `seed_orders(order_id=2001).note` 已由测试 `finally` 恢复，无脏值。

---

## 5. W08 前置项

| # | 动作 | 说明 |
|---|---|---|
| W08-1 | Reconciler 落账 | 对「已 Prepared 未 Observed」统一落 `RECONCILIATION_REQUIRED`；`core/execution/reconciler.py:218` 已有事件写入，缺调度载体。Worker **不做**补偿、不重放（P0-18） |
| W08-2 | KD-2 处置 | 删除 `tool_gateway` 占位服务，或改名 `gateway_legacy_placeholder` 并附 §4 说明 |
| W08-3 | 评估 prepare probe 迁出 API（**需 ADR**） | 这是网络级隔离的真正前提；**明确不含「摘 api 的 lab_net」**（KD-1） |
| W08-4 | ~~同步 `workers/README.md:87` 的 NV-1 三段式~~ | **已完成**（P6，随定稿追加提交入库）；保留本行仅为追溯，**不再作为 W08 动作** |
| W08-5 | **容器运行态实跑（可补强）** | Docker daemon 已可用（29.7.2），建议 W08 首日补 `docker build -f deploy/Dockerfile.worker` + `compose up worker` 的容器运行态与网络隔离实跑——**这是把 NV-1 彻底关掉的唯一路径** |
| W08-6 | 收紧断言强度（低优先） | `tests/unit/test_worker_layering.py:148` 用 `split("  worker:")[1].split("\n  model_service:")[0]` 切块，实际包含中间的 `tool_gateway` 块，结论仍成立但依赖「`model_service` 紧随其后」这一未断言的顺序前提；本轮**不动** |

---

## 6. 对外表述禁令（§C.5 重申，引用本复核件时必须遵守）

| 禁止表述 | 应改为 |
|---|---|
| 「W07 已收口 / 门禁通过 / 放行」 | 「**有条件放行（QA 最终复核确认，2026-11-11 23:56）**；C-4 tag 尚未创建」 |
| 「容器网络隔离已验证」 | 「compose 配置层已实测解析；容器运行态 `NOT_VERIFIED`（没跑过，不是跑不了）」（NV-1 三段式） |
| 「环境可复现 / 可清理 / 已清理」 | 「存在 7 条已登记的测试残留，见 `docs/w07/worker-residue.md`」（KD-3 / C-3） |
| 「QA 独立实跑通过」 | 「**只读数据复核通过（依赖工程师实跑产物）**」（GR-4） |
| 「Gateway 为独立部署组件」 | 「Gateway 与 Worker 同进程装配（`workers/stack.py:217/:229`）；`tool_gateway` 为遗留占位（KD-2）」 |

---

## 7. 证据锚点（本文以 tag 为锚，不以工作区快照为锚）

- **冻结时刻**：`T0 = 2026-11-11T23:52:44+08:00`（所有关键文件 mtime 均早于 T0，QA 与 team-lead 隔夜复算一致）
- **锚点 tag**：`w07-close` —— 已建立并随定稿提交前移；取锚点一律用 `git rev-parse w07-close`，**不写死 commit hash**
- **现状说明**：初版 157 行入库于 `d937b77`（2026-11-12 13:51:35+08:00，缺 KD-4/KD-5 与 §7 提交口径）；**含 KD-4/KD-5、C-4 定稿与 §7 哈希校准的 160 行最终版经其后追加提交入库，`w07-close` 已前移**。`d937b77` 保留为「首次入库提交」，不 amend、不删除。本复核件**以 tag 为锚、不以工作区快照为锚**。
- **提交方式与时点口径**：**首次入库 `d937b77` 采用 `git add -- docs/w07/` 目录形式；后续追加提交按主理人裁定改为逐路径 `git add -- <路径>`，禁用 `-A` / `-u` / 目录形式**；`docs/w07/02-design.md` mtime `23:56` **晚于 T0**，提交时按当时实际内容重新取哈希，**不使用提交前预检的体积/哈希作为最终快照**。**T0 冻结仅适用于代码与已冻结文档，不覆盖治理文档后续追加**（本复核件、PM 签署件、附 C 的 NV-1 更正均属此类）。

**QA 实测 sha256 基准（架构师本地复算，7 个前缀与 QA 实测逐字一致，完整 64 位）**：

| 文件 | sha256 |
|---|---|
| `workers/runner.py` | `fe2d3bd4ef9fd20f708dfb1bc272db220afdcb312a7188e9d85038e489890907` |
| `workers/stack.py` | `e258ebcdf9300fdadf344e984907109c753e941500b504e82598c8c7efe14d49` |
| `workers/discovery.py` | `dc6c016207a422ec725c1f849f475ddcfb3e0967166c3fd5d1f6ca93012f0723` |
| `tests/unit/test_worker_runner.py` | `7a222efe872e6e71329e5798aa91c5882d3bdb208f83b7d693dc713613184fdd` |
| `tests/integration/test_worker_process.py` | `5a2c69bfc816bbc75bbda22e040b36669ac14de7c78c971ef53cd2c31ccd494b` |
| `workers/README.md` | **tag 内实际值** `6cdeac106c4712eeb3ad1f9cca97333d1f8e8b63ada5f1c6f44b9d43726bb20a`（8,011 B / 102 行）；QA 复核时基准为 `c74cc3fccaddbf4a06d273b9518dbf2846c2fbf5217f21fe7bfb5f77bff4678d`（7,504 B，**P6 NV-1 三段式更正前**版本） |
| `docs/w07/worker-residue.md` | `6fd7a1b14ca69090aea3c40b1b3ab523786946e2f8f51bb41b55bc8ea3057469` |

> 本表满足 C-1。**`workers/README.md` 行已注明 P6 更正前后两值**：QA 复核时基准为 `c74cc3fc…`，经 team-lead 授权的 P6（NV-1 三段式）更正后，tag 内实际为 `6cdeac10…`；其余 6 个代码/测试/残留文档文件为 QA 复核时的冻结值，与 tag `w07-close` 内容逐字一致。**除上行已注明的该处差异外**，若 tag `w07-close` 所含任一文件内容与上表不符，本复核件的证据锚点失效，须重新复核。**本表以 `tag w07-close` 的实际内容为准；任何 P6 类事后更正必须同步本表、追加提交并前移 `w07-close`，不得只改工作区。锚点取值一律用 `git rev-parse w07-close`，不写死 commit hash。**
