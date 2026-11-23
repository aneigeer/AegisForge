"""W09 T01 规则集（R01→R12），仅用标准库 `re`，禁止 `eval`/第三方 `regex`。

规则表顺序固定；同一字符串叶子内按 R01→R12 各扫一遍、不回溯。
`SENSITIVE_KEY_MARKERS` 与 `core/audit/events.py::_SENSITIVE_MARKERS` 同值，
但**不导入**旧模块的私有常量、更不修改它——避免改变 W03–W08 存量脱敏行为。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from core.redaction.models import SensitiveCategory

# 规则集版本：
# - `RS_REDACTION_V2` 是**当前默认**（`DataRedactionEngine` 的 `ruleset_version` 缺省值，
#   即 R05 拆分后的「A 组精确 + B 组 13 项子串」词表口径）；
# - `RS_REDACTION_V1` 的值**不得改动、不得删除**，仅供历史数据库行与既有审计口径
#   引用/复现（显式传 `ruleset_version=RS_REDACTION_V1` 即可复现历史行）；
# - 增补 `NAME_EXACT_KEYS` / `NAME_KEY_MARKERS` 任一组须继续升版（下一次即 v3），
#   不得原地改词表而不升版。
RS_REDACTION_V1 = "redaction-v1"
RS_REDACTION_V2 = "redaction-v2"

MAX_REDACTION_DEPTH = 12
MAX_REDACTION_ITEMS = 5_000
MAX_SCAN_CHARS = 200_000

SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "dsn",
    "authorization",
    "apikey",
    "credential",
)
# R05 键名判定（`docs/w09/05-rulings.md` R1 **v2** 书面裁定，两组均为闭集）：
# 裸 "name" 若留在子串词表，`ToolResult.tool_name` / `TargetObjectSpec.name_field` /
# `table_name` 等元数据键会被误判为人名，T02 会把每份工具结果送进隔离区。
# - A 组（精确）：归一化后**完全相等**才命中，唯一成员 `name`；
# - B 组（子串）：13 项完整 marker，**不含裸 "name"**；显式补齐 5 个人名 marker
#   （user_name/customer_name/display_name/account_name/full_name）避免欠脱敏。
# v2 已撤回 v1 拟设的 SYSTEM_METADATA_KEYS 白名单（见裁定 :26）：精确匹配本身
# 即让 15 个 ToolResult 顶层字段与 name_field 全部为 False，无需再维护一个需
# 长期同步的元数据闭集。此处是**纯键名函数**：不读值、无启发式 → AC-02 天然确定。
# 版本口径：v2 词表即当前默认 `RS_REDACTION_V2`；`RS_REDACTION_V1` 仅保留给历史行。
# 增补任一组须升 `redaction_version`（下一次即 v3）、改裁定文档并补回归断言。
NAME_EXACT_KEYS: frozenset[str] = frozenset({"name"})
NAME_KEY_MARKERS: tuple[str, ...] = (
    "姓名",
    "联系人",
    "商户号",
    "owner_name",
    "real_name",
    "order_no",
    "biz_id",
    "customer_id",
    "user_name",
    "customer_name",
    "display_name",
    "account_name",
    "full_name",
)

BLOCKER_BINARY_LEAF = "BINARY_LEAF"
BLOCKER_UNKNOWN_ENCODING = "UNKNOWN_ENCODING"
BLOCKER_SCAN_SIZE_EXCEEDED = "SCAN_SIZE_EXCEEDED"
BLOCKER_DEPTH_EXCEEDED = "DEPTH_EXCEEDED"

REASON_ID_CHECKSUM_INVALID = "ID_CHECKSUM_INVALID"
REASON_SEMANTIC_CONFIRMATION = "SEMANTIC_CONFIRMATION_REQUIRED"
REASON_UNREPLACEABLE_CONNECTION_STRING = "UNREPLACEABLE_CONNECTION_STRING"
REASON_UNREPLACEABLE_KEY_MATERIAL = "UNREPLACEABLE_KEY_MATERIAL"
REASON_UNREPLACEABLE_CERTIFICATE = "UNREPLACEABLE_CERTIFICATE"
REASON_BINARY_LEAF = "BINARY_LEAF"

# ---- R01 SQL 字面量 ----------------------------------------------------
SQL_SINGLE_RE = re.compile(r"'((?:[^'\\]|\\.|'')*)'")
SQL_DOUBLE_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NUMERIC_BODY_RE = re.compile(r"-?\d+(?:\.\d+)?")
_SQL_EXCLUDED_WORDS = frozenset({"NULL", "TRUE", "FALSE", "NONE"})

# ---- R02 手机号 --------------------------------------------------------
PHONE_PLAIN_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PHONE_SEPARATED_RE = re.compile(r"(?<!\d)1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)")

# ---- R03 邮箱 ----------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# ---- R04 身份证（GB 11643 mod 11-2） -----------------------------------
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"

# ---- R05 人名/业务号 ---------------------------------------------------
BIZ_ID_RE = re.compile(r"^[A-Z]{2,6}\d{6,}$")

# ---- R06 连接串 --------------------------------------------------------
CONNECTION_STRING_RE = re.compile(
    r"\b(?:mysql|mariadb|postgres(?:ql)?)(?:\+\w+)?://[^\s\"'<>]+",
    re.IGNORECASE,
)

# ---- R07 用户名 + 口令（同一叶子内同时出现） ---------------------------
USER_ASSIGN_RE = re.compile(
    r"\b(user(?:name)?|uid|account|登录名)\s*[:=]\s*([^\s,;&}]+)", re.IGNORECASE
)
PASSWORD_ASSIGN_RE = re.compile(
    r"\b(password|passwd|pwd)\s*[:=]\s*([^\s,;&}]+)", re.IGNORECASE
)
PASSWORD_PRESENT_RE = re.compile(
    r"\b(?:password|passwd|pwd)\s*[:=]", re.IGNORECASE
)

# ---- R08 Token ---------------------------------------------------------
BEARER_TOKEN_RE = re.compile(r"\b(bearer)\s+([A-Za-z0-9._\-]{8,})", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")
TOKEN_ASSIGN_RE = re.compile(
    r"\b(token|session[_\-]?id)\s*[:=]\s*([A-Za-z0-9._\-]{8,})", re.IGNORECASE
)

# ---- R09 API Key -------------------------------------------------------
API_KEY_ASSIGN_RE = re.compile(
    r"\b(api[_\-]?key|access[_\-]?key|secret[_\-]?key|appkey|x-api-key)"
    r"\s*[:=]\s*([A-Za-z0-9/+_\-]{8,})",
    re.IGNORECASE,
)
AKIA_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# ---- R10 / R11 不可安全改写的密钥材料 ----------------------------------
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
CERTIFICATE_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----"
)

RULE_ORDER: tuple[str, ...] = (
    "R01", "R02", "R03", "R04", "R05", "R06",
    "R07", "R08", "R09", "R10", "R11", "R12",
)
RULE_CATEGORY: dict[str, SensitiveCategory] = {
    "R01": SensitiveCategory.SQL_LITERAL,
    "R02": SensitiveCategory.PHONE,
    "R03": SensitiveCategory.EMAIL,
    "R04": SensitiveCategory.ID_CARD,
    "R05": SensitiveCategory.PERSON_OR_BUSINESS_ID,
    "R06": SensitiveCategory.CONNECTION_STRING,
    "R07": SensitiveCategory.USERNAME_PASSWORD,
    "R08": SensitiveCategory.TOKEN,
    "R09": SensitiveCategory.API_KEY,
    "R10": SensitiveCategory.PRIVATE_KEY,
    "R11": SensitiveCategory.CERTIFICATE,
    "R12": SensitiveCategory.LOG_FIELD,
}


def sha8(value: str) -> str:
    """替换标记里的 8 位相关性摘要（单向，短值由调用方判断）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def normalize_key(key: object) -> str:
    """键名归一化：小写、仅保留字母数字（中文字符 `isalnum()` 为真，予以保留）。"""
    return "".join(char for char in str(key).lower() if char.isalnum())


