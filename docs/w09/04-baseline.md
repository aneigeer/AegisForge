# W09 开工基线记录（QA 独立核验）

> 性质：**开工基线快照**，用于防止把上游遗留误算到 W09 头上。
> 观测方式：全程**只读**——未改代码/测试/文档、未跑写库用例、未执行迁移、未 commit/tag。
> 末次采样时间：`2026-11-13T18:16:11+08:00`（本机时钟）。
> 上游依据：`docs/w08/07-gate-review.md`（GR-0）、`docs/w08/06-qa-report.md` §1.1。
> 本文不修改 `docs/w08/*`（W08 文档永久冻结），不修改 `docs/w09/01-prd.md`、`02-design.md`、`03-task-cards.md`。

---

## 1. 基线数字（实跑）

| 项 | 实测值 | 命令 |
|---|---|---|
| 单元回归 | **891 passed / 0 failed**（两次复跑：`891 passed in 8.57s`、`891 passed in 9.91s`） | `timeout 180 ./.venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider` |
| 收集完整性 | **1260 tests collected / 0 import error**（`exit=0`，`1260 tests collected in 2.53s`） | `timeout 180 ./.venv/Scripts/python.exe -m pytest --collect-only -o addopts="" -q -p no:cacheprovider` |
| W08 门禁总结论 | **GR-0 = 有条件放行** | `docs/w08/07-gate-review.md` §GR-0（只读核验） |

两次单元复跑分别落在核验起点 HEAD `12a2417` 与最终 HEAD `0d93d5b`，均为 891 passed，说明 W09 设计/任务卡入库未扰动测试基线。

**强制口径（不得省略）**：

1. 解释器必须用 `./.venv/Scripts/python.exe`（Python 3.14.7）。PATH 上的裸 `python` 是 **3.13.14**，不满足 `requires-python = ">=3.14,<3.15"`。
2. 统计必须带 `-o addopts=""`。`pyproject.toml` 已设 `addopts = "-q"`，叠加命令行 `-q` 会变成 `-qq`，**吞掉 passed/failed 汇总行**；历史上「把收集总数当成通过数」的错误即源于此类口径缺失。

**W08 迁移 C-4 在库状态（只读核验）**：`008_w08_verification.sql`(1612B) 与 `rollback/008_rollback.sql`(1037B) 均存在；`information_schema` 显示 `dg_system.verification_results` 存在（`TABLE_ROWS=17`）、`approvals.compensation_of` 为 `varchar(64) / IS_NULLABLE=YES`、`uq_approvals_compensation_of` 的 `NON_UNIQUE=0`；`schema_migrations` 含 `008_w08_verification @ 2026-11-12 14:20:01`。C-4 **满足**。

---

## 2. 锚点口径（关键）

| 锚点对象 | 以什么为准 | 完整/短哈希 |
|---|---|---|
| **W08 基线** | **tag `w08-close`，不以 HEAD 为准** | `12a2417095ea157fa201bfa210864dcc6d89eb72`（`git rev-parse w08-close` 与 `12a2417` 一致） |
| **W09 基线** | commit `0d93d5b` | `0d93d5b14246d30f8230fb7a4e89ad6801135d86` |
| W07 历史锚点 | tag `w07-close`（未移动） | `c8a739ad9ff1dabdcf4908e353bb11baacd30075` |

核验起点的 `git log -3` 为 `12a2417 / c8a739a / fe6135e`，`git tag -l` 仅 `w07-close`；核验期间 **`w08-close` 才被创建**（正确指向 `12a2417`），并新增 `0d93d5b W09: 增量设计与任务卡（T01–T06）` 使 HEAD 前移。

> **口径裁定：任何「HEAD = 12a2417」的旧表述一律作废。**
> 引用 W08 结论请写「tag `w08-close` / `12a2417`」；引用 W09 开工状态请写「`0d93d5b`」。

---

## 3. 三处漂移（上游登记值 → 当前实测值 → 影响）

