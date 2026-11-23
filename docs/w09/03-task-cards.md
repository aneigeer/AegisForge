# W09 任务卡｜报告、脱敏与记忆（6 卡，按依赖硬顺序）

- 依据：`docs/w09/01-prd.md`（AC-01…AC-21）、`docs/w09/02-design.md`（§0 A1–A10 裁决、§2 脱敏、§4 报告、§5 记忆、§6 降级、§7 KD 承接、§8 迁移）
- 上游门禁：W08 **「有条件放行」**（`docs/w08/07-gate-review.md:10-27`）——本批任务**不得**表述为「W08 已通过」
- 硬顺序：`T01 → T02 → T03 → T04 → T05 → T06`；后卡可依赖前卡，**任一时刻不得有两张卡改同一个文件**
- 全局纪律（6 卡均适用，逐卡不再重复展开）：
  1. **不许为了让测试变绿而放宽断言、删用例、加 `skip`、改 `xfail`、缩小参数化范围**。断言不成立时先判定「是实现错」还是「断言错」；认为断言错必须先回报 team-lead，不得自行修改。
  2. 不新增第三方依赖（沿用 `requirements.lock.txt`）；不新增前端、不新增 API 端点。
  3. 不扩大写能力：PG 仍只读；写动作仍只有 `mysql_lab_update_order_note`/`1.0`（`adapters/mysql/lab_repair.py`）。
  4. 测试**不得依赖** `DG_ADMIN_DSN`、**不得依赖** PG 5432 可连；需要 DB 的用例只做 `INSERT`（追加），不做清理。
  5. 所有新表/新列只追加；`dg_ctl` 无 `UPDATE`/`DELETE`。
  6. 每个 PR 只改本卡「涉及文件」；发现必须改卡外文件，先回报 team-lead 改卡。

---

## 依赖关系与交付顺序

| 卡 | 标题 | 依赖 | 关键路径 |
|---|---|---|---|
| **T01** | 脱敏引擎（规则、结果模型、确定性、错误码与事件枚举） | — | ✅ 起点 |
| **T02** | 全链路脱敏接入（6 个闸门、7 条通道、证据原文锚点、迁移 009） | T01 | ✅ |
| **T03** | 结构化报告（引用校验、两组六态、安全渲染、导出）+ KD-1 补偿拒绝事件 | T02 | ✅ |
| **T04** | 五类记忆 + `KnowledgeEntry` + 冲突解析器（迁移 011） | T03 | ✅ |
| **T05** | 模型降级（状态机、一次重试、规则简报、`disabled` 装配） | T02、T04 | ✅ |
| **T06** | 脱敏回归样本集 + 训练样本 + `sweep()` 扫描总数 + KD-3/4/5 | T03、T05 | ✅ 收口 |

**文件所有权（防并行冲突）**

| 卡 | 独占文件 |
|---|---|
| T01 | `core/redaction/{__init__,models,payload_text,rules,engine}.py`、`core/errors.py`、`core/audit/events.py` |
| T02 | `core/redaction/gate.py`、`core/evidence/{models,service,repository}.py`、`core/diagnosis/evidence_mapping.py`、`core/context/{builder,contracts}.py`、`core/tools/{contracts,executor}.py`、`core/models/gateway.py`、`core/agent/trajectory.py`、`workers/stack.py`、`database/migrations/009_*` |
| T03 | `core/report/{__init__,models,validate,render,export,builder,repository}.py`、`core/approval/{service,repository}.py`、`database/migrations/010_*` |
| T04 | `core/memory/*`、`database/migrations/011_*` |
| T05 | `core/models/{degradation,factory}.py`、`core/report/brief.py`、`core/agent/loop.py`、`apps/api/routes/agent.py` |
| T06 | `core/execution/reconciler.py`、`workers/runner.py`、`core/audit/service.py`、`core/report/samples.py`、`tests/unit/test_worker_scheduler.py` |

---

## T01｜脱敏引擎

- **依赖**：无
- **源需求**：P0-W09-01（`01-prd.md:61-68`）；AC-01/AC-02/AC-03/AC-04（`:131-134`）；设计 §0 A3、§2
- **涉及文件**（新建 5 / 修改 2 / 测试 1）
  - 新建：`core/redaction/__init__.py`、`core/redaction/models.py`、`core/redaction/payload_text.py`、`core/redaction/rules.py`、`core/redaction/engine.py`
  - 修改：`core/errors.py`（4 个错误码 + 2 个异常类）、`core/audit/events.py`（6 个事件类型）
  - 测试：`tests/unit/test_w09_redaction_engine.py`（新建）

- **关键签名（可直接抄）**

```python
# core/redaction/payload_text.py
def canonical_payload_text(payload: Any) -> str: ...      # json.dumps(sort_keys=True, ensure_ascii=False,
                                                          #   separators=(",", ":"), default=str)
def canonical_payload_hash(payload: Any) -> str: ...      # sha256(canonical_payload_text(payload).encode())

# core/redaction/models.py
class RedactionStatus(str, Enum):
    CLEAN = "CLEAN"; REDACTED = "REDACTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"; BLOCKED = "BLOCKED"

class SensitiveCategory(str, Enum):
    SQL_LITERAL = "SQL_LITERAL"; PHONE = "PHONE"; EMAIL = "EMAIL"; ID_CARD = "ID_CARD"
    PERSON_OR_BUSINESS_ID = "PERSON_OR_BUSINESS_ID"; CONNECTION_STRING = "CONNECTION_STRING"
    USERNAME_PASSWORD = "USERNAME_PASSWORD"; TOKEN = "TOKEN"; API_KEY = "API_KEY"
    PRIVATE_KEY = "PRIVATE_KEY"; CERTIFICATE = "CERTIFICATE"; LOG_FIELD = "LOG_FIELD"

class RedactionChannel(str, Enum):
    TOOL_RESULT = "TOOL_RESULT"; EVIDENCE = "EVIDENCE"; MODEL_INPUT = "MODEL_INPUT"
    MODEL_MESSAGES = "MODEL_MESSAGES"; REPORT = "REPORT"; EXPORT = "EXPORT"
    TRAINING_SAMPLE = "TRAINING_SAMPLE"

class SensitiveSpan(BaseModel):                 # extra="forbid"
    category: SensitiveCategory
    json_path: str                              # 例如 data.rows[3].note
    start: int; end: int                        # 叶子内偏移
    reason: str                                 # ID_CHECKSUM_INVALID / UNREPLACEABLE_KEY_MATERIAL / ...

class RedactionResult(BaseModel):               # extra="forbid"
    payload_redacted: dict[str, Any]
    redaction_version: str
    detected_categories: tuple[SensitiveCategory, ...]
    redaction_count: int = Field(ge=0)
    unresolved_sensitive_spans: tuple[SensitiveSpan, ...]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RedactionStatus
    blockers: tuple[str, ...] = ()
    @property
    def is_passable(self) -> bool: ...          # status in {CLEAN, REDACTED}

class RedactionSummary(BaseModel):              # extra="forbid"；落证据/审计/报告，不含 payload
    redaction_version: str; status: RedactionStatus
    detected_categories: tuple[SensitiveCategory, ...]; redaction_count: int
    source_payload_hash: str; redacted_payload_hash: str | None = None

# core/redaction/rules.py
RS_REDACTION_V2 = "redaction-v2"   # 当前默认
RS_REDACTION_V1 = "redaction-v1"   # 历史行/既有审计口径引用，不得删除或改值
MAX_REDACTION_DEPTH = 12
MAX_REDACTION_ITEMS = 5_000
MAX_SCAN_CHARS = 200_000
SENSITIVE_KEY_MARKERS = ("password", "passwd", "pwd", "secret", "token",
                         "dsn", "authorization", "apikey", "credential")   # 与 core/audit/events.py:26-36 同值

# core/redaction/engine.py
class DataRedactionEngine:
    def __init__(self, *, ruleset_version: str = RS_REDACTION_V2,
                 max_depth: int = MAX_REDACTION_DEPTH,
                 max_items: int = MAX_REDACTION_ITEMS,
                 max_scan_chars: int = MAX_SCAN_CHARS) -> None: ...
    @property
    def version(self) -> str: ...
    def redact(self, payload: Any, *, channel: RedactionChannel) -> RedactionResult: ...
    def redact_text(self, text: str, *, channel: RedactionChannel) -> tuple[str, RedactionResult]: ...
    def summary(self, result: RedactionResult) -> RedactionSummary: ...
```

