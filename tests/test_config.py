"""Configuration validation.

Every one of these is a real deployment failure. A bad value must produce one
readable line and exit code 2 — not a stack trace, and not a systemd restart
loop that hammers the journal because restarting cannot fix a typo.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from kbot.__main__ import EXIT_CONFIG, main
from kbot.config import ConfigError, load_settings


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """A minimal valid environment; tests break one thing at a time."""
    for name in list(
        [
            "KALSHI_SERIES", "SPOT_PRODUCTS", "MANUAL_PAY_ADDRESSES", "PRICES_USD",
            "ADMIN_IDS", "WEBHOOK_PORT", "PAYMENT_PROVIDER", "KALSHI_PRIVATE_KEY",
            "KALSHI_PRIVATE_KEY_PATH", "SCAN_INTERVAL_S", "RESULTS_CHAT_ID",
        ]
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DB_PATH", str(tmp_path / "k.sqlite3"))
    return monkeypatch


def test_a_minimal_environment_loads(env):
    settings = load_settings()
    assert settings.telegram_token == "123:abc"
    assert settings.series  # defaults applied
    assert settings.payment_provider == "manual"


def test_missing_token_is_a_config_error(env):
    env.delenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        load_settings()


def test_missing_master_key_names_the_generator(env):
    env.delenv("MASTER_KEY")
    with pytest.raises(RuntimeError, match="kbot.tools genkey"):
        load_settings()


def test_a_short_master_key_is_rejected(env):
    """Otherwise this surfaces as a ValueError from deep inside cryptography."""
    env.setenv("MASTER_KEY", "hunter2")
    with pytest.raises(ConfigError, match="too short"):
        load_settings()


def test_a_proper_fernet_key_is_used_verbatim(env):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    env.setenv("MASTER_KEY", key)
    assert load_settings().master_key == key


def test_a_long_random_secret_is_derived_into_a_usable_key(env):
    """One-click hosts generate their own secrets; those must work too."""
    from cryptography.fernet import Fernet

    env.setenv("MASTER_KEY", "a" * 40)
    key = load_settings().master_key
    Fernet(key.encode())  # usable


def test_key_derivation_is_stable_across_restarts(env):
    """Credentials encrypted before a redeploy must still decrypt after it."""
    env.setenv("MASTER_KEY", "some-host-generated-secret-value-1234")
    first = load_settings().master_key
    second = load_settings().master_key
    assert first == second


def test_different_secrets_derive_different_keys(env):
    env.setenv("MASTER_KEY", "x" * 40)
    a = load_settings().master_key
    env.setenv("MASTER_KEY", "y" * 40)
    assert load_settings().master_key != a


def test_malformed_series_json_says_what_was_expected(env):
    env.setenv("KALSHI_SERIES", "{not json")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "KALSHI_SERIES" in str(excinfo.value)
    assert "KXBTC15M" in str(excinfo.value)  # shows the expected shape


def test_series_must_be_an_object_not_a_list(env):
    env.setenv("KALSHI_SERIES", '["BTC"]')
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_settings()


def test_non_numeric_admin_ids_are_rejected(env):
    env.setenv("ADMIN_IDS", "alice,bob")
    with pytest.raises(ConfigError, match="numeric Telegram user IDs"):
        load_settings()


def test_admin_ids_accept_commas_and_spaces(env):
    env.setenv("ADMIN_IDS", "1, 2  3")
    assert load_settings().admin_ids == frozenset({1, 2, 3})


def test_port_out_of_range_is_rejected(env):
    env.setenv("WEBHOOK_PORT", "99999")
    with pytest.raises(ConfigError, match="1-65535"):
        load_settings()


def test_non_numeric_port_is_rejected(env):
    env.setenv("WEBHOOK_PORT", "eighty")
    with pytest.raises(ConfigError, match="whole number"):
        load_settings()


def test_unknown_payment_provider_is_rejected(env):
    env.setenv("PAYMENT_PROVIDER", "stripe")
    with pytest.raises(ConfigError, match="manual"):
        load_settings()


def test_a_missing_private_key_file_names_the_path(env):
    env.setenv("KALSHI_PRIVATE_KEY_PATH", "/definitely/not/here.pem")
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings()


def test_an_inline_private_key_survives_escaped_newlines(env, tmp_path):
    env.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN-----\\nbody\\n-----END-----")
    settings = load_settings()
    assert settings.md_private_key.count("\n") == 2


def test_malformed_prices_are_rejected(env):
    env.setenv("PRICES_USD", "twenty five dollars")
    with pytest.raises(ConfigError, match="PRICES_USD"):
        load_settings()


def test_valid_prices_override_the_defaults(env):
    env.setenv("PRICES_USD", '{"monthly": 149}')
    assert load_settings().prices["monthly"] == 149.0


def test_non_numeric_interval_is_rejected(env):
    env.setenv("SCAN_INTERVAL_S", "fast")
    with pytest.raises(ConfigError, match="must be a number"):
        load_settings()


# ---------------- the process contract ----------------


def test_a_config_error_exits_2_without_a_traceback(env, capsys):
    """systemd is told not to restart on 2, so this must be exact."""
    env.setenv("MASTER_KEY", "not-a-key")
    assert main() == EXIT_CONFIG == 2
    err = capsys.readouterr().err
    assert err.startswith("config error:")
    assert "Traceback" not in err


def test_a_missing_token_also_exits_2(env, capsys):
    env.delenv("TELEGRAM_BOT_TOKEN")
    assert main() == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_the_service_units_do_not_restart_on_a_config_error():
    """A typo in .env must stop the service, not crash-loop it."""
    from pathlib import Path

    for unit in Path("deploy").glob("*.service"):
        text = unit.read_text()
        assert "RestartPreventExitStatus=2" in text, unit.name
        assert "Restart=always" in text, unit.name
