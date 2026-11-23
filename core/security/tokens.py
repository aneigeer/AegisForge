"""会话 Token 生成与摘要。

约束：
- raw token 使用 ``dg_`` 前缀 + ``secrets.token_urlsafe(32)``；
- 数据库只存 SHA-256 十六进制摘要（64 字符）；
- fingerprint 只保留 raw token 前 8 位，任何日志/异常不得输出完整 token；
- 空 token 直接拒绝。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

TOKEN_PREFIX: Final = "dg_"
_TOKEN_RANDOM_BYTES: Final = 32
TOKEN_HASH_LENGTH: Final = 64
FINGERPRINT_LENGTH: Final = 8


def _ensure_token(raw_token: str) -> str:
    if not isinstance(raw_token, str) or not raw_token.strip():
        raise ValueError("token 不能为空")
    return raw_token


def new_session_token() -> tuple[str, str]:
    """返回 ``(raw_token, token_hash)``；raw 只返回调用方一次，落库只存 hash。"""
    raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)}"
    return raw_token, hash_token(raw_token)


def hash_token(raw_token: str) -> str:
    token = _ensure_token(raw_token)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_fingerprint(raw_token: str) -> str:
    """只保留前 8 位，用于日志关联；绝不返回完整 token。"""
    token = _ensure_token(raw_token)
    return token[:FINGERPRINT_LENGTH]


__all__ = [
    "FINGERPRINT_LENGTH",
    "TOKEN_HASH_LENGTH",
    "TOKEN_PREFIX",
    "hash_token",
    "new_session_token",
    "token_fingerprint",
]