- **必须实现的语义**
  1. **12 类逐类命中**（AC-01）：按设计 §2.2 的 `R01…R12` 表实现；`detected_categories` 按枚举声明序去重输出（**不是**命中顺序）。
  2. **确定性**（AC-02）：同一输入连跑 5 次，`{redaction_count}`、`{redaction_version}`、`{payload_hash}` 三个集合大小各为 1；dict 键走 `sorted()`、list 按下标遍历；规则表顺序固定；同一叶子内按 R01→R12。
  3. **不可靠不冒充**（AC-03）：`status ∈ {REVIEW_REQUIRED, BLOCKED}` 的判据闭集见设计 §2.2/§2.3；`status == REDACTED` 时 `unresolved_sensitive_spans` 必须为空。
  4. **哈希口径**（AC-04）：`payload_hash == sha256(canonical_payload_text(原始 payload))`，且 `payload_hash != canonical_payload_hash(payload_redacted)`（脱敏确有替换时）。**用 `canonical_payload_text`，不用 `canonical_digest`**（后者拒绝 float/datetime/bytes，见 `core/security/canonical.py:108-147`）；`RedactionResult` 注入多余字段 → `ValidationError`。
  5. **错误码**：`ErrorCode.REDACTION_BLOCKED` / `REDACTION_REVIEW_REQUIRED` / `EVIDENCE_REF_INVALID` / `KNOWLEDGE_INVALID`（后两个本卡只加枚举与异常类，T03/T04 使用）。
  6. **事件枚举**：`AuditEventType` 增 `REDACTION_APPLIED` / `REDACTION_BLOCKED` / `REDACTION_REVIEW_REQUIRED` / `MODEL_DEGRADED` / `REPORT_GENERATED` / `KNOWLEDGE_REVOKED`（本卡只加枚举，发射点在 T02/T03/T05）。
  7. **`redact_text` 兼容**：`DataRedactionEngine.redact_text` 是**新增**入口；`core/logging.py::redact_text`（`:27`）与 `core/audit/events.py::redact_payload`（`:122`）**行为不变**（PRD `01-prd.md:38`）。

- **禁止项**
  - 禁止修改 `core/logging.py` 与 `core/audit/events.py::redact_payload` 的行为与常量值（新规则表可引用 `SENSITIVE_KEY_MARKERS`，但**不得**为了复用而改旧常量）。
  - 禁止引入 `regex` 包、「姓名识别 NLP」、任何机器学习/向量组件；不得新增第三方依赖。
  - 禁止把 `blockers` 合并进 `detected_categories`（会让 AC-01 的 12 类基线不可判）。
  - 禁止用「截断」代替「无法脱敏」（截断 = 静默丢弃，必须 `REVIEW_REQUIRED`）。
  - 禁止让 `redaction_version` 取 `"none"` 或缺省为空串。
  - 禁止把 `payload_hash` 改为 `canonical_digest`（会与 W03–W08 存量口径冲突）。
  - 禁止在本卡修改 `core/evidence/*`、`core/context/*`、`core/tools/*`（属 T02）。

- **完成判据**
  - `tests/unit/test_w09_redaction_engine.py` 全绿，覆盖 AC-01…AC-04 共 5 组断言（12 类逐类 / 5 轮确定性 / 未覆盖→非 REDACTED / 哈希口径 + `extra="forbid"` / `status` 不变量）。
  - `python -m pytest tests/unit -q` 通过，且用例数 ≥ 891 + 本卡新增数（W08 收口基线 891，`docs/w08/07-gate-review.md:35`）。
  - `core/redaction` 的公开 API 不含任何 DSN/registry 形参（自查：`inspect.signature`）。

- **验收命令（tester 可照跑）**
```bash
python -m pytest tests/unit/test_w09_redaction_engine.py -q
python -m pytest tests/unit/test_audit_events.py tests/unit/test_evidence_models.py -q
python -m pytest tests/unit -q
```

---

## T02｜全链路脱敏接入

- **依赖**：T01
- **源需求**：P0-W09-02（`01-prd.md:70-77`）；AC-05/AC-06/AC-07/AC-08（`:135-138`）；设计 §0 A1/A2、§3、§8.2
- **依据（补充裁定）**：`docs/w09/05-rulings.md` **R1 / R4-A / R4-B / R4-D**（权威顺序 **R4-D > R4 > R1**；R4-D 覆盖 R4-A 中「C 组保留」的过渡措辞）
- **T02 开工前置检查（缺一不可；依据 05-rulings R1 / R4-A / R4-D）**

| # | 检查项 | 通过判据 |
|---|---|---|
| ① | **R1 冲突已闭环** | `core/redaction/rules.py` 的 R05 修订已落地（`NAME_EXACT_KEYS={"name"}` 精确命中 + `NAME_KEY_MARKERS` 13 项归一化子串），且 QA 独立复验结论记为「R1 冲突已闭环（含欠脱敏缺口）」；**此前不得**把 `for_tool_result()` 接到 `executor.py:238-241` |
| ② | **默认版本已升版** | 开工前确认默认 `redaction_version == "redaction-v2"`（常量 `RS_REDACTION_V2`）；`RS_REDACTION_V1`（`"redaction-v1"`）仍并存但**仅用于读取历史行**，不得用于任何新产出；`core/redaction/engine.py` 默认值、`__init__.py` 导出、`rules.py` 版本注释三处同步 |
| ③ | **复验口径已定** | 回归按 R4-D 两组口径执行（正例 23 条 / 负例 15+14 条），只断言 `is_name_key()` 输入输出行为；**不要求** `SYSTEM_METADATA_KEYS` / `NAME_KEY_EXACT` / `NAME_KEY_SUBSTRING` 等旧常量或 C 组白名单存在 |

- **涉及文件**（新建 2 / 修改 10 / 迁移 2 / 测试 2+ 镜像用例）
  - 新建：`core/redaction/gate.py`、`database/migrations/009_w09_redaction.sql` + `database/migrations/rollback/009_rollback.sql`
  - 修改：`core/evidence/models.py`、`core/evidence/service.py`、`core/evidence/repository.py`、`core/diagnosis/evidence_mapping.py`、`core/context/builder.py`、`core/context/contracts.py`、`core/tools/contracts.py`、`core/tools/executor.py`、`core/models/gateway.py`、`core/agent/trajectory.py`、`workers/stack.py`
  - 测试：`tests/integration/test_w09_redaction_pipeline.py`（新建）、`tests/unit/test_w09_redaction_gate.py`（新建，离线镜像用例）
  - **卡内行号均为当前快照**（以符号名 + 相对位置为准；实施后偏移属正常，不得据此判卡错）：`ToolExecutor.execute` 的 `result.model_copy(...)` 之后 / `_observe_step` 之前插 `for_tool_result`；`EvidenceService.create()` 的 `canonical_payload_hash` 之前加闸门；`evidence_mapping` 的 `redacted_payload=copy.deepcopy(payload)` 处改真脱敏。

- **关键签名（可直接抄）**

