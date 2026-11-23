# W09 T01｜QA 独立复验报告（第二轮，基于 R4-A 补齐 + 版本升版后实现；含第二轮补测）

> **版本说明**：本文件首轮为「只读复验」版（131 / 1022 passed，遗留 1 登记为覆盖缺口）。经 team-lead 授权后**本轮补测**：在 `tests/unit/test_w09_redaction_engine.py` 新增 37 条独立参数化用例（R4-A 验收 #1 的 7 词根变体 + R4-D 裁定 2 的 29 条负例 + 1 条清单一致性），并核实 R3 回归已全覆盖（6/6，无需新增）。数字已全部刷新为 **168 / 1059 passed**；结论不变。

- 复验人：严过关（Yan）· QA 工程师
- 复验对象：`core/redaction/{__init__,models,rules,engine,payload_text}.py`、`tests/unit/test_w09_redaction_engine.py`
- 裁定依据（权威顺序）：`docs/w09/05-rulings.md` **R4-D（最高）** → R4-A / R4-B → R1 / R3
- 设计口径：`docs/w09/02-design.md` §2.2（R05 行、KD-W09-02 块）、`docs/w09/03-task-cards.md` T01/T02 卡
- 复验时间：2026-11-13（本地，离线）
- 结论措辞：**R1 冲突已闭环（含欠脱敏缺口）**。本报告不对 T02 及 AC-05/06/08 全链路作任何判定。

---

## ① 复验范围与口径

### 1.1 环境

| 项 | 值 |
|---|---|
| 仓库 | `D:/DBGuaredian`（Windows，Git Bash） |
| 解释器 | `D:/DBGuaredian/.venv/Scripts/python.exe` → **Python 3.14.7**（满足 `requires-python >=3.14`；PATH 上的裸 `python` 为 3.13.14，**未使用**） |
| 测试命令口径 | 一律 `-o addopts="" -q -p no:cacheprovider`（仓库 `addopts="-q"`，叠加会变 `-qq` 吞掉汇总行） |
| 数据库 | **全程未连库**：PG 5432 不可达、`DG_ADMIN_DSN` root 报 1045 → 未写库、未调 admin 清理、未断言「表已清空」 |
| 源码写入 | **源码零改动**：`core/redaction/**` 未改、`05-rulings.md` 未改、`docs/w08/**` 未动 |
| 测试写入 | 仅 `tests/unit/test_w09_redaction_engine.py`，**只增不删**（+37 条用例），经 team-lead 授权 |
| git | 未执行任何 `git add` / `git commit` |

### 1.2 独立复验方法（不重跑工程师的命令）

`tests/unit` 两条 pytest 命令照跑作回归基线；**8 组中的第 1/2/3/4/5/6/7 组全部由本人另写最小复现脚本实测**（`python - <<'PYEOF'` 直连模块、逐条断言、打印中间值），未采信工程师回传的任何结论数字。所有关键输出原文粘贴于 ②/③。

---

## ② 实跑证据

### 2.1 回归命令与汇总行（原文）

```
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_redaction_engine.py -o addopts="" -q -p no:cacheprovider
168 passed in 0.18s

$ .venv/Scripts/python.exe -m pytest tests/unit -o addopts="" -q -p no:cacheprovider
1059 passed in 6.06s

$ .venv/Scripts/python.exe -m pytest tests/integration/test_w03_integrity.py -o addopts="" -q -p no:cacheprovider
1 failed, 2 passed, 3 errors in 1.68s
```

- 单文件：**168 passed**（首轮复验时 131 passed；本轮按 R4-A 验收 #1 补 37 条参数化用例，见 2.10）
- `tests/unit`：**1059 passed / 0 failed**，总数 ≥ 1022 ✓（同时满足裁定「≥996 + 新增」）
- `test_w03_integrity.py`：**非通过**——3 个用例的 teardown 全部 `core.errors.DatabaseAuthError: 数据库认证失败`，根因 `OperationalError(1045, "Access denied for user 'root'@'localhost'")`（`database/mysql/connection.py:60`）。逐用例状态：

```
test_events_and_evidence_are_append_only_for_control_account      FAILED / ERROR at teardown
test_composite_tenant_fk_rejects_cross_tenant_evidence            PASSED / ERROR at teardown
test_cancel_emits_requested_before_cancelled                      PASSED / ERROR at teardown
```

  → **判定为环境阻塞，不计入通过**；本项对 R2（T02 委托重构）的验收不可在当前离线环境完成，须在有库环境补跑。

### 2.2 第 1 组（R05 冲突闭环）关键输出

```
positive count=23 negative count=29 (TR15=15)
positive FAIL=[]
negative FAIL=[]
old-const hasattr: {'SYSTEM_METADATA_KEYS': False, 'NAME_KEY_EXACT': False, 'NAME_KEY_SUBSTRING': False}
```

### 2.3 第 2 组（欠脱敏缺口·7 词根前后缀变体）关键输出

每个词根各测 5 种变法（`xx_` / `some_` / `prefix_` / `_xx` 后缀 / `X…Y` 夹心），共 35 条，**全 True**：

```
ownername:  {'xx_ownername': True, 'some_ownername': True, 'prefix_ownername': True, 'ownername_xx': True, 'XownernameY': True}
realname:   {'xx_realname': True, 'some_realname': True, 'prefix_realname': True, 'realname_xx': True, 'XrealnameY': True}
username:   {'xx_username': True, 'some_username': True, 'prefix_username': True, 'username_xx': True, 'XusernameY': True}
customername:{'xx_customername': True, 'some_customername': True, 'prefix_customername': True, 'customername_xx': True, 'XcustomernameY': True}
displayname:{'xx_displayname': True, 'some_displayname': True, 'prefix_displayname': True, 'displayname_xx': True, 'XdisplaynameY': True}
accountname:{'xx_accountname': True, 'some_accountname': True, 'prefix_accountname': True, 'accountname_xx': True, 'XaccountnameY': True}
fullname:   {'xx_fullname': True, 'some_fullname': True, 'prefix_fullname': True, 'fullname_xx': True, 'XfullnameY': True}
```

纯键名函数证据（`core/redaction/rules.py:185-196`，全文见下，**函数体内无任何值变量**）：

```python
def is_name_key(key: object) -> bool:
    normalized = normalize_key(key)
    if normalized in _EXACT_NORMALIZED:
        return True
    return any(marker in normalized for marker in _NAME_NORMALIZED)
```

调用点仅 `core/redaction/engine.py:412` / `:414`，两处传参均为 `key`（键名），**未传 `text`（值）**：

```
core/redaction/engine.py:412:        if is_name_key(key) or BIZ_ID_RE.fullmatch(text):
core/redaction/engine.py:414:            token = f"<NAME:{sha8(text)}>" if is_name_key(key) else f"<BIZ:{sha8(text)}>"
```

### 2.4 第 3 组（版本升版）关键输出

```
default version = 'redaction-v2' (expect redaction-v2) -> True
RS_REDACTION_V1 = 'redaction-v1'  RS_REDACTION_V2 = 'redaction-v2'  V1!=V2 -> True
explicit V1 version = 'redaction-v1' -> True
legacy result.redaction_version='redaction-v1' current='redaction-v2'
pkg __all__ has both: True True
rules __all__ has both: True True
same object pkg<->rules: True True
```

三处同步（行号为实测）：

| 落点 | 证据 |
|---|---|
| 引擎默认值 | `core/redaction/engine.py:141` `ruleset_version: str = RS_REDACTION_V2,` |
| `__init__` 导出 | `core/redaction/__init__.py:26-27` 导入 + `:37-38` 列入 `__all__` |
| `rules.py` 版本注释 | `rules.py:17`「`RS_REDACTION_V2` 是**当前默认**」、`:50`「v2 词表即当前默认 `RS_REDACTION_V2`」 |

### 2.5 第 4 组（只升标签、行为不变）关键输出

