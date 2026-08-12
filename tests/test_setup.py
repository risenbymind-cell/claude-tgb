"""The first-time setup wizard.

It writes the file that holds a bot token and the key protecting every user's
Kalshi credentials, so the things worth pinning are: it validates before
writing, it never silently overwrites, and the file it produces is not
world-readable.
"""

from __future__ import annotations

import stat

import pytest

from kbot.tools import cmd_setup


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point the wizard at a scratch tree instead of the real repo."""
    (tmp_path / "kbot").mkdir()
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        'const BOT = "YourBotUsername";\n'
    )
    fake_module = tmp_path / "kbot" / "tools.py"
    fake_module.write_text("")
    monkeypatch.setattr("kbot.tools.__file__", str(fake_module))
    return tmp_path


def drive(monkeypatch, answers: list[str], *, me: dict | None):
    """Feed the prompts, and stub the Telegram round trip."""
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(it))

    class FakeTG:
        def __init__(self, token):
            self.token = token

        async def get_me(self):
            if me is None:
                from kbot.telegram.api import TelegramError

                raise TelegramError("Unauthorized", 401)
            return me

        async def aclose(self):
            pass

    monkeypatch.setattr("kbot.telegram.api.TelegramClient", FakeTG)


GOOD_TOKEN = "123456:AAaaBBbbCCccDDddEEeeFFffGGgghhhhIIII"


def test_a_valid_run_writes_a_usable_env(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "987654321", "1"], me={"username": "mybot"})
    assert cmd_setup() == 0

    env = (sandbox / ".env").read_text()
    assert f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}" in env
    assert "ADMIN_IDS=987654321" in env
    assert "KALSHI_DEMO=false" in env
    assert "BOT_USERNAME=mybot" in env

    # The generated key must actually work as a Fernet key.
    from cryptography.fernet import Fernet

    key = next(l.split("=", 1)[1] for l in env.splitlines() if l.startswith("MASTER_KEY="))
    Fernet(key.encode())

    # And the whole thing must load as real settings.
    for line in env.splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_PATH", str(sandbox / "k.sqlite3"))
    from kbot.config import load_settings

    settings = load_settings()
    assert settings.admin_ids == frozenset({987654321})
    assert settings.demo is False


def test_the_env_file_is_not_world_readable(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "1", "1"], me={"username": "b"})
    cmd_setup()
    mode = (sandbox / ".env").stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_choosing_demo(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "1", "2"], me={"username": "b"})
    cmd_setup()
    assert "KALSHI_DEMO=true" in (sandbox / ".env").read_text()


def test_an_existing_env_is_never_clobbered(sandbox, monkeypatch):
    (sandbox / ".env").write_text("TELEGRAM_BOT_TOKEN=precious\n")
    drive(monkeypatch, [], me={"username": "b"})
    assert cmd_setup() == 1
    assert (sandbox / ".env").read_text() == "TELEGRAM_BOT_TOKEN=precious\n"


def test_force_replaces_it(sandbox, monkeypatch):
    (sandbox / ".env").write_text("old\n")
    drive(monkeypatch, [GOOD_TOKEN, "1", "1"], me={"username": "b"})
    assert cmd_setup("--force") == 0
    assert "old" not in (sandbox / ".env").read_text()


def test_a_token_telegram_rejects_is_not_accepted(sandbox, monkeypatch):
    # Rejected once, then the user gives up (StopIteration -> no more input).
    drive(monkeypatch, [GOOD_TOKEN], me=None)
    with pytest.raises(StopIteration):
        cmd_setup()
    assert not (sandbox / ".env").exists()


def test_a_malformed_token_is_caught_before_the_network(sandbox, monkeypatch):
    drive(monkeypatch, ["hunter2"], me={"username": "b"})
    with pytest.raises(StopIteration):
        cmd_setup()
    assert not (sandbox / ".env").exists()


def test_a_non_numeric_admin_id_is_rejected(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "alice"], me={"username": "b"})
    with pytest.raises(StopIteration):
        cmd_setup()


def test_the_site_is_pointed_at_the_bot(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "1", "1"], me={"username": "realbot"})
    cmd_setup()
    html = (sandbox / "site" / "index.html").read_text()
    assert 'const BOT = "realbot"' in html
    assert "YourBotUsername" not in html


def test_the_data_directory_is_created(sandbox, monkeypatch):
    drive(monkeypatch, [GOOD_TOKEN, "1", "1"], me={"username": "b"})
    cmd_setup()
    assert (sandbox / "data").is_dir()
