# W12 表述禁令扫描报告（T06 / AC-19 / AC-20 / AC-21）

> 生成方式：`core/release/claims.py::scan_claims()` → `render_claim_scan_report()`。
> 口径：`docs/w12/02-design.md` §8（AC-19 必须实现为**上下文感知**扫描，D-10）+
> `docs/w10-w12-lead-rulings.md` §15（**W12-CORR-01**：值/句级否定语境豁免）。
> 本文件是 W12 对外材料，因此**它自身也在被扫描范围内**。

## 1. 结论

| 项 | 值 |
|---|---|
| `violations` | 0 条（必须为空） |
| `demo_input_violations` | 0 条（必须为空） |
| `limitation_integrity_ok` | `True` |
| `evidence_refs_ok` | `True` |
| `exempted_lines` | 24（否定式引用 / 规范引用，逐条见第 3 节） |

最终判定：`ok = True`（`violations` 为空且三组附加校验通过）。

## 2. 三范围划分（不静默跳过任何对外材料）

### 2.1 严格模式：W12 对外材料（禁用表述零容忍）

| 文件 | 绑定口径 |
|---|---|
| `docs/w12/deployment-guide.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/demo-script.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/known-limitations.json` | W12-CORR-01 值/句级否定语境豁免 |
| `docs/w12/known-limitations.md` | W12-CORR-01 值/句级否定语境豁免 |
| `docs/w12/api-contract.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/security-materials.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/benchmark-baseline.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/alpha-acceptance-report.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/release-notes.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/claim-scan-report.md` | 严格零容忍（唯一例外 `门禁通过：否`） |
| `docs/w12/openapi.json` | 严格零容忍（唯一例外 `门禁通过：否`） |

### 2.2 `spec_mode`：内部设计件（须引用禁令表本身）

| 文件 |
|---|
| `docs/w12/02-design.md` |
| `docs/w12/03-task-cards.md` |

### 2.3 排除件（上游输入，理由逐条登记）

| 文件 | 排除理由 |
|---|---|
| `docs/w12/01-prd.md` | 上游输入件：其 `:113` / `:200` 正含禁用词表与被引用的原文（含 `门禁通过` 字样），按 D-10 显式排除；AC-19 的「全文」按「W12 本周对外产出全文 + 内部件 spec_mode」解释。 |

## 3. 豁免逐条留痕（W12-CORR-01，不得静默豁免）

合计 **24** 条；每条给出「文件:位置｜值摘要｜命中短语｜判定依据」。

判定依据取值：`ALLOWED_PHRASE`（精确命中固定短语）、`NEGATION_WINDOW:<否定词>`（短语前 12 字符内有否定词）、`NEGATION_PREFIX:<否定词>`（该值/行以否定词开头）、`SPEC_MODE_MARKER`（内部件规范引用整行豁免）。