```python
# core/redaction/gate.py
class QuarantineRepository(Protocol):
    def insert(self, *, tenant_id: str, task_id: str | None, channel: str,
               result: RedactionResult) -> str: ...        # 只写哈希/类别/路径，不写原文

class RedactionGate:
    def __init__(self, engine: DataRedactionEngine, *, audit,
                 quarantine: QuarantineRepository | None = None) -> None: ...
    def for_tool_result(self, result: ToolResult, *, tenant_id: str, task_id: str) -> ToolResult: ...
    def for_evidence(self, request: EvidenceCreate, *, tenant_id: str, task_id: str) -> EvidenceCreate: ...
    def for_model_input(self, bundle: ContextBundle, *, tenant_id: str, task_id: str) -> ContextBundle: ...
    def for_model_messages(self, messages: list[ModelMessage], *, task_id: str,
                           tenant_id: str | None = None) -> list[ModelMessage]: ...
    def for_document(self, payload: dict[str, Any], *, channel: RedactionChannel,
                     task_id: str, tenant_id: str | None = None
                     ) -> tuple[dict[str, Any], RedactionResult]: ...   # REPORT / EXPORT / TRAINING_SAMPLE
#   tenant_id 仅 for_model_messages / for_document 为可选（只用于隔离区写入，表列 NOT NULL）；其余方法保持必填
#   tenant_id 可选是**引擎侧**为兼容无租户上下文而留；**T03/T06 调用时必须传 tenant_id**，否则 REVIEW_REQUIRED
#   无法写隔离区（redaction_quarantine.tenant_id 为 NOT NULL），复核留痕缺失。缺失时 gate 发 REDACTION_REVIEW_REQUIRED
#   且 payload 带 quarantine_skipped: true，可被审计检索——但这是降级留痕，不是正常路径。
#   回归（team-lead 裁定）：tests/unit/test_w09_redaction_wiring.py 的
#   test_quarantine_skip_is_flagged_in_event_when_tenant_missing（tenant 缺失）/
#   test_quarantine_skip_flagged_when_repository_missing（quarantine 未装配）/
#   test_quarantine_not_flagged_when_written（正向）只复用不重写；
#   键集合等值断言已落地：以 test_three_states_keep_exactly_six_audit_payload_fields 为准（含
#   len(detected_categories) == 1 纯净性断言；BLOCKED 取 R06/R10/R11 纯净样本，与 R3_BLOCKED_SAMPLES 同值）；
#   CLEAN/REDACTED/BLOCKED 三态事件 payload 恰为六字段（redaction_version / source_payload_hash /
#   redacted_payload_hash / status / detected_categories / redaction_count），跳过时八字段（+ quarantine_skipped + channel）；
#   test_event_payload_keys_are_exactly_six 同口径保留，覆盖 for_tool_result 路径（勿删：test_three_states… 走的是
#   for_model_messages，两条路径不同）。
#   历史单态用例 test_audit_payload_has_exactly_the_six_required_fields（REDACTED 单态）**保留勿扩写**；三处重叠以上述两条为判据，**不再新增第 4 条**。
#   for_document 的 REVIEW_REQUIRED **不抛**：返回「已抹除原串的文档 + result」，由 T03/T06 落 published=False；仅 BLOCKED 抛 RedactionBlockedError
#   导入路径（避免 contracts ↔ redaction 循环导入）：统一 `from core.redaction.gate import RedactionGate`；
#   **不经** `core/redaction/__init__.py` 导出，QA 断言按此路径

# core/evidence/models.py（新增）
WEAK_COLLECTION_STATUSES: frozenset[CollectionStatus]   # {PARTIAL, DENIED, TIMEOUT, NOT_SUPPORTED, UNKNOWN}
class EvidenceCreate(BaseModel):                        # extra="forbid" 保持
    ...
    source_payload_hash: str | None = None               # 原文哈希（引擎给出）；None = 未脱敏路径
class Evidence(BaseModel):
    ...
    source_payload_hash: str | None = None

# core/evidence/service.py（闸门增量；hash 计算口径不变）
def create(self, request: EvidenceCreate) -> Evidence: ...
#   1) 闸门：result.status in {CLEAN, REDACTED}，否则抛 RedactionBlocked/ReviewRequiredError（不写行）
#   2) payload_hash         = canonical_payload_hash(request.payload)      # 口径与 W08 完全一致，不改
#   3) redacted_payload_hash= canonical_payload_hash(request.payload)      # 既有逻辑 :93-95，不改
#   4) source_payload_hash  = request.source_payload_hash                  # 原文锚点，仅落库，不参与上面两项

# core/evidence/repository.py（新增）
def fetch_by_ids(self, tenant_id: str, evidence_ids: Sequence[str]) -> list[Evidence]: ...

# core/tools/contracts.py（新增可选字段；不动 action_digest 字段集）
class ToolResult(BaseModel):
    ...
    redaction: RedactionSummary | None = None

# core/context/contracts.py（新增可选字段）
class EvidenceItem(BaseModel):  ...  redaction: RedactionSummary | None = None
class ContextBundle(BaseModel): ...  redaction: RedactionSummary | None = None
```

- **必须实现的语义**
  1. **闸门①（工具结果）**：`core/tools/executor.py::execute` 内 `result.model_copy(...)`（`:238-240`）之后、`_observe_step`（`:241`）之前调用 `for_tool_result`；`CLEAN/REDACTED` → `data` 换脱敏后内容并带 `redaction`；`REVIEW_REQUIRED/BLOCKED` → `data={}` + `truncated=True` + `redaction.status` 如实标注，**不修改 `ToolStatus`**（设计 §3 #1）。
  2. **闸门②（证据持久化）**：`EvidenceService.create` 入口断言 `status ∈ {CLEAN, REDACTED}`；`BLOCKED` 抛 `RedactionBlockedError` 且 **`evidence` 表 0 新增行**（AC-05）；`REVIEW_REQUIRED` 抛 `RedactionReviewRequiredError` 并写隔离区（无原文）。**`BLOCKED` 抛 `RedactionBlockedError` 前，`payload_redacted` 必须已抹除原串**（以固定 `<BLOCKED:CATEGORY>` 占位，`CATEGORY` 取 `SensitiveCategory` 值：`CONNECTION_STRING` / `PRIVATE_KEY` / `CERTIFICATE`），否则 AC-01 不成立；隔离区只存哈希 / 类别 / `json_path` 定位，**不含任何原文片段**（与设计 §3 闸门② 一致；依据 05-rulings **R3**）。
  3. **闸门③④（模型输入）**：`ContextBuilder.render_messages`（`core/context/builder.py:102`）前对全部 `EvidenceItem` 断言；`ModelGateway.generate`（`:85`）与 `generate_structured`（`:105`）入口对 `messages[].content` 复查；不通过**不调用适配器**，发 `REDACTION_BLOCKED`（AC-06）。
  4. **七条通道全接通**：导出（T03 的 `export.py`）与训练样本（T06 的 `samples.py`）在各自卡调用 `for_document(..., channel=REPORT|EXPORT|TRAINING_SAMPLE)`；本卡负责让 `DataRedactionEngine`/`RedactionGate` 可从组合根取到（`workers/stack.py` + `apps/api/routes/agent.py` 的装配点由 T05 统一改，本卡只加 `WorkerStack` 字段）。
  5. **真脱敏替换假脱敏**：`core/diagnosis/evidence_mapping.py:117-118` 的 `redacted_payload=copy.deepcopy(payload)` 必须改为引擎产物，`payload` 同步为脱敏后内容，`source_payload_hash` 填原文哈希，`redaction_version` 取引擎版本（不再硬编码 `tool-redaction-v1`）。历史行**不改写**。
  6. **审计一致性**（AC-08）：`REDACTION_APPLIED` 的 payload 含 `{redaction_version, source_payload_hash, redacted_payload_hash, status, detected_categories, redaction_count}`；证据行 `redaction_version` 与事件 `redaction_version` **相等**。
  7. **轨迹通道**：`TrajectoryRecorder.record`（`core/agent/trajectory.py:73-81`）落盘前追加引擎扫描；`BLOCKED` 时该事件 payload 置 `{"redaction": "BLOCKED"}` 且不带原值。
     - **已知限制 KD-W09-03（依据设计 `:67` 与 PRD `01-prd.md:38`）**：`StepPrepared.request`（工具入参）与 `TrajectoryRecorder.record` / `usage`（轨迹事件）沿用 W03–W08 的 `redact_payload` 口径（敏感键名 + 2 条正则），W09 只追加「`BLOCKED` 时整条置 `{"redaction": "BLOCKED"}`、原文零残留」，**不接管 `REDACTED` / `REVIEW_REQUIRED` 的改写**；故**非敏感键名下的 PII 仍可能原样落盘**，属已声明限制。**本限制只针对 `REDACTED` / `REVIEW_REQUIRED` 路径；`BLOCKED` 已由 W09 覆盖为整条占位，不在此列。** `StepObserved.result` 位于闸门① 之后、内容已由引擎兜住，**不属本限制**。收紧须替换 `redact_payload` → 改 W03–W08 存量口径，须升 `redaction_version` + 补冻结回归，**W10 再议**。
  8. **隔离区只存定位信息**：`redaction_quarantine`（迁移 009）**不含任何原文片段**；`json_path`/`start`/`end` 仅用于人工复核定位。**列以设计 §8.2 冻结 DDL 为准**（早前口头的扁平列 `categories`/`json_path`/`start`/`end` 口径作废）：`quarantine_id` / `tenant_id` / `task_id` / `channel` / `status` / `payload_hash` / `detected_categories`(JSON) / `unresolved_spans_json`(JSON) / `blockers`(JSON) / `redaction_version` / `created_at`；`json_path`/`start`/`end` 作为 **span 字段内嵌于 `unresolved_spans_json`**（一载荷多 span，扁平列表达不了），`redaction_version` 另供 AC-08 与证据行对齐。
  9. **组合根纪律**（依据 05-rulings **R4-B**）：`ToolResult.data` / `ToolResult.source_metadata` **只放业务键值**；**不得**把 `ToolDefinition` / `TargetObjectSpec` 等契约对象整包 `model_dump()` 塞进待脱敏载荷；契约对象如需传递，走**结构化字段**。这是**组合层纪律，不是引擎规则**——引擎仍只按键名确定性判定，`is_name_key("name") is True` 保持不变，**不得**因此给引擎加路径感知、值形态判定或启发式。
  10. **版本升版与历史行**（依据 05-rulings **R4-A / R4-D**）：本卡产出的证据行 / 审计事件 / 摘要，`redaction_version` 一律取引擎当前默认版本 `"redaction-v2"`（常量 `RS_REDACTION_V2`，与 `RS_REDACTION_V1` 并存，**V1 仅用于读取历史行**）；证据行 `redaction_version` 必须与 `REDACTION_APPLIED` 事件中的 `redaction_version` **相等**（AC-08）；**历史行一律不改写**（不 `UPDATE`、不回填、不重算已有哈希）。

