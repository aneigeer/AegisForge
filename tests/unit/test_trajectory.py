"""W04 单元测试：轨迹记录、脱敏与原子落盘。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agent.trajectory import TrajectoryRecorder, TrajectoryStore
from core.tasks.models import new_id


def test_recorder_records_sequence_and_redacts() -> None:
    recorder = TrajectoryRecorder(
        task_id="task_1", run_id="run_1", goal="g", model_version="fake/fake/0.1"
    )
    first = recorder.record("model_call", {"purpose": "plan", "password": "secret-value"})
    second = recorder.record("tool_result", {"tool_name": "get_locks"})
    assert first.sequence == 0
    assert second.sequence == 1
    assert first.payload["password"] == "<redacted>"
    assert recorder.trajectory_id.startswith("traj_")


def test_finish_sets_stop_reason_and_usage() -> None:
    recorder = TrajectoryRecorder(task_id="t", run_id="r", goal="g")
    recorder.record("plan", {"steps": 2})
    trajectory = recorder.finish(stop_reason="planned_stop", usage={"tool_calls": 2})
    assert trajectory.stop_reason == "planned_stop"
    assert trajectory.usage == {"tool_calls": 2}
    assert trajectory.finished_at is not None
    assert len(trajectory.events) == 1


def test_store_save_and_load_roundtrip(tmp_path: Path) -> None:
    recorder = TrajectoryRecorder(task_id="task_roundtrip", run_id="run_1", goal="g")
    recorder.record("plan", {"steps": 1})
    trajectory = recorder.finish(stop_reason="completed", usage={"steps": 1})
    store = TrajectoryStore(tmp_path)
    path = store.save(trajectory)
    assert path.exists()
    assert path.parent.name == "task_roundtrip"
    loaded = store.load(path)
    assert loaded.trajectory_id == trajectory.trajectory_id
    assert loaded.stop_reason == "completed"
    assert loaded.events[0].payload == {"steps": 1}


def test_store_rejects_path_traversal(tmp_path: Path) -> None:
    recorder = TrajectoryRecorder(task_id="../evil", run_id="run_1", goal="g")
    trajectory = recorder.finish(stop_reason="completed", usage={})
    with pytest.raises(ValueError):
        TrajectoryStore(tmp_path).save(trajectory)


def test_generated_ids_are_safe(tmp_path: Path) -> None:
    task_id = new_id("task")
    recorder = TrajectoryRecorder(task_id=task_id, run_id="run_1", goal="g")
    trajectory = recorder.finish(stop_reason="completed", usage={})
    path = TrajectoryStore(tmp_path).save(trajectory)
    assert path.exists()
