"""Dashboard logic, exercised without touching the network."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.storage import DEFAULT_SETTINGS, Storage
from kbot.telegram import ui
from kbot.telegram.bot import SETTING_VIEWS, Bot


@pytest.fixture()
async def bot(tmp_path):
    settings = Settings(
        telegram_token="test",
        admin_ids=frozenset({99}),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "bot.sqlite3",
        demo=True,
        series={"BTC": "KXBTCD", "ETH": "KXETHD"},
        spot_products={},
    )
    storage = Storage(settings.db_path, settings.master_key)
    b = Bot(settings, storage)
    yield b
    await b.engine.stop()
    await b.tg.aclose()
    storage.close()


async def user(bot):
    await bot.storage.upsert_user(1, "trader")
    key = (await bot.storage.mint_keys("lifetime", 1))[0]
    await bot.storage.redeem_key(1, key)
    return await bot.storage.get_user(1)


async def test_every_setting_view_maps_to_a_real_setting():
    assert set(SETTING_VIEWS) <= set(DEFAULT_SETTINGS)


async def test_mode_and_strategy_toggles_persist(bot):
    u = await user(bot)
    assert await bot._apply_setting(u, "mode", "auto") == "Auto-execute"
    assert (await bot.storage.get_user(1)).get("mode") == "auto"

    await bot._apply_setting(u, "strategy", "fade")
    assert (await bot.storage.get_user(1)).get("strategy") == "fade"


async def test_unknown_strategy_is_ignored(bot):
    u = await user(bot)
    assert await bot._apply_setting(u, "strategy", "nonsense") is None
    assert (await bot.storage.get_user(1)).get("strategy") == "drift"


async def test_going_live_requires_credentials(bot):
    u = await user(bot)
    toast = await bot._apply_setting(u, "paper", "0")
    assert "Connect a Kalshi key" in toast
    assert (await bot.storage.get_user(1)).get("paper") is True

    await bot.storage.set_credentials(1, "key-id", "PEM")
    u = await bot.storage.get_user(1)
    assert await bot._apply_setting(u, "paper", "0") == "Live mode"
    assert (await bot.storage.get_user(1)).get("paper") is False


async def test_entry_band_cannot_invert(bot):
    u = await bot.storage.upsert_user(1, "trader")
    u = await bot.storage.update_settings(1, {"min_entry_price": 40})
    toast = await bot._apply_setting(u, "max_entry_price", "35")
    assert "40c floor" in toast
    assert (await bot.storage.get_user(1)).get("max_entry_price") == 65


async def test_confidence_is_stored_as_a_fraction(bot):
    u = await user(bot)
    await bot._apply_setting(u, "min_confidence", "70")
    assert (await bot.storage.get_user(1)).get("min_confidence") == 0.7


async def test_unknown_setting_is_rejected(bot):
    u = await user(bot)
    assert await bot._apply_setting(u, "not_a_setting", "1") is None


async def test_coin_toggle_adds_and_removes(bot):
    u = await user(bot)
    await bot._toggle_coin(u, "ETH")  # already on by default
    assert "ETH" not in (await bot.storage.get_user(1)).get("coins")

    u = await bot.storage.get_user(1)
    await bot._toggle_coin(u, "ETH")
    assert "ETH" in (await bot.storage.get_user(1)).get("coins")


async def test_unconfigured_coin_cannot_be_selected(bot):
    u = await user(bot)
    assert await bot._toggle_coin(u, "DOGE") == "Not available"


async def test_start_requires_access(bot):
    u = await bot.storage.upsert_user(1, "trader")
    assert "access" in (await bot._toggle_running(u, True))
    assert (await bot.storage.get_user(1)).enabled is False


async def test_start_requires_a_coin(bot):
    u = await user(bot)
    u = await bot.storage.update_settings(1, {"coins": []})
    assert "coin" in (await bot._toggle_running(u, True))


async def test_start_and_stop(bot):
    u = await user(bot)
    assert await bot._toggle_running(u, True) == "Running — paper"
    assert (await bot.storage.get_user(1)).enabled is True

    u = await bot.storage.get_user(1)
    assert await bot._toggle_running(u, False) == "Stopped"
    assert (await bot.storage.get_user(1)).enabled is False


async def test_live_start_requires_credentials(bot):
    u = await user(bot)
    u = await bot.storage.update_settings(1, {"paper": False})
    assert "Kalshi key" in (await bot._toggle_running(u, True))


async def test_every_view_renders(bot):
    u = await user(bot)
    for view in ("main", "coins", "strategy", "size", "exit", "risk", "positions"):
        text, markup = await bot._view(u, view)
        assert text and markup["inline_keyboard"]


async def test_dashboard_warns_without_access(bot):
    u = await bot.storage.upsert_user(1, "trader")
    text, _ = await bot._view(u, "main")
    assert "No active access" in text


async def test_admin_check(bot):
    u = await user(bot)
    assert bot._is_admin(u) is False
    admin = await bot.storage.upsert_user(99, "boss")
    assert bot._is_admin(admin) is True


def test_access_line_wording():
    class Fake:
        lifetime = True
        access_until = 0.0

    assert "lifetime" in ui.access_line(Fake())


# ---------------- kill switch over Telegram ----------------


async def _sent(bot):
    """Capture outgoing messages instead of calling Telegram."""
    out: list[tuple[int, str]] = []

    async def send(tg_id, text, **kwargs):
        out.append((tg_id, text))
        return {}

    bot.tg.send_message = send
    return out


async def test_only_an_admin_can_pull_the_kill_switch(bot):
    out = await _sent(bot)
    await bot.storage.upsert_user(1, "trader")
    ordinary = await bot.storage.get_user(1)

    await bot._cmd_kill(ordinary, [], {})

    assert not bot.engine.kill.engaged
    assert "Admins only" in out[-1][1]


async def test_an_admin_kill_engages_and_persists(bot):
    out = await _sent(bot)
    await bot.storage.upsert_user(99, "boss")
    admin = await bot.storage.get_user(99)

    await bot._cmd_kill(admin, ["market", "looks", "wrong"], {})

    state = bot.engine.kill.state()
    assert state.engaged
    assert "market looks wrong" in (state.reason or "")
    assert "KILL SWITCH ENGAGED" in out[-1][1]
    # Persisted, so a restart cannot resume trading on its own.
    assert bot.settings.kill_file.exists()


async def test_resume_clears_it(bot):
    out = await _sent(bot)
    await bot.storage.upsert_user(99, "boss")
    admin = await bot.storage.get_user(99)

    await bot._cmd_kill(admin, [], {})
    await bot._cmd_resume(admin, [], {})

    assert not bot.engine.kill.engaged
    assert "cleared" in out[-1][1]


async def test_resume_admits_when_it_cannot_clear_an_env_switch(bot, monkeypatch):
    """Reporting "resumed" while the switch is still engaged would be the most
    dangerous possible lie in this file."""
    out = await _sent(bot)
    await bot.storage.upsert_user(99, "boss")
    admin = await bot.storage.get_user(99)

    await bot._cmd_kill(admin, [], {})
    monkeypatch.setenv("KILL_SWITCH", "1")
    await bot._cmd_resume(admin, [], {})

    assert bot.engine.kill.engaged
    assert "Still engaged" in out[-1][1]


async def test_only_an_admin_can_resume(bot):
    out = await _sent(bot)
    await bot.storage.upsert_user(99, "boss")
    admin = await bot.storage.get_user(99)
    await bot._cmd_kill(admin, [], {})

    await bot.storage.upsert_user(1, "trader")
    ordinary = await bot.storage.get_user(1)
    await bot._cmd_resume(ordinary, [], {})

    assert bot.engine.kill.engaged, "a non-admin must not be able to resume"
    assert "Admins only" in out[-1][1]


async def test_health_names_the_mode_and_the_switch(bot):
    out = await _sent(bot)
    await bot.storage.upsert_user(1, "trader")
    who = await bot.storage.get_user(1)

    await bot._cmd_health(who, [], {})
    text = out[-1][1]
    assert "Mode:" in text and "Kill switch:" in text
    assert "PAPER" in text


# ---------------- /data ----------------
#
# The recorder has to run for days before any strategy question can be
# answered, so its status belongs somewhere checkable from a bus stop. The
# last run stopped after two hours and nobody noticed for a day and a half.


def _write_recordings(directory, *, markets=1, result="yes", t0=1_000_000.0):
    import gzip
    import json

    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(directory / "2020-01-01.jsonl.gz", "wt") as fh:
        for m in range(markets):
            ticker = f"KX{m}"
            for i in range(2):
                fh.write(json.dumps({
                    "type": "book", "t": t0 + m * 10 + i, "ticker": ticker,
                    "coin": "BTC", "o": t0, "c": t0 + 900,
                    "yes": [[500, 10]], "no": [[490, 10]],
                }) + "\n")
            fh.write(json.dumps(
                {"type": "settle", "ticker": ticker, "result": result}
            ) + "\n")
    return directory


def test_data_is_a_registered_command():
    from kbot.telegram.bot import COMMANDS

    assert any(name == "data" for name, _ in COMMANDS)


async def test_no_recordings_says_how_to_start(bot, tmp_path, monkeypatch):
    out = await _sent(bot)
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "empty"))
    u = await user(bot)
    await bot._cmd_data(u, [], {})
    text = out[-1][1]
    assert "Nothing recorded yet" in text
    assert "no API keys" in text


async def test_it_reports_the_recorder_as_stopped(bot, tmp_path, monkeypatch):
    """The failure that actually happened, and the one worth seeing first."""
    out = await _sent(bot)
    monkeypatch.setenv("RECORDINGS_DIR", str(_write_recordings(tmp_path / "rec")))
    u = await user(bot)
    await bot._cmd_data(u, [], {})
    text = out[-1][1]
    assert "not running" in text
    assert "settled markets" in text


async def test_an_unreadable_directory_still_answers(bot, monkeypatch):
    """A status command that raises tells you nothing at the moment you most
    wanted to know."""
    out = await _sent(bot)

    def boom(_):
        raise OSError("disk gone")

    monkeypatch.setattr("kbot.research.inventory.take_inventory", boom)
    u = await user(bot)
    await bot._cmd_data(u, [], {})
    assert "Could not read" in out[-1][1]


async def test_a_one_sided_sample_is_flagged(bot, tmp_path, monkeypatch):
    """Volume alone is not evidence: a sample that settled one way is one move
    counted many times."""
    out = await _sent(bot)
    monkeypatch.setenv(
        "RECORDINGS_DIR", str(_write_recordings(tmp_path / "rec", markets=10, result="no"))
    )
    u = await user(bot)
    await bot._cmd_data(u, [], {})
    assert "settled the same way" in out[-1][1]