- **禁止项**
  - 禁止改变 `payload_hash` / `redacted_payload_hash` 的**计算口径**：`payload_hash == canonical_payload_hash(request.payload)` 必须与 W08 完全一致（`tests/integration/test_evidence_service.py:98` 必须仍绿）；`source_payload_hash` 是**新增列**，不得参与这两项计算，更不得用它覆盖 `payload_hash`。
  - 禁止改 `ToolStatus` 语义、禁止把「内容被隔离」记成 `DENIED`/`ERROR`。
  - 禁止改动 `action_digest` 字段集（`ToolResult` 新增字段不参与任何摘要；加一条断言：既有冻结摘要向量不变）。
  - 禁止为了通过测试而把 `EvidenceService` 的闸门做成「可关闭开关」；禁止新增「跳过脱敏」配置项。
  - 禁止在 `apps/api/**` 新增端点；禁止改 `core/gateway/**`（补偿事件在 T03 的 `ApprovalService`）。
  - 禁止用 `DG_ADMIN_DSN` 清理测试数据；禁止断言「测试后表为空」。
  - 禁止修改 `core/logging.py` 与 `redact_payload`（审计链路不变）。
  - 禁止把 `ToolDefinition` / `TargetObjectSpec` 契约对象 `model_dump()` 整包塞进 `ToolResult.data` / `source_metadata` 或任何待脱敏载荷（依据 05-rulings **R4-B**）：契约对象走结构化字段；违反会在载荷里引入嵌套裸 `name`，命中 A 组精确规则后降级为 `REVIEW_REQUIRED`（见 KD-W09-02）。
  - 禁止用 `RS_REDACTION_V1`（`"redaction-v1"`）产出任何新行；禁止把版本升版做成「按行可选 / 可回退」的运行时开关；禁止为升版而回填或改写历史行的 `redaction_version` 与既有哈希（依据 05-rulings **R4-A / R4-D**）。

- **完成判据**
  - AC-05：`status=BLOCKED` 注入 → 领域错误 + 证据行新增 0（离线镜像用例用假仓储计数 `insert_calls == 0` 断言；DB 用例用 `count_for_task == 0`，`core/evidence/repository.py:196-201`）。
  - AC-06：未脱敏载荷渲染前被拒；`user_content` 不含原串；`task_events` / 审计替身含 `REDACTION_BLOCKED`。
  - AC-07：导出/训练样本再扫描（T03/T06 联调断言，本卡提供 `EXPORT`/`TRAINING_SAMPLE` 通道可调用）。
  - AC-08：证据行 `redaction_version` 与事件 `redaction_version` 相等（DB 用例）+ 离线镜像（同一次调用内捕获事件断言）。
  - **R3 占位语义**（依据 05-rulings **R3**）：12 类规则逐类断言 `raw_secret not in payload_redacted`（AC-01 原判据、强度不降）；`redaction_count` **不被 BLOCKED 三类（R06 连接串 / R10 私钥 / R11 证书）增加**（占位是掩码，不得误统计成「已脱敏」）；BLOCKED 输出必须含固定 `<BLOCKED:CATEGORY>` 占位（`CATEGORY` = `SensitiveCategory` 值 `CONNECTION_STRING` / `PRIVATE_KEY` / `CERTIFICATE`）且**不含原文片段 / 哈希 / 任何可逆信息**。现有 `tests/unit/test_w09_redaction_engine.py` 的 R3 六条用例已覆盖（**以用例名为准**；当前快照段 `:739-838`，行号随文件增长会漂移，不作为判据）：`R3_BLOCKED_SAMPLES` `:744`；`test_r3_1` `:767` 原串不残留、`test_r3_2` `:774` 占位符对应类别、`test_r3_3` `:787` `redaction_count` 未增、`test_r3_4` `:798` `BLOCKED` + span 齐备、`test_r3_5` `:811` 五轮一致、`test_r3_6` `:823` locator 不含原文，T02 **只复用不重写**（不得放宽断言、删用例、缩小参数化范围）。
  - 三个既有文件回归绿：`tests/integration/test_evidence_service.py`、`tests/unit/test_evidence_models.py`、`tests/unit/test_diagnosis_analyzers.py`。
  - **R4-A 7 词根参数化回归**（依据 05-rulings **R4-A**）：`xx_customer_name` / `some_user_name` / `xx_owner_name` / `prefix_real_name` / `x_display_name` / `y_account_name` / `z_full_name` 逐个断言 `is_name_key(...) is True`——守住「补词根前的欠脱敏缺口已关闭」。
  - **R4-D 正负例逐条断言**（依据 05-rulings **R4-D**）：
    - 正例 **23 条**必须 `True`：`name`、`姓名`、`联系人`、`商户号`、`user_name`、`customer_name`、`display_name`、`account_name`、`full_name`、`owner_name`、`real_name`、`order_no`、`biz_id`、`customer_id`、`xx_order_no`、`Customer-ID`、`xx_customer_name`、`some_user_name`、`xx_owner_name`、`prefix_real_name`、`x_display_name`、`y_account_name`、`z_full_name`。
    - 负例 **15 + 14 条**必须 `False`：`ToolResult` 15 个顶层字段（`tool_name`、`status`、`task_id`、`run_id`、`tool_call_id`、`started_at`、`duration_ms`、`data`、`warnings`、`error`、`source`、`observed_at`、`truncated`、`capability_ref`、`source_metadata`）+ `name_field`、`database_field`、`schema_field`、`keys_field`、`table_name`、`column_name`、`schema_name`、`index_name`、`file_name`、`class_name`、`host_name`、`field_name`、`definition_name`、`nickname`。
    - 判定口径：只断言 `is_name_key()` 的**输入输出行为**，**不**比对模块内部常量名或分组数量（`SYSTEM_METADATA_KEYS` / `NAME_KEY_EXACT` / `NAME_KEY_SUBSTRING` 不要求存在；C 组白名单不要求存在）。
  - **版本断言**：默认 `redaction_version == "redaction-v2"`；证据行 `redaction_version` 与 `REDACTION_APPLIED` 事件 `redaction_version` 相等（AC-08，含同一次调用内捕获事件的离线镜像断言）；历史行 `redaction_version` 与既有 `payload_hash` / `redacted_payload_hash` 在改造前后逐字不变。
  - **通道级不误伤**：真实 `ToolResult.model_dump()` 过闸门① → `status ∈ {CLEAN, REDACTED}`、`data` 未被清空、`tool_name` 逐字不变。
  - **业务号影响登记**（依据 05-rulings **R4-D**）：闸门①接入后逐个工具确认 `data` 是否携带业务号字段（`order_no` / `biz_id` / `customer_id`）——命中者按 R4-D 属**有意** `REVIEW_REQUIRED` + 隔离，需在卡内登记工具清单，避免「接入后带业务号的工具结果全被隔离」的落地意外；**不因此放宽 `is_name_key`**。工具清单**只登记、不改规则**；若确需可用化，走**组合层**（工具 `data` 只回传 rowcount / 脱敏后摘要），不得改引擎词表或加路径白名单。

- **已知限制（KD-W09-02，依据 05-rulings R4-B / R4-D）**
  - 现象：嵌套裸 `name`（如合成载荷 `{"definition": {"name": "mysql_lab_update_order_note"}}`）会命中 A 组精确规则 → `REVIEW_REQUIRED` + `<NAME:...>` 掩码。
  - 定性：属 **fail-closed（不泄露）**，方向是可用性降级，与「欠脱敏」不同级；**不判 FAIL**。
  - 本周处置：**不引入路径感知**（会新增 `json_path` 闭集维护 + 再升版 + 再复验，收益不匹配）；`is_name_key("name") is True` **保持**，不加值形态判定、不加启发式。
  - 触发 W10 的条件：仅在 W10 出现**真实误判**（真实链路而非合成载荷）时，再以「**闭集 `json_path` 白名单 + 升 `redaction_version` + 补回归**」处理；本卡不预判其形态。
  - 边界：`ToolResult.model_dump()` 顶层 15 字段不含 `definition`，该样例**不是**闸门① 的实际输入；`source_metadata` 键名本身属负例，其中若嵌套 `name` 键即按本条处理。

- **验收命令**
```bash
python -m pytest tests/unit/test_w09_redaction_gate.py -q
python -m pytest tests/unit/test_evidence_models.py tests/unit/test_diagnosis_analyzers.py -q
python -m pytest tests/integration/test_evidence_service.py -q          # DB 不可用时应 skipped 且带原因，不得 failed
python -m pytest tests/integration/test_w09_redaction_pipeline.py -q   # 同上
python -m pytest tests/unit -q
```