同一混合载荷（手机号 / 邮箱 / SQL 字面量 / `user=… password=…` / Bearer / 中文业务键 / 业务号）分别在 v1、v2 下脱敏：

```
v1.redaction_version='redaction-v1' v2='redaction-v2'
payload_redacted identical: True
redaction_count v1/v2: 8 8 identical -> True
detected_categories identical: True ['SQL_LITERAL','PHONE','EMAIL','ID_CARD','PERSON_OR_BUSINESS_ID','USERNAME_PASSWORD','TOKEN','LOG_FIELD']
status v1/v2: REVIEW_REQUIRED REVIEW_REQUIRED True
unresolved_spans identical: True 3
blockers identical: True ()
payload_hash v2 == canonical_payload_hash(payload): True
payload_hash v1 == v2: True
only field that differs is redaction_version -> {'redaction_version'}
```

补测 AC-04 哈希口径（`03-task-cards.md` T01 必须语义 #4）：

```
payload_hash == sha256(original): True
payload_hash != hash(redacted) (replacement happened): True
payload_text parity w/ evidence service: True        # 与 core/evidence/service.py::canonical_payload_hash 逐字等价
extra=forbid enforced on RedactionResult: YES ValidationError
```

### 2.6 第 5 组（AC-02 五轮确定性）关键输出

```
redaction_count set: {8} size 1
redaction_version set: {'redaction-v2'} size 1
payload_hash set: {'dfca5df3…76227f'} size 1
payload_redacted hash set size: 1
```

### 2.7 第 6 组（真实 `ToolResult` 通道级）关键输出

`ToolResult` 由**真实契约类**构造（`core/tools/contracts.py`），非手写 dict：

```
model_dump field count: 15
keys: ['capability_ref','data','duration_ms','error','observed_at','run_id','source','source_metadata',
       'started_at','status','task_id','tool_call_id','tool_name','truncated','warnings']
all 15 top-level keys is_name_key False: True
status=CLEAN is_passable=True count=0 spans=0 blockers=() cats=[]
tool_name verbatim: True
payload identical to input (zero rewrite): True
data intact: {'elapsed': 0.42, 'rows_affected': 1}
```

对照（`data` 内含业务号 `order_no` 时，属 R4-D 正例集，**有意命中**、非误伤）：

```
status=REVIEW_REQUIRED is_passable=False data={'order_no': '<NAME:af1fbc1a>', 'rows_affected': 1}
spans: [('PERSON_OR_BUSINESS_ID', '$.data.order_no', 'SEMANTIC_CONFIRMATION_REQUIRED')]
tool_name still verbatim: True
```

### 2.8 第 7 组（KD-W09-02 实测）关键输出

```
payload  = {"definition": {"name": "mysql_lab_order_update"}, "tool_name": "mysql_lab_update_order_note"}
status   = REVIEW_REQUIRED
masked   = <NAME:e533808b>
raw 'mysql_lab_order_update' in payload_redacted serialization: False
is_passable (must be False = fail-closed): False
spans    = [('PERSON_OR_BUSINESS_ID', '$.definition.name', 'SEMANTIC_CONFIRMATION_REQUIRED')]
```

### 2.9 第 8 组（T02 边界未越过）关键输出

```
$ ls core/redaction/gate.py
ls: cannot access 'core/redaction/gate.py': No such file or directory
$ grep -rn "for_tool_result" core/tools/executor.py
（无输出）
$ ls database/migrations/ | tail
007_w07_disclosure.sql  008_w08_verification.sql  __init__.py  __pycache__  rollback
（无 009_*）
$ grep -rn "REDACTION_APPLIED" core/ workers/ apps/
core/audit/events.py:111:    REDACTION_APPLIED = "REDACTION_APPLIED"    # 仅枚举，无发射点（T01 卡 #6 要求）
```

### 2.10 第二轮补测（按 R4-A 验收 #1 补齐显式回归，+37 用例）

首轮复验将「7 词根带前后缀参数化用例未落盘」登记为遗留 1（行为实测正确、显式回归缺失）。本轮按 team-lead 指令补齐，**新增 37 条用例，全部独立成用例**（`@pytest.mark.parametrize` 一条一例，不塞进单个大 `assert`，规避 3.14 断言重写器的长表达式风险）：

| 新增用例 | 条数 | 断言 |
|---|---|---|
| `test_r05_affixed_person_name_roots_still_match` | 7 | `is_name_key(key) is True`（`xx_customer_name` / `some_user_name` / `xx_owner_name` / `prefix_real_name` / `x_display_name` / `y_account_name` / `z_full_name`） |
| `test_r4d_negative_keys_are_not_name_keys` | 29 | `is_name_key(key) is False`（15 个 `ToolResult` 顶层字段 + `name_field`/`database_field`/`schema_field`/`keys_field`/`table_name`/`column_name`/`schema_name`/`index_name`/`file_name`/`class_name`/`host_name`/`field_name`/`definition_name`/`nickname`） |
| `test_r4d_negative_key_set_matches_written_ruling` | 1 | 负例集 `len == 29`、无重复、且 `TOOL_RESULT_FIELDS ⊆ 负例集`（防清单被悄悄删项） |

收集结果（证明一条一例、未被合并）：

```
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_redaction_engine.py -o addopts="" -q -p no:cacheprovider \
    -k "affixed or r4d_negative or r4d_negative_key_set" --collect-only
...
tests/unit/test_w09_redaction_engine.py::test_r4d_negative_keys_are_not_name_keys[definition_name]
tests/unit/test_w09_redaction_engine.py::test_r4d_negative_keys_are_not_name_keys[nickname]

37/168 tests collected (131 deselected) in 0.09s
```

新增用例的**拦截价值**（为何不能只靠 `len(NAME_KEY_MARKERS) == 13`）：若有人把 13 项词表替换为「等价物」但令子串匹配退化（例如改成精确匹配 13 项），裸形式断言与 `len == 13` 仍会全绿，只有这 7 条前后缀变体会转红。命名刻意用 `R4D_NEGATIVE_KEYS` 而非 `SYSTEM_METADATA_KEYS`，避免与「被测模块中不应存在的那 9 项白名单常量」混淆。

---

## ③ 8 组逐组结论

| # | 组 | 结论 | 证据要点 |
|---|---|---|---|
| 1 | R05 冲突闭环（R4-D 裁定 2 正负例表逐条） | **PASS** | 正例 23/23 `True`、负例 29/29（15 个 `ToolResult` 顶层字段 + 14 项元数据键）`False`，零例外；判定仅用 `is_name_key()` 输入输出行为，未比对常量名/分组数；旧常量 `SYSTEM_METADATA_KEYS` / `NAME_KEY_EXACT` / `NAME_KEY_SUBSTRING` 均**不存在**（`hasattr` 全 `False`），测试是 `assert not hasattr(...)`，符合 R4-D 裁定 1 |
| 2 | 欠脱敏缺口（7 词根前后缀变体） | **PASS（行为 + 显式回归）** | 本人脚本实测 35 条变体全 `True`；**本轮已补 7 条带前后缀参数化用例并入套件**（`test_r05_affixed_person_name_roots_still_match`）；`is_name_key` 签名只收 `key`、函数体无值变量、调用点只传键名 → 确证「纯键名函数」 |
| 3 | 版本升版（R4-A 硬要求） | **PASS** | 默认 `redaction-v2`、`RS_REDACTION_V1=="redaction-v1"` 且不相等、显式 v1 可复现 `redaction-v1`、包/`rules.py` 双向导出且同对象、三处同步逐处见面 |
| 4 | 行为不变（只升标签） | **PASS** | 同载荷 v1/v2 下 `payload_redacted` / `redaction_count`(8) / `detected_categories` / `status` / `unresolved_spans` / `blockers` 逐字相同；`model_dump()` 差异集合仅 `{'redaction_version'}`；`payload_hash == canonical_payload_hash(payload)`，且与 `core/evidence/service.py` 实现逐字等价 |
| 5 | AC-02 五轮确定性 | **PASS** | `{count}`/`{version}`/`{hash}` 三个集合大小各 1，`version` 集合 `== {"redaction-v2"}`；`payload_redacted` 哈希集合亦为 1 |
| 6 | 真实 `ToolResult` 通道级不误伤 | **PASS** | 契约构造、15 字段齐全；`CLEAN`、`is_passable=True`、`tool_name` 逐字不变、`data` 未被清空或整体掩码、payload 与入参**逐字相等（零改写）** |
| 7 | KD-W09-02 实测 | **PASS（fail-closed，不判缺陷）** | `REVIEW_REQUIRED` + `<NAME:e533808b>` 掩码 + 原文不出现在 `payload_redacted` + `is_passable=False`；**定性为 fail-closed 保守降级，非泄露**，按 R4-B / design §2.2 KD 块**不判缺陷** |
| 8 | 回归与边界 | **PASS（离线可验部分）** | 单文件 **168 passed**；`tests/unit` **1059 passed / 0 failed**；T02 边界三项均未越过；`test_w03_integrity.py` **环境阻塞**（1 failed / 2 passed / 3 errors，全部 root@localhost 1045），**如实记为未通过、不计入通过** |

