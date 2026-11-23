"""W08 AC-05（端口自身）：封闭白名单、无 SQL 入参、独立只读 client。

`tests/integration/test_w08_verifier_readback.py` 的 Verifier 侧三条断言
（read_only / id 不同 / 篡改 result 不影响判定）依赖 T03 的 Verifier，归 T03 建；
本卡只覆盖端口与适配器自身，不硬凑 Verifier。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from adapters.mysql.readback import MysqlReadbackAdapter
from core.errors import ErrorCode, RequestInvalidError
from core.verification.readback import (
    READBACK_TARGETS,
    READBACK_TARGET_NOT_ALLOWED,
    ReadbackPort,
    ReadbackSample,
    TargetObject,
)


class _FakeReadOnlyClient:
    def __init__(self, *, read_only: bool = True, value: Any = "repaired") -> None:
        self.read_only = read_only
        self._value = value
        self._rows: list[dict[str, Any]] | None = None
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []

    def fetch_all(
        self, sql: str, params: tuple[Any, ...] | None = None, *, max_rows: int | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if self._rows is not None:
            return list(self._rows)
        return [{"readback_value": self._value}]

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> int:
        raise AssertionError("回读端口不得执行任何写语句")


def _target(**overrides: Any) -> TargetObject:
    base: dict[str, Any] = {
        "database": "dg_lab", "table": "seed_orders",
        "key_column": "order_id", "key_value": 2001, "column": "note",
    }
    base.update(overrides)
    return TargetObject(**base)


# ---- 封闭白名单 ----------------------------------------------------------
def test_readback_targets_is_closed() -> None:
    assert READBACK_TARGETS == frozenset({("dg_lab", "seed_orders", "order_id", "note")})
    assert _target().key_value == 2001


@pytest.mark.parametrize(
    "overrides",
    [
        {"database": "evil_db"},
        {"table": "other_table"},
        {"key_column": "1; DROP TABLE x"},
        {"column": "note; --"},
    ],
)
def test_target_object_rejects_non_whitelisted_quadruple(overrides: dict[str, Any]) -> None:
    with pytest.raises(RequestInvalidError) as exc:
        _target(**overrides)
    assert exc.value.code is ErrorCode.REQUEST_INVALID
    assert exc.value.details["reason"] == READBACK_TARGET_NOT_ALLOWED


def test_target_object_requires_integer_primary_key() -> None:
    with pytest.raises(ValueError):
        _target(key_value="2001")


# ---- 端口签名：不接受 SQL / host / port / DSN -----------------------------
def test_readback_port_has_no_sql_entrypoint() -> None:
    params = list(inspect.signature(ReadbackPort.read_target).parameters)
    assert params == ["self", "target"]
    fields = set(inspect.signature(TargetObject).parameters)
    assert not fields & {"sql", "query", "host", "port", "dsn", "statement"}


def test_adapter_satisfies_protocol() -> None:
    adapter = MysqlReadbackAdapter(_FakeReadOnlyClient())  # type: ignore[arg-type]
    assert isinstance(adapter, ReadbackPort)


def test_adapter_rejects_arbitrary_input() -> None:
    adapter = MysqlReadbackAdapter(_FakeReadOnlyClient())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        adapter.read_target("SELECT note FROM dg_lab.seed_orders")  # type: ignore[arg-type]


# ---- 独立只读 client -----------------------------------------------------
def test_adapter_is_read_only_and_has_distinct_identity() -> None:
    ro_client = _FakeReadOnlyClient(read_only=True)
    execution_client = _FakeReadOnlyClient(read_only=False)
    adapter = MysqlReadbackAdapter(ro_client)  # type: ignore[arg-type]
    assert adapter.read_only is True
    assert adapter.client_identity() == id(ro_client)
    assert adapter.client_identity() != id(execution_client)


def test_adapter_rejects_writable_client() -> None:
    with pytest.raises(ValueError):
        MysqlReadbackAdapter(_FakeReadOnlyClient(read_only=False))  # type: ignore[arg-type]


def test_adapter_uses_fixed_sql_with_bound_primary_key() -> None:
    client = _FakeReadOnlyClient()
    adapter = MysqlReadbackAdapter(client)  # type: ignore[arg-type]
    adapter.read_target(_target())
    assert len(client.calls) == 1
    sql, params = client.calls[0]
    assert sql.lstrip().upper().startswith("SELECT")
    assert "UPDATE" not in sql.upper() and "DELETE" not in sql.upper()
    assert "2001" not in sql  # 主键值只走绑定参数，不拼进 SQL
    assert params == (2001,)


def test_adapter_returns_sample_with_id_and_timestamp() -> None:
    adapter = MysqlReadbackAdapter(_FakeReadOnlyClient(value="repaired"))  # type: ignore[arg-type]
    sample = adapter.read_target(_target())
    assert isinstance(sample, ReadbackSample)
    assert sample.value == "repaired"
    assert sample.sample_id
    assert sample.collected_at is not None


def test_adapter_missing_row_is_none_value() -> None:
    client = _FakeReadOnlyClient()
    client._rows = []
    adapter = MysqlReadbackAdapter(client)  # type: ignore[arg-type]
    sample = adapter.read_target(_target())
    assert sample.value is None