| # | 漂移 | 上游登记值 | 当前实测值 | 影响 |
|---|---|---|---|---|
| ① | **锚点漂移** | 预期 HEAD=`12a2417`、tag 仅 `w07-close` | `w08-close` 已建于核验期间；HEAD=`0d93d5b`；W09 三份文档均已入库 | 旧「HEAD=12a2417」表述作废；W08 结论一律挂 tag，避免把 W09 提交误算进 W08 门禁证据 |
| ② | **E-3 残留漂移** | `docs/w08/07-gate-review.md` §5：`dg_system.tasks` = **830** 行，**非终态 = 763** | 只读 SELECT 实测 **`tasks = 1341`**（**+511**），按非终态启发式统计 **1220**；分布 `PENDING 773 / NEEDS_RECONCILIATION 241 / PLANNING 154 / COMPLETED 56 / FAILED 41 / EXECUTING 28 / CANCELLED 24 / OBSERVING 10 / MANUAL_INTERVENTION 5 / WAITING_APPROVAL 4` | 影响启动恢复**预算护栏**的输入规模、**T06 `sweep()` 真实扫描总数**（KD-2）的期望值、**W11 备份恢复演练**输入量。**禁止再用 830/763 描述当前库**；W08 文档冻结，差异只在本文登记 |
| ③ | **文档状态漂移** | 核验起点 `docs/w09/02-design.md`、`docs/w09/03-task-cards.md` 为 `??`（未跟踪） | 已随 `0d93d5b` 入库；`git ls-files docs/w09/` 返回三份文档；`git status --porcelain` 仅剩 `?? .workbuddy/`、`?? 项目规划/`（环境目录，非 W09 代码） | 后续若再出现 `?? docs/w09/*`，属**新改动**，不计入开工基线；W09 基线以 `0d93d5b` 为唯一参照 |

---

## 4. 上游承接清单

### 4.1 W08 放行条件 `C-1..C-4`（`docs/w08/07-gate-review.md`）

| 条件 | 状态 | 承接要求 |
|---|---|---|
| C-1 | **满足**：偏差 #1（`COMPENSATION_DENIED` 执行阶段不补发）已由 KD-1 书面登记 | W09 通过 KD-1 修事件完整性 |
| C-2 | **满足（降级）**：全量回归口径降级为「分层证据」，写明「全量通过不可判定」 | **W11 环境补齐后必须重跑一次真正跑完的全量**，否则 W12 不得宣称「回归全绿」 |
| C-3 | **满足（登记）**：环境阻塞写入 §5 限制清单 | **W11 门禁前必须解决或由主理人书面豁免**；未解决则 W12 Alpha 只能发技术预览 |
| C-4 | **满足**：迁移 008 与新增表结构已在库核验（本文 §1 复验通过） | 无 |

### 4.2 未验证项 `NV-1..NV-5`（**不得当作通过**；W11 承接为主）

| 编号 | 未验证项 | 承接 |
|---|---|---|
| NV-1 | 全量 integration 一次跑完（900s 仅 77%，13 文件未触达） | **W11 必须重跑真正跑完的全量**；与 C-2 同一动作 |
| NV-2 | 17 条 FAILED / 135 条 ERROR 逐条根因（QA 只对 4 条取栈） | W11 环境修复后逐条复核，不得凭分布一致性推断 |
| NV-3 | AC-20 生产 registry 实跑构造（需连库，现为静态核对 + unit 断言） | W11 |
| NV-4 | 容器运行态（对应 E-4） | **W11 容器实跑；W12 发布前必须补** |
| NV-5 | 测试写库残留清理（`DG_ADMIN_DSN` 不可用，降级为 warning） | W11 提供管理员凭据后处置；与 ② 的 1341 行残留一并处理 |

环境阻塞 `E-1..E-4`（§5）：**E-1** PG 5432 拒绝连接（本次复测仍 `ConnectionRefusedError`，另 55432 亦 CLOSED；MySQL 3306 OPEN）｜**E-2** `DG_ADMIN_DSN` root 1045｜**E-3** 控制库残留（已由 830 漂移到 1341，见 ②）｜**E-4** W07 NV-1 容器运行态未跑。E-2/E-4 本次依只读边界**未做登录/容器探测**，维持文档登记状态。

### 4.3 已知偏离 `KD-1..KD-5`（**W09 全部承接**）

