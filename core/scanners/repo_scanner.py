"""V4.1 Track A / STEP-01：本地代码库安全扫描与 Git Diff 增量采集器。

职责边界（``INV-NO-RULE-DUPLICATION``）：

- 本模块**只做输入适配**：把真实文件系统里的源码、依赖清单与 K8s Manifest
  喂给既有的 :class:`~core.plugins.appsec.plugin.AppSecProbe` 与
  :class:`~core.plugins.k8s.plugin.K8sProbe`；
- 检测正则、CVSS 与规则编号全部留在插件内，本模块不复制任何一条检测规则；
- 唯一新增的"知识"是一张**离线依赖公告表**（``OFFLINE_ADVISORIES``），
  用来给 ``requirements.txt`` 里精确锁定的版本补 ``cve`` 字段——因为
  ``AppSecProbe`` 的 SCA 分支按既有契约只消费 ``{"cve": ...}`` 已富化的依赖项，
  没有它就只能联网查询，违反 ``INV-ZERO-HEAVY-INFRA``（离线可验）。

工程红线：

- 扫描过程**不 import 目标仓库任何模块**，只按字节读文本；
- ``changed_only`` 的三种输入（``git diff`` / ``git status`` / 全量回退）都必须
  把失败原因写进 ``warnings``，禁止静默吞错；
- 二进制、超限文件与被排除目录一律记入 ``skipped_files`` 并跳过，绝不解码。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.plugins.appsec.plugin import AppSecPlugin
from core.plugins.contracts import (
    DOMAIN_APPSEC,
    DOMAIN_K8S,
    DomainPlugin,
    ProbeBudget,
    ProbeFinding,
    TargetAsset,
)
from core.plugins.k8s.plugin import K8sPlugin
from core.plugins.registry import DomainPluginRegistry

#: 默认排除目录（与规范一致，另补 ``.pytest_cache`` / ``.mypy_cache`` / egg-info 噪声目录）。
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "runs",
        ".screenshots",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dbguardian.egg-info",
        "项目规划",
    }
)

#: 被视为"后端源码"的扩展名（首期重点保证 ``.py`` 与通用文本规则命中）。
CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".js", ".ts", ".go", ".java"})
#: K8s 清单可能出现的 YAML 扩展名。
YAML_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml"})
#: 依赖清单文件名（``requirements*.txt`` 另行按通配匹配）。
DEPENDENCY_FILES: frozenset[str] = frozenset({"pyproject.toml", "package.json"})

DEFAULT_MAX_FILES = 500
DEFAULT_MAX_FILE_BYTES = 524_288  # 512 KiB
#: 目录遍历时忽略的单文件（版本控制与锁文件不属于代码审计输入）。
_IGNORED_FILENAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})
#: 依赖声明行中不作为包名的 Docker/PEP 508 前缀。
_DEP_SKIP_PREFIXES: tuple[str, ...] = ("-r", "-e", "--", "git+", "http://", "https://", "file:")

_REQ_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*"
    r"(?P<op>===|==|>=|<=|~=|!=|>|<)?\s*(?P<version>[0-9][0-9A-Za-z.\-]*)?"
)
_REQ_INLINE_CVE_RE = re.compile(r"cve[:\s=]+(?P<cve>CVE-\d{4}-\d{4,})", re.IGNORECASE)
_REQ_INLINE_FIXED_RE = re.compile(r"fixed[:\s=]+(?P<fixed>[0-9][0-9A-Za-z.\-]*)", re.IGNORECASE)
_PYPROJECT_REQ_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(?P<rest>.*)$")

#: 离线依赖公告种子表：``包名 -> {精确版本: (CVE, 修复版本)}``。
#:
#: 只收录**可离线复现**的公开通告，用于 CI 门禁在无网络环境下仍能给出 SCA 结论。
#: 新增条目必须写明来源（NVD / GHSA），不接受"看起来像"的猜测。
OFFLINE_ADVISORIES: dict[str, dict[str, tuple[str, str]]] = {
    "requests": {
        "2.19.0": ("CVE-2023-32681", "2.31.0"),
        "2.19.1": ("CVE-2023-32681", "2.31.0"),
        "2.6.0": ("CVE-2018-18074", "2.20.0"),
    },
    "pyyaml": {
        "5.3.1": ("CVE-2020-14343", "5.4.1"),
        "5.1": ("CVE-2017-18342", "5.4"),
    },
    "lodash": {
        "4.17.15": ("CVE-2020-8203", "4.17.19"),
    },
    "django": {
        "3.2.0": ("CVE-2021-33203", "3.2.4"),
    },
}


# ---------------------------------------------------------------------------
# 配置与结果模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RepoScanConfig:
    """一次本地仓库扫描的输入约定。"""

    repo_root: Path
    changed_only: bool = False
    base_ref: str | None = None
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def resolved_root(self) -> Path:
        root = Path(self.repo_root).expanduser()
        if not root.is_dir():
            raise ValueError(f"仓库根目录不存在或不是目录: {root}")
        return root.resolve()


@dataclass(frozen=True)
class RepoInventory:
    """扫描输入清单：源码 / 依赖 / K8s Manifest / 被跳过文件。"""

    asset_id: str
    sources: dict[str, str] = field(default_factory=dict)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    k8s_manifests: dict[str, str] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """瘦视图：只含计数与相对路径，不含源码内容。"""
        return {
            "asset_id": self.asset_id,
            "source_count": len(self.sources),
            "dependency_count": len(self.dependencies),
            "k8s_manifest_count": len(self.k8s_manifests),
            "skipped_files": list(self.skipped_files),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RepoScanResult:
    """扫描结果：统一 ProbeFinding 列表 + 采集清单 + 可观测元数据。"""

    asset_id: str
    findings: tuple[ProbeFinding, ...]
    inventory: RepoInventory
    domains: tuple[str, ...] = ()
    scanned_files: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def highest_priority(self) -> str:
        """最高风险等级（无发现时返回 ``P3``，即"无风险"的保守下界）。"""
        if not self.findings:
            return "P3"
        return min((priority_from_cvss(item.base_cvss) for item in self.findings), key=_priority_rank)

    def count_by_priority(self) -> dict[str, int]:
        counts = {level: 0 for level in ("P0", "P1", "P2", "P3")}
        for finding in self.findings:
            counts[priority_from_cvss(finding.base_cvss)] += 1
        return counts

    def to_summary(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "finding_count": len(self.findings),
            "highest_priority": self.highest_priority,
            "by_priority": self.count_by_priority(),
            "domains": list(self.domains),
            "scanned_files": self.scanned_files,
            "skipped_files": list(self.inventory.skipped_files),
            "warnings": list(self.warnings),
            "findings": [finding.public_view() for finding in self.findings],
        }


# ---------------------------------------------------------------------------
# 优先级工具
# ---------------------------------------------------------------------------
_PRIORITY_ORDER: tuple[str, ...] = ("P0", "P1", "P2", "P3")


def _priority_rank(priority: str) -> int:
    return _PRIORITY_ORDER.index(priority) if priority in _PRIORITY_ORDER else len(_PRIORITY_ORDER)


def priority_from_cvss(cvss: float) -> str:
    """CVSS 基础分 -> 门禁优先级（与 SARIF level 阈值同源：7.0 为 error 线）。"""
    try:
        score = float(cvss)
    except (TypeError, ValueError):
        return "P3"
    if score >= 9.0:
        return "P0"
    if score >= 7.0:
        return "P1"
    if score >= 4.0:
        return "P2"
    return "P3"


# ---------------------------------------------------------------------------
# 依赖解析
# ---------------------------------------------------------------------------
def _with_advisory(name: str, version: str) -> dict[str, Any] | None:
    advisory = OFFLINE_ADVISORIES.get(name.strip().lower(), {})
    hit = advisory.get(version.strip())
    if hit is None:
        return None
    cve, fixed = hit
    return {"cve": cve, "fixed_version": fixed}


def _parse_requirement_line(line: str) -> dict[str, Any] | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    lowered = raw.lower()
    if lowered.startswith(_DEP_SKIP_PREFIXES):
        return None

    # 先把 ``# cve:/fixed:`` 行内注解摘出来（fixture 与内部清单可用其显式标注）。
    annotation = raw
    requirement = raw.split("#", 1)[0].strip() if "#" in raw else raw
    match = _REQ_LINE_RE.match(requirement)
    if match is None:
        return None
    name = match.group("name")
    op = match.group("op") or ""
    version = match.group("version") or ""
    if not name:
        return None

    dep: dict[str, Any] = {"name": name, "version": version or "*", "source": "requirements"}
    pinned = bool(version) and op in ("==", "===", "")
    if pinned:
        advisory = _with_advisory(name, version)
        if advisory:
            dep.update(advisory)
        inline_cve = _REQ_INLINE_CVE_RE.search(annotation)
        inline_fixed = _REQ_INLINE_FIXED_RE.search(annotation)
        if inline_cve:
            dep["cve"] = inline_cve.group("cve").upper()
            dep["fixed_version"] = (inline_fixed.group("fixed") if inline_fixed else "latest-safe")
    return dep


def _parse_requirements_text(text: str, *, source: str) -> list[dict[str, Any]]:
    deps: list[dict[str, Any]] = []
    for line in text.splitlines():
        dep = _parse_requirement_line(line)
        if dep is not None:
            dep["source"] = source
            deps.append(dep)
    return deps


def _parse_pyproject_text(text: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    project = data.get("project") or {}
    raw_deps: Iterable[Any] = project.get("dependencies") or []
    deps: list[dict[str, Any]] = []
    for item in raw_deps:
        if not isinstance(item, str):
            continue
        cleaned = item.split(";", 1)[0].strip()
        match = _PYPROJECT_REQ_RE.match(cleaned)
        if match is None:
            continue
        name = match.group("name")
        rest = match.group("rest") or ""
        version_match = re.search(r"==\s*([0-9][0-9A-Za-z.\-]*)", rest)
        version = version_match.group(1) if version_match else ""
        dep: dict[str, Any] = {"name": name, "version": version or "*", "source": "pyproject.toml"}
        if version:
            advisory = _with_advisory(name, version)
            if advisory:
                dep.update(advisory)
        deps.append(dep)
    return deps


def _parse_package_json_text(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    deps: list[dict[str, Any]] = []
    for section in ("dependencies", "devDependencies"):
        block = data.get(section) or {}
        if not isinstance(block, dict):
            continue
        for name, version in block.items():
            cleaned = str(version).lstrip("^~=v ")
            dep: dict[str, Any] = {
                "name": str(name),
                "version": cleaned or "*",
                "source": f"package.json:{section}",
            }
            advisory = _with_advisory(str(name), cleaned)
            if advisory:
                dep.update(advisory)
            deps.append(dep)
    return deps


# ---------------------------------------------------------------------------
# 文件遍历与增量过滤
# ---------------------------------------------------------------------------
def _git_changed_paths(root: Path, base_ref: str | None) -> tuple[list[str] | None, list[str]]:
    """返回 ``(changes, warnings)``；``changes`` 为 ``None`` 表示需要全量回退。"""
    warnings: list[str] = []
    diff_args = (
        ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD"]
        if base_ref
        else ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=ACMR", "HEAD"]
    )
    code, out, err = _run_git(diff_args)
    if code == 0:
        return _split_git_output(out), warnings
    warnings.append(f"git diff 失败（exit={code}）：{_tail(err)}；回退 git status --porcelain")

    code, out, err = _run_git(["git", "-C", str(root), "status", "--porcelain"])
    if code == 0:
        paths: list[str] = []
        for line in out.splitlines():
            rel = line[3:].strip() if len(line) > 3 else ""
            if "->" in rel:  # 重命名行形如 "R  old -> new"
                rel = rel.split("->", 1)[1].strip()
            if rel:
                paths.append(rel.strip('"'))
        return paths, warnings

    warnings.append(f"git status 亦失败（exit={code}）：{_tail(err)}；回退全量扫描")
    return None, warnings


def _run_git(args: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境缺失 git
        return 127, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


def _split_git_output(stdout: str) -> list[str]:
    return [line.strip().strip('"') for line in stdout.splitlines() if line.strip()]


def _tail(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat[-limit:] if len(flat) > limit else flat


def _iter_candidate_files(root: Path) -> Iterable[Path]:
    """确定性遍历（排序后产出），并跳过排除目录。"""
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in EXCLUDED_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                if entry.name in _IGNORED_FILENAMES:
                    continue
                yield entry


def _is_dependency_file(name: str) -> bool:
    lowered = name.lower()
    if lowered in DEPENDENCY_FILES:
        return True
    return lowered.startswith("requirements") and lowered.endswith(".txt")


def _read_text_safely(path: Path, max_bytes: int) -> tuple[str | None, str | None]:
    """返回 ``(text, skip_reason)``；二进制或超限时 ``text is None``。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"stat:{type(exc).__name__}"
    if size > max_bytes:
        return None, f"oversize:{size}>{max_bytes}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"read:{type(exc).__name__}"
    if b"\x00" in raw:
        return None, "binary"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "non_utf8"