- **环境注意（`tests/integration/test_evidence_service.py`）**：该文件 **6 个用例 → 6 passed + 6 teardown errors**；`admin_client` 清理需管理员权限（root 报 `1045`），属**环境缺陷、非用例失败**（用例本体全过）；残留行**不得 `DELETE`**（全局纪律：需要 DB 的用例只 `INSERT`、不清理）。注：先前「10 passed」系把 `test_w09_redaction_pipeline.py` 的 4 条合并统计，team-lead 已确认更正为 6 + 6。

---

## T03｜结构化报告 + KD-1 补偿拒绝事件

- **依赖**：T02
- **源需求**：P0-W09-03（`01-prd.md:79-85`）；AC-09/AC-10/AC-11/AC-12（`:139-142`）；W08 KD-1（`docs/w08/07-gate-review.md:65`）；设计 §4、§7 KD-1、§8.3
- **涉及文件**（新建 7 / 修改 2 / 迁移 2 / 测试 3+）
  - 新建：`core/report/{__init__,models,validate,render,export,builder,repository}.py`、`database/migrations/010_w09_report.sql` + `rollback/010_rollback.sql`
  - 修改：`core/approval/repository.py`、`core/approval/service.py`
  - 测试：`tests/unit/test_w09_report_schema.py`、`tests/integration/test_w09_report_evidence_ref.py`、`tests/unit/test_w09_compensation_denied.py`（新建）；P1 可选 `tests/unit/test_w09_report_export.py`

- **关键签名（可直接抄）**

```python
# core/report/models.py
class FactStatus(str, Enum):
    CONFIRMED = "CONFIRMED"; PARTIAL = "PARTIAL"; STALE = "STALE"
    DENIED = "DENIED"; UNKNOWN = "UNKNOWN"; CONFLICTED = "CONFLICTED"

class ActionState(str, Enum):
    PROPOSED = "PROPOSED"; NOT_EXECUTED = "NOT_EXECUTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"; EXECUTED = "EXECUTED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"; VERIFIED = "VERIFIED"

class VerificationState(str, Enum):
    NONE = "NONE"; PENDING = "PENDING"; PASS = "PASS"; FAIL = "FAIL"; UNKNOWN = "UNKNOWN"

class Recommendation(BaseModel):            # extra="forbid"
    text: str = Field(min_length=1, max_length=1000)
    state: ActionState = ActionState.PROPOSED
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"
    requires_approval: bool = False
    @field_validator("text")
    @classmethod
    def _no_completion_wording(cls, value: str) -> str: ...

class StructuredReport(BaseModel):          # extra="forbid"
    report_id: str; report_version: str = "report-v1"
    tenant_id: str; task_id: str
    database_id: str; db_engine: str; db_version: str; adapter_version: str
    environment_id: str; environment_fingerprint_digest: str | None = None
    policy_version: str
    conclusion: str = Field(min_length=1, max_length=2000)
    fact_status: FactStatus; confidence_band: ConfidenceBand
    supporting_evidence_ids: list[str]; contradicting_evidence_ids: list[str]
    missing_evidence: list[str]
    recommendations: list[Recommendation]
    action_state: ActionState; verification_state: VerificationState
    verification_result_id: str | None = None
    scope: str = "LAB"
    degraded: bool = False; degrade_reason: str | None = None
    redaction: RedactionSummary | None = None
    @model_validator(mode="after")
    def _enforce_state_consistency(self) -> "StructuredReport": ...

# core/report/validate.py
class ReportEvidencePort(Protocol):
    def fetch_by_ids(self, tenant_id: str, evidence_ids: Sequence[str]) -> list[Evidence]: ...

def derive_fact_status(report: StructuredReport, evidence: Sequence[Evidence]) -> FactStatus: ...
def validate_evidence_refs(report: StructuredReport, port: ReportEvidencePort) -> "EvidenceRefCheck":
    ...   # dataclass: fact_status / missing_evidence / weak_ids / env_mismatch_ids

# core/report/render.py
def sanitize_text(value: str) -> str: ...
def render_markdown(report: StructuredReport) -> str: ...
def to_html(report: StructuredReport) -> str: ...        # 仅 <pre> + 纯文本转义

# core/report/export.py
def to_csv_rows(report: StructuredReport, *, gate: RedactionGate) -> list[list[str]]: ...
def to_json_document(report: StructuredReport, *, gate: RedactionGate) -> str: ...

# core/report/builder.py
class ReportBuilder:
    def __init__(self, *, evidence: ReportEvidencePort, verifications, steps,
                 audit, gate: RedactionGate, reports: ReportRepository) -> None: ...
    def build(self, *, task: Task, facts: ReportFacts,
              recommendations: Sequence[Recommendation]) -> StructuredReport: ...

# core/approval/repository.py（新增，KD-1）
def find_compensation_for(self, original_approval_id: str) -> str | None: ...
```

- **必须实现的语义**
  1. **两组六态封闭且两两不等价**（AC-09）：`FactStatus` 6 值 / `ActionState` 6 值；`model_validator` 强制 `ActionState` 由证据链阶段唯一确定（设计 §4.2 表），`VERIFIED` ⇒ `verification_state == PASS` ∧ `verification_result_id` 非空。
  2. **引用归属校验**（AC-09）：跨 `tenant_id` 或不存在 → `EVIDENCE_REF_INVALID`（不区分二者，沿用 `core/evidence/repository.py:126-136` 口径）；`environment_id` 与报告不一致 → 记 `env_mismatch_ids` 并降 `STALE`。
  3. **弱证据不升格**（AC-10）：`collection_status ∈ WEAK_COLLECTION_STATUSES` 或 `quality ∈ {LOW, UNKNOWN}` 被引用 ⇒ `fact_status != CONFIRMED`（`derive_fact_status` 第 5 条 + `model_validator` 在 `CONFIRMED` 时主动复核）。
  4. **失效引用**（AC-12）：`fact_status=STALE` 且 `missing_evidence` 非空；不得静默剔除后仍报 `CONFIRMED`。
  5. **措辞禁令**（AC-11）：`Recommendation.text` 与「未执行态」的 `conclusion` 禁含 `已修复/已执行/已完成/已恢复/已解决/fixed/resolved/already applied`（大小写不敏感）→ `ValidationError`。
  6. **安全渲染**（AC-12）：`<script>` 前后（含大小写、`< script`）一律转义；渲染结果不得含 `<script`；不生成任何 HTML 标签（`to_html` 只包 `<pre>`）。
  7. **导出复扫**（AC-07）：`to_csv_rows`/`to_json_document` 在**拼接完成后**调用 `gate` 的 `EXPORT` 通道；命中即抛错，不产生文件。
  8. **KD-1**：`ApprovalService.consume()` 第 2 步（`core/approval/service.py:581-582`）在 `status is CONSUMED` 时用 `find_compensation_for(approval_id)` 反查；命中则**先发** `COMPENSATION_DENIED(reason=COMPENSATION_REUSES_ORIGINAL_APPROVAL, deny_code=APPROVAL_ALREADY_CONSUMED, original_approval_id, compensation_approval_id)` 再抛错；发事件用 `try_emit`（拒绝优先，写失败不改判定）；`reason` 常量在 `core/approval/service.py` 内定义，并由测试断言与 `core.verification.compensation.COMPENSATION_REUSES_ORIGINAL_APPROVAL` **同值**（防漂移，且不引入反向依赖）。
  9. **落快照**：`ReportRepository.insert` 写 `report_snapshots`（只追加）。

- **禁止项**
  - 禁止让 `FactStatus`/`ActionState` 支持「布尔组合推导」（必须由阶段唯一确定）；禁止新增第 7 个状态。
  - 禁止 `VERIFIED` 接受 `verdict != PASS`（含 `UNKNOWN`）；禁止把 `UNKNOWN` 映射成 `EXECUTED` 或「已修复」。
  - 禁止用正则白名单「清洗」HTML 标签（不可靠）；只能纯文本化。
  - 禁止在 `apps/api/**` 加报告查询端点（P2-W09-02 明确延期，`01-prd.md:122`）。
  - 禁止在 `consume()` 里重排/绕过任何一项校验（只在第 2 步**之前**补一条事件）。
  - 禁止为让报告「好看」而放宽引用校验（例如把跨租户 id 过滤掉后继续 `CONFIRMED`）。
  - 禁止改 `core/evidence/**`（T02 所有）与 `core/gateway/**`。

- **完成判据**
  - AC-09/AC-10/AC-11 的单测全绿；AC-12 的 `STALE` + `missing_evidence` 断言绿；`REPORT_GENERATED` 事件含 `fact_status/action_state/verification_state/degraded`。
  - KD-1：复用原 `approval_id` 执行补偿 → `COMPENSATION_DENIED` 事件 1 条且 `reason=COMPENSATION_REUSES_ORIGINAL_APPROVAL`；目标行不变（复用 W08 既有断言）。
  - 导出列名与冻结清单逐字相等（P1 若做）。