**缺陷路由判定：`NoOne`（未发现源码 Bug，未发现测试断言错误）。**

- 未出现「断言期望正确行为、实现给出错误输出」的用例；
- 未出现断言本身写错的用例；
- 首轮登记的 QA 域覆盖缺口（遗留 1）**已于本轮补齐**（+37 条独立参数化用例），该缺口关闭。

---

## ④ 与 R4-D 口径的差异核对

| R4-D 要求 | 实际 | 核对结果 |
|---|---|---|
| 裁定 1：不要求 C 组白名单实体存在；不因「文档写 C 组、代码没有」判回归 | 代码为两组实现（`NAME_EXACT_KEYS` 1 + `NAME_KEY_MARKERS` 13），无 `SYSTEM_METADATA_KEYS` | **符合**；本次**未**据此判 FAIL |
| 裁定 1：不要求 `NAME_KEY_EXACT` / `NAME_KEY_SUBSTRING` 存在 | 两者均不存在；测试为 `assert not hasattr(...)`，是断言「不存在」而非「存在」 | **符合**，不构成缺陷 |
| 裁定 1：判定统一为 `is_name_key()` 输入输出行为 | 本次全部结论仅基于 `is_name_key(k)` 的返回布尔值 | **符合** |
| 裁定 2：正例集 23 条必须 `True` | 23/23 `True` | **符合** |
| 裁定 2：`display_name` / `user_name` / `customer_name` **移出负例、移入正例** | 三者均 `True`；且全仓测试**未残留**任何将它们断言为 `False` 的旧用例（`grep` 复核） | **符合**——QA 第一轮报告中的「误判清单」已随实现与测试一并作废 |
| 裁定 2：负例集 15 个 `ToolResult` 字段 + `name_field` 等 14 项必须 `False` | 29/29 `False`（含 `source_metadata`，其键名本身不含 marker） | **符合** |
| 裁定 2：`source_metadata` 内嵌套 `name` 按 KD-W09-02 处理 | 已实测该形态为 `REVIEW_REQUIRED` + 掩码（第 7 组） | **符合**，fail-closed 不判 FAIL |
| 「只读复核当前状态」唯一未闭合项：`RS_REDACTION_V1` 未升版 | 已升为 `RS_REDACTION_V2 = "redaction-v2"` 并作为默认，`V1` 保留可复现历史行 | **已闭合**（R4-D 该未闭合项现已消除） |
| R4-A 验收 #1：7 词根**参数化**回归（`xx_customer_name` 等 7 条）齐备且全绿 | **已补齐**：`test_r05_affixed_person_name_roots_still_match`（7 条）+ `test_r4d_negative_keys_are_not_name_keys`（29 条）+ 清单一致性 1 条，共 +37 断言独立成例，全绿（单文件 168 passed） | **已闭合**（首轮登记的覆盖缺口关闭，见 2.10） |
| 设计 §2.2 R05 行与代码是否一致 | `02-design.md:139` 已改为「A 组精确 1 项 + B 组受限子串 13 项 + 无元数据白名单」，与 `rules.py:52-67` 逐项一致 | **一致** |
| 设计 §2.2 KD-W09-02 块是否落地 | `02-design.md` 已有 KD-W09-02 段落（现象 / fail-closed 定性 / 影响面 / 不引入路径感知 / T02 组合层纪律 / 升级路径） | **一致** |
| R3 回归（R06/R10/R11 三类）是否已覆盖 | `test_w09_redaction_engine.py` 的 R3 段（当前 `:739-852`）有 `R3_BLOCKED_SAMPLES`（3 个纯净样本）+ 6 个参数化用例 = 18 条，覆盖 R3 验收 #1–#6 全部 | **已齐，无需新增**（逐条见 5.3；**判据以用例名为准，行号随文件增长会漂移**） |
| R3 占位符形态：取**类别值**而非规则号 | 全仓 `grep "BLOCKED:R0\|BLOCKED:R1"` **零命中**；实现 `engine.py` 为 `f"<BLOCKED:{category.value}>"`，测试 `:781` 断言 `payload_redacted == {key: f"<BLOCKED:{category}>"}`；设计 `:140/:144/:145/:149` 已统一为类别值口径 | **一致**（`<BLOCKED:R06/R10/R11>` 形态在代码、测试、设计中**均不存在**，无需作废任何快照） |

**结论：正例集已按要求移对、负例集已作废 QA 第一轮的误判清单，两组口径全项吻合。**

---

## ⑤ 遗留与已知限制

### 5.1 已知限制 KD-W09-02（接受，不判缺陷）

- **现象**：嵌套裸 `name` 键（如 `definition.name`）命中 A 组精确规则 → `REVIEW_REQUIRED` + `<NAME:sha8>` 掩码。
- **定性**：**fail-closed（保守降级，不泄露原文）**，非「欠脱敏」。实测原文确实不出现在 `payload_redacted`，`is_passable=False` 故不会流向任何下游通道。
- **影响面（本人补充实测）**：扫描 `adapters/**/*.py` 中 180 个字面量字典键，命中 R05/R12 的**仅 `"name"` 一个**，且分布于 `ToolDefinition.model_copy(update={"name": ...})`、`TargetObjectSpec` 声明、MCP `tools/call` 请求体等**契约对象序列化点**，不在工具结果的业务 `data` 里。因此 R4-B 的「T02 组合层纪律」（`data` / `source_metadata` 只放业务键值，契约对象走结构化字段）**是当前唯一有效的规避手段**，T02 必须遵守。

### 5.2 环境项（阻塞、非缺陷）

1. `tests/integration/test_w03_integrity.py`：3/6 用例 teardown 因 root@localhost 1045 报 `DatabaseAuthError`。**未通过、未计为通过**；R2（委托重构后哈希逐字节不变）的验收须在有库环境补跑。
2. PG 5432 不可达：本次未涉及，如实登记。

### 5.3 遗留风险（3 条：1 条已关闭、1 条待裁定、1 条设计文本缺口，均不阻塞 T01 结论）

