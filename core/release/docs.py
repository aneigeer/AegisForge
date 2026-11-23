"""W12 T04：文档、模型与已知限制清单（AC-13 / AC-14 / AC-15）。

口径来源：`docs/w12/01-prd.md` §5（AC-13…AC-15）、`docs/w12/02-design.md` §4.3/§6/§9.3–§9.5、
`docs/w12/03-task-cards.md` T04、`docs/w10-w12-lead-rulings.md` §4（W12-Q5/Q8）、
§13（KD-W11-01/02/03）、§14（KD-W12-01/02）。

三条验收的落地方式：

| AC | 口径 | 落点 |
|---|---|---|
| AC-13 | OpenAPI 主版本 `3.1`；四类文档清单逐项 `exists`；完整性如实 `PARTIAL` | :func:`export_openapi_31` / :func:`collect_doc_inventory` |
| AC-14 | `baseline_score is None` + `未微调` / `指标未测得` / `BLOCKED_MODEL_ENV_MISSING` | :class:`DocInventory` 校验器 |
| AC-15 | 六类不承诺能力逐条进限制清单，带 `NOT_SUPPORTED`/`PARTIAL`/`UNKNOWN` | :data:`NON_PROMISES` |

**限制清单不可删（W12-Q8 / AC-21）**：13 个自动基础项（W11 六项 + W12 六类不承诺 +
`DOC-PARTIAL`）由常量派生，`KnownLimitations` 校验器**重算**并逐字比对 marker/statement，
缺失或改字 → 构造失败（`LIMITATION_REMOVED`）。人工项 `origin=MANUAL`，`added_by`/`reason`
必填，**只能追加**。

**W11 同源对账（D-09）**：优先只读 `docs/w11/limitations.json` 取六项并算
``w11_source_digest``；仅当文件缺失才用 :data:`W11_AUTO_BASE_SPEC` 常量派生并标
``w11_source_status = NOT_AVAILABLE``。**绝不**写 `docs/w11/**`、**绝不**伪造 W11 输出。

**单一数据源**：`docs/w12/known-limitations.json` 是唯一真值；`known-limitations.md`
内嵌**同一函数序列化**的 JSON 块，并与 `.json` 做 `items_digest` / `w11_source_digest` 对账。

**零新增依赖**：OpenAPI 走既有 FastAPI（`create_app().openapi()`）；摘要走
`core/security/canonical.py::canonical_digest`；不改 `apps/api/main.py`、不加端点。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from core.release import AcceptanceStatus
from core.security.canonical import canonical_digest

#: 限制清单版本（单一数据源版本号）。
LIMITATIONS_VERSION: str = "w12-limitations-v1"

#: 限制清单的两个落位（`.json` 为唯一真值，`.md` 内嵌同源块）。
LIMITATIONS_JSON: str = "docs/w12/known-limitations.json"
LIMITATIONS_MD: str = "docs/w12/known-limitations.md"

#: W11 六项自动基础项（`docs/w11/limitations.json` 逐字镜像；文件缺失时的 fallback）。
#: 顺序固定：`ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL` /
#: `DECISION-TECH-PREVIEW`。
W11_AUTO_BASE_SPEC: tuple[dict[str, str], ...] = (
    {
        "item_id": "ENV-PG5432",
        "marker": "NOT_SUPPORTED",
        "statement": (
            "PostgreSQL LAB（5432）拒绝连接：跨引擎只读诊断的 PG 段未执行，"
            "按 NOT_EXECUTED 处置，不判通过、不冒充已执行。"
        ),
    },
    {
        "item_id": "ENV-1045",
        "marker": "NOT_SUPPORTED",
        "statement": (
            "DG_ADMIN_DSN（root@localhost）认证失败 1045：库级物理备份恢复不可用，"
            "W11 只做逻辑影子回放，不声称物理恢复已支持。"
        ),
    },
    {
        "item_id": "ENV-RESIDUE",
        "marker": "PARTIAL",
        "statement": (
            "控制库只追加表存在未观测历史残留（W09 实测 140 行且随时间增长）："
            "W11 各卡继续只追加不清理，所有计数按 task_id/run_id 过滤。"
        ),
    },
    {
        "item_id": "ENV-NV1",
        "marker": "UNKNOWN",
        "statement": (
            "容器运行态 NOT_VERIFIED、镜像 digest 为 UNKNOWN："
            "容器网络隔离未在运行态验证，不声称已验证。"
        ),
    },
    {
        "item_id": "ENV-MODEL",
        "marker": "UNKNOWN",
        "statement": (
            "模型环境 BLOCKED_MODEL_ENV_MISSING：模型版本与生成时延等指标未测得，"
            "相关字段一律保持 null。"
        ),
    },
    {
        "item_id": "DECISION-TECH-PREVIEW",
        "marker": "PARTIAL",
        "statement": (
            "发布结论为 TECH_PREVIEW_ONLY：存在未清环境阻塞或红线失败，"
            "不得表述为完整 Alpha。"
        ),
    },
)

#: W12 六类不承诺能力（PRD §2 / AC-15，设计 §6.2）：(item_id, 能力名, marker)。
NON_PROMISES: tuple[tuple[str, str, str], ...] = (
    ("NP-PROD-AUTO-WRITE", "生产自动写入", "NOT_SUPPORTED"),
    ("NP-GENERIC-AUTO-REPAIR", "通用故障自动修复", "NOT_SUPPORTED"),
    ("NP-ALWAYS-DIAGNOSE", "所有场景必然诊断成功", "NOT_SUPPORTED"),
    ("NP-FULL-DEADLOCK-INDEX", "完整死锁/索引评估", "PARTIAL"),
    ("NP-EXACTLY-ONCE", "跨系统 Exactly Once", "NOT_SUPPORTED"),
    (
        "NP-NETWORK-ISOLATION-VERIFIED",
        "网络级隔离已验证（KD-1 账号级）",
        "NOT_SUPPORTED",
    ),
)

#: 文档完整性滞后项（D-05/D-07/W07 冻结缺件）的 item_id。
DOC_PARTIAL_ITEM_ID: str = "DOC-PARTIAL"

#: 13 个自动基础项 id（6 W11 + 6 不承诺 + DOC-PARTIAL），**不可人工删除**。
AUTO_ITEM_IDS: frozenset[str] = frozenset(
    [spec["item_id"] for spec in W11_AUTO_BASE_SPEC]
    + [item_id for item_id, _capability, _marker in NON_PROMISES]
    + [DOC_PARTIAL_ITEM_ID]
)

#: `DOC-PARTIAL` 的固定陈述（逐字比对，不得改字）。
#:
#: 2026-11-19 收窄：原三项事实中，`docs/migrations.md` 版本表止于 007 已由发布件同步卡补齐
#: （001–011 正向 + 8 支回滚）、`.env.example` 三态已由收尾轮补齐；仅剩 W07 冻结四件套的
#: **形成过程未复核**一项，故仍标 `PARTIAL` 且**不声称齐全**（13 个自动基础项计数不变）。
DOC_PARTIAL_STATEMENT: str = (
    "文档完整性滞后（D-05/D-07）：迁移版本表与回滚清单已于 2026-11-19 补至 011"
    "（11 正向 + 8 回滚）、`.env.example` 已写明 fake/openai_compat/disabled 三态；"
    "仍存疑者为 W07 冻结四件套：05-freeze.md / 06-conformance-audit.md 虽于 2026-11-17 落盘，"
    "其形成过程未经本卡独立复核，故不声称四件套齐全。"
)

#: 已知偏离（`origin=MANUAL`，由主理人裁定并要求登记；**只登记，不改自动项**）。
KNOWN_DEVIATIONS: tuple[dict[str, str], ...] = (
    {
        "item_id": "KD-W11-01",
        "marker": "PARTIAL",
        "statement": (
            "W11 备份恢复口径偏离：audit_chain 桶命名与 s3 键不一致（docs/w11/backup-restore-report.md:124-133）。"
        ),
        "reason": (
            "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-1：只登记，不驱动路由、不降级 AC；"
            "W12 只读引用，不修 W11 逻辑。"
        ),
    },
    {
        "item_id": "KD-W11-02",
        "marker": "PARTIAL",
        "statement": (
            "W11 备份恢复覆盖漏项：未覆盖 approval_consumptions 与 step_prepared 两张只追加表。"
        ),
        "reason": (
            "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-2：只登记，不降级 AC；W12 不改 W11 实现。"
        ),
    },
    {
        "item_id": "KD-W11-03",
        "marker": "PARTIAL",
        "statement": (
            "W11 恢复报告哨兵归属错位（G1–G3），且 delta/after>before 缓解断言实属 T05 备份用例。"
        ),
        "reason": (
            "docs/w10-w12-lead-rulings.md §13.3 已知限制 L-3（含 KD-W11-03 文本小瑕说明）：只登记。"
        ),
    },
)

#: **已关闭**的已知偏离（只留档，**不进** `items`）。
#:
#: 关闭项从 :data:`KNOWN_DEVIATIONS` 移除后，其历史编号/陈述/关闭依据保留在本常量里，
#: 供 `known-limitations.md` 的「已关闭偏离」小节追溯，**不再**计入 `items` 与
#: `items_digest`（已关闭的偏离不应继续留在「当前限制」中）。
#:
#: - `KD-W12-01` 关闭依据：主理人裁定「后续批次修 `_collect_observed` 兼容
#:   `result["data"]` 并补真实形状回归」，关闭条件已满足 → 2026-11-17 关闭。
#: - `KD-W12-02` 关闭依据：`core/diagnosis/analyzers.py::analyze_slow_query` 入口给
#:   `unindexed` 初值并在无 `table_stats` 证据时显式降级（`UNKNOWN` + 证据不足），
#:   配套真实形状回归 → 2026-11-19 关闭（关闭条件见台账 §14.2）。
CLOSED_DEVIATIONS: tuple[dict[str, str], ...] = (
    {
        "item_id": "KD-W12-01",
        "marker": "PARTIAL",
        "closed_at": "2026-11-17",
        "statement": (
            "审批修复闭环的独立验证结论曾为 FAILED：StepObserved.result 落成全量快照，"
            "验证器在顶层取指标，affected_rows=UNKNOWN / snapshot_comparable=FAIL。"
        ),
        "closure": (
            'core/verification/plan.py 读取端形状归一：优先 result["data"]（执行器落盘的真实'
            "形状）、回落顶层（兼容历史扁平注入），不改写入端与断言常量；AC-03 由 FAILED 转 PASSED"
        ),
        "verification": (
            "真实形状回归 5 例（tests/unit/test_verification_plan.py）；"
            "W12 T01 AC-03：status=PASSED / verdict=PASS / reason_code=OK，三条断言全 PASS；"
            "W08 崩溃矩阵 7 passed；tests/unit 1801 passed"
        ),
        "reason": (
            "docs/w10-w12-lead-rulings.md §14.1 关闭条件（修 _collect_observed 兼容 "
            'result["data"] 并补真实形状回归）已满足；主理人 2026-11-17 裁定关闭并同步交付件。'
        ),
    },
    {
        "item_id": "KD-W12-02",
        "marker": "UNKNOWN",
        "closed_at": "2026-11-19",
        "statement": (
            "慢查询分析器在「全表扫描 + 无 table_stats 证据」输入下会抛 UnboundLocalError"
            "（core/diagnosis/analyzers.py:425）；W12 以证据包规避，AC-02 未覆盖该输入形状。"
        ),
        "closure": (
            "core/diagnosis/analyzers.py::analyze_slow_query 入口把 unindexed 初始化为 False，"
            "并在无 table_stats 证据时走显式降级分支（结论 UNKNOWN + 证据不足 + missing_evidence"
            " 标注 table_stats），不再读未赋值局部变量；有证据路径逐值不变"
        ),
        "verification": (
            "回归 2 例（tests/unit/test_diagnosis_analyzers.py：无 table_stats 不抛异常且降级 / "
            "补齐 table_stats 后正常路径逐值不变）；tests/unit 1803 passed"
        ),
        "reason": (
            "docs/w10-w12-lead-rulings.md §14.2 关闭条件（把 unindexed 初始化提前或显式分支处理"
            "并补回归）已满足；2026-11-19 发布件同步卡经生成器关闭并从当前限制清单移除。"
        ),
    },
)

#: 人工项的登记人（主理人裁定）。
MANUAL_ADDED_BY: str = "主理人裁定"

#: 基线口径常量（AC-14 / W12-Q5）：不许填任何数值。
BASELINE_MODEL_STATUS: str = "未微调"
BASELINE_METRICS_STATUS: str = "指标未测得"
BASELINE_BLOCKED_REASON: str = "BLOCKED_MODEL_ENV_MISSING"


# ---------------------------------------------------------------------------
# AC-13：OpenAPI 3.1 合同
# ---------------------------------------------------------------------------


class OpenAPIContract(BaseModel):
    """OpenAPI 合同的**实测摘要**（只读导出，不新增端点）。"""

    model_config = ConfigDict(extra="forbid")

    openapi_version: str
    info_title: str
    info_version: str
    path_count: int
    source: Literal["apps.api.main.create_app().openapi()"] = (
        "apps.api.main.create_app().openapi()"
    )
    exported_path: str = "docs/w12/openapi.json"


def export_openapi_31(root: Path | None = None) -> OpenAPIContract:
    """导出并校验 OpenAPI 合同（AC-13）。

    函数内 import `apps.api.main`：避免配置加载副作用在模块导入期提前发生；
    只读调用既有 `create_app().openapi()`，**不新增端点、不引新框架、不改 `main.py`**。

    Args:
        root: 可选，仓库根（决定 `docs/w12/openapi.json` 落位）；缺省按本文件上溯三级。

    Returns:
        :class:`OpenAPIContract`（`openapi_version` 实测应形如 ``3.1.x``）。

    Raises:
        AssertionError: 主版本不是 3.1（口径不符时不得静默通过）。
    """
    from apps.api.main import create_app  # 函数内 import（设计 §9.4）

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    schema = create_app().openapi()
    version = str(schema.get("openapi", ""))
    assert version.startswith("3.1"), f"OpenAPI 主版本必须是 3.1，实际 {version}"
    info = schema.get("info") or {}
    paths = schema.get("paths") or {}

    target = base / "docs" / "w12" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return OpenAPIContract(
        openapi_version=version,
        info_title=str(info.get("title", "")),
        info_version=str(info.get("version", "")),
        path_count=len(paths),
    )


# ---------------------------------------------------------------------------
# AC-13：四类文档清单（完整性如实标 PARTIAL）
# ---------------------------------------------------------------------------


class DocCategory(str, Enum):
    API_CONTRACT = "API_CONTRACT"
    SECURITY = "SECURITY"
    DATA_BENCHMARK = "DATA_BENCHMARK"
    LIMITATIONS = "LIMITATIONS"


class DocEntry(BaseModel):
    """一条文档清单项；`status=PARTIAL` 必须在 `note` 写明缺什么。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    category: DocCategory
    exists: bool
    status: Literal["EXISTS", "PARTIAL", "UNKNOWN"]
    note: str = ""

    @model_validator(mode="after")
    def _partial_must_be_explained(self) -> "DocEntry":
        """`PARTIAL` 必须登记原因；`EXISTS` 不能与 `exists=False` 并存。"""
        if self.status == "PARTIAL" and not self.note.strip():
            raise ValueError(f"{self.path}：status=PARTIAL 必须在 note 写明缺失项")
        if self.status == "EXISTS" and not self.exists:
            raise ValueError(f"{self.path}：不存在时不得标 EXISTS")
        return self


