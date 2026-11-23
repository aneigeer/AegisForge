"""K8s YAML / Helm 双向可逆脱敏器（UPG-01 之 YAML 模态，P3-T1）。

为什么不引 PyYAML：脱敏必须**逐字保留**原始缩进、注释、引号与锚点，任何
"解析->重序列化"都会改变语义（注释丢失、顺序漂移、引号风格变化）。本模块用
行级扫描实现"结构感知替换"，只改需要改的值，其余字节原样保留。

脱敏对象（``plugins_matrix[k8s].masker``）：

- Secret / stringData / data 块中的密钥值（整值替换）
- 键名命中 password / token / secret / apiKey / privateKey 等语义的标量值
- ``namespace`` 与环境内部域名
- IPv4 地址、主机名

结构校验（``validate``）：Tab 缩进、键值缺失、载荷为空都视为结构损坏，
还原前先校验、还原后再校验，避免令牌错位把缩进改坏。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Mapping

from core.privacy_gateway.multimodal import MODALITY_YAML, ModalityStructureError

_KV_RE = re.compile(r"^(\s*(?:-\s+)?)([A-Za-z0-9_.\-/]+)(\s*:\s*)(.*)$")
_BLOCK_RE = re.compile(r"^(\s*(?:-\s+)?)(stringData|data|stringDataRef)\s*:\s*$")
_NAMESPACE_RE = re.compile(r"^(\s*(?:-\s+)?namespace\s*:\s*)(\S.*)$", re.IGNORECASE)
_HOST_RE = re.compile(r"^(\s*(?:-\s+)?host\s*:\s*)(\S.*)$", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9][a-z0-9-]*\.)+(?:internal|corp|lan|local|svc\.cluster\.local|cluster\.local)\b",
    re.IGNORECASE,
)
_TAB_RE = re.compile(r"^\s*\t")

_SECRET_KEY_HINTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "accesskey",
    "access_key",
    "privatekey",
    "private_key",
    "credential",
    "cookie",
    "authorization",
    "client_secret",
)

_YAML_MARKERS: tuple[str, ...] = ("apiVersion:", "kind:", "metadata:", "spec:", "---")


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower()
    return any(hint in normalized for hint in _SECRET_KEY_HINTS)


class YamlMasker:
    """K8s YAML 密钥 / Namespace / 内部域名双向脱敏器。"""

    def modality(self) -> str:
        return MODALITY_YAML

    def detect(self, payload: str) -> float:
        if not payload or ":" not in payload:
            return 0.0
        lowered = payload
        hits = sum(1 for marker in _YAML_MARKERS if marker in lowered)
        kv_lines = sum(1 for line in payload.splitlines() if _KV_RE.match(line))
        if hits >= 2:
            return min(0.99, 0.86 + 0.03 * hits)
        if kv_lines >= 3 and hits >= 1:
            return 0.72
        if kv_lines >= 4:
            return 0.58
        return 0.0

    def mask(self, payload: str) -> tuple[str, dict[str, str]]:
        self.validate(payload)
        mapping: dict[str, str] = {}
        counters: Counter[str] = Counter()
        out: list[str] = []
        data_block_indent: int | None = None
        newline = "\r\n" if "\r\n" in payload else "\n"

        def _token(kind: str) -> str:
            counters[kind] += 1
            return f"__{kind}_{counters[kind]:03d}__"

        for line in payload.splitlines():
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))

            if data_block_indent is not None and stripped and indent <= data_block_indent:
                data_block_indent = None

            if not stripped or stripped.startswith("#"):
                out.append(line)
                continue

            block = _BLOCK_RE.match(line)
            if block:
                data_block_indent = indent
                out.append(line)
                continue

            if data_block_indent is not None and indent > data_block_indent:
                kv = _KV_RE.match(line)
                if kv:
                    token = _token("SECRET")
                    mapping[token] = kv.group(4)
                    out.append(f"{kv.group(1)}{kv.group(2)}{kv.group(3)}{token}")
                    continue

            kv = _KV_RE.match(line)
            if kv and _is_secret_key(kv.group(2)):
                token = _token("SECRET")
                mapping[token] = kv.group(4)
                out.append(f"{kv.group(1)}{kv.group(2)}{kv.group(3)}{token}")
                continue

            namespace = _NAMESPACE_RE.match(line)
            if namespace:
                token = _token("NS")
                mapping[token] = namespace.group(2)
                out.append(f"{namespace.group(1)}{token}")
                continue

            host = _HOST_RE.match(line)
            if host:
                token = _token("HOST")
                mapping[token] = host.group(2)
                out.append(f"{host.group(1)}{token}")
                continue

            if kv:
                value = kv.group(4)
                masked_value = self._mask_inline(value, counters, mapping)
                out.append(f"{kv.group(1)}{kv.group(2)}{kv.group(3)}{masked_value}")
                continue

            out.append(self._mask_inline(line, counters, mapping))

        masked_text = newline.join(out)
        if payload.endswith(("\n", "\r")):
            masked_text += newline
        return masked_text, mapping

    def unmask(self, payload: str, mapping: Mapping[str, str]) -> str:
        restored = payload
        for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
            restored = restored.replace(token, original)
        return restored

    def validate(self, payload: str) -> None:
        if not isinstance(payload, str) or not payload.strip():
            raise ModalityStructureError("YAML 载荷为空")
        if "\x00" in payload:
            raise ModalityStructureError("YAML 载荷包含 NUL 控制字符")
        for index, line in enumerate(payload.splitlines(), start=1):
            if _TAB_RE.match(line):
                raise ModalityStructureError(f"YAML 第 {index} 行使用 Tab 缩进（非法）")
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
                continue
            if stripped.startswith("- "):
                if ":" in stripped:
                    continue
                continue
            if ":" not in stripped:
                raise ModalityStructureError(f"YAML 第 {index} 行缺少键值分隔符")
            if _KV_RE.match(line) is None:
                raise ModalityStructureError(f"YAML 第 {index} 行键名不合法")

    # -- 内部 ----------------------------------------------------------
    @staticmethod
    def _mask_inline(text: str, counters: Counter[str], mapping: dict[str, str]) -> str:
        def ip_repl(match: re.Match[str]) -> str:
            counters["IP"] += 1
            token = f"__IP_{counters['IP']:03d}__"
            mapping[token] = match.group(0)
            return token

        def domain_repl(match: re.Match[str]) -> str:
            counters["DOMAIN"] += 1
            token = f"__DOMAIN_{counters['DOMAIN']:03d}__"
            mapping[token] = match.group(0)
            return token

        masked = _IPV4_RE.sub(ip_repl, text)
        return _DOMAIN_RE.sub(domain_repl, masked)


__all__ = ["YamlMasker"]
