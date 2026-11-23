# W12 定向验收与 QA 收口（Alpha 验收与发布）

> 署名：**QA 工程师 严过关（Yan）**｜团队 `software-dbguardian-kd1201`（第 2 轮）/ `software-dbguardian-w12`（第 1 轮）
> 时点：第 1 轮 `2026-11-16`；**第 2 轮 `2026-11-17`（最终生效）**
> 权威口径：`docs/w12/03-task-cards.md`（AC-01…AC-22）、`docs/w12/02-design.md`、`docs/w12/01-prd.md`、
> `docs/w10-w12-lead-rulings.md`（§4 W12-Q1..Q8、§9 Q-W12-A..E、§13 W11 收口、§14 KD-W12-01/02、§15 W12-CORR-01）
> 本件性质：**QA 定向验收意见**。W12 门禁结论**未评**——本件不替主理人签门禁、不宣布发布。
> 结论口径只有两档：`定向验收通过` / `定向验收不通过`（第 2 轮对应 `第 2 轮复验通过` / `第 2 轮复验不通过`）。
>
> **文档结构说明（务必先读）**：§1–§11 是 **第 1 轮原始记录**（时点 2026-11-16，AC-03 当时为 `FAILED`），
> 逐字保留以构成审计链；**最终生效口径在 §12（第 2 轮，2026-11-17）**。当 §1–§11 与 §12 冲突时，以 §12 为准。

---

## 0. 结论摘要（已按第 2 轮复验刷新）

| 项 | 结论 |
|---|---|
| 定向验收（第 1 轮） | **不通过（有条件）**：AC-01…AC-22 中 **21 条通过 / 1 条不通过**（AC-03 `FAILED`，当时登记 `KD-W12-01`）——见 §4 |
| 定向验收（第 2 轮，最终） | **第 2 轮复验通过**：AC 覆盖 **22 PASSED / 0 FAILED / 0 未覆盖**；AC-03 由 `FAILED` 转 `PASSED`（2026-11-17，修复点 `core/verification/plan.py`）——见 §12 |
| 覆盖 | AC 覆盖 **22/22**（无未覆盖项）；"部分覆盖"标注保留 3 处（AC-02 输入形状 `KD-W12-02`、AC-04 的 PG 段 `NOT_EXECUTED`、AC-12 的演示段 `NOT_EXECUTED`）——见 §12.7 |
| 独立证伪 | 第 1 轮 **8 组 42 项**（§5）；第 2 轮新增 **4 组**：真实链实测 + 退化反证 + 形状边界 5 例 + 限制清单 8 项（§12.2–§12.5） |
| 智能路由 | 第 1 轮 **NoOne** + 1 项残余风险提请裁定（§11）；**第 2 轮最终：`NoOne`**（见 §12.11） |
| 串行复跑 | 第 1 轮 `tests/unit` **1796 passed**；**第 2 轮 `tests/unit` 1801 passed / exit 0**，W12 七个用例文件合计 **120 passed**，W08 崩溃矩阵 **7 passed** |
| 已知问题 | 第 1 轮 5 条制式 + 7 条环境 + 3 条 QA 残余；**第 2 轮降为 4 条制式**（`KD-W11-01/02/03`、`KD-W12-02`）+ 7 条环境 + 3 条 QA 残余 = **14 条**（§12.9） |
| 发布档位 | `release_type` 仍 **`TECH_PREVIEW`**（由 W11 `decision` 派生），**不因 AC-03 转绿而改变** |
| W12 门禁 | **仍未评**。本件不签门禁、不宣布发布；第 2 轮只给"复验通过/不通过" |
| git | **未执行** `git commit` / `add` / `checkout` / `reset`；工作区仍为未提交状态 |

> 一句话：**第 1 轮的结论是"21 通过 / 1 不通过"，红线项 AC-03 被如实记为 `FAILED`；第 2 轮工程师在 `plan.py` 读取端做了形状归一（不改写入端、不改断言常量），我以"退化反证"独立证明 AC-03 转绿是根因修复所致（同一份输入把修复函数退化为恒等函数后立刻回落 `FAILED`），故 AC 覆盖改为 22 PASSED / 0 FAILED / 0 未覆盖。但发布结论仍只能是技术预览档——W12 门禁未评，环境阻塞未清。**

---

## 1. 现场基线（实测，非转述）

| 项 | 实测值 | 与上游交接的差异 |
|---|---|---|
| `git rev-parse HEAD` | `2850eca326850eec491e9159ec7033a85807d080` | 一致 |
| `git diff --name-only`（tracked 修改） | **20** | 一致（19 存量 + W08 授权改写 1 处） |
| `git status --short` | **113**（20 `M` + 93 `??`） | 一致 |
| W12 新增文件 | **27**（`core/release/` 7 + `docs/w12/` 13 + `tests/**test_w12*` 7） | 交接记 25，实测 **27**（差 2，见 §9-`D-01`） |
| `tests/unit` 全量 | **1796 passed / 15.35s / exit 0** | 交接记 1793，实测 **1796**（+3，见 §9-`D-02`） |
| `test_w12_claims.py` | **25 passed** | 交接记 22，实测 **25**（`§15.5` 补的 3 条 kernel 负测已落盘） |
| 解释器 | `.venv/Scripts/python.exe` = Python **3.14.7** | 一致 |
| 冻结资产 | `docs/w07/**`、`docs/w08/**`、`docs/migrations.md` **无 `M`**；`benchmarks/w10/**`、`docs/w10/**`、`docs/w11/**` 为 `??` 新增（从未提交） | 一致 |
| `docs/w09/06-qa-report.md` | `M`，`git diff --numstat` = **128 / 0**，626 行，末行为多余空行（L624 为空行） | 与 `§14` / `§15` 记载一致，**本轮未动** |
| AC-22 既存件 | `tests/unit/test_write_tool_gate.py`、`tests/unit/test_tools_contracts.py` **无 `M`** | 一致 |

**变更分类**：`20 M` = W09/W10/W11 存量 tracked 修改；`93 ??` = W09/W10/W11/W12 全部新增（W12 占 27）。
**未提交事实**：W09–W12 全部改动仍在工作区，本轮**未做任何 git 写操作**。

---

## 2. 串行命令与真实数字（逐条可复核）

纪律：解释器 `.venv/Scripts/python.exe`；每条 pytest 带 `-o addopts="" -q -p no:cacheprovider`；**全程串行，两个 pytest 未并发**；未使用 `--timeout=`；集成件外层 `timeout 180`。

