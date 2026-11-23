# W09 T01 三处设计冲突｜书面裁定（v2 定稿）

- 裁定人：架构师｜依据：`docs/w09/01-prd.md` AC-01/AC-02、`docs/w09/02-design.md`、`docs/w09/03-task-cards.md`、`docs/w09/04-baseline.md`、team-lead 转来的工程师/QA 实测
- 实测基线：`tests/unit/test_w09_redaction_engine.py` **57 passed**、`tests/unit` **948 passed / 0 failed**（W08 基线 891 + 57），离线、未 commit
- **版本说明**：本文件上一轮已落盘（189 行）。本版按 team-lead 最新指令改版：① R1 补钉 15 个 `ToolResult` 字段 / `name_field` / 9 个人名键的回归断言，并按核实结果**撤回白名单 C 组**（精确匹配方案下已无必要）；② **R2 结论反转为「T02 做纯委托重构」**（上一版裁定「不做」作废，KD-W09-01 一并撤回）；③ 开工结论改为「**待 QA 独立复验通过 + 本裁定落盘后方可开工**」。R3 不变。
- 本轮**只改此一个文件**；不改 `docs/w09/01-prd.md` / `02-design.md` / `03-task-cards.md` / `04-baseline.md`、不改 `docs/w08/*`（永久冻结）、不写代码、不 commit、不碰 git
- 文档修改点以「**建议改文**」给出，由 team-lead 指定文档负责人落盘，本裁定不代改

---

## R1｜R05 `name` 子串与 `ToolResult.tool_name` 冲突（阻断 T02）

### 事实

1. `core/redaction/rules.py:32-42` 的 `NAME_KEY_MARKERS` 含裸 `name`；`:159-162` 的 `is_name_key()` 用归一化**子串**法（`any(marker in normalized for ...)`）。
2. `core/tools/contracts.py:42-59` 的 `ToolResult` **15 个顶层字段**实为：`tool_name`, `status`, `task_id`, `run_id`, `tool_call_id`, `started_at`, `duration_ms`, `data`, `warnings`, `error`, `source`, `observed_at`, `truncated`, `capability_ref`, `source_metadata` —— **没有** `display_name` / `user_name` / `customer_name`。`normalize_key("tool_name")` → `"toolname"`，含 `"name"` → `is_name_key(...) is True`。
3. `core/tools/contracts.py:62-80` 的 `TargetObjectSpec` 含 **`name_field`**（`:77`）；`normalize_key("name_field")` → `"namefield"`，同样会因裸 `name` 子串误命中，T02 接入契约对象时会踩同一个坑。
4. 工程师实测：`RedactionGate.for_tool_result()` 接到 `core/tools/executor.py:238-241` 后，所有工具结果都会 `status=REVIEW_REQUIRED`、`unresolved_sensitive_spans=[SEMANTIC_CONFIRMATION_REQUIRED]`、`data={}` + `truncated=True` —— **整条执行链被降级**，不是实现 bug。
5. R12 的 `SENSITIVE_KEY_MARKERS` 是**与 `core/audit/events.py:26-36` 共享的常量**，不属于 R05 词表，不受本冲突影响。
6. `AC-02`（`01-prd.md:132`）要求同一输入 5 轮 `redaction_count` / `redaction_version` / `payload_hash` 集合大小各为 1 —— **任何依赖值形态的启发式都会破坏确定性**。

### 裁定

**定稿方案：「裸 `name` 精确匹配 + 补齐显式人名 marker + 子串词表移除裸 `name`」，禁止任何「值像人名」启发式；R12 与共享常量不动。**

> **相对上一版的变更**：上一版设了 `SYSTEM_METADATA_KEYS`（9 项）作第二道防线。经 team-lead 核实 15 个 `ToolResult` 顶层字段均不含人名 marker 后，精确匹配方案**本身已使全部 15 个字段与 `name_field` 为 `False`**，白名单不再是必要条件，反而引入一个需长期维护的闭集。**本版撤回 C 组白名单**，实现更简洁；若后续新增 marker 导致回归断言转红，再按闭集方式单独加固（须升 `redaction_version`）。

**确定性规则定义（可直接抄进实现）**