class DocInventory(BaseModel):
    """文档/基线清单（AC-13 / AC-14）；`extra="forbid"`。"""

    model_config = ConfigDict(extra="forbid")

    entries: tuple[DocEntry, ...]
    #: W12-Q5：baseline 未执行 → 恒 `None`（禁止填任何数值，含 0）。
    baseline_score: float | None = None
    baseline_model_status: Literal["未微调"] = "未微调"
    baseline_metrics_status: Literal["指标未测得"] = "指标未测得"
    baseline_blocked_reason: str = BASELINE_BLOCKED_REASON

    @model_validator(mode="after")
    def _baseline_not_fabricated(self) -> "DocInventory":
        """基线未实测：分数必须是 `None`，状态字面量必须是冻结值。"""
        if self.baseline_score is not None:
            raise ValueError(
                "baseline_score 必须为 None（results.json 实测 NOT_RUN / scores=null）"
            )
        if self.baseline_model_status != BASELINE_MODEL_STATUS:
            raise ValueError(f"baseline_model_status 必须是「{BASELINE_MODEL_STATUS}」")
        if self.baseline_metrics_status != BASELINE_METRICS_STATUS:
            raise ValueError(f"baseline_metrics_status 必须是「{BASELINE_METRICS_STATUS}」")
        if self.baseline_blocked_reason != BASELINE_BLOCKED_REASON:
            raise ValueError(f"baseline_blocked_reason 必须是 {BASELINE_BLOCKED_REASON}")
        categories = {entry.category for entry in self.entries}
        if categories != set(DocCategory):
            raise ValueError(f"四类文档必须齐全，实际 {sorted(item.value for item in categories)}")
        return self