- **验收命令**
```bash
python -m pytest tests/unit/test_w09_report_schema.py -q
python -m pytest tests/unit/test_w09_compensation_denied.py -q
python -m pytest tests/integration/test_w08_compensation.py -q        # W08 既有，必须仍绿（DB 不可用时 skipped 带原因）
python -m pytest tests/integration/test_w09_report_evidence_ref.py -q
python -m pytest tests/unit -q
```

---

## T04｜五类记忆 + `KnowledgeEntry` + 冲突解析器

- **依赖**：T03
- **源需求**：P0-W09-04（`01-prd.md:87-93`）；AC-13/AC-14/AC-15（`:143-145`）；设计 §0 A5、§5、§8.4
- **涉及文件**（新建 7 / 迁移 2 / 测试 2）
  - 新建：`core/memory/{__init__,models,resolver,repository,service,working,episodic}.py`、`database/migrations/011_w09_memory.sql` + `rollback/011_rollback.sql`
  - 测试：`tests/unit/test_w09_memory_models.py`、`tests/integration/test_w09_conflict_resolver.py`

- **关键签名（可直接抄）**

```python
# core/memory/models.py
class MemoryKind(str, Enum):
    WORKING = "WORKING"; EPISODIC = "EPISODIC"; SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"; ENVIRONMENT = "ENVIRONMENT"

class KnowledgeStatus(str, Enum):
    PROPOSED = "PROPOSED"; INDEXED = "INDEXED"; CONFLICTED = "CONFLICTED"
    REVOKED = "REVOKED"; EXPIRED = "EXPIRED"

class ValidationLevel(str, Enum):
    UNVERIFIED = "UNVERIFIED"; SINGLE_CASE = "SINGLE_CASE"
    REGRESSION = "REGRESSION"; FIELD = "FIELD"

class MemoryQuery(BaseModel):            # extra="forbid"；无 registry/gateway/dsn 字段
    tenant_id: str; db_engine: str; db_version: str; adapter_version: str | None = None
    capabilities: tuple[str, ...] = (); environment_fingerprint_digest: str | None = None
    risk_level: str = "R0"

class KnowledgeEntry(BaseModel):         # extra="forbid"
    knowledge_id: str; revision: int = Field(ge=1); tenant_id: str
    kind: MemoryKind; status: KnowledgeStatus = KnowledgeStatus.PROPOSED
    db_engine: str | None = None
    version_min: str | None = None; version_max: str | None = None
    adapter_version: str | None = None
    capability_scope: tuple[str, ...] = ()
    environment_fingerprint_digest: str | None = None
    risk_scope: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = (); test_case_ids: tuple[str, ...] = ()
    validation_level: ValidationLevel = ValidationLevel.UNVERIFIED
    reviewed_by: str | None = None; reviewed_at: str | None = None
    effective_from: str; effective_until: str
    revoked_at: str | None = None; revoked_reason: str | None = None
    source_license: str = Field(min_length=1)
    supersedes: str | None = None
    content: dict[str, Any]; redaction_version: str
    @property
    def is_indexable(self) -> bool: ...
    def indexable_at(self, now: datetime) -> bool: ...      # 显式时钟，供解析器使用

# core/memory/resolver.py
class MemoryResolution(BaseModel):
    available: tuple[KnowledgeEntry, ...]
    conflicted: tuple[KnowledgeEntry, ...]
    excluded: tuple[tuple[str, str], ...]                   # (knowledge_id, reason)
class MemoryConflictResolver:
    def __init__(self, *, max_candidates: int = 20) -> None: ...
    def resolve(self, *, query: MemoryQuery,
                entries: Sequence[KnowledgeEntry],
                now: datetime | None = None) -> MemoryResolution: ...

# core/memory/service.py
class MemoryService:
    def __init__(self, repository: KnowledgeRepository, *, gate: RedactionGate, audit) -> None: ...
    def record(self, entry: KnowledgeEntry, *, principal) -> KnowledgeEntry: ...
    def review(self, knowledge_id: str, revision: int, *, reviewed_by: str,
               validation_level: ValidationLevel) -> KnowledgeEntry: ...
    def revoke(self, knowledge_id: str, revision: int, *, reason: str, principal) -> KnowledgeEntry: ...
    def expire_due(self, now: datetime | None = None) -> tuple[str, ...]: ...
    def list_versions(self, knowledge_id: str, *, tenant_id: str) -> tuple[KnowledgeEntry, ...]: ...
```

- **必须实现的语义**
  1. **五类封闭枚举，大小 == 5**（AC-13）；`extra="forbid"`。
  2. **`is_indexable`**：仅 `SEMANTIC` ∧ `status=INDEXED` ∧ `reviewed_by` 非空 ∧ `reviewed_at` 非空 ∧ `validation_level != UNVERIFIED` ∧ 未撤销 ∧ 在有效期内（AC-13 两条：缺审核人、已过期 → `False`）。
  3. **8 道硬过滤先于比较**（AC-14）：顺序与短路见设计 §5.4；`postgresql` 知识在 `mysql` 任务上返回空集，**不因文本/向量相似度命中**（结构上没有相似度）。过滤原因逐一可枚举（`ENGINE_MISMATCH` 等）。
  4. **冲突组**：前两名 `content_digest` 不同 ⇒ 两条均 `CONFLICTED`、进 `conflicted`、不进 `available`，并要求当前工具重新验证（本卡只产出判定，不触发执行）。
  5. **撤销/过期保留版本与原因**：`revoke` **追加** `revision+1`（`status=REVOKED`、`superseded=原 id`），原行不改；`list_versions` 返回全量；撤销后仍可按 `knowledge_id` 查到并带 `revoked_reason`（AC-14 集成断言）。
  6. **记忆不授权执行**（AC-15）：`inspect.signature` 无 `tool_registry/gateway/dsn/ro_dsn/rw_dsn/control_dsn`；`KnowledgeEntry.model_fields` 无上述键；`core/memory/*.py` 的 `ast` 导入扫描禁止 `core.tools.registry`/`core.gateway`/`core.tools.executor`/`database.mysql`/`database.postgres`。
  7. **入库前脱敏**：`MemoryService.record` 用 `gate` 的 `EVIDENCE` 通道扫描 `content`，`redaction_version` 落库；不通过则拒绝入库。
  8. **`WORKING` 不落库**、`EPISODIC` 只读投影（`task_events` + `evidence`）：二者**不得**出现在 `knowledge_entries` 的落库路径（`record` 对这两种 kind 抛 `KNOWLEDGE_INVALID`）。
  9. `KNOWLEDGE_REVOKED` 事件（T01 已加枚举）在 `revoke` 成功后发出。

- **禁止项**
  - 禁止引入向量库/embedding/相似度打分（P2-W09-01 延期）；禁止用 `difflib` 之类做「模糊匹配」代替硬过滤。
  - 禁止 `resolve` 写库（只判定）；禁止 `revoke` 使用 `UPDATE`（只能追加新 revision）。
  - 禁止在 `core/memory/**` 导入工具注册表/Gateway/数据库驱动；禁止给 `MemoryQuery`/`resolve` 加 DSN 或 registry 形参。
  - 禁止让 `PROCEDURAL`/`ENVIRONMENT` 进入 `available` 授权集（设计 §5.1：只有 `SEMANTIC` 可索引）。
  - 禁止把「相似度命中」写成 `CONFLICTED`（冲突只能由同组候选的 `content_digest` 差异产生）。
  - 禁止改 `core/evidence/**`、`core/context/**`（T02 所有）。

- **完成判据**
  - AC-13/AC-14/AC-15 全绿（含 `inspect.signature` 与 AST 导入扫描两重结构断言）。
  - 迁移 011 在控制库可应用（`python scripts/migrate.py` 输出含 `011_w09_memory`），且表无 `UPDATE`/`DELETE` 语义（`dg_ctl` 授权不变）。
  - 撤销后 `list_versions` 至少 2 个 revision，`revoked_reason` 可读。

- **验收命令**
```bash
python -m pytest tests/unit/test_w09_memory_models.py -q
python -m pytest tests/integration/test_w09_conflict_resolver.py -q   # DB 不可用时 skipped 带原因
python -m pytest tests/unit -q
```

---

## T05｜模型降级

- **依赖**：T02、T04
- **源需求**：P0-W09-05（`01-prd.md:95-102`）；AC-16/AC-17/AC-18（`:146-148`）；设计 §0 A9、§6
- **涉及文件**（新建 2 / 修改 4 / 测试 2）
  - 新建：`core/models/degradation.py`、`core/report/brief.py`
  - 修改：`core/models/factory.py`、`core/agent/loop.py`、`apps/api/routes/agent.py`（组装，不加端点）
  - 测试：`tests/unit/test_w09_model_degradation.py`、`tests/integration/test_w09_degrade_audit.py`