```python
# core/redaction/rules.py —— 替换 :32-42 与 :159-162
# A 组：唯一精确键名（归一化后相等才命中）—— 裸 name 必须只在这里出现
NAME_EXACT_KEYS = frozenset({"name"})

# B 组：归一化子串词表（**不含裸 "name"**）
#   原词表 8 项去掉 name，补 5 个显式人名 marker，避免欠脱敏
NAME_KEY_MARKERS = (
    "姓名", "联系人", "商户号",
    "owner_name", "real_name", "order_no", "biz_id", "customer_id",
    "user_name", "customer_name", "display_name", "account_name", "full_name",
)

_EXACT_NORMALIZED = frozenset(normalize_key(k) for k in NAME_EXACT_KEYS)
_NAME_NORMALIZED = tuple(normalize_key(k) for k in NAME_KEY_MARKERS)  # 词表同样归一化，:147-150 既有口径

def is_name_key(key: object) -> bool:
    """R05 键名判定：确定性、纯键名函数，不读值、不引入启发式。"""
    n = normalize_key(key)                       # :142-144 既有归一化，不动
    if n in _EXACT_NORMALIZED:
        return True
    return any(m in n for m in _NAME_NORMALIZED)
```

**分组归属与「为何不误伤 `ToolResult` 顶层」（回应待确认点 4）**

| 键 | 归组 | 归一化 | 说明 |
|---|---|---|---|
| `name` | **A 精确** | `name` | 唯一精确项；`toolname` / `namefield` 均 ≠ `name` → 全部 False |
| `姓名` / `联系人` / `商户号` | B 子串 | 原样（中文 `isalnum()` 为真） | 中文业务键常带前后缀，保留子串 |
| `owner_name` / `real_name` / `user_name` / `customer_name` / `display_name` / `account_name` / `full_name` | B 子串 | `ownername` / `realname` / `username` / `customername` / `displayname` / `accountname` / `fullname` | **显式补齐 5 项**：只改精确匹配会漏掉这些原本靠 `name` 子串命中的键，造成**欠脱敏**（比误命中更危险） |
| `order_no` / `biz_id` / `customer_id` | B 子串 | `orderno` / `bizid` / `customerid` | 业务号，保留子串以覆盖 `xx_order_no` 等带前缀形式 |

- **15 个 `ToolResult` 顶层字段**：逐一归一化后，均不在 A 组，也**不含 B 组任何 marker**（已核实，见验收断言 1）→ 全部 `False`。
- **B 组业务号/中文键**只可能出现在 `data` / `source_metadata` 等**业务数据**里，本就应视为敏感；即使出现也属正确命中，不是误伤。
- 不变项：R05 的**值**规则 `^[A-Z]{2,6}\d{6,}$`（`02-design.md:139`）、`normalize_key()`（`:142-144`）、`is_sensitive_key()` / `SENSITIVE_KEY_MARKERS` / R12（`:146`）全部不动。
- 确定性：`is_name_key()` 仅依赖键名字符串，无值读取、无随机、无启发式 → AC-02 的 5 轮确定性天然成立。

### 对 `03-task-cards.md` T02 卡与 `02-design.md` §2.2 的具体修改点

| 位置 | 改成什么 |
|---|---|
| `core/redaction/rules.py:32-42` | `NAME_KEY_MARKERS` 换为上文 B 组 13 项（**移除裸 `name`**），新增 `NAME_EXACT_KEYS = frozenset({"name"})` |
| `core/redaction/rules.py:159-162` | `is_name_key()` 换为上文确定性实现；删除上一版拟设的 `SYSTEM_METADATA_KEYS` / `_METADATA_NORMALIZED` |
| `docs/w09/02-design.md:139`（R05 行） | 「键名归一化命中 `name`/`姓名`/…（复用归一化子串法）」→「**键名判定两组：`NAME_EXACT_KEYS={"name"}` 精确命中；`NAME_KEY_MARKERS` 13 项归一化子串（**不含裸 `name`**）；R05 值规则不变**」；替换产物与 `REVIEW_REQUIRED` 判定列不动 |
| `docs/w09/02-design.md:146`（R12 行） | 末尾补「**R12 仍用子串法且与 `core/audit/events.py:26-36` 共享常量，不随 R05 调整**」 |
| `docs/w09/03-task-cards.md:149-156`（T02 卡「涉及文件」） | 追加「**开工前置**：`core/redaction/rules.py` 的 R05 修订（见 `05-rulings.md` R1）+ 回归断言；**未落地且 QA 未复验前不得把 `for_tool_result()` 接到 `executor.py:238-241`**」 |
| `docs/w09/03-task-cards.md:206`（闸门①语义） | 补「接入后 `ToolResult` 任一顶层字段不得触发 `REVIEW_REQUIRED`，否则视为 R1 未落实」 |