1. **~~测试覆盖缺口~~ → 已补（本轮关闭）**：R4-A 验收 #1 的 7 条带前后缀词根参数化用例已落入 `test_w09_redaction_engine.py::test_r05_affixed_person_name_roots_still_match`（7 条），并把 R4-D 裁定 2 的 29 条负例同步落成 `test_r4d_negative_keys_are_not_name_keys`（29 条）+ 清单一致性 1 条，合计 **+37 条独立用例**；单文件 **168 passed**、`tests/unit` **1059 passed / 0 failed**。**该缺口关闭，转为常规回归资产。**
2. **真实工具结果含业务号会被隔离（T02 卡未覆盖，已上报 team-lead 待裁，非缺陷）**：`data` 中若出现 `order_no` / `biz_id` / `customer_id`，按 R4-D 属**有意命中** → `REVIEW_REQUIRED`、`is_passable=False`，T02 闸门①接入后该结果会进隔离区。这是设计选择而非缺陷，但**可用性影响真实存在**。经 PM 复核：T02 卡最接近的 `:217`（闸门②语义）与 `:252`（通道级不误伤，只断言真实 `model_dump()` → `CLEAN/REDACTED`）**均不涉及**「`data` 携带业务号 → 整体进隔离」的落地风险；PM 已把 1 行补充草案上报 team-lead 待裁，批准后落盘。→ **状态标记为「已上报、待裁定」，不写成已闭环。**
3. **`<BIZ:…>` 语义标签偏斜（设计文本缺口，非代码 Bug）**：`engine.py` 的三元表达式为**键名命中优先**，故 `order_no`（B 组 marker）必然产 `<NAME:…>`；`<BIZ:…>` 只在「键名未命中 + 值匹配 `^[A-Z]{2,6}\d{6,}$`」时出现。设计 `:139` 产出列只并列写 `<NAME:sha8>` / `<BIZ:sha8>`，未写优先级 → 属设计**文本**缺口。**复验口径**（已按此落到用例）：断言时按 `is_name_key(key)` 分支取期望值（业务号键 → `<NAME:…>`），**不得**断言 `<BIZ:…>`；`<BIZ:…>` 另用「非人名键 + 值形如 `AB1234567`」的样本覆盖（见 ⑨ 补测说明）。规则纠偏留 W10，不为它单独升版。
3. **~~文档遗留（R3 改文待落盘）~~ → 已落盘（本轮关闭）**：架构师/PM 本轮已把 `docs/w09/02-design.md:140/:144/:145` 的 R06/R10/R11 改写为「**不做可用化改写**：插入固定 `<BLOCKED:CATEGORY>` 占位（取 `SensitiveCategory` 类别值）抹除原串，**占位不计入 `redaction_count`**；原串仅以 `SensitiveSpan` 定位信息保留」；`:149`（「无法可靠脱敏」第 1 条）补「`BLOCKED` 不等于原样外发，必须已抹除原串」；T02 卡 `:217`（闸门②）与 `:244`（R3 占位语义 + 只复用不重写）亦已写入。**该文档缺口关闭**，本项仅作历史记录。

**R3 核实明细（逐条对照 `05-rulings.md` R3「可机器验证的验收方式」）**

> 位置取当前快照（文件现 **852 行**，R3 段 `:739-852`）。**判据以用例名为准**——T02 卡 `:244` 已明确「行号随文件增长会漂移，不作为判据」，`:167` 另有全卡级注记「卡内行号均为当前快照（以符号名 + 相对位置为准）」，与本报告口径一致。

| R3 验收项 | 现有用例（`tests/unit/test_w09_redaction_engine.py`） | 结论 |
|---|---|---|
| #1 `raw_secret not in payload_redacted` | `test_r3_1_blocked_raw_secret_not_in_payload`（`:767`，3 类参数化） | **在，已覆盖** |
| #2 `<BLOCKED:...>` 占位与类别一一对应 | `test_r3_2_blocked_placeholder_matches_category`（`:774`；`:781` 断言 `payload_redacted == {key: f"<BLOCKED:{category}>"}`） | **在，已覆盖** |
| #3 `redaction_count` 不因 BLOCKED 增加 | `test_r3_3_blocked_does_not_increment_redaction_count`（`:787`，对照 `clean.redaction_count == 0`） | **在，已覆盖** |
| #4 `status is BLOCKED` 且 span 非空、闸门不放行 | `test_r3_4_blocked_status_and_spans_are_recorded`（`:798`） | **在，已覆盖** |
| #5 同输入 5 轮 `payload_redacted` 完全相同 | `test_r3_5_blocked_payload_is_identical_over_five_runs`（`:811`） | **在，已覆盖** |
| #6 隔离定位信息不含原文片段 | `test_r3_6_spans_carry_only_locators_not_raw_fragments`（`:823`；`:838` 断言 `"<BLOCKED:" not in locator`） | **在，已覆盖** |

→ 6/6 齐备；`R3_BLOCKED_SAMPLES`（`:744`）为「纯净样本」（每类只命中自身一条规则），已避免 `SAMPLES` 中连接串额外命中邮箱导致 `redaction_count` 测不准。**结论：R3 无需补测。**

**占位符形态**：`<BLOCKED:CONNECTION_STRING>` / `<BLOCKED:PRIVATE_KEY>` / `<BLOCKED:CERTIFICATE>`（**类别值**）。全仓 `grep "BLOCKED:R0\|BLOCKED:R1"` **零命中**，`<BLOCKED:R06/R10/R11>` 这种规则号形态在代码、测试、设计中均不存在，本报告亦从未使用。`test_r3_6:838` 进一步断言 locator 中连 `"<BLOCKED:"` 都不出现，即占位符只存在于 `payload_redacted`，不进定位信息。

### 5.4 附：非本次范围但需知情

`docs/w08/02-design.md`、`docs/w08/03-task-cards.md` 的改动**已由他人在 `8bb87a4 W08-doc: T06 调度边界裁定归档（tag 不移动）` 归档提交**，当前 `git status --porcelain docs/w08/` 为空、`git diff HEAD -- docs/w08/` 无差异 → **该项已闭环，无需 team-lead 再处置**。该提交**非本次复验所出**（本次未执行任何 `git add` / `git commit`）。

### 5.5 本轮写入范围（team-lead 授权范围内）

| 文件 | 改动 | 授权 |
|---|---|---|
| `tests/unit/test_w09_redaction_engine.py` | +37 条独立参数化用例（7 词根变体 + 29 负例 + 1 清单一致性），无删除、无改写既有用例 | team-lead 明确授权 |
| `docs/w09/06-qa-report.md` | 本报告 | 本任务指定产出 |

未改源码（`core/redaction/**` 零改动）、未改 `docs/w08/**`、未改 `05-rulings.md`、未 `git add` / `git commit`。

---

## ⑥ 总结论

1. **R1 冲突已闭环（含欠脱敏缺口）**：R4-D 裁定 2 的正例 23 条全部 `True`、负例 29 条全部 `False`，`display_name` / `user_name` / `customer_name` 已正确移入正例集且旧「误判清单」全仓无残留；7 个人名词根的前后缀变体（本人脚本实测 35 条全 `True`）已固化为套件内 7 条参数化用例，`is_name_key` 经源码与调用点双重核实为**纯键名函数**（不读值、无启发式）。
2. **版本升版已落地**：默认 `redaction-v2`，`redaction-v1` 保留并可显式复现历史行，三处同步一致；**只升标签、行为不变**（v1/v2 结果除 `redaction_version` 外逐字相同，`payload_hash` 与 `canonical_payload_hash(payload)` 一致且与 W03–W08 现有实现逐字等价）。
3. **AC-02 五轮确定性成立**（三个集合大小各 1，版本集合 `== {"redaction-v2"}`）；**真实 `ToolResult` 通道级零改写、`CLEAN`、`passable=True`**，R1 描述的「整条执行链被降级」已不再复现。
4. **KD-W09-02 为 fail-closed 保守降级，非泄露，按裁定不判缺陷**；但其真实触发点（`ToolDefinition` / `TargetObjectSpec` 序列化）要求 T02 严格执行 R4-B 组合层纪律。
5. **回归**：`tests/unit` **1138 passed / 0 failed**（连跑 2 次一致；该总数随工程师并行新增用例增长）、引擎单文件 **173 passed**（含本轮新增 42 条：7 词根变体 + 29 负例 + 清单一致性 1 + R05 值规则 `<BIZ:>` 5）、`test_w09_redaction_gate.py` **44 passed**（含三态六字段用例 5 条）、`test_w09_redaction_wiring.py` **30 passed**；`test_w03_integrity.py` 因 DB 认证阻塞**未通过**（环境项，不计入通过）。**R3 六条验收已全覆盖、无需补测；占位符为类别值形态（`<BLOCKED:CONNECTION_STRING>` 等），全仓无 `<BLOCKED:R06>` 类规则号形态残留。**
6. **未发现源码 Bug、未发现测试断言错误** → 智能路由 **NoOne**（就 T01 引擎范围）。遗留见 5.3 三条：R3 文档已落盘并关闭、`docs/w08/**` 已归档提交；「业务号进隔离」**已上报 team-lead 待裁**（不写成已闭环）；`<BIZ:>` 标签偏斜属**设计文本缺口**，纠偏留 W10。

