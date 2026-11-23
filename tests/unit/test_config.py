from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from core.config import Settings, get_settings
from core.errors import ConfigError


def _settings(**overrides: object) -> Settings:
    values = {
        "ro_dsn": "mysql://dg_ro:secret_ro@127.0.0.1:3306/dg_lab",
        "rw_dsn": "mysql://dg_rw:secret_rw@127.0.0.1:3306/dg_lab",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_settings_defaults_and_dsn_parsing() -> None:
    settings = _settings()
    assert settings.env == "dev"
    assert settings.log_level == "INFO"
    assert settings.database_id == "lab_mysql_01"
    assert settings.ro_mysql_dsn().user == "dg_ro"
    assert settings.ro_mysql_dsn().database == "dg_lab"
    assert "secret_ro" not in repr(settings.ro_dsn)


def test_settings_rejects_non_mysql_dsn() -> None:
    with pytest.raises(ValidationError):
        _settings(ro_dsn="postgresql://user:pass@localhost/db")


def test_settings_normalizes_log_level() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_settings_rejects_bad_log_level() -> None:
    with pytest.raises(ValidationError):
        _settings(log_level="TRACE")


def test_missing_config_reports_field_names_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DG_RO_DSN", raising=False)
    monkeypatch.delenv("DG_RW_DSN", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError) as excinfo:
            get_settings()
        assert "ro_dsn" in excinfo.value.details["fields"]
        assert "rw_dsn" in excinfo.value.details["fields"]
        assert "secret" not in str(excinfo.value)
    finally:
        get_settings.cache_clear()


def test_secretstr_is_used_for_dsns() -> None:
    settings = _settings()
    assert isinstance(settings.ro_dsn, SecretStr)
    assert settings.ro_dsn.get_secret_value().endswith("/dg_lab")


def test_dev_local_auth_is_default_outside_prod() -> None:
    settings = _settings(env="dev")
    assert settings.auth_mode == "dev_local"


def test_prod_rejects_dev_local_auth_bypass() -> None:
    with pytest.raises(ValidationError):
        _settings(env="prod", auth_mode="dev_local")


def test_prod_requires_gateway_signing_key() -> None:
    with pytest.raises(ValidationError):
        _settings(env="prod", auth_mode="token")


def test_prod_token_mode_with_signing_key_is_valid() -> None:
    settings = _settings(
        env="prod",
        auth_mode="token",
        gateway_signing_key=SecretStr("prod-signing-key"),
    )
    assert settings.env == "prod"
    assert settings.gateway_key() == b"prod-signing-key"

