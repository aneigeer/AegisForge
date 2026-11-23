"""W12 T06：「演示 ≠ 验收」与「限制不可删除」的机器校验（AC-19…AC-22）。

口径来源：`docs/w12/01-prd.md` §5（AC-19…AC-22）、`docs/w12/02-design.md` §7/§8、
`docs/w12/03-task-cards.md` T06、`docs/w10-w12-lead-rulings.md` §4（W12-Q7/Q8）、
§14（KD-W12-01/02）、**§15（W12-CORR-01：值/句级否定语境豁免）**。

四条验收的落地方式：

| AC | 口径 | 落点 |
|---|---|---|
| AC-19 | 双范围**上下文感知**扫描（严格模式对外材料 / `spec_mode` 内部件 / 上游件排除），`violations` 必须为空 | :func:`scan_claims` |
| AC-20 | 验收结果模型**结构上无** `demo_passed`/`demo_status`/`demo_ok`；`demo_passed` 进 AC 输入即 `DEMO_AS_ACCEPTANCE`；`evidence_refs` 必含 W11/W10/W09 门禁件 | :func:`assert_no_demo_input` / :func:`assert_evidence_refs` |
| AC-21 | 13 个自动基础项不可删/不可改；人工项 `added_by`/`reason` 必填 | :func:`assert_limitation_integrity` |
| AC-22 | 写面不回退：PG 7 个全 `read`、observer 10 个全 `read`、写工具仍单一 LAB+mysql、R3/R4 拒绝仍生效 | :func:`check_write_surface` |

**为什么必须"上下文感知"（D-10）**：禁用集合含「门禁通过」，而权威 W07 口径本身就是
「有条件放行；**门禁通过：否**」；`docs/w12/` 内部件还必须**引用**禁令表。朴素全文本
子串扫描会让 W12 自己的合规文档判红。因此三范围划分 + 剥离代码语境 + 否定语境豁免。

**W12-CORR-01（主理人裁定，`docs/w10-w12-lead-rulings.md` §15）**：`known-limitations.{json,md}`
按**值/句级**否定语境豁免，**不整文件跳过、不改写 W11 原文**：

1. 粒度：`.json` 解析后**按每个字符串值**逐条扫描（不按物理行）；`.md` 按行扫描；
2. 豁免条件（满足其一）：① 短语**前 12 字符**内有否定词；② 该字符串/行以否定词开头；
   ③ 精确命中 :data:`ALLOWED_PHRASES`；
3. `allowed_phrases` **维持 4 条**（§15.2 第 3 条口径微调：不追加精确短语白名单，
   否定语境已覆盖，避免多一条宽松路径）；
4. **留痕**：`exempted_lines` 计数 + `exempted_items` 逐条明细（文件 / 值摘要 / 命中短语 /
   判定依据），逐条写进 `claim-scan-report.md`，**不得静默豁免**；
5. 证伪负测：把否定句改成肯定宣称（「已达成完整 Alpha」）或删掉否定词只留短语 → **必须违规**；
6. 外溢禁止：该豁免**只**适用于这两个文件，其余对外件严格零容忍（唯一例外 `门禁通过：否`）。

**只读边界**：本模块只读文件与既有模型；不改 `docs/w11/**`、不改任何结果件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.release.docs import (
    AUTO_ITEM_IDS,
    KnownLimitations,
    LimitationOrigin,
    _expected_auto_items,
)
from core.release.functional import DEMO_INPUT_FIELDS
from core.release.publish import (
    EVIDENCE_CANDIDATES,
    AlphaAcceptanceReport,
    REQUIRED_EVIDENCE_WEEKS,
)

#: 禁用表述（正则闭集）；`门禁通过` 用负向先行断言放行「：否」权威写法（D-10）。
FORBIDDEN_CLAIMS: tuple[tuple[str, str], ...] = (
    ("GATE_CLOSED", r"已收口"),
    ("GATE_PASSED", r"门禁通过(?!：否|:否)"),
    ("NETWORK_VERIFIED", r"容器网络隔离已验证"),
    ("ENV_CLEANED", r"已清理"),
    ("QA_INDEPENDENT", r"QA\s*独立实跑通过"),
)

#: W12 特有禁用表述（AC-19 补充闭集）。
W12_EXTRA_FORBIDDEN: tuple[str, ...] = (
    "上游已通过",
    "W12 开工",
    "回归全绿",
    "环境已清零",
    "Alpha 已就绪",
    "完整 Alpha",
)

#: 否定语境标记（`spec_mode` 整行豁免；卡面 `:396` 闭集）。
NEGATION_MARKERS: tuple[str, ...] = (
    "禁用",
    "不得",
    "禁止",
    "应改为",
    "表述禁令",
    "自查",
    "沿用",
    "不写",
    "不含",
)

#: W12-CORR-01 否定词闭集（值/句级豁免用，台账 §15 逐字）。
CORR01_NEGATIONS: tuple[str, ...] = (
    "不得",
    "禁止",
    "不能",
    "不应",
    "避免",
    "不写",
    "不含",
    "禁用",
    "不声称",
)

#: W12-CORR-01 豁免窗口：短语**前 12 字符**内出现否定词即视为否定式引用。
EXEMPTION_CONTEXT_CHARS: int = 12

#: 规范引用标记（仅 `spec_mode` 生效）。
SPEC_MARKERS: tuple[str, ...] = (
    "未写",
    "不预写",
    "不宣布",
    "互斥",
    "豁免",
    "排除",
    "负向先行",
    "字面",
    "规则",
    "常量",
    "断言",
)

#: 严格模式扫描的 W12 对外材料（**全部**，不得静默跳过）。
STRICT_SCAN_FILES: tuple[str, ...] = (
    "docs/w12/deployment-guide.md",
    "docs/w12/demo-script.md",
    "docs/w12/known-limitations.json",
    "docs/w12/known-limitations.md",
    "docs/w12/api-contract.md",
    "docs/w12/security-materials.md",
    "docs/w12/benchmark-baseline.md",
    "docs/w12/alpha-acceptance-report.md",
    "docs/w12/release-notes.md",
    "docs/w12/claim-scan-report.md",
    "docs/w12/openapi.json",
)

#: 内部设计件（`spec_mode`：剥离代码语境 + 否定语境豁免）。
SPEC_MODE_FILES: tuple[str, ...] = (
    "docs/w12/02-design.md",
    "docs/w12/03-task-cards.md",
)

#: 上游输入（显式排除，理由写进 `claim-scan-report.md`）。
EXCLUDED_FILES: tuple[str, ...] = ("docs/w12/01-prd.md",)

#: 排除理由（逐字写进报告）。
EXCLUDED_REASONS: tuple[tuple[str, str], ...] = (
    (
        "docs/w12/01-prd.md",
        "上游输入件：其 `:113` / `:200` 正含禁用词表与被引用的原文（含 `门禁通过` 字样），"
        "按 D-10 显式排除；AC-19 的「全文」按「W12 本周对外产出全文 + 内部件 spec_mode」解释。",
    ),
)

#: W12-CORR-01 的豁免范围（**仅**这两个文件；其余对外件严格零容忍）。
NEGATION_TOLERANT_FILES: tuple[str, ...] = (
    "docs/w12/known-limitations.json",
    "docs/w12/known-limitations.md",
)

#: 固定允许短语（报告内必须原样出现）。
#: **W12-CORR-01 口径微调（主理人 §15.2 第 3 条）**：维持 4 条，**不**追加
#: 「不得表述为…」这类精确短语——否定语境已覆盖，加白名单只会多一条宽松路径。
ALLOWED_PHRASES: tuple[str, ...] = (
    "有条件放行；门禁通过：否",
    "有条件放行",
    "配置层已实测 / 运行态 NOT_VERIFIED",
    "只读数据复核通过（依赖工程师实跑产物）",
)

#: 违规码（闭集）。
CODE_DEMO_AS_ACCEPTANCE: str = "DEMO_AS_ACCEPTANCE"
CODE_LIMITATION_REMOVED: str = "LIMITATION_REMOVED"
CODE_LIMITATION_MODIFIED: str = "LIMITATION_MODIFIED"
CODE_EVIDENCE_REF_INCOMPLETE: str = "EVIDENCE_REF_INCOMPLETE"
CODE_EVIDENCE_REF_MISSING_FILE: str = "EVIDENCE_REF_MISSING_FILE"

#: 每周门禁件的路径前缀（判定"每周至少 1 条"用）。
WEEK_PREFIXES: dict[str, str] = {
    "W11": "docs/w11/",
    "W10": "docs/w10/",
    "W09": "docs/w09/",
}

#: 豁免判定依据标签（写进留痕明细）。
BASIS_ALLOWED_PHRASE: str = "ALLOWED_PHRASE"
BASIS_NEGATION_WINDOW: str = "NEGATION_WINDOW"
BASIS_NEGATION_PREFIX: str = "NEGATION_PREFIX"
BASIS_SPEC_MODE_MARKER: str = "SPEC_MODE_MARKER"


class AcceptanceIntegrityError(ValueError):
    """AC-19…AC-22 的校验失败（带稳定 `code`，便于断言）。"""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ClaimScanResult:
    """表述扫描结果（三范围 + 三组附加校验）。

    `exempted_items` 是 W12-CORR-01 的**留痕**字段：逐条记录被豁免的值/行、命中短语与
    判定依据，渲染进 `claim-scan-report.md`（不得静默豁免）。
    """

    scanned_files: tuple[str, ...]
    spec_mode_files: tuple[str, ...]
    excluded_files: tuple[str, ...]
    exempted_lines: int
    violations: tuple[str, ...]
    allowed_phrases: tuple[str, ...]
    demo_input_violations: tuple[str, ...]
    limitation_integrity_ok: bool
    evidence_refs_ok: bool
    exempted_items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """三组校验全绿且无违规。"""
        return (
            not self.violations
            and not self.demo_input_violations
            and self.limitation_integrity_ok
            and self.evidence_refs_ok
        )


@dataclass(frozen=True)
class AcceptanceEvidence:
    """单条 AC 的证据引用（★ 结构上无 demo 字段，AC-20）。"""

    ac_id: str
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 扫描内核（短语命中 + 否定语境判定）
# ---------------------------------------------------------------------------

_INLINE_CODE = re.compile(r"`[^`\n]*`")


@dataclass(frozen=True)
class _Hit:
    """一次禁用表述命中。"""

    code: str
    phrase: str
    start: int


def _hits_in_text(text: str) -> tuple[_Hit, ...]:
    """返回文本内全部禁用表述命中（带位置，用于否定窗口判定）。"""
    hits: list[_Hit] = []
    for name, pattern in FORBIDDEN_CLAIMS:
        for match in re.finditer(pattern, text):
            hits.append(_Hit(code=name, phrase=match.group(0), start=match.start()))
    for phrase in W12_EXTRA_FORBIDDEN:
        start = text.find(phrase)
        while start >= 0:
            hits.append(_Hit(code="W12_EXTRA", phrase=phrase, start=start))
            start = text.find(phrase, start + 1)
    return tuple(hits)


def _exemption_basis(text: str, hit: _Hit) -> str | None:
    """判定一次命中是否属于否定式引用（W12-CORR-01），返回判定依据或 ``None``。

    三个条件按台账 §15 顺序：① 精确命中 `allowed_phrases`；② 短语前 12 字符内有否定词；
    ③ 该值/行以否定词开头。
    """
    if hit.phrase in ALLOWED_PHRASES:
        return BASIS_ALLOWED_PHRASE
    window = text[max(0, hit.start - EXEMPTION_CONTEXT_CHARS) : hit.start]
    for token in CORR01_NEGATIONS:
        if token in window:
            return f"{BASIS_NEGATION_WINDOW}:{token}"
    stripped = text.lstrip()
    for token in CORR01_NEGATIONS:
        if stripped.startswith(token):
            return f"{BASIS_NEGATION_PREFIX}:{token}"
    return None


def _scan_unit(unit: str, *, exemptable: bool, label: str) -> tuple[list[str], list[str]]:
    """扫描一个"值 / 行"单元，返回 (违规, 豁免留痕)。

    Args:
        unit: 待扫描文本（已剥离行内 code span）。
        exemptable: 是否启用 W12-CORR-01 否定语境豁免（**仅**两个限制清单文件为 True）。
        label: 留痕用位置标签（如 ``docs/w12/known-limitations.json:$.items[7].statement``）。
    """
    violations: list[str] = []
    exemptions: list[str] = []
    for hit in _hits_in_text(unit):
        basis = _exemption_basis(unit, hit) if exemptable else None
        if basis is not None:
            excerpt = unit.strip().replace("\n", " ")[:80]
            exemptions.append(
                f"{label}｜值摘要：{excerpt}｜命中：{hit.code}:{hit.phrase}｜依据：{basis}"
            )
            continue
        violations.append(f"{label}｜{hit.code}:{hit.phrase} -> {unit.strip()[:120]}")
    return violations, exemptions


def _walk_json_strings(node: Any, path: str = "$") -> tuple[tuple[str, str], ...]:
    """把 JSON 结构拍平成 (路径, 字符串值) 序列（CORR-01：按值扫描，不按物理行）。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((path, node))
    elif isinstance(node, Mapping):
        for key, value in node.items():
            found.extend(_walk_json_strings(value, f"{path}.{key}"))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, value in enumerate(node):
            found.extend(_walk_json_strings(value, f"{path}[{index}]"))
    return tuple(found)