> 本报告 T01 部分到此结束。**T02a（闸门）已开工并完成只读抽验，见 ⑧①–④ 与 ⑩（`tenant_id` 降级路径，修复后 6 场景全 PASS）**——但抽验均为**离线假仓储镜像**，AC-05（`evidence` 表 0 新增行）/AC-06（下游未被调用）/AC-08（证据行与事件同源）的**真实库端到端**仍未验证；T03–T06 亦未开工。故本节结论不得外推为 T02 或 W09 整体结论。

---

## ⑦ D1 判定：`ToolResult` 新增 `redaction` 字段后的字段集断言

**背景**：T02a 给 `ToolResult` 追加 `redaction: RedactionSummary | None = None`（`core/tools/contracts.py`，置于字段表末尾）。原断言 `set(TOOL_RESULT_FIELDS) == set(ToolResult.model_fields)` 与新契约事实互斥，故 `test_r05_tool_result_field_list_matches_model_exactly` 被改为：

```python
assert len(TOOL_RESULT_FIELDS) == 15
assert set(TOOL_RESULT_FIELDS) <= set(ToolResult.model_fields)
assert set(ToolResult.model_fields) - set(TOOL_RESULT_FIELDS) == set(T02_ADDED_TOOL_RESULT_FIELDS)  # ("redaction",)
```

### 判定：**契约扩展的必要同步，不是放宽断言**

| 维度 | 结论 |
|---|---|
| 该用例原本保护什么 | R4-D 负例集的「15 个 `ToolResult` 顶层字段不得删除 / 改名」。`<=` **精确保留**这一语义（少任何一个立即转红） |
| 原等号是否仍应保留 | **不应**。等号是「契约冻结在 15」时代的产物；字段数变 16 后继续写 `== 15` 是**事实错误**，不是更强的断言 |
| 新断言是否补回闭包性 | **是**。`set(model_fields) - set(TOOL_RESULT_FIELDS) == {"redaction"}` 锁住「只允许多出这一个」，任何额外字段注入会失败 —— 比单纯改成 `<=` 强得多，避免了「只放宽不设界」 |
| 是否触及 R4-D 23 正 / 29 负 | **否**。正例、负例两组参数化用例原样保留、全部仍绿；R3 六用例亦未被动 |

### 但**仍不够强**，建议工程师补 1 条断言（等价写法）

理由：当前只锁住字段**名字**，没锁**类型/默认值**；且允许集来自测试内部常量 `T02_ADDED_TOOL_RESULT_FIELDS`，后来者往该元组追加一项即可让用例转绿 —— 这是「自我放宽」的口子（软门）。

```python
def test_t02_added_field_is_pinned_to_redaction_summary() -> None:
    """T02 追加的唯一字段必须就是 `redaction`，且类型/默认值一并钉死。"""
    from core.redaction.models import RedactionSummary

    extra = set(ToolResult.model_fields) - set(TOOL_RESULT_FIELDS)
    assert extra == {"redaction"}                      # 不依赖可被追加的常量
    field = ToolResult.model_fields["redaction"]
    assert field.annotation == (RedactionSummary | None)
    assert field.default is None                       # 必须可缺省，否则既有构造点全崩
    assert field.is_required() is False
```

### 附带：工程注释一处**不准确**（非断言问题，风险低）

注释写「新增字段刻意放在字段表末尾，既有 `model_dump()` 键序/哈希向量零漂移」。**前半句对，后半句对 `model_dump()` 不成立**：

- 实测 `ToolResult(...).model_dump()` 现含 `redaction: None` 共 **16 键**，其 `canonical_payload_hash` 与剔除该键的版本**不同**（`1c105f53…` vs `cb1d8b8e…`）。
- **但当前无实际风险**：全仓 `grep` 未发现对 `ToolResult.model_dump()` 计算哈希的调用点；`action_digest` 取自 `action_descriptor`（参数/目标对象），与本模型无关；`StepObserved.result` 落 `result_json`（普通 JSON 列，迁移里无 `result_hash`/生成列）。
- **建议**改为「字段末尾追加；`model_dump()` 会多出 `redaction: null`，当前无调用点对该 dump 计算哈希，故无向量漂移」，避免后人误以为「dump 不变」。**不作 FAIL**，登记为文档准确性项。

---

## ⑧ T02a 抽验（只读，四项）

> 抽验**首次运行遇 7 failed**，根因 `TypeError: RedactionGate.audit_payload() got an unexpected keyword argument 'source_payload_hash'`（`gate.py:387` 调用处 vs `:437` 签名，属并发编辑中间态）。工程师于 **23:07:57** 修复 `gate.py` 后重跑 **35 passed**（本轮补三态用例后 **38 passed**）。以下结论基于**修复后的稳定态**（全量 `tests/unit` 连跑 2 次 **1138 passed / 0 failed**）。该中间态**不计为缺陷**，仅记录。
>
> **另一处并发瞬时态（同一处理）**：复跑期间曾出现 1 次 `FAILED test_w09_redaction_wiring.py::test_gate3_context_builder_renders_clean_bundle`（全量下 1128 passed / 1 failed），但该文件**单跑 30/30 通过**、随后**全量连续 2 次均全绿**、不可复现 —— 判定为工程师同时写文件导致的读取瞬时态，**不计为缺陷，也不记为 flake**（未观察到稳定复现路径）。**该线索保留**：若 CI 再复现，须按「共享状态/测试隔离」单独排查，不要当 flake 忽略。

