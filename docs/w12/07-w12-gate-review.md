# W12 最终 Alpha 门禁评审（Alpha 验收与发布）

| 项 | 值 |
|---|---|
| 评审件编号 | `docs/w12/07-w12-gate-review.md` |
| 评审人 | 严过关（Edward）· QA 工程师｜团队 `software-dbguardian-w09-closeout2` |
| 评审性质 | **门禁评审**（不是定向验收；`docs/w12/06-w12-qa-closeout.md:7` 与 `:27` 明写「**W12 门禁结论未评**——本件不替主理人签门禁、不宣布发布」） |
| 时点锚 | `HEAD = 2850eca326850eec491e9159ec7033a85807d080`；全部改动**未提交**（`git diff --name-only` = 25、`git status --short` = 121 = 25 `M` + 96 `??`） |
| 解释器 / 现场 | `.venv/Scripts/python.exe` = Python 3.14.7；`D:/DBGuaredian` |
| 判据来源 | `docs/w12/01-prd.md` **§6 `:154-170`**（4 条最终 Alpha 门禁 + 未通过处置 + 完成定义）、§5 AC 表 `:129-152` |
| 发布档位 | **`release_type = TECH_PREVIEW`**（由 W11 `publish_decision → TECH_PREVIEW_ONLY` 派生）。**本件不宣布发布、不写 Alpha 结论** |
| 上游口径 | W07「**有条件放行；门禁通过：否**」；W08「**有条件放行**」；W09「**有条件放行｜门禁通过：否**」；W10「**有条件放行｜门禁通过：否**」；W11「**有条件放行｜门禁通过：否**」（四份同批出件） |
| 唯一写入 | **本文件**。未改任何 `.py` / SQL / 配置 / 既有 docs（含 `docs/w12/` 既有 17 件，**尤其未改 `known-limitations.json` / `known-limitations.md` / `openapi.json`**）；未改数据库；未 `git commit/add/checkout/reset` |

---

## ① 执行摘要

**门禁结论：`有条件放行`｜门禁通过：否。发布档位：`TECH_PREVIEW`（不是 Alpha）。**

> **2026-11-18 复核更新（结论未变）**：工程师随后修了 A（`derive_release_type` 枚举派生）、B（`alpha-acceptance-report.md:29` 断言态）、C（`12-w09-qa-closeout.md:344/:346` 过期登记）三项。我逐项独立复核，A/B/C 全部 **PASS**，**`C-W12-3` 已关闭，条件数 7 → 6**；但 `C-W12-1`（未提交）、`C-W12-4`、`C-W12-5`（发布结论仍走 `decision=None` 默认分支）、`C-W12-2`/`C-W12-6`/`C-W12-7` 仍开放，**门禁结论维持「`有条件放行`｜门禁通过：否」**。详见 **§⑧**（其中 §8.5 披露了我在首版 §3.2 的一处取证错误并已纠正）。

判据一句话：PRD §6 的四条最终 Alpha 门禁判据中 **1 条满足、3 条部分满足**，且 `docs/w12/01-prd.md:170` 的**完成定义「交付物已提交到仓库」不满足**（零 commit）——存在多项未满足，**不能判「通过」，更不能据此标记完整 Alpha**。

**为什么不是「未通过」**：
- **无 FAIL 的 AC**：22 条 AC 全部 `PASSED`（AC-03 已于 2026-11-17 由 `FAILED` 转 `PASSED`，我用上一轮遗留的退化反证记录 + 本轮 `test_w12_functional_acceptance.py` 14 passed 双向采信）；
- 无删减安全门禁：`G-2`「至少一类受审批的 LAB 修复闭环」**满足**（MySQL LAB 单行 UPDATE + 独立验证三断言全 PASS）；
- `G-3` 的四份报告齐备，`G-4` 的交付物 11 项必需文件缺失 0、限制清单 17 项与 `items_digest` 自洽；
- 环境阻塞（`ENV-PG5432` / `ENV-1045` / `ENV-NV1` / `ENV-MODEL` / `ENV-RESIDUE`）按 PRD `:162`/`:168` 如实登记，不判 FAIL、不算实现失败。

**为什么不能判「通过」**：

1. **完成定义「交付物已提交到仓库」不满足**：`HEAD` 仍为 `2850eca`，25 `M` + 96 `??`，零 commit。PRD `:170` 原文「**不得仅以「代码已写完」作为完成标准**」。
2. **`G-1` 双引擎只读诊断**：PG 段 `NOT_EXECUTED`（`ENV-PG5432`）→ 「双引擎」只完成 MySQL 一侧。
3. **`G-3` 备份恢复报告**：继承 W11 的 `KD-W11-01` / `KD-W11-02` / `KD-W11-03` 三项口径偏离（桶命名、漏 2 张只追加表、哨兵空真），仍在 `known-limitations.json` 的 `MANUAL` 项内。
4. **`G-4` 交付物「限制诚实」维度存在 3 处实证问题**（我本轮独立发现，前序收口件未登记，详见 §②G-4 与 §③3.2）：
   - `docs/w12/alpha-acceptance-report.md` 的发布结论**落到 `decision=None` 默认分支**（`source=DEFAULT_TECH_PREVIEW_ONLY`、登记项 `PUBLISH_DECISION_MISSING`、`decision_digest=None`）——即 W11 真实 `publish_decision()` 输出**未被这份发布件消费**；
   - 同一文件 `:29` 仍写「`affected_rows=UNKNOWN、snapshot_comparable=FAIL`」（AC-03 修复**前**的断言态），与同文件「功能验收」段的 `AC-03 PASS` **自相矛盾**；
   - `KD-W12-02` 的**代码已于 2026-11-18 修复**（`core/diagnosis/analyzers.py` +16 行、回归 2 例）但 `known-limitations.json` 仍标 `UNKNOWN` / 未修，发布件与实现不一致。

**「定向验收通过」≠「门禁通过」**：`06-w12-qa-closeout.md:7,27` 明确本件只给「复验通过 / 不通过」，门禁未评。本件不继承其结论；`release_type` 亦不因 AC-03 转绿而升级（`:26` 原文：「**不因 AC-03 转绿而改变**」）。

> **口径声明（全件有效）**：本件**不使用**「Alpha 通过」「Alpha 就绪」「完整 Alpha 已达成」等表述。本件结论只有两档用语：**「`有条件放行`｜门禁通过：否」** 与 **「`TECH_PREVIEW`」**。

---

## ② 门禁判据逐条对账

> 逐条取自 `docs/w12/01-prd.md` §6（`:157-160`），另加 `:170` 的完成定义。

### G-1｜双引擎只读诊断稳定（`01-prd.md:157`）— **部分满足**

> 原文：AC-01～AC-04 全绿（PG 侧受 5432 阻塞段按 `NOT_EXECUTED` 如实登记，不冒充通过）。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-01 | 五态生命周期可追溯（`seq` 连续） | `tests/integration/test_w12_functional_acceptance.py` | **E-2 中该文件 `14 passed`**（含在 120 内） | 满足 |
| AC-02 | 三类报告五字段非空率 100% | 同上 | E-2 覆盖；**部分覆盖**（`KD-W12-02` 输入形状） | 部分满足 |
| AC-03 | 审批修复闭环独立验证 PASS | 同上 | E-2 覆盖；修复点 `core/verification/plan.py`（读取端形状归一），`executor.py` 一行未动 | 满足 |
| AC-04 | 第二引擎保持只读（`write_tools() == []`） | 同上 | E-2 覆盖；**PG 段 `NOT_EXECUTED`** | **部分满足** |

**G-1 结论**：`部分满足` —— 「双引擎」中的 PG 侧从未在真实链路上跑过（`ENV-PG5432`）。按 PRD `:157` 原文这不算 FAIL，但**也不构成「稳定」的完整证据**：单引擎稳定 ≠ 双引擎稳定。

### G-2｜至少一类受审批的 LAB 修复闭环通过独立验证（`01-prd.md:158`）— **满足**

> 原文：AC-03 全绿；另一引擎未完成写闭环时保持只读（AC-04）。

| 子判据 | 实测 | 判定 |
|---|---|---|
| AC-03 `verdict == PASS`、`tool_calls == 1`、目标行变化 == 1 次 | `test_w12_functional_acceptance.py` 在 E-2 内 `14 passed`（0 failed） | 满足 |
| 修复点是根因修复、不是放宽判定 | `06-w12-qa-closeout.md` §12.3 的**退化反证**：把 `_observed_payload` 在内存里退化为恒等函数后同一份输入立刻回落 `FAILED(OBSERVED_SHAPE_MISMATCH)`；`git diff` 核对该函数只改**读取端** | 满足（采信前序件 + 我核对 `git diff --stat core/verification/plan.py` 存在改动、`tests/unit/test_verification_plan.py` 有新增） |
| 另一引擎保持只读 | `write_tools() == []`（AC-04） | 满足 |
| AC-22 写面不回退 | **E-8 `11 passed`（6 + 5）** | 满足 |