> 本裁定**不修改**上述文件，仅给出改文。

### 可机器验证的验收方式

在 `tests/unit/test_w09_redaction_engine.py` 增（离线、无 DB）：

1. **15 个 `ToolResult` 顶层字段全为 False**：对 `tool_name, status, task_id, run_id, tool_call_id, started_at, duration_ms, data, warnings, error, source, observed_at, truncated, capability_ref, source_metadata` 逐个 `assert is_name_key(k) is False`（参数化，不只测 `tool_name`）；
2. **`assert is_name_key("name_field") is False`**（`TargetObjectSpec` 同款坑）；可顺带覆盖 `database_field` / `schema_field` / `keys_field`；
3. **9 个人名键全为 True**：`name`, `姓名`, `user_name`, `customer_name`, `display_name`, `account_name`, `full_name`, `owner_name`, `real_name` 逐个断言（守住不欠脱敏）；
4. **B 组业务号生效**：`order_no`, `biz_id`, `customer_id`, `商户号`, `联系人`, `xx_order_no` 为 True；
5. **通道级回归**：`engine.redact({"tool_name": "mysql_lab_update_order_note", "data": {"rows": 1}}, channel=TOOL_RESULT)` → `status is not REVIEW_REQUIRED`、无 `SEMANTIC_CONFIRMATION_REQUIRED` span、`data` 未被清空；
6. **确定性回归**：同一输入 5 轮 `{status, redaction_count, detected_categories, payload_hash}` 集合大小各为 1（AC-02）。

验收命令：`pytest tests/unit/test_w09_redaction_engine.py -q`（应保持 **≥60 passed**，即原 57 + 新增 ≥3；`test_ac01_r05_key_markers_are_normalized_before_matching` 的 `Customer-ID`/`owner_name`/`order_no`/`biz_id`/`商户号` 不受影响）。

---

## R2｜`canonical_payload_hash` 是否由 T02 做纯委托重构

### 事实

1. `docs/w09/02-design.md:170` 写「`core/evidence/service.py::canonical_payload_hash` 改为**委托** `core/redaction/payload_text.py::canonical_payload_text`（保持向后兼容导出），**全仓仅一份实现**」；文件清单 `:628`、`:660` 亦列「改为委托」。
2. `docs/w09/03-task-cards.md:185-189` 写「hash **计算口径不变**」「口径与 W08 完全一致，不改」；`:216` 禁止项「禁止改变 `payload_hash`/`redacted_payload_hash` 的**计算口径**」。
3. 两处实现**逐字等价**（复核）：`core/evidence/service.py:30-39` 与 `core/redaction/payload_text.py:17-30` 均为 `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)` → `sha256(UTF-8).hexdigest()`。
4. 工程师已加 `tests/unit/test_w09_redaction_engine.py:326` `test_ac04_new_helper_matches_existing_evidence_hash_implementation` 证明两份实现逐字等价，重构风险可控。
5. 冻结回归资产：`database/migrations/003_evidence_redaction_hash.sql`、`docs/w06/05-freeze.md`、`tests/integration/test_w03_integrity.py`（实测存在）。

### 裁定

**T02 做纯委托重构，行为不变。** `core/evidence/service.py::canonical_payload_hash` 保留**同名导出、同签名、同返回类型**，函数体改为委托 `core/redaction/payload_text.py::canonical_payload_text`（或直接 `return canonical_payload_hash(payload)` 并 re-export）；**调用点、参数、序列化参数一个都不改**。达成设计 §2.4「全仓仅一份实现」。

**「设计说改为委托」与「任务卡说口径不变」并不矛盾**：前者指**实现位置**，后者指**计算口径/结果值**。两者同时成立，不构成冲突；上一版裁定的「不做重构」与 **KD-W09-01 一并作废**。

