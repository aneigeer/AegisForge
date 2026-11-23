"""W09 T01 脱敏引擎单测：AC-01…AC-04（全离线，不连库、不依赖 DG_ADMIN_DSN/PG）。"""

from __future__ import annotations

import inspect
import json
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from core.audit import events as audit_events
from core.redaction import rules as core_redaction_rules
from core.errors import (
    ErrorCode,
    RedactionBlockedError,
    RedactionReviewRequiredError,
)
from core.tools.contracts import ToolResult, ToolSource, ToolStatus
from core.redaction import (
    DataRedactionEngine,
    RedactionChannel,
    RedactionResult,
    RedactionStatus,
    SensitiveCategory,
    canonical_payload_hash,
    canonical_payload_text,
)
from core.redaction.rules import (
    NAME_EXACT_KEYS,
    NAME_KEY_MARKERS,
    RS_REDACTION_V1,
    RS_REDACTION_V2,
    SENSITIVE_KEY_MARKERS,
    is_name_key,
)

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _valid_id_card() -> str:
    body = "11010519491231002"
    total = sum(int(char) * weight for char, weight in zip(body, _ID_WEIGHTS))
    return body + _ID_CHECK_CHARS[total % 11]


VALID_ID = _valid_id_card()

# 12 类样本：key + 原始敏感串。断言「逐类命中 + 原串不得出现在脱敏后载荷」。
SAMPLES: dict[str, tuple[str, str]] = {
    "SQL_LITERAL": ("q", "SELECT note FROM t WHERE note = 'abcdefghijklmnop'"),
    "PHONE": ("p", "13812345678"),
    "EMAIL": ("e", "alice@example.com"),
    "ID_CARD": ("id", VALID_ID),
    "PERSON_OR_BUSINESS_ID": ("姓名", "张三"),
    "CONNECTION_STRING": ("target", "mysql://user:pass@host.internal:3306/db"),
    "USERNAME_PASSWORD": ("creds", "user=admin password=hunter2xyz"),
    "TOKEN": ("auth", "Bearer abcdefghijklmnop1234"),
    "API_KEY": ("k", "api_key=ABCDEFGH12345678"),
    "PRIVATE_KEY": (
        "pem",
        "-----BEGIN RSA PRIVATE KEY-----MIIBOgIBAAJBAK-----END RSA PRIVATE KEY-----",
    ),
    "CERTIFICATE": (
        "cert",
        "-----BEGIN CERTIFICATE-----MIIBkTCB-----END CERTIFICATE-----",
    ),
    "LOG_FIELD": ("password", "hunter2secretvalue"),
}


def _engine() -> DataRedactionEngine:
    return DataRedactionEngine()


