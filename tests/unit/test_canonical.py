"""ADR-0005 冻结向量的逐字节复现与反向量拒绝（W06 D1）。

6 组正向向量必须同时校验"规范串"与"摘要"——只校验摘要在规范化实现改变但
恰好自洽时会漏检；8 条反向量必须抛 `CanonicalizationError`（绝不降级）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.errors import ErrorCode
from core.security.canonical import (
    MAX_SAFE_INTEGER,
    CanonicalizationError,
    assert_canonical_supported,
    canonical_digest,
    canonical_json,
    is_canonical_supported,
    normalize_timestamp,
    sha256_hex,
)

# 冻结向量：(编号, 输入, 规范串, SHA-256)。逐字节比对，不允许"格式等价"。
FROZEN_VECTORS = [
    (
        "V1",
        {"b": 1, "a": 2, "c": "x"},
        '{"a":2,"b":1,"c":"x"}',
        "38b5d9a6be58441e40ba736c50ec9ac004336e9818b647f78fabfd53c64cd302",
    ),
    (
        "V2",
        {"target_objects": ["t1", "t2"], "meta": {"z": None, "a": True}},
        '{"meta":{"a":true,"z":null},"target_objects":["t1","t2"]}',
        "8f975e8f28effd0f00d336b5792cb8cb01a599141d4da2f630250db2c63ba0f9",
    ),
    (
        "V3",
        {"note": '中文"引号"\n换行\\反斜杠', "tab": "a\tb", "ctrl": "\u0001"},
        '{"ctrl":"\\u0001","note":"中文\\"引号\\"\\n换行\\\\反斜杠","tab":"a\\tb"}',
        "5749ea9be5c94fa197da09e996a67e12a9e7e5db1fc720521112e98bce2bc0bc",
    ),
    (
        "V4",
        {"max": 9007199254740991, "min": -9007199254740991, "zero": 0},
        '{"max":9007199254740991,"min":-9007199254740991,"zero":0}',
        "b7b2401ddca2165824e98c61890c0aaec470258d3119dd265d02be9438bf47e6",
    ),
    (
        "V5",
        {"\uff3a": 1, "\U0001f600": 2},
        '{"\U0001f600":2,"\uff3a":1}',
        "d0044d5452bc6cacd54894da533ae405d6d3d8063f9bc4ba7546a6d84faafc8f",
    ),
    (
        "V6",
        {"a": {"c": [1, {"e": [], "d": None}], "b": "x"}, "A": 0},
        '{"A":0,"a":{"b":"x","c":[1,{"d":null,"e":[]}]}}',
        "a0a5f63d039042c56ec693c51152ba4676dd17162b6a6f06d447d6b84b0a7b61",
    ),
]


@pytest.mark.parametrize("label,value,expected_text,expected_digest", FROZEN_VECTORS)
def test_frozen_vector_is_byte_exact(
    label: str, value: object, expected_text: str, expected_digest: str
) -> None:
    text = canonical_json(value)
    assert text == expected_text, f"{label} 规范串不一致"
    assert sha256_hex(text) == expected_digest, f"{label} 摘要不一致"
    assert canonical_digest(value) == expected_digest


def test_key_order_does_not_change_digest() -> None:
    """V1 的等价输入必须得到同一摘要（顺序无关性）。"""
    expected = "38b5d9a6be58441e40ba736c50ec9ac004336e9818b647f78fabfd53c64cd302"
    assert canonical_digest({"c": "x", "a": 2, "b": 1}) == expected
    assert canonical_digest({"a": 2, "c": "x", "b": 1}) == expected


def test_v5_locks_utf16_code_unit_order() -> None:
    """补充平面字符（UTF-16 代理对）必须排在 U+FF3A 之前——码点序会得到相反结果。"""
    text = canonical_json({"\uff3a": 1, "\U0001f600": 2})
    assert text.index("\U0001f600") < text.index("\uff3a")
    # 若误用码点序，摘要会是另一个值。
    codepoint_order = '{"\uff3a":1,"\U0001f600":2}'
    assert sha256_hex(codepoint_order) != canonical_digest({"\uff3a": 1, "\U0001f600": 2})


def test_no_whitespace_in_output() -> None:
    text = canonical_json({"a": [1, 2], "b": {"c": 3}})
    assert " " not in text
    assert "\n" not in text
    assert text == '{"a":[1,2],"b":{"c":3}}'


def test_non_ascii_is_not_escaped() -> None:
    assert canonical_json({"名称": "订单"}) == '{"名称":"订单"}'
    assert canonical_json("中文") == '"中文"'


def test_max_safe_integer_boundary_is_accepted() -> None:
    assert canonical_json(MAX_SAFE_INTEGER) == "9007199254740991"
    assert canonical_json(-MAX_SAFE_INTEGER) == "-9007199254740991"


def test_boolean_is_not_treated_as_int() -> None:
    assert canonical_json({"flag": True}) == '{"flag":true}'
    assert canonical_json({"flag": False}) == '{"flag":false}'


# 8 条反向量：必须拒绝，且不得降级为字符串。
REJECTED_VALUES = [
    ("float", {"x": 1.5}),
    ("nan", {"x": float("nan")}),
    ("decimal", {"x": Decimal("1.0")}),
    ("bytes", {"x": b"a"}),
    ("set", {"x": {1, 2}}),
    ("non_str_key", {1: "a"}),
    ("lone_surrogate", {"x": "\ud800"}),
    ("int_overflow", {"x": 9007199254740992}),
]


@pytest.mark.parametrize("label,value", REJECTED_VALUES)
def test_rejected_values_raise_canonicalization_error(label: str, value: object) -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_json(value)
    assert excinfo.value.code is ErrorCode.REQUEST_INVALID
    assert excinfo.value.status_code == 400
    assert label  # 参数化标签仅用于失败时的可读性
    assert is_canonical_supported(value) is False


def test_nested_unsupported_value_is_rejected() -> None:
    """深层嵌套的非法值同样必须被拒绝（不能只检查顶层）。"""
    with pytest.raises(CanonicalizationError):
        canonical_json({"a": [{"b": [1, {"c": 1.5}]}]})


def test_datetime_is_rejected_and_timestamp_normalizer_is_required() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 678_901, tzinfo=timezone.utc)
    with pytest.raises(CanonicalizationError):
        canonical_json({"at": moment})
    normalized = normalize_timestamp(moment)
    assert normalized == "2026-01-02T03:04:05.678Z"
    assert canonical_json({"at": normalized}) == '{"at":"2026-01-02T03:04:05.678Z"}'


def test_normalize_timestamp_is_ms_precision_utc_z() -> None:
    moment = datetime(2026, 11, 9, 12, 0, 0, 999_999, tzinfo=timezone.utc)
    assert normalize_timestamp(moment) == "2026-11-09T12:00:00.999Z"
    # 非 UTC 时区必须换算到 UTC，且必须保留毫秒位（固定三位）。
    from datetime import timedelta

    tz8 = timezone(timedelta(hours=8))
    local = datetime(2026, 11, 9, 20, 0, 0, 0, tzinfo=tz8)
    assert normalize_timestamp(local) == "2026-11-09T12:00:00.000Z"


def test_normalize_timestamp_rejects_non_datetime() -> None:
    with pytest.raises(CanonicalizationError):
        normalize_timestamp("2026-11-09T12:00:00Z")  # type: ignore[arg-type]


def test_assert_canonical_supported_raises_for_float() -> None:
    assert_canonical_supported({"a": 1})
    with pytest.raises(CanonicalizationError):
        assert_canonical_supported({"a": 1.0})


def test_sha256_hex_rejects_non_str() -> None:
    with pytest.raises(CanonicalizationError):
        sha256_hex(b"abc")  # type: ignore[arg-type]


def test_error_details_do_not_leak_values() -> None:
    """错误详情只暴露路径与类型，不回显原值（避免把敏感内容写进日志）。"""
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_json({"password": 1.5})
    assert excinfo.value.details["path"] == "password"
    assert excinfo.value.details["value_type"] == "float"
    assert 1.5 not in excinfo.value.details.values()
