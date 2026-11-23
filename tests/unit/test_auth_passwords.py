"""密码哈希单元测试（无数据库依赖）。"""

from __future__ import annotations

import base64

import pytest

from core.security.passwords import (
    KEY_BYTES,
    SALT_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    hash_password,
    verify_password,
)


def _parts(value: str) -> list[str]:
    return value.split("$")


def test_hash_roundtrip_and_format() -> None:
    password = "Correct-Horse-Battery-Staple"
    hashed = hash_password(password)
    parts = _parts(hashed)
    assert len(parts) == 6
    assert parts[0] == "scrypt"
    assert int(parts[1]) == SCRYPT_N == 16384
    assert int(parts[2]) == SCRYPT_R == 8
    assert int(parts[3]) == SCRYPT_P == 1
    assert len(base64.b64decode(parts[4], validate=True)) == SALT_BYTES == 16
    assert len(base64.b64decode(parts[5], validate=True)) == KEY_BYTES == 32
    assert password not in hashed
    assert verify_password(password, hashed) is True


def test_hash_uses_random_salt() -> None:
    password = "same-password"
    first = hash_password(password)
    second = hash_password(password)
    assert first != second
    assert _parts(first)[4] != _parts(second)[4]
    assert verify_password(password, first)
    assert verify_password(password, second)


def test_wrong_password_is_rejected() -> None:
    hashed = hash_password("right-password")
    assert verify_password("wrong-password", hashed) is False
    assert verify_password("", hashed) is False


def test_tampered_digest_is_rejected() -> None:
    hashed = hash_password("secret")
    parts = _parts(hashed)
    digest = parts[5]
    replacement = "B" if digest[0] != "B" else "C"
    parts[5] = replacement + digest[1:]
    tampered = "$".join(parts)
    assert verify_password("secret", tampered) is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        None,
        "",
        "plaintext",
        "md5$16384$8$1$AAAA$AAAA",
        "scrypt$16384$8$1$AAAA",
        "scrypt$16384$8$1$AAAA$AAAA",
        "scrypt$2$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "scrypt$16384$8$1$!!!!$AAAA",
        "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$%%%%",
    ],
)
def test_invalid_hash_format_returns_false(bad_hash: str | None) -> None:
    assert verify_password("secret", bad_hash) is False


def test_empty_password_can_be_hashed() -> None:
    hashed = hash_password("")
    assert verify_password("", hashed) is True
    assert verify_password("x", hashed) is False


def test_non_string_password_is_rejected() -> None:
    assert verify_password(None, hash_password("secret")) is False  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        hash_password(None)  # type: ignore[arg-type]