def _scan_json_file(relative: str, text: str, *, exemptable: bool) -> tuple[list[str], list[str]]:
    """按**解析后的字符串值**逐条扫描 `.json`（W12-CORR-01 粒度要求）。"""
    try:
        payload = json.loads(text)
    except ValueError:
        return ([f"{relative}｜SCAN_TARGET_UNPARSABLE（对外材料不得不可解析）"], [])
    violations: list[str] = []
    exemptions: list[str] = []
    for path, value in _walk_json_strings(payload):
        bad, exempt = _scan_unit(
            _INLINE_CODE.sub("", value), exemptable=exemptable, label=f"{relative}:{path}"
        )
        violations.extend(bad)
        exemptions.extend(exempt)
    return violations, exemptions


def _scan_markdown_file(
    relative: str, text: str, *, exemptable: bool
) -> tuple[list[str], list[str]]:
    """按**行**扫描 markdown；跳过 fenced code block（规则表/正则本身在代码语境）。"""
    violations: list[str] = []
    exemptions: list[str] = []
    in_fence = False
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        bad, exempt = _scan_unit(
            _INLINE_CODE.sub("", raw_line), exemptable=exemptable, label=f"{relative}:L{line_no}"
        )
        violations.extend(bad)
        exemptions.extend(exempt)
    return violations, exemptions