| # | 命令（依赖路径，非无路径全量） | 结果 | exit |
|---|---|---|---|
| 1 | `pytest tests/unit/test_w12_claims.py` | **25 passed / 0.52s** | 0 |
| 2 | `pytest tests/integration/test_w12_functional_acceptance.py`（`timeout 180`） | **14 passed / 33.32s** | 0 |
| 3 | `pytest tests/integration/test_w12_security_acceptance.py`（`timeout 180`） | **18 passed / 9.16s** | 0 |
| 4 | `pytest tests/integration/test_w12_deploy_package.py`（`timeout 180`） | **12 passed / 0.08s** | 0 |
| 5 | `pytest tests/integration/test_w12_demo_script.py`（`timeout 180`） | **11 passed / 0.05s** | 0 |
| 6 | `pytest tests/unit/test_w12_doc_inventory.py` | **17 passed / 0.86s** | 0 |
| 7 | `pytest tests/unit/test_w12_publish_gate.py` | **23 passed / 0.12s** | 0 |
| 8 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py`（AC-22 既存） | **11 passed / 0.27s** | 0 |
| 9 | `pytest tests/integration/test_w09_redaction_pipeline.py`（AC-09 既存，`timeout 180`） | **4 passed / 3.15s** | 0 |
| 10 | `pytest tests/unit`（全量） | **1796 passed / 15.35s** | 0 |

- W12 七个文件合计 **120 passed**（25+14+18+12+11+17+23）。
- **数字口径变更（重要，避免后续读者以为对不上）**：上游交接曾给出 `test_w12_claims.py = 22`、`tests/unit = 1793`。**以实测 25 / 1796 为准**。差额 **+3** 的来源是 `§15.5` 要求补的 **3 条内核级证伪/对照用例**（对 `_scan_unit` 直接断言）：
  - `test_corr01_kernel_affirmative_rewrite_violates`（`本版本已达成完整 Alpha` → 违规）
  - `test_corr01_kernel_negation_removed_violates`（`完整 Alpha` → 违规）
  - `test_corr01_kernel_original_sentence_is_exempted`（`不得表述为完整 Alpha` → 豁免，**反向对照**）
  更早一轮 CORR-01 落地时已新增 7 条（值级粒度、否定词开头、外溢禁止、缺件不静默、集成级证伪/对照）。两者叠加使该文件从 15 → 22 → **25**，`tests/unit` 相应 1793 → **1796**。**未发现任何回归**，差额全部为新增用例。
- **`ALLOWED_PHRASES` 由 7 条收回 4 条**（按 `§15.5` 勘误一：不追加白名单，避免多一条宽松路径）。收回后实测 `scan_claims()` 仍 `ok=True`、`violations=()`、`exempted_lines=24`，且 24 条豁免的依据分布**不含 `ALLOWED_PHRASE`**（详见 §5.3 第 16 项）。`test_ac19_allowed_phrases_are_frozen` 为「与常量相等」断言（非硬编码 7 条），收回后**仍通过**。
- **异常终止登记**：本轮 **0 次**异常终止——无 `Windows fatal exception: access violation`、无 exit 139、**无隔离重跑**。因此不存在"首次失败—重跑通过"的记录可登记。
- `ENV-PY314-AST-CRASH`：**本轮 3 次以上 pytest 全程未复现**（与工程师本轮口径一致）。

---

## 3. 独立证伪的总原则（为什么不是"重跑签字"）

我没有以"工程师命令重跑通过"作为验收依据。逐条 AC 的判定由三源交叉得出：

1. **重跑**：串行重跑工程师的用例（§2）——只证明"用例当前是绿的"，不足以证明"断言是对的"。
2. **复算**：在**不连库、不改生产代码**的前提下，用真实模型/真实契约/真实落盘件重算结论（§4 大部分 AC）。
3. **证伪**：主动构造破坏输入（删除、改写、注入、绕过、注入环境变量、`model_construct`、`object.__setattr__`），验证"该拒绝的确实拒绝"（§5）。

凡三源无法覆盖的（需真实 DB 的写链、控制库事件行），我在表格备注里**明确写"采信工程师实跑产物"**，不冒充独立实跑。

---

## 4. AC-01…AC-22 覆盖矩阵（逐条给结论）

图例：`PASSED` = 本轮实测通过；`FAILED` = 本轮实测不通过；`NOT_EXECUTED` = 环境阻塞未执行（**不判 FAIL**）。
"判定依据"列标注该结论的主要来源：`重跑` / `复算` / `证伪` / `采信`（工程师实跑产物）。

| AC | 判定 | 判定依据 | 实测要点（真实数字） | 备注 |
|---|---|---|---|---|
| **AC-01** | `PASSED` | 重跑 + 读断言 | 五态 create/query/pause/resume/cancel 各 1 例；`task_events` 可查询且 `seq` 连续；`run_id` 单值 | — |
| **AC-02** | `PASSED` | 重跑 + 读断言 | `slow_query`/`connection_pressure`/`lock_wait` 各 1 例，五字段非空率 3/3；`ReportFacts` 按 `core/evidence/models.py:121-130` 实测口径 | **部分覆盖**：「全表扫描 + 无 `table_stats`」输入形状未覆盖（已登记 `KD-W12-02`） |
| **AC-03** | **`FAILED`** | **复算（独立重现归因）** + 重跑 | `verdict=FAIL`、`status=FAILED`、`reason_code=OBSERVED_SHAPE_MISMATCH`；`tool_calls=1`、`row_changes=1`；逐条：`affected_rows=UNKNOWN`、`snapshot_comparable=FAIL`、`target_note_final=PASS` | 主理人 `§14.1` 裁定「不修、如实记 FAILED」；我独立复算确认归因指向 `executor.py:486` ↔ `verification/plan.py:296-309` 的**形状不一致**，非 W12 `functional.py` 读错（§5.1） |
| **AC-04** | `PASSED` | 复算 + 重跑 | `[d for d in registry.definitions() if d.side_effect=="write"] == []`（PG 7 条全 `read`）；PG 段 `NOT_EXECUTED`（`ENV-PG5432`）→ 不判 FAIL | PG 段**未执行**（环境阻塞），非"通过" |
| **AC-05** | `PASSED` | 重跑 + 静态核对 | 冻结集 **29 条 / 去重 29**；`review_negative_cases(ports=...)` 29/29 `denied=True`；`w11_source_status=PASSED`；无端口时返回 `()` + `NOT_AVAILABLE`（**如实拒答，不编造**） | — |
| **AC-06** | `PASSED` | 复算 + 重跑 | 执行侧注册表（observer 10 + PG 7）**无 `R4` 定义**；`r3_denied=True`、`r4_denied=True`（真实 `PolicyEngine`） | — |
| **AC-07** | `PASSED` | 重跑（需真实 LAB） | 四类绕过 `model`/`sql_comment`/`mcp_param`/`ui` 全 `True`，目标行不变 | 采信本轮 18 passed 用例实跑 |
| **AC-08** | `PASSED` | 重跑（需真实 LAB/控制库） | `UNKNOWN` → 不重试（无 `TOOL_CALL_RETRIED`，`tool_calls_delta=0`）；审计不可写 → `emit_required` 抛 `AuditUnavailableError`，R2 链不接 `try_emit` | 采信本轮用例实跑；复算仅覆盖 `emit_required`/`try_emit` 静态区分 |
| **AC-09** | `PASSED` | 重跑（既存件） | `tests/integration/test_w09_redaction_pipeline.py` **4 passed**；模型上下文/日志/训练集三边界；未审核知识 `is_indexable is False` | 未新建替代用例（合规） |
| **AC-10** | `PASSED` | **复算** | `REQUIRED_DEPLOY_FILES` **11 项，缺失 0**；`.env.example` 口令全 `CHANGE_ME_*`（14 处）→ `env_example_has_real_secret=False` | 按 `§15.3` 收口 **11 项**（非 9 项） |
| **AC-11** | `PASSED`（含 5 项 `NOT_EXECUTED`） | **复算** | 五项检查各恰 1 条，全 `NOT_EXECUTED` 且**逐条带原因**（`ENV-NV1` ×3、`DG_ADMIN_DSN 不可用（ENV-1045）` ×1、`本轮不执行 rollback` ×1）；`commit` = `git rev-parse HEAD`（实测相等）；`artifact_hash` 64 位 SHA-256；`image_digest=UNKNOWN` + 登记 | 五项未执行**不判 FAIL**（W12-Q3） |
| **AC-12** | `PASSED` | **复算** | S1–S4 四段各 1 例且全 `NOT_EXECUTED`+原因；`lab_only=True`、`production_dsn_referenced=False`、`web_console_used=False`；措辞红线 `completion_wording_for_advice` 对「建议…已修复」判违规、对否定式不判违规；`demo-script.md` 自检 `report_has_completion_wording_for_advice=False` | 演示**未实跑**，结论为"未执行"而非"通过" |
| **AC-13** | `PASSED` | **复算** | OpenAPI **3.1.0 / 31 paths / 63 561 B**，落盘 `openapi.json` 版本与 path 数一致；文档清单 **23 条，缺失 0**，`PARTIAL` **2**（`docs/migrations.md`、`docs/w07/07-gate-review.md`） | 迁移说明止于 007、W07 四件套缺两件 → 如实 `PARTIAL` |
| **AC-14** | `PASSED` | **复算 + 证伪** | `baseline_score is None`；注入 `0` / `0.0` / `0.42` / `1` **四种全部 `ValidationError`**；`baseline_model_status="未微调"`、`baseline_metrics_status="指标未测得"`、`BLOCKED_MODEL_ENV_MISSING`；`benchmark-baseline.md` 三要素齐全 | 不填任何估计值 |
| **AC-15** | `PASSED` | **复算** | `NON_PROMISES` 六项逐条在清单内且 marker 与常量一致（`NOT_SUPPORTED` ×5 / `PARTIAL` ×1）；全部 marker 落在 `{NOT_SUPPORTED, PARTIAL, UNKNOWN}` 闭集 | — |
| **AC-16** | `PASSED` | **复算 + 证伪** | `None` → `TECH_PREVIEW`+`DBGuardian 技术预览（受限发布）`+`DEFAULT_TECH_PREVIEW_ONLY`+`PUBLISH_DECISION_MISSING`；`TECH_PREVIEW_ONLY` → `TECH_PREVIEW`；`ALPHA_ALLOWED` → `ALPHA`+`DBGuardian Alpha`；6 个异常值（`alpha_allowed`/空串/`FULL_ALPHA`/`PASSED`/`AlphaAllowed`）→ 全 `TECH_PREVIEW`+`UNKNOWN_DECISION_VALUE` | 名称大小写不敏感检查生效 |
| **AC-17** | `PASSED` | **复算 + 证伪** | `inspect.signature == ['decision']`；`ReleaseGate` 6 字段、**无时间字段**；源码无 `utc_now()`/`os.environ`/`getenv`/文件 I/O；同输入 `to_public_dict()` 逐位相等；报告 **8 章 + 延期项 + 证据引用 + 口径边界**（11 个 `##`） | 覆盖形参负测见 §5.2 |
| **AC-18** | `PASSED` | **复算** | `model="baseline"`、`sft_reproducible_gain=False`、`baseline_score=None`；`dpo_deferred`/`full_14b_deferred`/`rl_deferred` 为 `ClassVar`（**不在 `model_fields`**），恒 `True` | 不可被数据覆盖 |
| **AC-19** | `PASSED` | **复算 + 证伪（15 项）** | `scan_claims().ok=True`；`violations=()`；三范围 **11 严格 / 2 spec_mode / 1 排除**；`exempted_lines=24`（`NEGATION_WINDOW:不得` 2 + `SPEC_MODE_MARKER` 22）**逐条可核**；`allowed_phrases` 4 条 | 见 §5.3；残余风险 `R-W12-QA-01` |
| **AC-20** | `PASSED` | **复算 + 证伪** | 三模型（`FunctionalAcceptanceResult`/`SecurityAcceptanceResult`/`AcceptanceEvidence`）字段集与 `{demo_passed,demo_status,demo_ok}` **交集为空**；注入 → `ValidationError`；`assert_no_demo_input` → `DEMO_AS_ACCEPTANCE`；`evidence_refs=19` 条，W09/W10/W11 **各 2 条**，路径全部真实存在 | — |
| **AC-21** | `PASSED` | **复算 + 证伪（7 项）** | 18 项 = 13 `AUTO` + 5 `MANUAL`；删任一 `AUTO` → **构造期 `LIMITATION_REMOVED`** 且 **`model_construct` 绕过层 `LIMITATION_REMOVED`**；改 marker / 改 statement / 改 origin → `LIMITATION_MODIFIED`；`MANUAL` 缺 `added_by` → 拒绝；复用自动 id → 拒绝；正常追加 18→19 成功 | 复用自动 id 的错误码依赖顺序，见 §9-`O-02` |
| **AC-22** | `PASSED` | **复算 + 重跑** | 全仓 **AST 枚举**：非测试代码中 `side_effect="write"` 的调用点**唯一**为 `adapters/mysql/lab_repair.py:77`；observer 10 条全 `read`、PG 7 条全 `read`、全局无 `R4`；既存两文件 **11 passed**、**无 `M`** | 未新建替代用例（合规） |

