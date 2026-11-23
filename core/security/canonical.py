"""规范化与摘要：RFC 8785 JCS 的受约束子集（W06 D1，ADR-0005）。

本模块是 W06 所有摘要（`action_digest`/`prerequisites_digest`/分通道摘要）的
**唯一计算入口**，任何其他位置不得再用 `json.dumps` + `hashlib` 自行拼摘要。

受约束子集（在被接受的值域内与 RFC 8785 逐字节一致，不宣称完整实现）：

====================  ==============================================
项                    规则
====================  ==============================================
对象键排序            按 **UTF-16 码元**升序（补充平面字符排在 U+FF3A 之前）
分隔符               ``,`` / ``:``，无任何空白
字符串转义            ``json.dumps(s, ensure_ascii=False)``（非 ASCII 原样输出）
整数                  十进制字面量，``|v| <= 2^53-1``，越界报错
浮点数                 一律拒绝（不实现 ECMAScript 最短往返表示）
``Decimal``           拒绝；调用方必须先转十进制字符串
``datetime``          拒绝；调用方必须先过 :func:`normalize_timestamp`
``bytes``/``set``     拒绝
非 ``str`` 键         拒绝
孤立代理项            拒绝（UTF-8 编码会失败）
====================  ==============================================

**不支持的类型一律抛 :class:`CanonicalizationError`，绝不降级为 `str(value)`。**
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from core.errors import DBGuardianError, ErrorCode

# ECMAScript 安全整数上界：2^53 - 1。
MAX_SAFE_INTEGER = 9_007_199_254_740_991

# 孤立代理项区间；含孤立代理项的字符串无法编码为 UTF-8。
_SURROGATE_START = 0xD800
_SURROGATE_END = 0xDFFF


class CanonicalizationError(DBGuardianError):
    """规范化失败：输入值域之外，属于请求非法（400），不得降级。"""

    default_status_code = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.REQUEST_INVALID, message, details=details)


def _fail(message: str, *, path: str, value_type: str) -> None:
    raise CanonicalizationError(
        message,
        details={"path": path, "value_type": value_type},
    )


def _check_string(text: str, *, path: str) -> None:
    """拒绝孤立代理项：JCS 要求字符串可编码为合法 UTF-8。"""
    for char in text:
        code = ord(char)
        if _SURROGATE_START <= code <= _SURROGATE_END:
            _fail("字符串包含孤立代理项，无法规范化", path=path, value_type="str")


def _quote(text: str, *, path: str) -> str:
    _check_string(text, path=path)
    # ensure_ascii=False：只转义 " \ 与 < U+0020 的控制字符，非 ASCII 原样输出。
    return quote_string(text)


def quote_string(text: str) -> str:
    """仅做 JSON 字符串转义（不校验代理项），供内部复用。"""
    return json.dumps(text, ensure_ascii=False)


def _key_bytes(key: str, *, path: str) -> bytes:
    _check_string(key, path=path)
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError:  # pragma: no cover - 已由 _check_string 拦截
        _fail("对象键无法按 UTF-16 码元排序", path=path, value_type="str")
        raise AssertionError("unreachable")


def _encode_int(value: int, *, path: str) -> str:
    if abs(value) > MAX_SAFE_INTEGER:
        _fail(
            "整数超出安全取值范围（|v| <= 2^53-1）",
            path=path,
            value_type="int",
        )
    return str(value)


def _encode(value: Any, *, path: str) -> str:
    # bool 必须先于 int 判断：Python 中 bool 是 int 的子类。
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _encode_int(value, path=path)
    if isinstance(value, str):
        return _quote(value, path=path)
    if isinstance(value, float):
        _fail(
            "拒绝浮点数：请改用十进制字符串或整数",
            path=path,
            value_type=type(value).__name__,
        )
    if isinstance(value, Decimal):
        _fail(
            "拒绝 Decimal：请先转换为十进制字符串",
            path=path,
            value_type="Decimal",
        )
    if isinstance(value, (datetime, date, time)):
        _fail(
            "拒绝日期时间对象：请先调用 normalize_timestamp 转为 ISO-8601 UTC 字符串",
            path=path,
            value_type=type(value).__name__,
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        _fail(
            "拒绝二进制数据：请先做 base64 或十六进制编码",
            path=path,
            value_type=type(value).__name__,
        )
    if isinstance(value, (set, frozenset)):
        _fail(
            "拒绝集合类型：集合无序，无法产生确定性摘要",
            path=path,
            value_type=type(value).__name__,
        )
    if isinstance(value, Mapping):
        return _encode_mapping(value, path=path)
    if isinstance(value, (list, tuple)):
        items = (_encode(item, path=f"{path}[{index}]") for index, item in enumerate(value))
        return "[" + ",".join(items) + "]"
    _fail(
        "不支持的值类型，拒绝降级为字符串",
        path=path,
        value_type=type(value).__name__,
    )
    raise AssertionError("unreachable")


def _encode_mapping(value: Mapping[Any, Any], *, path: str) -> str:
    prepared: list[tuple[bytes, str, str]] = []
    for index, (key, item) in enumerate(value.items()):
        if not isinstance(key, str):
            _fail(
                "对象键必须是字符串",
                path=path,
                value_type=type(key).__name__,
            )
        if key == "__proto__":  # pragma: no cover - 保留位，防止原型污染语义歧义
            _fail("对象键不合法", path=path, value_type="str")
        child_path = f"{path}.{key}" if path else key
        prepared.append((_key_bytes(key, path=child_path), key, _encode(item, path=child_path)))
    # 按 UTF-16 码元升序排序：不得使用 Python 默认的码点序（会与 V5 向量冲突）。
    prepared.sort(key=lambda item: item[0])
    return "{" + ",".join(f"{_quote(key, path=path)}:{encoded}" for _, key, encoded in prepared) + "}"


def canonical_json(value: Any) -> str:
    """返回 JCS 受约束子集下的规范字符串；不合法输入抛 `CanonicalizationError`。"""
    return _encode(value, path="")


def assert_canonical_supported(value: Any) -> None:
    """断言取值可被规范化（不返回字符串）；供 `normalize_tool_parameters` 收尾校验。"""
    canonical_json(value)


def sha256_hex(text: str) -> str:
    """对 UTF-8 字节取 SHA-256 十六进制摘要。"""
    if not isinstance(text, str):
        raise CanonicalizationError(
            "sha256_hex 只接受 str",
            details={"value_type": type(text).__name__},
        )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(value: Any) -> str:
    """规范化后取 SHA-256；这是 W06 全部摘要的唯一入口。"""
    return sha256_hex(canonical_json(value))


def normalize_timestamp(value: datetime) -> str:
    """把 datetime 归一化为固定格式 ISO-8601 UTC（毫秒精度，`Z` 结尾）。

    进程内时间必须 tz-aware UTC；naive 值按 UTC 解释（仅用于兼容历史数据读取），
    摘要中一律使用本函数输出，不接受客户端时间。
    """
    if not isinstance(value, datetime):
        raise CanonicalizationError(
            "normalize_timestamp 只接受 datetime",
            details={"value_type": type(value).__name__},
        )
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def is_canonical_supported(value: Any) -> bool:
    """返回值是否可规范化（不抛异常），供测试与诊断使用。"""
    try:
        canonical_json(value)
    except CanonicalizationError:
        return False
    return True


def sorted_by_canonical(items: Iterable[Any]) -> list[Any]:
    """按各自规范串的 UTF-8 字节序升序排序（`target_objects` 规范化用）。"""
    return sorted(items, key=lambda item: canonical_json(item).encode("utf-8"))


__all__ = [
    "CanonicalizationError",
    "MAX_SAFE_INTEGER",
    "assert_canonical_supported",
    "canonical_digest",
    "canonical_json",
    "is_canonical_supported",
    "normalize_timestamp",
    "quote_string",
    "sha256_hex",
    "sorted_by_canonical",
]