# 词表本身也要归一化：设计里的 `customer_id` / `order_no` / `owner_name` 含下划线，
# 而键名归一化后会去掉下划线（如 `Customer-ID` → `customerid`），不归一化词表将永远匹配不上。
_SENSITIVE_NORMALIZED = tuple(normalize_key(item) for item in SENSITIVE_KEY_MARKERS)
_EXACT_NORMALIZED = frozenset(normalize_key(item) for item in NAME_EXACT_KEYS)
_NAME_NORMALIZED = tuple(normalize_key(item) for item in NAME_KEY_MARKERS)


def is_sensitive_key(key: object) -> bool:
    """R12 判定：与 `core/audit/events.py::_is_sensitive_key` 同款子串法。"""
    normalized = normalize_key(key)
    return any(marker in normalized for marker in _SENSITIVE_NORMALIZED)


def is_name_key(key: object) -> bool:
    """R05 键名判定（裁定 R1 v2）：确定性纯键名函数，不读值、无启发式。

    判定顺序固定：① 精确键名（仅 `name`）→ True；② 13 项完整 marker 归一化子串。
    裸 `name` 只出现在 ①，绝不作子串匹配；`tool_name` / `name_field` /
    `table_name` 等元数据键归一化后既不等于 `name`，也不含任何完整 marker，
    因此不误判。返回 True 仍会由 `DataRedactionEngine` 记 REVIEW_REQUIRED span。
    """
    normalized = normalize_key(key)
    if normalized in _EXACT_NORMALIZED:
        return True
    return any(marker in normalized for marker in _NAME_NORMALIZED)


