"""操作摘要构建（W06 D1，ADR-0005 §2–§4）。

**不得复用 `core/gateway/envelope.py::compute_args_digest`**：它使用
`sort_keys=True`（Python 码点序，与 UTF-16 码元序在补充平面字符上相反）+
`default=str`（静默降级），会直接违背 ADR-0005 的冻结向量。

所有摘要都经 `core/security/canonical.py`（唯一入口），本模块只负责把业务
上下文整理成**字段集封闭**的规范输入。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.security.canonical import (
    CanonicalizationError,
    canonical_digest,
    canonical_json,
    sha256_hex,
    sorted_by_canonical,
)
from core.tools.contracts import ToolDefinition

ACTION_DIGEST_VERSION = "w06-v1"
PREREQUISITES_DIGEST_VERSION = "w06-v1"

# 字段集封闭：多一个、少一个、拼错都报错（ADR-0005 §2）。
DESCRIPTOR_KEYS: tuple[str, ...] = (
    "action_digest_version",
    "tenant_id",
    "environment_id",
    "db_engine",
    "environment_fingerprint",
    "tool",
    "normalized_operation",
    "target_objects",
    "parameters",
    "policy_version",
)
FINGERPRINT_KEYS: tuple[str, ...] = (
    "db_engine",
    "db_version",
    "image_digest",
    "network_id",
    "config_version",
)
TOOL_KEYS: tuple[str, ...] = ("name", "version")
OPERATION_KEYS: tuple[str, ...] = (
    "operation_class",
    "side_effect",
    "idempotent",
    "statement_kind",
    "summary",
)
TARGET_OBJECT_KEYS: tuple[str, ...] = ("object_type", "database", "schema", "name", "keys")

_ENGINES = ("mysql", "postgresql")
_OPERATION_CLASSES = ("observe", "diagnose", "experiment", "repair")
_SIDE_EFFECTS = ("none", "read", "write")

# 语句类别推导：由工具契约与目标对象确定性生成，不接受自由文本覆盖。
_STATEMENT_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("update", "update"),
    ("delete", "delete"),
    ("insert", "insert"),
    ("create", "ddl"),
    ("alter", "ddl"),
    ("drop", "ddl"),
    ("truncate", "ddl"),
    ("replace", "update"),
)

_OPERATION_LABELS = {
    "observe": "只读观察",
    "diagnose": "只读诊断",
    "experiment": "受控实验",
    "repair": "修复写入",
}


def _fail(message: str, **details: Any) -> None:
    raise CanonicalizationError(message, details=details)


def _require_keys(value: Mapping[str, Any], keys: Sequence[str], *, label: str) -> None:
    """校验字段集封闭：缺失 = 错误（不是 null），多余 = 错误。"""
    present = set(value)
    expected = set(keys)
    missing = sorted(expected - present)
    extra = sorted(present - expected)
    if missing:
        _fail(f"{label} 缺少必填字段", missing=missing, label=label)
    if extra:
        _fail(f"{label} 含未声明字段（字段集封闭）", extra=extra, label=label)


def _require_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} 必须是非空字符串", label=label, value_type=type(value).__name__)
    return value


# ---------------------------------------------------------------------------
# 环境指纹
# ---------------------------------------------------------------------------


def environment_fingerprint_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """从 `dg_system.environments` 行提取指纹：事实来源唯一，不得凭空生成。

    缺列按 `null` 处理（并在 :func:`fingerprint_completeness` 中标记 PARTIAL），
    这是 ADR-0005 §2 的既定口径。
    """
    return {
        "db_engine": row.get("db_engine"),
        "db_version": row.get("db_version"),
        "image_digest": row.get("image_digest"),
        "network_id": row.get("network_id"),
        "config_version": row.get("config_version"),
    }


def fingerprint_completeness(fingerprint: Mapping[str, Any]) -> str:
    """`COMPLETE` / `PARTIAL`。

    `image_digest` 为 null（当前本机 LAB 的实际情况）时标记 `PARTIAL`：摘要仍然
    包含该 `null`，审批照常进行，但披露层必须如实显示"指纹不完整"。
    """
    if not isinstance(fingerprint, Mapping):
        return "PARTIAL"
    for key in FINGERPRINT_KEYS:
        value = fingerprint.get(key)
        if not isinstance(value, str) or not value.strip():
            return "PARTIAL"
    return "COMPLETE"


def environment_fingerprint_digest(fingerprint: Mapping[str, Any]) -> str:
    """环境指纹摘要（分通道摘要之一，用于定位"环境事实变了"）。"""
    _require_keys(fingerprint, FINGERPRINT_KEYS, label="environment_fingerprint")
    return canonical_digest(dict(fingerprint))


# ---------------------------------------------------------------------------
# 参数规范化
# ---------------------------------------------------------------------------


def normalize_tool_parameters(
    definition: ToolDefinition, params: Mapping[str, Any] | None
) -> dict[str, Any]:
    """把请求参数规范化为摘要输入：校验 → 填默认值 → 断言可取摘要。

    审批创建侧与执行侧**必须调用同一个函数**，否则"显式写了默认值"与"没写"
    会得到不同摘要（ADR-0005 §2.3）。
    """
    import jsonschema

    raw: dict[str, Any] = dict(params or {})
    schema = definition.input_schema or {"type": "object"}
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda error: list(error.path))
    if errors:
        raise CanonicalizationError(
            "工具参数不符合 input_schema",
            details={
                "tool_name": definition.name,
                "errors": [
                    {"path": [str(part) for part in error.path], "message": error.message}
                    for error in errors[:20]
                ],
            },
        )

    filled = _fill_defaults(raw, schema)
    try:
        canonical_json(filled)
    except CanonicalizationError as exc:
        raise CanonicalizationError(
            "工具参数无法规范化（拒绝 float/Decimal/datetime 等）",
            details={
                "tool_name": definition.name,
                "reason": exc.details,
            },
        ) from None
    return filled


def _fill_defaults(value: Any, schema: Mapping[str, Any]) -> Any:
    """按 schema 声明的 `default` 递归补齐缺省值；不改变已给出的值。"""
    if not isinstance(schema, Mapping):
        return value
    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return {key: _fill_defaults(item, {}) for key, item in value.items()}
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = properties.get(key, {})
            result[key] = _fill_defaults(item, child)
        for key, child in properties.items():
            if key in result or not isinstance(child, Mapping):
                continue
            if "default" in child:
                result[key] = _fill_defaults(child["default"], child)
        return result
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            return [_fill_defaults(item, item_schema) for item in value]
        return list(value)
    return value


# ---------------------------------------------------------------------------
# 目标对象规范化
# ---------------------------------------------------------------------------


def extract_target_objects(
    definition: ToolDefinition, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """按工具契约的 `target_objects` 声明，从**已规范化参数**中抽取目标对象。

    这是"参数里的目标"与"摘要里的目标"同源的唯一通道：
    - 返回结构可直接交给 :func:`normalize_target_objects`（五键封闭）；
    - 必填字段（`required=True`）缺失或类型不符 → `CanonicalizationError`，不降级；
    - `required=False` 的声明在参数缺失时整条跳过（不产生 `null` 占位对象）；
    - 声明为空 → 返回空列表（是否允许为空由调用方按 `side_effect` 决定）。
    """
    specs = getattr(definition, "target_objects", None) or []
    extracted: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        label = f"target_objects[{index}]"
        database = _read_field(params, spec.database_field, label=label)
        name = _read_field(params, spec.name_field, label=label)
        schema_value = (
            _read_field(params, spec.schema_field, label=label) if spec.schema_field else None
        )
        keys_value = (
            _read_field(params, spec.keys_field, label=label) if spec.keys_field else None
        )
        if database is None or name is None:
            if spec.required:
                _fail(
                    "目标对象必填字段缺失",
                    label=label,
                    database_field=spec.database_field,
                    name_field=spec.name_field,
                )
            continue
        if not isinstance(schema_value, (str, type(None))):
            _fail("目标对象 schema 必须是字符串或 null", label=label)
        extracted.append(
            {
                "object_type": spec.object_type,
                "database": _require_text(database, label=f"{label}.database"),
                "schema": schema_value,
                "name": _require_text(name, label=f"{label}.name"),
                "keys": _normalize_keys(keys_value, label=label),
            }
        )
    return normalize_target_objects(extracted)


def _read_field(params: Mapping[str, Any], field_name: str, *, label: str) -> Any:
    """从参数中读取 spec 声明的字段名；缺失返回 None（由调用方决定是否报错）。"""
    _ = label
    return params.get(field_name)


def _normalize_keys(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # 单值主键允许写成标量，但摘要里统一为单元素数组（"id=1001"）。
        return [value]
    if isinstance(value, (list, tuple)):
        keys: list[str] = []
        for item in value:
            if not isinstance(item, str):
                _fail("目标对象 keys 必须是字符串数组", label=label)
            keys.append(item)
        return keys
    _fail("目标对象 keys 必须是字符串、字符串数组或 null", label=label)
    raise AssertionError("unreachable")


def normalize_target_objects(target_objects: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(target_objects):
        if not isinstance(item, Mapping):
            _fail("target_objects 元素必须是对象", index=index)
        _require_keys(item, TARGET_OBJECT_KEYS, label=f"target_objects[{index}]")
        object_type = _require_text(item["object_type"], label="target_objects.object_type")
        database = _require_text(item["database"], label="target_objects.database")
        name = _require_text(item["name"], label="target_objects.name")
        schema_value = item["schema"]
        if schema_value is not None and not isinstance(schema_value, str):
            _fail("target_objects.schema 必须是字符串或 null", index=index)
        keys_value = item["keys"]
        if keys_value is None:
            keys: list[str] = []
        elif isinstance(keys_value, (list, tuple)):
            deduped: dict[bytes, str] = {}
            for key in keys_value:
                if not isinstance(key, str):
                    _fail("target_objects.keys 必须是字符串数组", index=index)
                deduped[key.encode("utf-16-be")] = key
            keys = [deduped[item_key] for item_key in sorted(deduped)]
        else:
            _fail("target_objects.keys 必须是字符串数组或 null", index=index)
        normalized.append(
            {
                "object_type": object_type,
                "database": database,
                "schema": schema_value,
                "name": name,
                "keys": keys,
            }
        )
    return sorted_by_canonical(normalized)


# ---------------------------------------------------------------------------
# 描述符与摘要
# ---------------------------------------------------------------------------


def statement_kind_for(definition: ToolDefinition) -> str:
    """由工具契约确定性推导语句类别（不接受自由文本覆盖）。"""
    lowered = definition.name.lower()
    for token, kind in _STATEMENT_KIND_HINTS:
        if token in lowered:
            return kind
    if definition.side_effect == "write":
        return "write"
    return "select"


def build_operation_summary(
    definition: ToolDefinition, target_objects: Sequence[Mapping[str, Any]]
) -> str:
    """服务端确定性描述（模板变更即摘要变更，须随 `action_digest_version` 管理）。"""
    label = _OPERATION_LABELS.get(definition.operation_class, "操作")
    targets = len(list(target_objects))
    scope = "、".join(
        f"{item.get('database')}.{item.get('name')}" for item in target_objects
    ) or "未指定目标对象"
    return f"{label}：{definition.name} v{definition.version} 作用于 {targets} 个对象（{scope}）"


def build_normalized_operation(
    definition: ToolDefinition, target_objects: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """构建封闭字段集的 `normalized_operation`。"""
    operation = {
        "operation_class": definition.operation_class,
        "side_effect": definition.side_effect,
        "idempotent": bool(definition.idempotent),
        "statement_kind": statement_kind_for(definition),
        "summary": build_operation_summary(definition, target_objects),
    }
    _validate_operation(operation)
    return operation


def _validate_operation(operation: Mapping[str, Any]) -> None:
    _require_keys(operation, OPERATION_KEYS, label="normalized_operation")
    if operation["operation_class"] not in _OPERATION_CLASSES:
        _fail("normalized_operation.operation_class 非法", value=operation["operation_class"])
    if operation["side_effect"] not in _SIDE_EFFECTS:
        _fail("normalized_operation.side_effect 非法", value=operation["side_effect"])
    if not isinstance(operation["idempotent"], bool):
        _fail("normalized_operation.idempotent 必须是布尔值")
    for key in ("statement_kind", "summary"):
        _require_text(operation[key], label=f"normalized_operation.{key}")


def build_action_descriptor(
    *,
    tenant_id: str,
    environment_id: str,
    db_engine: str,
    fingerprint: Mapping[str, Any],
    definition: ToolDefinition,
    normalized_operation: Mapping[str, Any],
    target_objects: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    policy_version: str,
    action_digest_version: str = ACTION_DIGEST_VERSION,
) -> dict[str, Any]:
    """构建 `action_digest` 的**唯一**输入结构（字段集封闭，键序由 JCS 决定）。"""
    _require_text(tenant_id, label="tenant_id")
    _require_text(environment_id, label="environment_id")
    _require_text(policy_version, label="policy_version")
    _require_text(action_digest_version, label="action_digest_version")
    if db_engine not in _ENGINES:
        _fail("db_engine 非法", value=db_engine)

    _require_keys(fingerprint, FINGERPRINT_KEYS, label="environment_fingerprint")
    if fingerprint["db_engine"] != db_engine:
        _fail(
            "environment_fingerprint.db_engine 与 db_engine 不一致",
            fingerprint_db_engine=fingerprint["db_engine"],
            db_engine=db_engine,
        )
    _validate_operation(normalized_operation)
    if not isinstance(parameters, Mapping):
        _fail("parameters 必须是对象")

    descriptor = {
        "action_digest_version": action_digest_version,
        "tenant_id": tenant_id,
        "environment_id": environment_id,
        "db_engine": db_engine,
        "environment_fingerprint": dict(fingerprint),
        "tool": {"name": definition.name, "version": definition.version},
        "normalized_operation": dict(normalized_operation),
        "target_objects": normalize_target_objects(target_objects),
        "parameters": dict(parameters),
        "policy_version": policy_version,
    }
    _require_keys(descriptor, DESCRIPTOR_KEYS, label="action_descriptor")
    _require_keys(descriptor["tool"], TOOL_KEYS, label="tool")
    return descriptor


def compute_action_digest(descriptor: Mapping[str, Any]) -> tuple[str, str, str]:
    """返回 (action_digest, parameters_digest, target_objects_digest)。

    分通道摘要用于定位"是参数变了、目标变了还是整体不符"，整体摘要仍是唯一门禁。
    """
    _require_keys(descriptor, DESCRIPTOR_KEYS, label="action_descriptor")
    _require_keys(descriptor["tool"], TOOL_KEYS, label="tool")
    _validate_operation(descriptor["normalized_operation"])
    _require_keys(
        descriptor["environment_fingerprint"], FINGERPRINT_KEYS, label="environment_fingerprint"
    )
    parameters = descriptor["parameters"]
    target_objects = descriptor["target_objects"]
    if not isinstance(parameters, Mapping):
        _fail("parameters 必须是对象")
    if not isinstance(target_objects, (list, tuple)):
        _fail("target_objects 必须是数组")
    return (
        canonical_digest(dict(descriptor)),
        canonical_digest(dict(parameters)),
        canonical_digest(list(target_objects)),
    )


def canonical_descriptor_text(descriptor: Mapping[str, Any]) -> str:
    """返回描述符的规范串（用于冻结文档逐字节比对与人工复核）。"""
    _require_keys(descriptor, DESCRIPTOR_KEYS, label="action_descriptor")
    return canonical_json(dict(descriptor))


# ---------------------------------------------------------------------------
# 前置条件摘要
# ---------------------------------------------------------------------------


def compute_prerequisites_digest(
    *, action_digest: str, checks: Sequence[Mapping[str, Any]]
) -> str:
    """由服务端探测结果生成 `prerequisites_digest`。

    - 人工输入（`operator_note`）**不得**进入本摘要；
    - `checks` 按 `check_id` 的 UTF-16 码元序排序，保证列举顺序不影响摘要。
    """
    _require_text(action_digest, label="action_digest")
    prepared: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, Mapping):
            _fail("prerequisites.checks 元素必须是对象", index=index)
        prepared.append(_normalize_check(check, index=index))
    prepared.sort(key=lambda item: item["check_id"].encode("utf-16-be"))
    return canonical_digest(
        {
            "prerequisites_digest_version": PREREQUISITES_DIGEST_VERSION,
            "action_digest": action_digest,
            "checks": prepared,
        }
    )


_CHECK_KEYS = ("check_id", "status", "observed", "source")
_CHECK_STATUSES = ("PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE")
_CHECK_SOURCES = ("server_probe", "control_db", "target_probe")


def _normalize_check(check: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    _require_keys(check, _CHECK_KEYS, label=f"prerequisites.checks[{index}]")
    check_id = _require_text(check["check_id"], label="prerequisites.check_id")
    status = check["status"]
    if status not in _CHECK_STATUSES:
        _fail("prerequisites.status 非法", value=status, index=index)
    source = check["source"]
    if source not in _CHECK_SOURCES:
        _fail("prerequisites.source 非法", value=source, index=index)
    observed = check["observed"]
    if observed is None:
        observed = {}
    if not isinstance(observed, Mapping):
        _fail("prerequisites.observed 必须是对象", index=index)
    return {
        "check_id": check_id,
        "status": status,
        "observed": dict(observed),
        "source": source,
    }


def digest_text(value: Any) -> str:
    """对任意值取规范化摘要（经唯一入口，供诊断/测试使用）。"""
    return canonical_digest(value)


def sha256_of_text(text: str) -> str:
    """对已规范文本取 SHA-256（供冻结文档校验）。"""
    return sha256_hex(text)


__all__ = [
    "ACTION_DIGEST_VERSION",
    "DESCRIPTOR_KEYS",
    "FINGERPRINT_KEYS",
    "OPERATION_KEYS",
    "PREREQUISITES_DIGEST_VERSION",
    "TARGET_OBJECT_KEYS",
    "build_action_descriptor",
    "build_normalized_operation",
    "build_operation_summary",
    "canonical_descriptor_text",
    "compute_action_digest",
    "compute_prerequisites_digest",
    "digest_text",
    "environment_fingerprint_digest",
    "environment_fingerprint_from_row",
    "extract_target_objects",
    "fingerprint_completeness",
    "normalize_target_objects",
    "normalize_tool_parameters",
    "sha256_of_text",
    "statement_kind_for",
]