```text
[1] docs/w12/known-limitations.json:$.items[5].statement｜值摘要：发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，不得表述为完整 Alpha。｜命中：W12_EXTRA:完整 Alpha｜依据：NEGATION_WINDOW:不得
[2] docs/w12/known-limitations.md:L25｜值摘要：|  |  | 发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，不得表述为完整 Alpha。 |｜命中：W12_EXTRA:完整 Alpha｜依据：NEGATION_WINDOW:不得
[3] docs/w12/02-design.md:L27｜值摘要：| **D-10** | **AC-19 表述禁令自相矛盾**：禁用集合含「门禁通过」，但权威 W07 口径是「有条件放行；门禁通过：否」；且  内引用禁令表的｜命中：GATE_PASSED:门禁通过｜依据：SPEC_MODE_MARKER
[4] docs/w12/02-design.md:L370｜值摘要：3.  → 、；**仍不写「完整 Alpha」**（主理人表述禁令）。｜命中：W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[5] docs/w12/02-design.md:L429｜值摘要：禁用集合含「门禁通过」，但主理人要求的 W07 权威口径是「有条件放行；**门禁通过：否**」；且 、 本身要引用禁令表（如本表）。若按  的字面做全文本子串匹｜命中：GATE_PASSED:门禁通过｜依据：SPEC_MODE_MARKER
[6] docs/w12/02-design.md:L456｜值摘要：3. **否定语境豁免**：严格模式仅豁免 ； 额外豁免含  ∪ （///////////含反引号的行）的行，累加 。例如 §16 自查行的「**未写**『上游｜命中：W12_EXTRA:上游已通过｜依据：SPEC_MODE_MARKER
[7] docs/w12/02-design.md:L639｜值摘要：| **R-06** | 容器运行态被写成「已验证」 | 部署文档措辞 | 违反 W12-Q3 / 表述禁令 | 、 为 ；五项检查可  + 原因；扫描器禁用「｜命中：NETWORK_VERIFIED:容器网络隔离已验证｜依据：SPEC_MODE_MARKER
[8] docs/w12/02-design.md:L657｜值摘要：| **Q-W12-D** |  的  命名文案 | 建议「DBGuardian 技术预览（受限发布）」（不含 Alpha 大小写变体）； 时用「DBGuard｜命中：W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[9] docs/w12/02-design.md:L664｜值摘要：1. **上游门禁**：W08「有条件放行」/ W07「有条件放行；门禁通过：否」/ W09「T01–T06 + L1/L2 已实现并通过定向验收，W09 门禁｜命中：W12_EXTRA:上游已通过,W12_EXTRA:W12 开工,W12_EXTRA:回归全绿,W12_EXTRA:环境已清零,W12_EXTRA:Alpha 已就绪,W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[10] docs/w12/03-task-cards.md:L4｜值摘要：- 上游门禁口径：W08 =「**有条件放行**」；W07 =「**有条件放行；门禁通过：否**」；W09 =「**T01–T06 + L1/L2 已实现并通过｜命中：W12_EXTRA:上游已通过,W12_EXTRA:W12 开工｜依据：SPEC_MODE_MARKER
[11] docs/w12/03-task-cards.md:L24｜值摘要：15. **W12 特有｜容器不作硬门槛**： 未跑通保持  + 写限制清单；五项检查可  + 原因，不判 FAIL；不得表述为「容器网络隔离已验证」（W12-｜命中：NETWORK_VERIFIED:容器网络隔离已验证｜依据：SPEC_MODE_MARKER
[12] docs/w12/03-task-cards.md:L25｜值摘要：16. **W12 特有｜表述禁令**：W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09「…W09 门禁未评」；不写「上游已通过」「W12 开工｜命中：W12_EXTRA:上游已通过,W12_EXTRA:W12 开工,W12_EXTRA:回归全绿,W12_EXTRA:环境已清零,W12_EXTRA:Alpha 已就绪,W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[13] docs/w12/03-task-cards.md:L154｜值摘要：5. 禁止写「完整 Alpha」「Alpha 已就绪」等表述。｜命中：W12_EXTRA:Alpha 已就绪,W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[14] docs/w12/03-task-cards.md:L212｜值摘要：5. ****：整理 Compose/网络隔离//Secret 注入/PG·MySQL LAB 镜像/初始化脚本/迁移；复核 KD-2 命名（Gateway 与｜命中：NETWORK_VERIFIED:容器网络隔离已验证｜依据：SPEC_MODE_MARKER
[15] docs/w12/03-task-cards.md:L221｜值摘要：6. 禁止用「已修复」描述建议、禁止写「环境已清零」。｜命中：W12_EXTRA:环境已清零｜依据：SPEC_MODE_MARKER
[16] docs/w12/03-task-cards.md:L350｜值摘要：2. **AC-16②**： → 、名称「DBGuardian Alpha」；**仍不写「完整 Alpha」**。｜命中：W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[17] docs/w12/03-task-cards.md:L356｜值摘要：8. ****：/ 由同一  生成，限制清单随包发布； 时不得出现「完整 Alpha」。｜命中：W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[18] docs/w12/03-task-cards.md:L365｜值摘要：5. 禁止写「完整 Alpha」「Alpha 已就绪」「上游已通过」。｜命中：W12_EXTRA:上游已通过,W12_EXTRA:Alpha 已就绪,W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[19] docs/w12/03-task-cards.md:L441｜值摘要：6. 禁止写「QA 独立实跑通过」「容器网络隔离已验证」「已清理」「已收口」等禁用表述（除否定语境引用）。｜命中：GATE_CLOSED:已收口,NETWORK_VERIFIED:容器网络隔离已验证,ENV_CLEANED:已清理,QA_INDEPENDENT:QA 独立实跑通过｜依据：SPEC_MODE_MARKER
[20] docs/w12/03-task-cards.md:L491｜值摘要：|  | 控制库未观测残留 **140 行**（ §⑧-1），随时间增长 | 集成用例连库会继续累加 | 所有计数按本卡 / 过滤；不 DELETE/UPDAT｜命中：W12_EXTRA:环境已清零｜依据：SPEC_MODE_MARKER
[21] docs/w12/03-task-cards.md:L492｜值摘要：|  | 容器运行态 （） | 镜像 digest 不可取、容器实跑未做 |  + 登记；（W12-Q3）；**不实跑 **、不写「容器网络隔离已验证」 |｜命中：NETWORK_VERIFIED:容器网络隔离已验证｜依据：SPEC_MODE_MARKER
[22] docs/w12/03-task-cards.md:L496｜值摘要：| **上游门禁** | W08 有条件放行、W09 门禁未评、W10/W11 未裁定；W11  未落地 | 不得宣布 W12 开工、不得预写 Alpha 结论｜命中：W12_EXTRA:W12 开工｜依据：SPEC_MODE_MARKER
[23] docs/w12/03-task-cards.md:L534｜值摘要：6. 不预写 W11  值、不预写 Alpha 验收结论（W12-Q1）；不在红线未过时准备完整 Alpha 包。｜命中：W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
[24] docs/w12/03-task-cards.md:L535｜值摘要：7. 不写「上游已通过」「W12 开工」「回归全绿」「环境已清零」「Alpha 已就绪」「完整 Alpha」；门禁结论由主理人/QA 在收口件中给。｜命中：W12_EXTRA:上游已通过,W12_EXTRA:W12 开工,W12_EXTRA:回归全绿,W12_EXTRA:环境已清零,W12_EXTRA:Alpha 已就绪,W12_EXTRA:完整 Alpha｜依据：SPEC_MODE_MARKER
```

