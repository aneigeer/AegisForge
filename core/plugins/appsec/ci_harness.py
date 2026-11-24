"""V4.1 Track A / STEP-02：隔离临时目录 Patch 沙箱验证器。

为什么需要它：``AppSecHarness`` 原有判定是**文本级**的（Patch 形状、模式是否被移除、
回滚是否形似 Patch），并不真正把补丁落到文件上。本模块补上"真实落地"这一层：

1. 在 :class:`tempfile.TemporaryDirectory` 内浅拷贝补丁涉及的文件（不含 ``.venv``
   等重目录），**绝不写 ``repo_root``**；
2. 解析并应用 unified diff：hunk 的 ``-`` 行必须与目标文件逐行一致，否则视为
   ``PATCH_APPLY_FAILED``（不做模糊匹配，避免把补丁落在错误位置）；
3. 对改动过的 ``*.py`` 执行 ``python -m py_compile``；
4. 可选执行 ``test_command``，受 ``timeout_seconds`` 硬约束（超时按失败处理）；
5. 应用反向 Patch 并逐个比对 SHA-256 —— 只有全部还原到原始哈希才算
   ``rollback_verified=True``。

安全红线：沙箱日志**只保留错误类型与位置**，丢弃源码回显行，避免把硬编码凭据
或 PII 带进日志（``INV-FAIL-CLOSED-PRIVACY``）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core.plugins.contracts import HarnessOutcome

#: 沙箱拷贝工作区时跳过的重目录（与扫描器口径一致，但独立常量以免反向依赖）。
_SKIP_DIRS: frozenset[str] = frozenset(
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
        "dbguardian.egg-info",
        "项目规划",
    }
)
#: 整树拷贝上限：超过即只拷贝受影响文件（测试命令可能因缺文件失败，会在 logs 里说明）。
_MAX_TREE_FILES = 2000
_MAX_TREE_BYTES = 64 * 1024 * 1024
_MAX_LOG_LINES = 24

_FILE_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+(\S+)")
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
_ERROR_LINE_RE = re.compile(
    r"^(?:File \"|SyntaxError|IndentationError|TabError|TypeError|ValueError|NameError|"
    r"FAILED |ERROR |E   |={2,} .*(?:passed|failed|error)|_{5,} )"
)


class PatchFormatError(ValueError):
    """补丁文本无法解析（不是 unified diff）。"""


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple["Hunk", ...]


@dataclass(frozen=True)
class Hunk:
    start: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]


@dataclass(frozen=True)
class RepoPatchSandboxResult:
    """一次沙箱落地的结构化结果（``HarnessOutcome`` 的底层明细）。"""

    patch_applied: bool
    compile_ok: bool
    tests_passed: bool | None
    rollback_verified: bool
    exit_code: int
    logs: str
    touched_files: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "patch_applied": self.patch_applied,
            "compile_ok": self.compile_ok,
            "tests_passed": self.tests_passed,
            "rollback_verified": self.rollback_verified,
            "exit_code": self.exit_code,
            "touched_files": list(self.touched_files),
            "failures": list(self.failures),
            "logs": self.logs,
        }


# ---------------------------------------------------------------------------
# 补丁解析
# ---------------------------------------------------------------------------
def parse_unified_diff(patch_text: str) -> list[FilePatch]:
    """解析 unified diff（支持单文件多 hunk / 多文件）。"""
    if not patch_text or not patch_text.strip():
        raise PatchFormatError("补丁为空")
    if not patch_text.startswith("--- ") and "\n--- " not in patch_text:
        raise PatchFormatError("不是 unified diff：缺少 --- 头")

    patches: list[FilePatch] = []
    current_path = ""
    hunks: list[Hunk] = []
    hunk_start = 0
    old_lines: list[str] = []
    new_lines: list[str] = []
    in_hunk = False

    def flush_hunk() -> None:
        nonlocal old_lines, new_lines, in_hunk
        if in_hunk:
            hunks.append(Hunk(start=hunk_start, old_lines=tuple(old_lines), new_lines=tuple(new_lines)))
        old_lines, new_lines, in_hunk = [], [], False

    def flush_file() -> None:
        nonlocal current_path, hunks
        flush_hunk()
        if current_path:
            patches.append(FilePatch(path=current_path, hunks=tuple(hunks)))
        hunks = []

    for raw_line in patch_text.splitlines():
        header = _FILE_HEADER_RE.match(raw_line)
        if raw_line.startswith("--- "):
            flush_file()
            # 新文件头：路径在 +++ 行确定，这里只做占位
            current_path = ""
            continue
        if raw_line.startswith("+++ "):
            if header is None:
                raise PatchFormatError(f"非法的 +++ 行: {raw_line}")
            current_path = _normalize_path(header.group(1))
            continue
        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            flush_hunk()
            hunk_start = int(hunk_match.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            # 允许文件头之间的空行与 diff --git / index 等元信息
            continue
        if raw_line.startswith("@@"):
            flush_hunk()
            continue
        if raw_line.startswith("-"):
            old_lines.append(raw_line[1:])
        elif raw_line.startswith("+"):
            new_lines.append(raw_line[1:])
        elif raw_line.startswith(" ") or raw_line == "":
            context = raw_line[1:] if raw_line.startswith(" ") else ""
            old_lines.append(context)
            new_lines.append(context)
        # "\ No newline at end of file" 等标记行忽略

    flush_file()
    if not patches or not any(item.hunks for item in patches):
        raise PatchFormatError("补丁不含任何有效 hunk")
    return patches


def _normalize_path(raw: str) -> str:
    path = raw.strip().split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.replace("\\", "/")


# ---------------------------------------------------------------------------
# 沙箱落地
# ---------------------------------------------------------------------------
def _iter_tree(root: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    total = 0
    stack = [root]
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
                if entry.name in _SKIP_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
                total += entry.stat().st_size
    return files, total


def _stage_workdir(root: Path, touched: tuple[str, ...], workdir: Path, *, copy_tree: bool) -> list[str]:
    """把受影响文件（必要时整棵轻量树）复制进隔离目录。"""
    logs: list[str] = []
    copied: set[str] = set()

    def copy_one(rel: str) -> None:
        source = root / rel
        if not source.is_file():
            return
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(rel)

    if copy_tree:
        files, total = _iter_tree(root)
        if len(files) <= _MAX_TREE_FILES and total <= _MAX_TREE_BYTES:
            for path in files:
                rel = path.relative_to(root).as_posix()
                copy_one(rel)
            logs.append(f"沙箱工作区: 整树拷贝 {len(copied)} 个文件")
            return logs
        logs.append(
            f"沙箱工作区: 目标树过大（files={len(files)}, bytes={total}），退化为仅拷贝受影响文件"
        )
    for rel in touched:
        copy_one(rel)
    logs.append(f"沙箱工作区: 拷贝受影响文件 {len(copied)} 个")
    return logs


def _apply_patch(workdir: Path, patches: list[FilePatch]) -> tuple[bool, list[str], list[str], set[str]]:
    """返回 ``(ok, logs, failures, changed_files)``；不改动 ``workdir`` 之外任何路径。"""
    logs: list[str] = []
    failures: list[str] = []
    changed: set[str] = set()

    for file_patch in patches:
        target = workdir / file_patch.path
        if not target.is_file():
            failures.append("PATCH_TARGET_MISSING")
            logs.append(f"补丁目标文件不存在: {file_patch.path}")
            return False, logs, failures, changed
        original_text = target.read_text(encoding="utf-8")
        newline = "\r\n" if "\r\n" in original_text else "\n"
        had_trailing = original_text.endswith(("\n", "\r\n"))
        lines = original_text.splitlines()
        offset = 0
        for hunk in file_patch.hunks:
            index = hunk.start - 1 + offset
            actual = tuple(line.rstrip("\r") for line in lines[index : index + len(hunk.old_lines)])
            expected = tuple(line.rstrip("\r") for line in hunk.old_lines)
            if actual != expected:
                failures.append("PATCH_CONTEXT_MISMATCH")
                logs.append(f"hunk 上下文不匹配: {file_patch.path}@{hunk.start}")
                return False, logs, failures, changed
            lines[index : index + len(hunk.old_lines)] = list(hunk.new_lines)
            offset += len(hunk.new_lines) - len(hunk.old_lines)
        payload = newline.join(lines)
        if had_trailing:
            payload += newline
        target.write_text(payload, encoding="utf-8")
        changed.add(file_patch.path)
        logs.append(f"已应用补丁: {file_patch.path}（{len(file_patch.hunks)} 个 hunk）")
    return True, logs, failures, changed


def _compile_changed(workdir: Path, changed: set[str], timeout_seconds: int) -> tuple[bool, int, list[str], list[str]]:
    python_files = sorted(path for path in changed if path.endswith(".py"))
    if not python_files:
        return True, 0, ["无 Python 文件变更，跳过 py_compile"], []
    exit_code = 0
    logs: list[str] = []
    failures: list[str] = []
    for rel in python_files:
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "py_compile", rel],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures.append("COMPILE_TIMEOUT")
            logs.append(f"py_compile 超时(>{timeout_seconds}s): {rel}")
            return False, 124, logs, failures
        if completed.returncode != 0:
            exit_code = completed.returncode
            logs.append(f"py_compile 失败: {rel}")
            logs.extend(_summarize_stream(completed.stderr or completed.stdout))
            if "COMPILE_FAILED" not in failures:
                failures.append("COMPILE_FAILED")
    if not failures:
        logs.append(f"py_compile 通过: {len(python_files)} 个文件")
    return not failures, exit_code, logs, failures


def _run_test_command(
    workdir: Path, test_command: list[str], timeout_seconds: int
) -> tuple[bool | None, int, list[str], list[str]]:
    if not test_command:
        return None, 0, ["未指定 test_command，跳过隔离单测"], []
    try:
        completed = subprocess.run(
            test_command,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, 124, [f"test_command 超时(>{timeout_seconds}s)，按失败处理"], ["SANDBOX_TESTS_TIMEOUT"]
    except OSError as exc:
        return False, 127, [f"test_command 无法执行: {type(exc).__name__}"], ["SANDBOX_TESTS_UNAVAILABLE"]

    passed = completed.returncode == 0
    logs = [f"test_command exit={completed.returncode}: {' '.join(test_command)}"]
    if not passed:
        logs.extend(_summarize_stream(completed.stdout + "\n" + completed.stderr))
    return passed, completed.returncode, logs, ([] if passed else ["SANDBOX_TESTS_FAILED"])


def _summarize_stream(text: str) -> list[str]:
    """只保留错误类型/位置行，丢弃源码回显，防止日志带出凭据原文。"""
    kept: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("^"):
            continue
        if not _ERROR_LINE_RE.match(stripped):
            continue
        kept.append(_scrub_values(stripped))
        if len(kept) >= _MAX_LOG_LINES:
            break
    return kept


_LITERAL_RE = re.compile(r"(\"[^\"\n]{12,}\"|'[^'\n]{12,}')")


def _scrub_values(line: str) -> str:
    """把错误行里过长的字符串字面量折叠掉（源码回显常见泄漏面）。"""
    return _LITERAL_RE.sub("<literal>", line)


def _tree_hashes(workdir: Path, rels: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for rel in sorted(rels):
        path = workdir / rel
        if path.is_file():
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def run_patch_sandbox(
    repo_root: Path,
    patch_text: str,
    rollback_patch_text: str,
    *,
    test_command: list[str] | None = None,
    timeout_seconds: int = 30,
) -> RepoPatchSandboxResult:
    """在隔离临时目录落地补丁并给出结构化结果（不触碰 ``repo_root``）。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正整数")
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root 不存在: {root}")

    logs: list[str] = []
    try:
        patches = parse_unified_diff(patch_text)
        rollback_patches = parse_unified_diff(rollback_patch_text)
    except PatchFormatError as exc:
        return RepoPatchSandboxResult(
            patch_applied=False,
            compile_ok=False,
            tests_passed=None,
            rollback_verified=False,
            exit_code=2,
            logs=f"补丁解析失败: {exc}",
            failures=("PATCH_PARSE_FAILED",),
        )

    touched = tuple(dict.fromkeys(item.path for item in patches))
    with tempfile.TemporaryDirectory(prefix="aegisforge-patch-") as tmp:
        workdir = Path(tmp)
        logs.extend(_stage_workdir(root, touched, workdir, copy_tree=bool(test_command)))
        baseline = _tree_hashes(workdir, set(touched))

        applied, apply_logs, apply_failures, changed = _apply_patch(workdir, patches)
        logs.extend(apply_logs)
        if not applied:
            return RepoPatchSandboxResult(
                patch_applied=False,
                compile_ok=False,
                tests_passed=None,
                rollback_verified=False,
                exit_code=2,
                logs="\n".join(logs),
                touched_files=touched,
                failures=tuple(apply_failures),
            )

        compile_ok, compile_exit, compile_logs, compile_failures = _compile_changed(
            workdir, changed, timeout_seconds
        )
        logs.extend(compile_logs)

        tests_passed: bool | None = None
        exit_code = compile_exit
        test_failures: list[str] = []
        if compile_ok:
            tests_passed, test_exit, test_logs, test_failures = _run_test_command(
                workdir, list(test_command or []), timeout_seconds
            )
            logs.extend(test_logs)
            if test_exit:
                exit_code = test_exit

        rollback_ok = False
        try:
            rb_applied, rb_logs, rb_failures, _rb_changed = _apply_patch(workdir, rollback_patches)
            logs.extend(rb_logs)
            if rb_applied:
                restored = _tree_hashes(workdir, set(touched))
                rollback_ok = restored == baseline
                if not rollback_ok:
                    logs.append("回滚后文件哈希与原始不一致")
            else:
                logs.extend(rb_failures)
        except PatchFormatError as exc:
            logs.append(f"反向补丁解析失败: {exc}")

        failures: list[str] = []
        failures.extend(compile_failures)
        failures.extend(test_failures)
        if not rollback_ok:
            failures.append("ROLLBACK_HASH_MISMATCH")

        return RepoPatchSandboxResult(
            patch_applied=True,
            compile_ok=compile_ok,
            tests_passed=tests_passed,
            rollback_verified=rollback_ok,
            exit_code=exit_code,
            logs="\n".join(logs),
            touched_files=touched,
            failures=tuple(dict.fromkeys(failures)),
        )