**G-2 结论**：`满足`。这是 W12 唯一**判据完全满足**的一条门禁项——MySQL LAB 的「审批 → 单次写 → 独立验证 PASS」闭环真实成立。

### G-3｜安全负面集、状态恢复、跨租户和备份恢复有报告（`01-prd.md:159`）— **部分满足**

> 原文：AC-05～AC-09、AC-19～AC-22 全绿；逐条复核 W11 报告，任一未审批/篡改/过期/重放/环境变化的 R2 被拒。

| AC | 判据要点 | 证据位置 | 我的独立取证 | 判定 |
|---|---|---|---|---|
| AC-05 | W11 负面集逐条复核（29 条 `denied is True`） | `tests/integration/test_w12_security_acceptance.py` | **E-2 中该文件 `18 passed`**；W11 侧我另跑 **E-16 `32 passed`** | 满足（但继承 W11 的 AC-03 空真 → 见下） |
| AC-06 | R3/R4 不进执行器 | 同上 | E-2 覆盖 | 满足 |
| AC-07 | 只读角色四类绕过被拒 | 同上 | E-2 覆盖（需真实 LAB） | 满足 |
| AC-08 | UNKNOWN 不重试 + 审计失败 fail closed | 同上 | E-2 覆盖；W11 侧 **E-17 `16 passed`** | 满足 |
| AC-09 | 敏感数据不越界 | 同上 + `test_w09_redaction_pipeline.py`（既存） | **E-12 `4 passed`（真实库）** | 满足 |
| AC-19 | 表述禁令（`docs/w12/` 全文不含 5 条禁用表述） | `tests/unit/test_w12_claims.py` | **E-2 内 `25 passed`**；我另做只读实算：`scan_claims()` → `ok=True`、`violations=()`、`exempted_items=24`、`allowed_phrases` 4 条、`limitation_integrity_ok=True`、`evidence_refs_ok=True`、`demo_input_violations=()` | 满足 |
| AC-20 | 演示不等于验收（`evidence_refs` 含 W09/W10/W11 门禁件） | 同上 | 同上（`evidence_refs_ok=True`） | 满足 |
| AC-21 | 限制清单不可删 | 同上 | **我独立只读复算 17 项 / 13 AUTO + 4 MANUAL / `items_digest` 重算 == 文件值**（§3.1） | 满足 |
| AC-22 | 写面与引擎不回退 | `test_write_tool_gate.py` + `test_tools_contracts.py` | **E-8 `11 passed`** | 满足 |

**「备份恢复有报告」的报告本体**：`docs/w11/backup-restore-report.md` 存在且口径诚实（`restore_mode="LOGICAL_SHADOW"`、`physical_restore="NOT_SUPPORTED"`、`measured_*=null`、`sla_claim=False`）。**但**该报告的三项口径偏离仍在册：

| 编号 | 内容 | 状态 |
|---|---|---|
| `KD-W11-01` | `audit_chain` 桶命名与设计 §8.2 / W08 语义不一致（`task_events` 才是哈希链本体） | `MANUAL` / `PARTIAL`，未关闭 |
| `KD-W11-02` | 快照未含 `approval_consumptions` 与 `step_prepared`（设计 §8.2 明列的 5 张表中漏 2 张） | `MANUAL` / `PARTIAL`，未关闭 |
| `KD-W11-03` | G1–G3 哨兵归属错位（29 条负面集中 23 条为结构性空真） | `MANUAL` / `PARTIAL`，未关闭 |

→ **「备份恢复有报告」满足；「备份恢复报告无口径偏离」不满足**。按 PRD `:159` 的字面（「有报告」+「逐条复核 W11 报告」），这条落在**部分满足**。

**G-3 结论**：`部分满足`。

### G-4｜交付物完整、限制诚实（`01-prd.md:160`）— **部分满足**

> 原文：AC-10～AC-18、AC-20～AC-21 全绿；源代码、部署、接口合同、安全材料、数据、Benchmark、模型版本与验收报告齐全；明确 Alpha 不支持生产自动写入；**不把建议、部分证据或未经验证的修复表述为成功**。

**（a）「齐」的三项我独立核对：全部通过**

| 项 | 我的实测 | 判定 |
|---|---|---|
| 部署必需文件 | `REQUIRED_DEPLOY_FILES` = **11 项，missing = []`** | 满足 |
| OpenAPI | `openapi = "3.1.0"`、`paths = 31` | 满足 |
| 限制清单 | **17 项 = 13 `AUTO` + 4 `MANUAL`**；`items_digest` 重算 == 文件值 `c395c691…`；`known-limitations.md` 内嵌 JSON 块与 `.json` **内容相等**；marker 落在 `{NOT_SUPPORTED, PARTIAL, UNKNOWN}` 闭集 | 满足 |
| 六类不承诺能力 | `NON_PROMISES` = 6 项（`NP-PROD-AUTO-WRITE` / `NP-GENERIC-AUTO-REPAIR` / `NP-ALWAYS-DIAGNOSE` / `NP-FULL-DEADLOCK-INDEX` / `NP-EXACTLY-ONCE` / `NP-NETWORK-ISOLATION-VERIFIED`），逐项在清单内 | 满足 |
| `.env.example` 三态 | 注释段含 `# DG_MODEL_PROVIDER=disabled`，正文 `DG_MODEL_PROVIDER=fake`，文档段列出 `fake` / `openai_compat` / `disabled` 三态 | 满足（收尾轮 B2 已落） |

**（b）「诚实」的三处实证问题：均不满足**

| 编号 | 问题 | 我的取证 | 严重性 |
|---|---|---|---|
| **F-W12-1** | **发布件的发布结论来自默认分支，不是 W11 的真实判定**：`docs/w12/alpha-acceptance-report.md` 的结论表为 `source = DEFAULT_TECH_PREVIEW_ONLY`、登记项 `PUBLISH_DECISION_MISSING`、`decision_digest = None`。即 `derive_release_type(decision=None)` 的兜底路径，而非 `publish_decision()` 的真实输出（我实算真实输出为 `TECH_PREVIEW_ONLY` + 5 个环境阻塞） | 只读读发布件 + 实算真实 `publish_decision()`（§3.2） | 中（**方向安全**：两者都落 `TECH_PREVIEW`；但"由 W11 判定派生"这一说法在发布件上是**未兑现的**，且 `PUBLISH_DECISION_MISSING` 是**不真实**的登记） |
| **F-W12-2** | **发布件内自相矛盾、且与实测不符**：`alpha-acceptance-report.md:29` 写「独立验证逐条断言：**`affected_rows=UNKNOWN`、`snapshot_comparable=FAIL`**、`target_note_final=PASS`」，而同文件「功能验收」段写 `AC-03 … 独立验证结论 PASS、闭环状态 PASSED（原因码 OK）` | `grep -n "affected_rows" docs/w12/alpha-acceptance-report.md` → 仅 1 处命中（`:29`） | 中（**方向偏保守**，不是夸大；但发布件内部矛盾 = "限制诚实"不成立） |
| **F-W12-3** | **发布件落后于实现**：`KD-W12-02`（慢查询分析器 `UnboundLocalError`）的**代码已于 2026-11-18 修复**（`core/diagnosis/analyzers.py` **+16 行**；`tests/unit/test_diagnosis_analyzers.py` **+74 行 = 2 条回归用例**），但 `known-limitations.json` 仍标 `marker="UNKNOWN"`、`statement` 写「会抛 `UnboundLocalError`」 | `git diff --stat`（§3.3）+ 只读读 `known-limitations.json` 第 132–138 行 | 低（**方向偏保守**：把已修的问题仍列为未修，属于欠报而非虚报） |

> **F-W12-3 的处置边界（必须照抄）**：`docs/w10-w12-lead-rulings.md` **R18-02 C** 明令「**不改** `docs/w12/known-limitations.json` 的发布物状态（改它需重算 digest，另立流程）」。因此**该不一致是裁定的已知结果，不是本轮违规**，也不允许我在本件里"顺手修掉"。它作为**放行条件**登记（C-W12-4）。

**G-4 结论**：`部分满足` —— 「齐全」满足，「诚实」的三条只满足"不夸大"，但违反"发布件与实现一致"。

### G-5｜本周完成定义（`01-prd.md:170`）— **不满足**