def _is_k8s_manifest(text: str) -> bool:
    """以 ``apiVersion`` + ``kind`` 双标记判定 K8s 清单。

    只看内容不看路径：``k8s/`` / ``deploy/`` 下的清单同样带这两个标记，
    而 ``.github/workflows/*.yml`` 两者都没有，因此不会误判。
    """
    return "apiVersion:" in text and "kind:" in text


# ---------------------------------------------------------------------------
# 采集入口
# ---------------------------------------------------------------------------
def build_asset_id(root: Path) -> str:
    """确定性资产 ID：仓库名 + 绝对路径摘要（同一仓库跨次扫描稳定）。"""
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "repo"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"repo-{name}-{digest}"


def collect_repo_inventory(config: RepoScanConfig) -> RepoInventory:
    """遍历本地仓库，产出可直接注入插件探针的 :class:`RepoInventory`。"""
    root = config.resolved_root()
    asset_id = build_asset_id(root)
    sources: dict[str, str] = {}
    dependencies: list[dict[str, Any]] = []
    k8s_manifests: dict[str, str] = {}
    skipped: list[str] = []
    warnings: list[str] = []

    allowlist: set[str] | None = None
    if config.changed_only:
        changes, git_warnings = _git_changed_paths(root, config.base_ref)
        warnings.extend(git_warnings)
        if changes is not None:
            allowlist = {item.replace("\\", "/").lstrip("./") for item in changes}

    accepted = 0
    for path in _iter_candidate_files(root):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - 符号链接逃逸保护
            continue
        if allowlist is not None and rel not in allowlist:
            continue

        suffix = path.suffix.lower()
        is_code = suffix in CODE_EXTENSIONS
        is_yaml = suffix in YAML_EXTENSIONS
        is_dep = _is_dependency_file(path.name)
        if not (is_code or is_yaml or is_dep):
            continue

        if accepted >= config.max_files:
            skipped.append(rel)
            continue

        text, skip_reason = _read_text_safely(path, config.max_file_bytes)
        if text is None:
            skipped.append(rel)
            if skip_reason and not skip_reason.startswith("oversize"):
                warnings.append(f"跳过 {rel}：{skip_reason}")
            continue

        accepted += 1
        if is_code:
            sources[rel] = text
        elif is_dep:
            if path.name.lower() == "pyproject.toml":
                dependencies.extend(_parse_pyproject_text(text))
            elif path.name.lower() == "package.json":
                dependencies.extend(_parse_package_json_text(text))
            else:
                dependencies.extend(_parse_requirements_text(text, source=rel))
        elif is_yaml and _is_k8s_manifest(text):
            k8s_manifests[rel] = text

    if config.changed_only:
        warnings.append(
            f"增量模式：{'git diff 白名单 ' + str(len(allowlist)) + ' 个文件' if allowlist is not None else '全量回退'}"
        )
    return RepoInventory(
        asset_id=asset_id,
        sources=sources,
        dependencies=_dedupe_dependencies(dependencies),
        k8s_manifests=k8s_manifests,
        skipped_files=skipped,
        warnings=warnings,
    )