| 编号 | 偏离 | W09 处置 |
|---|---|---|
| KD-1 | 执行阶段复用原审批被 Gateway 拒时不补发 `COMPENSATION_DENIED`（只发 `APPROVAL_ALREADY_CONSUMED`） | **P0，T03 承接**：补发 `COMPENSATION_DENIED(reason=REUSES_ORIGINAL_APPROVAL)`；修前不得宣称「补偿拒绝事件完整」 |
| KD-2 | `RECONCILIATION_SWEPT.scanned` 是产出结论条数而非全表扫描行数 | **T06 承接**：扩展 `sweep()` 返回值，补真实扫描总数（PM 已落位） |
| KD-3 | 调度事件挂伪任务 `system:worker_scheduler`，`tenant_id` 过滤列为 NULL，审计 API 按租户查不到 | **T06 承接**：明确检索方式或补租户归属；此前写入运维手册限制说明 |
| KD-4 | `reconcile_interval_s` 只被校验、未被读取（节奏实际由 `worker_poll_interval_s` 决定） | **T06 承接**：明确语义或删除该配置 |
| KD-5 | `sweep` 失败计入 `RunReport.failed`（无专用计数位） | **W09 或 W11**：增 `reconcile_failed` 计数位（**KD-5 可延 W11**）；当前以 `RECONCILIATION_FAILED` 事件为权威 |

> 时点硬约束：**W11 必须重跑一次真正跑完的全量回归**（C-2 / NV-1）；**W12 发布前必须补容器实跑**（NV-4 / E-4，对应 C-3）。
> W09 文档承接结构：`docs/w09/01-prd.md` §8 上游遗留映射、`docs/w09/02-design.md` §7 W08 门禁承接项、`docs/w09/03-task-cards.md` T03/T06 卡。

---

## 5. 验收禁令清单（W09 全局纪律）

- ❌ `pytest tests/integration`（全量，900s 仅 77% 会挂起）；需要集成证据只跑**定向单文件 + `timeout 180`**并注明。
- ❌ 「全量回归全绿」「1260 全绿」——C-2 已降级为分层证据，NV-1 明示一次跑完未验证。
- ❌ 把 NV-1..NV-5 当作通过；不得把 NV-2 的 17 FAILED/135 ERROR 说成"已逐条证明根因"。
- ❌ 把 PG 相关集成结果计入通过（E-1 未解决）；不得用 `tasks=830/763` 描述当前库。
- ❌ 把 `DG_ADMIN_DSN` 1045 导致的 fixture teardown ERROR 直接判为新代码缺陷。
- ❌ 用裸 `python`（PATH 3.13.14）跑测试；必须 `.venv/Scripts/python.exe`。
- ❌ 裸 `-q` 统计（`addopts="-q"` 叠加成 `-qq` 会吞统计行）；必须带 `-o addopts=""`。
- ❌ 执行 `rollback/008_rollback.sql`、`scripts/migrate.py` 或任何 INSERT/UPDATE/DELETE 来"造基线"。

---

## 6. 结论

**W09 T01（脱敏引擎，`core/redaction/*`，纯离线单元层）开工无硬阻塞。**

理由：`891 passed / 0 failed`、`1260 tests collected / 0 import error` 可稳定复现；T01 不依赖 PG 5432（E-1 未解决）、`DG_ADMIN_DSN`（E-2 1045）、容器运行态（E-4/NV-4 未跑），也不受 ② 的控制库残留规模影响。§3 三处漂移均为**记账项**，不是 T01 阻断项。

W09 全局的验收纪律见 §5；上游承接与 W11/W12 时点硬约束见 §4，不得以「已通过」口径引用。

---

## 7. 复现命令（只读）

```bash
# 1) 单元基线
timeout 180 ./.venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider

# 2) 收集完整性
timeout 180 ./.venv/Scripts/python.exe -m pytest --collect-only -o addopts="" -q -p no:cacheprovider

# 3) 锚点
git log -3 --oneline
git tag -l
git rev-parse w08-close w07-close HEAD
git status --porcelain

# 4) 迁移 008 在库（只读 SELECT / information_schema，禁止 DDL/DML）
#    见本文 §1 的四项断言口径

# 5) 环境连通性（仅 TCP，不登录）
#    MySQL 127.0.0.1:3306 / PG 127.0.0.1:5432 / PG 127.0.0.1:55432
```