| # | 抽验项 | 结论 | 证据 |
|---|---|---|---|
| 1 | `tests/unit/test_w09_redaction_gate.py` 是否**真离线** | **PASS（附一处措辞校正）** | 全文无 `DG_ADMIN_DSN` / `os.environ` / `pymysql` / `connect(` / `MysqlClient(...)` 实例化；DB 交互全部用 `FakeAudit` / `FakeQuarantine` / 假仓储计数。**唯一** `database.*` 引用是 `test_migration_009_is_pickable_by_migrate_py` 内的 `from database.migrations import ...`，该模块虽 `import MysqlClient`（**仅类型注解用**），但测试只做 `Path.read_text` 静态解析 SQL —— 无连接、无 DSN。故「真离线」成立；「无 DB 客户端 import」字面上不完全成立 |
| 2 | 隔离区写入参数**不含原文** | **PASS** | `core/redaction/quarantine.py::insert` 只取 `result` 元数据：`status.value` / `payload_hash`(SHA-256) / `detected_categories` / `unresolved_spans`（经 `_span_located` 仅留 `category/json_path/start/end/reason`）/ `blockers` / `redaction_version` / `created_at`；`payload_redacted` **完全不参与**。运行期证明：`REVIEW_REQUIRED` 时隔离区实参仅 `['channel','result','task_id','tenant_id']`，标量入参中原文 `张三` **不出现**，`payload_redacted` 未被传入，span 为 `('$.data.name', 0, 2, 'SEMANTIC_CONFIRMATION_REQUIRED')` 纯定位 |
| 3 | `source_payload_hash` 与 `payload_hash` **语义分离** | **PASS** | `EvidenceCreate.source_payload_hash` 注释即「仅落库，**不参与** `payload_hash` 计算」；`gate.for_evidence` 以 `original_hash` 透传、**不写入** `payload`/`redacted_payload`；`service.py:92` 的 `payload_hash = canonical_payload_hash(request.payload)` 算的是**实际落库（已脱敏）**载荷，`source_payload_hash` 由 `:135` 单独赋给独立列；`repository.py:93` INSERT 含 `source_payload_hash, redacted_payload_hash, redaction_version` 三个独立列。`source_payload_hash` 从不进入 `canonical_payload_hash` 入参 |
| 4 | 闸门①三态是否**不动 `ToolStatus`** | **PASS（附范围说明）** | `for_tool_result` 的 `update` 字典**从不含 `status`**。运行期证明：`SUCCESS` + 手机号 → 仍 `success`、`truncated=False`、`data={'phone':'<PHONE:38aed904>'}`、原文不残留；`PARTIAL` + 嵌套 `name` → 仍 `partial`、`truncated=True`、`data={}`、隔离区 1 行。**范围说明**：放行路径除 `data`/`redaction` 外还会替换 `warnings` / `source_metadata` / `error`（与 `_tool_result_payload` 只扫 `data/warnings/source_metadata/error` 一致，属正确设计），`truncated` **仅在不可放行时**置 `True`（不伪造截断）—— 比「只改 3 个字段」的描述更宽，但都服务于「不留原文」，且 `ToolStatus` 确实零改动 |

附带核对通过：审计载荷六字段齐备且无原文 —— `{"redaction_version":"redaction-v2","source_payload_hash":…,"redacted_payload_hash":…,"status":"REDACTED","detected_categories":["PHONE"],"redaction_count":1}`；`redaction_version` 与证据行同源（AC-08 的**离线镜像**，真实库端到端仍未验证）。

---

## ⑨ 补测：R05 值规则 `<BIZ:…>` 分支（+5 用例，补既有覆盖空白）

抽验中发现**全仓无任何 `<BIZ:` 断言** —— R05 的**值规则** `^[A-Z]{2,6}\d{6,}$` 分支此前**零测试覆盖**（既有 `SAMPLES` 的 R05 样本走键名路径）。按 PM 建议的复验口径补齐，单文件 168 → **173 passed**：

| 新增用例 | 条数 | 锁定内容 |
|---|---|---|
| `test_r05_biz_id_value_rule_emits_biz_token_for_non_name_key` | 1 | 非人名键 `ref` + 值 `AB1234567` → `<BIZ:…>`、`count==1`、`REVIEW_REQUIRED`、命中 `PERSON_OR_BUSINESS_ID` |
| `test_r05_name_key_takes_priority_over_biz_id_value_rule` | 1 | **键名优先**：同值在 `order_no` 下产 `<NAME:…>` 而非 `<BIZ:…>`，且两者哈希段相同（差异只来自键名判定） |
| `test_r05_biz_id_value_rule_boundaries_do_not_match` | 3 | `AB12345`（数字段 5 位）/ `ab1234567`（小写前缀）/ `ABCDEFG1234567`（前缀 7 位）→ 不命中、`CLEAN`、`count==0` |

运行期实测（补测依据）：

```
{"ref": "AB1234567"}      -> {'ref': '<BIZ:d3225756>'}       status=REVIEW_REQUIRED count=1
{"order_no": "AB1234567"} -> {'order_no': '<NAME:d3225756>'} status=REVIEW_REQUIRED count=1   # 哈希段相同
AB12345 / ab1234567 / ABCDEFG1234567 -> 原样保留, CLEAN, count=0
```

> 这 5 条把 PM 指出的「`<NAME:>` vs `<BIZ:>` 设计文本缺口」变成**可执行断言**：若日后把设计措辞改成「值匹配优先」，用例会立即转红并指向 `02-design.md:139`。
>
> 补充：设计 `:139` 现已补上「**键名命中优先于值匹配**」半句（含 `engine.py:412-414` 的三元表达式引用），与本报告 5.3-3 的定性一致。team-lead 已将「`order_no` 产 `<NAME:…>`」定为 **W10 观察项**，本轮不进卡、不当缺陷。

---

## ⑩ 追加抽验：`tenant_id` 降级路径（PM 新增判据 `T02:189-191`）

**判据**（PM 转述 team-lead 裁定，已并入 T02 卡 `:189-191`）：`for_model_messages` / `for_document` 的 `tenant_id` 可选是为了兼容无租户上下文；T03/T06 调用时必须传；缺失时 gate 仍需发 `REDACTION_REVIEW_REQUIRED`，且 payload 带 `quarantine_skipped: true`（降级留痕）。

### 实测（`for_model_messages` + 校验位非法的 18 位证件号 → `REVIEW_REQUIRED`）

> **时间线（看闭环用）**：① 首次抽验（`gate.py` mtime 23:07）→ 降级路径**无** `quarantine_skipped`，登记为「卡要求 vs 实现未落地」并路由 Engineer；② 工程师 **23:24:44** 落地 `gate.py:412-423`；③ 本人**复跑全部六种场景**，结论如下。

**修复后实测（6 场景全覆盖）**

| # | 场景 | 隔离区 | 事件 | `quarantine_skipped` | `channel` | 结论 |
|---|---|---|---|---|---|---|
| A | **不传 `tenant_id`**（降级主因） | **0 行** ✓ | `REDACTION_REVIEW_REQUIRED` 仍发出 ✓ | **`True`** ✓ | `MODEL_MESSAGES` ✓ | **PASS** |
| B | **传 `tenant_id="t1"`**（正向） | **1 行** ✓ | `REDACTION_REVIEW_REQUIRED` ✓ | **键不存在** ✓ | 不存在 ✓ | **PASS** |
| C | **未装配 `quarantine`**（传了 tenant） | **0 行** ✓ | 仍发出 ✓ | **`True`** ✓ | `MODEL_MESSAGES` ✓ | **PASS**（触发面比卡文更宽，属更保守） |
| D | `REDACTED`（手机号） | 0 行 | `REDACTION_APPLIED` | 不存在 ✓ | 不存在 ✓ | **PASS**（六字段不变） |
| E | `CLEAN`（普通文本） | 0 行 | `REDACTION_APPLIED` | 不存在 ✓ | 不存在 ✓ | **PASS**（六字段不变） |
| F | `BLOCKED`（连接串） | 0 行 | `REDACTION_BLOCKED` | 不存在 ✓ | 不存在 ✓ | **PASS**（六字段不变） |

- 正向 payload 实测恰为**六字段**：`detected_categories / redacted_payload_hash / redaction_count / redaction_version / source_payload_hash / status` —— 契约未变。
- 降级 payload 为**八字段**（六 + `quarantine_skipped` + `channel`）—— 只在跳过时追加，**无默认 `False`**，故「正向不应出现该键」严格成立。
- 六种场景原文 `123456789012345678` **均未进入事件 payload**。
- **PM 预警的两个「才是真缺陷」情形均已排除**：`BLOCKED` 不带该键（场景 F）、正向不带该键（场景 B）。
- **回归锚点（引用例名，比行号稳）**：场景 A ↔ `test_quarantine_skip_is_flagged_in_event_when_tenant_missing`；场景 C ↔ `test_quarantine_skip_flagged_when_repository_missing`；场景 B ↔ `test_quarantine_not_flagged_when_written`（均在 `tests/unit/test_w09_redaction_wiring.py`）。场景 D/E/F 的六字段等值断言由本轮新增的 `test_three_states_keep_exactly_six_audit_payload_fields` 守住。

### 修复前的差异项（保留记录，已闭环）

