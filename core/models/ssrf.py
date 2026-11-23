"""P1-BYOK-T2：`base_url` 的 SSRF 校验（INV-09）。

拦截面：

1. **协议**：生产默认只允许 `https`；`http` 仅在显式开启 `allow_http`（本地 dev）
   且 host 命中 allowlist 时放行；
2. **凭据**：URL 里不得出现 `user:pass@`（模型侧永远不接收凭据，INV-02）；
3. **主机**：命中受控主机名（`control_db` / `mysql_lab` / `pg_lab` …）直接拒绝；
4. **DNS**：先解析再判定，任一解析结果落在 loopback / link-local（含云元数据
   `169.254.169.254`）/ 私网 / IPv6 ULA 即拒绝——防 DNS rebinding 的关键是
   **在连接前按解析结果判定**，本模块同时被注册期与适配器连接前调用；
5. **端口**：数据库端口（3306/5432）默认禁止，除非 host 在 allowlist 内。

allowlist 语义：`DG_BYOK_ALLOWED_HOSTS` 里的主机跳过 3/4/5 的网络段判定，
但仍要求 https（除非 `allow_http` 也为真）。allowlist 是**运维显式声明**，
不是自动放行私网。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlsplit

from core.errors import SsrfBlockedError

#: 明确禁止的受控主机名（compose 服务名与常见内网别名）。
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "control_db",
        "control-mysql",
        "mysql_lab",
        "lab_mysql",
        "pg_lab",
        "postgres",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

#: 禁止的端口：数据库控制面/数据面。
BLOCKED_PORTS: frozenset[int] = frozenset({3306, 5432, 33060})

#: 禁止的网段（IPv4 + IPv6）。
BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

Resolver = Callable[[str, int], Sequence[str]]


def _default_resolver(host: str, port: int) -> Sequence[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addresses:
            addresses.append(str(sockaddr[0]))
    return tuple(addresses)


def _is_blocked_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - resolver 返回非法地址
        return True
    return any(parsed in network for network in BLOCKED_NETWORKS)


def validate_base_url(
    url: str,
    *,
    allowlist: Iterable[str] = (),
    allow_http: bool = False,
    resolver: Resolver | None = None,
) -> str:
    """校验并返回规范化后的 base_url；不通过即抛 `SsrfBlockedError`。"""
    raw = str(url or "").strip()
    if not raw:
        raise SsrfBlockedError("base_url 不能为空", details={"field": "base_url"})

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").strip().lower()
    if parts.username or parts.password:
        raise SsrfBlockedError(
            "base_url 不得内嵌用户名/密码",
            details={"field": "base_url", "reason": "USERINFO_NOT_ALLOWED"},
        )
    if not host:
        raise SsrfBlockedError(
            "base_url 缺少主机名", details={"field": "base_url", "reason": "HOST_MISSING"}
        )

    allowed_hosts = {str(item).strip().lower() for item in allowlist if str(item).strip()}
    allowlisted = host in allowed_hosts

    if scheme != "https":
        if scheme != "http" or not (allow_http and allowlisted):
            raise SsrfBlockedError(
                "base_url 必须使用 https（http 仅在显式开启且命中 allowlist 时允许）",
                details={"field": "base_url", "reason": "SCHEME_NOT_ALLOWED", "scheme": scheme},
            )

    if host in BLOCKED_HOSTNAMES and not allowlisted:
        raise SsrfBlockedError(
            "base_url 指向受控主机名，已阻断",
            details={"field": "base_url", "reason": "BLOCKED_HOSTNAME", "host": host},
        )

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise SsrfBlockedError(
            "base_url 端口非法", details={"field": "base_url", "reason": "PORT_INVALID"}
        ) from None

    if port in BLOCKED_PORTS and not allowlisted:
        raise SsrfBlockedError(
            "base_url 指向数据库端口，已阻断",
            details={"field": "base_url", "reason": "BLOCKED_PORT", "port": port},
        )

    if allowlisted:
        return raw

    # 主机本身就是 IP 字面量时**直接判定**，不能交给解析器（否则可被伪造解析绕过）。
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(str(literal)):
            raise SsrfBlockedError(
                "base_url 指向内网/环回/链路本地地址，已阻断",
                details={
                    "field": "base_url",
                    "reason": "BLOCKED_ADDRESS",
                    "host": host,
                    "blocked_count": 1,
                },
            )
        return raw

    resolve = resolver or _default_resolver
    try:
        addresses = tuple(resolve(host, port))
    except OSError as exc:
        raise SsrfBlockedError(
            "base_url 主机无法解析",
            details={"field": "base_url", "reason": "DNS_FAILED", "host": host,
                     "error": type(exc).__name__},
        ) from None
    if not addresses:
        raise SsrfBlockedError(
            "base_url 主机无解析结果",
            details={"field": "base_url", "reason": "DNS_EMPTY", "host": host},
        )
    blocked = [address for address in addresses if _is_blocked_address(address)]
    if blocked:
        raise SsrfBlockedError(
            "base_url 解析到内网/环回/链路本地地址，已阻断",
            details={
                "field": "base_url",
                "reason": "BLOCKED_ADDRESS",
                "host": host,
                "blocked_count": len(blocked),
            },
        )
    return raw


__all__ = [
    "BLOCKED_HOSTNAMES",
    "BLOCKED_NETWORKS",
    "BLOCKED_PORTS",
    "validate_base_url",
]
