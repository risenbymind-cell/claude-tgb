"""Results channel and the performance breakdown.

The property that matters: the public record includes losses. A wins-only feed
is not evidence, so these tests pin the default behaviour.
"""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from kbot.storage import Storage
from kbot.telegram import ui
from kbot.telegram.channel import ChannelStats, ResultsChannel, format_post


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return {}


@pytest.fixture()
async def store(tmp_path):
    storage = Storage(tmp_path / "ch.sqlite3", Fernet.generate_key().decode())
    await storage.upsert_user(1, "trader")
    yield storage
    storage.close()


async def closed_trade(storage, *, exit_dc: int, entry_dc: int = 500, count: int = 10,
                       coin: str = "BTC", side: str = "yes", paper: bool = True,
                       opened_at: float | None = None):
    trade_id = await storage.record_entry(
        tg_id=1, ticker=f"KX{coin}15M-{exit_dc}-{entry_dc}", coin=coin, side=side,
        count=count, entry_price_dc=entry_dc, target_price_dc=entry_dc + 80,
        entry_fee_dc=20, paper=paper, entry_order_id=None, reason="t",
    )
    if opened_at is not None:
        await storage._run(
            lambda: (
                storage._conn.execute(
                    "UPDATE trades SET opened_at = ? WHERE id = ?", (opened_at, trade_id)
                ),
                storage._conn.commit(),
            )
        )
    return await storage.close_trade(trade_id, exit_price_dc=exit_dc, exit_fee_dc=20)


# ---------------- results channel ----------------


async def test_a_win_is_posted_with_the_running_tally(store):
    tg = FakeTelegram()
    channel = ResultsChannel(tg, store, "@results")
    trade = await closed_trade(store, exit_dc=580)

    await channel.post_trade(trade, "drift")

    assert len(tg.sent) == 1
    chat, text = tg.sent[0]
    assert chat == "@results"
    assert "CASHED OUT" in text
    assert "BTC UP" in text
    assert "Running:" in text


async def test_losses_are_posted_too_by_default(store):
    tg = FakeTelegram()
    channel = ResultsChannel(tg, store, "@results")
    trade = await closed_trade(store, exit_dc=200)  # a loss

    await channel.post_trade(trade, "drift")

    assert len(tg.sent) == 1
    assert "CLOSED RED" in tg.sent[0][1]


async def test_losses_can_be_suppressed_but_that_is_not_the_default(store):
    tg = FakeTelegram()
    channel = ResultsChannel(tg, store, "@results", post_losses=False)
    await channel.post_trade(await closed_trade(store, exit_dc=200), "drift")
    assert tg.sent == []

    # And the default really is to post them.
    assert ResultsChannel(tg, store, "@results").post_losses is True


async def test_nothing_is_posted_without_a_channel(store):
    tg = FakeTelegram()
    channel = ResultsChannel(tg, store, None)
    assert channel.enabled is False
    await channel.post_trade(await closed_trade(store, exit_dc=580), "drift")
    assert tg.sent == []


async def test_small_trades_can_be_filtered(store):
    tg = FakeTelegram()
    channel = ResultsChannel(tg, store, "@results", min_net_dc=1000)
    await channel.post_trade(await closed_trade(store, exit_dc=505), "drift")
    assert tg.sent == []


async def test_the_running_tally_counts_losses(store):
    channel = ResultsChannel(FakeTelegram(), store, "@results")
    await closed_trade(store, exit_dc=580)  # win
    await closed_trade(store, exit_dc=590)  # win
    await closed_trade(store, exit_dc=100)  # loss

    stats = await channel.running_stats(paper=True)
    assert stats.trades == 3
    assert stats.wins == 2
    assert round(stats.win_rate) == 67
    # The net is dominated by the loss — which is the point of publishing it.
    assert stats.net_dc < 0


