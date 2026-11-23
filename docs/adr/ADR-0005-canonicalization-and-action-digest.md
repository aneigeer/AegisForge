# ADR-0005 操作摘要的规范化算法（RFC 8785 JCS 受约束子集）与冻结测试向量

- 状态：Accepted（Alpha / 本机开发；待独立复核）
- 日期：2026-11-09
- 相关：V2.2 W06 D1｜`docs/w06/01-prd.md` P0-02/P0-03/P0-08｜`docs/w06/00-task-book.md` D1
- 实现位置：`core/security/canonical.py`、`core/approval/digest.py`
- 依赖约束：**不得新增第三方依赖**（`requirements.lock.txt` 冻结）

## 背景

审批必须绑定"某一次具体操作"而不是"一次修复"。绑定手段是摘要：把一次操作的全部决策要素规范化成唯一字节串后取 SHA-256。若规范化不确定，同一逻辑操作会产生不同摘要（审批无故失效），或不同操作产生相同摘要（审批被换参数复用）——两者都是安全事故。

总纲建议 RFC 8785 JCS。但 Python 生态里成熟的 JCS 实现均为第三方包，本轮不允许新增依赖；同时 JCS 最易出错的正是浮点数序列化（要求 ECMAScript `Number::toString` 的最短往返表示）。

## 决策

### 1. 采用 RFC 8785 JCS 的**受约束子集**，并在值域内逐字节一致

实现只用标准库：`json`（字符串转义）、`hashlib`（SHA-256）、`str.encode("utf-16-be")`（键序）。

