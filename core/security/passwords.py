"""本地账号密码哈希（scrypt，仅标准库）。

格式：``scrypt$16384$8$1$<salt_b64>$<digest_b64>``。

约束：
- 只使用 hashlib.scrypt，固定 n=2**14、r=8、p=1、dklen=32，盐为 16 字节随机值；
- 校验使用 hmac.compare_digest；
- 存储格式非法、缺失或参数不符时返回 False，不抛异常，并执行一次 dummy 校验以减小
  用户不存在/哈希损坏时的时序差异；
- 明文密码绝不落库、不写日志。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from typing import Final

SCHEME: Final = "scrypt"
SCRYPT_N: Final = 2**14
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
SALT_BYTES: Final = 16
KEY_BYTES: Final = 32

# 固定 dummy 凭据：仅用于统一失败路径的 scrypt 计算耗时。
_DUMMY_SALT = b"\x00" * SALT_BYTES
_DUMMY_DIGEST = b"\x00" * KEY_BYTES


def _b64_encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


_DUMMY_HASH = (
    f"{SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
    f"${_b64_encode(_DUMMY_SALT)}${_b64_encode(_DUMMY_DIGEST)}"
)


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )


def _parse(password_hash: str | None) -> tuple[bytes, bytes] | None:
    """严格解析存储格式；任何偏差返回 None。"""
    if not isinstance(password_hash, str):
        return None
    parts = password_hash.split("$")
    if len(parts) != 6:
        return None
    scheme, n_text, r_text, p_text, salt_text, digest_text = parts
    if scheme != SCHEME:
        return None
    try:
        n = int(n_text)
        r = int(r_text)
        p = int(p_text)
    except (TypeError, ValueError):
        return None
    if (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
        return None
    try:
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        digest = base64.b64decode(digest_text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if len(salt) != SALT_BYTES or len(digest) != KEY_BYTES:
        return None
    return salt, digest


def _dummy_verify(password: str) -> None:
    """执行一次与真实校验等价的 scrypt 计算；结果固定为不匹配。"""
    try:
        candidate = _derive(password, _DUMMY_SALT)
    except (TypeError, ValueError, UnicodeEncodeError):
        return
    hmac.compare_digest(candidate, _DUMMY_DIGEST)


def hash_password(password: str) -> str:
    """生成随机盐的 scrypt 哈希串；调用方负责不记录明文。"""
    if not isinstance(password, str):
        raise TypeError("password 必须是字符串")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt)
    return (
        f"{SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
        f"${_b64_encode(salt)}${_b64_encode(digest)}"
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    """恒定时间校验密码；格式非法/缺失一律 False，不抛异常。"""
    if not isinstance(password, str):
        _dummy_verify("")
        return False
    parsed = _parse(password_hash)
    if parsed is None:
        _dummy_verify(password)
        return False
    salt, expected = parsed
    try:
        actual = _derive(password, salt)
    except (TypeError, ValueError, UnicodeEncodeError):
        _dummy_verify(password)
        return False
    return hmac.compare_digest(actual, expected)


__all__ = [
    "KEY_BYTES",
    "SALT_BYTES",
    "SCHEME",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "hash_password",
    "verify_password",
]
