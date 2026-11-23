"""AegisForge 独立桌面端启动器与本地引擎桥接器（原生桌面客户端 + 本地加密 BYOK + 实时脱敏诊断）。

功能：
1. 自动检查并初始化 `.env` 与 `DG_SECRET_MASTER_KEY`（Fernet 密钥），开箱即用支持 BYOK；
2. 提供本地 CLI 桥接（`--save-byok` / `--run-diagnose` / `--export-sarif`），供原生 Windows 桌面软件 `AegisForge.exe` 直接调用，无需跳转任何 Web 浏览器；
3. 若直接通过 Python 启动（`python -m apps.desktop.launcher`），默认拉起原生 `AegisForge.exe` 桌面程序。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]

_EDGE_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def ensure_env_and_master_key(repo_root: Path = ROOT) -> Path:
    """确保 `.env` 存在且配置了有效的 `DG_SECRET_MASTER_KEY`（不改动已有有效值）。"""
    env_path = repo_root / ".env"
    example_path = repo_root / ".env.example"
    if not env_path.exists():
        if example_path.exists():
            shutil.copyfile(example_path, env_path)
        else:
            env_path.write_text("", encoding="utf-8")

    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"^DG_SECRET_MASTER_KEY=(.*)$", text, flags=re.MULTILINE)
    if match:
        current_val = match.group(1).strip()
        if current_val and not current_val.startswith("CHANGE_ME"):
            return env_path
        new_key = Fernet.generate_key().decode("ascii")
        updated = re.sub(
            r"^DG_SECRET_MASTER_KEY=.*$",
            f"DG_SECRET_MASTER_KEY={new_key}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        env_path.write_text(updated, encoding="utf-8")
        return env_path

    new_key = Fernet.generate_key().decode("ascii")
    sep = "" if text.endswith("\n") or not text else "\n"
    env_path.write_text(
        f"{text}{sep}\n# 桌面端自动生成的 BYOK Fernet 主密钥\nDG_SECRET_MASTER_KEY={new_key}\n",
        encoding="utf-8",
    )
    return env_path


def _read_master_key(repo_root: Path = ROOT) -> bytes:
    env_path = ensure_env_and_master_key(repo_root)
    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"^DG_SECRET_MASTER_KEY=(.*)$", text, flags=re.MULTILINE)
    if not match or not match.group(1).strip():
        raise RuntimeError("DG_SECRET_MASTER_KEY 未初始化")
    return match.group(1).strip().encode("ascii")


def save_local_byok_config(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    """在本地使用 Fernet 主密钥加密保存桌面端 BYOK 模型配置。"""
    master_key = _read_master_key(repo_root)
    fernet = Fernet(master_key)
    raw_key = api_key.strip()
    ciphertext = fernet.encrypt(raw_key.encode("utf-8")).decode("ascii") if raw_key else ""
    masked = (
        f"{raw_key[:3]}***{raw_key[-4:]}"
        if len(raw_key) >= 8
        else ("***" if raw_key else "EMPTY")
    )
    out_dir = repo_root / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = out_dir / "desktop_byok.json"
    record = {
        "provider": provider.strip() or "openai_compatible",
        "model": model.strip() or "gpt-4o-mini",
        "base_url": base_url.strip() or "https://api.openai.com/v1",
        "api_key_ciphertext": ciphertext,
        "api_key_masked": masked,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    cfg_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "provider": record["provider"],
        "model": record["model"],
        "base_url": record["base_url"],
        "api_key_masked": masked,
        "config_path": str(cfg_path),
    }


def run_local_diagnosis(symptom: str, *, repo_root: Path = ROOT) -> dict[str, Any]:
    """执行本地脱敏与根因诊断演练，返回原生桌面端可直接渲染的结构化结果。"""
    ensure_env_and_master_key(repo_root)
    raw_text = (
        symptom.strip()
        or "订单库 checkout 事务 p99 延迟飙升至 4120ms，伴随 pg_locks RowExclusiveLock 等待与 phone=13812345678 慢查询"
    )
    try:
        from core.security.redaction import DataRedactionEngine

        redacted = DataRedactionEngine().redact_text(raw_text).redacted_text
    except Exception:
        redacted = re.sub(r"1[3-9]\d{9}", "[REDACTED_PHONE]", raw_text)

    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:8]
    lower = redacted.lower()
    if "lock" in lower or "锁" in lower or "wait" in lower:
        root_cause = "长事务持有 RowExclusiveLock 阻塞热点更新（建议终止阻塞 PID 并收敛事务边界）"
        action_sql = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle in transaction'"
        hub = "#07"
    elif "index" in lower or "seq" in lower or "慢" in lower or "索引" in lower:
        root_cause = "高频过滤列缺失复合索引退化为 Seq Scan（建议低峰期 CONCURRENTLY 创建覆盖索引）"
        action_sql = "CREATE INDEX CONCURRENTLY idx_orders_status_created ON orders(status, created_at)"
        hub = "#08"
    else:
        root_cause = "连接池突发饱和与统计信息陈旧耦合（建议执行 ANALYZE 并启用只读预检闸门）"
        action_sql = "ANALYZE VERBOSE public.orders"
        hub = "#04"

    return {
        "status": "ok",
        "task_id": f"T-{digest[:3].upper()}",
        "trace_hash": digest,
        "hub": hub,
        "redacted_symptom": redacted,
        "root_cause": root_cause,
        "recommended_action": action_sql,
        "confidence": 0.93,
    }


def is_dbguardian_running(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    """探测目标端口是否已运行 DBGuardian API。"""
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8", errors="ignore")
            return '"dbguardian"' in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pick_port(host: str = "127.0.0.1", preferred: int = 8000) -> tuple[int, bool]:
    """返回 `(port, already_running)`：若首选端口已运行 DBGuardian 则直接复用。"""
    if preferred > 0 and is_dbguardian_running(host, preferred):
        return preferred, True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, preferred))
            return int(sock.getsockname()[1]), False
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1]), False


def find_app_browser(candidates: Sequence[str] = _EDGE_CANDIDATES) -> str | None:
    """查找支持 `--app=` 原生窗口模式的 Chromium 内核浏览器路径。"""
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    for binary in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(binary)
        if found:
            return found
    return None


def build_browser_app_command(
    browser_exe: str,
    url: str,
    *,
    profile_dir: Path,
    width: int = 1500,
    height: int = 940,
) -> list[str]:
    """构造独立桌面窗口命令。"""
    return [
        browser_exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
    ]


def wait_until_healthy(host: str, port: int, *, timeout_s: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_dbguardian_running(host, port, timeout_s=1.0):
            return True
        time.sleep(0.25)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DBGuardian 独立桌面软件启动器与本地桥接器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="首选端口")
    parser.add_argument("--prepare-only", action="store_true", help="仅初始化 .env 与密钥并退出")
    parser.add_argument("--no-browser", action="store_true", help="只启动后端，不弹出窗口")
    parser.add_argument("--web", action="store_true", help="启动 Web 服务端模式")
    parser.add_argument("--save-byok", action="store_true", help="加密保存本地 BYOK 模型配置")
    parser.add_argument("--provider", default="openai_compatible", help="BYOK 供应商标识")
    parser.add_argument("--model", default="gpt-4o-mini", help="BYOK 模型名称")
    parser.add_argument("--base-url", default="https://api.openai.com/v1", help="BYOK Base URL")
    parser.add_argument("--api-key", default="", help="BYOK API Key")
    parser.add_argument("--run-diagnose", action="store_true", help="运行本地脱敏根因诊断演练")
    parser.add_argument("--symptom", default="", help="故障症状描述")
    parser.add_argument("--run-full-pipeline", action="store_true", help="运行十大核心模块全链路扫描、沙箱验证与可逆脱敏")
    parser.add_argument("--export-sarif", action="store_true", help="导出 SARIF v2.1.0 报告与 WORM 审计链")
    args = parser.parse_args(argv)

    ensure_env_and_master_key(ROOT)
    if args.prepare_only:
        return 0

    if args.run_full_pipeline:
        from core.platform.ten_modules import PlatformPipelineOrchestrator

        orch = PlatformPipelineOrchestrator()
        payload = orch.run_full_inspection()
        sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        return 0

    if args.export_sarif:
        from core.platform.ten_modules import PlatformPipelineOrchestrator

        orch = PlatformPipelineOrchestrator()
        orch.run_full_inspection()
        assets = orch.vault.list_assets()
        asset_map = {a.asset_id: a for a in assets}
        scored = orch.scorer.score_and_prioritize(orch.scanner.scan_assets(assets), asset_map)
        bundles = {f.finding_id: orch.fix_gen.generate(f) for f in scored}
        out_path = ROOT / "runs" / "reports" / "dbguardian-audit.sarif.json"
        sarif = orch.audit.export_sarif_report(scored, bundles, output_path=out_path)
        res = {
            "status": "ok",
            "sarif_path": str(out_path),
            "results_count": len(sarif["runs"][0]["results"]),
            "worm_entries": len(orch.audit.entries),
        }
        sys.stdout.buffer.write(json.dumps(res, ensure_ascii=False).encode("utf-8") + b"\n")
        return 0

    if args.save_byok:
        result = save_local_byok_config(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            repo_root=ROOT,
        )
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
        return 0

    if args.run_diagnose:
        result = run_local_diagnosis(args.symptom, repo_root=ROOT)
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
        return 0

    native_exe = ROOT / "AegisForge.exe"
    if not native_exe.is_file():
        native_exe = ROOT / "DBGuardian.exe"
    if os.name == "nt" and not args.web and not args.no_browser and native_exe.is_file():
        subprocess.Popen([str(native_exe)], cwd=str(ROOT))
        return 0

    port, already_running = pick_port(args.host, args.port)
    console_url = f"http://{args.host}:{port}/console/"

    server_proc: subprocess.Popen[bytes] | None = None
    if not already_running:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                args.host,
                "--port",
                str(port),
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if not wait_until_healthy(args.host, port, timeout_s=25.0):
            if server_proc.poll() is None:
                server_proc.terminate()
            return 1

    try:
        if not args.no_browser:
            browser_exe = find_app_browser()
            if browser_exe:
                profile_dir = ROOT / "runs" / ".desktop_app_profile"
                profile_dir.mkdir(parents=True, exist_ok=True)
                cmd = build_browser_app_command(browser_exe, console_url, profile_dir=profile_dir)
                subprocess.Popen(cmd, cwd=str(ROOT))
            else:
                webbrowser.open(console_url)
        if server_proc is not None:
            server_proc.wait()
    finally:
        if server_proc is not None and server_proc.poll() is None:
            server_proc.terminate()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