def is_sql_excluded(body: str) -> bool:
    """R01 排除：短值 / 纯数字 / NULL/TRUE/FALSE，避免把 SQL 结构误判为敏感字面量。"""
    if len(body) < 8:
        return True
    if _NUMERIC_BODY_RE.fullmatch(body.strip()):
        return True
    return body.strip().upper() in _SQL_EXCLUDED_WORDS


def id_card_checksum_valid(value: str) -> bool:
    """GB 11643 mod 11-2 校验位；非 18 位或含非法字符返回 False。"""
    upper = value.upper()
    if len(upper) != 18 or not upper[:17].isdigit():
        return False
    total = sum(int(char) * weight for char, weight in zip(upper[:17], _ID_WEIGHTS))
    return _ID_CHECK_CHARS[total % 11] == upper[17]


def short_value_hashed(value: str) -> bool:
    """仅对 >=8 字符的值做 8 位摘要，短值不哈希（避免字典穷举）。"""
    return len(value) >= 8


def is_binary_leaf(value: Any) -> bool:
    return isinstance(value, (bytes, bytearray, memoryview))


__all__ = [
    "AKIA_RE",
    "API_KEY_ASSIGN_RE",
    "BEARER_TOKEN_RE",
    "BIZ_ID_RE",
    "BLOCKER_BINARY_LEAF",
    "BLOCKER_DEPTH_EXCEEDED",
    "BLOCKER_SCAN_SIZE_EXCEEDED",
    "BLOCKER_UNKNOWN_ENCODING",
    "CERTIFICATE_RE",
    "CONNECTION_STRING_RE",
    "EMAIL_RE",
    "ID_CARD_RE",
    "JWT_RE",
    "MAX_REDACTION_DEPTH",
    "MAX_REDACTION_ITEMS",
    "MAX_SCAN_CHARS",
    "NAME_EXACT_KEYS",
    "NAME_KEY_MARKERS",
    "PASSWORD_ASSIGN_RE",
    "PASSWORD_PRESENT_RE",
    "PHONE_PLAIN_RE",
    "PHONE_SEPARATED_RE",
    "PRIVATE_KEY_RE",
    "REASON_BINARY_LEAF",
    "REASON_ID_CHECKSUM_INVALID",
    "REASON_SEMANTIC_CONFIRMATION",
    "REASON_UNREPLACEABLE_CERTIFICATE",
    "REASON_UNREPLACEABLE_CONNECTION_STRING",
    "REASON_UNREPLACEABLE_KEY_MATERIAL",
    "RS_REDACTION_V1",
    "RS_REDACTION_V2",
    "RULE_CATEGORY",
    "RULE_ORDER",
    "SENSITIVE_KEY_MARKERS",
    "SQL_DOUBLE_RE",
    "SQL_SINGLE_RE",
    "TOKEN_ASSIGN_RE",
    "USER_ASSIGN_RE",
    "id_card_checksum_valid",
    "is_binary_leaf",
    "is_name_key",
    "is_sensitive_key",
    "is_sql_excluded",
    "normalize_key",
    "sha8",
    "short_value_hashed",
]