| 项 | 规则 | 受支持值域内与 RFC 8785 的一致性 |
|---|---|---|
| 对象键排序 | 按 **UTF-16 码元**升序：`sorted(items, key=lambda kv: kv[0].encode("utf-16-be"))` | 一致。**不得**用 Python 默认的码点序——补充平面字符（如 U+1F600 `😀`，UTF-16 为 `D83D DE00`）在 UTF-16 序中排在 U+FF3A `Ｚ` **之前**，码点序相反（见向量 V5）。 |
| 分隔符 | `,` / `:`，无任何空白 | 一致 |
| 字符串转义 | `json.dumps(s, ensure_ascii=False)`：转义 `"` `\` 与 `< U+0020` 控制字符（`\b \t \n \f \r` 用短形式，其余 `\u00xx`）；非 ASCII **原样输出 UTF-8** | 一致（ECMAScript `JSON.stringify` 行为） |
| 布尔 / `null` | `true` / `false` / `null` | 一致 |
| 数组 | 保持元素顺序，逐元素递归 | 一致 |
| 整数 | 十进制字面量；**|v| ≤ 2^53−1（9007199254740991）**，越界报错 | 一致（ECMAScript 安全整数） |
| `tuple` | 按数组序列化（与 `list` 同形）；仅提供 Python 侧便利，不改变规范串 | 一致（输出与 `list` 不可区分） |
| **浮点数** | **一律拒绝**（`CanonicalizationError`） | **不在值域内**，不支持（JCS 要求 ECMAScript `Number::toString` 最短往返表示，本轮不实现） |
| `Decimal` | 拒绝；调用方必须先转十进制字符串 | 不支持 |
| `datetime` | 拒绝；必须先转 ISO-8601 UTC 字符串（`normalize_timestamp`） | 不支持（避免时区/精度歧义） |
| `bytes`/`set`/自定义对象 | 拒绝 | 不支持 |
| 非 `str` 对象键 | 拒绝 | 不支持（JCS 只允许字符串键） |
| 孤立代理项（lone surrogate） | 拒绝（UTF-8 编码会失败） | 一致（JCS 禁止） |

**不支持的类型一律抛错，绝不降级为 `str(value)` 或 `default=str`。** 这是本 ADR 最关键的约束：`compute_args_digest` 现有实现用了 `default=str`，属于降级路径，W06 的摘要**不得复用**它。

**口径（对外表述必须照抄）**：本实现是 RFC 8785 JCS 的**受约束子集（restricted subset）**——在被接受的值域内与 JCS 输出逐字节一致，但**不是完整实现、不是合规实现**，不宣称"支持 RFC 8785"。浮点/`Decimal`/`datetime` 在值域之外，遇到即报错。依据：任务书 D1 原文允许"RFC 8785 JCS **或经 ADR 确认的等价方案**"。

### 2. `action_digest` 的精确输入结构

```jsonc
{
  "action_digest_version": "w06-v1",
  "tenant_id": "<str>",
  "environment_id": "<str>",
  "db_engine": "mysql" | "postgresql",
  "environment_fingerprint": {          // 事实来源：dg_system.environments
    "db_engine": "<str>",
    "db_version": "<str>",
    "image_digest": "<str>" | null,
    "network_id": "<str>",
    "config_version": "<str>"
  },
  "tool": { "name": "<str>", "version": "<str>" },
  "normalized_operation": {
    "operation_class": "observe"|"diagnose"|"experiment"|"repair",
    "side_effect": "none"|"read"|"write",
    "idempotent": <bool>,
    "statement_kind": "<str>",          // update/delete/insert/ddl/...
    "summary": "<str>"                  // 服务端生成的确定性描述，不接受自由文本覆盖
  },
  "target_objects": [ /* 见 2.2 */ ],
  "parameters": { /* 见 2.3 */ },
  "policy_version": "<str>"
}
```

规则：
- **字段集封闭**：多一个字段、少一个字段、拼写错误都报错；不接受"以后会用到"的透传字段。
- **缺失值**：必填字段缺失 = 错误（不是 `null`）；只有 `image_digest`、`target_objects[].schema`、`target_objects[].keys` 允许 `null`/空数组，且必须**显式写出**。缺省与显式 `null` 不等价。
- **键序与空白无关**：由 JCS 保证。同一逻辑操作只要字段值相同，键顺序、源文本空白差异都不改变摘要。
- **`environment_fingerprint` 不得凭空生成**：必须来自 `environments` 行的真实列值；缺列按 `null` 处理并在 `fingerprint_completeness` 中标记 `PARTIAL`。

#### 2.2 `target_objects` 规范形

每项固定五键：`object_type`、`database`、`schema`、`name`、`keys`。

规范化步骤（在进入 JCS 之前完成）：
1. `keys`（字符串数组，如 `"id=1001"`）**去重后按 UTF-16 序升序排序**；
2. 空 `keys` 统一为 `[]`，无 schema 统一为 `null`；
3. 数组元素整体**按各自规范串的字节序字典序排序**。

因此"同一组目标对象，列举顺序不同"得到同一摘要；而"多一个对象/少一个对象/范围不同"必然不同。

#### 2.3 `parameters` 与工具 `input_schema` 的关系

`parameters` **不是**原始请求体，而是 `normalize_tool_parameters(definition, params)` 的输出：
1. 用工具的 pydantic `input_model` 或 `input_schema`（`jsonschema`，已在依赖内）校验；
2. 填充声明的缺省值；
3. `model_dump(mode="json")` 得到 JSON 原生结构；
4. 过一遍 `assert_canonical_supported`（拒绝 `float`/`datetime`/`Decimal`）。

**审批创建侧与执行侧必须调用同一个函数**，否则"显式写了等于默认值的参数"与"没写"会得到不同摘要。任一必填参数缺失 → `REQUEST_INVALID`，不建立审批。

### 3. `prerequisites_digest` 的输入与来源边界

```jsonc
{
  "prerequisites_digest_version": "w06-v1",
  "action_digest": "<该次审批的 action_digest>",
  "checks": [
    { "check_id": "<str>", "status": "PASS"|"FAIL"|"UNKNOWN",
      "observed": { /* 仅服务端探测到的事实 */ },
      "source": "server_probe" | "control_db" }
  ]
}
```

- `checks` 由 `PrerequisiteProbe`（服务端）生成，固定 check 集：`environment_fingerprint`、`db_identity_privilege`、`target_object_exists`、`tool_version_matches`、`policy_version_matches`，外加工具契约 `ToolDefinition.preconditions` 声明的额外项。
- **人工输入一律不得进入摘要**：操作员可写 `operator_note`（存 `approvals.operator_note`、进审计事件 payload），但**不参与** `prerequisites_digest`。理由：否则审批人可能批准自己编造的前置条件。
- 任一 check `status != PASS` → 拒绝创建审批（`APPROVAL_PREREQUISITE_FAILED`，422），不产生 `PENDING` 行。
- 消费时重算，不一致 → `APPROVAL_DIGEST_MISMATCH`（`channel=prerequisites`）。

### 4. 分通道摘要用于定位不符项

除整体 `action_digest` 外，额外存 `parameters_digest` 与 `target_objects_digest`（同算法，输入为对应子树的规范串）。消费时先比 `parameters_digest`、再比 `target_objects_digest`、最后比 `action_digest`，从而能精确回答"是参数变了还是目标变了"。整体摘要仍是唯一门禁。

### 5. 冻结测试向量（实现必须逐字节复现）

规范化串 `S` = `canonical_json(value)`，摘要 = `sha256(S.encode("utf-8")).hexdigest()`。

| # | 输入（Python 值） | 规范串 `S` | SHA-256 |
|---|---|---|---|
| V1 | `{"b":1,"a":2,"c":"x"}` | `{"a":2,"b":1,"c":"x"}` | `38b5d9a6be58441e40ba736c50ec9ac004336e9818b647f78fabfd53c64cd302` |
| V2 | `{"target_objects":["t1","t2"],"meta":{"z":None,"a":True}}` | `{"meta":{"a":true,"z":null},"target_objects":["t1","t2"]}` | `8f975e8f28effd0f00d336b5792cb8cb01a599141d4da2f630250db2c63ba0f9` |
| V3 | `{"note":"中文\"引号\"\n换行\\反斜杠","tab":"a\tb","ctrl":"\u0001"}` | `{"ctrl":"\u0001","note":"中文\"引号\"\n换行\\反斜杠","tab":"a\tb"}` | `5749ea9be5c94fa197da09e996a67e12a9e7e5db1fc720521112e98bce2bc0bc` |
| V4 | `{"max":9007199254740991,"min":-9007199254740991,"zero":0}` | `{"max":9007199254740991,"min":-9007199254740991,"zero":0}` | `b7b2401ddca2165824e98c61890c0aaec470258d3119dd265d02be9438bf47e6` |
| V5 | `{"Ｚ":1,"😀":2}`（键 U+FF3A / U+1F600） | `{"😀":2,"Ｚ":1}` | `d0044d5452bc6cacd54894da533ae405d6d3d8063f9bc4ba7546a6d84faafc8f` |
| V6 | `{"a":{"c":[1,{"e":[],"d":None}],"b":"x"},"A":0}` | `{"A":0,"a":{"b":"x","c":[1,{"d":null,"e":[]}]}}` | `a0a5f63d039042c56ec693c51152ba4676dd17162b6a6f06d447d6b84b0a7b61` |

**向量覆盖类型矩阵**（`✓`=已覆盖并冻结；`✗`=**明确不在值域内，只有反向量**——后来人不得以为 `float` 等类型被支持）：

| 类型 | 覆盖向量 | 说明 |
|---|---|---|
| `str`（ASCII / 中文 / 控制字符 / 引号反斜杠） | V1 V3 V6 | V3 覆盖 `\u0001`、`\n`、`\t`、`\"`、`\\` |
| `tuple` | 无独立向量（必然经 `list` 路径） | `canonical_json((1,2)) == "[1,2]"`，见 `tests/unit/test_canonical.py` |
| `int`（正 / 负 / 0 / 2^53−1 边界） | V4 | 越界 `2^53` 见反向量 |
| `bool` / `null` | V2 V6 | 显式 `null` 参与摘要 |
| 空数组 / 空对象 / `null` 元素 | V2 V6 | `[]` 与 `{}` 各出现一次 |
| 数组（顺序敏感） | V2 V6 | 顺序改变必然改变摘要 |
| 嵌套对象（3 层） | V6 | — |
| 键序（含非 BMP 字符） | V5 | 锁死 UTF-16 码元序 |
| `float`/`nan`/`inf`、`Decimal`、`datetime`、`bytes`、`set`、非 `str` 键、孤立代理项 | **✗ 仅反向量** | 报错，不降级 |

