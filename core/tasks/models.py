"""任务领域模型：状态、步骤、工具调用、事件与检查点（V2.2 W03）。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    OBSERVING = "OBSERVING"
    DIAGNOSING = "DIAGNOSING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

StepStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    risk_level: Literal["R0", "R1", "R2", "R3", "R4"] = "R0"
    cancel_requested: bool = False
    pause_requested: bool = False
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    # V2.2 W03：服务端归属与并发/预算字段
    tenant_id: str = "tenant_local"
    environment_id: str = "env_mysql_local"
    # W05：任务所有者；授权校验为 tenant_id + created_by 双重条件。
    created_by: str = "user_local"
    state_version: int = 1
    deadline_at: datetime | None = None
    max_steps: int = 8
    max_tool_calls: int = 10
    max_tokens: int = 20_000
    replan_count: int = 0


class TaskStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    task_id: str
    run_id: str
    step_index: int
    title: str
    tool_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = "PENDING"
    created_at: datetime
    updated_at: datetime


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    task_id: str
    run_id: str
    step_id: str | None = None
    tool_name: str
    idempotency_key: str | None = None
    status: str = "running"
    attempt: int = 1
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None


class TaskEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    task_id: str
    run_id: str
    tool_call_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    seq: int = 0
    previous_event_hash: str | None = None
    event_hash: str | None = None
    trace_id: str | None = None


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    task_id: str
    run_id: str
    sequence: int
    state: dict[str, Any]
    created_at: datetime