- **关键签名（可直接抄）**

```python
# core/models/degradation.py
class DegradeReason(str, Enum):
    MODEL_DISABLED = "MODEL_DISABLED"; MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"; MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    QUALITY_REGRESSION = "QUALITY_REGRESSION"
    QUALITY_REGRESSION_NO_VERIFIED_VERSION = "QUALITY_REGRESSION_NO_VERIFIED_VERSION"

class ModelState(str, Enum):
    AVAILABLE = "AVAILABLE"; SUSPENDED = "SUSPENDED"

class DisabledModelGateway:
    """provider=disabled 时的占位网关：所有方法抛 ModelUnavailableError，不装配适配器。"""
    def generate(self, *args, **kwargs): ...
    def generate_structured(self, *args, **kwargs): ...

class ModelVersionPin:
    def __init__(self, *, manifest_path: Path, current_version: str) -> None: ...
    def previous_verified(self) -> str | None: ...     # model_commit 为 null 时恒为 None

class DegradationController:
    def __init__(self, *, audit, provider: str, model_version: str | None = None,
                 pin: ModelVersionPin | None = None,
                 max_repair_retries: int = MAX_REPAIR_RETRIES_LIMIT) -> None: ...
    @classmethod
    def from_settings(cls, settings, *, audit, pin: ModelVersionPin | None = None) -> "DegradationController": ...
    @property
    def state(self) -> ModelState: ...
    @property
    def planning_suspended(self) -> bool: ...
    def record_failure(self, reason: DegradeReason, *, task_id: str, run_id: str) -> None: ...
    def repair_attempt(self, attempt: int, *, task_id: str, run_id: str) -> str: ...   # "RETRY" | "STOP"
    def resume(self, verified_version: str | None) -> bool: ...

# core/report/brief.py
class DegradedBrief(BaseModel):        # extra="forbid"
    task_id: str; degraded: bool = True; degrade_reason: DegradeReason
    model_used: bool = False; planning_suspended: bool = True
    fact_status: FactStatus            # 只允许 UNKNOWN / PARTIAL
    evidence_ids: tuple[str, ...]; missing_evidence: tuple[str, ...]
    observations: tuple[str, ...]      # 已脱敏的证据摘要
    redaction: RedactionSummary | None = None

def build_rule_brief(*, task: Task, evidence: Sequence[Evidence],
                     summaries: Sequence[str], reason: DegradeReason,
                     gate: RedactionGate) -> DegradedBrief: ...
```

- **必须实现的语义**
  1. **`disabled` 可构造**（A9）：`build_model_gateway` 对 `model_provider="disabled"` 返回 `DisabledModelGateway`，**不再抛 `ConfigError`**（现状 `core/models/factory.py:28-32` 会抛）。
  2. **暂停新规划**（AC-16）：`AgentLoop.run` 在进入 planner **之前**检查 `planning_suspended`；`True` 时**不调用** planner、不调用 `tool_call`，直接返回 `DegradedBrief`（`degraded=True`、`model_used=False`）。
  3. **规则化简报**：只基于已有 `Evidence`（`DiagnosisRunner` 的 `rules-v0` 口径，`core/diagnosis/runner.py:265`），`fact_status ∈ {UNKNOWN, PARTIAL}` **永不 `CONFIRMED`**；简报文本经 `gate` 再脱敏。
  4. **非法输出只重试 1 次**（AC-17）：复用 `MAX_REPAIR_RETRIES_LIMIT = 1`（`core/recovery/controller.py:54`）；`repair_attempt(1)` → `"RETRY"`，`repair_attempt(2)` → `"STOP"`；`adapter.complete` 调用次数 == 2（mock 计数），无第 3 次。
  5. **降级事件**（AC-18）：`MODEL_DEGRADED` 落 `task_events`（payload：`reason`、`planning_suspended`、`model_used=false`、`retry_count`、`model_version`（可为 null）、`pin_target`（可为 null））；降级路径 `tool_calls` 增量 == 0。事件顺序：`AGENT_RECOVERY_STARTED` → `AGENT_RECOVERY_STOPPED(reason=MODEL_OUTPUT_INVALID)` → `MODEL_DEGRADED`（设计 §11 R7）。
  6. **质量回归路由**（A9）：`pin.previous_verified()` 为 `None`（当前 `model_commit=null`，`benchmarks/baseline_v0/baseline_manifest.json:6`）⇒ `reason=QUALITY_REGRESSION_NO_VERIFIED_VERSION`，保持 `SUSPENDED`；**不指定任何模型版本**。
  7. **不触凭据/不执行**（AC-18）：`DegradationController.__init__` 与 `build_rule_brief` 的 `inspect.signature` 不含 `ro_dsn/rw_dsn/control_dsn/pg_ro_dsn`；`tool_call()` 的 `allow_write=False` 默认不变（`core/models/gateway.py:179`）。

- **禁止项**
  - 禁止凭空指定降级目标模型版本（`model_commit` 为 `null` 是事实，不得用 `model_name` 顶替）。
  - 禁止第 3 次调用（重试上限恒为 1，不得做成可配置的 >1）。
  - 禁止在降级路径调用任何工具/planner（`tool_calls_delta` 必须为 0）。
  - 禁止让降级简报出现「模型已诊断」「已修复」等措辞；禁止 `fact_status=CONFIRMED`。
  - 禁止改 `allow_write` 默认值、禁止给 `ModelGateway`/适配器加 DSN 参数。
  - 禁止在 `apps/api/**` 新增端点（只改 `_build_loop` 的装配）。
  - 禁止为了通过测试而让 `DegradationController` 读全局单例/环境变量绕过显式注入。

- **完成判据**
  - AC-16/AC-17/AC-18 全绿；`inspect.signature` 结构断言（模型输入构造不含 DSN 形参）绿。
  - `model_provider=disabled` 时 API 装配**不崩**（`build_model_gateway` 返回 `DisabledModelGateway`），`AgentLoop.run` 返回 `degraded=True`。

- **验收命令**
```bash
python -m pytest tests/unit/test_w09_model_degradation.py -q
python -m pytest tests/unit/test_model_gateway.py tests/unit/test_model_isolation.py -q   # 既存，必须仍绿
python -m pytest tests/integration/test_w09_degrade_audit.py -q
python -m pytest tests/unit -q
```

---

## T06｜脱敏回归样本集 + 训练样本 + `sweep()` 扫描总数（含 KD-3/4/5）

- **依赖**：T03、T05
- **源需求**：P0-W09-06（`01-prd.md:104-111`）；AC-19/AC-20（`:149-150`）、AC-21 回归（`:151`）；W08 KD-2/3/4/5（`docs/w08/07-gate-review.md:66-69`）；设计 §0 A6/A7/A8、§7
- **涉及文件**（新建 2 / 修改 4 / 测试 3）
  - 新建：`core/report/samples.py`、`tests/unit/test_w09_sweep_scanned.py`、`tests/unit/test_w09_redaction_samples.py`
  - 修改：`core/execution/reconciler.py`、`workers/runner.py`、`core/audit/service.py`、`tests/unit/test_worker_scheduler.py`（**只加用例，不改既有断言**）

- **关键签名（可直接抄）**

```python
# core/execution/reconciler.py
@dataclass(frozen=True)
class SweepReport:
    scanned_total: int                       # 真实扫描行数（分页 find_unobserved 返回行数之和）
    conclusions: tuple[ReconcileResult, ...] # 产出结论条数 == len(conclusions)
    scanned_tenants: tuple[str, ...]

class Reconciler:
    def sweep(self, *, tenant_id: str | None = None,
              older_than_s: float = 60.0) -> list[ReconcileResult]: ...        # 签名/返回值不变
    def sweep_detailed(self, *, tenant_id: str | None = None,
                       older_than_s: float = 60.0) -> SweepReport: ...          # 新增

# workers/runner.py（RunReport 增量）
@dataclass(frozen=True)
class RunReport:
    ...
    reconciled: int = 0
    reconcile_failed: int = 0        # 新增：failed 的子集
    reconcile_scanned_total: int = 0 # 新增：本轮真实扫描行数（跨轮累加）
# WorkerRunner(..., monotonic: Callable[[], float] = time.monotonic)  # 节流时钟，可注入

# core/audit/service.py（KD-3）
def emit_required(self, event: TaskEvent, *, actor_user_id: str | None = None,
                  tenant_id: str | None = None) -> TaskEvent: ...
def try_emit(self, event: TaskEvent, *, actor_user_id: str | None = None,
             tenant_id: str | None = None) -> bool: ...

# core/report/samples.py
class SkippedSample(BaseModel):      # extra="forbid"
    reason: str; detected_categories: tuple[str, ...]; source_digest: str
def build_training_sample(*, trajectory: Trajectory, evidences: Sequence[Evidence],
                          gate: RedactionGate) -> dict[str, Any] | SkippedSample: ...
```