| 子条件 | 实测 | 判定 |
|---|---|---|
| 交付物已**提交到仓库** | `HEAD` = `2850eca…`；25 `M` + 96 `??`；W12 新增 27 个文件全部 `??`；**零 commit** | **不满足** |
| 关键测试有自动化结果/可复现记录 | E-1…E-17 共 17 条串行实跑 exit 0 | 满足 |
| 未通过项已登记并说明影响 | `06-w12-qa-closeout.md` §12.9（14 条）+ `known-limitations.json`（17 项） | 满足 |

**G-5 结论**：`不满足`（唯一原因是「未提交」）。

### 小结表

| # | 判据（PRD 行号） | 判定 |
|---|---|---|
| G-1 | 双引擎只读诊断稳定（`:157`） | **部分满足**（PG 段 `NOT_EXECUTED`） |
| G-2 | 至少一类受审批的 LAB 修复闭环通过独立验证（`:158`） | **满足** |
| G-3 | 安全负面集/状态恢复/跨租户/备份恢复有报告（`:159`） | **部分满足**（继承 `KD-W11-01/02/03`） |
| G-4 | 交付物完整、限制诚实（`:160`） | **部分满足**（齐全 ✓ / 诚实 ✗ 三处） |
| G-5 | 完成定义：交付物已提交到仓库（`:170`） | **不满足** |

### 与 PRD `:163`/`:164` 的关系（发布档位为何是 `TECH_PREVIEW`）

> `:163`「**否则发布技术预览，不标记完整 Alpha**：`publish_decision == TECH_PREVIEW_ONLY`（W11 P0-W11-06）或上述任一条未满足 → `release_type == TECH_PREVIEW`，限制清单随包发布，`release_name` 不含「Alpha」（AC-16）」。

我的判断：**两个触发条件同时成立** ——
1. W11 真实 `publish_decision()` = `TECH_PREVIEW_ONLY`（我实算，`environment_blocks` 5 项非空）；
2. 本件 §② 有 3 条门禁项「部分满足」+ 1 条完成定义「不满足」。

→ `release_type = TECH_PREVIEW` **不是**任何人的选择，是 PRD 写死的派生结果。我实算 `derive_release_type()` 的公开视图：

```
release_type       = TECH_PREVIEW
release_name       = DBGuardian 技术预览（受限发布）
effective_decision = TECH_PREVIEW_ONLY
decision_digest    = 96ca7de0e185bf1bee6d97740f6ab55e50a207287b0f0b93a63326324a240ecb   ← 2026-11-18 前（修复前）实测
```

> **`decision_digest` 已被 2026-11-18 的 A 修复取代**（见 §⑧）。修复后同一真实入参为
> `d22aff9f4c21f5401dcff62e091ecfb764a770a635b936c8a21877510fb29bd1`；上面 `96ca7de0…` 是**修复前**值，保留作对照。
> 原因：修复前 `_decision_payload()` 把枚举 `str()` 成 `"Decision.TECH_PREVIEW_ONLY"`，该字符串进了摘要；修复后取 `.value`（`"TECH_PREVIEW_ONLY"`），摘要随之变化。**`decision_digest` 无任何冻结件硬编码**（`alpha-acceptance-report.md`/`release-notes.md` 均为 `None`），故不构成兼容性破坏。

并核对 `derive_release_type(*, decision)` 的签名只有 `['decision']` ——**无 `force_alpha` / `override` / 任何 `force_*`**（AC-17 成立）。同时我实测 `release_name` 不含 `alpha`（大小写不敏感校验在 `ReleaseGate.__post_init__` 内）。

---

## ③ 独立取证

### 3.1 `docs/w12/known-limitations.json` 的 17 项与 `items_digest` 只读核对

方法：只读解析 + `core.release.docs.KnownLimitations.model_validate()` + `items_digest()` 独立重算。**未写任何文件、未改任何发布物。**

```
json_sha256         = c0371b7231018867ce93d80a44cff37125babfe6725fa85c6e4be1f9f8c0c52b
json_size_bytes     = 6282
top_level_keys      = [items, items_digest, source_path, version, w11_source_digest, w11_source_status]
items_total         = 17
AUTO_COUNT          = 13
MANUAL_COUNT        = 4
MANUAL_IDS          = [KD-W11-01, KD-W11-02, KD-W11-03, KD-W12-02]
markers_used        = [NOT_SUPPORTED, PARTIAL, UNKNOWN]
file_items_digest   = c395c69152d36aec90ce98111294d849764139e8d7ffbd25d89be4ff0bd34f7f
recomputed_digest   = c395c69152d36aec90ce98111294d849764139e8d7ffbd25d89be4ff0bd34f7f
digest_equal        = True
model_validate      = OK
version             = w12-limitations-v1
w11_source_status   = PASSED
w11_source_digest   = fbc39d0b847e1e6513806cdf4e5861988315ebc9e4049c35f227f3ce238e4ba7
md_json_blocks      = 1
md_block_equal      = True          # known-limitations.md 内嵌块 == .json 文件（逐字相等）
CLOSED_DEVIATIONS   = 1 (KD-W12-01) # core.release.docs 模块常量，不参与 items_digest
AUTO_ITEM_IDS       = 13
NON_PROMISES        = 6
```

**13 个 `AUTO` 项逐条**：`ENV-PG5432`（`NOT_SUPPORTED`）、`ENV-1045`（`NOT_SUPPORTED`）、`ENV-RESIDUE`（`PARTIAL`）、`ENV-NV1`（`UNKNOWN`）、`ENV-MODEL`（`UNKNOWN`）、`DECISION-TECH-PREVIEW`（`PARTIAL`）、`NP-PROD-AUTO-WRITE`（`NOT_SUPPORTED`）、`NP-GENERIC-AUTO-REPAIR`（`NOT_SUPPORTED`）、`NP-ALWAYS-DIAGNOSE`（`NOT_SUPPORTED`）、`NP-FULL-DEADLOCK-INDEX`（`PARTIAL`）、`NP-EXACTLY-ONCE`（`NOT_SUPPORTED`）、`NP-NETWORK-ISOLATION-VERIFIED`（`NOT_SUPPORTED`）、`DOC-PARTIAL`（`PARTIAL`）。

**与前序件的唯一微差**：`06-w12-qa-closeout.md` §12.5 记「`known-limitations.md` 内嵌块长 **4686** 字符」；我的正则捕获为 **4687**（含尾部换行）。**内容相等为 `True`**，非缺陷，仅登记口径差。

### 3.2 发布链路的只读实算（不是重跑工程师命令）

| 取证项 | 方法 | 实测 |
|---|---|---|
| `derive_release_type` 签名 | `inspect.signature` | `['decision']`（keyword-only，无任何 `force_*`） |
| `None` 分支 | 直调 | `TECH_PREVIEW` + `DBGuardian 技术预览（受限发布）` + `DEFAULT_TECH_PREVIEW_ONLY` |
| `TECH_PREVIEW_ONLY`（duck-type str） | 直调 | `TECH_PREVIEW` + `W11_PUBLISH_DECISION` |
| `ALPHA_ALLOWED`（duck-type str） | 直调 | `ALPHA` + `W11_PUBLISH_DECISION` + `registered_issues=()` |
| 6 个异常值（`alpha_allowed`/`FULL_ALPHA`/空串等） | 直调 | 全 `TECH_PREVIEW` + `UNKNOWN_DECISION_VALUE`（fail-closed） |
| **真实 `PublishDecision(decision=Decision.TECH_PREVIEW_ONLY)`** | 直调 | **修复前**：`TECH_PREVIEW` / **`source=DEFAULT_TECH_PREVIEW_ONLY`** / **`issues=('UNKNOWN_DECISION_VALUE',)`** / digest `96ca7de0…`（**我首次出件时误记为 `source=W11_PUBLISH_DECISION` + `issues=()`，属未实测即断言的 QA 自身错误，2026-11-18 已实测纠正，见 §⑧.4**）。**修复后**：`TECH_PREVIEW` / `source=W11_PUBLISH_DECISION` / `issues=()` / digest `d22aff9f…` |
| **真实 `PublishDecision(decision=Decision.ALPHA_ALLOWED)`（三字段全空）** | 直调 | **修复前**：**`TECH_PREVIEW` + `source=DEFAULT_TECH_PREVIEW_ONLY` + `UNKNOWN_DECISION_VALUE`** ← **与 AC-16 的文档映射不一致**（F-W12-4）。**修复后**：`ALPHA` + `source=W11_PUBLISH_DECISION` + `issues=()` |
| `scan_claims(root)` | 直调 | `ok=True`、`violations=()`、`exempted_items=24`、`allowed_phrases=('有条件放行；门禁通过：否','有条件放行','配置层已实测 / 运行态 NOT_VERIFIED','只读数据复核通过（依赖工程师实跑产物）')`（4 条） |
| `REQUIRED_DEPLOY_FILES` | 直调 | 11 项，`missing = []` |
| `openapi.json` | 只读 JSON | `openapi="3.1.0"`、`paths=31` |
| `docs/migrations.md` | 只读 | `has_007=True`、`has_010=False`、`has_011=False`、93 行 → **仍止于 007**，与 R18-01 一致 |

