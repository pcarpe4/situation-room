import pytest

from situation_room import config as config_mod
from situation_room.config import ConfigError

from .conftest import make_env


def load(**overrides):
    return config_mod.load(make_env("postgresql://x@/db", **overrides))


def test_valid_dev_config_loads():
    cfg = load()
    assert cfg.is_dev_signin
    assert cfg.database_url.startswith("postgresql+psycopg://")
    assert cfg.stale_after == 3


@pytest.mark.parametrize("key", ["SR_AUTH_MODE", "SR_ENVIRONMENT", "SR_TIMEZONE", "SR_PUBLIC_URL", "SR_SECRET_KEY", "SR_DATABASE_URL"])
def test_missing_required_key_is_named_and_its_value_is_not(key):
    with pytest.raises(ConfigError) as err:
        load(**{key: ""})
    assert err.value.code == "CONFIG_MISSING"
    assert err.value.key == key


def test_dev_signin_refused_in_production():
    with pytest.raises(ConfigError) as err:
        load(SR_ENVIRONMENT="production")
    assert err.value.key == "SR_AUTH_MODE"


def test_clock_override_refused_outside_dev():
    with pytest.raises(ConfigError) as err:
        load(SR_AUTH_MODE="oidc", SR_ENVIRONMENT="production", SR_CLOCK_OVERRIDE="2026-11-11T09:00")
    assert err.value.key == "SR_CLOCK_OVERRIDE"


def test_unbuilt_auth_modes_refuse_to_start():
    with pytest.raises(ConfigError) as err:
        load(SR_AUTH_MODE="oidc", SR_ENVIRONMENT="production", SR_PUBLIC_URL="https://board.example.com")
    assert err.value.code == "CONFIG_UNSUPPORTED"


@pytest.mark.parametrize(("key", "value"), [
    ("SR_TIMEZONE", "Mars/Olympus"),
    ("SR_PUBLIC_URL", "board.example.com"),
    ("SR_PUBLIC_URL", "http://board.example.com/some/path"),
    ("SR_SECRET_KEY", "short"),
    ("SR_STALE_AFTER", "0"),
    ("SR_STALE_AFTER", "three"),
    ("SR_CLOCK_OVERRIDE", "yesterday"),
])
def test_invalid_values_are_refused_by_key(key, value):
    with pytest.raises(ConfigError) as err:
        load(**{key: value})
    assert err.value.key == key
    assert value not in str(err.value)
