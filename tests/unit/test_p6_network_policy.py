"""P6-T1 静态网络策略测试：锁住"当前过渡态"，防止进一步放宽。

背景：`POST /v1/agent/{task_id}/run` 目前在 **API 进程内**执行 AgentLoop，
因此 api 仍需要 lab_net。P6-T1 要求的"api 只保留 control_net"必须先把执行权
移交 Worker，否则隔离会直接打断诊断链路。

本测试的作用不是"证明隔离已完成"，而是：

1. 锁住各服务**不该**有的网络（模型服务永不进 lab_net；数据库只在各自网络）；
2. 断言 api 上的 `P6-T1-TODO` 标记存在——移除 lab_net 时必须同时删标记，
   避免"悄悄忘记"或"悄悄放宽"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _networks(compose: dict, service: str) -> set[str]:
    raw = compose["services"][service].get("networks") or []
    if isinstance(raw, dict):
        return set(raw)
    return set(raw)


def test_databases_are_isolated_to_their_own_networks(compose: dict) -> None:
    assert _networks(compose, "control_db") == {"control_net"}
    assert _networks(compose, "mysql_lab") == {"lab_net"}
    assert _networks(compose, "pg_lab") == {"lab_net"}


def test_model_service_never_joins_lab_net(compose: dict) -> None:
    assert "lab_net" not in _networks(compose, "model_service")


def test_worker_has_control_and_lab_only(compose: dict) -> None:
    assert _networks(compose, "worker") == {"control_net", "lab_net"}
    assert "model_net" not in _networks(compose, "worker"), "Worker 不得调用模型"


def test_api_transitional_lab_access_is_explicitly_marked(compose: dict) -> None:
    """api 仍连 lab_net 时必须带 TODO 标记；移除网络时也要移除标记。"""
    api_networks = _networks(compose, "api")
    text = COMPOSE.read_text(encoding="utf-8")
    has_marker = "P6-T1-TODO" in text
    if "lab_net" in api_networks:
        assert has_marker, "api 连 lab_net 却缺少 P6-T1-TODO 标记：请登记过渡原因"
    else:
        assert not has_marker, "api 已隔离，应删除 P6-T1-TODO 标记"