**顺序无关性**：V1 的等价输入 `{"c":"x","a":2,"b":1}` 必须得到同一个 `38b5d9a6…`。
**V5 的意义**：若误用码点序，得到的串会是 `{"Ｚ":1,"😀":2}`，摘要不同——这条向量专门锁死 UTF-16 码元排序。

**必须拒绝的输入（反向量，断言抛 `CanonicalizationError`）**：`{"x":1.5}`、`{"x":float("nan")}`、`{"x":Decimal("1.0")}`、`{"x":b"a"}`、`{"x":{1,2}}`、`{1:"a"}`、`{"x":"\ud800"}`、`{"x":9007199254740992}`。

#### 完整 `action_digest` 冻结向量（W06 端到端基准）

输入（键序在源文本中可任意打乱，结果必须一致）：

```jsonc
{
  "action_digest_version": "w06-v1",
  "db_engine": "mysql",
  "environment_fingerprint": {"config_version":"w03-local","db_engine":"mysql","db_version":"8.0.46","image_digest":null,"network_id":"lab_net"},
  "environment_id": "env_mysql_local",
  "normalized_operation": {"idempotent":false,"operation_class":"repair","side_effect":"write","statement_kind":"update","summary":"将 orders 表中 id=1001 的行 status 从 NEW 改为 PAID"},
  "parameters": {"expected_rows":1,"id":1001,"status":"PAID","table":"orders"},
  "policy_version": "w06-v1",
  "target_objects": [{"database":"dg_lab","keys":["id=1001"],"name":"orders","object_type":"table","schema":null}],
  "tenant_id": "tenant_local",
  "tool": {"name":"mysql_update_rows","version":"1.0"}
}
```

