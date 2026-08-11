"""End-to-end engine tests against injected market state.

No network: the market feed and discovery results are stubbed, so these drive
the real tick loop, risk gate, paper broker and position manager.
"""

from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from kbot.config import Settings
from kbot.engine.discovery import LiveMarket
from kbot.engine.runner import Engine
from kbot.kalshi.orderbook import OrderBook
from kbot.storage import Storage


def make_settings(tmp_path) -> Settings:
    return Settings(
        telegram_token="test",
        admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "engine.sqlite3",
        demo=True,
        series={"BTC": "KXBTCD"},
        spot_products={},
    )


class Harness:
    def __init__(self, tmp_path):
        self.settings = make_settings(tmp_path)
        self.storage = Storage(self.settings.db_path, self.settings.master_key)
        self.messages: list[tuple[int, str]] = []
        self.engine = Engine(self.settings, self.storage, self.notify)

    async def notify(self, tg_id: int, text: str) -> None:
        self.messages.append((tg_id, text))

    async def setup_user(self, **settings):
        await self.storage.upsert_user(1, "trader")
        key = (await self.storage.mint_keys("lifetime", 1))[0]
        await self.storage.redeem_key(1, key)
        defaults = {"coins": ["BTC"], "min_confidence": 0.3, "paper": True}
        defaults.update(settings)
        await self.storage.update_settings(1, defaults)
        await self.storage.set_enabled(1, True)

    def place_market(self, seconds_left: float = 400.0) -> LiveMarket:
        now = time.time()
        market = LiveMarket(
            coin="BTC",
            ticker="KXBTCD-NOW",
            title="BTC up or down",
            open_time=now - (900 - seconds_left),
            close_time=now + seconds_left,
        )
        self.engine.discovery.markets = {"BTC": market}
        return market

    def set_book(self, yes_depth=250, no_depth=60, yes_bid=45, no_bid=52):
        book = OrderBook("KXBTCD-NOW")
        book.apply_snapshot(yes=[[yes_bid, yes_depth]], no=[[no_bid, no_depth]])
        self.engine.feed.books["KXBTCD-NOW"] = book
        return book

    def seed_rising_history(self):
        """Fill fair-value history with a clean upward drift."""
        now = time.time()
        for i in range(40):
            self.engine.history.observe(
                "KXBTCD-NOW", 40.0 + i * 0.2, now - (40 - i) * 1.5
            )

    async def close(self):
        await self.engine.stop()
        self.storage.close()


@pytest.fixture()
async def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    await h.close()


async def test_manual_mode_sends_a_signal_and_places_no_trade(harness):
    await harness.setup_user(mode="manual")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    assert len(harness.messages) == 1
    _, text = harness.messages[0]
    assert "BTC UP" in text
    assert "Buy <b>YES</b>" in text
    assert await harness.storage.open_trades(1) == []


async def test_auto_mode_opens_a_paper_position(harness):
    await harness.setup_user(mode="auto", contracts=2)
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    trades = await harness.storage.open_trades(1)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.paper is True
    assert trade.side == "yes"
    assert trade.count == 2
    assert trade.entry_price == 48  # 100 - best NO bid
    assert trade.target_price == 48 + 8  # default profit_cents
    assert trade.exit_order_id is not None  # exit resting immediately
    assert "PAPER" in harness.messages[0][1]


async def test_only_one_signal_per_market_window(harness):
    await harness.setup_user(mode="auto")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()
    await harness.engine._tick()
    await harness.engine._tick()

    assert len(await harness.storage.open_trades(1)) == 1


async def test_confidence_floor_suppresses_weak_signals(harness):
    await harness.setup_user(mode="auto", min_confidence=0.99)
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    assert await harness.storage.open_trades(1) == []
    assert harness.messages == []


async def test_risk_block_is_reported_not_silent(harness):
    # An entry cap below the ask blocks the trade at the risk gate.
    await harness.setup_user(mode="auto", max_entry_price=30)
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    assert await harness.storage.open_trades(1) == []
    assert "Skipped" in harness.messages[0][1]


async def test_disabled_user_is_never_traded(harness):
    await harness.setup_user(mode="auto")
    await harness.storage.set_enabled(1, False)
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    assert await harness.storage.open_trades(1) == []


async def test_expired_access_is_never_traded(harness):
    await harness.setup_user(mode="auto")
    await harness.storage._run(
        lambda: (
            harness.storage._conn.execute(
                "UPDATE users SET lifetime = 0, access_until = 0 WHERE tg_id = 1"
            ),
            harness.storage._conn.commit(),
        )
    )
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    assert await harness.storage.open_trades(1) == []


async def test_position_closes_when_the_target_is_bid(harness):
    await harness.setup_user(mode="auto")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()
    await harness.engine._tick()

    trade = (await harness.storage.open_trades(1))[0]
    # Someone now bids at our target on the YES side.
    harness.set_book(yes_bid=trade.target_price, no_bid=100 - trade.target_price - 1)

    await harness.engine._manage_positions()

    assert await harness.storage.open_trades(1) == []
    closed = (await harness.storage.trades_since(1, 0))[0]
    assert closed.status == "closed"
    assert closed.exit_price == trade.target_price
    assert closed.pnl_cents == (trade.target_price - trade.entry_price) * trade.count
    assert "✅" in harness.messages[-1][1]


async def test_position_stays_open_below_the_target(harness):
    await harness.setup_user(mode="auto")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()
    await harness.engine._tick()

    trade = (await harness.storage.open_trades(1))[0]
    harness.set_book(yes_bid=trade.target_price - 3)

    await harness.engine._manage_positions()

    assert len(await harness.storage.open_trades(1)) == 1


async def test_expired_window_settles_from_the_market_result(harness):
    await harness.setup_user(mode="auto")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()
    await harness.engine._tick()
    trade = (await harness.storage.open_trades(1))[0]

    # The window rolls: a new market becomes current and the old one settles NO.
    harness.engine.discovery.markets = {}

    async def fake_get_market(ticker):
        assert ticker == trade.ticker
        return {"status": "finalized", "result": "no"}

    harness.engine.public.get_market = fake_get_market

    await harness.engine._manage_positions()

    closed = (await harness.storage.trades_since(1, 0))[0]
    assert closed.status == "expired"
    assert closed.exit_price == 0  # held YES, market settled NO
    assert closed.pnl_cents == -trade.entry_price * trade.count
    assert "❌" in harness.messages[-1][1]


async def test_unsettled_expired_market_is_left_alone(harness):
    await harness.setup_user(mode="auto")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()
    await harness.engine._tick()

    harness.engine.discovery.markets = {}

    async def pending(ticker):
        return {"status": "closed", "result": ""}

    harness.engine.public.get_market = pending

    await harness.engine._manage_positions()

    assert len(await harness.storage.open_trades(1)) == 1


async def test_live_mode_without_credentials_stops_the_user(harness):
    await harness.setup_user(mode="auto", paper=False)
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()

    await harness.engine._tick()

    user = await harness.storage.get_user(1)
    assert user.enabled is False
    assert "credentials" in harness.messages[0][1]
    assert await harness.storage.open_trades(1) == []


async def test_signal_cache_is_pruned_when_the_window_rolls(harness):
    await harness.setup_user(mode="manual")
    harness.place_market()
    harness.set_book()
    harness.seed_rising_history()
    await harness.engine._tick()
    assert harness.engine._signalled

    harness.engine.discovery.markets = {}
    harness.engine._prune_signal_cache()
    assert not harness.engine._signalled
    assert harness.engine.history.tickers() == []