**统计**：`PASSED` **21** ｜ `FAILED` **1**（AC-03）｜ `NOT_EXECUTED` 作为**子项**存在于 AC-04/AC-11/AC-12（不单独计 FAIL）｜ 未覆盖 **0** ｜ 部分覆盖标注 **2**（AC-02 输入形状、AC-04/AC-12 的 PG/演示段未执行）。

---

## 5. 独立证伪结果（8 组 **42 项**）

QA 临时脚本全部放在**仓库外** `D:/tmp/w12qa/`（`verify_a.py` / `verify_b.py` / `verify_c.py`），未向仓库写入任何脚本或产物（唯一新增文件是本件）。

### 5.1 G1｜AC-03 归因独立复算（不连库、不写库）

**做法**：用**真实** `adapters/mysql/lab_repair.py` 的验证计划（3 条断言），构造**真实** `ToolResult`（`data={"before","after","affected_rows","idempotent_noop"}`），按 `executor.py:486` 的落盘方式 `model_dump(mode="json")` 作为 `step_observed["result"]`，调用真实 `evaluate_plan`。

| 场景 | `affected_rows` | `snapshot_comparable` | `target_note_final` |
|---|---|---|---|
| **执行器真实形状**（指标在 `result["data"]` 下） | `UNKNOWN` / `OBSERVED_MISSING` | `FAIL`（取到 `None`） | `PASS`（走独立回读，不受影响） |
| **反事实：把 `data` 摊平到顶层**（W08 五个测试的注入形状） | `PASS` | `PASS` | `PASS` |

**归因结论（独立成立）**：
- `executor.py:486` 写 `StepObserved.result = redact_payload(ToolResult.model_dump(mode="json"))` → 指标位于 `result["data"][*]`；
- `verification/plan.py:296-309` 的 `_collect_observed` 对 `step_observed` 来源执行 `result.get(assertion.metric)` → **顶层**取 `affected_rows` / `before` / `after`，取不到；
- 同一份数据**摊平后两条断言立刻可判定**——证明是**形状契约不一致**，不是"证据缺失"，也不是 W12 `functional.py` 读错字段。
- 逐条结论与工程师落盘产物**完全一致**（`affected_rows=UNKNOWN`、`snapshot_comparable=FAIL`、`target_note_final=PASS`、`tool_calls=1`、`row_changes=1`），且工程师用例 `test_ac03_repair_closure_is_single_write` 断言的是 **`FAILED` + 归因码**，**不是**期望 `PASSED`（未改绿、未加 skip/xfail）。

**补充证伪**（`_classify_verification_gap` 归因器本身是否正确）：

| 输入 | 输出 | 判定 |
|---|---|---|
| `StepObserved` 行存在 + 断言报 `OBSERVED_MISSING` | `OBSERVED_SHAPE_MISMATCH` | 符合 `KD-W12-01` 定义 |
| `StepObserved` 行不存在 + 断言报 `OBSERVED_MISSING` | `STEP_OBSERVED_MISSING` | 正确区分"形状问题"与"真缺失" |

→ **路由判定：不指向 Engineer**（W12 侧无错）；`KD-W12-01` 归因**成立**。

### 5.2 G2｜`release_type` 不可覆盖（AC-16/17）

| 攻击面 | 尝试 | 结果 |
|---|---|---|
| 签名 | `inspect.signature` | `['decision']`；无 `force_alpha`/`override`/`manual_override`/任何 `force_*` |
| 环境变量 | `DG_FORCE_ALPHA=1`、`RELEASE_TYPE=ALPHA`、`DG_RELEASE_TYPE=ALPHA` 后 `derive_release_type(decision=None)` | 仍 `TECH_PREVIEW`（派生函数不读环境） |
| 构造期 | `TECH_PREVIEW` + 名称含 `Alpha` | `ReleaseTypeDerivationError`（大小写不敏感） |
| 构造期 | `release_type=ALPHA` + `effective_decision=TECH_PREVIEW_ONLY` | `ReleaseTypeDerivationError` |
| 构造期 | 名称含「完整 Alpha」 | `ReleaseTypeDerivationError`（表述禁令） |
| 构造期 | `effective_decision="FORCED"` | `ReleaseTypeDerivationError` |
| `dataclasses.replace` | `replace(gate, release_type=ALPHA)` | `ReleaseTypeDerivationError`（`__post_init__` 重跑） |
| `object.__setattr__` | 对 frozen dataclass 强改 `release_type=ALPHA` | **可写入**，但产物自相矛盾：`release_type=ALPHA` + `effective_decision=TECH_PREVIEW_ONLY` + 名称「技术预览」→ 记为残余风险 `R-W12-QA-02`（Python 语言级逃逸，非"正常路径"） |
| 正常路径 | 全部攻击尝试之后重新派生 | `TECH_PREVIEW`（未被污染） |
| 确定性 | 同输入两次 `to_public_dict()` | 逐位相等（`decision_digest=6431042e…`） |

**结论**：**正常路径恒由 `decision` 派生**；`None` 不变 `ALPHA`；只有 `ALPHA_ALLOWED` 出 `ALPHA`。AC-17 的"纯函数、无 I/O、无全局状态、无时间"经源码级核对成立。

### 5.3 G3｜扫描器不可被骗（AC-19 + W12-CORR-01，**16 项**）

负测全部在 `docs/w12/` 的 **tmp 副本**上改写后调用真实 `scan_claims(root)`，不触碰仓库。