- 规范串：见 `docs/w06/05-freeze.md`（由实现生成后冻结，逐字节比对）
- `action_digest` = `f31f153767a028a758bb3ef02fb9322422e8fc0ff77c287ad459893f7a8d5bc2`
- `prerequisites_digest`：**以 `docs/w06/05-freeze.md` §D.3 为唯一权威基准** = `c5b4b8175724de5a55a16772a1bffc6da2276950f69c37d695aa0e78bd64d0dc`（输入为该节三项 `PASS` check 的完整 `observed`，已独立复算验证一致）。本 ADR 早期给出的 `a15af786…` **作废**：当时未记录那三项 check 的 `observed` 内容，**不可复现**，不满足"冻结向量必须可复现"的要求。

**失效子向量**（每个都必须与基准不同，用于 T2 六个子用例）：

| 变更项 | 变更后摘要 |
|---|---|
| `parameters.expected_rows: 1 → 2` | `9c25c50aabfa54ee11dc9dbe1515f4c662bf72a11041fb3a381b1c2da69c1b04` |
| `policy_version: w06-v1 → w06-v2` | `0b164e1a34ffff0817def520daebd45d021a099fec3b2c04b392e7ef344943ee` |
| `environment_fingerprint.config_version: w03-local → w06-local` | `215e72a91cdf7d1b10b055d1956c60bed6ae7c9df67f624b2a77269393254277` |
| `target_objects[0].name: orders → orders_archive` | `a91569943a0f4353cb2abaae60e1f6401bca6b671a08605d957434cae6bd3710` |
| `tool.version: 1.0 → 1.1` | `ed21be519c2828991119ef5abb4a560d4c5f0c834ec4667ba201cf69c1d27974` |

## 备选方案与取舍

- **引入 `rfc8785` 第三方包**：实现最省事，但违反"不新增依赖"冻结，且包本身仍需审计。选择标准库子集。
- **继续用 `json.dumps(sort_keys=True, default=str)`**（现有 `compute_args_digest` 的做法）：`sort_keys` 是码点序（V5 会错），`default=str` 是静默降级（`datetime`/`Decimal`/自定义对象会被"胡乱"字符串化，导致两个不同操作可能碰撞）。**明确弃用**，W06 摘要不复用它。
- **支持浮点并实现 ECMAScript `Number::toString`**：可完整合规，但实现与测试成本高、收益低——R2 写操作的参数（主键、行数、状态字符串）不需要浮点，金额类应走十进制字符串。选择拒绝。
- **用 protobuf/CBOR 等二进制规范形**：与总纲建议偏离，且需新依赖。不选。
- **只对"参数"取摘要，不纳入环境/策略版本**：实现简单，但审批会变成"批准一次修复"而非"批准一次操作"，与 P0-03 冲突。不选。

## 后果

- 摘要算法一旦上线即冻结；`action_digest_version` 变更视为**不兼容变更**，旧审批全部失效（这是期望行为）。
- 写工具的参数若必须含浮点，将无法建立审批——属于有意的 fail closed；W07 若需要，应把该参数改为十进制字符串并升级 `action_digest_version`。
- `target_objects` 的五键结构成为对外协议的一部分，W07 新增对象类型必须走版本升级，不能悄悄加字段。
- 摘要不含 `requested_by`/`approver_id`/`expires_at`（这些是审批自身属性，不是操作属性），因此"同一操作可以由不同人分别申请"——这是设计意图，不是缺陷。

## 验证方式

- `tests/unit/test_canonical.py`：本 ADR 全部 6 组向量 + 8 条反向量 + V1 顺序无关性。
- `tests/unit/test_action_digest.py`：完整 `action_digest` 基准 + 5 条失效子向量 + `parameters` 缺省值填充一致性（显式写默认值 vs 不写 → 同摘要）。
- 集成：`tests/integration/test_approval_consume.py` 覆盖参数/目标/环境/指纹/策略版本/工具版本六类变更的消费拒绝与 `channel` 定位。

## 已知限制

- 不是完整 RFC 8785 实现：浮点、`Decimal`、`datetime` 在值域之外（拒绝而非降级）。对外表述必须是"受约束子集"。
- 仅覆盖 Python 侧；其他语言实现若接入，需按本 ADR 的向量逐字节对齐，否则跨语言摘要不可比。
- `summary` 字段由服务端模板生成，其文本质量依赖模板；但它参与摘要，模板变更即摘要变更——因此模板版本必须随 `action_digest_version` 一起管理。