## 4. 规则（规则表放在代码围栏内：扫描时整块跳过，避免规则自述被判违规）

### 4.1 禁用正则

```text
GATE_CLOSED	已收口
GATE_PASSED	门禁通过(?!：否|:否)
NETWORK_VERIFIED	容器网络隔离已验证
ENV_CLEANED	已清理
QA_INDEPENDENT	QA\s*独立实跑通过
```

### 4.2 W12 追加禁用表述

```text
上游已通过
W12 开工
回归全绿
环境已清零
Alpha 已就绪
完整 Alpha
```

### 4.3 否定词（W12-CORR-01 值/句级豁免）

```text
不得
禁止
不能
不应
避免
不写
不含
禁用
不声称
```

豁免窗口：短语**前 12 字符**；另接受「该值/行以否定词开头」与「精确命中固定短语」。

### 4.4 否定语境标记（`spec_mode` 整行豁免）

```text
禁用
不得
禁止
应改为
表述禁令
自查
沿用
不写
不含
```

### 4.5 规范引用标记（仅 `spec_mode`）

```text
未写
不预写
不宣布
互斥
豁免
排除
负向先行
字面
规则
常量
断言
```

### 4.6 固定允许短语（清单放在代码围栏内：短语本身即被引用的原文）

```text
有条件放行；门禁通过：否
有条件放行
配置层已实测 / 运行态 NOT_VERIFIED
只读数据复核通过（依赖工程师实跑产物）
```

## 5. 处理流程（可复核）

1. **粒度**：`.json` 先解析，再**按每个字符串值**逐条扫描；`.md` 按行扫描；
2. **剥离代码语境**：逐行 fence 状态机跳过 fenced code block，并剥离行内 code span；
3. **`spec_mode` 放宽**：剥离后含否定/规范引用标记的行整行豁免（仅内部件）；
4. **负向先行断言**：`门禁通过(?!：否|:否)` 使权威写法「有条件放行；门禁通过：否」不被判违规；
5. **W12-CORR-01 值/句级豁免**：仅 `known-limitations.json` / `known-limitations.md` 启用，三条件（精确短语 / 前 12 字符否定词 / 以否定词开头）满足其一即计为否定式引用，并逐条留痕；
6. **剩余命中**计入 `violations`；缺失对外材料记 `SCAN_TARGET_MISSING`，**不静默跳过**；
7. **证伪负测**：把否定句改成肯定宣称（例如 `已达成完整 Alpha`）或删掉否定词只留短语 → 必须违规。

## 6. AC-20 结构校验

- `FunctionalAcceptanceResult` / `SecurityAcceptanceResult` / `AlphaAcceptanceReport` / `AcceptanceEvidence` 的字段集与 `demo_passed`/`demo_status`/`demo_ok` 交集为空；
- `assert_no_demo_input(payload)` 命中任一 demo 键 → `DEMO_AS_ACCEPTANCE`；
- `evidence_refs` 必须覆盖 W11/W10/W09 各至少 1 条，且路径真实存在。

### 6.1 门禁件可用性（实测）

| 周 | 路径 | 存在 |
|---|---|---|
| W11 | `docs/w11/02-design.md` | `True` |
| W11 | `docs/w11/03-task-cards.md` | `True` |
| W11 | `docs/w11/05-w11-qa-closeout.md` | `True` |
| W11 | `docs/w11/rc-freeze.md` | `True` |
| W10 | `docs/w10/02-design.md` | `True` |
| W10 | `docs/w10/03-task-cards.md` | `True` |
| W10 | `docs/w10/04-w10-qa-closeout.md` | `True` |
| W10 | `benchmarks/w10/metrics-v1.json` | `True` |
| W09 | `docs/w09/12-w09-qa-closeout.md` | `True` |
| W09 | `docs/w09/13-l1-l2-design.md` | `True` |
| W09 | `docs/w09/14-l1-l2-qa-closeout.md` | `True` |

> `benchmarks/w10/metrics-v1.json` 与 `docs/w11/rc-freeze.md` **现已存在**，
> 按实测标为可用（不再 `NOT_AVAILABLE`）；若将来缺失只会降级为登记，不判 FAIL（D-09）。

## 7. AC-21 限制清单校验

- 自动基础项 `AUTO_ITEM_IDS` 固定 13 项（6 W11 + 6 不承诺 + `DOC-PARTIAL`）；
- `assert_limitation_integrity()` 重算并逐字比对；删除 → `LIMITATION_REMOVED`，改写 → `LIMITATION_MODIFIED`；
- 人工项 `origin=MANUAL`，`added_by`/`reason` 必填，只能追加。

## 8. 上游口径引述（主理人版本，逐字）

- W07：「有条件放行；门禁通过：否」
- W08：「有条件放行」
- W09：「T01–T06 + L1/L2 已实现并通过定向验收，W09 门禁未评」
- 容器：「配置层已实测 / 运行态 NOT_VERIFIED」