| # | 构造 | 期望 | 实测 |
|---|---|---|---|
| 1 | 真实仓扫描 | `violations=()` | `ok=True`、`violations=0`、`demo=0`、`exempted=24`、`limitation_ok=True`、`evidence_ok=True` |
| 2 | 三范围 | 11/2/1 | 严格 **11**、`spec_mode` **2**、排除 **1**（`01-prd.md` 未混入严格集）；`allowed_phrases=4` |
| 3 | 豁免逐条可核 | 24 条且文件合法 | 24 条；依据分布 `NEGATION_WINDOW:不得` **2** + `SPEC_MODE_MARKER` **22**；文件分布 `known-limitations.json` 1 / `.md` 1 / `02-design.md` 7 / `03-task-cards.md` **15**；越界 0、外溢 0 |
| 4 | 豁免算式复核（独立重算 12 字符窗口） | 1 条 JSON 值级豁免可复现 | 独立重算 **1 条**（`$.items[5].statement`，窗口 `…塞或红线失败，不得表述为…`，否定词 `不得`）与扫描器一致 |
| 5 | **外溢①**：`release-notes.md`（非豁免件）写「已达成完整 Alpha」 | 必须违规 | **违规**（`W12_EXTRA:完整 Alpha`，L32） |
| 6 | **外溢②**：`release-notes.md` 写**否定式**「不得表述为完整 Alpha」 | **仍必须违规** | **违规**（L32）——豁免**不外溢** |
| 7 | `门禁通过：否` 精确放行 vs `门禁通过。` | 前者放行、后者违规 | 前者放行、后者 `GATE_PASSED` 违规（负向先行生效） |
| 8 | `known-limitations.md` 删否定词只留「完整 Alpha」 | 必须违规 | **违规**（L25） |
| 9 | `known-limitations.md` 改「已达成完整 Alpha」 | 必须违规 | **违规**（L25） |
| 10 | `known-limitations.json` 改「已达成完整 Alpha」 | 必须违规 | **违规**（`$.items[5].statement`） |
| 11 | 否定词距短语 **>12 字符**（且行首非否定词） | 必须违规 | **违规**（窗口规则确有边界） |
| 12 | 行首否定词（`不得以任何形式…完整 Alpha`） | 按裁定豁免 | 不违规（`§15.2` 条件②） |
| 13 | 其余 9 件零豁免 | 0 外溢 | 严格零容忍件 **9 件**，豁免外溢 **0** |
| 14 | `NEGATION_TOLERANT_FILES` 范围 | 仅两个限制清单 | `('docs/w12/known-limitations.json', 'docs/w12/known-limitations.md')` |
| 15 | **代码围栏探针**：`release-notes.md` 的 ``` 块内写「已达成完整 Alpha」 | 设计 §8.2 第 2 步允许剥离 | **未命中** → 记残余风险 `R-W12-QA-01`（详见 §6） |
| 16 | **白名单兜底证伪**：① 统计 24 条豁免的依据分布；② **在内存中把 `ALLOWED_PHRASES` 清空为 `()`**（不落盘、不改文件）后重扫 | 依据分布**不含** `ALLOWED_PHRASE`；清空白名单后结果**逐条不变** | 依据分布 `NEGATION_WINDOW:不得` **2** + `SPEC_MODE_MARKER` **22**，**含 `ALLOWED_PHRASE` = False**；清空后 `ok=True`、`violations=0`、`exempted_lines=24`、**`exempted_items` 与清空前逐条相等（True）** |

**第 16 项的意义（本轮新增的关键证伪）**：这证明 24 条豁免**完全靠否定语境与 `spec_mode` 标记真实生效**，**不是**靠 `allowed_phrases` 白名单兜底。清空白名单后扫描结果**一位不变**——若某条豁免实际只能靠白名单成立，此处必然翻红。**该项未推翻，故不路由 Engineer。**

**`§15.2` 第 5 条"证伪负测"落实核对**：两条 mandatory 负测（① 改肯定式 → 违规；② 删否定词 → 违规）在实现层（#8/#9/#10）与用例层（`test_corr01_affirmative_rewrite_is_a_violation`、`test_corr01_phrase_without_negation_is_a_violation`、`test_corr01_kernel_affirmative_rewrite_violates`、`test_corr01_kernel_negation_removed_violates`）**双向存在**，`test_w12_claims.py` 实测 **25 passed**。

### 5.4 G4｜限制清单不可删（AC-21，7 项）

| 攻击 | 结果 |
|---|---|
| 基线 | 18 项 = 13 `AUTO` + 5 `MANUAL`；`w11_source_status=PASSED`、`w11_source_digest=fbc39d0b…` |
| 删 `ENV-PG5432`（**同步重算 `items_digest`**，排除"因摘要不符而拒"的假象） | 构造期 `LIMITATION_REMOVED` |
| 同上 + `model_construct` 绕过校验器 | 第二层 `assert_limitation_integrity` → `LIMITATION_REMOVED` |
| 改 `marker` | `LIMITATION_MODIFIED` |
| 改 `statement`（追加 2 字） | `LIMITATION_MODIFIED` |
| `origin` 改为 `MANUAL` | `LIMITATION_MODIFIED` |
| `MANUAL` 缺 `added_by`/`reason` | 拒绝（`MANUAL 项必须提供 added_by`） |
| `MANUAL` 复用自动 `item_id` | 拒绝（错误码见 §9-`O-02`） |
| `statement` 空白 | 拒绝 |
| 正常追加人工项 | 18 → 19 成功 |

**两层防护确认**：模型校验器**重算** 13 项并逐字比对 marker/statement；`model_construct` 绕过第一层时第二层仍拦截。AC-21 成立。

### 5.5 G5｜`demo` 结构性排除（AC-20）

| 检查 | 结果 |
|---|---|
| 三模型字段集 ∩ `{demo_passed,demo_status,demo_ok}` | 三者**全为空集**（**结构上没有**，不靠校验器拦截） |
| `FunctionalAcceptanceResult.model_validate({"demo_passed":True})` | `ValidationError`（`extra="forbid"`） |
| `model_construct(demo_passed=True)` | 字段**未被存储**（`__dict__` 与 `__pydantic_extra__` 均无）、无该属性 |
| `assert_no_demo_input({"ac_id":"AC-03","demo_passed":True})` | `DEMO_AS_ACCEPTANCE` |
| `AcceptanceEvidence(ac_id=..., demo_ok=True)` | `TypeError`（dataclass 不接受该形参） |
| `AlphaAcceptanceReport` 字段集 | 6 字段，`deferred_*` 3 项**不在** `model_fields`（`ClassVar`） |

### 5.6 G6｜诚实性（6 项）

| 项 | 实测 |
|---|---|
| `baseline_score` | 恒 `None`；注入 `0`/`0.0`/`0.42`/`1` 全部 `ValidationError` |
| `image_digest` | `UNKNOWN`，且 `registered_issues` 含 digest 登记（未编造值） |
| `artifact_hash` | 64 位 SHA-256（真实计算） |
| `container_runtime_status` / `nv1_is_hard_gate` | `NOT_VERIFIED` / `False`；`nv1_is_hard_gate` **不在** `model_fields`（`ClassVar`），用数据注入 `True` → `ValidationError` |
| `commit` | 与 `git rev-parse HEAD` **逐字相等**（未写死） |
| `deployment-guide.md` 的 KD-2 命名 | 提「同进程装配」✔、提「遗留占位」✔；**未**出现「api → gateway 容器」类误写（4 种模式全部 0 命中） |

### 5.7 G7｜AC-05/AC-10…AC-18 只读复算（10 项，见 §4 矩阵）

补充两项**独立于 `claims.py` 豁免逻辑**的自扫：

- **宽口径自扫（不剥离行内 code span）**：在 9 个严格件上命中 **3 处**——全部为 `claim-scan-report.md` 的**规则自述行内代码引用**（L49 `` `门禁通过` ``、L167 `` `门禁通过(?!：否|:否)` ``、L170 `` `已达成完整 Alpha` ``）。
- **剥离行内 code span 后**：命中 **0 处**，与扫描器一致。
→ 说明：扫描器的"代码语境剥离"是**设计明确机制**（`docs/w12/02-design.md:455`、`claim-scan-report.md` §4 标题即写明"规则表放在代码围栏内"），不是隐藏豁免；但同时构成 `R-W12-QA-01` 的**实际可达路径**（见 §6）。

### 5.8 QA 自身脚本缺陷登记（如实披露）

独立证伪过程中有 **3 项**先报红，经诊断为**我自己的脚本写错**（不是被测代码问题），已自修并重跑：

| 项 | 我的错误 | 真实行为 |
|---|---|---|
| `snapshot_comparable` 期望值 | 我断言应为 `UNKNOWN` | 实际 `FAIL`（快照键取到 `None` → 与期望不等即 `FAIL`），与工程师产物一致；已改断言 |
| 否定词窗口边界构造 | 填隙文本里误用了「避免」（本身是否定词） | 被"行首/窗口否定词"正确豁免；改用中性填隙后如预期判违规 |
| 豁免条数统计 | 我按 2 条 JSON 豁免断言 | 实际 JSON 1 条 + MD 1 条 = 2 条 `NEGATION_WINDOW`；已改 |

→ 这三项属"**QA（self）**"路由（测试代码自身缺陷），**已自修重跑**，不是 Engineer 侧 Bug。全程未修改任何 W12 生产代码与既有断言。

---

## 6. 残余风险与待裁定项

### `R-W12-QA-01`｜严格件内的代码语境被剥离（**提请主理人裁定，非本轮判 FAIL**）

- **现象**：在 `release-notes.md`（非豁免件）的 ``` 围栏内写入「已达成完整 Alpha」，`scan_claims` **不报违规**；行内 code span 同理（见 §5.7 的 3 处命中）。
- **依据**：这是**设计明文机制**——`docs/w12/02-design.md:455`「剥离：去掉 fenced code blocks 与行内 code spans —— 禁令表、正则、模型定义都在代码语境」；`claim-scan-report.md` §4 的规则表本身就在围栏内（否则规则自述必被判违规）。因此**不构成"W12-CORR-01 豁免外溢"**，也**不是**实现偏离设计。
- **但存在口径张力**：`§9 Q-W12-A` 第①条要求「对外材料严格模式零容忍，**扫描强度不得降低**」；`03-task-cards.md:427` 写「唯一例外 `门禁通过：否`」。围栏剥离事实上提供了第三条绕过路径，且**不留痕**（不进 `exempted_items`）。
- **影响评估（当前现场）**：9 个严格件内**没有**任何围栏/行内代码藏有正向宣称；`claim-scan-report.md` 的 3 处命中全部是规则自述。**当前风险为"未来新增内容"的可用性风险，不是现存缺陷**。
- **建议（不自行实施）**：① 由主理人裁定是否在 T06 增补一条"严格件围栏内命中同样计违规（规则自述改为显式豁免清单）"的负测；② 或在 `claim-scan-report.md` 的规则说明里把"围栏剥离"从隐含机制升格为**逐条留痕的显式豁免**（与 `exempted_items` 同口径）。
- **我的路由态度**：**不路由 Engineer**（无实现偏离可证、且禁止我改 W12 生产代码）；**交 team-lead 裁定**。

### `R-W12-QA-02`｜frozen dataclass 的语言级写穿

- `ReleaseGate` 为 `@dataclass(frozen=True)`，`object.__setattr__` 可强改字段，产生自相矛盾的公开视图（`ALPHA` + `TECH_PREVIEW_ONLY`）。`dataclasses.replace` 会被 `__post_init__` 拦住，`object.__setattr__` 属 Python 通用逃逸，**正常路径不可达**。AC-17 的判定口径是"正常路径恒由 `decision` 派生"，故**不判 FAIL**；此处仅登记，供后续版本决定是否加 `__slots__`/只读视图。

### `R-W12-QA-03`｜本收口件不在 T06 严格扫描白名单内

- `STRICT_SCAN_FILES` 共 11 件，**不含** `docs/w12/06-w12-qa-closeout.md`。本件为说明禁令必然**引用**禁用短语原文（如「完整 Alpha」「已收口」），若将来把收口件纳入严格集，会**直接变红**。
- **实测命中数**（用 `FORBIDDEN_CLAIMS` + `W12_EXTRA_FORBIDDEN` 对本件做围栏外自扫）：**20 处**。逐处复核后**全部**属于三类**非宣称**语境——① 禁令原文引述（如「不得写『完整 Alpha』」行）；② 证伪负测的**构造输入说明**（如"把 X 改成『已达成完整 Alpha』→ 必须违规"）；③ 扫描器/豁免机制的**规则名**（`GATE_PASSED`、`GATE_CLOSED` 等码名）。**零处**为正向宣称。
- 处理建议：纳入时按 `spec_mode` 或 `NEGATION_TOLERANT` 同级口径处理，并在 `claim-scan-report.md` 留痕。**本轮不改 `claims.py`**（禁止改 W12 生产代码）。

---

## 7. `KD-*` 登记核对

| 编号 | 内容摘要 | 是否在 `known-limitations.json` 中 | 是否在 `alpha-acceptance-report.md`「已知问题」中 | QA 复核 |
|---|---|---|---|---|
| `KD-W11-01` | W11 备份恢复口径偏离（桶命名） | 是（`MANUAL`） | 是 | 一致 |
| `KD-W11-02` | W11 覆盖漏项 | 是（`MANUAL`） | 是 | 一致 |
| `KD-W11-03` | W11 哨兵归属 | 是（`MANUAL`） | 是 | 一致 |
| `KD-W12-01` | AC-03 `OBSERVED_SHAPE_MISMATCH`（`executor.py:486` ↔ `plan.py:296-309`） | 是（`MANUAL`） | 是（含「不得表述为 verification PASS」） | **独立复算成立**（§5.1） |
| `KD-W12-02` | 慢查询分析器「全表扫描 + 无 `table_stats`」潜在崩溃，AC-02 未覆盖该形状 | 是（`MANUAL`） | 是 | 一致（部分覆盖已标注） |