**硬约束与执行顺序**（降低同文件混改风险）：
- **不得改变 W03–W08 任何已冻结的 `payload_hash` 值**；
- T02 内**分步提交**：① 先做纯委托重构 + 跑完冻结回归；② 再在 `create()` 加脱敏闸门。两步分开，便于一旦哈希漂移可单步定位/回退；
- 重构后 `test_ac04_new_helper_matches_existing_evidence_hash_implementation` 仍须绿（此时两边本就是同一实现，断言不得放宽或删除）。

### 对 `03-task-cards.md` T02 卡与 `02-design.md` §2.2 的具体修改点

| 位置 | 改成什么 |
|---|---|
| `docs/w09/02-design.md:170`、`:628`、`:660` | **保持原文不改**（「改为委托 / 全仓仅一份实现」即本裁定结果） |
| `docs/w09/03-task-cards.md:185-189` | 「hash 计算口径不变、**不改**」→「**实现**改为委托 `payload_text.py`，**计算口径与结果值不变**；先重构 + 冻结回归，再加闸门（分步）」；`:188/:189` 的「结果值与 W08 完全一致」保留 |
| `docs/w09/03-task-cards.md:216` | **不改**（禁止改变计算口径仍有效）；末尾补「并保留 `test_ac04_new_helper_matches_existing_evidence_hash_implementation` 等价断言，不得删除/放宽」 |
| `docs/w09/03-task-cards.md`（T02 验收命令） | 追加 `tests/integration/test_w03_integrity.py` 与冻结向量比对 |

### 可机器验证的验收方式

1. `pytest tests/unit/test_w09_redaction_engine.py::test_ac04_new_helper_matches_existing_evidence_hash_implementation -q`（绿，断言不得放宽）；
2. **`pytest tests/integration/test_w03_integrity.py -q` 全绿**（W03 证据链与哈希完整性回归）；
3. **`database/migrations/003_evidence_redaction_hash.sql` 相关既有行**：重构前后 `payload_hash` / `redacted_payload_hash` **逐字节比对一致**（对同一批固定样本逐行 SELECT 比对，不升版本、不重算）；
4. **`docs/w06/05-freeze.md` 冻结向量**逐字节比对（`action_digest` 与载荷哈希均不得变化）；
5. `pytest tests/integration/test_evidence_service.py -q`（`03-task-cards.md:216` 指定的 W08 回归）绿；
6. 静态断言：`core/evidence/service.py` 中 `canonical_payload_hash` 的**导出名、签名、返回类型**未变，`json.dumps` 参数字面量在 `payload_text.py` 中保持 `sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str`。

---

## R3｜BLOCKED 类「不替换」与 AC-01「不含原串」

### 事实

1. `docs/w09/02-design.md:140/:144/:145` 对 R06（连接串）/R10（私钥）/R11（证书）写「**不替换**，整段定位」，判定 `BLOCKED`；`:148-151` 把「命中 R06/R10/R11」列为「无法可靠脱敏」闭集第 1 条。
2. `docs/w09/01-prd.md:131` AC-01 要求 12 类样本逐类「`raw_secret not in payload_redacted`」；`01-prd.md:26` G1 要求敏感载荷**不进入**下游通道。
3. 工程师实现：`<BLOCKED:CATEGORY>` 占位抹掉原串、**不计入 `redaction_count`**，保留「不做可用化改写 + 记 unresolved span + 状态 `BLOCKED`」。
4. **AC-01 不需改判**：工程师 12 个参数化用例逐类断言 `original not in json.dumps(payload_redacted)` 且全绿 —— 占位符已使「原串不残留」对 12 类全部成立，**不得**对 QA 说「这 3 类不算原串不残留」。真正需要同步的只是**设计措辞**。
5. 若按字面「不替换」，隔离载荷 / `payload_redacted` 里会残留**可用的 DSN 与私钥原文**，既违反 AC-01，也把高危材料写进人工复核区，与 G1 直接冲突。

### 裁定

**确认工程师实现为设计意图的正确解读，不改回字面「不替换」。保留 `<BLOCKED:CATEGORY>` 占位符；AC-01/AC-02 均不改判。**

