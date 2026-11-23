"""P4-T3：遥测专用的**二次清洗**（在 `redact_text` 之后再做结构性屏蔽）。

`redact_text` 负责 PII/凭据的通用规则；遥测还需要挡住两类**结构性**泄漏：

- 连接串（`mysql://user:pass@host/db`、`postgresql://...`）——整段替换；
- 常见密钥形态（`sk-...`、`ghp_...`、`AKIA...`）——整段替换。

这不是"再加一层正则就安全"的声明：它只是纵深防御的一层，真正的保证是
**调用方不把参数/返回值/DSN 放进标签或 span 属性**（避免路径本就不存在）。
"""

from __future__ import annotations

import re

from core.logging import redact_text

_DSN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"']+")
_KEY_RE = re.compile(
    r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{6,}|\bghp_[A-Za-z0-9]{10,}|\bAKIA[0-9A-Z]{12,}"
)
_SQL_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|TRUNCATE)\b[^\n;]*",
    re.IGNORECASE,
)


def scrub(value: object) -> str:
    """把任意值清成"可安全放进遥测"的短字符串。"""
    text = redact_text(str(value if value is not None else ""))
    text = _DSN_RE.sub("<dsn>", text)
    text = _KEY_RE.sub("<key>", text)
    text = _SQL_RE.sub("<sql>", text)
    return text.replace("\n", " ").replace('"', "'").strip()


__all__ = ["scrub"]