- **当时现状**：`grep quarantine_skipped` 全仓仅命中 `docs/w09/03-task-cards.md:191`，`core/**` 与 `tests/**` 均无；降级路径 payload 只有六字段。
- **当时判定**：属**实现缺失（工程师侧）**、非卡错 → 路由 Engineer；**未放宽或删除任何断言**。
- **现状**：已落地于 `gate.py:412-423`，逻辑为 `needs_quarantine = status is REVIEW_REQUIRED` / `will_quarantine = needs_quarantine and quarantine 已装配 and bool(tenant_id)` / `quarantine_skipped = needs_quarantine and not will_quarantine`，**仅跳过时**追加键。与卡 `:189-191` 一致（实现是卡文的**超集**：多覆盖「未装配 `quarantine`」这一保守情形），**卡文无需改动**。
- **断言口径建议（后续 T02 复验照此写）**：降级一律断言「**跳过即真**」（`quarantine_skipped is True`），**不要**写成「仅 `tenant_id` 缺失才为真」；payload 键集合用**包含式**断言（因跳过时多一个 `channel`），勿断言「恰为 7 项」。

### 覆盖情况：降级/未装配/正向三面**已有回归**，仅「三态六字段键集合」缺，已补

> **更正**：本报告上一版曾写「`quarantine_skipped` 零测试覆盖」——**该表述错误**，起因是我只 `grep` 了 `tests/unit/test_w09_redaction_gate.py`（该文件确实零命中），属**grep 范围偏窄**。经 PM 指出后全仓复核，覆盖实际存在：

| 面 | 现有回归锚点（**以用例名为准**） | 覆盖内容 |
|---|---|---|
| 降级（`tenant_id` 缺失） | `test_w09_redaction_wiring.py::test_quarantine_skip_is_flagged_in_event_when_tenant_missing`（`:596`） | `insert_calls == 0` + `quarantine_skipped is True` + `channel == REPORT` + 事件仍发 |
| 未装配 `quarantine` | `...::test_quarantine_skip_flagged_when_repository_missing`（`:611`） | 仓储未装配 → `is True`（即本报告场景 C 的「触发面更宽」已被守住） |
| 正向（已写入隔离区） | `...::test_quarantine_not_flagged_when_written`（`:623`） | `insert_calls == 1` + `"quarantine_skipped" not in payload` |
| 真实库正向（待环境） | `test_w09_redaction_pipeline.py:241` | `not in review[-1].payload` + span 键集合 `{category,json_path,start,end,reason}` |

→ 因此「gate 行为落在 wiring 层测」是**分层选择，不是缺口**；我此前的结论与之不符，特此更正。

**唯一真缺口（已补）**：三态 `CLEAN` / `REDACTED` / `BLOCKED` 的 payload **键集合等值**断言。既有 `test_audit_payload_has_exactly_the_six_required_fields` 只覆盖 `REDACTED` 一种；`CLEAN` 与 `BLOCKED` 仅有「不含该键」的**否定式**（且 `BLOCKED` 无显式用例），缺「恰为六字段」的**等值**约束 —— 将来若把 `channel` / `quarantine_skipped` 带进正向三态，无人拦截。

已新增（`tests/unit/test_w09_redaction_gate.py`，**只增不删**，gate 测试 35 → **44 passed**；我提交 3 条，工程师并发把 `BLOCKED` 扩为 R06/R10/R11 三个纯净样本共 5 条，并加 `len(detected_categories) == 1` 纯净性守护）：

```python
@pytest.mark.parametrize(
    ("expected_status", "content"),
    [("CLEAN", …), ("REDACTED", …),
     ("BLOCKED", CONNECTION_RAW), ("BLOCKED", PRIVATE_KEY_RAW), ("BLOCKED", CERTIFICATE_RAW)],
    ids=["CLEAN", "REDACTED", "BLOCKED_R06", "BLOCKED_R10", "BLOCKED_R11"],
)
def test_three_states_keep_exactly_six_audit_payload_fields(...):
    assert set(payload) == {redaction_version, source_payload_hash,
                            redacted_payload_hash, status,
                            detected_categories, redaction_count}
    assert "quarantine_skipped" not in payload
    assert "channel" not in payload
    if expected_status == "BLOCKED":
        assert len(payload["detected_categories"]) == 1   # 纯净样本守护
```

采用 `for_model_messages` 统一入口（三态均可在同一入口触发；`BLOCKED` 抛 `RedactionBlockedError` 但事件仍先发出），与 wiring 三面**不重复**。降级/正向两面继续以 wiring 用例为锚点，不再复制。

### 隔离区列口径核对（避免假失败）

- `database/migrations/009_w09_redaction.sql:19-31` 逐列核对：**11 列** = `quarantine_id / tenant_id / task_id / channel / status / payload_hash / detected_categories(JSON) / unresolved_spans_json(JSON) / blockers(JSON) / redaction_version / created_at`。
- `json_path` / `start` / `end` **不是独立列**，只存在于 `unresolved_spans_json` 的 JSON 元素里（`_span_located` 产 `{category,json_path,start,end,reason}`）。按扁平列写 SQL 或断言键名会**假失败**，本报告与后续 T02 复验一律按 JSON 内字段断言。

---

## ⑪ T02 终验（独立实跑，不采信回传数字）

> 结论口径：**T02 离线镜像 + 真实库端到端见下列 4 条 integration 用例**。本报告**不**宣称 W09 整体通过；T03–T06、W10 与 AC-06 下游业务链路均未在本轮验证（见 11.6）。

### 11.1 五条命令实跑（原文汇总行）

```
$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_redaction_gate.py     -o addopts="" -q -p no:cacheprovider
44 passed in 0.17s

$ .venv/Scripts/python.exe -m pytest tests/unit/test_w09_redaction_wiring.py   -o addopts="" -q -p no:cacheprovider
30 passed in 0.23s

$ .venv/Scripts/python.exe -m pytest tests/integration/test_w09_redaction_pipeline.py -o addopts="" -q -p no:cacheprovider -rs
4 passed in 2.28s          # -rs 无 skip 行 → 未走 skip 分支，真实连库

$ .venv/Scripts/python.exe -m pytest tests/unit                             -o addopts="" -q -p no:cacheprovider
1138 passed in 5.58s

$ .venv/Scripts/python.exe -m pytest tests/integration/test_evidence_service.py -o addopts="" -q -p no:cacheprovider
6 passed, 6 errors in 4.77s     # 6 个 error 全为 teardown（admin_client → root@localhost 1045）
```

- `test_evidence_service.py` 的 6 个 `error` **全部发生在 teardown**（`admin_client` 清理需管理员账号，本机 root 报 1045），**用例本体 6 passed**。→ `payload_hash` 冻结口径的用例确实跑通；代价是**清理失败、残留行未被删除**（本机环境限制，未做任何 DELETE）。
- `test_w09_redaction_pipeline.py` 用 `control_client`（非 root 控制账号）连接，`control_db` fixture 的 `pytest.skip` 分支**未触发**（`-rs` 无输出）。

### 11.2 真实库自证（我自己连库，不依赖用例自述）

```
SELECT 1 -> [{'ok': 1}]                                  # 控制库确实可连
quarantine 列数: 11  ['quarantine_id','tenant_id','task_id','channel','status','payload_hash',
                      'detected_categories','unresolved_spans_json','blockers','redaction_version','created_at']
evidence 有 source_payload_hash: True
最近 3 行: status=REVIEW_REQUIRED, redaction_version='redaction-v2', created_at 15:31 UTC (本地 23:31)
最近 20 行的 JSON 列：'张三' / '13812345678' / 'mysql://' / '-----BEGIN' / 'dg_ro' / '127.0.0.1' 全部 False
样本 spans 键集合: ['category','end','json_path','reason','start']
```

→ 真实库、真实表结构、真实历史行；隔离区确无原文，`json_path/start/end` 只在 JSON 内。