设计里的「不替换」= **不做可用化/保结构的改写**（不生成可用的脱敏后 DSN、不伪造截断私钥、不把证书改成可解析形式），**不是**「保留原文字节」。`<BLOCKED:CATEGORY>` 是**掩码**而非「替换」：

- 固定字面量按类别取 `<BLOCKED:CONNECTION_STRING>` / `<BLOCKED:PRIVATE_KEY>` / `<BLOCKED:CERTIFICATE>`；**不得**包含原文片段、哈希或任何可逆信息；
- **不计入 `redaction_count`**（否则会把「阻断」误统计成「已脱敏」）；
- `status` 仍为 `BLOCKED`、**`unresolved_sensitive_spans` 必须记录**、闸门①/② 仍**不放行**下游通道；
- 固定字面量 → 5 轮确定性天然成立（AC-02）。

### 对 `03-task-cards.md` T02 卡与 `02-design.md` §2.2 的具体修改点

| 位置 | 改成什么 |
|---|---|
| `docs/w09/02-design.md:140`、`:144`、`:145` | 「**不替换**，整段定位」→「**不做可用化改写**；以固定 `<BLOCKED:CATEGORY>` 占位抹除原串（**不计入 `redaction_count`**），记 unresolved span，判定 `BLOCKED`」 |
| `docs/w09/02-design.md:148-151` | 「无法可靠脱敏」第 1 条补「掩码后 `payload_redacted` **不得含原文片段**」 |
| `docs/w09/03-task-cards.md:207`（闸门②） | 追加「`BLOCKED` 抛 `RedactionBlockedError` 前，`payload_redacted` 必须已抹除原串；隔离区只存定位信息，与 `:213` 一致」 |
| `docs/w09/03-task-cards.md`（T02 测试清单） | 增 R3 回归（见下）；**AC-01 判据原文不动** |

### 可机器验证的验收方式

1. R06/R10/R11 三类各构造样本：`assert raw_secret not in payload_redacted`（AC-01 原判据，强度不降）；
2. `assert "<BLOCKED:" in payload_redacted` 且占位符与类别一一对应；
3. `assert redaction_count` **未因这三类增加**（与 `CLEAN/REDACTED` 用例对照）；
4. `assert status is BLOCKED` 且 `len(unresolved_sensitive_spans) > 0`；
5. 同一输入 5 轮 `payload_redacted` 完全相同（AC-02）；
6. 隔离区 `redaction_quarantine` 的 `json_path/start/end` 存在且**不含任何原文片段**（与 `03-task-cards.md:213` 一致）。

---

## 结论：T02 开工条件

**T02 待 QA 独立复验通过 + 本裁定落盘后方可开工。**

| 项 | 状态 | 说明 |
|---|---|---|
| **R1** | **未闭合（前置）** | 须先按本裁定改 `core/redaction/rules.py:32-42/159-162`，补齐 15 字段 / `name_field` / 9 人名键 / B 组业务号 / 通道级 / 5 轮确定性回归；**并待 QA 独立复验 R05 冲突已消除**。此前不得把 `for_tool_result()` 接到 `executor.py:238-241`。R1 修订属 T01 收尾补丁，作为 T02 的 step 0 同批提交。 |
| **R2** | 随 T02 落实 | 做纯委托重构（行为不变），**分步**：先重构 + 冻结回归全绿，再加闸门；不阻塞开工，但须在 T02 内完成。 |
| **R3** | 已明确，无需生产改动 | 现有 `<BLOCKED:CATEGORY>` 实现即正确；只补设计文档改文与回归测试，AC-01 不改判。 |

> 开工检查清单（缺一不可）：
> ① **QA 独立复验通过**（`#25` 复现 R05 冲突已消除，且记为「设计冲突已闭环」而非「实现通过」）；
> ② 本裁定 `docs/w09/05-rulings.md` 已落盘并被 T02 卡引用；
> ③ `is_name_key` 对 15 个 `ToolResult` 字段与 `name_field` 全 `False`、对 9 个人名键全 `True` 的断言齐备且全绿；
> ④ `test_ac04_new_helper_matches_existing_evidence_hash_implementation`、`tests/integration/test_w03_integrity.py`、`tests/integration/test_evidence_service.py` 全绿，`003` 迁移相关 `payload_hash` 与 W06 冻结向量逐字节不变；
> ⑤ R3 的 `raw_secret not in payload_redacted` 对 12 类全绿、`redaction_count` 未被 BLOCKED 三类增加；
> ⑥ `tests/unit` 全量不低于 948 + 新增用例数，`0 failed`。
>
> **补充**：T01 收尾阶段新增 **R4**（实现版本对齐 + 嵌套裸 `name` 遗留），权威顺序 **R4 > R1**；QA 复验须基于 **R4-A 补齐后**的实现，见文末。

