"""P4-T1 单元测试：OIDC JWKS/RS256 校验与"授权只认 DB"。

覆盖验收：

- 篡改签名 / `alg=none` / 过期 / 受众不符 / 未映射主体 → 一律 fail closed；
- 令牌里自带 `roles: ["Administrator"]` **不生效**：Principal.roles 只来自 memberships；
- 多租户且令牌未指明 tenant → 拒绝（不猜）。

测试用确定性 RSA 密钥与注入的 JWKS fetcher，不触网、不依赖真实 IdP。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.auth.oidc import JwksCache, OIDCService, verify_jwt
from core.errors import AuthInvalidError
from core.security.models import Principal
from core.security.roles import Role

ISSUER = "https://idp.example.com"
AUDIENCE = "dbguardian"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"
KID = "test-key-1"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_NUMBERS = _PRIVATE_KEY.public_key().public_numbers()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwks() -> dict[str, Any]:
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(_PUBLIC_NUMBERS.n.to_bytes((_PUBLIC_NUMBERS.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(_PUBLIC_NUMBERS.e.to_bytes(3, "big")),
            }
        ]
    }


def _token(claims: dict[str, Any], *, kid: str = KID, alg: str = "RS256") -> str:
    header = _b64url(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    if alg == "RS256":
        signature = _PRIVATE_KEY.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = b""
    return f"{header}.{payload}.{_b64url(signature)}"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "oidc|alice",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 300,
        # 刻意放一个"提权"声明：实现必须忽略它。
        "roles": ["Administrator"],
        "tenant": "tenant_local",
    }
    base.update(overrides)
    return base


class _Repo:
    def __init__(self, users: dict[tuple[str, str], dict[str, Any]], memberships: dict[str, dict[str, tuple[Role, ...]]]) -> None:
        self._users = users
        self._memberships = memberships

    def get_user_by_oidc(self, issuer: str, subject: str) -> dict[str, Any] | None:
        return self._users.get((issuer, subject))

    def memberships_by_tenant(self, user_id: str) -> dict[str, tuple[Role, ...]]:
        return self._memberships.get(user_id, {})


def _service(repository: _Repo) -> OIDCService:
    return OIDCService(
        repository=repository,
        jwks=JwksCache(fetcher=lambda uri: _jwks()),
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri=JWKS_URI,
    )


def _repo(*, roles: tuple[Role, ...] = (Role.VIEWER,), tenant: str = "tenant_local") -> _Repo:
    return _Repo(
        users={(ISSUER, "oidc|alice"): {"user_id": "user_alice", "display_name": "Alice"}},
        memberships={"user_alice": {tenant: roles}},
    )


def test_valid_token_yields_principal_with_db_roles_only() -> None:
    principal = _service(_repo(roles=(Role.VIEWER,))).authenticate(_token(_claims()))
    assert isinstance(principal, Principal)
    assert principal.user_id == "user_alice"
    assert principal.tenant_id == "tenant_local"
    assert principal.roles == (Role.VIEWER,), "令牌里的 roles 声明必须被忽略"


def test_tampered_payload_is_rejected() -> None:
    token = _token(_claims())
    header, payload, signature = token.split(".")
    tampered = _b64url(json.dumps(_claims(sub="oidc|mallory")).encode())
    broken = f"{header}.{tampered}.{signature}"
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(broken)
    assert excinfo.value.details["reason"] == "SIGNATURE_INVALID"


def test_alg_none_is_rejected() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(), alg="none"))
    assert excinfo.value.details["reason"] == "ALG_NOT_ALLOWED"


def test_unknown_kid_is_rejected() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(), kid="other-key"))
    assert excinfo.value.details["reason"] == "KID_NOT_FOUND"


def test_unmapped_subject_is_rejected() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(sub="oidc|nobody")))
    assert excinfo.value.details["reason"] == "SUBJECT_UNMAPPED"


def test_expired_token_is_rejected() -> None:
    past = int(time.time()) - 3600
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(exp=past, nbf=past - 10, iat=past - 10)))
    assert excinfo.value.details["reason"] == "TOKEN_EXPIRED"


def test_wrong_audience_is_rejected() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(aud="another-app")))
    assert excinfo.value.details["reason"] == "AUD_MISMATCH"


def test_multi_tenant_without_tenant_claim_is_ambiguous() -> None:
    repo = _Repo(
        users={(ISSUER, "oidc|alice"): {"user_id": "user_alice", "display_name": "Alice"}},
        memberships={"user_alice": {"t1": (Role.VIEWER,), "t2": (Role.OPERATOR,)}},
    )
    claims = _claims()
    claims.pop("tenant")
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(repo).authenticate(_token(claims))
    assert excinfo.value.details["reason"] == "TENANT_AMBIGUOUS"


def test_tenant_claim_must_be_a_real_membership() -> None:
    with pytest.raises(AuthInvalidError) as excinfo:
        _service(_repo()).authenticate(_token(_claims(tenant="tenant_other")))
    assert excinfo.value.details["reason"] == "TENANT_NOT_MEMBER"


def test_verify_jwt_returns_claims_for_good_token() -> None:
    claims = verify_jwt(
        _token(_claims()),
        jwks=JwksCache(fetcher=lambda uri: _jwks()),
        jwks_uri=JWKS_URI,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    assert claims["sub"] == "oidc|alice"