#: 文档清单定义：(相对路径, 类别, 状态, 说明)。
#: 既有件只**引用**（`docs/adr/**`、`docs/security/**`、`docs/migrations.md` 等），W12 不改它们。
_DOC_SPEC: tuple[tuple[str, DocCategory, str, str], ...] = (
    # ---- 接口合同 ----
    ("docs/w12/openapi.json", DocCategory.API_CONTRACT, "EXISTS", "OpenAPI 3.1 只读导出"),
    ("docs/w12/api-contract.md", DocCategory.API_CONTRACT, "EXISTS", "接口合同说明（本卡新建）"),
    (
        "docs/migrations.md",
        DocCategory.API_CONTRACT,
        "EXISTS",
        "版本表与回滚清单已补至 011（11 正向 + 8 回滚）；环境清单不入库（KD-4）",
    ),
    ("core/tasks/state_machine.py", DocCategory.API_CONTRACT, "EXISTS", "任务状态机合同（源码即合同）"),
    ("core/tools/contracts.py", DocCategory.API_CONTRACT, "EXISTS", "Tool JSON Schema 契约"),
    ("core/errors.py", DocCategory.API_CONTRACT, "EXISTS", "错误码闭集"),
    ("docs/adr/ADR-0001-runtime-and-dual-engine.md", DocCategory.API_CONTRACT, "EXISTS", "ADR 只引用"),
    # ---- 安全材料 ----
    ("docs/w12/security-materials.md", DocCategory.SECURITY, "EXISTS", "安全材料索引（本卡新建）"),
    ("docs/security/identity-and-policy.md", DocCategory.SECURITY, "EXISTS", "RBAC / 身份与策略"),
    ("docs/security/w05-security-tests.md", DocCategory.SECURITY, "EXISTS", "安全测试材料"),
    (
        "docs/w07/07-gate-review.md",
        DocCategory.SECURITY,
        "PARTIAL",
        "W07 冻结四件套的 05-freeze.md / 06-conformance-audit.md 于 2026-11-17 落盘，"
        "形成过程未经本卡独立复核（只登记，不改既有文档）",
    ),
    ("benchmarks/faults/manifests", DocCategory.SECURITY, "EXISTS", "Fault Manifest 六场景冻结集"),
    ("core/redaction/engine.py", DocCategory.SECURITY, "EXISTS", "审计/证据脱敏引擎（闸门①–④）"),
    ("docs/w10/02-design.md", DocCategory.SECURITY, "EXISTS", "Oracle 隔离与数据切分设计"),
    # ---- 数据与 Benchmark ----
    ("docs/w12/benchmark-baseline.md", DocCategory.DATA_BENCHMARK, "EXISTS", "基线口径（不包装）"),
    (
        "benchmarks/baseline_v0/results.json",
        DocCategory.DATA_BENCHMARK,
        "EXISTS",
        "实测 status=NOT_RUN / scores=null / metrics=null（未执行）",
    ),
    (
        "benchmarks/baseline_v0/baseline_manifest.json",
        DocCategory.DATA_BENCHMARK,
        "EXISTS",
        "冻结版本字段",
    ),
    ("benchmarks/w10/metrics-v1.json", DocCategory.DATA_BENCHMARK, "EXISTS", "W10 指标定义冻结件"),
    ("benchmarks/w11/negative_cases.json", DocCategory.DATA_BENCHMARK, "EXISTS", "W11 负面集 29 条"),
    ("benchmarks/w11/rc_manifest.json", DocCategory.DATA_BENCHMARK, "EXISTS", "W11 RC 冻结件"),
    # ---- 限制清单 ----
    (LIMITATIONS_JSON, DocCategory.LIMITATIONS, "EXISTS", "唯一真值（13 自动项 + 人工项）"),
    (LIMITATIONS_MD, DocCategory.LIMITATIONS, "EXISTS", "内嵌同源 JSON 块 + digest 对账"),
    ("docs/w11/limitations.json", DocCategory.LIMITATIONS, "EXISTS", "W11 单一数据源（只读引用）"),
)


