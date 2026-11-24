"""桌面端启动器与打包配置单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.fernet import Fernet

from apps.desktop.launcher import (
    build_browser_app_command,
    ensure_env_and_master_key,
    get_eval_and_data_summary,
    main,
    pick_port,
    run_desktop_golden_export,
    run_guardrail_eval,
    run_local_diagnosis,
    save_local_byok_config,
    scan_local_repo,
)


def test_ensure_env_and_master_key_generates_and_preserves_fernet_key(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("DG_ENV=dev\nDG_SECRET_MASTER_KEY=\n", encoding="utf-8")

    env_path = ensure_env_and_master_key(tmp_path)
    assert env_path.is_file()
    content1 = env_path.read_text(encoding="utf-8")
    lines = dict(
        line.split("=", 1)
        for line in content1.splitlines()
        if "=" in line and not line.strip().startswith("#")
    )
    key1 = lines["DG_SECRET_MASTER_KEY"].strip()
    # 验证生成的确实是合法的 Fernet 密钥
    fernet = Fernet(key1.encode("ascii"))
    assert fernet.decrypt(fernet.encrypt(b"dbguardian")) == b"dbguardian"

    # 第二次调用必须幂等保留原密钥，不得覆盖已有密钥导致历史密文无法解密
    ensure_env_and_master_key(tmp_path)
    content2 = env_path.read_text(encoding="utf-8")
    assert f"DG_SECRET_MASTER_KEY={key1}" in content2


def test_build_browser_app_command_uses_isolated_app_mode(tmp_path: Path) -> None:
    cmd = build_browser_app_command(
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "http://127.0.0.1:8000/console/",
        profile_dir=tmp_path / "profile",
        width=1500,
        height=940,
    )
    assert cmd[0].endswith("msedge.exe")
    assert "--app=http://127.0.0.1:8000/console/" in cmd
    assert "--window-size=1500,940" in cmd
    assert any(arg.startswith("--user-data-dir=") for arg in cmd)


def test_pick_port_returns_valid_tcp_port() -> None:
    port, running = pick_port("127.0.0.1", preferred=0)
    assert isinstance(port, int) and port > 0
    assert running is False


def test_cli_prepare_only_exits_zero() -> None:
    assert main(["--prepare-only"]) == 0


def test_save_local_byok_config_encrypts_api_key_without_plaintext(tmp_path: Path) -> None:
    raw_secret = "sk-live-secret-99887766"
    res = save_local_byok_config(
        provider="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        api_key=raw_secret,
        repo_root=tmp_path,
    )
    assert res["status"] == "ok"
    assert res["provider"] == "deepseek"
    cfg_file = Path(res["config_path"])
    assert cfg_file.is_file()
    disk_content = cfg_file.read_text(encoding="utf-8")
    assert raw_secret not in disk_content
    assert "api_key_ciphertext" in disk_content


def test_run_local_diagnosis_redacts_pii_and_returns_hub(tmp_path: Path) -> None:
    res = run_local_diagnosis(
        "订单库 pg_locks 锁等待超时，联系人手机 13812345678",
        repo_root=tmp_path,
    )
    assert res["status"] == "ok"
    assert "13812345678" not in res["redacted_symptom"]
    assert res["hub"] in {"#07", "#08", "#04"}
    assert res["confidence"] > 0.8


def test_eval_summary_respects_same_direction_verdict_and_golden_export(tmp_path: Path) -> None:
    exported = run_desktop_golden_export(repo_root=tmp_path)
    assert exported["status"] == "ok"
    assert exported["kept"] == 3
    assert exported["leakage_count"] == 0
    out_dir = Path(exported["output_dir"])
    samples_text = (out_dir / "samples.jsonl").read_text(encoding="utf-8")
    assert "13812345678" not in samples_text
    assert (out_dir / "manifest.json").is_file()

    summary_ok = get_eval_and_data_summary(repo_root=tmp_path, conflict_mode=False)
    assert summary_ok["verdict"] == "CANDIDATE_BETTER"
    assert summary_ok["golden_kept"] == 3

    summary_conflict = get_eval_and_data_summary(repo_root=tmp_path, conflict_mode=True)
    assert summary_conflict["verdict"] == "INCONCLUSIVE"
    assert summary_conflict["reason"] == "MIXED_DIRECTION"


# ---------------------------------------------------------------------------
# V4.1：桌面端真实仓库扫描与护栏基准评测入口
# ---------------------------------------------------------------------------
def _vulnerable_repo(root: Path) -> Path:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "orders.py").write_text(
        'def get_order(cur, order_id):\n'
        '    cur.execute("SELECT * FROM orders WHERE id = " + order_id)\n'
        '    return cur.fetchone()\n\n'
        'DB_PASSWORD = "desktop-fixture-pass-7788"\n',
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("requests==2.19.0\n", encoding="utf-8")
    return root


def test_scan_repo_returns_masked_summary_and_sarif(tmp_path: Path) -> None:
    repo = _vulnerable_repo(tmp_path / "target")
    payload = scan_local_repo(repo, repo_root=tmp_path)

    assert payload["status"] == "ok"
    assert payload["finding_count"] >= 3
    assert payload["highest_priority"] == "P0"
    assert Path(payload["sarif_path"]).is_file()
    # 返回的是 public_view：不含 evidence 字段，也不得出现凭据明文
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "desktop-fixture-pass-7788" not in rendered
    assert all("evidence" not in item for item in payload["findings"])


def test_run_guardrail_eval_persists_live_report(tmp_path: Path) -> None:
    payload = run_guardrail_eval(repo_root=tmp_path)

    assert payload["status"] == "ok"
    assert payload["verdict"] == "CANDIDATE_BETTER"
    assert payload["data_source"] == "LIVE_EVAL"
    assert payload["leakage"]["candidate"] == 0
    report_path = Path(payload["report_path"])
    assert report_path.is_file()

    stored = json.loads(report_path.read_text(encoding="utf-8"))
    assert stored["suite_version"]
    assert stored["golden"]["leakage_count"] == 0

    summary = get_eval_and_data_summary(repo_root=tmp_path)
    assert summary["data_source"] == "LIVE_EVAL"
    assert summary["verdict"] == "CANDIDATE_BETTER"
    assert summary["guardrail_eval"]["leakage_candidate"] == 0


def test_cli_scan_repo_and_guardrail_eval_emit_json(tmp_path: Path, capfd) -> None:
    repo = _vulnerable_repo(tmp_path / "target")

    assert main(["--scan-repo", str(repo)]) == 0
    scan_json = json.loads(capfd.readouterr().out.strip())
    assert scan_json["status"] == "ok"
    assert scan_json["finding_count"] >= 1

    assert main(["--run-guardrail-eval"]) == 0
    eval_json = json.loads(capfd.readouterr().out.strip())
    assert eval_json["status"] == "ok"
    assert eval_json["verdict"] in {"CANDIDATE_BETTER", "INCONCLUSIVE", "BASELINE_BETTER"}