### 11.3 闸门③④「下游未被调用」——用调用计数证明，不只看抛错

**闸门③（`ContextBuilder.render_messages`）**：对 `ContextBundle.model_dump_for_prompt` 做类级计数探针（渲染器入口）：

| 载荷 | 结果 | 渲染器调用计数 |
|---|---|---|
| `BLOCKED`（连接串） | `RedactionBlockedError` | **0** |
| `REVIEW_REQUIRED`（业务号 `AB12345678`） | `RedactionReviewRequiredError` | **0** |
| `CLEAN` | 返回 2 条消息 | 1 |

**闸门④（`ModelGateway.generate`）**：`FakeModelAdapter.requests` 计数：

| 载荷 | 结果 | `adapter.requests` |
|---|---|---|
| `BLOCKED`（连接串） | `RedactionBlockedError` | **0** |
| `REVIEW_REQUIRED`（校验位非法 18 位） | `RedactionReviewRequiredError` | **0** |
| `CLEAN` | 返回响应 | 1 |

**③→④ 组合链路**：违规 bundle 在闸门③ 即抛 `RedactionBlockedError`，下游 `adapter.requests == 0` —— 证明「渲染未发生、适配器未调用」，而非「调用后才发现问题」。

### 11.4 轨迹通道（`TrajectoryRecorder.record`）

| 载荷 | 落盘 payload | 判定 |
|---|---|---|
| `BLOCKED`（`note` 键 + 连接串） | `{"redaction": "BLOCKED"}` | **PASS**（恰为固定占位，原文零残留） |
| `BLOCKED`（`detail` 键 + 私钥） | `{"redaction": "BLOCKED"}` | **PASS** |
| `CLEAN`（`{"tool":"x","rows":1}`） | `{"tool":"x","rows":1}` | **PASS**（不变） |
| `REDACTED`（手机号） | `{"tool":"x","phone":"13812345678"}` | **⚠ 见 11.5** |
| `REVIEW_REQUIRED`（裸 `name`） | `{"tool":"x","name":"张三"}` | **⚠ 见 11.5** |

> 注意：`{"dsn": CONN}` 这类**敏感键名**会在旧链 `redact_payload` 阶段先被掩成 `<redacted>`，引擎随后看到的是干净值 → **不会**触发 BLOCKED 占位（该路径安全，但也不产生占位）。真正的 BLOCKED 路径要落在**非敏感键名**下（上表用 `note` / `detail`）。

### 11.5 发现项（需裁定，非我单方判 FAIL）

**轨迹通道对 `REDACTED` / `REVIEW_REQUIRED` 丢弃引擎脱敏产物，手机号与姓名原文仍落盘。**

实测对照（同一次调用内）：

```
旧链 redact_payload 产物 : {"tool": "x", "phone": "13812345678", "name": "张三"}
引擎判定                 : REVIEW_REQUIRED
引擎脱敏产物             : {"name": "<NAME:1d841bc0>", "phone": "<PHONE:38aed904>", "tool": "x"}
实际落盘 payload         : {"tool": "x", "phone": "13812345678", "name": "张三"}   ← 引擎产物未被采用
```

- **成因**：`trajectory.py` 只在 `status is BLOCKED` 时用占位覆盖；其余状态直接沿用 `redact_payload` 的产物，`scan.payload_redacted` 被丢弃。
- **口径冲突**：设计 `:67` 一方面说三个内部通道「**同样不得落原文**」，另一方面要求「**不替换**该审计链路、W03–W08 行为零漂移」。当前实现严格服从后者 → 对 `REDACTED`/`REVIEW_REQUIRED` 不脱敏。

**泄露面精确定位（本报告上一版判断有误，已据实测更正）**

| 通道 | 是否泄露面 | 实测依据 |
|---|---|---|
| `StepPrepared.request`（`executor.py` `request=redact_payload(params)`） | **是** | 工具**入参**不经闸门①；`{"phone":"138…","note":"联系人 张三"}` → 原样落盘（实测 `13812345678` 残留 = True）。闸门① 只管结果，不管入参 |
| `TrajectoryRecorder.record` / `usage` | **是** | 事件 payload 由调用方构造、未被引擎接管；仅 `BLOCKED` 覆盖为占位 |
| `StepObserved.result` | **不是** | **更正**：闸门①（`executor.py:251` `result = self._redaction.for_tool_result(...)`）在 `_observe_step`（`:259`）**之前**执行，故 `_observe_step` 收到的是已脱敏结果。实测 `{"phone": PHONE}` 经闸门后再 `redact_payload(model_dump())` → `data` 已是 `{"phone":"<PHONE:38aed904>"}`，**手机号不残留**。旧链在此只是二次兜底 |

> 上一版把 `StepObserved.result` 列为同类泄露面是**错误**的（当时未核对接线顺序），特此更正；PM 指出后我实测确认。

- **边界说明（非本 KD 范围）**：无论哪个通道，**值里的人名**（如 `{"note":"联系人 张三"}`）都不会被识别 —— R05 是**纯键名**判定（R4-D 裁定），不做值级人名识别/NLP。这是引擎既定能力边界，不是 T02 接线缺口；若要做需 W10 另立规则并升版。
- **风险**：**中**。PII 会随轨迹文件与 `StepPrepared.request` 落盘；但**不违反** W09 已声明的「审计链路口径不变」，且不进入七条对外通道（`01-prd.md:38` 明确 `redact_payload` 保持不变）。
- **处置（待 team-lead 裁定）**：PM 已拟 **KD-W09-03** 措辞（「三个内部通道沿用 `redact_payload` 口径（已声明限制）」，并明确 `StepObserved.result` 不属本限制），team-lead 批准后落卡。② 备选方案是让轨迹与 `StepPrepared` 采用 `scan.payload_redacted`，但会改 W03–W08 存量口径，须升 `redaction_version` + 补冻结回归 → 建议留 W10。**本报告只引用 KD 编号，不引卡内行号（防漂移）。**

### 11.6 未验证项（单列，不作为任何通过依据）

| 项 | 状态 |
|---|---|
| T03 / T04 / T05 / T06 | **未开工**，未验证 |
| AC-06「下游未被调用」的**业务侧**端到端（真实 Agent/报告/导出链路） | 仅验证到闸门③④ 的单元级调用计数，业务编排未验 |
| 真实库端到端 | **仅** `test_w09_redaction_pipeline.py` 4 条（证据落库/零新增行/隔离区/哈希不覆盖）+ `test_evidence_service.py` 6 条（含 teardown error） |
| AC-08 | 真实库用例覆盖（证据行 vs `REDACTION_APPLIED` 事件版本），已复核**确实读库**（`evidence_service.get` 回读 + `control_db` 查事件 + `count_for_task` 计数），非 skip 误判 |
| W10 路径感知（KD-W09-02） | 未验证、明确留 W10 |
| `docs/w09/06` 之外证据（PG 5432） | 未涉及 |

### 11.7 测试冗余提示（非缺陷，以哪条为准已定）

六字段等值断言现有 **3 处**，覆盖不同入口/状态，**不冲突但重叠**。**以以下两条为准**（不删除历史用例、也不得再扩写）：

| 用例 | 入口 | 覆盖 | 定位 |
|---|---|---|---|
| `test_three_states_keep_exactly_six_audit_payload_fields` | `for_model_messages` | 5 例（CLEAN / REDACTED / BLOCKED_R06/R10/R11）+ 断言不含 `quarantine_skipped`/`channel` | **权威之一** |
| `test_event_payload_keys_are_exactly_six` | `for_tool_result` | 3 态（含 `BLOCKED` 显式成例） | **权威之一** |
| `test_audit_payload_has_exactly_the_six_required_fields` | `for_tool_result` | 仅 `REDACTED` 单态 | **历史用例，保留勿扩写** |

后续若要收敛，合并方向是「通道 × 状态」一次参数化；按「只增不删」纪律本次未动。