### 3.3 **F-W12-4（本轮新发现，前序件未登记）：枚举互操作缺口**

**现象**：`derive_release_type(decision=<真实 PublishDecision>)` 在 `decision=ALPHA_ALLOWED` 时**不会**派生 `ALPHA`。

**根因（源码级）**：`core/release/publish.py:196-204` 的 `_decision_payload()` 用 `str(getattr(decision, "decision", "") or "")` 取标量。W11 的 `PublishDecision.decision` 类型是 `class Decision(str, Enum)`（`core/assurance/publish.py:76`）。在 **Python 3.11+**，`str, Enum` 混入成员的 `__str__` 来自 `Enum`，返回 `"Decision.ALPHA_ALLOWED"` 而**不是** `"ALPHA_ALLOWED"`：

```
repr(Decision.ALPHA_ALLOWED)      = <Decision.ALPHA_ALLOWED: 'ALPHA_ALLOWED'>
str(Decision.ALPHA_ALLOWED)       = 'Decision.ALPHA_ALLOWED'      <-- 与常量 'ALPHA_ALLOWED' 不等
Decision.ALPHA_ALLOWED.value      = 'ALPHA_ALLOWED'
f'{Decision.ALPHA_ALLOWED}'       = 'Decision.ALPHA_ALLOWED'
python                            = 3.14.7
```

→ `value != DECISION_ALPHA_ALLOWED` 与 `!= DECISION_TECH_PREVIEW_ONLY` 同时成立 → 落入 `core/release/publish.py:259-267` 的 fail-closed 兜底：`release_type=TECH_PREVIEW` + `registered_issues=('UNKNOWN_DECISION_VALUE',)`。

**影响评估（分层，不夸大）**：

| 维度 | 结论 |
|---|---|
| 是否**安全方向** | **是**。任何情况下都不会把 `TECH_PREVIEW_ONLY` 误升为 `ALPHA`；最坏只是把 `ALPHA_ALLOWED` 降级为 `TECH_PREVIEW` |
| 对**当前发布物**的影响 | **无**。当前真实判定是 `TECH_PREVIEW_ONLY`，走 `W11_PUBLISH_DECISION` 分支正确 |
| 对**AC-16 的「`ALPHA_ALLOWED` → `ALPHA`」** | **在真实 W11 值对象上不可达**；只有传"decision 字段是裸 `str` 的 duck-type"时才可达 |
| 为什么**测试没发现** | `tests/unit/test_w12_publish_gate.py:54-60` 的替身 `_Decision`（`class _Decision:` 在 `:55`，字段 `decision: str` 在 `:58`）把 `decision` 声明为 `str` 并直接传 `"ALPHA_ALLOWED"` → 绕过枚举 `__str__` 分歧。**这是测试代码的替身保真度不足（QA 侧缺口），不是源码 bug 的充分证明** |
| 生产调用点 | `grep -rn "derive_release_type("` 全仓只命中 `tests/unit/test_w12_publish_gate.py`（5 处）+ `core/release/publish.py:553` 的文档字符串。**无生产调用点**；`write_release_documents()` 亦无仓库内调用者（发布件由仓库外脚本生成） |
| 定性 | **潜在缺陷 + 测试替身失真**。方向安全，当前无实质影响；一旦 W11 真的返回 `ALPHA_ALLOWED`，W12 会静默降级并在发布件里登记一条**不真实**的 `UNKNOWN_DECISION_VALUE` |

> 按分工：**只登记、不回派、不自修**。作为放行条件 C-W12-3 登记。

### 3.4 我自己跑的数字