def _scan_spec_mode(relative: str, text: str) -> tuple[list[str], list[str]]:
    """`spec_mode`：逐行 fence 状态机 + 否定/规范引用标记整行豁免。

    不使用整文件正则剥离：`docs/w12/02-design.md` 正文里有「内嵌 ```json 同源块」这类
    **行内**三反引号，整文件配对会把后续代码块内容误当正文暴露出来。
    """
    spec_negations = tuple(dict.fromkeys([*NEGATION_MARKERS, *SPEC_MARKERS]))
    violations: list[str] = []
    exemptions: list[str] = []
    in_fence = False
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_CODE.sub("", raw_line)
        hits = _hits_in_text(line)
        if not hits:
            continue
        joined = ",".join(f"{hit.code}:{hit.phrase}" for hit in hits)
        if any(token in line for token in spec_negations):
            exemptions.append(
                f"{relative}:L{line_no}｜值摘要：{line.strip()[:80]}｜命中：{joined}"
                f"｜依据：{BASIS_SPEC_MODE_MARKER}"
            )
            continue
        violations.append(f"{relative}:L{line_no}｜{joined} -> {line.strip()[:120]}")
    return violations, exemptions


# ---------------------------------------------------------------------------
# AC-19：三范围扫描
# ---------------------------------------------------------------------------