- **必须实现的语义**
  1. **版本化回归样本集**（AC-19）：五类（多语言 / SQL 注释 / `\` 转义 / 连接串 / 嵌套 JSON）各 1 例，每例三断言「原始含敏感 / 脱敏后不含 / `detected_categories` 正确」；样本集断言 `redaction_version == engine.version` 且非空。
  2. **训练样本出口复扫**（AC-07 训练样本分支）：`build_training_sample` 对拼接后的样本再扫描；`BLOCKED`/`REVIEW_REQUIRED` → 返回 `SkippedSample` 且**不落盘**；`CLEAN`/`REDACTED` → 样本头含 `redaction_version` 与 `source_payload_hash`。
  3. **`scanned_total` 可观测**（AC-20 / KD-2）：5 行候选仅 2 行产出结论 → `scanned_total == 5` 且 `len(conclusions) == 2`；`RECONCILIATION_SWEPT` payload **新增** `scanned_total`，`scanned` 保留原语义（= `len(conclusions)`）。
  4. **`sweep()` 契约不变**（A6）：仍返回 `list[ReconcileResult]`；`tests/integration/test_step_prepared.py:211` 的 `results == []` 必须仍成立。
  5. **KD-3**：`_emit_scheduler_event` 传 `tenant_id=stack.tenant_id`；`emit_required/try_emit` 的可选 `tenant_id` 缺省仍走 `_resolve_tenant(task_id)`（`core/audit/service.py:209-255`），**禁止 payload 兜底**；历史 NULL 行不改写。
  6. **KD-4**：`reconcile_interval_s` 生效为节流；`getattr(settings, "reconcile_interval_s", 0.0)` 缺省 `0.0`（替身不节流）；未到间隔的轮次**不发** `RECONCILIATION_SWEPT`，只记日志 `reason=RECONCILE_INTERVAL_NOT_DUE`。
  7. **KD-5**：`reconcile_failed` 与 `failed` 同步累加（子集），`failed` 语义与 `clean` 判定逐字不变（保护 `tests/unit/test_worker_scheduler.py:402` 的 `failed == 1`）。
  8. **AC-21 回归**：`tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py` 全绿，PG 工具集仍 7 个且全 `side_effect=read`，写工具集合仍 `LAB+mysql` 单一工具。

- **禁止项**
  - **禁止改变 `sweep()` 的签名/返回类型**（不得改成返回 `SweepReport` 或自定义 Sequence）——会让 `results == []` / `results[0]` 等 7 处既有调用点红。
  - 禁止把 `scanned` 改名或改语义（只增 `scanned_total`）。
  - 禁止改 `failed`/`clean` 语义；禁止删除或放宽 `tests/unit/test_worker_scheduler.py` 的既有断言（只允许**新增**用例）。
  - 禁止把样本集阈值改成「只要有 1 例通过」；五类各 1 例必须逐例三断言。
  - 禁止用 `DG_ADMIN_DSN` 建/清样本数据；禁止依赖 PG。
  - 禁止为让 `scanned_total` 好看而在 `_reconcile_one` **之后**计数（必须在过滤前计数）。
  - 禁止在 `AuditService` 里用 `event.payload["tenant_id"]` 作过滤列来源（W06 冻结：`docs/w06/05-freeze.md` A.3 口径不变）。

- **完成判据**
  - AC-19/AC-20 全绿；`test_worker_scheduler.py` 既有 22 个用例**逐字仍绿**，新增用例覆盖 `scanned_total`/`reconcile_failed`/节流/租户列。
  - `RECONCILIATION_SWEPT` 同时含 `scanned`（=2）与 `scanned_total`（=5）的构造断言绿。
  - AC-21 两项既存测试绿。

- **验收命令**
```bash
python -m pytest tests/unit/test_w09_redaction_samples.py -q
python -m pytest tests/unit/test_w09_sweep_scanned.py -q
python -m pytest tests/unit/test_worker_scheduler.py -q
python -m pytest tests/unit/test_write_tool_gate.py tests/unit/test_tools_contracts.py -q
python -m pytest tests/unit -q
```

---

## 验收总表（AC → 卡 → 测试文件）

| AC | 判据要点 | 卡 | 测试文件 |
|---|---|---|---|
| AC-01 | 12 类逐类命中 | T01 | `tests/unit/test_w09_redaction_engine.py` |
| AC-02 | 5 轮确定性（count/version/hash） | T01 | 同上 |
| AC-03 | 未覆盖样本 → 非 `REDACTED` | T01 | 同上 |
| AC-04 | 哈希口径 + `extra="forbid"` | T01 | 同上 |
| AC-05 | 持久化前闸门（0 新增行） | T02 | `tests/integration/test_w09_redaction_pipeline.py`（+ 离线镜像 `test_w09_redaction_gate.py`） |
| AC-06 | 模型输入前闸门 + `REDACTION_BLOCKED` | T02 | 同上 |
| AC-07 | 导出/训练集再扫描 | T02/T03/T06 | 同上 + `test_w09_report_export.py` + `test_w09_redaction_samples.py` |
| AC-08 | 证据行与事件 `redaction_version` 一致 | T02 | `test_w09_redaction_pipeline.py` |
| AC-09 | 引用归属 + 六态两两不等价 | T03 | `tests/unit/test_w09_report_schema.py` |
| AC-10 | 弱证据不升格 | T03 | 同上 |
| AC-11 | 措辞禁令 | T03 | 同上 |
| AC-12 | 安全渲染 + `STALE`/`missing_evidence` | T03 | `tests/integration/test_w09_report_evidence_ref.py` |
| AC-13 | 五类枚举 + `is_indexable` | T04 | `tests/unit/test_w09_memory_models.py` |
| AC-14 | 硬过滤先行 + 冲突组 + 撤销可查 | T04 | 同上 + `tests/integration/test_w09_conflict_resolver.py` |
| AC-15 | 记忆不授权执行（签名/AST） | T04 | `tests/unit/test_w09_memory_models.py` |
| AC-16 | `disabled` → 暂停规划 + 降级简报 | T05 | `tests/unit/test_w09_model_degradation.py` |
| AC-17 | 非法输出只重试 1 次 | T05 | 同上 |
| AC-18 | 降级不触凭据 + `MODEL_DEGRADED` + 0 工具调用 | T05 | `tests/integration/test_w09_degrade_audit.py` |
| AC-19 | 五类样本三断言 + 版本非空 | T06 | `tests/unit/test_w09_redaction_samples.py` |
| AC-20 | `scanned_total` / `conclusions` 分离 | T06 | `tests/unit/test_w09_sweep_scanned.py` + `test_worker_scheduler.py`（扩展） |
| AC-21 | 写面与引擎不回退 | T06 | `tests/unit/test_write_tool_gate.py` + `tests/unit/test_tools_contracts.py` |

## W08 承接项落卡

| 承接项 | 落卡 | 关键文件 |
|---|---|---|
| KD-1 补发 `COMPENSATION_DENIED` | **T03** | `core/approval/{service,repository}.py` |
| KD-2 `sweep()` 真实扫描总数 | **T06** | `core/execution/reconciler.py`、`workers/runner.py` |
| KD-3 调度事件租户归属/检索方式 | **T06** | `core/audit/service.py`、`workers/runner.py` |
| KD-4 `reconcile_interval_s` 语义 | **T06** | `workers/runner.py` |
| KD-5 `RunReport` 核对失败计数位 | **T06** | `workers/runner.py` |

> 本表**不代表** W08 已通过门禁；W08 结论仍为「有条件放行」（`docs/w08/07-gate-review.md:10`）。KD 的修复完成不等于 W08 条件 C-1…C-4 全部关闭。

## 环境约束（所有卡共用，写进每张卡的自检清单）

| 约束 | 具体要求 |
|---|---|
| PG 5432 不可连 | 不得有任何新用例依赖 PG；PG 相关既有用例按原状 skipped/failed **不算本批回归失败**，但也不得被用来「证明通过」 |
| `DG_ADMIN_DSN` 1045 | 测试**不得**调用 admin 清理；不得断言「表已清空」；残留按 E-3 如实登记 |
| 控制库 830+ 历史行 | `sweep` 断言一律用**构造数据**（注入替身），不扫真实历史 |
| 门禁表述 | 禁止「W08 已通过」「环境已可复现清零」（`docs/w07/07-gate-review.md:129-137` 表述禁令延续） |
