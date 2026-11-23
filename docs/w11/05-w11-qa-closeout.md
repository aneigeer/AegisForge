# W11 定向验收与 QA 收口（安全回归与长稳）

- **署名**：Edward（严过关）— QA Engineer，团队 `software-dbguardian-w11`
- **时点锚**：`HEAD = 2850eca`；W09/W10/W11 全部改动**未提交**；`git diff --name-only` = **20**、`git status --short` = **93**（20 ` M` + 73 `??`）
- **验收性质**：**定向验收**（不是门禁签署）。**W11 门禁状态：未评**——门禁由主理人裁定，本文件不代签。
- **轮次**：本文件含 **2 轮**记录。§1–§10 为**第 1 轮**原始记录（保留不改，便于追溯）；**§11 为第 2 轮 W11-CORR-02 复验，最终生效**。凡与 §11 冲突处，以 §11 为准（§1 相关 4 行、§4 相关 4 行已就地标注 `R2` 并更新）。
- **结论（一句话）**：**第 1 轮定向验收：通过**；**第 2 轮复验：通过**（4 项守卫补丁独立证伪全部生效）。最终 AC 覆盖 **已覆盖 19 / 部分覆盖 2 / 未覆盖 0**（AC-03、AC-12 因 KD-W11-01/02/03 移交 W12 仍记部分覆盖）。仍遗留：**5 项 `ENV-*` + `ENV-RESIDUE` 未解**、**长稳未实跑**。
- **未执行**：`git commit` / `git add` / `git checkout` / `git reset`（全程零提交，见 §8）。
- **未执行**：任何 `DELETE` / `UPDATE` / `TRUNCATE`（控制库历史残留未清理，见 §7）；未触碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`。

> 本报告不以「重跑工程师命令通过」作为完成依据：AC 矩阵、证伪探针、计数口径、覆盖强度均为 QA 侧独立核对，探针脚本落在仓库外临时目录（`%TEMP%/w11_qa_probe.py`、`%TEMP%/w11_qa_dbprobe.py`），**未在仓库内新增/修改任何文件**。

---

## 1. AC-01…AC-21 覆盖矩阵

判据以 `docs/w11/03-task-cards.md:498-527` 验收总表为准；AC-21 以 `docs/w10-w12-lead-rulings.md` §12.1 的**现场事实更新**为准（该裁定覆盖卡面旧前提，见表下注）。

| AC | 判据要点 | 覆盖判定 | 证据文件 / 用例名 | QA 独立核对 |
|---|---|---|---|---|
| AC-01 | 负面集四元组完整（非空率 100%） | **已覆盖** | `tests/integration/test_w11_security_negative.py`（32 passed）；`core/assurance/negative.py:365` `EVIDENCE_EMPTY`、`:419` 四元组校验 | 29/29 case 均有 `expected_deny_reason`（脚本核验，缺失 0） |
| AC-02 | 诱导逐条被拒；用例数 == 冻结清单数 29 | **已覆盖** | 同上；`test_w11_negative_manifest.py`（unit） | 独立读 `benchmarks/w11/negative_cases.json`：`version=negative-set-v1`、29 条、`G1=9/G2=7/G3=7/G4=6`、`LIVE 23 / STATIC 6` |
| AC-03 | 拒绝不改数据（前后计数相等，按本卡 `task_id` 过滤） | **部分覆盖**（见 L-9；**R2 维持**，KD-W11-03 移交 W12） | 同上；`negative.py:334-342` | SQL 为 `WHERE task_id = %s`（非全表）；但 G1–G3 用哨兵归属，前后均为 0 → 仅 G4（6/29）非空验证 |
| AC-04 | manifest `test_id` 无重复、版本号非空 | **已覆盖** | `tests/unit/test_w11_negative_manifest.py` | 独立核验：29 个 `test_id` 去重后仍 29，版本非空 |
| AC-05 | 六类对象 × 跨租户 → `OBJECT_AUTH_DENIED` 且返回集为空；含伪任务边界 | **已覆盖** | `tests/integration/test_w11_cross_tenant.py`（**16 passed**） | `CROSS_TENANT_PROBE_IDS` 实测 7 项（6 类 + 伪任务 `system:worker_scheduler`） |
| AC-06 | 并发零重复副作用（10 类） | **已覆盖** | `tests/integration/test_w11_concurrency.py`（**19 passed**） | `CONCURRENCY_CASE_IDS` 实测恰 10 项 |
| AC-07 | 旧 Worker → `FENCING_TOKEN_STALE`；双 Worker → `WORKER_CONFLICT` | **已覆盖** | 同上 | `EXPECTED_DENIAL_CODES = {FENCING_TOKEN_STALE, WORKER_CONFLICT}` |
| AC-08 | Audit Writer 失败 + R2 → `AUDIT_WRITE_FAILED`、`tool_calls_delta==0` | **已覆盖** | `tests/integration/test_w11_fault_injection.py`（**16 passed**） | `AUDIT_WRITER` 走 `emit_required`（fail-closed） |
| AC-09 | R0 按策略返回且 `alert_emitted is True` | **已覆盖** | 同上 | `FaultInjectionResult.alert_emitted` 参与 `inputs_digest`（`publish.py:383`） |
| AC-10 | 六类恢复后 `seq` 连续、`state_version` 单调、Evidence 引用可解析 | **已覆盖** | 同上 | `RecoveryPredicates` 六项均参与红线判定（`publish.py:300-311`） |
| AC-11 | 恢复不重放（`tool_calls_delta==0`、无 `TOOL_CALL_RETRIED`） | **已覆盖** | 同上 | `_fault_failures` 对 `REPLAY_OCCURRED` / `TOOL_CALLS_DELTA` 判红 |
| AC-12 | 备份实际恢复并校验四类计数逐值相等 | **部分覆盖**（见 L-2 / L-3；**R2 维持**，KD-W11-01/02 移交 W12） | `tests/integration/test_w11_backup_restore.py`（**22 passed**，R2 复跑亦 22） | 逐值相等字面成立；但 4 类中 3 类快照为空（0==0），且设计 §8.2 要求的 `approval_consumptions` / `step_prepared` **未纳入快照** |
| AC-13 | RPO/RTO 目标与实测分列；`measured_* is None` → `sla_claim is False` | **已覆盖** | 同上；`BackupRestoreReport._no_sla_without_measurement` | `test_report_model_rejects_overclaim` 7 组参数；实测报告 `null/null`、`sla_claim=False` |
| AC-14 | 长稳 `sample_size>0`/`duration_s>0`/`smoke_test is False`；告警 6 项集合相等 | **已覆盖**（**R2 升级**：L-8 补丁后守卫双向） | `tests/unit/test_w11_soak_report.py`（**23 passed**）；`docs/w11/backup-restore-report.md:88-91` | R1：交付件诚实但「计划值不得回填实测」单向。**R2**：`status=PLANNED` ⇒ `measured_*`/`p50`/`p95` 必须全 `None`，5 条组合实测全拒；`COMPLETED + 200/3600` 反向对照通过；`smoke_test=True` 仍拒 |
| AC-15 | RC 清单逐项有值；缺项 `UNKNOWN` + 登记；版本与 manifest/迁移一致 | **已覆盖** | `tests/unit/test_w11_rc_manifest.py` | `commit` 由 `git rev-parse` 取得（= `2850eca…` 40 位）；`migration_versions` 11 项；4 条 `ISSUE-*` 齐备 |
| AC-16 | E2E 四段各自断言；PG 段 `NOT_EXECUTED` 不判 FAIL | **已覆盖** | `tests/integration/test_w11_e2e_regression.py`（**5 passed**） | S1 `PASSED`（PG `NOT_EXECUTED` 且显式 `!= FAILED`）；S2/S3/S4 `PASSED` |
| AC-17 | 全绿 → `ALPHA_ALLOWED`；注入 1 条红线失败 → `TECH_PREVIEW_ONLY` | **已覆盖** | `tests/unit/test_w11_publish_decision.py`；QA 探针 B/C 复现 | 红线命中 → `TECH_PREVIEW_ONLY`/`RED_LINE_FAILED`；空集+无 env block → `TECH_PREVIEW_ONLY`/`ENV_BLOCK_UNRESOLVED` 且 `failed_test_ids == ()` |
| AC-18 | 签名无 `force_allow`/`override`；限制清单自动含三标记且自动项不可删 | **已覆盖**（**R2 升级**：L-5 补丁后正常构造路径不可伪造） | `tests/unit/test_w11_publish_decision.py`（**43 passed**）；QA 探针 A/E（R1）+ R2 六路径 | R1：函数层成立，值对象层可伪造。**R2**：`__post_init__` 后直接构造三条 + `replace()` 三条**全部 `PUBLISH_DECISION_INCONSISTENT`**；反向对照（`TPO` 带 failed、`ALPHA_ALLOWED` 三字段全空）通过；真实 `publish_decision()` = `TECH_PREVIEW_ONLY`、`failed_test_ids=()`。`object.__setattr__` 仍可绕 → 记 `IN_PROCESS_BYPASS`（不在威胁模型内） |
| AC-19 | 写面与引擎不回退 | **已覆盖**（未被替代） | **既存** `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py`（**11 passed = 6+5**） | 两文件真实执行；未新建替代用例；`e2e_regression` 文件头明示「不重复实现、不另造替代」 |
| AC-20 | 冻结 P0 门禁清单 9 项逐项 `present is True` | **已覆盖**（**R2 加固**：L-7 补丁后 `gate_ids` 不可裁剪） | `tests/unit/test_w11_gate_inventory.py`（**28 passed**）；`core/assurance/gates.py` | `GATE_IDS` 恰 9 项；逐项置 `False` → 全部 `GateRemovedError(GATE_REMOVED)`；判定以行为/AST 为主，非 grep（表 §2-G）。**R2**：`gate_ids` 传 8 项/换序/空/10 项（含 `model_validate` 路径）**全拒**；`check_gate_inventory(REPO)` 仍 9/9 全 True |
| AC-21 | 上游指标口径未回退 | **已覆盖**（按 §12.1 实测口径；**R2 加固** L-4） | **W10** `tests/unit/test_w10_metrics.py`（**21 passed**，真跑）；`tests/unit/test_w11_rc_manifest.py`（**22 passed**） | 真跑而非只断文件存在；`metrics-v1.json` 内容独立核验：`version=metrics-v1`、`definitions=14`。**R2**：`frozen_evidence_available` 改为内容校验（`version` + `definitions` 长度 14 + 测试文件含 `def test_`）。**残余**：只校验长度、不校验元素内容 → 14 个空对象仍判 `True`（见 §11.1，登记为已知限制，不降级） |

**统计**：**第 1 轮** 已覆盖 **17** / 部分覆盖 **4**（AC-03、AC-12、AC-14、AC-18）/ 未覆盖 **0**；**第 2 轮（最终）** 已覆盖 **19** / 部分覆盖 **2**（AC-03、AC-12）/ 未覆盖 **0**。合计 21。

> **AC-21 口径说明**：卡面 `03-task-cards.md:472` 写「`test_w10_metrics.py` 尚不存在 → `NOT_EXECUTED`、`frozen_evidence_available=False`」，该前提已被 `docs/w10-w12-lead-rulings.md` §12.1 **覆盖更新**为「按实测执行」。本报告以 §12.1 为准，同时保留「W10 门禁未评」的边界：**不写「同一冻结版本已复核」，不另造 `metrics-v2`**。

---

## 2. 独立证伪结果（逐项）

> 方法：读源码 + 仓库外探针脚本 + 只读库查询。**不修改** W11 生产代码与既有断言。

| # | 怀疑项 | 证伪结论 | 依据 |
|---|---|---|---|
| **2-A** | T05 `audit_chain → step_observed` 是否有设计依据、是否把「审计链」偷换成「执行回执」 | **无设计依据；属命名/口径偏差**（L-1） | ① `backup.py:23-31` 同一 docstring 内，`:27` 称 `task_events` 为「追加式事件（**哈希链本体**）」，`:30` 却把 `step_observed` 标为 `audit_chain`；② 真实审计链由 `AuditService.verify` 对 `task_events` 校验（`backup.py:474`）；③ `docs/w08/01-prd.md:67` 定义 `step_observed` 为「只追加**证据表**」，`docs/w08/02-design.md:41` 称其为「执行回执」；④ 设计 §8.2 ① 的分桶是 `task_events / approvals(+approval_consumptions) / step_prepared+step_observed / evidence`，**没有** `audit_chain` 这个逻辑类。物理表落到 `step_observed` 本身正确，错的是**逻辑类名**，易被读成「审计链已单独备份」 |
| **2-B** | T05 是否漏了设计要求的对象 | **漏 2 项**（L-2） | 设计 §8.2 ① 明确列 `approvals(+approval_consumptions)` 与 `step_prepared+step_observed`；`BACKUP_OBJECT_TABLES`（`backup.py:67-72`）只覆盖 `task_events / approvals / evidence / step_observed`，**未含 `approval_consumptions`、未含 `step_prepared`**。故「四类」≠ 设计四桶（设计实为 5 张表） |
| **2-C** | `counts_after_restore` 四类相等是否真证明「点恢复」而非「计数巧合」 | **字面成立，但强度不均**（L-3） | `counts_before = {task_events:1, approvals:0, step_evidence:0, audit_chain:0}` → 3 类的「逐值相等」是 **0==0**；测试 `test_appended_rows_are_not_in_shadow_store:253-255` 的**正向回放**循环对空类**空真**。真正非空证据只有 `task_events`（快照 1 行）。**缓解**：`test_append_inserts_exactly_twelve_rows:224-226` 断言每类 `delta==3`，`:238-240` 断言 `after_append > before`，故「追加确实落库、且未进影子存储」仍成立 |
| **2-D** | S2 的写复原：`note` 是否真回到原值、有无额外残留、是否 W07 冻结协议允许的唯一写动作 | **成立** | `tests/integration/test_w11_e2e_regression.py:178-180` 在 `finally` 执行 `RESTORE_SQL`；`RESTORE_SQL = "UPDATE dg_lab.seed_orders SET note = %s WHERE order_id = %s LIMIT 1"`（`test_w07_repair_e2e.py:40`），与 W07 自身 `:239`/`:262` **同一语句、同一用法**（还原为「执行前读到的 `before`」，非硬编码值）。仅改 `note` 一列、限 1 行；S2 断言行只看 `tool_calls==1`/`consumptions==1`/目标行等于批准值 |
| **2-E** | `GATE_IDS` 的 `present=True` 是否只做「字符串存在」弱检查 | **非弱 grep，但有 3 项依赖字面量**（见 2-G / L-7） | 逐项看实现（`gates.py:94-237`）：真实注册表 + `side_effect` 校验、真实 `PolicyEngine.decide` 探针、`inspect.signature` 强制 `tenant_id`、全仓 **AST** 扫 `ToolGateway(` 调用点。仅 `HUMAN_APPROVAL`（`inspect.getsource` 内含 `"approval_grant is None"`）与 `INDEPENDENT_VERIFIER`（`"readback = MysqlReadbackAdapter(ro_client)"` 字面匹配）为源码字符串判定 |
| **2-F** | `frozen_evidence_available=True` 是否基于内容（14 项定义/版本） | **否，仅 `is_file()`**（L-4） | `rc.py:306-308` 只做两文件 `is_file()`；`test_w11_rc_manifest.py:144-148` 把该语义**固化为断言**。**探针实证**：临时目录放 `{}` 空内容 + 空测试文件 → `frozen_evidence_available = True`（无内容/版本校验）。**但 AC-21 本身是真跑的**（21 passed），且 `metrics-v1.json` 内容独立核验为 `version=metrics-v1`、`definitions=14` |
| **2-G** | `publish_decision` 是否恒 `TECH_PREVIEW_ONLY` 且无可覆盖入口 | **函数层成立；值对象层可伪造**（L-5） | 探针 A：`inspect.signature` = 4 个 **keyword-only** 形参，`allow/override/force/skip/bypass` 命中 **0**。探针 B：注入 `DG_ALLOW_ALPHA/FORCE_ALLOW/ALPHA_ALLOWED/DG_OVERRIDE` 后红线结果不变（`TECH_PREVIEW_ONLY`/`RED_LINE_FAILED`）。**探针 D**：`PublishDecision` 为 frozen dataclass 但**无自校验** → 直接构造 `decision=ALPHA_ALLOWED, failed_test_ids=("x",)` **成功**；`dataclasses.replace(d, decision=ALPHA_ALLOWED)` **成功改写结论** |
| **2-H** | `limitations.json` 是否单一真值（改 marker / 删自动项是否真拒） | **成立** | 探针 E：自动项 **6**、marker = `{NOT_SUPPORTED, PARTIAL, UNKNOWN}` 恰三值；**删除 / 改 marker / 改 statement / AUTO→MANUAL 四条路径全部被拒**（`LIMITATION_REMOVED`）。探针 F：`build_rc_freeze` 对 decision 不一致抛 `ValueError`。磁盘件 digest = `89f75988f6b7b2e6d5950b25217ec74942b187523099d8bc5aaba8484a18f4f3`，与 `rc-freeze.md` 内嵌块一致（`test_freeze_embeds_limitations_same_source` 通过） |
| **2-I** | 长稳计划值/实测是否真分离，有无回填 | **交付件诚实，但守卫单向**（L-8） | `build_soak_report(measured=None)` → `sample_size=200 / duration_s=3600 / smoke_test=False / measured_*=None / status=PLANNED / p50=p95=None`；报告件 `:88-91` 同。`COMPLETED` 无实测 → 拒；`smoke_test=True` → 拒。**但** `SoakReport(measured_sample_size=200, measured_duration_s=3600.0)`（`status` 仍 `PLANNED`）**被接受**，且 `assert_soak_not_overclaimed` **未拦住** |
| **2-J** | W08 改写边界（1 个 hunk？断言未动？runner/reconciler 零改动？） | **成立** | `git diff` 该文件**恰 1 个 hunk**：仅 `monkeypatch.setattr(..., "sweep", ...)` → `"sweep_detailed"`（+3 行说明注释），断言行与其余 6 例一字未动。`workers/runner.py` / `core/execution/reconciler.py` 虽在 20 个 ` M` 中，但**对 20 个文件逐一 grep `w11`：仅 `test_w08_crash_matrix.py` 命中 1 次**（CORR-01 注释），其余 19 个（含 runner/reconciler）命中 **0** → 其改动早于 W11。`runner.py:466-468` 确证「优先 `sweep_detailed()`」 |
| **2-K** | 计数口径（是否误用全表计数） | **成立** | `backup.py:453-463` `_select` 为 `WHERE task_id IN (%s…)`；negative `count_target` 为 `WHERE task_id = %s` / `WHERE tenant_id = %s`（`test_w11_security_negative.py:135-149,960-976`）。**无任何 `COUNT(*)` 全表计数** |
| **2-L** | 计数口径的补充保留 | **G1–G3 空真**（L-9） | G1–G3（23/29）用哨兵归属 `w11-neg-scope:<test_id>` / `w11-neg-tenant:<test_id>`，永不命中 → 前后均 0；仅 G4（6/29）计真实对象 |

---

## 3. 串行现场数据（QA 侧真实执行）

执行纪律：解释器 `.venv/Scripts/python.exe`（3.14.7）；每条均带 `-o addopts="" -q -p no:cacheprovider`；**全程串行，两个 pytest 无并发**；集成用外层 `timeout 180/420`；**未使用 `--timeout=`**。

| # | 命令 | 真实结果 | 与工程师口径对照 |
|---|---|---|---|
| 1 | `pytest tests/unit` | **1721 passed in 17.42s** | 一致（1721） |
| 2 | `pytest tests/unit/test_w10_metrics.py`（AC-21） | **21 passed in 0.38s** | 一致（21，真跑） |
| 3 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py`（AC-19） | **11 passed in 0.32s** | 一致（6+5） |
| 4 | `timeout 420 … tests/integration/test_w08_crash_matrix.py` | **7 passed in 207.57s** | 案例数一致（7）；耗时 207.57s（工程师 155.8s，机器差异；**仍在 420s 窗口内**） |
| 5 | `timeout 180 … test_w11_security_negative.py`（T01） | **32 passed in 3.97s** | 一致（32） |
| 6 | `timeout 180 … test_w11_cross_tenant.py`（T02） | **16 passed in 2.01s** | 一致（16） |
| 7 | `timeout 180 … test_w11_concurrency.py`（T02） | **19 passed in 0.47s** | 一致（19） |
| 8 | `timeout 180 … test_w11_fault_injection.py`（T03） | **16 passed in 4.90s** | 一致（16） |
| 9 | `timeout 180 … test_w11_backup_restore.py`（T05） | **22 passed in 2.57s** | 一致（22） |
| 10 | `timeout 180 … test_w11_e2e_regression.py`（T06） | **5 passed in 12.09s** | 一致（5） |

**集成小计**：7+32+16+19+16+22+5 = **117 passed / 0 failed**（10 条命令合计 117 例集成 + 1721 例单元）。

**异常终止与重跑**：本次 10 条命令**均正常结束（exit 0）**，**未发生** `Windows fatal exception: access violation` / exit 139 / exit 124，因此**无隔离重跑**、无「首次失败」登记。若后续复跑出现异常，须按纪律单独登记首次失败与重跑结果。

---

## 4. 已知问题与残余风险（9 项）

> 全部为**代码已通过**但**实现弱于设计措辞 / 证据强度不足 / 口径偏差**类问题，**无失败用例**。按纪律，QA **不得**修改 W11 生产代码与既有断言来「造」一个失败测试，故不构成本轮 `Engineer` 路由触发条件。

| ID | 等级 | 问题 | 影响 | 建议处置 |
|---|---|---|---|---|
| **L-1** | 中（口径） | `audit_chain` 逻辑类名映射到 `step_observed`，与设计 §8.2 / W08 语义不符；同一 docstring 内 `task_events` 已自称「哈希链本体」 | 报告读者可能误读为「审计链已单独备份」；物理覆盖无误 | 改名（如 `step_observed` / `execution_receipt`）需改冻结逻辑类名，**须主理人裁定**；本轮仅在报告披露 |
| **L-2** | 中（覆盖） | 快照未含设计 §8.2 明列的 `approval_consumptions` 与 `step_prepared` | AC-12 覆盖弱于设计 | 交接 W12 补桶；本轮不擅自加 |
| **L-3** | 中（证据强度） | 4 类中 3 类快照为 0，`test_appended_rows_are_not_in_shadow_store:253-255` 正向回放对空类空真 | 点恢复对 3/4 类仅弱证明 | 建议 W12 让每类在备份前各预置 ≥1 行 |
| **L-4** | 低（守卫） | `frozen_evidence_available` 仅 `is_file()`，无内容/版本校验；临时目录空 `{}` 即判 `True` | 冻结件被清空仍会亮绿灯 | **→ R2 已修补**（`_frozen_evidence_available`：version + 14 项 + `def test_`）；残余：只校长度不校元素（见 §11.1） |
| **L-5** | 中（可用性/安全） | `PublishDecision` frozen dataclass 无自校验，可直构 `ALPHA_ALLOWED` 或 `dataclasses.replace` 改写 | 「不可人工覆盖」仅对**函数**成立，值对象层可伪造（需代码级配合，非 env/配置） | **→ R2 已修补**（`__post_init__`：`ALPHA_ALLOWED` ⇒ 三字段必须全空）；`object.__setattr__` 仍可绕 → `IN_PROCESS_BYPASS`（§11.2） |
| **L-6** | 低（API 语义） | `LimitationRemoved` 在 pydantic 校验器内抛出后被包成 `ValidationError`，其声明的 `code="LIMITATION_REMOVED"` 无法按异常类型捕获 | 调用方只能靠消息文本识别 | 文档标注，或提供显式 `recheck()` 入口 |
| **L-7** | 低（守卫） | `GateInventory.gate_ids` 为调用方可传字段：`GateInventory(gate_ids=("ENV_ISOLATION",), present={...:True})` **可构造成功**，绕过「9 项」 | 生产入口 `check_gate_inventory()` 使用模块级 `GATE_IDS`，**实际路径安全** | **→ R2 已修补**（`_gate_ids_are_frozen`：顺序与内容须全等）；4 种裁剪 + `model_validate` 全拒（§11.3） |
| **L-8** | 中（守卫） | 「计划值不得回填实测字段」**单向**：`status=PLANNED` + `measured_*=200/3600` 被接受，`assert_soak_not_overclaimed` 不拦 | 未来回填可能不被拦截；当前交付件诚实 | **→ R2 已修补**（`status=PLANNED` ⇒ `measured_*`/`p50`/`p95` 全 `None`，5 条组合全拒）；`RUNNING + measured` 仍被接受（合理，见 §11.4） |
| **L-9** | 低（证据强度） | AC-03 对 G1–G3（23/29）用哨兵归属，前后计数恒 0 | 「拒绝不改数据」仅 6/29 非空验证 | 建议 G1–G3 也挂真实对象或明确标注「结构性空真」 |

**W09 存量告警（未动）**：`docs/w09/06-qa-report.md:624` 的 `new blank line at EOF` 为 W09 存量 ` M`，按纪律**不改**，并入 W09 收口件。

**文件计数小差异**：设计 §13.1 与工程师口径为「28 个新文件」；QA 实测落盘 **31 个**（`core/assurance/*.py` 9 + `benchmarks/w11/*` 4 + `docs/w11/*` 7 + `tests/**/test_w11_*.py` 11），其中 `docs/w11/01-prd.md` **已被 git 跟踪**（`git ls-files` 可见），余 30 个未跟踪。差异疑为「源文件计 28、不含 2 个生成件 `rc_manifest.json`/`limitations.json` 与 `core/assurance/__init__.py`」。**不影响验收**，如实登记。

---

## 5. 环境阻塞登记（`ENV-*`，5 项全部未解 + 残留）

| 编号 | 现状 | 对 W11 的影响 | 处置（已执行） |
|---|---|---|---|
| `ENV-PG5432` | PG LAB 5432 拒连 | 跨引擎诊断 PG 段、PG 侧跨租户/负面集不可执行 | E2E **S1 `PASSED`**（MySQL 段真实只读）、PG 段 **`NOT_EXECUTED`**；**不判 FAIL、不冒充通过** |
| `ENV-1045` | `DG_ADMIN_DSN` root 1045 | 不能做物理库级恢复 | `restore_mode="LOGICAL_SHADOW"`、`physical_restore="NOT_SUPPORTED"` + 登记；**未碰账号/`.env`** |
| `ENV-RESIDUE` | **持续增长** | 连库用例继续累加 | 见 §6；**全程未 DELETE/UPDATE/TRUNCATE** |
| `ENV-NV1` | 容器运行态 `NOT_VERIFIED` | RC 镜像 digest 不可取 | `db_image_digest="UNKNOWN"` + `ISSUE-DB-IMAGE-DIGEST`；`container_runtime_status="NOT_VERIFIED"`；**仅配置层已实测，运行态未验证** |
| `ENV-MODEL` | `BLOCKED_MODEL_ENV_MISSING` | 模型版本/TTFT 不可测 | `model_version=None` + `ISSUE-MODEL-VERSION`；相关字段 `null`；**未下载模型、未联网、未填估计值** |
| **RPO/RTO** | 未做物理恢复 ⇒ 实测缺席 | — | `measured_rpo_minutes` / `measured_rto_hours` = `null`；`sla_claim=False`；`sla_note="建议目标，未验证 SLA"`。15 分钟 / 4 小时**仅为建议目标**，非门禁硬阈值 |
| **长稳** | **未实跑** | 无 1 小时长稳数据 | `status=PLANNED`、`measured_sample_size`/`measured_duration_s` = `null`、`smoke_test=False`；**不以短时 smoke 冒充** |

---

## 6. `ENV-RESIDUE` 现状（只读统计，未清理）

QA 侧只读查询（`SELECT`，未使用任何写语句）：

| 表 | 行数 |
|---|---|
| `dg_system.task_events` | **78319** |
| `dg_system.tasks` | 1766 |
| `dg_system.approvals` | 597 |
| `dg_system.evidence` | 326 |
| `dg_system.step_observed` | 171 |

- 工程师回传 `task_events = 77433` → QA 实测 **78319**（**+886**，由本轮及先前复跑的只追加用例累加），**证实 `ENV-RESIDUE` 持续增长**。
- 所有 W11 计数均按本卡 `task_id` / `run_id` 过滤（§2-K），**不依赖全表数**。
- **未执行任何清理**；**不得表述为「环境已清零」**。
- 说明：只读 client 拦截 `LIKE '%…%'`（防全表扫描），故未做前缀扫描统计；未尝试绕过该只读保护。

---

## 7. 上游口径与表述禁令自查

| 项 | 本报告取值 |
|---|---|
| W07 | 「**有条件放行；门禁通过：否**」 |
| W08 | 「有条件放行」 |
| W09 | 「门禁未评」 |
| W10 | 「门禁未评」 |
| W11 门禁 | **未评**（本文件只给「定向验收」结论，不代签门禁） |
| 容器 | 「配置层已实测 / 运行态 `NOT_VERIFIED`」 |
| SLA | 「建议目标，未验证 SLA」 |
| 恢复 | 未声称「全部可自动恢复」；六类边界 `AUTO 3 / MANUAL 3`（`AUDIT_WRITER`/`DB_DISCONNECT`/`WORKER_CRASH` = MANUAL） |

**禁用表述自查**（逐条确认本文件未使用）：未写「W07 已收口 / 门禁通过 / 放行（作为肯定断言）」、「容器网络隔离已验证」、「环境已清理 / 已可复现清零」、「QA 独立实跑通过」、「回归全绿」、「Alpha 已就绪」、「已验证 SLA」、「全部可自动恢复」。本报告用词为「定向验收通过」「本次串行现场无失败用例」。

---

## 8. 变更与提交状态

- **未执行** `git commit` / `git add` / `git checkout` / `git reset` / `git stash`；**零提交**。
- QA **未修改** W11 生产代码、**未修改**任何既有断言、**未新增**仓库内文件（探针脚本在仓库外 `%TEMP%`）。
- 基线保持：`HEAD = 2850eca`、`git diff --name-only` = 20、`git status --short` = 93（20 ` M` + 73 `??`），与交接时一致。
- 冻结资产未触碰：`benchmarks/w10/**`、`benchmarks/baseline_v0/**`、`benchmarks/faults/**`、`docs/w08|w09|w10/**`（含 `docs/w09/06-qa-report.md:624`）。

---

## 9. 智能路由判定

**路由：`NoOne`**

依据（按 `Engineer` / `QA` / `NoOne` 三选一的决策规则）：

- **`Engineer`（源码有 Bug）不成立**：本轮 10 条串行命令 **0 failed**（单元 1721 + 集成 117），**不存在**「断言正确、实现错误」的失败用例。§4 的 9 项均为「无失败用例」的弱守卫/口径偏差，且纪律禁止 QA 改 W11 断言去制造失败用例来触发该路由。
- **`QA`（测试自身有 Bug）不成立**：未发现断言写错；4 项「部分覆盖」是**证据强度**问题，非断言错误。
- **`NoOne` 成立**：全部执行用例通过，残余风险已按 §4 如实登记为 Known Issues。

**轮次**：第 **1** 轮即收敛（未进入第 2 轮）。**不写成「2 轮通过」，也不写成「回归全绿」**——准确表述：**本轮串行现场无失败用例；9 项残余风险与 5 项环境阻塞结转。**

> **R2 更新**：第 2 轮（W11-CORR-02 守卫补丁复验）的最终路由仍为 **`NoOne`**，详见 **§11.6**。上段「第 1 轮」表述仅描述第 1 轮事实，最终判定以 §11.6 为准。

---

## 10. 交付清单

| 项 | 值 |
|---|---|
| AC 覆盖（R1） | 21 条：已覆盖 **17** / 部分 **4** / 未覆盖 **0** |
| AC 覆盖（**R2 最终**） | 21 条：已覆盖 **19** / 部分 **2**（AC-03、AC-12）/ 未覆盖 **0** |
| 单元现场 | R1 `1721 passed`；**R2 `1731 passed`**（4 次尝试中 2 次 exit 139，见 §11.5） |
| 集成现场 | R1 `117 passed / 0 failed`；**R2 复跑 `22 + 5 + 7`（backup_restore / e2e / crash_matrix）全通过** |
| 证伪项 | R1 12 组（§2）；**R2 4 组守卫补丁独立证伪全生效**（§11.1–§11.4） |
| 路由判定 | **`NoOne`**（R1、R2 一致） |
| 已知问题 | R1 **9 项** → R2 **4 项已修补**（L-4/L-5/L-7/L-8）；**仍遗留 5 项**（L-1/L-2/L-3 转 KD-W11-01/02/03、L-6、L-9）+ 2 项 `IN_PROCESS_BYPASS`/内容深度限制 |
| 环境遗留 | `ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE`（**R2 实测 79313**，增长中） / `ENV-NV1` / `ENV-MODEL`；**长稳未实跑**；W09 存量 blank-line 告警 |
| git 提交 | **未执行（零提交）**；`git diff --name-only` 仍 20、`git status --short` 仍 94 |

---

## 11. 第 2 轮：W11-CORR-02 守卫补丁复验（**最终生效**）

- **复验对象**：主理人裁定的 4 项守卫补丁（L-4 / L-5 / L-7 / L-8）；L-1/L-2/L-3 **不改逻辑**，转 `KD-W11-01/02/03` 移交 W12。
- **复验原则**：**不重跑工程师的负测**，QA 自建临时仓库/自构输入独立证伪；临时脚本在仓库外 `%TEMP%/w11_qa_round2_probe.py`。
- **基线未变**：`HEAD = 2850eca`、`git diff --name-only` = **20**、`git status --short` = **94**；4 处补丁全在**未跟踪新文件**内；`test_w08_crash_matrix.py` 的 diff 仍**恰 1 个 hunk**。
- **威胁模型边界**：`object.__setattr__` / `model_construct()` 类**进程内恶意绕过不在威胁模型内**——实测能绕者**仅登记为 `IN_PROCESS_BYPASS` 已知限制，不判 FAIL、不要求再修、不因此降级 AC**。

### 11.1 L-4 `_frozen_evidence_available`（内容校验）独立证伪

QA 自建临时仓库（`benchmarks/w10/metrics-v1.json` + `tests/unit/test_w10_metrics.py`），**实际返回值**：

| 输入构造 | 期望 | **实测** | 判定 |
|---|---|---|---|
| ① 空 `{}`（无 version/definitions） | False | **False** | ✔ 修补生效 |
| ② `version` 对但 `definitions=[]` | False | **False** | ✔ |
| ③ **`version` 对 + `definitions` = 14 个空对象 `{}`** | ?（关键） | **True** | ⚠ **只校验长度，不校验元素内容**（已知限制，见下） |
| ④ `version` 对 + 14 个非空对象 | True | **True** | ✔ 正向可用 |
| ⑤ `version="metrics-v2"` + 14 项 | False | **False** | ✔ 版本闸生效 |
| ⑥ `definitions` = 13 项 | False | **False** | ✔ |
| ⑦ `definitions` 为 **dict**（14 键）而非 list | False | **False** | ✔ 类型闸生效 |
| ⑧ 14 项但测试文件**无 `def test_`** | False | **False** | ✔ 测试文件闸生效 |
| ⑨ `metrics-v1.json` 缺失 | False | **False** | ✔ |
| ⑩ **真实仓库** | True | **True** | ✔ |

**结论**：L-4 修补**在「正常内容」与 8 类退化输入上全部按预期**（较 R1 的 `is_file()` 有实质提升：版本、条数、类型、测试文件四项闸门均生效）。
**残余（不降级 AC-21）**：判据是 `len(definitions) == 14`，**不做元素级校验** → 14 个空对象仍判 `True`。该输入属**退化/伪造件**，非正常运行产物；且 AC-21 的实质证据是 `test_w10_metrics.py` **真跑 21 passed** + 真实件 `version=metrics-v1`/`definitions=14`，故 AC-21 **维持「已覆盖」**，此残余登记为已知限制（`CONTENT_DEPTH_LIMIT`）。工程师新增负测 `test_frozen_evidence_requires_real_content` 用的是**非空** 14 项，未覆盖该退化输入（故本项仍需 QA 独立发现）。

### 11.2 L-5 `PublishDecision.__post_init__` 独立证伪

| 路径 | 构造 | **实测** |
|---|---|---|
| 直接构造 ① | `ALPHA_ALLOWED` + `failed_test_ids=("t1",)` | **ValueError `PUBLISH_DECISION_INCONSISTENT`** ✔ |
| 直接构造 ② | `ALPHA_ALLOWED` + `red_line_failures=(RedLineFailure(...),)` | **ValueError `PUBLISH_DECISION_INCONSISTENT`** ✔ |
| 直接构造 ③ | `ALPHA_ALLOWED` + `environment_blocks=("ENV-PG5432",)` | **ValueError `PUBLISH_DECISION_INCONSISTENT`** ✔ |
| `replace()` ① | 合法 TPO（带 failed）→ `ALPHA_ALLOWED` | **ValueError** ✔ |
| `replace()` ② | 合法 TPO（带 blocks）→ `ALPHA_ALLOWED` | **ValueError** ✔ |
| `replace()` ③ | 合法 TPO（带 red_line）→ `ALPHA_ALLOWED` | **ValueError** ✔ |
| 反向对照 ① | `replace(TPO)` 带 failed，**保持 TPO** | **通过**（`TECH_PREVIEW_ONLY`）✔ 未误伤 |
| 反向对照 ② | `ALPHA_ALLOWED` 三字段全空 | **通过** ✔ 未误杀合法结论 |
| 真实函数 | `publish_decision(negative=(), concurrency=(), faults=(), environment_blocks=ENVIRONMENT_BLOCK_IDS)` | **`TECH_PREVIEW_ONLY`**、`failed_test_ids=()`、`blocks=[ENV-1045, ENV-MODEL, ENV-NV1, ENV-PG5432, ENV-RESIDUE]` ✔（环境阻塞属完备性守卫，不计失败用例） |
| 真实函数 | 空集 + 无 block | `TECH_PREVIEW_ONLY` / `ENV_BLOCK_UNRESOLVED` / `failed=()` ✔ |

**结论**：**正常构造与 `replace()` 六条伪造路径全部被拒，反向对照不误伤** → L-5 修补生效，AC-18 由「部分覆盖」升为**「已覆盖」**。

**`IN_PROCESS_BYPASS`（仅登记，不判 FAIL）**：`object.__setattr__` 可把已构造对象的 `decision` 改为 `ALPHA_ALLOWED` 并保留 `failed_test_ids`（实测绕过成功）。按主理人第 5 条裁定，进程内恶意绕过**不在威胁模型内**，登记为已知限制即可。

### 11.3 L-7 `GateInventory.gate_ids` 冻结独立证伪

| 输入 | `GateInventory(...)` 直接构造 | `model_validate` 路径 |
|---|---|---|
| 8 项（去末项） | **拒**（ValueError）✔ | **拒** ✔ |
| 换序（前两项对调） | **拒** ✔ | **拒** ✔ |
| 空 tuple | **拒** ✔ | **拒** ✔ |
| 10 项（多 1 项） | **拒** ✔ | **拒** ✔ |
| 正序 9 项 | **通过**，`count()==9` ✔ | — |
| `check_gate_inventory(真实仓库)` | **9/9 项 `present=True`** ✔（全 True） | — |

**结论**：`_gate_ids_are_frozen` 要求**顺序与内容全等**，4 种裁剪 + `model_validate` 共 8 条路径全拒，生产入口仍 9/9 → L-7 修补生效（AC-20 的 R1 保留项解除，维持**「已覆盖」**且加固）。

### 11.4 L-8 `SoakReport` PLANNED 不得夹带实测值独立证伪

| 构造 | **实测** |
|---|---|
| `PLANNED + measured_sample_size=200` | **拒**（`SOAK_OVERCLAIMED`）✔ |
| `PLANNED + measured_duration_s=3600.0` | **拒** ✔ |
| `PLANNED + p50_ms=1.0` | **拒** ✔ |
| `PLANNED + p95_ms=2.0` | **拒** ✔ |
| `PLANNED + measured_sample_size=0` | **拒** ✔ |
| `PLANNED` 纯计划值（默认） | **通过**（`measured=None/None`）✔ 未误伤 |
| `COMPLETED + measured 200/3600.0` | **通过** ✔ 反向对照 |
| `COMPLETED` 无实测（既有分支） | **拒** ✔ 仍生效 |
| `smoke_test=True`（既有分支） | **拒** ✔ 仍生效 |
| `RUNNING + measured_sample_size=200`（未定义区，仅观察） | **通过** — 见下 |
| `build_soak_report(measured=None)` | `PLANNED` / `measured=None/None` / `p50=None`；`assert_soak_not_overclaimed` 通过 ✔ |

**结论**：`status=="PLANNED"` 的四个字段闸门全部生效，反向对照不误伤 → L-8 修补生效，AC-14 由「部分覆盖」升为**「已覆盖」**。
**观察（非缺陷）**：`RUNNING + measured` 被接受——「运行中」携带已采集样本属合理语义，且 `status` 不会因此变成 `COMPLETED`，不构成「计划值冒充实测」。登记为**设计未定义区的口径选择**，不判缺陷。

### 11.5 回归（串行，无并发）

| # | 命令 | 真实结果 |
|---|---|---|
| 1 | `pytest tests/unit`（第 1 次） | **exit 139**（`Windows fatal exception: access violation`，`ast.py copy_location`，~2 s） |
| 2 | `pytest tests/unit`（第 2 次，隔离重跑） | **exit 139**（同点，~2 s） |
| 3 | `pytest tests/unit --collect-only` | **exit 0，1731 tests collected in 3.95 s** |
| 4 | 逐文件：`test_w11_gate_inventory.py` / `test_w11_rc_manifest.py` / `test_w11_publish_decision.py` / `test_w11_soak_report.py` | **28 / 22 / 43 / 23 passed**，全部 exit 0 |
| 5 | `pytest tests/unit`（第 3 次） | **1731 passed in 15.80 s，exit 0** |
| 6 | `pytest tests/unit`（第 4 次） | **1731 passed in 18.28 s，exit 0** |
| 7 | `timeout 180 … test_w11_backup_restore.py` | **22 passed in 1.99 s** |
| 8 | `timeout 180 … test_w11_e2e_regression.py` | **5 passed in 11.05 s** |
| 9 | `timeout 420 … test_w08_crash_matrix.py` | **7 passed in 198.41 s** |

**逐文件 4 项数字与工程师回执完全一致**（28/22/43/23）；单元总数 **1731 = 1721 + 10** 一致。

**`exit 139` 如实登记（不得写成「一次通过」）**

- **首次与第二次均异常终止**：`Windows fatal exception: access violation`，栈顶 `Lib/ast.py:?? in copy_location`，`EXIT=139`，两次均在 ~2 s 内发生，**同一位置**。
- **归因证据**：① `--collect-only` **exit 0 且 1731 全收集** → 收集期正常；② 4 个受补丁影响的测试文件**单独运行全通过**；③ 同一命令第 3、4 次**完整通过**（1731/15.80 s、1731/18.28 s）。
- **判定**：**间歇性原生崩溃（环境/工具链层）**，非 W11-CORR-02 引入、非用例失败。栈顶在 CPython 3.14 的 `ast.copy_location`（pytest 断言重写路径），与 W10 收口时 `tests/unit` exit 139 的口径同源（`docs/w10-w12-lead-rulings.md` §11.3）。
- **口径**：本报告**不写「回归全绿」**；准确表述为「4 次尝试中 2 次 exit 139、2 次全量 1731 passed；崩溃点已定位并归因环境，非补丁引入」。

### 11.6 最终路由判定

**路由：`NoOne`**

- 第 2 轮 **0 个用例失败**；4 项补丁的**正常构造/校验路径全部按预期拒绝伪造**，反向对照**未见误伤**（无「修得过头」的过严断言）。
- `exit 139` 为环境层间歇崩溃，已定位、已归因，且第 3/4 次全量通过，**不构成 Engineer 路由**（非源码 Bug）、**也不构成 QA 路由**（非测试断言写错）。
- `IN_PROCESS_BYPASS` 与 `CONTENT_DEPTH_LIMIT` 按裁定只登记、不降级、不驱动路由。
- **两轮均已结束**（第 2 轮为 SOP 上限），不再进入第 3 轮。

### 11.7 「无既有断言被放宽」核对方法与结论

> **方法学限制（如实说明）**：4 个测试文件与 4 个源文件**均为未跟踪文件**，仓库无第 1 轮快照，**无法做逐字 git diff**。故改用 4 项替代证据：

| 核对项 | 结果 |
|---|---|
| ① 文件只增不减 | 行数 298→**331**、633→**686**、227→**258**、229→**255**（+33/+53/+31/+26，共 +143），**无一缩小** |
| ② 第 1 轮断言逐字存续 | `test_w11_rc_manifest.py` 中 QA 第 1 轮记录的 **14 个关键断言模式全部命中**（`commit==expected`、`len(commit)==40`、`EXPECTED_MIGRATION_COUNT==11`、`migrations[0]/[-1]`、`db_image_digest==UNKNOWN`、`model_version is None`、`docs_migrations_lag is True`、`"011" not in doc`、`on_disk == model_dump`、`on_disk == generated`、`digest in on_disk`、`len(ids)==len(set(ids))` 等）；**原 `test_frozen_evidence_available_reflects_real_files`（L144-148）一字未改** |
| ③ 第 1 轮测试函数无一消失 | `test_w11_rc_manifest.py` 的 **21 个第 1 轮测试名全部存在且唯一**；当前 22 个 = 21 + 1 新增（`test_frozen_evidence_requires_real_content`），纯**插入式**追加（位于 L156-187，夹在原 148 与 190 之间） |
| ④ 无弱化痕迹 | 4 个文件 `pytest.skip`=**0**、`xfail`=**0**、`放宽/weaken/tolerate` 注释=**0**；新增负测均断言**具体错误码**（`PUBLISH_DECISION_INCONSISTENT` / `GATE_REMOVED` / `SOAK_OVERCLAIMED`），非「只要抛异常即可」的弱断言 |
| ⑤ 公开签名未变 | `inspect.signature` 核对 **8 个入口逐字一致**：`publish_decision`（4 keyword-only 形参、`environment_blocks=()` 默认）、`build_limitations`、`limitations_to_json`、`check_gate_inventory`、`collect_rc_manifest`、`build_rc_freeze`、`build_soak_report`、`_frozen_evidence_available` |

**结论**：**未发现既有断言被放宽或替换**；补丁为「源文件加守卫 + 测试文件插入式加负测」，符合主理人「不改既有断言」的要求。

### 11.8 第 2 轮 AC 变更与遗留

**AC 变更**：AC-14、AC-18 由「部分覆盖」→**「已覆盖」**；AC-20、AC-21 维持「已覆盖」并加固。AC-03、AC-12 **维持「部分覆盖」**（KD-W11-01/02/03 移交 W12，本轮明令不改逻辑）。最终 **19 / 2 / 0**。

**KD 登记核对**：`docs/w11/backup-restore-report.md:124-133` 已登记 `KD-W11-01`（`audit_chain` 桶语义/命名与设计不一致，`task_events` 才是哈希链本体）、`KD-W11-02`（`approval_consumptions`/`step_prepared` 未纳入）、`KD-W11-03`（G1–G3 哨兵归属），与 QA 第 1 轮 L-1/L-2/L-9 **一致**，并已注明「本轮不改逻辑，移交 W12」。*（措辞小瑕：KD-W11-03 的「`delta==3` / `after>before` 缓解断言」实际属 T05 备份用例，非负面集；不影响结论，建议 W12 顺手订正。）*

**已知限制清单（第 2 轮结束后）**

| 编号 | 类型 | 内容 | 处置 |
|---|---|---|---|
| `IN_PROCESS_BYPASS`-1 | 威胁模型外 | `object.__setattr__` 可改写 frozen `PublishDecision.decision` | 仅登记，不判 FAIL、不降级 AC-18 |
| `CONTENT_DEPTH_LIMIT`-1 | 内容深度 | `_frozen_evidence_available` 只校验 `definitions` 长度 14，不校验元素 → 14 个空对象判 `True` | 仅登记，不降级 AC-21 |
| `ENV-PY314-AST-CRASH` | 环境 | `tests/unit` 间歇 `exit 139`（`ast.py copy_location`），4 次中 2 次 | 环境归因，非缺陷；复跑可恢复 |
| L-6 | 低 | `LimitationRemoved` 被 pydantic 包成 `ValidationError`，`code` 不能按类型捕获 | 文档标注 |
| L-1/L-2/L-3 | 中 | 桶命名、漏桶、计数空真 | **KD-W11-01/02/03**，移交 W12 |

**遗留（未解，如实登记）**：`ENV-PG5432` / `ENV-1045` / `ENV-NV1` / `ENV-MODEL` 全未解；**`ENV-RESIDUE` 继续增长**——`task_events` 由第 1 轮 **78319** 增至本轮 **79313**（**+994**，本轮回归与逐文件复跑所致），全程**未 DELETE/UPDATE/TRUNCATE**、**未写「环境已清零」**；**长稳仍未实跑**（`measured_*=null`、`status=PLANNED`）；W09 `docs/w09/06-qa-report.md:624` blank-line 存量**未动**。

---

*Edward（严过关）｜QA Engineer｜software-dbguardian-w11*