def collect_doc_inventory(root: Path) -> DocInventory:
    """采集四类文档清单（AC-13）与基线口径（AC-14）。

    Args:
        root: 仓库根目录。

    Returns:
        :class:`DocInventory`；`exists` 逐项实测，`PARTIAL` 项带 `note` 说明，
        `baseline_score` 恒 `None`。
    """
    base = Path(root)
    entries = tuple(
        DocEntry(
            path=relative,
            category=category,
            exists=(base / relative).exists(),
            status=status,
            note=note,
        )
        for relative, category, status, note in _DOC_SPEC
    )
    return DocInventory(entries=entries, baseline_score=None)


# ---------------------------------------------------------------------------
# AC-15 + W12-Q8：限制清单（13 自动项不可删 + 人工项只追加）
# ---------------------------------------------------------------------------


class LimitationOrigin(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class LimitationMarker(str, Enum):
    NOT_SUPPORTED = "NOT_SUPPORTED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class LimitationItem(BaseModel):
    """一条限制项；人工项必须带 `added_by` / `reason`。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    marker: LimitationMarker
    origin: LimitationOrigin
    statement: str
    added_by: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _manual_requires_attribution(self) -> "LimitationItem":
        """人工项必须可追溯（谁加的、为什么）；自动项不得带人工字段。"""
        if self.origin is LimitationOrigin.MANUAL:
            for field_name in ("added_by", "reason"):
                value = getattr(self, field_name)
                if not (value or "").strip():
                    raise ValueError(f"{self.item_id}：MANUAL 项必须提供 {field_name}")
        if not self.statement.strip():
            raise ValueError(f"{self.item_id}：statement 不得为空")
        return self


class KnownLimitations(BaseModel):
    """限制清单结果件（13 自动项 + 人工追加项）。

    校验器 `_auto_items_immutable` 会**重算** 13 个自动基础项：
    - 任一自动项缺失 → ``LIMITATION_REMOVED``（以 `ValidationError` 承载，见 W11 L-6）；
    - 自动项的 marker/statement 与常量不逐字一致 → `LIMITATION_MODIFIED`；
    - 自动项被改成 `origin=MANUAL` → `LIMITATION_MODIFIED`；
    - 人工项复用自动 `item_id` → `LIMITATION_ID_CONFLICT`。
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["w12-limitations-v1"] = LIMITATIONS_VERSION
    items: tuple[LimitationItem, ...]
    w11_source_status: AcceptanceStatus
    w11_source_digest: str | None = None
    items_digest: str
    source_path: str = LIMITATIONS_JSON

    @model_validator(mode="after")
    def _auto_items_immutable(self) -> "KnownLimitations":
        expected = _expected_auto_items()
        actual = {item.item_id: item for item in self.items}
        missing = sorted(set(expected) - set(actual))
        if missing:
            raise ValueError(
                f"LIMITATION_REMOVED：自动基础项不可删除，缺失 {missing}"
            )
        for item_id, template in expected.items():
            item = actual[item_id]
            if item.origin is not LimitationOrigin.AUTO:
                raise ValueError(
                    f"LIMITATION_MODIFIED：自动项 {item_id} 的 origin 必须是 AUTO"
                )
            if item.marker is not template.marker or item.statement != template.statement:
                raise ValueError(
                    f"LIMITATION_MODIFIED：自动项 {item_id} 的 marker/statement 被改写"
                )
        for item in self.items:
            if item.origin is LimitationOrigin.MANUAL and item.item_id in AUTO_ITEM_IDS:
                raise ValueError(
                    f"LIMITATION_ID_CONFLICT：人工项不得复用自动 item_id（{item.item_id}）"
                )
        if self.items_digest != items_digest(self.items):
            raise ValueError("LIMITATION_DIGEST_MISMATCH：items_digest 与 items 不一致")
        return self


def items_digest(items: Sequence[LimitationItem]) -> str:
    """`items` 的规范化摘要（`canonical_digest`，与 W06/W11 同源）。"""
    return canonical_digest([item.model_dump(mode="json") for item in items])


def _expected_auto_items() -> dict[str, LimitationItem]:
    """13 个自动基础项的**权威重算**（校验器与派生共用同一份常量）。"""
    expected: dict[str, LimitationItem] = {}
    for spec in W11_AUTO_BASE_SPEC:
        expected[spec["item_id"]] = LimitationItem(
            item_id=spec["item_id"],
            marker=LimitationMarker(spec["marker"]),
            origin=LimitationOrigin.AUTO,
            statement=spec["statement"],
        )
    for item_id, capability, marker in NON_PROMISES:
        expected[item_id] = LimitationItem(
            item_id=item_id,
            marker=LimitationMarker(marker),
            origin=LimitationOrigin.AUTO,
            statement=(
                f"W12 不承诺「{capability}」：该项能力不在本轮交付范围内，"
                "不得据此推断已具备。"
            ),
        )
    expected[DOC_PARTIAL_ITEM_ID] = LimitationItem(
        item_id=DOC_PARTIAL_ITEM_ID,
        marker=LimitationMarker("PARTIAL"),
        origin=LimitationOrigin.AUTO,
        statement=DOC_PARTIAL_STATEMENT,
    )
    return expected


def read_w11_limitations(
    root: Path,
) -> tuple[tuple[LimitationItem, ...] | None, AcceptanceStatus, str | None]:
    """只读 W11 限制清单（D-09 同源对账）。

    Returns:
        ``(六项, w11_source_status, w11_source_digest)``。文件存在且六项可解析 →
        ``PASSED`` + 六项摘要；文件缺失/不可读 → ``(None, NOT_AVAILABLE, None)``
        ——**不伪造** W11 输出、**不写** `docs/w11/**`。
    """
    path = Path(root) / "docs" / "w11" / "limitations.json"
    if not path.is_file():
        return None, AcceptanceStatus.NOT_AVAILABLE, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, AcceptanceStatus.NOT_AVAILABLE, None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return None, AcceptanceStatus.NOT_AVAILABLE, None
    try:
        items = tuple(
            LimitationItem(
                item_id=str(entry["item_id"]),
                marker=LimitationMarker(str(entry["marker"])),
                origin=LimitationOrigin(str(entry.get("origin", "AUTO"))),
                statement=str(entry["statement"]),
                added_by=entry.get("added_by"),
                reason=entry.get("reason"),
            )
            for entry in raw_items
            if isinstance(entry, dict)
        )
    except (KeyError, ValueError):
        return None, AcceptanceStatus.NOT_AVAILABLE, None
    auto_only = tuple(item for item in items if item.origin is LimitationOrigin.AUTO)
    if len(auto_only) != len(W11_AUTO_BASE_SPEC):
        return None, AcceptanceStatus.NOT_AVAILABLE, None
    return auto_only, AcceptanceStatus.PASSED, items_digest(auto_only)


def build_known_limitations(
    *,
    w11_items: Sequence[LimitationItem] | None,
    decision: Any | None = None,
) -> KnownLimitations:
    """派生限制清单（13 自动项）。

    **纯派生**：先取 W11 六项（`w11_items` 为 `None` 时用 :data:`W11_AUTO_BASE_SPEC`
    常量 fallback 并标 `NOT_AVAILABLE`），再追加 W12 六类不承诺 + `DOC-PARTIAL`。

    Args:
        w11_items: W11 六项自动基础项（来自 :func:`read_w11_limitations`）。
        decision: W11 `publish_decision`（duck-typed，仅用于签名兼容；`None` 视为
            「裁定缺失」，按 W12-Q4 默认 `TECH_PREVIEW_ONLY`，不写进本件字段）。

    Returns:
        :class:`KnownLimitations`（`items` 恰 13 项，全部 `origin=AUTO`）。
    """
    expected = _expected_auto_items()
    if w11_items is None:
        w11_ids = [spec["item_id"] for spec in W11_AUTO_BASE_SPEC]
        status = AcceptanceStatus.NOT_AVAILABLE
        digest: str | None = None
    else:
        w11_ids = [item.item_id for item in w11_items]
        status = AcceptanceStatus.PASSED
        digest = items_digest(w11_items)
    # 与常量 fallback 逐项比对：W11 的六项必须落在自动集合内（不新增、不改字）。
    for item in w11_items or ():
        template = expected.get(item.item_id)
        if template is None or item.marker is not template.marker:
            raise ValueError(
                f"W11_LIMITATION_DRIFT：{item.item_id} 不在 W12 自动基础项口径内"
            )
    ordered: list[LimitationItem] = [expected[item_id] for item_id in w11_ids]
    ordered.extend(
        expected[item_id]
        for item_id, _capability, _marker in NON_PROMISES
    )
    ordered.append(expected[DOC_PARTIAL_ITEM_ID])
    items = tuple(ordered)
    _ = decision  # duck-typed 入参：本件的 13 自动项不因 decision 变化（W12-Q8）
    return KnownLimitations(
        items=items,
        w11_source_status=status,
        w11_source_digest=digest,
        items_digest=items_digest(items),
    )


def append_manual_limitation(
    limitations: KnownLimitations, *, item: LimitationItem
) -> KnownLimitations:
    """追加一条人工限制项（**只能追加**，不得修改/覆盖自动项）。

    Args:
        limitations: 现有清单。
        item: 人工项（`origin=MANUAL`，`added_by`/`reason` 必填）。

    Returns:
        新的 :class:`KnownLimitations`（`items_digest` 重算）。

    Raises:
        ValueError: `origin` 不是 MANUAL、或 `item_id` 与现有项冲突。
    """
    if item.origin is not LimitationOrigin.MANUAL:
        raise ValueError("LIMITATION_NOT_MANUAL：追加项必须 origin=MANUAL")
    if any(existing.item_id == item.item_id for existing in limitations.items):
        raise ValueError(f"LIMITATION_DUPLICATED：{item.item_id} 已存在，只允许新增")
    items = tuple(limitations.items) + (item,)
    return KnownLimitations(
        items=items,
        w11_source_status=limitations.w11_source_status,
        w11_source_digest=limitations.w11_source_digest,
        items_digest=items_digest(items),
    )


def known_deviation_items() -> tuple[LimitationItem, ...]:
    """主理人裁定要求登记的 **3 条**在册已知偏离（`origin=MANUAL`）。

    `KD-W12-01`（2026-11-17）与 `KD-W12-02`（2026-11-19）均已关闭，移到
    :data:`CLOSED_DEVIATIONS` 只留档，**不再**计入 :attr:`KnownLimitations.items`。
    """
    return tuple(
        LimitationItem(
            item_id=spec["item_id"],
            marker=LimitationMarker(spec["marker"]),
            origin=LimitationOrigin.MANUAL,
            statement=spec["statement"],
            added_by=MANUAL_ADDED_BY,
            reason=spec["reason"],
        )
        for spec in KNOWN_DEVIATIONS
    )


def build_full_known_limitations(
    root: Path, *, decision: Any | None = None
) -> KnownLimitations:
    """构建**完整**限制清单：13 自动项 + 3 条在册已知偏离（`MANUAL`）。

    `KD-W12-01`（2026-11-17）与 `KD-W12-02`（2026-11-19）已关闭
    （:data:`CLOSED_DEVIATIONS` 只留档），故不再进 `items`。

    Args:
        root: 仓库根（用于只读 W11 同源件）。
        decision: W11 `publish_decision`（duck-typed，可 `None`）。

    Returns:
        :class:`KnownLimitations`，`items` 共 16 项（13 `AUTO` + 3 `MANUAL`）。
    """
    w11_items, _status, _digest = read_w11_limitations(root)
    limitations = build_known_limitations(w11_items=w11_items, decision=decision)
    for item in known_deviation_items():
        limitations = append_manual_limitation(limitations, item=item)
    return limitations


# ---------------------------------------------------------------------------
# 单一数据源：`.json` 唯一真值 + `.md` 内嵌同源块
# ---------------------------------------------------------------------------


def limitations_payload(limitations: KnownLimitations) -> dict[str, Any]:
    """限制清单的**唯一**序列化形式（`.json` 与 `.md` 内嵌块共用）。"""
    return limitations.model_dump(mode="json")


def write_known_limitations(root: Path, limitations: KnownLimitations) -> tuple[Path, Path]:
    """写出 `known-limitations.json`（真值）与 `known-limitations.md`（内嵌同源块）。

    Args:
        root: 仓库根。
        limitations: 已知限制清单。

    Returns:
        ``(json_path, md_path)``。
    """
    base = Path(root)
    payload = limitations_payload(limitations)
    json_path = base / LIMITATIONS_JSON
    md_path = base / LIMITATIONS_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _render_markdown(payload: dict[str, Any]) -> str:
    """渲染限制清单说明文档（内嵌与 `.json` **同一函数序列化**的 JSON 块）。"""
    auto_items = [item for item in payload["items"] if item["origin"] == "AUTO"]
    manual_items = [item for item in payload["items"] if item["origin"] == "MANUAL"]
    lines: list[str] = [
        "# W12 已知限制清单（T04 / AC-15 / AC-21）",
        "",
        "> 本文件由 `core/release/docs.py::write_known_limitations()` 生成：",
        "> **`docs/w12/known-limitations.json` 是唯一真值**，下列 JSON 块与本文件正文由**同一函数**序列化，",
        "> 因此 `items_digest` / `w11_source_digest` 必然可对账（W11 Q-W11-D 同款）。",
        "> 自动基础项（13 项）**不可人工删除或改写**；人工项只能追加并须写明 `added_by` / `reason`。",
        "",
        "## 1. 版本与对账",
        "",
        f"- `version`：`{payload['version']}`",
        f"- `w11_source_status`：`{payload['w11_source_status']}`（`docs/w11/limitations.json` 只读同源对账）",
        f"- `w11_source_digest`：`{payload['w11_source_digest']}`",
        f"- `items_digest`：`{payload['items_digest']}`",
        f"- 自动项 {len(auto_items)} / 人工项 {len(manual_items)} / 合计 {len(payload['items'])}",
        "",
        "## 2. 自动基础项（13 项，不可删）",
        "",
        "| item_id | marker | 陈述 |",
        "|---|---|---|",
    ]
    for item in auto_items:
        statement = str(item["statement"]).replace("|", "\\|")
        lines.append(f"| `{item['item_id']}` | `{item['marker']}` | {statement} |")
    lines.extend(
        [
            "",
            "## 3. 已知偏离（人工项，`origin=MANUAL`）",
            "",
            "> 逐条来自主理人裁定（`docs/w10-w12-lead-rulings.md` §13.3 / §14），只登记，",
            "> 不驱动路由、不降级自动项。",
            "> 已关闭的偏离（如 `KD-W12-01`）**已从 `items` 移除**，只在 §3.1 留档，",
            "> 不再计入 `items_digest`。",
            "",
            "| item_id | marker | 陈述 | 登记人 | 依据 |",
            "|---|---|---|---|---|",
        ]
    )
    for item in manual_items:
        statement = str(item["statement"]).replace("|", "\\|")
        reason = str(item["reason"]).replace("|", "\\|")
        lines.append(
            f"| `{item['item_id']}` | `{item['marker']}` | {statement} | "
            f"{item['added_by']} | {reason} |"
        )
    lines.extend(
        [
            "",
            f"### 3.1 已关闭偏离（已从 `items` 移除，仅留档 {len(CLOSED_DEVIATIONS)} 项）",
            "",
            "> 关闭项**不参与** `items_digest`，也不构成当前限制；此表保留关闭依据与验证数字，",
            "> 供追溯与对账（`CLOSED_DEVIATIONS`，`core/release/docs.py`）。",
            "",
            "| item_id | marker | 关闭日期 | 关闭依据（修复点） | 验证数字 | 原陈述 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for closed in CLOSED_DEVIATIONS:
        statement = str(closed["statement"]).replace("|", "\\|")
        closure = str(closed["closure"]).replace("|", "\\|")
        verification = str(closed["verification"]).replace("|", "\\|")
        lines.append(
            f"| `{closed['item_id']}` | `{closed['marker']}` | {closed['closed_at']} | "
            f"{closure} | {verification} | {statement} |"
        )
    # 关闭裁定依据逐条列在表格**之后**：放进循环里会把 markdown 表格切断（≥2 项时）。
    for closed in CLOSED_DEVIATIONS:
        lines.append("")
        lines.append(f"> `{closed['item_id']}` 关闭裁定依据：{closed['reason']}")
    lines.extend(
        [
            "",
            "## 4. 机器可读真值（与 `.json` 逐字一致）",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## 5. 口径说明",
            "",
            "- 六类不承诺能力逐条列在自动项内（`NP-*`）：既不表示已具备，也不构成任何后续承诺；",
            "- `DOC-PARTIAL` 为文档完整性滞后项（迁移说明与回滚清单已补至 011、`.env.example` 三态已补齐；"
            "W07 冻结四件套的 05-freeze.md / 06-conformance-audit.md 于 2026-11-17 落盘但形成过程未经独立复核，"
            "故仍标 `PARTIAL`、不声称齐全）；",
            "- 环境阻塞五项（`ENV-PG5432` / `ENV-1045` / `ENV-RESIDUE` / `ENV-NV1` / `ENV-MODEL`）未解，",
            "  相关用例按 `NOT_EXECUTED` / `NOT_VERIFIED` 处置，**不判 FAIL**，环境未清零；",
            "- 本清单不改变 W07「有条件放行；门禁通过：否」、W08「有条件放行」、W09/W10/W11「门禁未评」的既有口径。",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "AUTO_ITEM_IDS",
    "BASELINE_BLOCKED_REASON",
    "BASELINE_METRICS_STATUS",
    "BASELINE_MODEL_STATUS",
    "CLOSED_DEVIATIONS",
    "DOC_PARTIAL_ITEM_ID",
    "DOC_PARTIAL_STATEMENT",
    "DocCategory",
    "DocEntry",
    "DocInventory",
    "KNOWN_DEVIATIONS",
    "KnownLimitations",
    "LIMITATIONS_JSON",
    "LIMITATIONS_MD",
    "LIMITATIONS_VERSION",
    "LimitationItem",
    "LimitationMarker",
    "LimitationOrigin",
    "MANUAL_ADDED_BY",
    "NON_PROMISES",
    "OpenAPIContract",
    "W11_AUTO_BASE_SPEC",
    "append_manual_limitation",
    "build_full_known_limitations",
    "build_known_limitations",
    "collect_doc_inventory",
    "export_openapi_31",
    "items_digest",
    "known_deviation_items",
    "limitations_payload",
    "read_w11_limitations",
    "write_known_limitations",
]
