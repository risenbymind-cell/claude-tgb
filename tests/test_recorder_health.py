"""The recorder, as a thing that has to run unattended for days.

Its correctness while running is covered elsewhere. This is about the failure
that actually happened: it stopped, and nothing said so. A day and a half of
silence that nobody could attribute until the recordings were counted
afterwards.

Two mechanisms come out of that. The recorder says something on *success*,
periodically, so an operator can tell a running process from a stopped one in
a log viewer. And it says something much louder when a period passes with
nothing captured, because a calm log during silent failure is what made the
original incident invisible.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.research.recorder import Recorder
from kbot.safety import TradingMode


@pytest.fixture()
def recorder(tmp_path):
    settings = Settings(
        telegram_token="", admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "d.sqlite3", demo=False,
        mode=TradingMode.PAPER,
        series={"BTC": "KXBTC15M"}, spot_products={},
    )
    return Recorder(settings, tmp_path / "recordings", coins=["BTC"], interval=1.0)


async def one_status_pass(recorder, caplog, *, written: int, markets: int = 0):
    """Run exactly one iteration of the status loop."""
    recorder.STATUS_INTERVAL_S = 0.01
    recorder.writer.written = written
    for i in range(markets):
        recorder.discovery.markets[f"C{i}"] = object()

    with caplog.at_level(logging.INFO, logger="kbot.research.recorder"):
        task = asyncio.create_task(recorder._status_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return caplog.records


# ---------------- it says something on success ----------------


async def test_it_reports_progress_while_healthy(recorder, caplog):
    """A process that logs only on failure is indistinguishable, in a
    platform's log viewer, from a process that is not running."""
    records = await one_status_pass(recorder, caplog, written=500, markets=9)
    assert records, "a healthy recorder must still say so"
    message = records[0].getMessage()
    assert "recording:" in message
    assert "9 markets live" in message
    assert "500" in message


async def test_the_status_line_carries_a_rate_not_just_a_total(recorder, caplog):
    """A cumulative total that stops climbing is the symptom worth spotting,
    and a bare total hides it."""
    records = await one_status_pass(recorder, caplog, written=500)
    assert "/s" in records[0].getMessage()


async def test_a_healthy_pass_is_not_an_error(recorder, caplog):
    records = await one_status_pass(recorder, caplog, written=500)
    assert records[0].levelno == logging.INFO


# ---------------- it shouts when it is not capturing ----------------


async def test_capturing_nothing_is_logged_as_an_error(recorder, caplog):
    """The exact incident this exists for: alive, calm, and recording
    nothing."""
    records = await one_status_pass(recorder, caplog, written=0)
    assert records
    assert records[0].levelno >= logging.ERROR
    assert "NOTHING CAPTURED" in records[0].getMessage()


async def test_a_stalled_counter_is_caught_even_after_a_good_start(recorder, caplog):
    """Records written long ago do not make the present healthy."""
    recorder._last_written = 1_000
    records = await one_status_pass(recorder, caplog, written=1_000)
    assert "NOTHING CAPTURED" in records[0].getMessage()


async def test_progress_after_a_stall_returns_to_normal(recorder, caplog):
    recorder._last_written = 1_000
    records = await one_status_pass(recorder, caplog, written=1_200)
    assert "NOTHING CAPTURED" not in records[0].getMessage()
    assert records[0].levelno == logging.INFO


# ---------------- the interval ----------------


def test_the_status_interval_suits_a_process_that_runs_for_weeks(recorder):
    """Often enough to notice a stall the same day; rare enough that weeks of
    it will not fill a disk."""
    assert 60 <= Recorder.STATUS_INTERVAL_S <= 900


def test_the_status_loop_is_one_of_the_running_tasks():
    """Registered alongside the others, so it cannot be started and then
    quietly not awaited."""
    import inspect

    src = inspect.getsource(Recorder.run)
    assert "_status_loop" in src


# ---------------- no credentials required ----------------


def test_recording_needs_no_credentials(tmp_path, monkeypatch):
    """Kalshi's books are public. Demanding a key, a token or an encryption
    secret to read them would put a setup wizard in front of the one thing
    that has to start before anything else is configured."""
    for name in (
        "TELEGRAM_BOT_TOKEN", "MASTER_KEY", "ADMIN_IDS",
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    from kbot.config import load_settings

    settings = load_settings(require_bot=False)
    assert settings.has_market_data_creds is False

    recorder = Recorder(settings, tmp_path / "rec", coins=["BTC"])
    assert recorder._signer is None, "recording must not require a signer"


def test_the_recorder_never_places_orders(tmp_path):
    """It has no broker at all -- not a disabled one."""
    import inspect

    src = inspect.getsource(Recorder)
    for forbidden in ("create_order", "LiveBroker", "PaperBroker", ".buy(", ".sell("):
        assert forbidden not in src, forbidden


# ---------------- deployment ----------------


def test_the_standalone_compose_file_restarts_on_reboot():
    """A recorder that does not survive a reboot is how you end up with 2%
    uptime and no idea why."""
    text = Path("docker-compose.recorder.yml").read_text()
    assert "restart: unless-stopped" in text


def test_the_standalone_compose_file_persists_the_recordings():
    text = Path("docker-compose.recorder.yml").read_text()
    assert "/app/data" in text
    assert "volumes:" in text


def test_the_standalone_compose_file_shuts_down_gracefully():
    """SIGTERM closes the gzip file cleanly; killed instead, the last block is
    lost and the day's file can end mid-record."""
    text = Path("docker-compose.recorder.yml").read_text()
    assert "SIGTERM" in text
    assert "stop_grace_period" in text


def test_the_hosted_recorder_configs_do_not_scale_to_zero():
    """Free tiers idle out on no inbound traffic, and the recorder has none by
    design -- so it looks permanently idle and gets stopped."""
    fly = Path("fly.recorder.toml").read_text()
    assert "auto_stop_machines" not in fly or "auto_stop_machines = false" in fly

    render = Path("render.yaml").read_text()
    assert "plan: free" not in render