def verify_patch_in_isolated_workdir(
    repo_root: Path,
    patch_text: str,
    rollback_patch_text: str,
    *,
    test_command: list[str] | None = None,
    timeout_seconds: int = 30,
) -> HarnessOutcome:
    """把沙箱结果映射为插件契约要求的 :class:`HarnessOutcome`。"""
    result = run_patch_sandbox(
        repo_root,
        patch_text,
        rollback_patch_text,
        test_command=test_command,
        timeout_seconds=timeout_seconds,
    )
    tests_ok = result.tests_passed is not False
    dry_run_passed = result.patch_applied and result.compile_ok and tests_ok
    all_good = dry_run_passed and result.rollback_verified and not result.failures
    logs = ["沙箱: tempfile 隔离工作区 (py_compile + 可选单测)"]
    logs.extend(result.logs.splitlines())
    return HarnessOutcome(
        proposal_id="",
        poc_reproduced=result.patch_applied,
        poc_blocked_after_fix=dry_run_passed,
        dry_run_passed=dry_run_passed,
        rollback_verified=result.rollback_verified,
        compatibility_score=1.0 if all_good else 0.0,
        sandbox_pool="TEMP_WORKDIR",
        logs=tuple(logs),
        failures=result.failures,
    )


__all__ = [
    "FilePatch",
    "Hunk",
    "PatchFormatError",
    "RepoPatchSandboxResult",
    "parse_unified_diff",
    "run_patch_sandbox",
    "verify_patch_in_isolated_workdir",
]
