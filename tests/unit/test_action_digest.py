"""`action_digest` 冻结向量、六类失效与参数规范化（ADR-0005 §2/§5）。

要点：
- 基准向量的**规范串与摘要**都要逐字节复现（只比摘要会漏掉自洽的规范化变更）；
- 六类字段变化（参数/目标对象/环境/环境指纹/策略版本/工具版本）必须各自产生不同摘要；
- `normalize_tool_parameters` 必须保证"显式写默认值"与"不写"同摘要。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from core.approval.digest import (
    ACTION_DIGEST_VERSION,
    build_action_descriptor,
    build_normalized_operation,
    canonical_descriptor_text,
    compute_action_digest,
    compute_prerequisites_digest,
    environment_fingerprint_digest,
    environment_fingerprint_from_row,
    extract_target_objects,
    fingerprint_completeness,
    normalize_target_objects,
    normalize_tool_parameters,
)
from core.security.canonical import CanonicalizationError
from core.tools.contracts import TargetObjectSpec, ToolDefinition
from tests.helpers.definitions import write_declarations

BASE_ACTION_DIGEST = "f31f153767a028a758bb3ef02fb9322422e8fc0ff77c287ad459893f7a8d5bc2"
BASE_CANONICAL_TEXT = (
    '{"action_digest_version":"w06-v1","db_engine":"mysql",'
    '"environment_fingerprint":{"config_version":"w03-local","db_engine":"mysql",'
    '"db_version":"8.0.46","image_digest":null,"network_id":"lab_net"},'
    '"environment_id":"env_mysql_local","normalized_operation":{"idempotent":false,'
    '"operation_class":"repair","side_effect":"write","statement_kind":"update",'
    '"summary":"将 orders 表中 id=1001 的行 status 从 NEW 改为 PAID"},'
    '"parameters":{"expected_rows":1,"id":1001,"status":"PAID","table":"orders"},'
    '"policy_version":"w06-v1","target_objects":[{"database":"dg_lab",'
    '"keys":["id=1001"],"name":"orders","object_type":"table","schema":null}],'
    '"tenant_id":"tenant_local","tool":{"name":"mysql_update_rows","version":"1.0"}}'
)


def base_descriptor() -> dict[str, Any]:
    """ADR-0005 的完整 `action_digest` 冻结输入（源文本键序可任意打乱）。"""
    return {
        "action_digest_version": "w06-v1",
        "db_engine": "mysql",
        "environment_fingerprint": {
            "config_version": "w03-local",
            "db_engine": "mysql",
            "db_version": "8.0.46",
            "image_digest": None,
            "network_id": "lab_net",
        },
        "environment_id": "env_mysql_local",
        "normalized_operation": {
            "idempotent": False,
            "operation_class": "repair",
            "side_effect": "write",
            "statement_kind": "update",
            "summary": "将 orders 表中 id=1001 的行 status 从 NEW 改为 PAID",
        },
        "parameters": {"expected_rows": 1, "id": 1001, "status": "PAID", "table": "orders"},
        "policy_version": "w06-v1",
        "target_objects": [
            {
                "database": "dg_lab",
                "keys": ["id=1001"],
                "name": "orders",
                "object_type": "table",
                "schema": None,
            }
        ],
        "tenant_id": "tenant_local",
        "tool": {"name": "mysql_update_rows", "version": "1.0"},
    }


# ---------------------------------------------------------------------------
# 冻结向量
# ---------------------------------------------------------------------------


def test_base_action_descriptor_is_byte_exact() -> None:
    descriptor = base_descriptor()
    assert canonical_descriptor_text(descriptor) == BASE_CANONICAL_TEXT
    action, parameters, targets = compute_action_digest(descriptor)
    assert action == BASE_ACTION_DIGEST
    # 分通道摘要稳定（用于定位不符项；此处锁定取值，防止算法被悄悄改掉）
    assert parameters == "195322569497093bcfc59f0025575f18a24d24c163878f02f0ae83d71e45c904"
    assert targets == "6f14bf084071058b294d76e9ea2f372822d8f637c6bd64ef4287471d7eb3eb59"


def test_key_order_in_source_does_not_change_digest() -> None:
    shuffled = base_descriptor()
    # 重排顶层与嵌套键的插入顺序（JCS 会重新排序）。
    reordered = {key: shuffled[key] for key in reversed(list(shuffled))}
    reordered["parameters"] = {
        key: shuffled["parameters"][key] for key in reversed(list(shuffled["parameters"]))
    }
    assert canonical_descriptor_text(reordered) == BASE_CANONICAL_TEXT
    assert compute_action_digest(reordered)[0] == BASE_ACTION_DIGEST


# ADR-0005 的 5 条失效子向量：每类变化必须得到指定的不同摘要。
INVALIDATION_VECTORS = [
    (
        "parameters",
        ("parameters", "expected_rows", 2),
        "9c25c50aabfa54ee11dc9dbe1515f4c662bf72a11041fb3a381b1c2da69c1b04",
    ),
    (
        "policy_version",
        ("policy_version", None, "w06-v2"),
        "0b164e1a34ffff0817def520daebd45d021a099fec3b2c04b392e7ef344943ee",
    ),
    (
        "environment_fingerprint",
        ("environment_fingerprint", "config_version", "w06-local"),
        "215e72a91cdf7d1b10b055d1956c60bed6ae7c9df67f624b2a77269393254277",
    ),
    (
        "target_objects",
        ("target_objects", "name", "orders_archive"),
        "a91569943a0f4353cb2abaae60e1f6401bca6b671a08605d957434cae6bd3710",
    ),
    (
        "tool_version",
        ("tool", "version", "1.1"),
        "ed21be519c2828991119ef5abb4a560d4c5f0c834ec4667ba201cf69c1d27974",
    ),
]


def _mutate(descriptor: dict[str, Any], path: tuple[str, str | None, Any]) -> dict[str, Any]:
    changed = copy.deepcopy(descriptor)
    first, second, value = path
    if second is None:
        changed[first] = value
    elif first == "target_objects":
        changed[first][0][second] = value
    else:
        changed[first][second] = value
    return changed


@pytest.mark.parametrize("label,path,expected", INVALIDATION_VECTORS)
def test_invalidation_vector_matches_frozen_digest(
    label: str, path: tuple[str, str | None, Any], expected: str
) -> None:
    changed = _mutate(base_descriptor(), path)
    assert compute_action_digest(changed)[0] == expected, label


def test_six_change_types_all_yield_distinct_digests() -> None:
    """六类失效（参数/目标对象/环境/环境指纹/策略版本/工具版本）必须两两不同。"""
    base = base_descriptor()
    variants: dict[str, dict[str, Any]] = {
        "parameters": _mutate(base, ("parameters", "status", "REFUNDED")),
        "target_objects": _mutate(base, ("target_objects", "name", "orders_archive")),
        "environment": _mutate(base, ("environment_id", None, "env_mysql_other")),
        "environment_fingerprint": _mutate(
            base, ("environment_fingerprint", "db_version", "8.0.47")
        ),
        "policy_version": _mutate(base, ("policy_version", None, "w06-v2")),
        "tool_version": _mutate(base, ("tool", "version", "1.1")),
    }
    digests = {name: compute_action_digest(descriptor)[0] for name, descriptor in variants.items()}
    assert len(set(digests.values())) == len(digests)
    assert BASE_ACTION_DIGEST not in digests.values()


def test_added_or_removed_target_object_changes_digest() -> None:
    base = base_descriptor()
    extra = copy.deepcopy(base)
    extra["target_objects"].append(
        {
            "database": "dg_lab",
            "keys": [],
            "name": "order_items",
            "object_type": "table",
            "schema": None,
        }
    )
    assert compute_action_digest(extra)[0] != BASE_ACTION_DIGEST


# ---------------------------------------------------------------------------
# 描述符字段集封闭
# ---------------------------------------------------------------------------


def test_descriptor_rejects_extra_field() -> None:
    descriptor = base_descriptor()
    descriptor["unexpected"] = 1
    with pytest.raises(CanonicalizationError):
        compute_action_digest(descriptor)


def test_descriptor_rejects_missing_field() -> None:
    descriptor = base_descriptor()
    descriptor.pop("policy_version")
    with pytest.raises(CanonicalizationError):
        compute_action_digest(descriptor)


def test_descriptor_rejects_unknown_operation_field() -> None:
    descriptor = base_descriptor()
    descriptor["normalized_operation"]["extra"] = "x"
    with pytest.raises(CanonicalizationError):
        compute_action_digest(descriptor)


def test_descriptor_rejects_float_parameter() -> None:
    """参数含浮点必须报错，不得降级为字符串（ADR-0005 §1）。"""
    descriptor = base_descriptor()
    descriptor["parameters"]["ratio"] = 0.5
    with pytest.raises(CanonicalizationError):
        compute_action_digest(descriptor)


def test_build_action_descriptor_matches_frozen_digest() -> None:
    """通过构建函数（而非手写字典）也要复现同一摘要。"""
    definition = ToolDefinition(
        name="mysql_update_rows",
        description="更新订单状态",
        version="1.0",
        risk_level="R2",
        side_effect="write",
        idempotent=False,
        operation_class="repair",
        permissions=["mysql:write"],
        required_role="dg_rw",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        target_objects=list(_DEFAULT_TARGET_SPECS),
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )
    operation = {
        "idempotent": False,
        "operation_class": "repair",
        "side_effect": "write",
        "statement_kind": "update",
        "summary": "将 orders 表中 id=1001 的行 status 从 NEW 改为 PAID",
    }
    descriptor = build_action_descriptor(
        tenant_id="tenant_local",
        environment_id="env_mysql_local",
        db_engine="mysql",
        fingerprint=base_descriptor()["environment_fingerprint"],
        definition=definition,
        normalized_operation=operation,
        target_objects=base_descriptor()["target_objects"],
        parameters={"expected_rows": 1, "id": 1001, "status": "PAID", "table": "orders"},
        policy_version="w06-v1",
        action_digest_version="w06-v1",
    )
    assert descriptor["action_digest_version"] == ACTION_DIGEST_VERSION
    assert compute_action_digest(descriptor)[0] == BASE_ACTION_DIGEST


def test_build_normalized_operation_is_deterministic() -> None:
    definition = ToolDefinition(
        name="mysql_update_rows",
        description="更新订单状态",
        version="1.0",
        risk_level="R2",
        side_effect="write",
        idempotent=False,
        operation_class="repair",
        permissions=["mysql:write"],
        required_role="dg_rw",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        target_objects=list(_DEFAULT_TARGET_SPECS),
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    )
    targets = [
        {"object_type": "table", "database": "dg_lab", "schema": None, "name": "orders", "keys": ["id=1001"]}
    ]
    first = build_normalized_operation(definition, targets)
    second = build_normalized_operation(definition, targets)
    assert first == second
    assert first["statement_kind"] == "update"
    assert set(first) == {"operation_class", "side_effect", "idempotent", "statement_kind", "summary"}


# ---------------------------------------------------------------------------
# 目标对象规范化
# ---------------------------------------------------------------------------


def test_target_object_key_order_and_listing_order_are_irrelevant() -> None:
    first = [
        {
            "object_type": "table",
            "database": "dg_lab",
            "schema": None,
            "name": "orders",
            "keys": ["id=1002", "id=1001", "id=1001"],
        },
        {
            "object_type": "table",
            "database": "dg_lab",
            "schema": "public",
            "name": "orders_archive",
            "keys": [],
        },
    ]
    second = list(reversed(copy.deepcopy(first)))
    normalized_first = normalize_target_objects(first)
    normalized_second = normalize_target_objects(second)
    assert normalized_first == normalized_second
    assert normalized_first[0]["keys"] == ["id=1001", "id=1002"]


def test_target_object_requires_closed_key_set() -> None:
    with pytest.raises(CanonicalizationError):
        normalize_target_objects(
            [{"object_type": "table", "database": "dg_lab", "name": "orders", "keys": []}]
        )
    with pytest.raises(CanonicalizationError):
        normalize_target_objects(
            [
                {
                    "object_type": "table",
                    "database": "dg_lab",
                    "schema": None,
                    "name": "orders",
                    "keys": [],
                    "extra": 1,
                }
            ]
        )


# ---------------------------------------------------------------------------
# 环境指纹
# ---------------------------------------------------------------------------


def test_fingerprint_from_environments_row() -> None:
    row = {
        "environment_id": "env_mysql_local",
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "network_id": "lab_net",
        "image_digest": None,
        "config_version": "w03-local",
        "status": "LAB",
    }
    fingerprint = environment_fingerprint_from_row(row)
    assert fingerprint == base_descriptor()["environment_fingerprint"]
    assert environment_fingerprint_digest(fingerprint) == environment_fingerprint_digest(
        base_descriptor()["environment_fingerprint"]
    )
    # image_digest 为 null 时标记 PARTIAL（ADR-0005 §2：不阻断 LAB 审批）
    assert fingerprint_completeness(fingerprint) == "PARTIAL"


def test_fingerprint_completeness_complete_when_all_present() -> None:
    row = {
        "db_engine": "mysql",
        "db_version": "8.0.46",
        "network_id": "lab_net",
        "image_digest": "sha256:abc",
        "config_version": "w03-local",
    }
    assert fingerprint_completeness(environment_fingerprint_from_row(row)) == "COMPLETE"


# ---------------------------------------------------------------------------
# 参数规范化：显式默认值 == 不写
# ---------------------------------------------------------------------------

_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "string"},
        "id": {"type": "integer"},
        "status": {"type": "string"},
        "expected_rows": {"type": "integer", "default": 1},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["table", "id", "status"],
    "additionalProperties": False,
}


_DEFAULT_TARGET_SPECS = [
    TargetObjectSpec(
        object_type="table",
        database_field="database",
        name_field="table",
        keys_field="keys",
    )
]


def _definition(
    schema: dict[str, Any],
    *,
    side_effect: str = "write",
    target_objects: list[TargetObjectSpec] | None = None,
) -> ToolDefinition:
    """写工具必须声明 target_objects（P0-03b 契约），读工具留空。"""
    specs = target_objects
    if specs is None:
        specs = list(_DEFAULT_TARGET_SPECS) if side_effect == "write" else []
    return ToolDefinition(
        name="mysql_update_rows",
        description="更新订单状态",
        version="1.0",
        risk_level="R2" if side_effect == "write" else "R0",
        side_effect=side_effect,  # type: ignore[arg-type]
        idempotent=False,
        operation_class="repair",
        permissions=["mysql:write"],
        required_role="dg_rw" if side_effect == "write" else "dg_ro",
        input_schema=schema,
        output_schema={"type": "object"},
        target_objects=specs,
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **(write_declarations() if side_effect == "write" else {}),
    )


def test_explicit_default_parameters_hash_identically() -> None:
    definition = _definition(_UPDATE_SCHEMA)
    minimal = normalize_tool_parameters(
        definition, {"table": "orders", "id": 1001, "status": "PAID"}
    )
    explicit = normalize_tool_parameters(
        definition,
        {
            "table": "orders",
            "id": 1001,
            "status": "PAID",
            "expected_rows": 1,
            "dry_run": False,
        },
    )
    assert minimal == explicit == {
        "table": "orders",
        "id": 1001,
        "status": "PAID",
        "expected_rows": 1,
        "dry_run": False,
    }


def test_parameter_normalization_changes_digest_when_value_changes() -> None:
    definition = _definition(_UPDATE_SCHEMA)
    first = normalize_tool_parameters(definition, {"table": "orders", "id": 1001, "status": "PAID"})
    second = normalize_tool_parameters(
        definition, {"table": "orders", "id": 1001, "status": "PAID", "expected_rows": 2}
    )
    assert first != second


def test_normalize_tool_parameters_rejects_missing_required() -> None:
    definition = _definition(_UPDATE_SCHEMA)
    with pytest.raises(CanonicalizationError):
        normalize_tool_parameters(definition, {"table": "orders"})


def test_normalize_tool_parameters_rejects_wrong_type() -> None:
    definition = _definition(_UPDATE_SCHEMA)
    with pytest.raises(CanonicalizationError):
        normalize_tool_parameters(definition, {"table": "orders", "id": "1001", "status": "PAID"})


def test_normalize_tool_parameters_rejects_float() -> None:
    """浮点即使符合 JSON Schema 也必须在规范化阶段被拒绝。"""
    definition = _definition({"type": "object", "properties": {"ratio": {"type": "number"}}})
    with pytest.raises(CanonicalizationError):
        normalize_tool_parameters(definition, {"ratio": 0.5})


def test_normalize_tool_parameters_rejects_unknown_key() -> None:
    definition = _definition(_UPDATE_SCHEMA)
    with pytest.raises(CanonicalizationError):
        normalize_tool_parameters(
            definition, {"table": "orders", "id": 1001, "status": "PAID", "where": "1=1"}
        )


# ---------------------------------------------------------------------------
# 前置条件摘要
# ---------------------------------------------------------------------------


def _check(check_id: str, status: str = "PASS") -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "observed": {"reason": "OK"},
        "source": "server_probe",
    }


def test_prerequisites_digest_is_order_independent_and_bound_to_action() -> None:
    checks = [_check("environment_fingerprint"), _check("tool_version_matches"), _check("policy_version_matches")]
    first = compute_prerequisites_digest(action_digest=BASE_ACTION_DIGEST, checks=checks)
    second = compute_prerequisites_digest(
        action_digest=BASE_ACTION_DIGEST, checks=list(reversed(checks))
    )
    assert first == second
    other_action = compute_prerequisites_digest(
        action_digest="0" * 64, checks=checks
    )
    assert other_action != first


def test_prerequisites_digest_changes_with_check_status() -> None:
    passed = compute_prerequisites_digest(
        action_digest=BASE_ACTION_DIGEST, checks=[_check("environment_fingerprint")]
    )
    failed = compute_prerequisites_digest(
        action_digest=BASE_ACTION_DIGEST,
        checks=[_check("environment_fingerprint", "UNKNOWN")],
    )
    assert passed != failed


def test_prerequisites_digest_rejects_open_field_set() -> None:
    bad = {"check_id": "x", "status": "PASS", "observed": {}, "source": "server_probe", "note": "人工输入"}
    with pytest.raises(CanonicalizationError):
        compute_prerequisites_digest(action_digest=BASE_ACTION_DIGEST, checks=[bad])


# ---------------------------------------------------------------------------
# 目标对象契约声明（P0-03b）：参数 → 目标对象 → 摘要 同源
# ---------------------------------------------------------------------------


def _declared_definition(**overrides: Any) -> ToolDefinition:
    """声明了目标对象映射的写工具（真实写工具由 W07 提供，本周用测试桩）。"""
    values: dict[str, Any] = {
        "name": "mysql_update_rows",
        "description": "更新订单状态",
        "version": "1.0",
        "risk_level": "R2",
        "side_effect": "write",
        "idempotent": False,
        "operation_class": "repair",
        "permissions": ["mysql:write"],
        "required_role": "dg_rw",
        "input_schema": _UPDATE_SCHEMA,
        "output_schema": {"type": "object"},
        "target_objects": [
            TargetObjectSpec(
                object_type="table",
                database_field="database",
                name_field="table",
                schema_field="schema",
                keys_field="keys",
            )
        ],
        # W07：写工具还需声明补偿方案与验证计划（构造期 fail closed）。
        **write_declarations(),
    }
    values.update(overrides)
    return ToolDefinition(**values)


def test_tool_definition_defaults_for_w06_contract_fields() -> None:
    read_tool = _definition({"type": "object"}, side_effect="read")
    assert read_tool.preconditions == []
    assert read_tool.target_objects == []


def test_write_tool_without_target_objects_is_rejected_at_contract() -> None:
    """契约定义期就 fail closed：写工具不允许"没声明目标对象"。"""
    with pytest.raises(Exception):
        _definition({"type": "object"}, side_effect="write", target_objects=[])


def test_extract_target_objects_from_declared_spec() -> None:
    definition = _declared_definition()
    params = {"database": "dg_lab", "table": "orders", "keys": ["id=1002", "id=1001", "id=1001"]}
    extracted = extract_target_objects(definition, params)
    assert extracted == [
        {
            "object_type": "table",
            "database": "dg_lab",
            "schema": None,
            "name": "orders",
            "keys": ["id=1001", "id=1002"],
        }
    ]


def test_extracted_targets_hash_identically_to_explicit_targets() -> None:
    """抽取结果与手写目标对象必须产出同一 `target_objects_digest`。"""
    definition = _declared_definition()
    extracted = extract_target_objects(
        definition, {"database": "dg_lab", "table": "orders", "keys": ["id=1001"]}
    )
    explicit = [
        {
            "object_type": "table",
            "database": "dg_lab",
            "schema": None,
            "name": "orders",
            "keys": ["id=1001"],
        }
    ]
    assert normalize_target_objects(extracted) == normalize_target_objects(explicit)


def test_extract_target_objects_requires_declared_fields() -> None:
    definition = _declared_definition()
    with pytest.raises(CanonicalizationError):
        extract_target_objects(definition, {"table": "orders"})


def test_optional_spec_is_skipped_when_absent() -> None:
    definition = _declared_definition(
        target_objects=[
            TargetObjectSpec(
                object_type="row",
                database_field="database",
                name_field="table",
                required=False,
            )
        ]
    )
    assert extract_target_objects(definition, {}) == []


def test_undeclared_tool_yields_no_targets() -> None:
    read_tool = _definition(_UPDATE_SCHEMA, side_effect="read")
    assert extract_target_objects(read_tool, {"table": "orders"}) == []


def test_scalar_key_becomes_single_element_array() -> None:
    definition = _declared_definition(
        target_objects=[
            TargetObjectSpec(
                object_type="row",
                database_field="database",
                name_field="table",
                keys_field="id",
            )
        ]
    )
    extracted = extract_target_objects(
        definition, {"database": "dg_lab", "table": "orders", "id": "id=1001"}
    )
    assert extracted[0]["keys"] == ["id=1001"]


def test_non_string_key_is_rejected() -> None:
    definition = _declared_definition()
    with pytest.raises(CanonicalizationError):
        extract_target_objects(
            definition, {"database": "dg_lab", "table": "orders", "keys": [1001]}
        )
