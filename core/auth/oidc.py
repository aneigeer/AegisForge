"""P4-T1：OIDC JWT 校验（RS256）+ 外部主体到本地用户的映射。

安全立场（与 W05 一致）：

- **授权不来自令牌**：`roles` / `tenant` 若出现在 claim 里一律忽略；角色只从
  `dg_system.memberships` 读，租户必须是该用户真实成员关系之一。
- **fail closed**：`alg` 只接受 RS256（拒绝 `none`/HS*）、必须能按 `kid` 找到公钥、
  签名/`iss`/`aud`/`exp`/`nbf` 任一不符即拒绝，未映射的 `sub` 直接拒绝。
- **不缓存身份**：JWKS 有 TTL 缓存，但"主体→用户"每次查库，避免撤权后仍可访问。

未安装 PyJWT，因此用 `cryptography` 直接做 RS256 验签（PKCS#1 v1.5 + SHA-256）。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.errors import AuthInvalidError, ConfigError
from core.security.models import AuthMethod, Principal

#: 允许的算法（只 RS256；HS* 会把"公开的 JWKS"变成共享密钥，必须拒绝）。
ALLOWED_ALGORITHMS = frozenset({"RS256"})
#: 时钟偏移容忍（秒）。
DEFAULT_LEEWAY_S = 60
#: JWKS 缓存 TTL（秒）。
DEFAULT_JWKS_TTL_S = 300


class OIDCConfigError(ConfigError):
    """OIDC 配置缺失/不合法。"""


def _b64url_decode(segment: str) -> bytes:
    padding_needed = (-len(segment)) % 4
    return base64.urlsafe_b64decode(segment + "=" * padding_needed)


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


@dataclass
class JwksCache:
    """按 `kid` 缓存的 JWKS；`fetcher` 可注入（测试用确定性公钥集）。"""

    fetcher: Callable[[str], Mapping[str, Any]]
    ttl_s: int = DEFAULT_JWKS_TTL_S
    _keys: dict[str, Any] = field(default_factory=dict)
    _fetched_at: float = 0.0
    _uri: str = ""

    def key_for(self, uri: str, kid: str, *, now: float | None = None) -> Any:
        moment = time.monotonic() if now is None else now
        if uri != self._uri or moment - self._fetched_at > self.ttl_s or kid not in self._keys:
            self._load(uri, moment)
        key = self._keys.get(kid)
        if key is None:
            raise AuthInvalidError(
                "令牌 kid 不在 JWKS 中", details={"reason": "KID_NOT_FOUND", "kid": kid}
            )
        return key

    def _load(self, uri: str, moment: float) -> None:
        try:
            payload = self.fetcher(uri)
        except Exception as exc:  # noqa: BLE001 - 拉取失败即拒绝（不沿用过期缓存）
            raise AuthInvalidError(
                "无法获取 JWKS", details={"reason": "JWKS_UNAVAILABLE", "error": type(exc).__name__}
            ) from None
        keys: dict[str, Any] = {}
        for jwk in payload.get("keys", []) or []:
            if str(jwk.get("kty")) != "RSA" or not jwk.get("kid"):
                continue
            try:
                keys[str(jwk["kid"])] = rsa.RSAPublicNumbers(
                    _b64url_uint(str(jwk["e"])), _b64url_uint(str(jwk["n"]))
                ).public_key()
            except Exception:  # noqa: BLE001 - 单个坏 key 不影响其它 key
                continue
        self._keys = keys
        self._fetched_at = moment
        self._uri = uri


def _split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise AuthInvalidError("令牌格式非法", details={"reason": "TOKEN_MALFORMED"})
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
    except Exception:  # noqa: BLE001
        raise AuthInvalidError("令牌头部/载荷非法", details={"reason": "TOKEN_MALFORMED"}) from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise AuthInvalidError("令牌头部/载荷非法", details={"reason": "TOKEN_MALFORMED"})
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    return header, payload, signing_input


def verify_jwt(
    token: str,
    *,
    jwks: JwksCache,
    jwks_uri: str,
    issuer: str,
    audience: str,
    leeway_s: int = DEFAULT_LEEWAY_S,
    now: float | None = None,
) -> dict[str, Any]:
    """校验 RS256 JWT 并返回 claims；任一条件不满足即抛 `AuthInvalidError`。"""
    header, claims, signing_input = _split(token)
    algorithm = str(header.get("alg") or "")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise AuthInvalidError(
            "不支持的签名算法", details={"reason": "ALG_NOT_ALLOWED", "alg": algorithm}
        )
    kid = str(header.get("kid") or "")
    if not kid:
        raise AuthInvalidError("令牌缺少 kid", details={"reason": "KID_MISSING"})
    public_key = jwks.key_for(jwks_uri, kid, now=now)

    parts = token.split(".")
    try:
        signature = _b64url_decode(parts[2])
    except Exception:  # noqa: BLE001
        raise AuthInvalidError("令牌签名非法", details={"reason": "TOKEN_MALFORMED"}) from None
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise AuthInvalidError("令牌签名校验失败", details={"reason": "SIGNATURE_INVALID"}) from None

    moment = time.time() if now is None else now
    if str(claims.get("iss") or "") != issuer:
        raise AuthInvalidError(
            "令牌签发方不匹配", details={"reason": "ISS_MISMATCH", "expected": issuer}
        )
    audiences = claims.get("aud")
    aud_list = audiences if isinstance(audiences, list) else [audiences]
    if audience not in [str(item) for item in aud_list if item is not None]:
        raise AuthInvalidError("令牌受众不匹配", details={"reason": "AUD_MISMATCH"})
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or moment > float(exp) + leeway_s:
        raise AuthInvalidError("令牌已过期", details={"reason": "TOKEN_EXPIRED"})
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and moment + leeway_s < float(nbf):
        raise AuthInvalidError("令牌尚未生效", details={"reason": "TOKEN_NOT_YET_VALID"})
    if not str(claims.get("sub") or "").strip():
        raise AuthInvalidError("令牌缺少 sub", details={"reason": "SUB_MISSING"})
    return claims


@dataclass(frozen=True)
class OIDCService:
    """把已验证的令牌换成本地 Principal（授权只来自 DB）。"""

    repository: Any
    jwks: JwksCache
    issuer: str
    audience: str
    jwks_uri: str
    leeway_s: int = DEFAULT_LEEWAY_S

    def authenticate(self, token: str, *, now: float | None = None) -> Principal:
        claims = verify_jwt(
            token,
            jwks=self.jwks,
            jwks_uri=self.jwks_uri,
            issuer=self.issuer,
            audience=self.audience,
            leeway_s=self.leeway_s,
            now=now,
        )
        subject = str(claims["sub"]).strip()
        user = self.repository.get_user_by_oidc(self.issuer, subject)
        if user is None:
            raise AuthInvalidError(
                "外部主体未映射到本地用户",
                details={"reason": "SUBJECT_UNMAPPED", "issuer": self.issuer},
            )
        memberships = self.repository.memberships_by_tenant(user["user_id"])
        tenant = str(claims.get("tenant") or "").strip()
        if not tenant:
            tenant = self._default_tenant(memberships)
        roles = memberships.get(tenant)
        if not roles:
            raise AuthInvalidError(
                "该主体不属于请求的租户",
                details={"reason": "TENANT_NOT_MEMBER", "tenant_id": tenant},
            )
        return Principal(
            user_id=str(user["user_id"]),
            tenant_id=tenant,
            roles=tuple(roles),
            display_name=user.get("display_name"),
            auth_method=AuthMethod.TOKEN,
        )

    @staticmethod
    def _default_tenant(memberships: Mapping[str, Any]) -> str:
        if not memberships:
            raise AuthInvalidError(
                "主体没有任何租户成员关系", details={"reason": "NO_MEMBERSHIP"}
            )
        if len(memberships) == 1:
            return next(iter(memberships))
        raise AuthInvalidError(
            "主体属于多个租户，必须在令牌中指明 tenant",
            details={"reason": "TENANT_AMBIGUOUS", "tenants": sorted(memberships)},
        )


__all__ = [
    "ALLOWED_ALGORITHMS",
    "JwksCache",
    "OIDCConfigError",
    "OIDCService",
    "verify_jwt",
]
