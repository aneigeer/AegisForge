"""会话 Token 单元测试（无数据库依赖）。"""

from __future__ import annotations

import re

import pytest

from core.security.tokens import (
    FINGERPRINT_LENGTH,
    TOKEN_HASH_LENGTH,
    TOKEN_PREFIX,
    hash_token,
    new_session_token,
    token_fingerprint,
)

_RAW_PATTERN = re.compile(r"^dg_[A-Za-z0-9_-]{43}$")
_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def test_new_session_token_shape_and_hash() -> None:
    raw, digest = new_session_token()
    assert raw.startswith(TOKEN_PREFIX)
    assert _RAW_PATTERN.match(raw)
    assert _HEX_PATTERN.match(digest)
    assert len(digest) == TOKEN_HASH_LENGTH == 64
    assert digest != raw
    assert hash_token(raw) == digest


def test_tokens_are_unique() -> None:
    tokens = {new_session_token()[0] for _ in range(100)}
    assert len(tokens) == 100


def test_hash_token_is_deterministic() -> None:
    raw = "dg_" + "a" * 43
    assert hash_token(raw) == hash_token(raw)


def test_fingerprint_never_exposes_full_token() -> None:
    raw, _ = new_session_token()
    fingerprint = token_fingerprint(raw)
    assert len(fingerprint) == FINGERPRINT_LENGTH == 8
    assert fingerprint == raw[:8]
    assert fingerprint != raw
    assert raw not in fingerprint


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_token_is_rejected(bad: str | None) -> None:
    with pytest.raises(ValueError):
        hash_token(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        token_fingerprint(bad)  # type: ignore[arg-type]