---

## R4｜补充裁定：实现版本对齐 + 嵌套裸 `name` 遗留

> 本节为 T01 收尾阶段新增，权威顺序 **R4 > R1**（R1 的「确定性规则定义」代码块以 R4-A 为准）。仍只改本裁定一个文件。

### 事实

1. **存在版本差**：本文件 v1（189 行，三组闭集：A 精确 14 / B 子串 6 / C 白名单 9）是工程师实现时的磁盘版；v2（194 行）按 team-lead 核实结果**撤回 C 组**、把 13 项移入子串组。工程师按「以裁定为准」实现的是 **v1**，并已回报 5 处与 team-lead 原指令的差异。实测：单文件 **105 passed**、`unit` **996 passed / 0 failed**，真实 `ToolResult.model_dump()` → `CLEAN` / `passable=True` / `tool_name` 逐字不变 / payload 零改写。
2. **v1 相对 v2 存在欠脱敏缺口（必修）**：A 组 14 项只在「归一化后完全相等」时命中，B 组 6 项不含人名 marker。故 `xx_customer_name` / `some_user_name` / `xx_owner_name` 归一化后既不相等、也不含 B 组任何词根 → `is_name_key` 返回 `False`；而**修正前的原始规则**（裸 `name` 作子串）会命中这三类。即 v1 收窄了原规则覆盖面，方向是**欠脱敏**，比误命中更危险。
3. 工程师观察：`engine.redact({"definition": {"name": "mysql_lab_update_order_note"}, "tool_name": ...}, channel=TOOL_RESULT)` → `REVIEW_REQUIRED`、`payload_redacted["definition"]["name"] = "<NAME:...>"`。已核实 `ToolResult` 顶层 15 字段无此问题，`definition_name`/`nickname`/`xx_owner_name` 为 `False`；**裁定验收 #3 明确要求 `is_name_key("name") is True`**，故这是有意保留的精确命中。
4. `core/redaction/engine.py:137` 的 `RS_REDACTION_V1` 为当前 ruleset 版本；`core/redaction/rules.py:38` 已注明「增补任一组须升 `redaction_version`、改裁定文档并补回归断言」。
5. `AC-02` 要求同输入 5 轮 `{redaction_count, redaction_version, payload_hash}` 集合大小各为 1。**path 感知若实现为 `(json_path, key)` 的纯函数，仍满足确定性**（不是启发式），但会引入新的闭集维护成本。

### 裁定

**R4-A｜版本对齐：不推倒重写，采用最小增量收敛到 v2 语义。**

- 权威语义以 **v2** 为准；当前 v1 实现**可接受为等价实现**，条件是补齐一项：
  **`NAME_KEY_SUBSTRING` 增补 7 个人名 marker 的归一化词根**：`ownername`、`realname`、`username`、`customername`、`displayname`、`accountname`、`fullname`（即 A 组里 7 个含 `_name` 的项在 B 组各放一份归一化形式）。B 组 6 → **13 项**。
- **结构口径以「两组」为准，C 组不必存在**（详见文末 **R4-D**）：权威形态为 `NAME_EXACT_KEYS = frozenset({"name"})` + `NAME_KEY_MARKERS` 13 项。本条「C 组保留但降格」的措辞基于工程师当时的 **v1 中途态**，现已被两组实现取代；**裁定判据是行为等价，不是结构形态**。
- A 组 14 项（含裸 `name` 精确）**不动**；`is_name_key` 判定顺序不动（元数据短路 → 精确 → 受限子串）。
- 补齐后 `is_name_key("xx_customer_name")` 为 `True`，欠脱敏缺口关闭；`tool_name` / `name_field` / 15 字段仍 `False`（已核：这些字符串不含上述 7 个词根）。
- **须升 `redaction_version`**（`RS_REDACTION_V1` → 新常量），同步 `core/redaction/rules.py:38` 注释；AC-02 的 5 轮确定性在同一版本内仍成立。