async def test_a_telegram_failure_never_propagates(store):
    from kbot.telegram.api import TelegramError

    class Broken:
        async def send_message(self, *a, **k):
            raise TelegramError("chat not found", 400)

    channel = ResultsChannel(Broken(), store, "@results")
    # Must not raise: a channel misconfiguration cannot be allowed to break
    # the trading loop that called it.
    await channel.post_trade(await closed_trade(store, exit_dc=580), "drift")


def test_post_shows_gross_fees_and_net():
    from kbot.storage import Trade

    trade = Trade(
        id=1, tg_id=1, ticker="KXBTC15M-1", coin="BTC", side="yes", count=10,
        entry_price_dc=500, exit_price_dc=580, target_price_dc=580,
        status="closed", paper=False, opened_at=0.0, closed_at=1.0,
        entry_fee_dc=20, exit_fee_dc=20, pnl_dc=760, gross_pnl_dc=800,
        entry_order_id=None, exit_order_id=None, reason=None,
    )
    text = format_post(trade, "drift", ChannelStats(trades=5, wins=3, net_dc=1500))
    assert "gross +$0.80" in text
    assert "fees $0.04" in text
    assert "+$0.76</b> net" in text
    assert "5 trades · 3 wins" in text
    # No user is identifiable in a public post.
    assert "1" not in text.split("Running")[0].replace("KXBTC15M-1", "")[:20]


# ---------------- stats ----------------


async def test_stats_breakdown_by_coin(store):
    await closed_trade(store, exit_dc=580, coin="BTC")
    await closed_trade(store, exit_dc=590, coin="BTC")
    await closed_trade(store, exit_dc=100, coin="ETH")

    rows = await store.strategy_breakdown(1, 0)
    coins = {r[0]: r for r in rows}
    assert coins["BTC"][2] == 2  # trades
    assert coins["BTC"][3] == 2  # wins
    assert coins["ETH"][4] < 0  # net negative


async def test_outcome_counts_split_target_from_expiry(store):
    trade_id = await store.record_entry(
        tg_id=1, ticker="T1", coin="BTC", side="yes", count=1,
        entry_price_dc=500, target_price_dc=580, entry_fee_dc=0,
        paper=True, entry_order_id=None, reason=None,
    )
    await store.close_trade(trade_id, exit_price_dc=580, status="closed")
    trade_id = await store.record_entry(
        tg_id=1, ticker="T2", coin="BTC", side="yes", count=1,
        entry_price_dc=500, target_price_dc=580, entry_fee_dc=0,
        paper=True, entry_order_id=None, reason=None,
    )
    await store.close_trade(trade_id, exit_price_dc=0, status="expired")

    counts = await store.outcome_counts(1, 0)
    assert counts == {"closed": 1, "expired": 1}


async def test_hourly_breakdown_groups_by_utc_hour(store):
    # 03:00 and 15:00 UTC on a known day.
    await closed_trade(store, exit_dc=580, opened_at=1_780_000_000.0)
    await closed_trade(store, exit_dc=580, opened_at=1_780_000_000.0 + 12 * 3600)
    hourly = await store.hourly_breakdown(1, 0)
    assert len(hourly) == 2


async def test_stats_text_reports_net_and_drawdown(store):
    await closed_trade(store, exit_dc=580)
    await closed_trade(store, exit_dc=100)
    trades = await store.trades_since(1, 0)
    text = ui.stats_text(
        trades,
        await store.strategy_breakdown(1, 0),
        await store.hourly_breakdown(1, 0),
        await store.outcome_counts(1, 0),
        7,
    )
    assert "Trades <b>2</b>" in text
    assert "Max drawdown" in text
    assert "net of Kalshi trading fees" in text


async def test_stats_text_is_graceful_with_nothing_to_report(store):
    text = ui.stats_text([], [], {}, {}, 7)
    assert "No closed trades yet" in text


async def test_share_is_off_by_default(store):
    user = await store.get_user(1)
    assert user.get("share_results") is False