- 自动项 **13**（W11 六项 + W12 六类不承诺 + `DOC-PARTIAL`）与人工项 **5** 共同构成 18 项；`items_digest` / `w11_source_digest` 由模型校验器在加载时强制重算（加载即通过）。
- `docs/w11/limitations.json` **只读引用**、未改写（`w11_source_status=PASSED`、`w11_source_digest=fbc39d0b…`）。

---

## 8. 环境与未执行项（`ENV-*`，如实登记，不判 FAIL）

| 编号 | 现象 | 影响面 | W12 处置 | QA 复核 |
|---|---|---|---|---|
| `ENV-PG5432` | PG LAB（5432）拒绝连接 | AC-04 的 PG 段、演示 S1 的 PG 分支 | `NOT_EXECUTED` + 原因，**不判 FAIL** | 一致，未修环境 |
| `ENV-1045` | `DG_ADMIN_DSN`（root@localhost）认证失败 1045 | 库级物理恢复、`upgrade` 演练 | `NOT_EXECUTED` + 原因 | 一致 |
| `ENV-NV1` | 容器运行态未验证 | `image_digest`、`container_runtime_status` | `UNKNOWN` / `NOT_VERIFIED` + 登记，`nv1_is_hard_gate=False` | 一致（§5.6 已证不可被数据覆盖） |
| `ENV-MODEL` | 模型环境缺失（`BLOCKED_MODEL_ENV_MISSING`） | 模型指标、`baseline_score` | `None` / `指标未测得`，不填估计值 | 一致 |
| `ENV-RESIDUE` | 控制库只追加表存在未观测历史残留且随时间增长 | 所有计数须按 `task_id`/`run_id` 过滤 | 只追加、不清理 | **本轮串行复跑继续累加**（`task_events` 起始 79313+，未精确复核条数——见 §9-`O-01`） |
| `DECISION-TECH-PREVIEW` | 发布结论 `TECH_PREVIEW_ONLY` | 发布档位 | 只登记，不升级 | 一致 |
| `DOC-PARTIAL` | 迁移说明止于 007、W07 冻结件缺 2 件 | 文档完整性 | `PARTIAL` + `note` | 一致（§4 AC-13） |
| `ENV-PY314-AST-CRASH` | 历史 Python 3.14 AST 崩溃 | 曾导致 pytest 异常终止 | 工程师本轮 3 次未复现 | **本轮 0 次复现**（§2） |

**未执行项口径**：AC-11 五项部署检查、AC-12 四段演示、AC-04/AC-12 的 PG 段均**未执行**，一律记 `NOT_EXECUTED` + 原因。它们**不是"通过"**，也**未被判 FAIL**。

---

## 9. 与上游交接的差异登记（`D-*`）与观察项（`O-*`）

| 编号 | 类型 | 内容 |
|---|---|---|
| `D-01` | 数字差异 | 交接记「W12 共 25 个新文件」，`git status --short -uall` 实测 **27**（`core/release/` 7 + `docs/w12/` 13 + `tests/**/test_w12*` 7）。本件落盘后为 **28**（+本件）。 |
| `D-02` | 数字差异（**以实测为准**） | 交接记 `tests/unit` = 1793、`test_w12_claims.py` = 22；**实测 1796 / 25**。差额 **+3** = `§15.5` 补的 3 条 kernel 级证伪/对照（`test_corr01_kernel_affirmative_rewrite_violates` / `_negation_removed_violates` / `_original_sentence_is_exempted`）；更早一轮 CORR-01 落地另有 +7 条，使该文件 15→22→**25**。**差额全部为新增用例，无回归。** 我未向旧数字对齐。 |
| `D-03` | 口径措辞 | 交接摘要中「W07 已收口/门禁通过」类表述**未在 W12 对外件中出现**（扫描器 + 我的宽口径自扫双向确认）。W07 口径保持「有条件放行；门禁通过：否」，W08「有条件放行」，W09/W10/W11「门禁未评」，W12「未评」。 |
| `O-01` | 未复核项 | `ENV-RESIDUE` 的**精确行数**本轮未复核（需连控制库 `COUNT`；为遵守"不 DELETE/UPDATE/TRUNCATE + 不新增写"纪律，未执行）。只登记"本轮复跑继续累加"这一**事实方向**，不给数字。 |
| `O-02` | 错误码顺序依赖 | 人工项复用自动 `item_id` 时：人工项**排在列表后** → 先命中 `origin` 校验 → 抛 `LIMITATION_MODIFIED`；**排在前** → 抛 `LIMITATION_ID_CONFLICT`。两种都**拒绝**（安全），但公开错误码依赖顺序，与 `docs.py` docstring 的单一承诺略有出入。建议后续版本在 `_auto_items_immutable` 里把 ID 冲突检查前置。**不影响 AC-21 判定**。 |
| `O-03` | 文案瑕疵 | `alpha-acceptance-report.md` AC-04 段「写定义集合为空 （True）」有多余空格；`benchmark-baseline.md`/`deployment-guide.md` 未见同类问题。不影响语义与断言，仅记录。 |
| `O-04` | 一致性说明 | `AlphaAcceptanceReport` 的校验器对「完整 Alpha」是**绝对禁止**（连否定式都禁），比扫描器的否定语境豁免**更严**；两者方向一致（严于宽），不构成冲突。 |

---

## 10. 表述禁令自查（C-7）

### 10.1 我在本件中的用词纪律（逐条）

| 禁令 | 本件处理 |
|---|---|
| 不得写「W07 已收口 / 门禁通过 / 放行」 | 仅引述权威口径「W07：有条件放行；门禁通过：否」，**未**作通过性宣称 |
| 不得写「容器网络隔离已验证」 | 统一写「配置层已实测 / 运行态 NOT_VERIFIED」 |
| 不得写「环境已清理 / 已可复现清零」 | 只登记 `ENV-RESIDUE` 只追加不清理，**未**声称清零 |
| 不得写「QA 独立实跑通过」 | 逐条区分「**重跑**」「**复算**」「**采信**」；未执行项明写 `NOT_EXECUTED` |
| 不得写「回归全绿」 | 只给真实数字：`1796 passed / exit 0`，并给出 exit code |
| 不得写「Alpha 已就绪」「完整 Alpha」 | **本件无此二表述的正向用法**；相关字样仅出现在**禁令原文引述**与**否定句**中 |
| 不得替主理人签门禁 | 结论只写「定向验收不通过（有条件）」，并声明 **W12 门禁「未评」**，不宣布发布 |

### 10.2 对 W12 对外件的双向自查

- **复用扫描器**：`scan_claims()` → `ok=True`、`violations=()`（11 严格件零违规）。
- **独立宽口径自扫**（自写正则，不复用豁免）：剥离行内 code span 后命中 **0**；不剥离时命中 **3**，全部为 `claim-scan-report.md` 的规则自述行内代码（§5.7）。
- **独立扫描器**与 `claims.py` 的两处实现差异（我方宽口径 vs 豁免逻辑）**结论一致**：现存 9 个严格件无任何正向禁用表述。

### 10.3 本件自扫结果（如实披露）

用 `FORBIDDEN_CLAIMS` 正则 + `W12_EXTRA_FORBIDDEN` 子串对本件做**围栏外**自扫：命中 **20 处**，逐处人工复核后**全部**为引述/构造输入说明/规则码名，**无一处正向宣称**（明细见 `R-W12-QA-03`）。这也说明：**若把本件纳入 T06 严格集，必须连同豁免口径一起裁定**，否则会与「禁止引述禁令原文」自相矛盾。

---

## 11. 智能路由判定

**判定：`NoOne`**（源码无可证 Bug；测试代码无遗留缺陷）+ **1 项残余风险交主理人裁定**（`R-W12-QA-01`）。

依据：
1. **AC-03 不是源码 Bug 待修**：归因独立复算成立（`executor.py:486` ↔ `verification/plan.py:296-309` 形状不一致），且主理人 `§14.1` 已裁定「不修、如实记 FAILED」。W12 `functional.py` 侧**无错** → **不路由 Engineer**。
2. **无测试代码 Bug 遗留**：本轮我自写的 3 处脚本缺陷（§5.8）已自修并重跑，属 **QA（self）** 路由且已闭环；W12 既有用例（120 passed）未发现断言错误。
3. **`R-W12-QA-01` 不构成 Engineer 路由**：属**设计明文机制**（`02-design.md:455`），修它等于改 W12 生产代码（我的纪律禁止）+ 需先有裁定。故**上报裁定**而非指派修复。
4. **轮次**：本轮为 **第 1 轮**，无源码缺陷需回退，故不进入第 2 轮（上限 2 轮未用尽）。

---

### 11.1 附：第 1 轮已知问题清单（原文保留，最终口径见 §12.9）

**制式登记 5 条**：`KD-W11-01`、`KD-W11-02`、`KD-W11-03`、`KD-W12-01`（AC-03 `FAILED`）、`KD-W12-02`（AC-02 输入形状未覆盖）。
**环境 7 条**：`ENV-PG5432`、`ENV-1045`、`ENV-NV1`、`ENV-MODEL`、`ENV-RESIDUE`、`DECISION-TECH-PREVIEW`、`DOC-PARTIAL`。
**QA 新增 3 条残余风险**：`R-W12-QA-01`（围栏/行内代码剥离，**待裁定**）、`R-W12-QA-02`（frozen dataclass 语言级写穿）、`R-W12-QA-03`（本件不在严格扫描白名单）。
**合计**：**15 条**（5 + 7 + 3）。