def parse_dependency_line(line: str) -> dict[str, Any] | None:
    """公开的依赖行解析入口（供评测引擎与外部工具复用同一份离线公告口径）。"""
    return _parse_requirement_line(line)


def _dedupe_dependencies(dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for dep in dependencies:
        key = (str(dep.get("name", "")).lower(), str(dep.get("version", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(dep)
    return result


# ---------------------------------------------------------------------------
# 扫描入口
# ---------------------------------------------------------------------------
def _resolve_plugin(
    registry: DomainPluginRegistry | None,
    domain: str,
    factory: type[AppSecPlugin] | type[K8sPlugin],
) -> DomainPlugin:
    if registry is not None:
        existing = registry.get(domain)
        if existing is not None:
            return existing
    return factory()  # type: ignore[call-arg]


def _k8s_manifests_payload(inventory: RepoInventory) -> dict[str, Any]:
    manifests = [
        {"path": path, "text": text}
        for path, text in sorted(inventory.k8s_manifests.items())
    ]
    has_network_policy = any("kind: NetworkPolicy" in text for text in inventory.k8s_manifests.values())
    return {"manifests": manifests, "has_network_policy": has_network_policy}


def scan_repository(
    config: RepoScanConfig,
    *,
    plugins: DomainPluginRegistry | None = None,
) -> RepoScanResult:
    """采集 + 注入既有探针 + 产出统一 :class:`ProbeFinding` 列表。"""
    inventory = collect_repo_inventory(config)
    asset_id = inventory.asset_id
    findings: list[ProbeFinding] = []
    domains: list[str] = []
    warnings = list(inventory.warnings)

    app_asset = TargetAsset(
        asset_id=asset_id,
        domain=DOMAIN_APPSEC,
        engine="python",
        version="",
        endpoint=f"repo://{config.repo_root}",
        labels=(("repo_scan", "true"),),
    )

    appsec = _resolve_plugin(plugins, DOMAIN_APPSEC, AppSecPlugin)
    if inventory.sources or inventory.dependencies:
        setter_sources = getattr(appsec, "set_sources", None)
        setter_deps = getattr(appsec, "set_dependencies", None)
        if callable(setter_sources):
            setter_sources(asset_id, inventory.sources)
        if callable(setter_deps):
            setter_deps(asset_id, inventory.dependencies)
        report = appsec.probe().collect(app_asset, budget=ProbeBudget())
        findings.extend(report.findings)
        domains.append(DOMAIN_APPSEC)
        if report.probe_tool.endswith("throttled"):
            warnings.append("AppSec 探针被限流（预算耗尽），本次结果可能不完整")

    if inventory.k8s_manifests:
        k8s_asset = TargetAsset(
            asset_id=f"{asset_id}-k8s",
            domain=DOMAIN_K8S,
            engine="kubernetes",
            endpoint=f"repo://{config.repo_root}#k8s",
            labels=(("repo_scan", "true"),),
        )
        k8s = _resolve_plugin(plugins, DOMAIN_K8S, K8sPlugin)
        payload = _k8s_manifests_payload(inventory)
        # 契约方法在 probe 侧（``K8sProbe.set_facts``），插件侧只暴露 ``set_manifests``；
        # 这里优先走 probe，缺失时退回插件便捷方法，二者写入同一份事实。
        probe_setter = getattr(k8s.probe(), "set_facts", None)
        plugin_setter = getattr(k8s, "set_manifests", None)
        injected = False
        if callable(probe_setter):
            probe_setter(k8s_asset.asset_id, payload)
            injected = True
        elif callable(plugin_setter):  # pragma: no cover - 第三方插件便捷入口
            plugin_setter(
                k8s_asset.asset_id,
                list(payload["manifests"]),
                has_network_policy=bool(payload["has_network_policy"]),
            )
            injected = True
        if injected:
            report = k8s.probe().collect(k8s_asset, budget=ProbeBudget())
            findings.extend(report.findings)
            domains.append(DOMAIN_K8S)
        else:  # pragma: no cover - 插件未暴露任何注入点
            warnings.append("K8s 插件未暴露 set_facts / set_manifests，Manifest 未送审")

    findings.sort(key=lambda item: (_priority_rank(priority_from_cvss(item.base_cvss)), item.location))
    return RepoScanResult(
        asset_id=asset_id,
        findings=tuple(findings),
        inventory=inventory,
        domains=tuple(dict.fromkeys(domains)),
        scanned_files=len(inventory.sources) + len(inventory.k8s_manifests),
        warnings=tuple(warnings),
    )


__all__ = [
    "CODE_EXTENSIONS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
    "DEPENDENCY_FILES",
    "EXCLUDED_DIRS",
    "OFFLINE_ADVISORIES",
    "RepoInventory",
    "RepoScanConfig",
    "RepoScanResult",
    "build_asset_id",
    "collect_repo_inventory",
    "parse_dependency_line",
    "priority_from_cvss",
    "scan_repository",
]