def _serialized(result: RedactionResult) -> str:
    return json.dumps(result.payload_redacted, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# AC-01：12 类逐类命中，且脱敏后不含原串
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category_name", sorted(SAMPLES))
def test_ac01_each_of_12_categories_is_detected_and_original_is_removed(
    category_name: str,
) -> None:
    key, original = SAMPLES[category_name]
    result = _engine().redact({key: original}, channel=RedactionChannel.TOOL_RESULT)

    assert SensitiveCategory(category_name) in result.detected_categories
    assert original not in _serialized(result), f"{category_name} 原串残留"
    assert result.status is not RedactionStatus.CLEAN
    assert result.redaction_version == RS_REDACTION_V2


def test_ac01_all_12_categories_are_present_in_one_scan() -> None:
    payload = {key: value for key, value in SAMPLES.values()}
    result = _engine().redact(payload, channel=RedactionChannel.EXPORT)

    assert set(result.detected_categories) == set(SensitiveCategory)
    assert result.status is RedactionStatus.BLOCKED  # 含 R06/R10/R11


def test_ac01_detected_categories_follow_enum_declaration_order() -> None:
    payload = {"e": "alice@example.com", "p": "13812345678", "id": VALID_ID}
    result = _engine().redact(payload, channel=RedactionChannel.REPORT)

    assert result.detected_categories == tuple(
        item for item in SensitiveCategory if item in result.detected_categories
    )
    assert result.detected_categories == (
        SensitiveCategory.PHONE,
        SensitiveCategory.EMAIL,
        SensitiveCategory.ID_CARD,
    )


def test_ac01_blockers_are_orthogonal_to_categories() -> None:
    result = _engine().redact(
        {"blob": b"binary-secret"}, channel=RedactionChannel.EVIDENCE
    )

    assert result.blockers == ("BINARY_LEAF",)
    assert all("BINARY" not in item.value for item in result.detected_categories)


@pytest.mark.parametrize(
    "key", ["order_no", "Customer-ID", "biz_id", "owner_name", "商户号"]
)
def test_ac01_r05_key_markers_are_normalized_before_matching(key: str) -> None:
    """设计词表含下划线/中文，键名归一化后仍须命中（同一归一化规则）。"""
    result = _engine().redact({key: "AB12345678"}, channel=RedactionChannel.EVIDENCE)
    assert SensitiveCategory.PERSON_OR_BUSINESS_ID in result.detected_categories
    assert result.status is RedactionStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("key", ["Db-Password", "CLIENT_SECRET", "mysql_dsn"])
def test_ac01_r12_key_markers_cover_normalized_variants(key: str) -> None:
    """R12 与 W03–W08 存量 `_is_sensitive_key` 同款：覆盖大小写/连字符/前缀变体。"""
    result = _engine().redact({key: "original-secret"}, channel=RedactionChannel.EVIDENCE)
    assert SensitiveCategory.LOG_FIELD in result.detected_categories
    assert "original-secret" not in _serialized(result)


# ---------------------------------------------------------------------------
# AC-02：确定性（5 轮三个集合各 1）
# ---------------------------------------------------------------------------


def test_ac02_five_runs_have_stable_count_version_and_hash() -> None:
    payload = {
        "rows": [
            {"phone": "13812345678", "email": "alice@example.com"},
            {"note": "plain text", "duration_ms": 1.25},
        ],
        "z": "last",
        "a": "first",
    }
    engine = _engine()
    results = [
        engine.redact(payload, channel=RedactionChannel.TOOL_RESULT)
        for _ in range(5)
    ]

    assert len({item.redaction_count for item in results}) == 1
    assert len({item.redaction_version for item in results}) == 1
    assert len({item.payload_hash for item in results}) == 1
    # 同一版本内确定性（R4-A）：默认版本已升到 v2，5 轮不得漂移。
    assert {item.redaction_version for item in results} == {RS_REDACTION_V2}
    # 更严格：脱敏后载荷逐字节一致（dict 键序不影响结果）。
    assert len({repr(item.payload_redacted) for item in results}) == 1
    assert len({repr(item.unresolved_sensitive_spans) for item in results}) == 1


def test_ac02_dict_key_order_does_not_change_result() -> None:
    left = {"a": "13812345678", "b": "alice@example.com"}
    right = {"b": "alice@example.com", "a": "13812345678"}
    engine = _engine()
    left_result = engine.redact(left, channel=RedactionChannel.EVIDENCE)
    right_result = engine.redact(right, channel=RedactionChannel.EVIDENCE)

    assert left_result.payload_hash == right_result.payload_hash
    assert left_result.payload_redacted == right_result.payload_redacted
    assert left_result.redaction_count == right_result.redaction_count


# ---------------------------------------------------------------------------
# AC-03：不可靠不冒充 REDACTED
# ---------------------------------------------------------------------------


def test_ac03_invalid_id_checksum_is_review_required_not_redacted() -> None:
    invalid = "110105194912310021"  # 校验位错误
    result = _engine().redact({"id": invalid}, channel=RedactionChannel.EVIDENCE)

    assert result.status is RedactionStatus.REVIEW_REQUIRED
    assert result.is_passable is False
    assert SensitiveCategory.ID_CARD in result.detected_categories
    assert [span.reason for span in result.unresolved_sensitive_spans] == [
        "ID_CHECKSUM_INVALID"
    ]


def test_ac03_binary_leaf_is_blocked_without_truncation() -> None:
    result = _engine().redact(
        {"blob": b"secret-bytes", "keep": "visible"},
        channel=RedactionChannel.EVIDENCE,
    )

    assert result.status is RedactionStatus.BLOCKED
    assert result.blockers == ("BINARY_LEAF",)
    assert result.payload_redacted["keep"] == "visible"  # 不删键
    assert result.is_passable is False


def test_ac03_unreplaceable_key_material_is_blocked() -> None:
    key, original = SAMPLES["PRIVATE_KEY"]
    result = _engine().redact({key: original}, channel=RedactionChannel.EXPORT)

    assert result.status is RedactionStatus.BLOCKED
    assert result.unresolved_sensitive_spans
    assert original not in _serialized(result)


def test_ac03_depth_limit_reports_blocker_without_dropping_keys() -> None:
    engine = DataRedactionEngine(max_depth=2)
    payload = {"l1": {"l2": {"l3": {"l4": "13812345678"}}}}
    result = engine.redact(payload, channel=RedactionChannel.EVIDENCE)

    assert result.status is RedactionStatus.REVIEW_REQUIRED
    assert "DEPTH_EXCEEDED" in result.blockers
    assert "l1" in result.payload_redacted
    assert "13812345678" in _serialized(result) or "PHONE" in _serialized(result)


def test_ac03_status_priority_is_independent_of_hit_order() -> None:
    # 先出现「可替换」的手机号，后出现「不可替换」的私钥 → 仍为 BLOCKED。
    result = _engine().redact(
        {"a": "phone 13812345678", "b": SAMPLES["PRIVATE_KEY"][1]},
        channel=RedactionChannel.TOOL_RESULT,
    )
    assert result.status is RedactionStatus.BLOCKED

    # 校验位失败 + 可替换邮箱 → REVIEW_REQUIRED（高于 REDACTED）。
    review = _engine().redact(
        {"a": "alice@example.com", "b": "110105194912310021"},
        channel=RedactionChannel.TOOL_RESULT,
    )
    assert review.status is RedactionStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("category_name", sorted(SAMPLES))
def test_ac03_status_invariants_hold_for_every_sample(category_name: str) -> None:
    key, original = SAMPLES[category_name]
    result = _engine().redact({key: original}, channel=RedactionChannel.TOOL_RESULT)

    if result.status is RedactionStatus.REDACTED:
        assert result.redaction_count > 0
        assert result.unresolved_sensitive_spans == ()
    if result.status in (
        RedactionStatus.REVIEW_REQUIRED,
        RedactionStatus.BLOCKED,
    ):
        assert result.unresolved_sensitive_spans or result.blockers


def test_ac03_spans_are_sorted_deterministically() -> None:
    result = _engine().redact(
        {"b": SAMPLES["PRIVATE_KEY"][1], "a": SAMPLES["CERTIFICATE"][1]},
        channel=RedactionChannel.EXPORT,
    )
    keys = [
        (span.json_path, span.start, span.category.value)
        for span in result.unresolved_sensitive_spans
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# AC-04：哈希口径 + extra="forbid"
# ---------------------------------------------------------------------------


def test_ac04_payload_hash_is_sha256_of_original_canonical_text() -> None:
    payload = {"phone": "13812345678", "duration_ms": 1.25, "when": "2026-01-01"}
    result = _engine().redact(payload, channel=RedactionChannel.TOOL_RESULT)

    assert result.payload_hash == canonical_payload_hash(payload)
    assert len(result.payload_hash) == 64
    assert result.payload_hash == __import__("hashlib").sha256(
        canonical_payload_text(payload).encode("utf-8")
    ).hexdigest()


def test_ac04_redacted_payload_hash_differs_when_replacement_happened() -> None:
    payload = {"phone": "13812345678"}
    result = _engine().redact(payload, channel=RedactionChannel.TOOL_RESULT)

    assert result.redaction_count > 0
    assert result.payload_hash != canonical_payload_hash(result.payload_redacted)


def test_ac04_extra_forbid_rejects_injected_field() -> None:
    result = _engine().redact({"p": "13812345678"}, channel=RedactionChannel.EVIDENCE)
    with pytest.raises(ValidationError):
        RedactionResult(**result.model_dump(), approval_grant="bypass")


def test_ac04_canonical_accepts_float_datetime_and_bytes() -> None:
    """证据：本卡用 `json.dumps(default=str)` 而非 `canonical_digest`（后者会抛）。"""
    payload = {
        "duration_ms": 1.25,
        "when": datetime(2026, 1, 1, 12, 0, 0),
        "raw": b"binary",
    }
    first = canonical_payload_hash(payload)
    assert len(first) == 64
    assert first == canonical_payload_hash(payload)
    assert ',":"' not in canonical_payload_text(payload)  # 紧凑分隔符生效


def test_ac04_clean_payload_hash_still_matches_canonical() -> None:
    payload = {"note": "nothing sensitive", "n": 7}
    result = _engine().redact(payload, channel=RedactionChannel.REPORT)

    assert result.status is RedactionStatus.CLEAN
    assert result.payload_hash == canonical_payload_hash(payload)
    assert result.payload_redacted == payload


def test_ac04_new_helper_matches_existing_evidence_hash_implementation() -> None:
    """T01 未改 `core/evidence/service.py`：两份实现口径必须逐字一致（T02 再委托）。"""
    from core.evidence.service import canonical_payload_hash as evidence_hash

    payload = {
        "z": "末",
        "a": 1,
        "duration_ms": 1.25,
        "when": datetime(2026, 1, 1, 12, 0, 0),
    }
    assert canonical_payload_hash(payload) == evidence_hash(payload)


# ---------------------------------------------------------------------------
# 公开 API / 存量兼容 / 新枚举
# ---------------------------------------------------------------------------


def test_engine_public_api_has_no_dsn_or_registry_parameters() -> None:
    forbidden = ("dsn", "registry", "client", "connection")
    for name, member in inspect.getmembers(DataRedactionEngine, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = inspect.signature(member).parameters
        assert not any(term in param.lower() for param in params for term in forbidden)


def test_redact_text_and_summary_are_consistent() -> None:
    engine = _engine()
    text, result = engine.redact_text(
        "phone 13812345678", channel=RedactionChannel.REPORT
    )

    assert "13812345678" not in text
    assert result.payload_hash == canonical_payload_hash({"text": "phone 13812345678"})
    summary = engine.summary(result)
    assert summary.source_payload_hash == result.payload_hash
    assert summary.redacted_payload_hash == canonical_payload_hash(
        result.payload_redacted
    )
    assert "payload" not in summary.model_dump()


def test_version_and_is_passable() -> None:
    engine = _engine()
    assert engine.version == RS_REDACTION_V2
    clean = engine.redact({"a": "plain"}, channel=RedactionChannel.EVIDENCE)
    assert clean.is_passable is True
    blocked = engine.redact(
        {"a": SAMPLES["PRIVATE_KEY"][1]}, channel=RedactionChannel.EVIDENCE
    )
    assert blocked.is_passable is False


def test_version_v2_is_default_and_v1_is_preserved_for_legacy_rows() -> None:
    """R4-A 硬要求：升版到 v2，同时 V1 常量与历史行口径必须仍可复现（不得删改）。"""
    assert RS_REDACTION_V1 == "redaction-v1"
    assert RS_REDACTION_V2 == "redaction-v2"
    # 历史数据库行与既有审计口径靠这两个值区分，必须互相隔离。
    assert RS_REDACTION_V1 != RS_REDACTION_V2

    current = DataRedactionEngine()
    assert current.version == RS_REDACTION_V2

    payload = {"phone": "13812345678", "email": "alice@example.com"}
    current_result = current.redact(payload, channel=RedactionChannel.TOOL_RESULT)

    legacy = DataRedactionEngine(ruleset_version=RS_REDACTION_V1)
    assert legacy.version == RS_REDACTION_V1
    legacy_result = legacy.redact(payload, channel=RedactionChannel.TOOL_RESULT)

    assert current_result.redaction_version == RS_REDACTION_V2
    assert legacy_result.redaction_version == "redaction-v1"
    # 只升版本标签、不改脱敏行为：两种版本口径下脱敏结果逐字段一致。
    assert current_result.payload_redacted == legacy_result.payload_redacted
    assert current_result.redaction_count == legacy_result.redaction_count
    assert current_result.detected_categories == legacy_result.detected_categories


def test_version_constants_are_exported_from_package() -> None:
    """包级导出必须同时给出 v2（当前默认）与 v1（历史口径）。"""
    import core.redaction as redaction_pkg

    assert "RS_REDACTION_V1" in redaction_pkg.__all__
    assert "RS_REDACTION_V2" in redaction_pkg.__all__
    assert redaction_pkg.RS_REDACTION_V1 is RS_REDACTION_V1
    assert redaction_pkg.RS_REDACTION_V2 is RS_REDACTION_V2


def test_new_error_codes_and_exception_classes_exist() -> None:
    assert ErrorCode.REDACTION_BLOCKED.value == "REDACTION_BLOCKED"
    assert ErrorCode.REDACTION_REVIEW_REQUIRED.value == "REDACTION_REVIEW_REQUIRED"
    assert ErrorCode.EVIDENCE_REF_INVALID.value == "EVIDENCE_REF_INVALID"
    assert ErrorCode.KNOWLEDGE_INVALID.value == "KNOWLEDGE_INVALID"
    blocked = RedactionBlockedError("blocked")
    review = RedactionReviewRequiredError("review")
    assert blocked.code is ErrorCode.REDACTION_BLOCKED
    assert blocked.default_status_code == 409
    assert review.code is ErrorCode.REDACTION_REVIEW_REQUIRED


def test_new_audit_event_types_exist() -> None:
    expected = (
        "REDACTION_APPLIED",
        "REDACTION_BLOCKED",
        "REDACTION_REVIEW_REQUIRED",
        "MODEL_DEGRADED",
        "REPORT_GENERATED",
        "KNOWLEDGE_REVOKED",
    )
    for name in expected:
        assert audit_events.AuditEventType[name].value == name


def test_legacy_redaction_behavior_is_unchanged() -> None:
    """新规则表与旧常量同值，但 `redact_payload` / `redact_text` 行为不动。"""
    from core.logging import redact_text as legacy_redact_text

    assert tuple(audit_events._SENSITIVE_MARKERS) == SENSITIVE_KEY_MARKERS
    assert audit_events.redact_payload({"db_password": "hunter2"}) == {
        "db_password": "<redacted>"
    }
    assert "***" in legacy_redact_text("mysql://u:pwd@host/db")


def test_channel_is_validated() -> None:
    with pytest.raises(ValueError):
        _engine().redact({"a": 1}, channel="TOOL_RESULT")  # type: ignore[arg-type]


def test_version_cannot_be_none() -> None:
    with pytest.raises(ValueError):
        DataRedactionEngine(ruleset_version="none")


# ---------------------------------------------------------------------------
# R05 负向回归（第二轮 QA 复验：元数据键不得被误判为人名）
# ---------------------------------------------------------------------------

# `core/tools/contracts.py:42-59` ToolResult 的 15 个字段，逐一断言不触发 R05。
TOOL_RESULT_FIELDS: tuple[str, ...] = (
    "tool_name",
    "status",
    "task_id",
    "run_id",
    "tool_call_id",
    "started_at",
    "duration_ms",
    "data",
    "warnings",
    "error",
    "source",
    "observed_at",
    "truncated",
    "capability_ref",
    "source_metadata",
)

# 常见数据库/文件元数据键，含 `_name` 后缀但不含人名语义。
METADATA_NAME_KEYS: tuple[str, ...] = (
    "table_name",
    "column_name",
    "file_name",
    "class_name",
    "host_name",
    "field_name",
    "schema_name",
    "index_name",
)

EXPECTED_PERSON_NAME_KEYS: tuple[str, ...] = (
    "name",
    "姓名",
    "user_name",
    "customer_name",
    "display_name",
    "account_name",
    "full_name",
    "owner_name",
    "real_name",
)


# W09 T02 在 `ToolResult` 末尾**追加**的取证字段（设计 §3 第 1 行：
# `redaction: RedactionSummary | None = None`）。它不是业务字段、不参与脱敏判定，
# 也不在 R4-D 的 15 个负例口径内，因此单列于此：字段集一旦再变，本用例仍会失败。
T02_ADDED_TOOL_RESULT_FIELDS: tuple[str, ...] = ("redaction",)


def test_r05_tool_result_field_list_matches_model_exactly() -> None:
    assert len(TOOL_RESULT_FIELDS) == 15
    # R4-D 15 字段仍必须逐一存在（不得删、不得改名）。
    assert set(TOOL_RESULT_FIELDS) <= set(ToolResult.model_fields)
    # 模型字段集只允许多出 T02 明确追加的那一个字段，别的增删一律失败。
    assert set(ToolResult.model_fields) - set(TOOL_RESULT_FIELDS) == set(
        T02_ADDED_TOOL_RESULT_FIELDS
    )


@pytest.mark.parametrize("field", TOOL_RESULT_FIELDS)
def test_r05_all_tool_result_fields_are_not_name_keys(field: str) -> None:
    assert is_name_key(field) is False, f"{field} 被误判为人名键"


def test_r05_target_object_name_field_is_not_a_name_key() -> None:
    """`core/tools/contracts.py:62-80` 的 `TargetObjectSpec.name_field` 同坑。"""
    assert is_name_key("name_field") is False
    for field in ("object_type", "database_field", "schema_field", "keys_field"):
        assert is_name_key(field) is False


@pytest.mark.parametrize("key", EXPECTED_PERSON_NAME_KEYS)
def test_r05_expected_person_name_keys_still_match(key: str) -> None:
    """补齐显式 marker 后不得出现欠脱敏：9 个正向键全部仍命中。"""
    assert is_name_key(key) is True, f"{key} 漏判"


@pytest.mark.parametrize(
    "key",
    ["order_no", "biz_id", "customer_id", "商户号", "联系人", "xx_order_no"],
)
def test_r05_business_id_markers_still_match(key: str) -> None:
    """裁定 v2 R1 验收 #4：B 组业务号/中文键（含带前缀形式）仍命中。"""
    assert is_name_key(key) is True, f"{key} 漏判"


# R05 的**值规则** `^[A-Z]{2,6}\d{6,}$`（设计 §2.2 R05 行）。这条分支此前无任何用例覆盖：
# `<BIZ:...>` 只在「键名**未**命中 + 值形如业务号」时出现；键名命中优先，故 `order_no`
# 这类业务号键产 `<NAME:...>` 而不是 `<BIZ:...>`（实现 `engine.py` 的三元表达式）。
# 两层语义分别锁死，避免后续误把业务号键断言成 `<BIZ:...>`。
def test_r05_biz_id_value_rule_emits_biz_token_for_non_name_key() -> None:
    """值规则生效路径：非人名键 + 值匹配 → `<BIZ:sha8>` + `REVIEW_REQUIRED`。"""
    result = _engine().redact({"ref": "AB1234567"}, channel=RedactionChannel.EXPORT)
    assert is_name_key("ref") is False
    assert str(result.payload_redacted["ref"]).startswith("<BIZ:")
    assert result.payload_redacted["ref"].endswith(">")
    assert result.redaction_count == 1
    assert result.status is RedactionStatus.REVIEW_REQUIRED
    assert SensitiveCategory.PERSON_OR_BUSINESS_ID in result.detected_categories


def test_r05_name_key_takes_priority_over_biz_id_value_rule() -> None:
    """优先级：键名命中优先于值匹配 → 业务号键产 `<NAME:...>`，**不是** `<BIZ:...>`。

    同一值 `AB1234567` 在 `ref`（非人名键）下产 `<BIZ:...>`、在 `order_no`（B 组）下产
    `<NAME:...>`，两者哈希段相同 —— 证明差异只来自键名判定，与值无关。
    """
    same_value = "AB1234567"
    by_value = _engine().redact({"ref": same_value}, channel=RedactionChannel.EXPORT)
    by_key = _engine().redact({"order_no": same_value}, channel=RedactionChannel.EXPORT)

    assert str(by_key.payload_redacted["order_no"]).startswith("<NAME:")
    assert not str(by_key.payload_redacted["order_no"]).startswith("<BIZ:")
    # 只差占位前缀，哈希段一致（sha8 同值同结果）。
    assert by_value.payload_redacted["ref"].removeprefix("<BIZ:") == (
        by_key.payload_redacted["order_no"].removeprefix("<NAME:")
    )


@pytest.mark.parametrize(
    "value",
    [
        "AB12345",          # 数字段 5 位 < 6
        "ab1234567",        # 小写前缀不匹配 [A-Z]
        "ABCDEFG1234567",   # 前缀 7 位 > 6
    ],
)
def test_r05_biz_id_value_rule_boundaries_do_not_match(value: str) -> None:
    """值规则的三个边界（位数不足 / 非大写 / 前缀过长）不得命中，保持 `CLEAN`。"""
    result = _engine().redact({"ref": value}, channel=RedactionChannel.EXPORT)
    assert result.payload_redacted["ref"] == value
    assert result.status is RedactionStatus.CLEAN
    assert result.redaction_count == 0


def test_r05_rule_groups_match_written_ruling() -> None:
    """`docs/w09/05-rulings.md` R1 **v2**：两组闭集（精确 1 + 子串 13），无白名单。

    v2 :26 明确撤回上一版拟设的 `SYSTEM_METADATA_KEYS` 短路白名单：精确匹配
    本身已使 15 个 `ToolResult` 字段与 `name_field` 全部为 False。
    """
    assert NAME_EXACT_KEYS == frozenset({"name"})
    assert len(NAME_KEY_MARKERS) == 13
    assert "name" not in NAME_KEY_MARKERS
    assert is_name_key("name") is True
    assert is_name_key("tool_name") is False
    assert is_name_key("name_field") is False
    # B 组受限子串仍生效：带前后缀的业务号键不得漏判（守住不欠脱敏）。
    assert is_name_key("xx_order_no") is True
    assert is_name_key("Customer-ID") is True
    assert is_name_key("customer_name") is True
    # 精确组与子串组逐项仍命中。
    for key in (*NAME_EXACT_KEYS, *NAME_KEY_MARKERS):
        assert is_name_key(key) is True, key
    # v2 撤回白名单：模块不再暴露该常量，避免下游误依赖一个已废弃闭集。
    assert not hasattr(core_redaction_rules, "SYSTEM_METADATA_KEYS")
    assert not hasattr(core_redaction_rules, "NAME_KEY_EXACT")
    assert not hasattr(core_redaction_rules, "NAME_KEY_SUBSTRING")


# 上一版裁定 C 组的 9 个元数据键；v2 撤回白名单后仍必须全部为 False，
# 只是改由「精确匹配 + 不含裸 name 的子串词表」天然保证，而非硬编码短路。
R1_METADATA_KEYS: tuple[str, ...] = (
    "tool_name",
    "table_name",
    "column_name",
    "schema_name",
    "index_name",
    "file_name",
    "class_name",
    "host_name",
    "field_name",
)


@pytest.mark.parametrize("key", R1_METADATA_KEYS)
def test_r05_metadata_keys_are_false_without_whitelist(key: str) -> None:
    """裁定 v2：不依赖白名单短路，9 个元数据键仍逐个为 False。"""
    assert is_name_key(key) is False, f"{key} 被误判为人名键"


# R4-A 验收 #1 / R4-D 裁定 2 正例集：7 个人名词根的**带前后缀变体**必须命中。
# 裸形式已由 `EXPECTED_PERSON_NAME_KEYS` 覆盖；此处单独锁住「词根子串法」不退化——
# 若有人把 13 项词表换成等价物却让前后缀匹配失效，`len(...) == 13` 与裸形式断言
# 都拦不住，只有这组变体断言能拦住欠脱敏缺口回归。
AFFIXED_PERSON_NAME_KEYS: tuple[str, ...] = (
    "xx_customer_name",
    "some_user_name",
    "xx_owner_name",
    "prefix_real_name",
    "x_display_name",
    "y_account_name",
    "z_full_name",
)


@pytest.mark.parametrize("key", AFFIXED_PERSON_NAME_KEYS)
def test_r05_affixed_person_name_roots_still_match(key: str) -> None:
    """R4-A 验收 #1：词根前后缀变体仍命中，守住欠脱敏缺口不回归。"""
    assert is_name_key(key) is True, f"{key} 漏判（欠脱敏缺口回归）"


# R4-D 裁定 2 负例全集：15 个 `ToolResult` 顶层字段 + 14 项系统/业务元数据键。
# 命名刻意避开已撤回的 `SYSTEM_METADATA_KEYS`（那是被测模块里**不应存在**的常量）。
R4D_NEGATIVE_KEYS: tuple[str, ...] = (
    *TOOL_RESULT_FIELDS,
    "name_field",
    "database_field",
    "schema_field",
    "keys_field",
    "table_name",
    "column_name",
    "schema_name",
    "index_name",
    "file_name",
    "class_name",
    "host_name",
    "field_name",
    "definition_name",
    "nickname",
)


def test_r4d_negative_key_set_matches_written_ruling() -> None:
    """R4-D 裁定 2：负例集 15 + 14 = 29 条，且无重复。"""
    assert len(R4D_NEGATIVE_KEYS) == 29
    assert len(set(R4D_NEGATIVE_KEYS)) == 29
    assert set(TOOL_RESULT_FIELDS) <= set(R4D_NEGATIVE_KEYS)


@pytest.mark.parametrize("key", R4D_NEGATIVE_KEYS)
def test_r4d_negative_keys_are_not_name_keys(key: str) -> None:
    """R4-D 裁定 2 负例：系统元数据键逐个独立断言，不得被误判为人名键。"""
    assert is_name_key(key) is False, f"{key} 被误判为人名键"


def _real_tool_result_payload() -> dict[str, Any]:
    result = ToolResult(
        tool_name="mysql_lab_update_order_note",
        status=ToolStatus.SUCCESS,
        task_id="task_w09_r05",
        run_id="run_w09_r05",
        tool_call_id="call_w09_r05",
        started_at="2026-01-01T00:00:00+00:00",
        duration_ms=12.5,
        data={"rows": 1},
        source=ToolSource(
            database_id="lab_mysql_01",
            db_version="8.0.46",
            collected_at="2026-01-01T00:00:00+00:00",
        ),
    )
    return result.model_dump()


def test_r05_real_tool_result_payload_stays_clean_and_tool_name_unchanged() -> None:
    """端到端负向：真实 ToolResult 载荷必须 CLEAN、可放行、tool_name 逐字不变。"""
    payload = _real_tool_result_payload()
    redaction = _engine().redact(payload, channel=RedactionChannel.TOOL_RESULT)

    assert redaction.status is RedactionStatus.CLEAN
    assert redaction.is_passable is True
    assert redaction.redaction_count == 0
    assert redaction.unresolved_sensitive_spans == ()
    assert redaction.blockers == ()
    assert redaction.detected_categories == ()
    assert redaction.payload_redacted["tool_name"] == "mysql_lab_update_order_note"
    assert redaction.payload_redacted == payload  # 结构同形、零改写


@pytest.mark.parametrize("key", METADATA_NAME_KEYS)
def test_r05_metadata_name_keys_remain_clean_and_unchanged(key: str) -> None:
    payload = {key: "seed_orders"}
    redaction = _engine().redact(payload, channel=RedactionChannel.EVIDENCE)

    assert is_name_key(key) is False
    assert redaction.status is RedactionStatus.CLEAN
    assert redaction.is_passable is True
    assert redaction.payload_redacted[key] == "seed_orders"


def test_r05_person_name_still_replaced_and_flagged() -> None:
    """修法不得造成欠脱敏：显式人名键仍走 R05 的 REVIEW_REQUIRED 路径。"""
    redaction = _engine().redact(
        {"user_name": "张三", "tool_name": "mysql_lab_update_order_note"},
        channel=RedactionChannel.TOOL_RESULT,
    )

    assert SensitiveCategory.PERSON_OR_BUSINESS_ID in redaction.detected_categories
    assert redaction.status is RedactionStatus.REVIEW_REQUIRED
    assert "张三" not in _serialized(redaction)
    assert redaction.payload_redacted["tool_name"] == "mysql_lab_update_order_note"


def test_r05_channel_level_regression_matches_written_ruling() -> None:
    """裁定 R1 验收 #4：通道级载荷不得被隔离，`data` 不得被清空。"""
    payload = {"tool_name": "mysql_lab_update_order_note", "data": {"rows": 1}}
    redaction = _engine().redact(payload, channel=RedactionChannel.TOOL_RESULT)

    assert redaction.status is not RedactionStatus.REVIEW_REQUIRED
    assert redaction.status is not RedactionStatus.BLOCKED
    assert all(
        span.reason != "SEMANTIC_CONFIRMATION_REQUIRED"
        for span in redaction.unresolved_sensitive_spans
    )
    assert redaction.payload_redacted["data"] == {"rows": 1}


def test_r05_determinism_regression_matches_written_ruling() -> None:
    """裁定 R1 验收 #5：含 `tool_name` 的输入 5 轮四量集合大小各为 1。"""
    payload = {"tool_name": "mysql_lab_update_order_note", "data": {"rows": 1}}
    engine = _engine()
    results = [
        engine.redact(payload, channel=RedactionChannel.TOOL_RESULT)
        for _ in range(5)
    ]

    assert len({item.status for item in results}) == 1
    assert len({item.redaction_count for item in results}) == 1
    assert len({item.detected_categories for item in results}) == 1
    assert len({item.payload_hash for item in results}) == 1


# ---------------------------------------------------------------------------
# R3 回归（`docs/w09/05-rulings.md` R3 验收 #1–#6）：BLOCKED 三类占位符语义
# ---------------------------------------------------------------------------

# R06/R10/R11 的**纯净样本**：每类只命中自身一条规则，避免像 `SAMPLES` 里
# 的连接串样本那样额外命中 R03 邮箱，从而把「redaction_count 未增」测准。
R3_BLOCKED_SAMPLES: dict[str, tuple[str, str]] = {
    "CONNECTION_STRING": (
        "target",
        "mysql://dg_ro:pw@127.0.0.1:3306/dg_lab",
    ),
    "PRIVATE_KEY": (
        "pem",
        "-----BEGIN RSA PRIVATE KEY-----MIIBOgIBAAJBAK-----END RSA PRIVATE KEY-----",
    ),
    "CERTIFICATE": (
        "cert",
        "-----BEGIN CERTIFICATE-----MIIBkTCB-----END CERTIFICATE-----",
    ),
}


def _r3_redact(category: str) -> tuple[RedactionResult, str]:
    key, raw = R3_BLOCKED_SAMPLES[category]
    result = _engine().redact({key: raw}, channel=RedactionChannel.EXPORT)
    return result, raw


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_1_blocked_raw_secret_not_in_payload(category: str) -> None:
    """R3 #1：`raw_secret not in payload_redacted`（AC-01 原判据，强度不降）。"""
    result, raw = _r3_redact(category)
    assert raw not in _serialized(result)


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_2_blocked_placeholder_matches_category(category: str) -> None:
    """R3 #2：固定占位符按类别一一对应，且不含哈希/可逆信息。"""
    result, raw = _r3_redact(category)
    serialized = _serialized(result)
    assert "<BLOCKED:" in serialized
    assert f"<BLOCKED:{category}>" in serialized
    assert result.payload_redacted == {
        R3_BLOCKED_SAMPLES[category][0]: f"<BLOCKED:{category}>"
    }
    assert raw not in serialized


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_3_blocked_does_not_increment_redaction_count(category: str) -> None:
    """R3 #3：BLOCKED 是掩码不是「已脱敏」，不得计入 redaction_count。"""
    result, _ = _r3_redact(category)
    clean = _engine().redact(
        {"k": "harmless text"}, channel=RedactionChannel.EXPORT
    )
    assert clean.redaction_count == 0
    assert result.redaction_count == clean.redaction_count == 0


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_4_blocked_status_and_spans_are_recorded(category: str) -> None:
    """R3 #4：状态 BLOCKED、闸门不放行、unresolved span 非空。"""
    result, _ = _r3_redact(category)
    assert result.status is RedactionStatus.BLOCKED
    assert result.is_passable is False
    assert len(result.unresolved_sensitive_spans) > 0
    assert any(
        span.category.value == category
        for span in result.unresolved_sensitive_spans
    )


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_5_blocked_payload_is_identical_over_five_runs(category: str) -> None:
    """R3 #5 / AC-02：同一输入 5 轮 payload_redacted 完全相同。"""
    key, raw = R3_BLOCKED_SAMPLES[category]
    engine = _engine()
    payloads = [
        _serialized(engine.redact({key: raw}, channel=RedactionChannel.EXPORT))
        for _ in range(5)
    ]
    assert len(set(payloads)) == 1


@pytest.mark.parametrize("category", sorted(R3_BLOCKED_SAMPLES))
def test_r3_6_spans_carry_only_locators_not_raw_fragments(category: str) -> None:
    """R3 #6：隔离定位信息只有 json_path/start/end/reason，不含任何原文片段。

    T01 领域层没有独立的 `redaction_quarantine` 实体（属 T02 闸门），等价载体
    是 `unresolved_sensitive_spans`；此断言锁定其字段不含原文片段，与
    `03-task-cards.md:213` 的「隔离区只存定位信息」口径一致。
    """
    result, raw = _r3_redact(category)
    assert result.unresolved_sensitive_spans
    for span in result.unresolved_sensitive_spans:
        assert isinstance(span.start, int) and span.start >= 0
        assert isinstance(span.end, int) and span.end >= span.start
        assert span.json_path
        for locator in (span.json_path, span.reason, span.category.value):
            assert raw not in locator
            assert "<BLOCKED:" not in locator
    # 整条 span 序列化后也不得出现原文片段（含 begin/end 标记等任意子串）。
    span_text = json.dumps(
        [span.model_dump(mode="json") for span in result.unresolved_sensitive_spans],
        ensure_ascii=False,
    )
    for fragment in (
        raw,
        raw[:12],
        "-----BEGIN",
        "MIIB",
        "dg_ro",
        "127.0.0.1",
    ):
        assert fragment not in span_text