**R4-B｜嵌套裸 `name`：本周不引入路径感知，登记为已知限制 `KD-W09-02`。**

- 理由：① `ToolResult.model_dump()` 顶层 15 字段**不含** `definition`，该样例是合成载荷，不是 T02 闸门① 的实际输入；② 掩码 + `REVIEW_REQUIRED` 是**保守方向**（fail-closed），造成可用性降级而非敏感数据泄露，与「欠脱敏」不同级；③ 路径白名单需维护「哪些 `json_path` 下的 `name` 是系统标识」的闭集，且会再触发一轮版本升级 + QA 复验，收益不匹配；④ 若 W10 出现真实误判，再以「闭集 `json_path` 白名单 + 升 `redaction_version` + 补回归」处理，本裁定不预判其形态。
- **T02 组合根约束**：`ToolResult.data` / `source_metadata` **只放业务键值**，不得把 `ToolDefinition` / `TargetObjectSpec` 契约对象整包塞进待脱敏载荷；契约对象如需传递，走结构化字段而非 `model_dump()` 塞字典。这是**组合层纪律**，不是引擎规则。
- `is_name_key("name") is True` **保持**（验收 #3 不变）；不加值形态判定、不加启发式。

**R4-C｜开工结论不变**：仍为「**T02 待 QA 独立复验通过 + 本裁定落盘后方可开工**」，且 **QA 复验必须基于 R4-A 补齐后的实现**；R4-A 之前的复验结果不覆盖欠脱敏缺口。

### 对 T02 任务卡 / 设计文档的具体影响

| 位置 | 改成什么 |
|---|---|
| `core/redaction/rules.py:56-62`（`NAME_KEY_SUBSTRING`） | 由 6 项补为 13 项，新增上述 7 个归一化词根 |
| `core/redaction/engine.py:137` / `rules.py:38` | 升 `redaction_version` 常量并同步注释 |
| `docs/w09/03-task-cards.md` T02 卡 | ① 加 R4-B 的组合根约束（`data`/`source_metadata` 不塞契约对象）；② 加 R4-A 的 7 词根参数化回归清单与升版步骤 |
| `docs/w09/02-design.md:139`（R05 行） | 词表说明改为「**A 组精确 14 项 + B 组受限子串 13 项（含 7 个人名词根）+ C 组元数据短路（冗余二道防线）**」 |
| 本文件 R1「确定性规则定义」 | 以 **R4-A** 为准（工程师当前实现 + B 组 7 词根）；v2 中「撤回 C 组」的表述由 R4-A 覆盖 |

### 可机器验证的验收方式

1. **7 词根参数化**：`xx_customer_name` / `some_user_name` / `xx_owner_name` / `prefix_real_name` / `x_display_name` / `y_account_name` / `z_full_name` 全为 `True`；
2. **回归不误伤**：15 个 `ToolResult` 字段 + `name_field` 仍全 `False`；9 个人名键仍全 `True`；真实 `ToolResult.model_dump()` → `CLEAN` / `passable=True` / payload 零改写；
3. `redaction_version` 已升，且与 `REDACTION_APPLIED` 审计事件中的 `redaction_version` 相等（AC-08）；
4. 5 轮确定性（AC-02）；`pytest tests/unit/test_w09_redaction_engine.py -q` ≥ **105 + 新增**、`tests/unit` ≥ **996 + 新增**、`0 failed`；
5. **QA 第二轮复验基于补齐后实现**，结论记为「R1 冲突已闭环（含欠脱敏缺口）」，而非「实现通过」。

---

## R4-D｜结构口径与回归基线（QA 复验对照，权威顺序最高）

> 回应 QA 两处待确认口径。**本节覆盖 R4-A 中关于「C 组保留」的过渡措辞。**

### 裁定 1｜两组实现可接受，且即为定稿形态

