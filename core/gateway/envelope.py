"""执行信封：参数摘要、规范化、HMAC 签名与时间/重放前置校验。

约束：
- 签名覆盖除 `signature` 外的全部字段；
- datetime 可能带/不带时区，规范化与比较统一按 UTC；
- 校验失败一律抛 `EnvelopeInvalidError`，且不泄漏完整 nonce。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from core.errors import EnvelopeInvalidError
from core.security.models import ExecutionEnvelope, RiskLevel
from core.tasks.models import new_id, utc_now

_FUTURE_TOLERANCE = timedelta(seconds=60)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_args_digest(params: dict[str, Any]) -> str:
    """对工具入参做稳定摘要；键顺序无关，UTF-8 原文参与哈希。"""
    text = json.dumps(
        params,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_envelope_payload(envelope: ExecutionEnvelope) -> bytes:
    """签名输入的规范字节串：UTC 时间统一为 ISO-8601。"""
    data: dict[str, Any] = dict(envelope.unsigned_payload())
    data["issued_at"] = _normalize_utc(envelope.issued_at).isoformat()
    data["expires_at"] = _normalize_utc(envelope.expires_at).isoformat()
    text = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return text.encode("utf-8")


def sign_envelope(envelope: ExecutionEnvelope, key: bytes) -> ExecutionEnvelope:
    """返回填好 HMAC-SHA256 签名的新信封（不改动入参）。"""
    signature = hmac.new(key, canonical_envelope_payload(envelope), hashlib.sha256).hexdigest()
    return envelope.model_copy(update={"signature": signature})


def verify_envelope_signature(envelope: ExecutionEnvelope, key: bytes) -> bool:
    expected = hmac.new(key, canonical_envelope_payload(envelope), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(envelope.signature or ""), expected)


def build_envelope(
    *,
    task_id: str,
    step_id: str | None,
    worker_id: str,
    lease_id: str,
    fencing_token: int,
    state_version: int,
    correlation_id: str,
    actor_user_id: str,
    tool_name: str,
    tool_version: str,
    environment_id: str,
    params: dict[str, Any],
    nonce: str | None = None,
    ttl_s: float,
    model_risk_hint: RiskLevel | None = None,
    approval_id: str | None = None,
    now: datetime | None = None,
) -> ExecutionEnvelope:
    """构造未签名信封；签名由调用方（Worker）使用服务端密钥完成。"""
    issued_at = _normalize_utc(now or utc_now())
    envelope = ExecutionEnvelope(
        envelope_id=new_id("env"),
        task_id=task_id,
        step_id=step_id,
        worker_id=worker_id,
        lease_id=lease_id,
        fencing_token=fencing_token,
        state_version=state_version,
        correlation_id=correlation_id,
        actor_user_id=actor_user_id,
        tool_name=tool_name,
        tool_version=tool_version,
        environment_id=environment_id,
        args_digest=compute_args_digest(params),
        nonce=nonce if nonce is not None else secrets.token_urlsafe(24),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_s),
        model_risk_hint=model_risk_hint,
        approval_id=approval_id,
        signature="",
    )
    return envelope


def verify_envelope(
    envelope: ExecutionEnvelope,
    key: bytes,
    *,
    now: datetime | None = None,
) -> None:
    """验签 + 有效期/时间窗口/nonce 校验；失败抛 EnvelopeInvalidError。"""
    if not verify_envelope_signature(envelope, key):
        raise EnvelopeInvalidError(
            "执行信封签名校验失败",
            details={"envelope_id": envelope.envelope_id, "task_id": envelope.task_id},
        )

    current = _normalize_utc(now or utc_now())
    issued_at = _normalize_utc(envelope.issued_at)
    expires_at = _normalize_utc(envelope.expires_at)

    if issued_at > current + _FUTURE_TOLERANCE:
        raise EnvelopeInvalidError(
            "执行信封签发时间超出允许时钟偏差",
            details={
                "envelope_id": envelope.envelope_id,
                "issued_at": issued_at.isoformat(),
            },
        )
    if expires_at <= current:
        raise EnvelopeInvalidError(
            "执行信封已过期",
            details={
                "envelope_id": envelope.envelope_id,
                "expires_at": expires_at.isoformat(),
            },
        )
    if issued_at >= expires_at:
        raise EnvelopeInvalidError(
            "执行信封有效期区间非法",
            details={"envelope_id": envelope.envelope_id},
        )
    if not str(envelope.nonce or "").strip():
        raise EnvelopeInvalidError(
            "执行信封缺少 nonce",
            details={"envelope_id": envelope.envelope_id},
        )


__all__ = [
    "build_envelope",
    "canonical_envelope_payload",
    "compute_args_digest",
    "sign_envelope",
    "verify_envelope",
    "verify_envelope_signature",
]
