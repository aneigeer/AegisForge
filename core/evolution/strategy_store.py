"""V5.0 / RSI-STEP-01：可版本化策略包仓库与不可变安全宪法锁。

设计意图（为什么这样切）：

- **唯一可自我改写面**限定为「声明式策略包」——规则、修复模板、Prompt 指导、
  噪声过滤与元经验。它们全部是数据，不携带可执行代码，因此可以被自动生成、
  比对、回滚，而不会把安全内核改坏；
- **安全宪法是代码，不是数据**：``core/guardrails/``、``core/security/``、
  ``core/audit/``、``core/orchestrator/state_graph.py`` 的规则/阈值/审计链
  只读不可写。:func:`assert_constitutional_safe` 在落盘与晋升两处强制校验，
  任何试图"跳过 HITL / 关闭脱敏 / 停用 WORM / 放宽自愈轮次"的候选包
  立即抛 :class:`ConstitutionalViolationError`；
- **版本只能写进托管目录**：``StrategyStore`` 拒绝越界路径与非法 version_id，
  并保留带 SHA-256 指纹的谱系链，支持任意代一键回滚。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

#: version_id 必须可直接做文件名，禁止路径分隔符与 ``..``。
_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 安全宪法：这些源码/配置任何自我改写路径都不得触碰（INV-CONSTITUTIONAL-LOCK）。
PROTECTED_PATHS: tuple[str, ...] = (
    "core/guardrails/",
    "core/security/",
    "core/audit/",
    "core/orchestrator/state_graph.py",
)

#: 与 ``core.guardrails.policies.BLOCKING_CATEGORIES`` 对齐的基础安全分类。
BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    {"SECRET_EXFIL", "PROMPT_INJECTION", "DESTRUCTIVE_WRITE", "PATH_ESCAPE"}
)
#: 基础策略中要求人工审批的分类：候选包不得把它们的 ``requires_hitl`` 降级。
HITL_REQUIRED_CATEGORIES: frozenset[str] = frozenset({"DESTRUCTIVE_WRITE", "PATH_ESCAPE"})
#: 允许出现的规则分类（OBSERVATION 仅用于噪声过滤，不阻断）。
ALLOWED_RULE_CATEGORIES: frozenset[str] = BLOCKING_CATEGORIES | frozenset({"OBSERVATION"})

#: 明显的"绕过安全边界"表述。命中任何一条即视为违宪，而不是靠调用方自觉。
_BYPASS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(?:skip|bypass|disable|turn\s*off|remove)\w*\s+(?:the\s+)?"
        r"(?:hitl|approval|redaction|masking|audit|worm|fernet|sandbox)"
    ),
    re.compile(
        r"(?i)\b(?:hitl|approval|redaction|masking|audit|worm|fernet)\s*"
        r"(?:=|:)\s*(?:off|none|disabled|false|0)"
    ),
    re.compile(r"(?i)max_self_correction_rounds\s*[=>:]+\s*[4-9]"),
    re.compile(r"(?:跳过|绕开|关闭|禁用|取消|免除).{0,8}(?:审批|脱敏|审计|人工|worm)", re.IGNORECASE),
    re.compile(r"(?i)\b(?:allow_all|no_policy|unrestricted_mode|sandbox_off|skip_all_checks)\b"),
)
#: 试图把自我改写落到安全内核源码上的表述。
_PROTECTED_WRITE_PATTERN = re.compile(
    r"(?i)(?:write|modify|patch|overwrite|edit|rewrite|apply\s+diff\s+to).{0,32}"
    r"(?:core/(?:guardrails|security|audit)|state_graph\.py)"
)
#: 规则里出现这些动作等价于"放行"。
_ALLOW_ACTIONS: frozenset[str] = frozenset({"allow", "pass", "permit", "ignore"})


class ConstitutionalViolationError(ValueError):
    """候选策略触碰安全宪法（跳过 HITL/脱敏/审计、越界写入、篡改指纹）。"""


class StrategyStoreError(RuntimeError):
    """策略仓库状态异常（版本缺失/未初始化）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(payload: Any) -> str:
    """稳定序列化：与既有评测/审计的 canonical 口径一致（排序键 + 紧凑分隔符）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 策略包
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentStrategyBundle:
    """一版可参与运行时决策的 Agent 策略包（Gen N）。"""

    version_id: str
    parent_version: str | None
    generation: int
    policy_rules: list[dict[str, Any]] = field(default_factory=list)
    repair_templates: dict[str, str] = field(default_factory=dict)
    prompt_directives: list[str] = field(default_factory=list)
    noise_filters: list[str] = field(default_factory=list)
    meta_lessons: list[dict[str, Any]] = field(default_factory=list)
    content_sha256: str = ""

    def mutable_payload(self) -> dict[str, Any]:
        """参与指纹计算的可变部分（不含 ``content_sha256`` 自身）。"""
        return {
            "version_id": self.version_id,
            "parent_version": self.parent_version,
            "generation": int(self.generation),
            "policy_rules": [dict(item) for item in self.policy_rules],
            "repair_templates": dict(self.repair_templates),
            "prompt_directives": list(self.prompt_directives),
            "noise_filters": list(self.noise_filters),
            "meta_lessons": [dict(item) for item in self.meta_lessons],
        }

    def compute_sha256(self) -> str:
        return _sha256(canonical_json(self.mutable_payload()))

    def seal(self) -> "AgentStrategyBundle":
        """返回带正确指纹的副本（不修改自身，保持不可变）。"""
        return replace(self, content_sha256=self.compute_sha256())

    def to_dict(self) -> dict[str, Any]:
        payload = self.mutable_payload()
        payload["content_sha256"] = self.content_sha256
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentStrategyBundle":
        return cls(
            version_id=str(data["version_id"]),
            parent_version=(None if data.get("parent_version") in (None, "") else str(data["parent_version"])),
            generation=int(data.get("generation", 0)),
            policy_rules=[dict(item) for item in data.get("policy_rules", [])],
            repair_templates={str(k): str(v) for k, v in dict(data.get("repair_templates") or {}).items()},
            prompt_directives=[str(item) for item in data.get("prompt_directives", [])],
            noise_filters=[str(item) for item in data.get("noise_filters", [])],
            meta_lessons=[dict(item) for item in data.get("meta_lessons", [])],
            content_sha256=str(data.get("content_sha256", "")),
        )


def _text_corpus(bundle: AgentStrategyBundle) -> list[tuple[str, str]]:
    """需要做违宪文本扫描的字段（``patterns`` 属于检测签名，不参与扫描）。"""
    corpus: list[tuple[str, str]] = []
    for index, directive in enumerate(bundle.prompt_directives):
        corpus.append((f"prompt_directives[{index}]", str(directive)))
    for key, value in bundle.repair_templates.items():
        corpus.append((f"repair_templates[{key}]", str(value)))
    for index, lesson in enumerate(bundle.meta_lessons):
        corpus.append((f"meta_lessons[{index}]", canonical_json(lesson)))
    for index, rule in enumerate(bundle.policy_rules):
        corpus.append((f"policy_rules[{index}].description", str(rule.get("description", ""))))
        corpus.append((f"policy_rules[{index}].action", str(rule.get("action", ""))))
    for index, noise in enumerate(bundle.noise_filters):
        corpus.append((f"noise_filters[{index}]", str(noise)))
    return corpus


def assert_constitutional_safe(bundle: AgentStrategyBundle) -> None:
    """在写盘与晋升前强制校验；任一违反即抛 :class:`ConstitutionalViolationError`。

    校验维度：
    1. 版本标识与代数合法，指纹未被篡改；
    2. 规则只增不减：分类合法、必须带检测形态、不得声明放行/停用；
    3. 不得把 ``DESTRUCTIVE_WRITE`` / ``PATH_ESCAPE`` 的人工审批降级；
    4. 任意文本字段不得出现"跳过 HITL / 关闭脱敏 / 停用 WORM / 放宽自愈轮次"；
    5. 不得把自我改写落到受保护的安全内核源码上。
    """
    problems: list[str] = []

    if not _VERSION_ID_RE.match(bundle.version_id or ""):
        problems.append(f"version_id 非法（需匹配 {_VERSION_ID_RE.pattern}）: {bundle.version_id!r}")
    if int(bundle.generation) < 0:
        problems.append("generation 不得为负")
    if bundle.content_sha256:
        expected = bundle.compute_sha256()
        if bundle.content_sha256 != expected:
            problems.append(
                "content_sha256 与内容不一致（疑似篡改）："
                f"declared={bundle.content_sha256[:12]} expected={expected[:12]}"
            )

    for index, rule in enumerate(bundle.policy_rules):
        label = f"policy_rules[{index}]"
        rule_id = str(rule.get("rule_id", ""))
        category = str(rule.get("category", ""))
        if not rule_id.startswith("GR-"):
            problems.append(f"{label}.rule_id 需形如 GR-XXX: {rule_id!r}")
        if category not in ALLOWED_RULE_CATEGORIES:
            problems.append(f"{label}.category 非法: {category!r}")
        patterns = rule.get("patterns") or []
        if not isinstance(patterns, (list, tuple)) or not patterns:
            problems.append(f"{label}.patterns 不能为空（禁止放空基础安全边界）")
        else:
            for pattern in patterns:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    problems.append(f"{label}.patterns 含非法正则: {exc}")
        if str(rule.get("action", "")).lower() in _ALLOW_ACTIONS:
            problems.append(f"{label}.action={rule.get('action')!r} 试图放行而非检测")
        if rule.get("suppress") or rule.get("disable") or rule.get("enabled") is False:
            problems.append(f"{label} 试图停用/抑制规则")
        if rule.get("replaces_base") or rule.get("override_base"):
            problems.append(f"{label} 试图替换基础安全规则")
        if category in HITL_REQUIRED_CATEGORIES and rule.get("requires_hitl") is False:
            problems.append(f"{label} 把 {category} 的人工审批降级为 False")

    for field_name, text in _text_corpus(bundle):
        for pattern in _BYPASS_PATTERNS:
            if pattern.search(text):
                problems.append(f"{field_name} 命中违宪表述: {pattern.pattern}")
        if _PROTECTED_WRITE_PATTERN.search(text):
            problems.append(f"{field_name} 试图改写受保护安全内核源码")
        for protected in PROTECTED_PATHS:
            if f"delete {protected}" in text.lower() or f"rm -rf {protected}" in text.lower():
                problems.append(f"{field_name} 试图删除受保护路径 {protected}")

    if problems:
        raise ConstitutionalViolationError("候选策略违反安全宪法：" + "；".join(problems))


def default_seed_bundle() -> AgentStrategyBundle:
    """Gen 0 基线 S_0：不含任何额外规则/模板，运行时行为等同既有默认策略。"""
    return AgentStrategyBundle(
        version_id="S_0",
        parent_version=None,
        generation=0,
        policy_rules=[],
        repair_templates={},
        prompt_directives=["修复必须先复现再改代码，禁止只加注释的敷衍补丁"],
        noise_filters=[],
        meta_lessons=[],
    ).seal()


# ---------------------------------------------------------------------------
# 策略仓库
# ---------------------------------------------------------------------------
class StrategyStore:
    """策略包的文件系统仓库：版本落盘、指纹校验、谱系追踪与一键回滚。

    目录布局（全部在 ``root_dir`` 内，绝不越界）::

        root_dir/
          versions/S_0.json
          active.json          {"version_id": "S_0", "updated_at": ...}
          lineage.json         {"records": [...]}
          archive/<ts>/...      reset_to_seed 归档的历史谱系
    """

    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir).resolve()
        self._versions_dir = self._root / "versions"
        self._versions_dir.mkdir(parents=True, exist_ok=True)
        self._active_path = self._root / "active.json"
        self._lineage_path = self._root / "lineage.json"

    # -- 路径与读写 ----------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    def _version_path(self, version_id: str) -> Path:
        if not _VERSION_ID_RE.match(version_id or ""):
            raise ConstitutionalViolationError(f"非法 version_id: {version_id!r}")
        path = (self._versions_dir / f"{version_id}.json").resolve()
        if self._versions_dir not in path.parents and path.parent != self._versions_dir:
            raise ConstitutionalViolationError(f"版本文件越界: {path}")
        return path

    def _write_version(self, bundle: AgentStrategyBundle, *, overwrite: bool = False) -> Path:
        path = self._version_path(bundle.version_id)
        if path.exists() and not overwrite:
            existing = self._read_version(bundle.version_id)
            if existing.content_sha256 != bundle.content_sha256:
                raise StrategyStoreError(
                    f"版本 {bundle.version_id} 已存在且内容不同，拒绝静默覆盖"
                )
            return path
        path.write_text(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _read_version(self, version_id: str) -> AgentStrategyBundle:
        path = self._version_path(version_id)
        if not path.is_file():
            raise StrategyStoreError(f"策略版本不存在: {version_id}")
        return AgentStrategyBundle.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def has_version(self, version_id: str) -> bool:
        try:
            return self._version_path(version_id).is_file()
        except ConstitutionalViolationError:
            return False

    # -- 谱系 ----------------------------------------------------------
    def lineage(self) -> list[dict[str, Any]]:
        if not self._lineage_path.is_file():
            return []
        data = json.loads(self._lineage_path.read_text(encoding="utf-8"))
        return [dict(item) for item in data.get("records", [])]

    def _append_lineage(
        self,
        bundle: AgentStrategyBundle,
        *,
        decision: str,
        reason: str = "",
        metrics: Mapping[str, Any] | None = None,
        rejected_mutations: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        records = self.lineage()
        record = {
            "seq": len(records) + 1,
            "version_id": bundle.version_id,
            "parent_version": bundle.parent_version,
            "generation": int(bundle.generation),
            "decision": decision,
            "reason": reason,
            "metrics": dict(metrics or {}),
            "rejected_mutations": [dict(item) for item in rejected_mutations],
            "content_sha256": bundle.content_sha256,
            "recorded_at": _now(),
        }
        records.append(record)
        self._lineage_path.write_text(
            json.dumps({"records": records}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return record

    def lineage_sha256(self) -> str:
        return _sha256(canonical_json(self.lineage()))

    # -- 生命周期 ------------------------------------------------------
    def seed_initial(self, bundle: AgentStrategyBundle) -> AgentStrategyBundle:
        """写入 Gen 0 基线并置为 active；已初始化时原样返回当前 active。"""
        if self._active_path.is_file():
            return self.get_active()
        sealed = bundle.seal()
        assert_constitutional_safe(sealed)
        self._write_version(sealed, overwrite=True)
        self._set_active(sealed.version_id)
        self._append_lineage(sealed, decision="SEED", reason="初始基线")
        return sealed

    def save_candidate(self, bundle: AgentStrategyBundle) -> AgentStrategyBundle:
        """保存候选版本（仅落盘，不改变 active）。"""
        sealed = bundle.seal()
        assert_constitutional_safe(sealed)
        if sealed.parent_version is not None and not self.has_version(sealed.parent_version):
            raise StrategyStoreError(f"候选版本引用了不存在的父版本: {sealed.parent_version}")
        self._write_version(sealed)
        return sealed

    def promote(
        self,
        version_id: str,
        *,
        reason: str = "",
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把已存在的版本置为 active，并写入谱系（PROMOTED）。"""
        bundle = self._read_version(version_id)
        # 二次校验：哪怕文件在磁盘上被改过，也要在晋升时重新过宪法锁
        assert_constitutional_safe(bundle)
        self._set_active(version_id)
        record = self._append_lineage(bundle, decision="PROMOTED", reason=reason, metrics=metrics)
        return {"promoted": True, "version_id": version_id, "lineage": record}

    def record_rejection(
        self,
        bundle: AgentStrategyBundle,
        *,
        reason: str,
        metrics: Mapping[str, Any] | None = None,
        rejected_mutations: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """记录被驳回的候选（不改变 active），供下一轮元反思避坑。"""
        sealed = bundle.seal()
        self._write_version(sealed)
        record = self._append_lineage(
            sealed,
            decision="REJECTED",
            reason=reason,
            metrics=metrics,
            rejected_mutations=rejected_mutations,
        )
        return {"promoted": False, "version_id": sealed.version_id, "lineage": record}

    def rollback(self, *, reason: str = "", to_version: str | None = None) -> dict[str, Any]:
        """回滚 active 到指定版本（缺省回父版本）；无父版本时保持不动。"""
        current = self.get_active()
        target = to_version or current.parent_version
        if not target:
            return {"rolled_back": False, "from_version": current.version_id, "active_version": current.version_id}
        if not self.has_version(target):
            raise StrategyStoreError(f"回滚目标版本不存在: {target}")
        self._set_active(target)
        target_bundle = self._read_version(target)
        self._append_lineage(
            target_bundle,
            decision="ROLLED_BACK",
            reason=reason or f"回滚自 {current.version_id}",
        )
        return {
            "rolled_back": True,
            "from_version": current.version_id,
            "active_version": target,
        }

    def get_active(self) -> AgentStrategyBundle:
        if not self._active_path.is_file():
            raise StrategyStoreError(f"策略仓库尚未初始化（缺少 {self._active_path}）")
        data = json.loads(self._active_path.read_text(encoding="utf-8"))
        return self._read_version(str(data["version_id"]))

    def _set_active(self, version_id: str) -> None:
        self._active_path.write_text(
            json.dumps({"version_id": version_id, "updated_at": _now()}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def reset_to_seed(self, bundle: AgentStrategyBundle, *, archive: bool = True) -> AgentStrategyBundle:
        """把仓库重置回 Gen 0（历史谱系归档保留），保证 CLI 每次都是可复现实验。"""
        if archive and (self._lineage_path.is_file() or self._versions_dir.exists()):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = self._root / "archive" / stamp
            target.mkdir(parents=True, exist_ok=True)
            # 归档是便利能力而非正确性前提：Windows 上文件被其他进程短暂占用时
            # 不应让整次 RSI 周期失败，留下标记供人工复核即可。
            try:
                if self._lineage_path.is_file():
                    shutil.copy2(self._lineage_path, target / "lineage.json")
                if self._active_path.is_file():
                    shutil.copy2(self._active_path, target / "active.json")
                if self._versions_dir.exists():
                    shutil.copytree(self._versions_dir, target / "versions", dirs_exist_ok=True)
            except OSError as exc:
                (target / "ARCHIVE_INCOMPLETE.txt").write_text(
                    f"归档未完成: {type(exc).__name__}: {exc}\n", encoding="utf-8"
                )
            self._lineage_path.unlink(missing_ok=True)
            self._active_path.unlink(missing_ok=True)
            shutil.rmtree(self._versions_dir, ignore_errors=True)
            self._versions_dir.mkdir(parents=True, exist_ok=True)
        elif self._active_path.is_file():
            self._active_path.unlink(missing_ok=True)
        return self.seed_initial(bundle)


__all__ = [
    "ALLOWED_RULE_CATEGORIES",
    "BLOCKING_CATEGORIES",
    "HITL_REQUIRED_CATEGORIES",
    "PROTECTED_PATHS",
    "AgentStrategyBundle",
    "ConstitutionalViolationError",
    "StrategyStore",
    "StrategyStoreError",
    "assert_constitutional_safe",
    "canonical_json",
    "default_seed_bundle",
]