**行为等价的两组实现完全可接受，且优先**：`core/redaction/rules.py` 现为 `NAME_EXACT_KEYS = frozenset({"name"})`（1 项精确）+ `NAME_KEY_MARKERS`（13 项归一化子串），**无 `SYSTEM_METADATA_KEYS`**。这正是裁定 **v2** 的形态：精确匹配本身已使 15 个 `ToolResult` 顶层字段与 `name_field` 全为 `False`，白名单不再是必要条件。

- **不要求 C 组实体存在**；QA 不得因「文档写 C 组保留、代码没有」判回归。
- **不要求 `NAME_KEY_EXACT` / `NAME_KEY_SUBSTRING` 旧常量存在**；当前测试已断言 `not hasattr(rules, "SYSTEM_METADATA_KEYS"/"NAME_KEY_EXACT"/"NAME_KEY_SUBSTRING")`，这是**正确行为**。
- 判定口径统一为：`is_name_key()` 的**输入输出行为**（下表），不比对模块内部常量名或分组数量。

### 裁定 2｜第二轮回归基线重定（正例 / 负例）

QA 第一轮报告把 `display_name` / `user_name` 列入「误判清单」——**该清单作废**。R4-A 补 7 词根后，这些键是**有意命中**，必须 `True`。

| 集合 | 键 | 期望 |
|---|---|---|
| **正例（人名 / 业务号，必须 `True`）** | `name`, `姓名`, `联系人`, `商户号`, `user_name`, `customer_name`, `display_name`, `account_name`, `full_name`, `owner_name`, `real_name`, `order_no`, `biz_id`, `customer_id`, `xx_order_no`, `Customer-ID`, `xx_customer_name`, `some_user_name`, `xx_owner_name`, `prefix_real_name`, `x_display_name`, `y_account_name`, `z_full_name` | `is_name_key is True` |
| **负例（系统元数据，必须 `False`）** | `ToolResult` 15 个顶层字段（`tool_name`, `status`, `task_id`, `run_id`, `tool_call_id`, `started_at`, `duration_ms`, `data`, `warnings`, `error`, `source`, `observed_at`, `truncated`, `capability_ref`, `source_metadata`）、`name_field`, `database_field`, `schema_field`, `keys_field`, `table_name`, `column_name`, `schema_name`, `index_name`, `file_name`, `class_name`, `host_name`, `field_name`, `definition_name`, `nickname` | `is_name_key is False` |

> `display_name` / `user_name` / `customer_name` **移出负例集、移入正例集**。`source_metadata` 虽是业务字典，但**其键名本身**不含任何 marker，故为负例；其中若嵌套 `name` 键则按 **KD-W09-02** 处理（fail-closed，已知限制，不判 FAIL）。

### 只读复核的当前状态（供 QA 对照，非判定）

- `pytest tests/unit/test_w09_redaction_engine.py --collect-only -q` → **123 collected，无 ImportError**；QA 早前报的 `ImportError` / `len(NAME_KEY_SUBSTRING) == 6` 来自 **18:38:24 的旧测试快照**，当前测试 mtime **18:51:00** 已同步：`:509` `NAME_EXACT_KEYS == frozenset({"name"})`、`:510` `len(NAME_KEY_MARKERS) == 13`、`:524` 断言旧常量不存在。
- 7 词根方向已闭合：`xx_customer_name` / `some_user_name` / `xx_owner_name` / `display_name` 全 `True`；`tool_name` / `table_name` / `column_name` / `name_field` 全 `False`。
- **唯一未闭合项**：`core/redaction/rules.py:16` 的 `RS_REDACTION_V1 = "redaction-v1"` **仍未升版**（R4-A 硬要求）。工程师须升常量并同步 `engine.py:137` 默认值、`__init__.py` 导出、`rules.py:38` 注释，以及 `REDACTION_APPLIED` 事件一致性（AC-08）。**此项未落地前 QA 不应出最终复验结论。**

### QA 复验口径

- 负例集按上表重定；`KD-W09-02` 显式实测（嵌套 `name` → `REVIEW_REQUIRED` + `<NAME:...>` 掩码）并注明「fail-closed 而非泄露」、T02 组合层约束不在引擎复验范围 —— 此处理**采纳**。
- 结论仍写「**R1 冲突已闭环（含欠脱敏缺口）**」；开工结论不变（待 QA 复验通过 + 本裁定落盘）。