def scan_claims(root: Path) -> ClaimScanResult:
    """扫描 W12 产出全文的禁用表述（AC-19 + W12-CORR-01）。

    Args:
        root: 仓库根目录。

    Returns:
        :class:`ClaimScanResult`；`violations` 与 `demo_input_violations` 必须为空，
        `limitation_integrity_ok` / `evidence_refs_ok` 必须为 ``True``。
        **不静默跳过任何对外材料**：缺失文件记为违规；每条豁免在 `exempted_items` 留痕。
    """
    base = Path(root)
    violations: list[str] = []
    exemptions: list[str] = []

    for relative in STRICT_SCAN_FILES:
        try:
            text = (base / relative).read_text(encoding="utf-8")
        except OSError:
            violations.append(f"{relative}｜SCAN_TARGET_MISSING（对外材料不得缺失/不可读）")
            continue
        exemptable = relative in NEGATION_TOLERANT_FILES
        if relative.endswith(".json"):
            bad, exempt = _scan_json_file(relative, text, exemptable=exemptable)
        else:
            bad, exempt = _scan_markdown_file(relative, text, exemptable=exemptable)
        violations.extend(bad)
        exemptions.extend(exempt)

    for relative in SPEC_MODE_FILES:
        try:
            text = (base / relative).read_text(encoding="utf-8")
        except OSError:
            violations.append(f"{relative}｜SCAN_TARGET_MISSING（内部件缺失）")
            continue
        bad, exempt = _scan_spec_mode(relative, text)
        violations.extend(bad)
        exemptions.extend(exempt)

    return ClaimScanResult(
        scanned_files=STRICT_SCAN_FILES,
        spec_mode_files=SPEC_MODE_FILES,
        excluded_files=EXCLUDED_FILES,
        exempted_lines=len(exemptions),
        violations=tuple(violations),
        allowed_phrases=ALLOWED_PHRASES,
        demo_input_violations=_scan_demo_inputs(),
        limitation_integrity_ok=_limitation_integrity_state(base),
        evidence_refs_ok=_evidence_refs_state(base),
        exempted_items=tuple(exemptions),
    )