> 第 2 轮变更：`KD-W12-01` 已于 2026-11-17 关闭（移到 `known-limitations.md` §3.1「已关闭偏离」留档），
> 故制式登记由 5 条降为 **4 条**，合计 **14 条**。原文其余判断不变。

**未解决即不判通过的红线（第 1 轮口径）**：AC-03 = `FAILED`。它使发布结论只能是技术预览档；**这不因文档写得好而改变**。
（第 2 轮：AC-03 经根因修复后实测 `PASSED`，该红线解除；但**发布结论仍为 `TECH_PREVIEW`**，因 W11 `decision` 未变、环境阻塞未清。）

---

### 11.2 附：第 1 轮声明（原文保留）

1. 本轮**未执行**任何 `git commit` / `git add` / `git checkout` / `git reset`；W09–W12 全部改动仍在工作区未提交。
2. **未修改**任何 W12 生产代码（`core/release/**`、`docs/w12/**` 既有件）、**未修改**任何既有断言与既存测试文件。
3. **未新建**替代用例绕开 `tests/unit/test_write_tool_gate.py`、`tests/unit/test_tools_contracts.py`、`tests/integration/test_w09_redaction_pipeline.py`。
4. **未执行** `DELETE` / `UPDATE` / `TRUNCATE`；**W12 零 DDL**；未执行 `rollback` / `DROP`；未触碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`。
5. **未改动**冻结资产（`benchmarks/baseline_v0|faults|w10/**`、`docs/w07–w11/**`、`docs/migrations.md`）与 `docs/w09/06-qa-report.md:624`。
6. QA 临时脚本全部落在仓库外 `D:/tmp/w12qa/`；本件是唯一新增文件。
7. **W12 门禁结论：未评。** 本件只给「定向验收不通过（有条件）」，不签门禁、不宣布发布。

— **QA 工程师 严过关（Yan）**（第 1 轮，时点 `2026-11-16`）

---

## 12. 第 2 轮：KD-W12-01 修复复验（最终生效）

> 时点 `2026-11-17`｜团队 `software-dbguardian-kd1201`｜现场 `HEAD=2850eca`（与第 1 轮同一提交，全部改动未提交）
> 本轮任务：**独立证伪**工程师对 `KD-W12-01` 的修复，并据此刷新 AC 覆盖与已知问题清单。
> 本轮**不是**重跑签字：核心证据是 §12.3 的**退化反证**——把修复函数在内存中退化为恒等函数后，同一份输入立刻回落 `FAILED`。

### 12.1 本轮变更边界（谁改了什么）

| 侧 | 文件 | 变更 | QA 复核 |
|---|---|---|---|
| 生产代码 | `core/verification/plan.py` | 新增 `_observed_payload(result)`（优先 `result["data"]`，非 Mapping 回落 `result`）；`_collect_observed` 的快照 `before`/`after` 与普通 metric 都**先 `data`、缺失回落顶层** | `git diff` 逐行核对：**仅读取端**，无断言常量改动、无写入端改动 |
| 生产代码 | `core/tools/executor.py` | **一行未动**（`git diff --stat` 为空，不在 22 个 `M` 内） | 落盘形状 `ToolResult.model_dump(mode="json")` 仍是全量快照（实测含 `status`/`data`/`error`，指标在 `data` 下） |
| 用例 | `tests/unit/test_verification_plan.py` | 新增 5 例（真实形状读 `data` / 真实形状值不符→`FAIL` / 扁平仍支持 / `data` 无快照键回落顶层 / 两处都无 metric 仍 `UNKNOWN`）；文件 19 passed | `git diff --numstat` = **131 增 / 1 删**，唯一删除行是 `VerificationAssertion` 单行 import 展开为多行；**既有用例零改写**，W08 扁平注入夹具 `_step_observed()` 保持扁平且相关用例全部通过 |
| 交付件 | T01 断言、`known-limitations.json/.md`、`alpha-acceptance-report.md`、`release-notes.md`、`claim-scan-report.md`、`demo-script.md`、`deployment-guide.md`、`security-materials.md`、`core/release/publish.py`、`core/release/deploy.py` | AC-03 段改 `PASSED`；`KD-W12-01` 移入 `CLOSED_DEVIATIONS`；清 7 处陈旧陈述与死常量 `ISSUE_RED_LINE_AC03` | 见 §12.5 / §12.6 |
| **QA 未改** | — | 本轮**未改**任何生产代码与既有断言；反证只在**内存**中 monkeypatch，未落盘 | 见 §12.10 |

**刻意保留**：`docs/w12/06-w12-qa-closeout.md` §1–§11 与 §11.1/§11.2 中的"当时 `FAILED`"表述**逐字保留**（主理人裁定：保留原值构成审计链，不改）。因此本件中仍可检索到 `FAILED` 字样，均属**历史记录**或**关闭叙述**，非当前结论。

### 12.2 AC-03 转 `PASSED`：实测四字段 + observed 值核查（防"空值被判 PASS"）

我按 `tests/integration/test_w12_functional_acceptance.py` 的同口径装配（真实控制库 + `build_worker_stack(settings)` + 真实 Gateway 单行 UPDATE），**独立跑 `run_functional_acceptance()`** 并直接打印 `repair_closure`：

| 字段 | 实测值 |
|---|---|
| `status` | **`PASSED`** |
| `verdict` | **`PASS`** |
| `reason_code` | **`OK`** |
| `assertion_verdicts` | `{"affected_rows": "PASS", "target_note_final": "PASS", "snapshot_comparable": "PASS"}` |
| `tool_calls` / `row_changes` | **1 / 1**（执行事实未变） |
| `detail`（含 observed） | `affected_rows=PASS(OK;observed=1); target_note_final=PASS(OK;observed='w12-ac03-73a4aa69'); snapshot_comparable=PASS(OK;observed='before=w08-comp-before\|after=w12-ac03-73a4aa69')` |

**observed 非空核查（关键）**：三条断言的 `observed` 均为**实值**——`1` / 目标 note 字符串 / `before=…|after=…` 双等值串，
**不是** `None`、**不是**空串。因此 `PASS` 不是"取到空值后被宽容判过"，而是"取到真值且与期望相等"。
`row_changes=1` 另有独立佐证：`finally` 分支把目标行 note 复原，故每次运行的前后值必然不同（`before != target`）。

### 12.3 退化反证（本轮最强证据：证明"转绿 = 根因修复"，不是别处凑巧）

做法：在同一进程内把 `core.verification.plan._observed_payload` 替换为**恒等函数** `lambda result: result`（等价于修复前"只在顶层取指标"的行为），**不落盘、不改文件**，对**同一个已装配的执行栈**再跑一次 `run_functional_acceptance()`；随后还原并第三次复核。

| 场景 | `status` | `verdict` | `reason_code` | 逐条断言 | 关键 observed |
|---|---|---|---|---|---|
| **A 修复后（真实链）** | `PASSED` | `PASS` | `OK` | 三全 `PASS` | `observed=1`、`before=…\|after=…` |
| **B 退化为恒等函数** | **`FAILED`** | **`FAIL`** | **`OBSERVED_SHAPE_MISMATCH`** | `affected_rows=UNKNOWN`、`snapshot_comparable=FAIL`、`target_note_final=PASS` | `observed=None`、`before=None\|after=None` |
| **C 还原后再次实跑** | `PASSED` | `PASS` | `OK` | 三全 `PASS` | 同 A（新 target） |

**推论（独立成立）**：
1. A 与 B 的**唯一差异**是 `_observed_payload` 的实现 → 结论翻转由该函数**单独**导致，不存在其他路径凑巧凑绿；
2. B 的失败形态与第 1 轮记录的 `KD-W12-01` 现场**逐字一致**（`affected_rows=UNKNOWN(OBSERVED_MISSING)`、`snapshot_comparable=FAIL`），说明第 1 轮归因正确；
3. C 证明 A 不是"某次运行的残留状态凑巧"（同一栈、同一时段连续三次，B 失败后 A 再次成功）；
4. 还原后 `plan_mod._observed_payload is original` 实测 `True` → **仓库文件未被改动**。

### 12.4 形状边界四类（防"一律 PASS"注水）

用真实 `adapters/mysql/lab_repair.py` 的验证计划（`affected_rows` / `target_note_final` / `snapshot_comparable` 三条真断言）与真实 `ToolResult.model_dump(mode="json")` 落盘形状，独立调 `evaluate_plan`：

| # | 构造 | `affected_rows` | `snapshot_comparable` | `target_note_final` | 判定 |
|---|---|---|---|---|---|
| ① | **真实形状**（指标在 `result["data"]` 下） | `PASS`（observed=`1`） | `PASS`（`before=broken\|after=repaired`） | `PASS` | 符合预期 |
| ② | **扁平形状**（W08 历史注入，指标在顶层） | `PASS` | `PASS` | `PASS` | 兼容未退化 |
| ③ | **混合形状**（`data` 只带普通指标，快照双等值在顶层） | `PASS` | `PASS` | `PASS` | 双向兜底生效 |
| ④ | **真实形状但值不符**（`affected_rows=2`、`before=WRONG`） | **`FAIL`**（observed=`2`） | **`FAIL`**（`before=WRONG\|after=repaired`） | `PASS` | **是 `FAIL` 不是 `UNKNOWN`** |
| ⑤ | **真实形状且两处都无该 metric** | **`UNKNOWN`** + `OBSERVED_MISSING` | `PASS` | `PASS` | 不注水成 `PASS` |

**④ 的意义**：如果修复把"取不到"一律当作"通过"，④ 会得到 `PASS`；实测是 `FAIL`——说明修复做的是**取值路径归一**，不是放宽判定。
**⑤ 的意义**：真缺失仍走 `OBSERVED_MISSING` 稳定原因码，`fail closed` 面未被打穿。

### 12.5 限制清单（`AC-21` 回归）

| 项 | 实测 |
|---|---|
| `items` 总数 | **17**（= `AUTO` **13** + `MANUAL` **4**） |
| `MANUAL` 明细 | `KD-W11-01`、`KD-W11-02`、`KD-W11-03`、**`KD-W12-02`** |
| `KD-W12-01` 是否在 `items` | **否**（已移出） |
| `CLOSED_DEVIATIONS` | **1 项**：`KD-W12-01`、`closed_at = 2026-11-17`、`closure` 含 `plan.py`、`verification` 含 `PASSED`、marker `PARTIAL` |
| `items_digest`（文件值） | `c395c69152d36aec90ce98111294d849764139e8d7ffbd25d89be4ff0bd34f7f` |
| `items_digest`（独立重算） | **与文件值逐字相等**（`items_digest(loaded.items)` 重算一致；`KnownLimitations.model_validate` 加载即通过） |
| `known-limitations.md` 内嵌 JSON 块 vs `.json` 文件 | **逐字相等 = True**（块长 4686 字符；唯一命中块，无重复/无缺失） |
| `.md` §3.1「已关闭偏离」小节 | **存在**，含 `2026-11-17`、含"由 FAILED 转 PASSED"关闭叙述与验证数字 |
| **删任一 `AUTO` 项**（删 `ENV-PG5432`，并**同步重算 `items_digest`** 以排除"因摘要不符而拒"的假象） | 第 1 层构造期：**`ValidationError` → `LIMITATION_REMOVED`**；第 2 层 `model_construct` 绕过 → **`AcceptanceIntegrityError: LIMITATION_REMOVED: 自动基础项不可删除`** |

→ 两层防护仍成立；关闭项**不再占用** `items_digest`，但**留档可追溯**。AC-21 判定 `PASSED` 不变。

### 12.6 扫描器与死常量

| 项 | 实测 |
|---|---|
| `scan_claims(root)` | `ok=True`；`violations=()`（**0**）；`exempted_lines=24`（`exempted_items` 亦 24） |
| 三范围 | 严格件 **11**；`spec_mode` **2**（`02-design.md`、`03-task-cards.md`）；排除 **1**（`01-prd.md`） |
| `allowed_phrases` | **4**（第 1 轮由 7 收回 4，未回涨） |
| `demo_input_violations` / `limitation_integrity_ok` / `evidence_refs_ok` | `()` / `True` / `True` |
| `grep -rn ISSUE_RED_LINE_AC03` 全仓 | **0 命中**（常量已删、`__all__` 已同步、无残留引用） |
| 对外件是否仍有"当前 = FAILED"表述 | **无**。7 个对外件中的 `FAILED` 全部属"原 `FAILED` → 修复后 `PASSED`"的**关闭叙述**（`alpha-acceptance-report.md` 1 行、`release-notes.md` 1 行、`known-limitations.md` 1 行、`demo-script.md` 2 行、`deployment-guide.md` 1 行、`security-materials.md` 1 行、`claim-scan-report.md` 0 行）；`api-contract.md`/`02-design.md`/`03-task-cards.md` 的命中是 `DB_AUTH_FAILED` 错误码与枚举定义，非结论 |

**当前仍含"AC-03 = FAILED"表述的文件**：只有本件的第 1 轮记录（§0 表格首行、§4 矩阵 AC-03 行、§5.1、§11 依据等，共 9 行），
且已在 §0 与本节明确标注"历史记录、最终口径见 §12"。**这是保留审计链的刻意选择**，不是遗漏。

### 12.7 AC 覆盖矩阵（第 2 轮最终口径）

图例：`PASSED` / `FAILED` / `NOT_EXECUTED`（环境阻塞未执行，**不判 FAIL**）。
判定依据列沿用第 1 轮三源（`重跑` / `复算` / `证伪` / `采信`），第 2 轮对 AC-03 追加 `退化反证`。

| AC | 判定 | 依据 | 第 2 轮复核要点 |
|---|---|---|---|
| **AC-01** | `PASSED` | 重跑 + 读断言 | 五态 create/query/pause/resume/cancel 各 1 例；T01 14 passed 覆盖 |
| **AC-02** | `PASSED` | 重跑 + 读断言 | 三类报告五字段非空；**部分覆盖**：`data` 中"全表扫描 + 无 `table_stats`"输入形状仍未覆盖（`KD-W12-02` 在册） |
| **AC-03** | **`PASSED`**（第 1 轮 `FAILED` → **2026-11-17 关闭**） | 重跑 + **退化反证** | `status=PASSED`/`verdict=PASS`/`reason_code=OK`/三断言全 `PASS`；退化后立刻回落 `FAILED(OBSERVED_SHAPE_MISMATCH)`；修复点 `core/verification/plan.py`（`_observed_payload` + `_collect_observed` 回落逻辑）；`executor.py` 未动 |
| **AC-04** | `PASSED` | 复算 + 重跑 | PG 7 条全 `read`、写集合为空；**部分覆盖**：PG 段 `NOT_EXECUTED`（`ENV-PG5432`），非"通过" |
| **AC-05** | `PASSED` | 重跑 + 静态核对 | 冻结集 29 条；`review_negative_cases` 29/29 `denied`；无端口时返回 `()` + `NOT_AVAILABLE` |
| **AC-06** | `PASSED` | 复算 + 重跑 | 执行侧注册表无 `R4`；`r3_denied`/`r4_denied` 均 True |
| **AC-07** | `PASSED` | 重跑（需真实 LAB） | 四类绕过全 True，目标行不变（采信 security 18 passed 实跑） |
| **AC-08** | `PASSED` | 重跑（需真实 LAB/控制库） | `UNKNOWN` 不重试；审计不可写抛 `AuditUnavailableError` |
| **AC-09** | `PASSED` | 重跑（既存件） | `test_w09_redaction_pipeline.py` 第 1 轮 4 passed；第 2 轮 `tests/unit` 全量含相关单测 |
| **AC-10** | `PASSED` | 复算 | `REQUIRED_DEPLOY_FILES` 11 项缺失 0；`env_example_has_real_secret=False` |
| **AC-11** | `PASSED`（含 5 项 `NOT_EXECUTED`） | 复算 | 五项检查各 1 条、逐条带原因；`commit == git rev-parse HEAD`；`image_digest=UNKNOWN` + 登记 |
| **AC-12** | `PASSED` | 复算 | S1–S4 全 `NOT_EXECUTED` + 原因；`lab_only=True`、`production_dsn_referenced=False`；措辞红线判定生效。**部分覆盖**：演示未实跑 |
| **AC-13** | `PASSED` | 复算 | OpenAPI 3.1.0 / 31 paths；文档清单 23 条缺失 0、`PARTIAL` 2 |
| **AC-14** | `PASSED` | 复算 + 证伪 | `baseline_score is None`；注入 0/0.0/0.42/1 全 `ValidationError`；`BLOCKED_MODEL_ENV_MISSING` |
| **AC-15** | `PASSED` | 复算 | `NON_PROMISES` 六项 marker 落在 `{NOT_SUPPORTED, PARTIAL, UNKNOWN}` |
| **AC-16** | `PASSED` | 复算 + 证伪 | `None`→`TECH_PREVIEW`+`DEFAULT_TECH_PREVIEW_ONLY`；仅 `ALPHA_ALLOWED` 出 `ALPHA`；6 个异常值全 `TECH_PREVIEW` |
| **AC-17** | `PASSED` | 复算 + 证伪 | `inspect.signature == ['decision']`；`ReleaseGate` 6 字段、**无时间字段**（第 2 轮实测 `['release_type','release_name','effective_decision','decision_digest','registered_issues','source']`）；同输入逐位相等 |
| **AC-18** | `PASSED` | 复算 | `model="baseline"`、`sft_reproducible_gain=False`、`baseline_score=None`；三项 `deferred` 为 `ClassVar`（不在 `model_fields`） |
| **AC-19** | `PASSED` | 复算 + 证伪 | 第 2 轮重扫：`ok=True`、`violations=0`、`exempted_lines=24`、三范围 11/2/1（§12.6） |
| **AC-20** | `PASSED` | 复算 + 证伪 | 三模型字段集与 demo 键交集为空；注入即 `ValidationError`；`assert_no_demo_input` → `DEMO_AS_ACCEPTANCE` |
| **AC-21** | `PASSED` | 复算 + 证伪 | **17 项 = 13 AUTO + 4 MANUAL**；`KD-W12-01` 已移入 `CLOSED_DEVIATIONS`；删任一 `AUTO` 两层均 `LIMITATION_REMOVED`（§12.5） |
| **AC-22** | `PASSED` | 复算 + 重跑 | 全仓 AST：非测试代码 `side_effect="write"` 唯一 `lab_repair.py:77`；既存两文件 11 passed、无 `M` |

**统计（第 2 轮）**：`PASSED` **22** ｜ `FAILED` **0** ｜ 未覆盖 **0** ｜ 部分覆盖标注 **3**（AC-02 输入形状、AC-04 的 PG 段、AC-12 的演示段——后两者是 `NOT_EXECUTED` 子项，**不单独计 FAIL**）。

**AC-03 状态变更留痕**：`FAILED`（第 1 轮，2026-11-16）→ **`PASSED`**（第 2 轮，2026-11-17）。关闭依据：主理人 `§14.1` 关闭条件（修 `_collect_observed` 兼容 `result["data"]` + 补真实形状回归）已满足；关闭记录落在 `core/release/docs.py::CLOSED_DEVIATIONS` 与 `docs/w12/known-limitations.md` §3.1。

### 12.8 串行命令与真实数字（第 2 轮逐条可复核）

纪律同第 1 轮：解释器 `.venv/Scripts/python.exe`；每条 pytest 带 `-o addopts="" -q -p no:cacheprovider`；**全程串行，两个 pytest 未并发**；未使用 `--timeout=`；集成件外层 `timeout 180`，崩溃矩阵 `timeout 420`。

| # | 命令（依赖路径，非无路径全量） | 结果 | exit |
|---|---|---|---|
| 1 | `pytest tests/unit/test_verification_plan.py` | **19 passed / 0.24s** | 0 |
| 2 | `pytest tests/unit/test_w12_claims.py` | **25 passed / 0.78s** | 0 |
| 3 | `pytest tests/unit/test_w12_doc_inventory.py` | **17 passed / 1.12s** | 0 |
| 4 | `pytest tests/unit/test_w12_publish_gate.py` | **23 passed / 0.21s** | 0 |
| 5 | `timeout 180 pytest tests/integration/test_w12_functional_acceptance.py`（T01） | **14 passed / 36.59s** | 0 |
| 6 | `timeout 180 pytest tests/integration/test_w12_deploy_package.py` | **12 passed / 0.10s** | 0 |
| 7 | `timeout 180 pytest tests/integration/test_w12_demo_script.py` | **11 passed / 0.09s** | 0 |
| 8 | `timeout 180 pytest tests/integration/test_w12_security_acceptance.py` | **18 passed / 10.46s** | 0 |
| 9 | `timeout 420 pytest tests/integration/test_w08_crash_matrix.py` | **7 passed / 284.42s** | 0 |
| 10 | `pytest tests/unit`（全量） | **1801 passed / 29.82s** | 0 |
| — | QA 独立脚本 `D:/tmp/w12qa2/verify_round2.py`（真实链 + 退化反证 + 还原复核） | `A=PASSED/B=FAILED/C=PASSED`，`DEGRADE_PROOF_OK=True` | 0 |
| — | QA 独立脚本 `D:/tmp/w12qa2/verify_shapes_limits.py`（形状/限制/扫描/诚实性） | `ALL_ASSERTS_OK` | 0 |

- W12 七个用例文件合计 **120 passed**（19 中仅 T01 属该七件：25+14+18+12+11+17+23 = **120**，与第 1 轮同口径）。
- **`tests/unit` 1796 → 1801（+5）**：差额恰为 `§12.1` 列的 5 条真实形状用例，**无回归**。
- **异常终止登记**：本轮 **0 次** exit 139、**0 次** `Windows fatal exception`、**0 次**隔离重跑；`ENV-PY314-AST-CRASH` 未复现（收集期未崩溃）。
- **QA 自写脚本缺陷登记（如实披露）**：`verify_shapes_limits.py` 首跑 3 次报错，均为**我自己的脚本问题**——① 误用 `core.tools.contracts.VerificationPlan` / `ReadbackSample` 的导入路径与构造签名；② 断言参数缺 `note`/`expected_note` 占位符（导致 `target_note_final`/`snapshot_comparable` 报 `UNKNOWN`，**不是**被测代码问题）；③ `ReleaseGate` 是 dataclass（无 `model_fields`）且 `DeployPackageManifest` 需经 `collect_deploy_package()` 构造。逐项修正后全部断言通过，**未修改任何仓库文件**。属 **QA（self）** 路由。

### 12.9 已知问题清单（第 2 轮最终口径）

**制式登记 4 条**（`known-limitations.json`，`items` 17 项）：

| 编号 | 内容 | 状态 |
|---|---|---|
| `KD-W11-01` | W11 备份恢复口径偏离（桶命名） | 在册（`MANUAL`） |
| `KD-W11-02` | W11 覆盖漏项 | 在册（`MANUAL`） |
| `KD-W11-03` | W11 哨兵归属 | 在册（`MANUAL`） |
| `KD-W12-02` | 慢查询分析器「全表扫描 + 无 `table_stats`」潜在崩溃，AC-02 未覆盖该形状 | 在册（`MANUAL`） |
| ~~`KD-W12-01`~~ | ~~AC-03 `OBSERVED_SHAPE_MISMATCH`~~ | **已关闭 2026-11-17**，移入 `CLOSED_DEVIATIONS` 只留档 |

**环境 7 条**（未清，不判 FAIL）：`ENV-PG5432`、`ENV-1045`、`ENV-NV1`、`ENV-MODEL`、`ENV-RESIDUE`、`DECISION-TECH-PREVIEW`、`DOC-PARTIAL`。
**QA 残余风险 3 条**（性质不变）：`R-W12-QA-01`（严格件内代码语境被剥离，**待主理人裁定**）、`R-W12-QA-02`（frozen dataclass 语言级写穿）、`R-W12-QA-03`（本件不在严格扫描白名单）。
**合计**：**14 条**（4 + 7 + 3）。

**第 2 轮新增观察项（不构成路由）**：
- `O-05`｜`core/release/functional.py` 的 `run_repair_closure` 与 `RepairClosureCheck` **docstring 仍描述"KD-W12-01 下真实链结论是 FAILED"**（第 600–611 行、第 148 行附近）。行为已修复、docstring 未同步；**只影响阅读，不影响判定**（不属于对外材料，扫描器不覆盖 `.py`）。我**未改**（禁止改 W12 生产代码），建议后续批次顺手同步。
- `O-06`｜本项目 `docs/w12/02-design.md`、`03-task-cards.md` 为冻结资产，其中 `FAILED` 出现处是枚举定义（`PASSED="PASSED"; FAILED="FAILED"`）与「`NOT_EXECUTED` 时不得 `FAILED`」口径，**非结论性表述**，无需修改。

### 12.10 诚实性复核（第 2 轮实测）

| 项 | 实测 | 结论 |
|---|---|---|
| `release_type`（`derive_release_type(decision=None)`） | **`TECH_PREVIEW`**，`effective_decision=TECH_PREVIEW_ONLY` | AC-03 转绿**未**改变发布档位 |
| `ReleaseGate` 字段 | `['release_type','release_name','effective_decision','decision_digest','registered_issues','source']`；**含时间字段 0 个** | 纯函数、可复现 |
| `nv1_is_hard_gate` | 不在 `DeployPackageManifest.model_fields`；`ClassVar=False` | 不可被数据覆盖 |
| `image_digest` / `container_runtime_status` | `UNKNOWN` / `NOT_VERIFIED` | 未编造 |
| `baseline_score` | 默认 `None`，注入即 `ValidationError` | 未填估计值 |
| `nv1_is_hard_gate` / `dpo_deferred` / `full_14b_deferred` / `rl_deferred` | 均不在 `model_fields` | `ClassVar` 恒值 |
| 全文正向表述自查 | 本件第 2 轮新增段落**未作任何通过性 / 就绪性 / 清零性宣称**；W12 门禁写明**未评**；发布结论写明仍为技术预览档 | 逐句人工复核 + 第 1 轮 §10 的用词纪律继续适用 |

### 12.11 最终路由判定与声明

**最终路由：`NoOne`。**

依据：
1. **AC-03 转 `PASSED` 是根因修复的结果**，且已用退化反证独立证明（§12.3）：修复函数退化为恒等函数即回落 `FAILED`，还原即恢复 `PASSED`；修复面仅限读取端，未动写入端与断言常量。
2. **无源码 Bug 待修**：第 2 轮 4 组独立证伪（真实链实测 / 退化反证 / 形状边界 5 例 / 限制清单 8 项）+ 扫描器 + 诚实性全部符合预期，未发现可归因到 `core/**` 的缺陷。
3. **无测试代码 Bug 遗留**：我自写的 3 处脚本缺陷（§12.8）已自修，属 **QA（self）** 路由且已闭环；W12 既有用例 120 passed、`tests/unit` 1801 passed，未发现错误断言或注水断言。
4. **`R-W12-QA-01` 仍不路由 Engineer**：属设计明文机制（`02-design.md:455`），修它需先有主理人裁定且属改 W12 生产代码（本轮纪律禁止）。
5. **轮次**：本轮为**第 2 轮**，两轮上限**已用尽**；本轮全绿，故无第 3 轮需求。

**声明（第 2 轮）**：
1. 本轮**未执行**任何 `git commit` / `git add` / `git checkout` / `git reset`；W09–W12 全部改动仍在工作区未提交（`HEAD=2850eca`、`git diff --name-only=22`、`git status --short=116` = 22 `M` + 94 `??`（目录折叠口径；`-uall` 逐文件为 172））。
2. **未修改**任何 W12 生产代码与既有断言（`plan.py` 只在**内存**中 monkeypatch 做反证，未落盘；实测还原后函数对象同一性为 `True`）。
3. **未新建**替代用例绕开 `test_write_tool_gate.py` / `test_tools_contracts.py` / `test_w09_redaction_pipeline.py`。
4. **未执行** `DELETE` / `UPDATE` / `TRUNCATE` 语句；**W12 零 DDL**；未触碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`。
   （说明：AC-03 真实链经**既有唯一 R2 写工具**对 LAB 单行 `note` 做受限更新，并在 `finally` 中复原——这是第 1 轮同一用例的既有行为，非本轮新增写能力。）
5. **未改动**冻结资产（`docs/w07–w11/**`、`docs/w12/02-design.md`、`docs/w12/03-task-cards.md`、`benchmarks/**`）与 `docs/w09/06-qa-report.md:624`；该文件 `git diff --numstat` 仍为 **128 / 0**，与第 1 轮一致。
6. QA 临时脚本全部在仓库外 `D:/tmp/w12qa2/`；本轮**唯一**改动的仓库文件是本件（`docs/w12/06-w12-qa-closeout.md`）。
7. **W12 门禁结论：未评。** 本轮只给「**第 2 轮复验通过**」，不签门禁、不宣布发布；**发布结论仍 `TECH_PREVIEW`**，不因 AC-03 转绿而改变。

— **QA 工程师 严过关（Yan）**（第 2 轮，时点 `2026-11-17`）
