"""W09 T05 模型降级：状态机、占位网关、版本钉与降级控制器。

设计依据：``docs/w09/02-design.md`` §6、``docs/w09/03-task-cards.md:518-605``。

- ``provider == "disabled"`` 时返回 ``DisabledModelGateway``（所有调用抛
  ``ModelUnavailableError``），**不再**让装配期抛 ``ConfigError``（A9）。
- ``AVAILABLE`` / ``SUSPENDED`` 两态；``SUSPENDED`` 时 ``planning_suspended`` 为真，
  **只允许**基于既有证据生成规则化简报，禁止新规划与工具选择。
- 非法输出最多 1 次修复性重试（复用 ``MAX_REPAIR_RETRIES_LIMIT``），第 2 次即停；
  事件顺序 ``AGENT_RECOVERY_STARTED`` → ``AGENT_RECOVERY_STOPPED`` →
  ``MODEL_DEGRADED``（设计 §11 R7）。
- 质量回归只在存在**已验证的冻结版本**时路由；当前清单 ``model_commit`` 为空，
  因此 ``reason=QUALITY_REGRESSION_NO_VERIFIED_VERSION`` 且不指定任何模型版本。
- 本模块不持有工具注册表、网关或数据库凭据；所有事件落点由显式注入的审计端口完成。
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from core.audit.events import AuditEventType, make_event
from core.errors import ModelUnavailableError, RequestInvalidError
from core.recovery.controller import MAX_REPAIR_RETRIES_LIMIT


class DegradeReason(str, Enum):
    MODEL_DISABLED = "MODEL_DISABLED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    QUALITY_REGRESSION = "QUALITY_REGRESSION"
    QUALITY_REGRESSION_NO_VERIFIED_VERSION = "QUALITY_REGRESSION_NO_VERIFIED_VERSION"


class ModelState(str, Enum):
    AVAILABLE = "AVAILABLE"
    SUSPENDED = "SUSPENDED"


def _unavailable(operation: str) -> ModelUnavailableError:
    return ModelUnavailableError(
        "模型服务不可用（provider=disabled），已降级为规则简报",
        details={"provider": "disabled", "reason": "MODEL_DISABLED", "operation": operation},
    )


class DisabledModelGateway:
    """``provider=disabled`` 时的占位网关：不装配适配器，所有调用抛领域错误。"""

    provider = "disabled"
    adapter_version = "disabled"
    model_name = "disabled"

    def __init__(self) -> None:
        self._usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    @property
    def adapter(self) -> None:
        return None

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    def reset_usage(self) -> None:
        self._usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise _unavailable("generate")

    def generate_structured(self, *args: Any, **kwargs: Any) -> Any:
        raise _unavailable("generate_structured")

    def tool_call(self, *args: Any, **kwargs: Any) -> Any:
        raise _unavailable("tool_call")


class ModelVersionPin:
    """读取冻结清单，给出「前一已验证版本」。

    只认 ``model_commit`` 非空且 ``status`` 不以 ``BLOCKED`` 开头的版本；
    当前 ``baseline_manifest.json`` 的 ``model_commit=null``、
    ``status=BLOCKED_MODEL_ENV_MISSING``，因此恒返回 ``None``——不得用
    ``model_name`` 顶替版本号。
    """

    def __init__(self, *, manifest_path: Path, current_version: str) -> None:
        self._manifest_path = Path(manifest_path)
        self._current_version = current_version

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def previous_verified(self) -> str | None:
        try:
            manifest = json.loads(
                self._manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if not isinstance(manifest, dict):
            return None
        commit = manifest.get("model_commit")
        if not isinstance(commit, str) or not commit.strip():
            return None
        status = str(manifest.get("status") or "").strip().upper()
        if status.startswith("BLOCKED"):
            return None
        if self._current_version and commit.strip() == self._current_version.strip():
            return None
        return commit.strip()


class DegradationController:
    """模型可用性状态机；只判定与发事件，不规划、不调用工具。"""

    def __init__(
        self,
        *,
        audit: Any,
        provider: str,
        model_version: str | None = None,
        pin: ModelVersionPin | None = None,
        max_repair_retries: int = MAX_REPAIR_RETRIES_LIMIT,
    ) -> None:
        retries = self._validate_retries(max_repair_retries)
        self._audit = audit
        self._provider = str(provider)
        self._model_version = model_version
        self._pin = pin
        self._max_repair_retries = retries
        self._retry_count = 0
        self._takeover_open = False
        self._stopped = False
        if self._provider == "disabled":
            self._state = ModelState.SUSPENDED
            self._last_reason: DegradeReason | None = DegradeReason.MODEL_DISABLED
        else:
            self._state = ModelState.AVAILABLE
            self._last_reason = None

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        audit: Any,
        pin: ModelVersionPin | None = None,
    ) -> "DegradationController":
        return cls(audit=audit, provider=str(settings.model_provider), pin=pin)

    # ---- 只读视图 --------------------------------------------------------
    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def planning_suspended(self) -> bool:
        return self._state is ModelState.SUSPENDED

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def last_reason(self) -> DegradeReason | None:
        return self._last_reason

    @property
    def max_repair_retries(self) -> int:
        return self._max_repair_retries

    # ---- 状态迁移 --------------------------------------------------------
    def record_failure(
        self, reason: DegradeReason, *, task_id: str, run_id: str
    ) -> None:
        """记录一次可用性/质量降级并落 ``MODEL_DEGRADED``。"""
        resolved = reason
        pin_target: str | None = None
        if reason is DegradeReason.QUALITY_REGRESSION:
            pin_target = (
                self._pin.previous_verified() if self._pin is not None else None
            )
            if pin_target is None:
                resolved = DegradeReason.QUALITY_REGRESSION_NO_VERIFIED_VERSION
        self._state = ModelState.SUSPENDED
        self._last_reason = resolved
        self._emit(
            AuditEventType.MODEL_DEGRADED,
            task_id=task_id,
            run_id=run_id,
            payload=self._degraded_payload(resolved, pin_target=pin_target),
        )

    def repair_attempt(self, attempt: int, *, task_id: str, run_id: str) -> str:
        """非法输出的修复性重试：第 1 次 ``RETRY``，第 2 次 ``STOP``（无第 3 次）。"""
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise RequestInvalidError(
                "attempt 必须是 >=1 的整数", details={"attempt": attempt}
            )
        if attempt == 1 and self._retry_count < self._max_repair_retries:
            self._retry_count += 1
            self._begin_recovery(task_id=task_id, run_id=run_id)
            return "RETRY"

        if not self._stopped:
            self._stopped = True
            self._state = ModelState.SUSPENDED
            self._last_reason = DegradeReason.MODEL_OUTPUT_INVALID
            self._begin_recovery(task_id=task_id, run_id=run_id)
            self._emit(
                AuditEventType.AGENT_RECOVERY_STOPPED,
                task_id=task_id,
                run_id=run_id,
                payload={
                    "outcome": "STOP_INVALID_OUTPUT",
                    "reason": DegradeReason.MODEL_OUTPUT_INVALID.value,
                    "attempt": attempt,
                    "retry_count": self._retry_count,
                    "explanation": (
                        "模型输出连续非法，唯一一次修复性重试已用尽（上限 "
                        f"{self._max_repair_retries}），停止规划并降级为规则简报。"
                    ),
                    "plan_version": 0,
                    "tool_calls_delta": 0,
                },
            )
            self._emit(
                AuditEventType.MODEL_DEGRADED,
                task_id=task_id,
                run_id=run_id,
                payload=self._degraded_payload(
                    DegradeReason.MODEL_OUTPUT_INVALID, pin_target=None
                ),
            )
        return "STOP"

    def resume(self, verified_version: str | None) -> bool:
        """只有「非空且经钉校验的已验证版本」才能恢复；否则保持暂停。"""
        if self._state is ModelState.AVAILABLE:
            return True
        if not verified_version or not verified_version.strip():
            return False
        if self._pin is not None:
            if self._pin.previous_verified() != verified_version.strip():
                return False
        self._state = ModelState.AVAILABLE
        self._last_reason = None
        self._retry_count = 0
        self._takeover_open = False
        self._stopped = False
        return True

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _validate_retries(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RequestInvalidError(
                "max_repair_retries 必须是整数", details={"max_repair_retries": value}
            )
        if not 0 <= value <= MAX_REPAIR_RETRIES_LIMIT:
            raise RequestInvalidError(
                "非法输出最多只允许一次修复性重试",
                details={
                    "max_repair_retries": value,
                    "limit": MAX_REPAIR_RETRIES_LIMIT,
                },
            )
        return value

    def _degraded_payload(
        self, reason: DegradeReason, *, pin_target: str | None
    ) -> dict[str, Any]:
        return {
            "reason": reason.value,
            "planning_suspended": self.planning_suspended,
            "model_used": False,
            "retry_count": self._retry_count,
            "model_version": self._model_version,
            "pin_target": pin_target,
        }

    def _begin_recovery(self, *, task_id: str, run_id: str) -> None:
        if self._takeover_open:
            return
        self._takeover_open = True
        self._emit(
            AuditEventType.AGENT_RECOVERY_STARTED,
            task_id=task_id,
            run_id=run_id,
            payload={
                "trigger": DegradeReason.MODEL_OUTPUT_INVALID.value,
                "call_key": None,
                "plan_version": 0,
                "evidence_count": 0,
                "tool_calls_delta": 0,
            },
        )

    def _emit(
        self,
        event_type: AuditEventType,
        *,
        task_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        event = make_event(
            event_type, task_id=task_id, run_id=run_id, payload=payload
        )
        self._audit.emit_required(event, actor_user_id=None)


__all__ = [
    "DegradationController",
    "DegradeReason",
    "DisabledModelGateway",
    "ModelState",
    "ModelVersionPin",
]