# ---------------------------------------------------------------------------
# AC-20：demo 输入排除 + 证据引用完整
# ---------------------------------------------------------------------------


def _scan_demo_inputs() -> tuple[str, ...]:
    """验收相关模型与 `AcceptanceEvidence` 的字段集不得含 demo 键。"""
    from core.release.functional import FunctionalAcceptanceResult
    from core.release.security import SecurityAcceptanceResult

    offenders: list[str] = []
    for model in (
        FunctionalAcceptanceResult,
        SecurityAcceptanceResult,
        AlphaAcceptanceReport,
        AcceptanceEvidence,
    ):
        fields = set(getattr(model, "model_fields", {})) or set(
            getattr(model, "__dataclass_fields__", {})
        )
        hit = sorted(set(DEMO_INPUT_FIELDS) & fields)
        if hit:
            offenders.append(f"{model.__name__}: {','.join(hit)}")
    return tuple(offenders)


def assert_no_demo_input(payload: Mapping[str, Any]) -> None:
    """AC 判定输入里出现 demo 键即拒绝（`DEMO_AS_ACCEPTANCE`）。

    Args:
        payload: 任一 AC 判定输入（例如 ``{"ac_id":..., "evidence_refs":..., "status":...}``）。

    Raises:
        AcceptanceIntegrityError: 命中 `demo_passed` / `demo_status` / `demo_ok`。
    """
    hit = sorted(set(DEMO_INPUT_FIELDS) & set(payload))
    if hit:
        raise AcceptanceIntegrityError(
            CODE_DEMO_AS_ACCEPTANCE,
            "演示结果不得进入验收判定输入",
            details={"fields": hit},
        )


def assert_evidence_refs(refs: Sequence[str]) -> None:
    """`evidence_refs` 必须覆盖 W11/W10/W09 且路径真实存在（AC-20）。

    Raises:
        AcceptanceIntegrityError: 某周缺引用（`EVIDENCE_REF_INCOMPLETE`）或路径不存在
            （`EVIDENCE_REF_MISSING_FILE`）。
    """
    missing_weeks = [
        week
        for week in REQUIRED_EVIDENCE_WEEKS
        if not any(str(ref).startswith(WEEK_PREFIXES[week]) for ref in refs)
    ]
    if missing_weeks:
        raise AcceptanceIntegrityError(
            CODE_EVIDENCE_REF_INCOMPLETE,
            "evidence_refs 必须覆盖 W11/W10/W09 门禁件",
            details={"missing_weeks": missing_weeks},
        )
    root = Path(__file__).resolve().parents[2]
    absent = [str(ref) for ref in refs if not (root / str(ref)).is_file()]
    if absent:
        raise AcceptanceIntegrityError(
            CODE_EVIDENCE_REF_MISSING_FILE,
            "evidence_refs 含不存在的路径",
            details={"absent": absent},
        )


