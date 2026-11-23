"""Agent 轨迹：记录每一步的结构化事件，并以原子方式落盘。

轨迹中可能包含工具输出与模型决策，所有 payload 在记录前统一脱敏。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from core.audit.events import redact_payload
from core.redaction.engine import DataRedactionEngine
from core.redaction.gate import RedactionGate
from core.redaction.models import RedactionStatus
from core.tasks.models import new_id

# 轨迹是"三个内部通道"之一（`docs/w09/02-design.md:67`）：审计链路的
# `redact_payload` **保持不变**，只在落盘前**追加**一次引擎扫描。
# 扫描走 `RedactionGate.scan_event`，与七条对外通道共用同一套判定/事件/隔离实现。
_TRAJECTORY_GATE = RedactionGate(DataRedactionEngine())
# `BLOCKED` 时的固定占位：只说明"该事件含不可安全改写内容"，不带任何原值。
_BLOCKED_PAYLOAD: dict[str, Any] = {"redaction": "BLOCKED"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]+")


class TrajectoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    kind: str
    at: str
    payload: dict[str, Any]


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    task_id: str
    run_id: str
    goal: str
    model_version: str | None = None
    started_at: str
    finished_at: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    events: list[TrajectoryEvent] = Field(default_factory=list)


class TrajectoryRecorder:
    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        goal: str,
        model_version: str | None = None,
    ) -> None:
        self.trajectory_id = new_id("traj")
        self._task_id = task_id
        self._run_id = run_id
        self._goal = goal
        self._model_version = model_version
        self._started_at = _utc_now_iso()
        self._finished_at: str | None = None
        self._stop_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._events: list[TrajectoryEvent] = []

    def record(self, kind: str, payload: dict[str, Any]) -> TrajectoryEvent:
        """记录一步事件：先走既有审计脱敏，再追加 W09 引擎扫描。

        引擎扫描只做一件事：判定该事件是否含**不可安全改写**的内容
        （连接串 / 私钥 / 证书）。命中 `BLOCKED` 时整条事件的 payload 换成固定
        占位 `{"redaction": "BLOCKED"}`，原值一个字节都不落盘；其余状态保持
        `redact_payload` 的既有产物（审计链路口径不变，W03–W08 行为零漂移）。
        """
        cleaned = cast(dict[str, Any], redact_payload(payload))
        scan = _TRAJECTORY_GATE.scan_event(cleaned, task_id=self._task_id)
        if scan.status is RedactionStatus.BLOCKED:
            cleaned = dict(_BLOCKED_PAYLOAD)
        event = TrajectoryEvent(
            sequence=len(self._events),
            kind=kind,
            at=_utc_now_iso(),
            payload=cleaned,
        )
        self._events.append(event)
        return event

    def finish(self, *, stop_reason: str, usage: dict[str, Any]) -> Trajectory:
        self._finished_at = _utc_now_iso()
        self._stop_reason = stop_reason
        self._usage = cast(dict[str, Any], redact_payload(usage))
        return self.build()

    def build(self) -> Trajectory:
        return Trajectory(
            trajectory_id=self.trajectory_id,
            task_id=self._task_id,
            run_id=self._run_id,
            goal=self._goal,
            model_version=self._model_version,
            started_at=self._started_at,
            finished_at=self._finished_at,
            stop_reason=self._stop_reason,
            usage=dict(self._usage),
            events=list(self._events),
        )


class TrajectoryStore:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def save(self, trajectory: Trajectory) -> Path:
        for value in (trajectory.task_id, trajectory.trajectory_id):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"轨迹标识包含非法字符：{value!r}")
        target_dir = self._directory / trajectory.task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{trajectory.trajectory_id}.json"
        content = json.dumps(
            trajectory.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )

        handle_fd, temp_name = tempfile.mkstemp(
            dir=target_dir,
            prefix=f".{trajectory.trajectory_id}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temp_name, path)
        except BaseException:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise
        return path

    def load(self, path: str | Path) -> Trajectory:
        return Trajectory.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )


__all__ = ["Trajectory", "TrajectoryEvent", "TrajectoryRecorder", "TrajectoryStore"]