纪律同前：`.venv/Scripts/python.exe`、`-o addopts="" -q -p no:cacheprovider`、**全程串行零并发**、需 DB 加外层 `timeout 180`、**不用 `--timeout=`**、仓库外全新 `--basetemp`。

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| E-1 | `pytest tests/unit`（全量） | `1803 passed in 24.87s` | 0 |
| E-2 | `pytest` W12 七件（`test_w12_claims.py` + `test_w12_doc_inventory.py` + `test_w12_publish_gate.py` + `test_w12_demo_script.py` + `test_w12_deploy_package.py` + `test_w12_functional_acceptance.py` + `test_w12_security_acceptance.py`） | `120 passed in 38.13s` | 0 |
| E-8 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py`（AC-22 既存） | `11 passed in 0.18s` | 0 |
| E-12 | `timeout 180 pytest tests/integration/test_w09_redaction_pipeline.py`（AC-09 既存） | `4 passed in 2.58s` | 0 |
| E-16 | `timeout 180 pytest tests/integration/test_w11_security_negative.py`（AC-05 上游） | `32 passed in 2.59s` | 0 |
| E-17 | `timeout 180 pytest tests/integration/test_w11_fault_injection.py`（AC-08 上游） | `16 passed in 3.86s` | 0 |

**W12 七件拆解（E-2 的 120 = 25 + 17 + 23 + 11 + 12 + 14 + 18）**，与前序件 `06-w12-qa-closeout.md:71,547` 的口径**逐项一致**。

**写入后复核（重要，防止评审件本身污染被测对象）**：4 份评审件全部落盘**之后**，我又复跑了两条命令，确认新增的 `docs/w09|w10|w11|w12/*-gate-review.md` **不破坏**任何既有校验：

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| E-2' | `pytest` W12 七件（写入后） | `120 passed in 40.45s` | 0 |
| E-1' | `pytest tests/unit`（写入后） | `1803 passed in 33.36s` | 0 |

并复核扫描器口径：`STRICT_SCAN_FILES` 仍是 **11 件**（`deployment-guide.md`/`demo-script.md`/`known-limitations.json`/`known-limitations.md`/`api-contract.md`/`security-materials.md`/`benchmark-baseline.md`/`alpha-acceptance-report.md`/`release-notes.md`/`claim-scan-report.md`/`openapi.json`），**`07-w12-gate-review.md` 不在其中**，`scan_claims()` 仍 `ok=True` / `violations=()` / `exempted=24`。→ 本件不影响 T06 表述扫描，也不改变文档清单校验结果（W-8 的口径风险仍成立）。

### 3.5 与前序 QA 收口件的数字对账

| 项 | `06-w12-qa-closeout.md` | 本轮实测 | 是否一致 | 说明 |
|---|---|---|---|---|
| W12 七件合计 | `120 passed`（`:24`、`:71`、`:547`） | **E-2 `120 passed`** | **一致** | 两次独立运行同数 |
| `tests/unit` 全量 | R2 `1801 passed`（`:24`、`:543`） | 1803 | 数字不同（+2） | 差额 **恰为 KD-W12-02 的 2 条回归用例**（`test_diagnosis_analyzers.py` +74 行）；**无回归** |
| AC-19 扫描 | `ok=True`、`violations=0`、`exempted_lines=24`、`allowed_phrases=4`（§12.6） | **我只读实算逐项一致** | **一致** | 双向独立取证 |
| 限制清单 17 项 | `17 = 13 AUTO + 4 MANUAL`（§12.5） | **我只读实算一致** | **一致** | 含 `items_digest` 重算 |
| `CLOSED_DEVIATIONS` | 1 项 `KD-W12-01`（§12.5） | **我实读模块常量 = 1 项** | **一致** | — |
| `REQUIRED_DEPLOY_FILES` | 11 项（§4 AC-10） | **11 项，missing = []** | **一致** | — |
| OpenAPI | 3.1.0 / 31 paths（§4 AC-13） | **3.1.0 / 31** | **一致** | — |
| 部署必需文件 / `.env.example` | 口令全 `CHANGE_ME_*` | 前序件结论；本轮只核对三态注释 | 未复核口令项 | 非本件判据 |
| W12 新文件计数 | 实测 27（`:41` `D-01`） | 未重数 | 未复核 | 非判据 |

**两条**与前序件**不一致**的实质发现（我新增、前序件未登记）：

| 编号 | 前序件的结论 | 我的实测 | 差异性质 |
|---|---|---|---|
| **D-1** | §5.2 G2 表：`"ALPHA_ALLOWED"` → `ALPHA` + `DBGuardian Alpha`（记为 AC-16/AC-17 已证） | 用**真实** `PublishDecision(decision=Decision.ALPHA_ALLOWED)` 调用 → `TECH_PREVIEW` + `UNKNOWN_DECISION_VALUE` | **前序件的 G2 用的是裸 `str` 替身**，未覆盖真实枚举对象；结论在真实对象上不成立（F-W12-4） |
| **D-2** | §12.6「对外件是否仍有"当前 = FAILED"表述 → **无**」 | `docs/w12/alpha-acceptance-report.md:29` 仍有 `affected_rows=UNKNOWN、snapshot_comparable=FAIL`（AC-03 修复**前**的断言态），且与同文件 AC-03 PASS 矛盾 | **前序件的 grep 维度漏了这一行**（它查的是「FAILED」字符串，而这行写的是 `FAIL`）；属"诚实性"缺口的漏检 |

### 3.6 取证方法学（如实披露）

| 编号 | 现象 | 根因 | 处置 |
|---|---|---|---|
| **M-1** | `tests/unit` 首轮 `1 failed, 1802 passed`，唯一红 `test_w11_rc_manifest.py::test_git_rev_parse_returns_none_outside_repo` | 我首轮把 `--basetemp` 设在**仓库内** `D:/DBGuaredian/.qa_tmp_base`；该用例要求 tmp_path 在**非 git 仓库**目录下 | **我的取证副作用**，非实现缺陷。改仓库外 basetemp 后 `1803 passed`。临时目录已删除，`git status --short` 复归 121 |
| **M-2** | W12 七件首轮 `106 passed, 14 errors` | 复用了**已存在**的 `--basetemp` 目录（tmp_path 冲突），非用例缺陷 | 改**每次全新** basetemp 后 `120 passed` / 0 error（E-2） |
| **M-3** | 我最初用 `from core.assurance.rc import build_soak_report` 报 `ImportError` | 函数实际在 `core.assurance.soak` | 属**我自己的脚本错误**（QA-self），已修正，不影响任何结论 |

---

## ④ 未闭合项

> 标记口径：`NOT_SUPPORTED` / `PARTIAL` / `UNKNOWN`。环境阻塞**不作为放行条件**。

### 4.1 `docs/w12/known-limitations.json` 17 项逐条落点（只读引用，未改）

| # | `item_id` | marker | 影响哪条门禁判据 | 我的判定 |
|---|---|---|---|---|
| 1 | `ENV-PG5432` | `NOT_SUPPORTED` | **G-1**（AC-04 PG 段 `NOT_EXECUTED`）、AC-11、AC-12 演示段 | `NOT_SUPPORTED`，不判 FAIL |
| 2 | `ENV-1045` | `NOT_SUPPORTED` | AC-11 的 `upgrade` 检查 `NOT_EXECUTED` | `NOT_SUPPORTED`，不判 FAIL |
| 3 | `ENV-RESIDUE` | `PARTIAL` | 所有计数须按 `task_id`/`run_id` 过滤 | `PARTIAL`（未清理、增长中） |
| 4 | `ENV-NV1` | `UNKNOWN` | AC-11 的 `image_digest=UNKNOWN`、`container_runtime_status=NOT_VERIFIED` | `UNKNOWN` |
| 5 | `ENV-MODEL` | `UNKNOWN` | AC-14 的 `baseline_score=None`、模型指标未测得 | `UNKNOWN` |
| 6 | `DECISION-TECH-PREVIEW` | `PARTIAL` | 决定 `release_type=TECH_PREVIEW` | `PARTIAL`（即本件结论的派生源） |
| 7 | `NP-PROD-AUTO-WRITE` | `NOT_SUPPORTED` | AC-15 不承诺能力清单 | `NOT_SUPPORTED` |
| 8 | `NP-GENERIC-AUTO-REPAIR` | `NOT_SUPPORTED` | AC-15 | `NOT_SUPPORTED` |
| 9 | `NP-ALWAYS-DIAGNOSE` | `NOT_SUPPORTED` | AC-15 | `NOT_SUPPORTED` |
| 10 | `NP-FULL-DEADLOCK-INDEX` | `PARTIAL` | AC-15 | `PARTIAL` |
| 11 | `NP-EXACTLY-ONCE` | `NOT_SUPPORTED` | AC-15 | `NOT_SUPPORTED` |
| 12 | `NP-NETWORK-ISOLATION-VERIFIED` | `NOT_SUPPORTED` | AC-15、W07 L8 表述禁令 | `NOT_SUPPORTED` |
| 13 | `DOC-PARTIAL` | `PARTIAL` | AC-13 文档清单（`docs/migrations.md` 止于 007；W07 四件套原缺 2 件） | `PARTIAL`（**注意**：`docs/w07/05-freeze.md`、`06-conformance-audit.md` 已于 2026-11-17 23:13 落盘但**未跟踪**；`docs/migrations.md` 按 R18-01 保持止于 007） |
| 14 | `KD-W11-01` | `PARTIAL` | **G-3**（备份桶命名） | `PARTIAL`，未关闭 |
| 15 | `KD-W11-02` | `PARTIAL` | **G-3**（漏 `approval_consumptions` / `step_prepared`） | `PARTIAL`，未关闭 |
| 16 | `KD-W11-03` | `PARTIAL` | **G-3**（G1–G3 哨兵空真） | `PARTIAL`，未关闭 |
| 17 | `KD-W12-02` | `UNKNOWN` | AC-02 输入形状 | **`UNKNOWN`（且已过期：代码 2026-11-18 已修，发布物未同步 → F-W12-3）** |

### 4.2 W12 自身遗留（逐条标标记）

| 编号 | 内容 | 标记 | 来源 |
|---|---|---|---|
| W-1 | **交付物未提交**（完成定义未满足） | `PARTIAL` | 本件 §② G-5 |
| W-2 | 双引擎只读诊断的 PG 段未执行 | `NOT_SUPPORTED`（环境，不判 FAIL） | 本件 §② G-1 |
| W-3 | 备份恢复口径三项偏离（`KD-W11-01/02/03`） | `PARTIAL` | 本件 §② G-3 |
| W-4 | AC-11 五项部署检查全 `NOT_EXECUTED`（安装/启动/停止/升级/回滚） | `NOT_SUPPORTED`（环境 + `本轮不执行 rollback`） | `06-…md` §4 AC-11 |
| W-5 | AC-12 演示四段全 `NOT_EXECUTED`（演示未实跑） | `NOT_SUPPORTED` | `06-…md` §4 AC-12 |
| W-6 | `R-W12-QA-01`：严格件内的 ``` 围栏 / 行内 code span 被剥离 → 可藏正向宣称且不留痕 | `PARTIAL`（待主理人裁定；前序件已提请） | `06-…md` §6 |
| W-7 | `R-W12-QA-02`：`ReleaseGate` 为 frozen dataclass，`object.__setattr__` 可语言级写穿 | `NOT_SUPPORTED`（正常路径不可达） | `06-…md` §6 |
| W-8 | `R-W12-QA-03`：本类评审件不在 T06 严格扫描白名单内（引述禁令原文时必然命中） | `PARTIAL`（口径问题） | `06-…md` §6 |
| W-9 | **F-W12-1**：发布件的发布结论来自 `decision=None` 默认分支（登记 `PUBLISH_DECISION_MISSING`），未消费 W11 真实判定 | `PARTIAL` | 本件 §② G-4(b) |
| W-10 | **F-W12-2**：`alpha-acceptance-report.md:29` 留 AC-03 修复前的断言态，与同文件 AC-03 PASS 矛盾 | `PARTIAL` | 本件 §② G-4(b) |
| W-11 | **F-W12-3**：`KD-W12-02` 代码已修、发布物仍标未修（R18-02 明令不改） | `PARTIAL` | 本件 §② G-4(b) |
| W-12 | **F-W12-4**：`derive_release_type` 对真实 `str, Enum` 的 `ALPHA_ALLOWED` 无法派生 `ALPHA`（fail-closed 到 `UNKNOWN_DECISION_VALUE`）；单测替身用裸 `str` 未覆盖 | `PARTIAL`（方向安全、当前无实质影响） | 本件 §3.3 |
| W-13 | `CONTENT_DEPTH_LIMIT`：`_frozen_evidence_available` 只校验 14 项长度、不校验元素 | `PARTIAL` | `05-w11-qa-closeout.md` §11.1 |
| W-14 | `IN_PROCESS_BYPASS`：`object.__setattr__` 可改写 frozen `PublishDecision` | `NOT_SUPPORTED`（威胁模型外） | `05-w11-qa-closeout.md` §11.2 |
| W-15 | 长稳未实跑（`SoakReport.status=PLANNED`） | `NOT_SUPPORTED` | 本件 §②；`05-…md` §11.4 |
| W-16 | `ENV-PY314-AST-CRASH`：`tests/unit` 历史上多次 `exit 139`（本轮 0 次复现） | `NOT_SUPPORTED`（环境层） | `06-…md` §2；`05-…md` §11.5 |

---

## ⑤ 放行条件（每条可判定、可关闭）

> **条件数：7 → 6**（`C-W12-3` 已于 2026-11-18 关闭，见 §⑧）。
> **门禁结论仍为「`有条件放行`｜门禁通过：否」，不因本轮修复改为「通过」**：`C-W12-1`（未提交）、`C-W12-4`（`KD-W12-02` 发布物未同步）、`C-W12-5`（发布结论仍走 `decision=None` 默认分支）、`C-W12-2`/`C-W12-6`/`C-W12-7` 全部仍未关闭。

| # | 条件 | 关闭判据（可机器核对） | 责任 |
|---|---|---|---|
| **C-W12-1** | 提交一次 commit，把 W12 交付物（`core/release/**`、`docs/w12/**`、`tests/**/test_w12_*`、`deploy/**`、`scripts/**`）入库，并记录 `commit` / 镜像 digest / 制品 hash | `git rev-parse HEAD != 2850eca…` 且 `git status --short` 对上述路径为空；`rc_manifest.json` 的 `commit` 与新 `HEAD` 相等 | 主理人 / 工程师 |
| **C-W12-2** | PG 段解阻后补跑（由主理人拍板 `ENV-PG5432`）：AC-04 的 PG 只读段 + AC-11 的五项部署检查 + AC-12 的演示四段 | 对应字段不再为 `NOT_EXECUTED`；`container_runtime_status != "NOT_VERIFIED"` | 主理人 / 工程师 |
| **C-W12-3** | **已关闭（2026-11-18）** — 原登记：**F-W12-4 修复**：`_decision_payload()` 对 `str, Enum` 取 `.value`（或 `derive_release_type` 接受 `Decision` 并显式取 `.value`）；并把 `test_w12_publish_gate.py` 的替身换成**真实** `PublishDecision` | 关闭证据（我 2026-11-18 独立复核，全部实测）：`core/release/publish.py` 新增 `_text_value()`（`Enum` → `.value`，`None` → 空串，其它 `str()`），`_decision_payload()` 改走它；真实 `PublishDecision(decision=Decision.ALPHA_ALLOWED, 三字段全空)` → **`release_type == ALPHA`**、`source == "W11_PUBLISH_DECISION"`、`issues == ()`；`Decision.TECH_PREVIEW_ONLY` → `TECH_PREVIEW`、`source == "W11_PUBLISH_DECISION"`、**`issues == ()`**；`test_w12_publish_gate.py` **29 passed**（+6 条真实枚举用例）。**反证已做**：把 `_text_value` 在内存还原为裸 `str()` 后同一入参立刻回落 `TECH_PREVIEW` + `UNKNOWN_DECISION_VALUE` → 证明测的是修复点而非恒真断言。详见 §⑧ | 工程师 |
| **C-W12-4** | **F-W12-3 同步**：由**生成器**（不是手工）重出 `known-limitations.{json,md}`，把 `KD-W12-02` 移入 `CLOSED_DEVIATIONS` 并重算 `items_digest` | 新 `known-limitations.json` 的 `items` 中不含 `KD-W12-02`、`CLOSED_DEVIATIONS` 含之；`items_digest` 与重算值相等；`known-limitations.md` 内嵌块与 `.json` 相等 | 工程师（经生成器） |
| **C-W12-5** | **F-W12-1/F-W12-2 修复**：用 W11 **真实** `publish_decision()` 输出重出 `alpha-acceptance-report.md`（`source` 应为 `W11_PUBLISH_DECISION`、`decision_digest` 非空、登记项不含 `PUBLISH_DECISION_MISSING`）；并清掉 `:29` 的 AC-03 修复前断言态 | 报告中 `source == "W11_PUBLISH_DECISION"`；`grep "affected_rows=UNKNOWN" docs/w12/alpha-acceptance-report.md` **零命中**；报告内 AC-03 段落前后一致。**2026-11-18 复核：仅达成 1/3**（`grep` 已零命中、AC-03 段落已一致，见 §⑧.3）→ **条件未关闭**：`source` 仍 `DEFAULT_TECH_PREVIEW_ONLY`、`decision_digest` 仍 `None`、登记项仍 `PUBLISH_DECISION_MISSING` | 工程师（经生成器） |
| **C-W12-6** | `KD-W11-01/02/03` 关闭（与 `docs/w11/06-w11-gate-review.md` 的 C-W11-2/C-W11-3 同源） | 备份桶覆盖 5 张表、四类计数非空；G1–G3 去哨兵空真 | 工程师 |
| **C-W12-7** | `R-W12-QA-01` 裁定（围栏/行内代码剥离是否构成第三条绕过路径） | 裁定台账新增一条；或扫描器改为逐条留痕的显式豁免 | 主理人 |

> `ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL` 本身**不列入**放行条件（PRD `:162`/`:168`）。C-W12-2 是「解阻后补跑」的后续迭代条件。
> **本件不宣布发布**：`release_type = TECH_PREVIEW` 是 PRD `:163` 的派生结果，签署与对外发布由主理人决定。

---

## ⑥ 口径自查（禁用表述）

| 禁用表述 | 本件是否出现 | 说明 |
|---|---|---|
| 「Alpha 通过 / Alpha 就绪 / 已达成完整 Alpha」 | **否** | 全件唯一档位表述是 `TECH_PREVIEW`；结论只用「`有条件放行`｜门禁通过：否」 |
| 「W12 门禁通过 / 已收口」 | **否** | 结论为 `有条件放行`，附 7 条未关闭条件 |
| 「容器网络隔离已验证」 | **否** | 只写 `container_runtime_status="NOT_VERIFIED"`、`image_digest="UNKNOWN"`、`NP-NETWORK-ISOLATION-VERIFIED` 为 `NOT_SUPPORTED` |
| 「环境已清理 / 已可复现清零」 | **否** | `ENV-RESIDUE` 只追加不清理 |
| 「QA 独立实跑通过」 | **否** | 逐条给原文汇总行 + exit code；并披露 M-1/M-2/M-3 三条我自己的取证副作用 |
| 「回归全绿」 | **否** | 只给原文汇总行；并登记 `ENV-PY314-AST-CRASH` 历史（W-16） |
| 「演示等于验收」 | **否** | AC-12 演示四段一律写 `NOT_EXECUTED`，不写"通过" |
| 「W07 / W08 / W09 / W10 / W11 已通过」 | **否** | 分别引「有条件放行；门禁通过：否」/「有条件放行」/「有条件放行｜门禁通过：否」/「有条件放行｜门禁通过：否」/「有条件放行｜门禁通过：否」 |
| 「训练已达标 / 模型已升级」 | **否** | `model="baseline"`、`sft_reproducible_gain=False`、`dpo/14b/rl` 恒 `True`（`ClassVar`，不在 `model_fields`） |

> 注：本件**必然**引述部分禁用短语原文（用于说明禁令与证伪负测）。这与 `docs/w12/06-w12-qa-closeout.md:§6 R-W12-QA-03` 是同一类情形：**若将来把评审件纳入 T06 严格扫描集，须同时裁定豁免口径**，否则会与"禁止引述禁令原文"自相矛盾。本件**不在** `STRICT_SCAN_FILES` 的 11 件内，`scan_claims()` 现状仍 `ok=True` / `violations=()`（我实算复核）。

---

## ⑦ 读写范围自检

| 项 | 状态 |
|---|---|
| 新建文件 | **仅**本文件 `docs/w12/07-w12-gate-review.md`（首版 2026-11-18 早；同日复核轮追加 §⑧ 并就地更新 C-W12-3 / C-W12-5 / §3.2 / §⑤ 条件数，**仍是同一个文件**） |
| 修改 `.py` / SQL / 配置 / 既有 docs | **0**（`docs/w12/` 既有 17 件全部未触碰；**`known-limitations.json` / `known-limitations.md` / `openapi.json` 未触碰**，未重算任何 digest；`docs/migrations.md` 未补 008–011，遵守 R18-01） |
| 手工 `INSERT/UPDATE/DELETE/TRUNCATE/ALTER` / rollback | **0**（未碰 `.env` / `DG_ADMIN_DSN` / `dg_ctl`） |
| 新增测试用例 | **0**（本任务只评审，不补测） |
| `git commit / add / checkout / reset` | **0** |
| 无路径全量 pytest / `--timeout=` | **0** |
| 临时产物 | 全部在 `%LOCALAPPDATA%/Temp/`（仓库外）；我一度在仓库内建的 `.qa_tmp_base` 与 `runs/.qa_unit_full.txt` 已删除，`git status --short` 复归 121 |

---

## ⑧ 2026-11-18 修复复核（A / B / C 三项独立验证）

> 复核性质：**独立验证，不采信工程师自报**。全部数字我自己串行实跑，全部断言我自己构造。
> 复核边界：**未改任何生产代码、未改任何测试、未改 `known-limitations.*`、未改 `docs/migrations.md`、未改其它三份门禁件**（`15-w09` / `05-w10` / `06-w11`）。
> 现场：`HEAD` 仍 `2850eca…`；`git diff --name-only` = 25、`git status --short` = 125（25 `M` + 100 `??`，含本批 4 份评审件）；**零 commit**。

### 8.1 逐项判定

| 项 | 内容 | 判定 | 关键证据 |
|---|---|---|---|
| **A** | `core/release/publish.py:196-233` 新增 `_text_value()`、`_decision_payload()` 改走它；`derive_release_type` 本体未改；`test_w12_publish_gate.py` 追加真实 `Decision` 用例 | **PASS** | 见 8.2 |
| **B** | `docs/w12/alpha-acceptance-report.md:29` 由修复前断言态改为修复后实测值，并保留「原登记（修复前 2026-11-16）…已关闭」审计链；发布结论字段一字未改 | **PASS**（仅 C-W12-5 的 1/3 判据） | 见 8.3 |
| **C** | `docs/w09/12-w09-qa-closeout.md:344`/`:346` 追加「2026-11-18 复核：已关闭 + 证据」，原结论保留 | **PASS** | 见 8.4 |

### 8.2 A 的独立验证（含反证）

**A-1 真实枚举 → ALPHA**（我自己构造，非跑工程师用例）：

```
PublishDecision(decision=Decision.ALPHA_ALLOWED, failed_test_ids=(), red_line_failures=(),
                environment_blocks=(), reason="QA 独立复核", inputs_digest="0"*64)
→ release_type       = ReleaseType.ALPHA | "ALPHA"
  release_name       = "DBGuardian Alpha"        # 含 "Alpha" 是 W12-Q4 规定文案，允许
  effective_decision = ALPHA_ALLOWED
  source             = W11_PUBLISH_DECISION
  registered_issues  = ()
  decision_digest    = 0c9eec96…（64 位）        CHECK_ALPHA = True
```

**A-2 真实枚举 → TECH_PREVIEW 且名称不含 alpha**：

```
PublishDecision(decision=Decision.TECH_PREVIEW_ONLY, environment_blocks=("ENV-PG5432",))
→ release_type       = TECH_PREVIEW
  release_name       = "DBGuardian 技术预览（受限发布）"   "alpha" in casefold() == False
  effective_decision = TECH_PREVIEW_ONLY
  source             = W11_PUBLISH_DECISION
  registered_issues  = ()                        CHECK_TPO = True
```

**A-3 未知值 fail-closed（不抛异常、结果确定）** — 7 个探针全部同向：

| 入参 | 结果 | `source` | `issues` | 两次调用逐位相等 |
|---|---|---|---|---|
| `"SOMETHING_ELSE"` | `TECH_PREVIEW` | `DEFAULT_TECH_PREVIEW_ONLY` | `UNKNOWN_DECISION_VALUE` | True |
| `123` | `TECH_PREVIEW` | `DEFAULT_TECH_PREVIEW_ONLY` | `UNKNOWN_DECISION_VALUE` | True |
| `object()` | `TECH_PREVIEW` | `DEFAULT_TECH_PREVIEW_ONLY` | `UNKNOWN_DECISION_VALUE` | True |
| `"alpha_allowed"` / `"FULL_ALPHA"` / `""` / `NoneType` 类 | `TECH_PREVIEW` | `DEFAULT_TECH_PREVIEW_ONLY` | `UNKNOWN_DECISION_VALUE` | True |

`object()` 两次产生**相同 digest** → 对象地址未进摘要（`_decision_payload` 的非 `str`/`Enum` 兜底归空串生效），纯函数确定性成立。

**A-4 反证（关键：证明测的是修复点，不是恒真断言）**

在**内存里**把 `RP._text_value` 还原为旧语义 `lambda v: "" if v is None else str(v)`（不落盘、不改文件），对**同一真实入参**再算：

```
退化后：release_type = TECH_PREVIEW
        source       = DEFAULT_TECH_PREVIEW_ONLY
        issues       = ('UNKNOWN_DECISION_VALUE',)
        digest       = 96ca7de0…        ← 与本件首版记录的修复前值逐字相同
COUNTER_PROOF = True
还原后 _text_value is orig = True；release_type 回到 ALPHA，与首次逐位相等 = True
```

→ 结论翻转由 `_text_value` **单独**导致；且退化值与我 2026-11-18 之前实测的 `96ca7de0…` **逐字相同**，双向印证归因正确。

**A-5 回归面（我自己核对）**

| 检查 | 实测 |
|---|---|
| `inspect.signature(derive_release_type)` | `['decision']`，无 `*args`/`**kwargs`（AC-17 仍成立） |
| `ReleaseGate` 字段 | `['release_type','release_name','effective_decision','decision_digest','registered_issues','source']` —— **6 个，无时间字段**（与首版记录逐字相同） |
| 同入参两次 `to_public_dict()` | 逐位相等；digest 相等 |
| 裸 `str` 替身 `_Decision("ALPHA_ALLOWED"/"TECH_PREVIEW_ONLY")` | 仍绿，且与真实枚举 `==`、`to_public_dict()` 相等、`decision_digest` 相等（向后兼容成立） |
| 真实 `publish_decision()`（真实代码路径，5 个环境阻塞） | `TECH_PREVIEW` / `source=W11_PUBLISH_DECISION` / `issues=()` / digest `d22aff9f…`，两次一致 |
| 有无冻结件硬编码 `decision_digest` | **无**。`alpha-acceptance-report.md:14` 与 `release-notes.md:11` 均为 `None`；无 JSON 冻结该值 → 摘要变化**不构成兼容性破坏** |
| 生产调用点 | `grep -rn "derive_release_type" core/ apps/ workers/ scripts/ benchmarks/`（排除定义处）→ **0 命中**，仍无生产调用者，故 A 的改动不影响任何运行路径 |

**A-6 新观察项 `O-W12-1`（只登记，不判 FAIL、不回派）**

`_decision_payload()` 现在把 `raw = getattr(decision, "decision", decision)` —— 即**没有 `.decision` 属性的裸标量字符串**也被当作决定值。实测行为变化：

```
修复前：derive_release_type(decision="ALPHA_ALLOWED")      → TECH_PREVIEW + UNKNOWN_DECISION_VALUE（fail-closed）
修复后：derive_release_type(decision="ALPHA_ALLOWED")      → ALPHA       + W11_PUBLISH_DECISION
```

- **方向**：这是**唯一**朝"更容易出 ALPHA"方向变化的一处；其余全部同向或更严。
- **风险评估（低）**：① 进程内调用者可构造 duck-type 本来就可达（W11 裁定已把 `IN_PROCESS_BYPASS` 划出威胁模型）；② 无生产调用点；③ `derive_release_type` 的威胁模型是「防人工在**发布流程配置/环境变量**里覆盖」，不是「防同进程代码」。
- **但确实是 fail-closed 面的收窄**：调用方把常量 `DECISION_ALPHA_ALLOWED`（字符串）误传进来时，以前会 fail-closed 并被 `UNKNOWN_DECISION_VALUE` 记录，现在会静默产出 `ALPHA`。
- **建议（不自行实施）**：若要收回，可要求入参必须 `hasattr(decision, "decision")`，裸标量一律归未知。**请主理人裁定是否单独立卡**。本项**不影响** C-W12-3 的关闭判定（C-W12-3 的关闭判据是"真实 Enum 两分支正确"，已达成）。

### 8.3 B 的独立验证

| 检查 | 我的实测 | 与记录是否一致 |
|---|---|---|
| `grep -c "affected_rows=UNKNOWN"` | **0**（`grep` 退出码 1） | 与工程师自报一致 |
| `:29` 现状 | 改为「（KD-W12-01 修复后，2026-11-17 实测）：`affected_rows=PASS（observed=1）`、`snapshot_comparable=PASS`、`target_note_final=PASS`，`status=PASSED / verdict=PASS / reason_code=OK`」，随后保留「**原登记（修复前，2026-11-16）**：… 曾为 `UNKNOWN`（`OBSERVED_MISSING`）、`snapshot_comparable=FAIL` …；该修复前断言态已于 2026-11-17 关闭」 | 审计链保留、原结论未删 |
| 与同文件「功能验收」段一致性 | 「功能验收」段 AC-03 写 `独立验证结论 PASS、闭环状态 PASSED（原因码 OK）`；「可靠性」段现为 `全 PASS` → **前后一致**（F-W12-2 已消除） | 一致 |
| `scan_claims(root)` | `ok=True`、`violations=()`、`exempted_lines=24`、`exempted_items=24`、`allowed_phrases=4`、`demo_input_violations=()`、`limitation_integrity_ok=True`、`evidence_refs_ok=True`、严格件 11 | **与 `docs/w12/claim-scan-report.md:12-17` 逐项一致**（`violations 0` / `demo 0` / `limitation True` / `evidence True` / `exempted 24` / `ok True`）；**与 `06-w12-qa-closeout.md:484` 一致**（`ok=True`、`violations=()`、`exempted_lines=24`） |
| 三范围 | 严格 **11**、`spec_mode` **2**（`02-design.md`、`03-task-cards.md`）、排除 **1**（`01-prd.md`） | **与 `06-w12-qa-closeout.md:485` 一致** |
| 发布结论字段（一字未改核对） | `release_type = TECH_PREVIEW`｜`release_name = DBGuardian 技术预览（受限发布）`｜`effective_decision = TECH_PREVIEW_ONLY`｜`source = DEFAULT_TECH_PREVIEW_ONLY`｜`decision_digest = None`｜登记项 `PUBLISH_DECISION_MISSING` | **与我 2026-11-18 首版记录逐字相同，未被改动** |
| 其它 strict 件是否被连带改动 | `test_w12_claims.py` **25 passed**、`test_w12_doc_inventory.py` **17 passed**（与首版同数） | 无连带影响 |

> `snapshot_comparable=FAIL` 字面仍出现在 `:29`，但**仅在「原登记（修复前，2026-11-16）」从句内**且紧随「已关闭」表述——属主理人要求的审计链保留，**不是**当前结论。
> **C-W12-5 维持未关闭**：`source` 仍为 `DEFAULT_TECH_PREVIEW_ONLY`、`decision_digest` 仍 `None`、登记项仍 `PUBLISH_DECISION_MISSING` → **F-W12-1 未修复**（发布件仍未消费 W11 真实判定）。

### 8.4 C 的独立验证

| 检查 | 我的实测 |
|---|---|
| `:344`（第 8 条） | 保留「**原登记**：…`apps/`、`workers/` 无任何引用…未接入 API/Worker 生产装配链路；AC-12 的「生产装配链路」未验证」，随后追加「**2026-11-18 复核：已关闭**」+ 证据。**原结论一字未删** |
| `:346`（第 10 条） | 保留「**原登记**：本轮不补，按已知偏离登记，关闭条件挂 W11」，随后追加「**2026-11-18 复核：已关闭**」+ 证据。**原结论一字未删** |
| `workers/report_assembly.py:35` | `from core.report.builder import ReportBuilder, StepChain` —— **行号准确** |
| `workers/report_assembly.py:193` | `builder = ReportBuilder(` —— **行号准确**（实际装配点） |
| `workers/stack.py:54` | `from workers.report_assembly import TaskReportPublisher, build_report_publisher` —— **行号准确** |
| `workers/stack.py:307` | `report_publisher = build_report_publisher(` —— **行号准确** |
| `workers/stack.py:339` | `report_publisher=report_publisher,`（挂入 `WorkerStack`） —— **行号准确** |
| `docs/w07/05-freeze.md` | 存在、可读、**16 556 字节**（与登记数字逐字相同） |
| `docs/w07/06-conformance-audit.md` | 存在、可读、**18 824 字节**（与登记数字逐字相同） |
| 两者 git 状态 | 均 `??`（未跟踪）—— 与登记「未跟踪、未提交」一致 |

→ C 的 7 处代码行号 + 2 个文件字节数**全部逐项命中**，无虚构。

### 8.5 QA 自身错误的纠正（必须披露）

我在 **2026-11-18 首版**本件 §3.2 写过一行：

> 「真实 `PublishDecision(decision=Decision.TECH_PREVIEW_ONLY)` → `TECH_PREVIEW` / **`source=W11_PUBLISH_DECISION`** / `decision_digest` 非空 → **符合预期**」

**该断言当时未实测 `source` 字段**（我的探针只打印了 `release_type`/`release_name`/`effective_decision`/`decision_digest`）。修复前实测为 **`source=DEFAULT_TECH_PREVIEW_ONLY` + `issues=('UNKNOWN_DECISION_VALUE',)`** —— 也就是说，**修复前连 `TECH_PREVIEW_ONLY` 这条路径的 provenance 也是错的**（只是 `release_type`/`effective_decision` 恰好与正确结果同值，掩盖了问题）。

- **定性**：**我的取证缺陷（QA-self）**，不是工程师实现问题。
- **处置**：已在 §3.2 就地更正为「修复前 / 修复后」双值；并在 §8.2 A-4 用内存退化复现出 `96ca7de0…` 与我首版记录的 digest 逐字相同，交叉印证。
- **对结论的影响**：**使 F-W12-4 更严重而非更轻**（原以为只影响 ALPHA 分支，实测连 TECH_PREVIEW_ONLY 的 `source`/`issues` 也错）。但方向始终是 fail-closed，且 `release_type` 未出错，故不影响首版门禁结论。

### 8.6 串行实跑（本轮，不使用 `--basetemp`、不使用 `--timeout=`）

| # | 命令 | 原文汇总行 | exit |
|---|---|---|---|
| M1 | `pytest tests/unit`（全量） | `1809 passed in 16.32s` | 0 |
| M2 | `pytest` W12 七件 | `126 passed in 39.95s` | 0 |
| M3 | `pytest tests/unit/test_w12_publish_gate.py` | `29 passed in 0.14s` | 0 |
| M4 | `pytest tests/unit/test_w12_claims.py` | `25 passed in 0.57s` | 0 |
| M5 | `pytest tests/unit/test_w12_doc_inventory.py` | `17 passed in 0.87s` | 0 |
| I1 | `pytest tests/unit/test_worker_layering.py` | `10 passed in 0.09s` | 0 |
| I2 | `pytest tests/unit/test_worker_isolation.py` | `2 passed in 0.08s` | 0 |
| I3 | `pytest tests/unit/test_worker_runner.py` | `38 passed in 0.35s` | 0 |
| I4 | `pytest tests/unit/test_worker_scheduler.py` | `35 passed in 0.33s` | 0 |
| I5 | `pytest tests/unit/test_w09_sweep_scanned.py` | `10 passed in 0.07s` | 0 |
| I6 | `pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py` | `11 passed in 0.20s` | 0 |
| I7 | `pytest tests/unit/test_verification_perf.py` | `10 passed in 0.13s` | 0 |
| I8 | `pytest tests/unit/test_baseline_manifest.py` | `9 passed in 0.05s` | 0 |

**差额归因（逐项对平，无"来源不明"的增量）**：

| 口径 | 首版（2026-11-18 早） | 本轮 | 差额 | 来源（我 `--collect-only` 逐条核对的 6 个新 node id） |
|---|---|---|---|---|
| `tests/unit` | 1803 | **1809** | **+6** | 恰为下列 6 条 |
| W12 七件 | 120 | **126** | **+6** | 同上 |
| `test_w12_publish_gate.py` | 23 | **29** | **+6** | 同上 |

新增 6 条（`pytest --collect-only` 实测）：
`test_real_enum_alpha_allowed_yields_alpha`、`test_real_enum_tech_preview_only_yields_tech_preview`、`test_bare_str_stub_and_real_enum_are_equivalent`、`test_unknown_decision_value_fails_closed_without_exception[SOMETHING_ELSE]`、`[123]`、`[value2]`。
→ **6 = 6 = 6，三处增量同源，无回归、无用例消失**（`test_w12_claims.py` 25、`test_w12_doc_inventory.py` 17 与首版同数，未被削减）。

**异常终止**：本轮 13 条命令**全部 exit 0**，无 `exit 139`、无 `Windows fatal exception`、无隔离重跑。

### 8.7 门禁结论（本轮更新后）

> **门禁结论：`有条件放行`｜门禁通过：否。发布档位：`TECH_PREVIEW`（不变）。**
> 条件数 **7 → 6**（`C-W12-3` 关闭）；§② 的 G-1 / G-3 / G-4 仍为「部分满足」、G-5 仍「不满足」，**故不因本轮修复改判「通过」**。
> 新登记观察项 `O-W12-1`（裸标量字符串入参的 fail-closed 面收窄，见 §8.2 A-6）——**只登记、不回派、不自修**，是否立卡由主理人裁定。

---

*严过关（Edward）｜QA 工程师｜`software-dbguardian-w09-closeout2`｜时点锚 `HEAD = 2850eca`，全部改动未提交*
