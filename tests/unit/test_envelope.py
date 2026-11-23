"""W05 单元测试：执行信封摘要、签名、篡改检测与有效期校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.errors import EnvelopeInvalidError
from core.gateway.envelope import (
    build_envelope,
    canonical_envelope_payload,
    compute_args_digest,
    sign_envelope,
    verify_envelope,
    verify_envelope_signature,
)
from core.security.models import ExecutionEnvelope

_KEY = b"unit-test-gateway-key"
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _build(*, now: datetime | None = _NOW, ttl_s: float = 60, nonce: str | None = None) -> ExecutionEnvelope:
    return build_envelope(
        task_id="task_unit",
        step_id="step_1",
        worker_id="worker_a",
        lease_id="lease_unit",
        fencing_token=1,
        state_version=3,
        correlation_id="corr_unit",
        actor_user_id="user_local",
        tool_name="stub_read",
        tool_version="1.0",
        environment_id="env_mysql_local",
        params={"limit": 10, "table": "t1"},
        nonce=nonce,
        ttl_s=ttl_s,
        now=now,
    )


def test_args_digest_is_order_independent() -> None:
    first = {"a": 1, "b": {"y": 2, "x": [1, 2]}, "c": "中文"}
    second = {"c": "中文", "b": {"x": [1, 2], "y": 2}, "a": 1}

    assert compute_args_digest(first) == compute_args_digest(second)
    assert compute_args_digest(first) != compute_args_digest({"a": 1})
    assert len(compute_args_digest(first)) == 64


def test_canonical_payload_normalizes_datetime_timezone() -> None:
    aware = _build()
    naive = aware.model_copy(
        update={
            "issued_at": aware.issued_at.replace(tzinfo=None),
            "expires_at": aware.expires_at.replace(tzinfo=None),
        }
    )

    assert canonical_envelope_payload(aware) == canonical_envelope_payload(naive)


def test_signed_envelope_verifies() -> None:
    envelope = sign_envelope(_build(), _KEY)

    assert envelope.signature
    assert len(envelope.signature) == 64
    assert verify_envelope_signature(envelope, _KEY) is True
    assert verify_envelope_signature(envelope, b"another-key") is False
    assert verify_envelope(envelope, _KEY, now=_NOW) is None


def test_verify_accepts_naive_datetime_envelope_at_same_instant() -> None:
    envelope = sign_envelope(_build(), _KEY)
    naive = envelope.model_copy(
        update={
            "issued_at": envelope.issued_at.replace(tzinfo=None),
            "expires_at": envelope.expires_at.replace(tzinfo=None),
        }
    )

    assert verify_envelope_signature(naive, _KEY) is True
    verify_envelope(naive, _KEY, now=_NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("envelope_id", "env_tampered"),
        ("task_id", "task_other"),
        ("step_id", "step_other"),
        ("worker_id", "worker_b"),
        ("lease_id", "lease_other"),
        ("fencing_token", 2),
        ("state_version", 4),
        ("correlation_id", "corr_other"),
        ("actor_user_id", "user_other"),
        ("tool_name", "other_tool"),
        ("tool_version", "2.0"),
        ("environment_id", "env_pg_local"),
        ("args_digest", "0" * 64),
        ("nonce", "tampered-nonce"),
        ("issued_at", _NOW + timedelta(seconds=5)),
        ("expires_at", _NOW + timedelta(seconds=120)),
        ("model_risk_hint", "R2"),
        ("signature", "f" * 64),
    ],
)
def test_tampering_any_field_breaks_signature(field: str, value: object) -> None:
    envelope = sign_envelope(_build(), _KEY)
    tampered = envelope.model_copy(update={field: value})

    assert verify_envelope_signature(tampered, _KEY) is False
    with pytest.raises(EnvelopeInvalidError):
        verify_envelope(tampered, _KEY, now=_NOW)


def test_expired_envelope_is_rejected() -> None:
    envelope = sign_envelope(_build(ttl_s=30), _KEY)

    with pytest.raises(EnvelopeInvalidError) as excinfo:
        verify_envelope(envelope, _KEY, now=_NOW + timedelta(seconds=31))
    assert "过期" in excinfo.value.message


def test_future_issued_at_is_rejected() -> None:
    future_now = _NOW + timedelta(seconds=120)
    envelope = sign_envelope(_build(now=future_now), _KEY)

    with pytest.raises(EnvelopeInvalidError) as excinfo:
        verify_envelope(envelope, _KEY, now=_NOW)
    assert "签发时间" in excinfo.value.message


def test_empty_nonce_is_rejected_even_with_valid_signature() -> None:
    envelope = _build()
    envelope = envelope.model_copy(update={"nonce": ""})
    signed = sign_envelope(envelope, _KEY)

    with pytest.raises(EnvelopeInvalidError) as excinfo:
        verify_envelope(signed, _KEY, now=_NOW)
    assert "nonce" in excinfo.value.message


def test_build_envelope_defaults() -> None:
    envelope = _build()

    assert envelope.envelope_id.startswith("env_")
    assert envelope.nonce
    assert envelope.signature == ""
    assert envelope.args_digest == compute_args_digest({"limit": 10, "table": "t1"})
    assert envelope.issued_at == _NOW
    assert envelope.expires_at == _NOW + timedelta(seconds=60)