def evidence_availability(root: Path | None = None) -> tuple[tuple[str, str, bool], ...]:
    """每周门禁件的**实测可用性**（`(周, 路径, 是否存在)`；只读）。

    `benchmarks/w10/metrics-v1.json` 与 `docs/w11/rc-freeze.md` 现已存在，
    因此按实测标为可用（不再 `NOT_AVAILABLE`）。
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    rows: list[tuple[str, str, bool]] = []
    for week in REQUIRED_EVIDENCE_WEEKS:
        for relative in EVIDENCE_CANDIDATES[week]:
            rows.append((week, relative, (base / relative).is_file()))
    return tuple(rows)


def _evidence_refs_state(base: Path) -> bool:
    """只读判定 `alpha-acceptance-report.md` 是否覆盖三周且引用路径存在。"""
    try:
        text = (base / "docs" / "w12" / "alpha-acceptance-report.md").read_text(encoding="utf-8")
    except OSError:
        return False
    refs = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*$", text, re.M)
    if not refs:
        return False
    for week in REQUIRED_EVIDENCE_WEEKS:
        if not any(ref.startswith(WEEK_PREFIXES[week]) for ref in refs):
            return False
    return all((base / ref).is_file() for ref in refs)


# ---------------------------------------------------------------------------
# AC-21：限制清单不可删
# ---------------------------------------------------------------------------


def assert_limitation_integrity(items: KnownLimitations) -> None:
    """重算 13 个自动基础项，要求 `AUTO ⊆ items` 且逐字未改（AC-21）。

    Args:
        items: 待校验的限制清单。

    Raises:
        AcceptanceIntegrityError: 缺自动项（`LIMITATION_REMOVED`）或自动项被改写
            （`LIMITATION_MODIFIED`）。
    """
    expected = _expected_auto_items()
    actual = {item.item_id: item for item in items.items}
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise AcceptanceIntegrityError(
            CODE_LIMITATION_REMOVED,
            "自动基础项不可删除",
            details={"missing": missing},
        )
    for item_id, template in expected.items():
        current = actual[item_id]
        if current.origin is not LimitationOrigin.AUTO:
            raise AcceptanceIntegrityError(
                CODE_LIMITATION_MODIFIED,
                "自动项的 origin 必须是 AUTO",
                details={"item_id": item_id, "origin": current.origin.value},
            )
        if current.marker is not template.marker or current.statement != template.statement:
            raise AcceptanceIntegrityError(
                CODE_LIMITATION_MODIFIED,
                "自动项的 marker/statement 不得改写",
                details={"item_id": item_id},
            )
    if set(expected) != set(AUTO_ITEM_IDS):
        raise AcceptanceIntegrityError(
            CODE_LIMITATION_MODIFIED,
            "自动基础项集合与 AUTO_ITEM_IDS 不一致",
            details={"expected": sorted(expected), "auto_ids": sorted(AUTO_ITEM_IDS)},
        )


def _limitation_integrity_state(base: Path) -> bool:
    """只读判定落盘的限制清单是否通过 AC-21 校验。"""
    try:
        payload = json.loads(
            (base / "docs" / "w12" / "known-limitations.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    try:
        limitations = KnownLimitations.model_validate(payload)
        assert_limitation_integrity(limitations)
    except Exception:  # noqa: BLE001 - 任何构造/校验失败都表示"不通过"
        return False
    return True


# ---------------------------------------------------------------------------
# AC-22：写面不回退（只读结构核对）
# ---------------------------------------------------------------------------


def check_write_surface(*, lab_registry: Any | None = None) -> dict[str, bool]:
    """写面不回退的只读核对（AC-22）。

    Args:
        lab_registry: 可选，LAB 注册表（需 DB 环境事实才能装配）；缺省时该项记
            ``False``（"未核对"，不冒充通过）。

    Returns:
        ``{"observer_all_read", "pg_all_read", "single_write_tool", "r3_r4_denied"}``。
    """
    from adapters.postgres_mcp.tools import TOOL_DEFINITIONS as PG_TOOLS
    from database.observers import build_observer_registry

    from core.release.security import check_risk_denials

    observers = build_observer_registry().definitions()
    denials = check_risk_denials()
    verdicts = {
        "observer_all_read": bool(observers) and all(item.side_effect == "read" for item in observers),
        "pg_all_read": bool(PG_TOOLS)
        and all(item.side_effect == "read" for item in PG_TOOLS.values()),
        "single_write_tool": False,
        "r3_r4_denied": bool(denials.get("r3_denied")) and bool(denials.get("r4_denied")),
    }
    if lab_registry is not None:
        writes = [item for item in lab_registry.definitions() if item.side_effect == "write"]
        verdicts["single_write_tool"] = (
            [item.name for item in writes] == ["mysql_lab_update_order_note"]
            and writes[0].allowed_environments == ["LAB"]
            and writes[0].risk_level == "R2"
        )
    return verdicts


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def _table_cell(text: str) -> str:
    """表格单元转义（避免 `|` 破坏 markdown 表）。"""
    return text.replace("|", "\\|")


def render_claim_scan_report(result: ClaimScanResult, *, root: Path | None = None) -> str:
    """渲染 `claim-scan-report.md`（三范围 + 豁免逐条留痕 + 门禁件可用性）。"""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    availability = evidence_availability(base)
    lines = [
        "# W12 表述禁令扫描报告（T06 / AC-19 / AC-20 / AC-21）",
        "",
        "> 生成方式：`core/release/claims.py::scan_claims()` → `render_claim_scan_report()`。",
        "> 口径：`docs/w12/02-design.md` §8（AC-19 必须实现为**上下文感知**扫描，D-10）+",
        "> `docs/w10-w12-lead-rulings.md` §15（**W12-CORR-01**：值/句级否定语境豁免）。",
        "> 本文件是 W12 对外材料，因此**它自身也在被扫描范围内**。",
        "",
        "## 1. 结论",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| `violations` | {len(result.violations)} 条（必须为空） |",
        f"| `demo_input_violations` | {len(result.demo_input_violations)} 条（必须为空） |",
        f"| `limitation_integrity_ok` | `{result.limitation_integrity_ok}` |",
        f"| `evidence_refs_ok` | `{result.evidence_refs_ok}` |",
        f"| `exempted_lines` | {result.exempted_lines}（否定式引用 / 规范引用，逐条见第 3 节） |",
        "",
        f"最终判定：`ok = {result.ok}`（`violations` 为空且三组附加校验通过）。",
        "",
        "## 2. 三范围划分（不静默跳过任何对外材料）",
        "",
        "### 2.1 严格模式：W12 对外材料（禁用表述零容忍）",
        "",
        "| 文件 | 绑定口径 |",
        "|---|---|",
    ]
    for relative in result.scanned_files:
        binding = (
            "W12-CORR-01 值/句级否定语境豁免"
            if relative in NEGATION_TOLERANT_FILES
            else "严格零容忍（唯一例外 `门禁通过：否`）"
        )
        lines.append(f"| `{relative}` | {binding} |")
    lines.extend(["", "### 2.2 `spec_mode`：内部设计件（须引用禁令表本身）", "", "| 文件 |", "|---|"])
    for relative in result.spec_mode_files:
        lines.append(f"| `{relative}` |")
    lines.extend(["", "### 2.3 排除件（上游输入，理由逐条登记）", "", "| 文件 | 排除理由 |", "|---|---|"])
    for relative in result.excluded_files:
        reason = next((text for path, text in EXCLUDED_REASONS if path == relative), "—")
        lines.append(f"| `{relative}` | {reason} |")
    lines.extend(
        [
            "",
            "## 3. 豁免逐条留痕（W12-CORR-01，不得静默豁免）",
            "",
            f"合计 **{result.exempted_lines}** 条；每条给出「文件:位置｜值摘要｜命中短语｜判定依据」。",
            "",
            "判定依据取值：`ALLOWED_PHRASE`（精确命中固定短语）、"
            "`NEGATION_WINDOW:<否定词>`（短语前 12 字符内有否定词）、"
            "`NEGATION_PREFIX:<否定词>`（该值/行以否定词开头）、"
            "`SPEC_MODE_MARKER`（内部件规范引用整行豁免）。",
            "",
        ]
    )
    if result.exempted_items:
        # 明细必须逐条列出，但**自身引用了被豁免的短语**，故放进代码围栏（扫描时整块跳过）。
        lines.extend(["```text"])
        for index, item in enumerate(result.exempted_items, start=1):
            lines.append(f"[{index}] {item}")
        lines.extend(["```"])
    else:
        lines.append("本轮无豁免项（`exempted_lines = 0`）。")
    lines.extend(
        [
            "",
            "## 4. 规则（规则表放在代码围栏内：扫描时整块跳过，避免规则自述被判违规）",
            "",
            "### 4.1 禁用正则",
            "",
            "```text",
        ]
    )
    for name, pattern in FORBIDDEN_CLAIMS:
        lines.append(f"{name}\t{pattern}")
    lines.extend(["```", "", "### 4.2 W12 追加禁用表述", "", "```text", *W12_EXTRA_FORBIDDEN, "```"])
    lines.extend(["", "### 4.3 否定词（W12-CORR-01 值/句级豁免）", "", "```text", *CORR01_NEGATIONS, "```"])
    lines.extend(
        [
            "",
            f"豁免窗口：短语**前 {EXEMPTION_CONTEXT_CHARS} 字符**；另接受「该值/行以否定词开头」与「精确命中固定短语」。",
            "",
            "### 4.4 否定语境标记（`spec_mode` 整行豁免）",
            "",
            "```text",
            *NEGATION_MARKERS,
            "```",
            "",
            "### 4.5 规范引用标记（仅 `spec_mode`）",
            "",
            "```text",
            *SPEC_MARKERS,
            "```",
            "",
            "### 4.6 固定允许短语（清单放在代码围栏内：短语本身即被引用的原文）",
            "",
            "```text",
        ]
    )
    for phrase in result.allowed_phrases:
        lines.append(phrase)
    lines.extend(["```"])
    lines.extend(
        [
            "",
            "## 5. 处理流程（可复核）",
            "",
            "1. **粒度**：`.json` 先解析，再**按每个字符串值**逐条扫描；`.md` 按行扫描；",
            "2. **剥离代码语境**：逐行 fence 状态机跳过 fenced code block，并剥离行内 code span；",
            "3. **`spec_mode` 放宽**：剥离后含否定/规范引用标记的行整行豁免（仅内部件）；",
            "4. **负向先行断言**：`门禁通过(?!：否|:否)` 使权威写法「有条件放行；门禁通过：否」不被判违规；",
            "5. **W12-CORR-01 值/句级豁免**：仅 `known-limitations.json` / `known-limitations.md` 启用，"
            "三条件（精确短语 / 前 12 字符否定词 / 以否定词开头）满足其一即计为否定式引用，并逐条留痕；",
            "6. **剩余命中**计入 `violations`；缺失对外材料记 `SCAN_TARGET_MISSING`，**不静默跳过**；",
            "7. **证伪负测**：把否定句改成肯定宣称（例如 `已达成完整 Alpha`）或删掉否定词只留短语 → 必须违规。",
            "",
            "## 6. AC-20 结构校验",
            "",
            "- `FunctionalAcceptanceResult` / `SecurityAcceptanceResult` / `AlphaAcceptanceReport` / "
            "`AcceptanceEvidence` 的字段集与 `demo_passed`/`demo_status`/`demo_ok` 交集为空；",
            "- `assert_no_demo_input(payload)` 命中任一 demo 键 → `DEMO_AS_ACCEPTANCE`；",
            "- `evidence_refs` 必须覆盖 W11/W10/W09 各至少 1 条，且路径真实存在。",
            "",
            "### 6.1 门禁件可用性（实测）",
            "",
            "| 周 | 路径 | 存在 |",
            "|---|---|---|",
        ]
    )
    for week, relative, exists in availability:
        lines.append(f"| {week} | `{relative}` | `{exists}` |")
    lines.extend(
        [
            "",
            "> `benchmarks/w10/metrics-v1.json` 与 `docs/w11/rc-freeze.md` **现已存在**，",
            "> 按实测标为可用（不再 `NOT_AVAILABLE`）；若将来缺失只会降级为登记，不判 FAIL（D-09）。",
            "",
            "## 7. AC-21 限制清单校验",
            "",
            f"- 自动基础项 `AUTO_ITEM_IDS` 固定 {len(AUTO_ITEM_IDS)} 项（6 W11 + 6 不承诺 + `DOC-PARTIAL`）；",
            "- `assert_limitation_integrity()` 重算并逐字比对；删除 → `LIMITATION_REMOVED`，改写 → `LIMITATION_MODIFIED`；",
            "- 人工项 `origin=MANUAL`，`added_by`/`reason` 必填，只能追加。",
            "",
            "## 8. 上游口径引述（主理人版本，逐字）",
            "",
            "- W07：「有条件放行；门禁通过：否」",
            "- W08：「有条件放行」",
            "- W09：「T01–T06 + L1/L2 已实现并通过定向验收，W09 门禁未评」",
            "- 容器：「配置层已实测 / 运行态 NOT_VERIFIED」",
            "",
        ]
    )
    return "\n".join(lines)


def write_claim_scan_report(root: Path, result: ClaimScanResult) -> Path:
    """把扫描报告写进 `docs/w12/claim-scan-report.md`（对外材料）。"""
    path = Path(root) / "docs" / "w12" / "claim-scan-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_claim_scan_report(result, root=root), encoding="utf-8")
    return path


def regenerate_claim_scan_report(root: Path) -> ClaimScanResult:
    """两遍生成 `claim-scan-report.md` 并返回最终扫描结果。

    为什么两遍：`claim-scan-report.md` 自身也是被扫描的对外材料，第一遍时它可能还不存在。
    第 1 遍得到"缺件"结论 → 写出报告 → 第 2 遍在报告已在场的前提下重新扫描并覆盖写出，
    因此最终 `violations` 反映的是"全部对外材料齐备"时的真实状态。
    """
    base = Path(root)
    write_claim_scan_report(base, scan_claims(base))
    final = scan_claims(base)
    write_claim_scan_report(base, final)
    return final


__all__ = [
    "ALLOWED_PHRASES",
    "AcceptanceEvidence",
    "AcceptanceIntegrityError",
    "CORR01_NEGATIONS",
    "ClaimScanResult",
    "EXCLUDED_FILES",
    "EXCLUDED_REASONS",
    "EXEMPTION_CONTEXT_CHARS",
    "FORBIDDEN_CLAIMS",
    "NEGATION_MARKERS",
    "NEGATION_TOLERANT_FILES",
    "SPEC_MARKERS",
    "SPEC_MODE_FILES",
    "STRICT_SCAN_FILES",
    "W12_EXTRA_FORBIDDEN",
    "assert_evidence_refs",
    "assert_limitation_integrity",
    "assert_no_demo_input",
    "check_write_surface",
    "evidence_availability",
    "regenerate_claim_scan_report",
    "render_claim_scan_report",
    "scan_claims",
    "write_claim_scan_report",
]
