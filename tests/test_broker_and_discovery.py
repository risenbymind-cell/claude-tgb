import pytest

from kbot.engine.broker import PaperBroker
from kbot.engine.discovery import MarketDiscovery, parse_ts
from kbot.kalshi.orderbook import OrderBook


# ---------------- paper broker ----------------


def book() -> OrderBook:
    b = OrderBook("KXBTCD-1")
    b.apply_snapshot(yes=[[45, 100]], no=[[52, 30]])
    return b


@pytest.mark.asyncio
async def test_paper_buy_fills_at_the_ask():
    result = await PaperBroker().buy("KXBTCD-1", "yes", 5, 60, book())
    assert result.ok
    # YES ask is 100 - 52 = 48; we pay the ask, not our limit.
    assert result.price == 48
    assert result.filled == 5


@pytest.mark.asyncio
async def test_paper_buy_is_capped_by_resting_size():
    result = await PaperBroker().buy("KXBTCD-1", "yes", 100, 60, book())
    assert result.ok
    assert result.filled == 30  # only 30 contracts rest at the touch


@pytest.mark.asyncio
async def test_paper_buy_rejects_a_limit_below_the_ask():
    result = await PaperBroker().buy("KXBTCD-1", "yes", 1, 40, book())
    assert not result.ok
    assert "below ask" in result.error


@pytest.mark.asyncio
async def test_paper_buy_needs_a_live_book():
    result = await PaperBroker().buy("KXBTCD-1", "yes", 1, 60, OrderBook("KXBTCD-1"))
    assert not result.ok


@pytest.mark.asyncio
async def test_paper_buy_rejects_a_side_with_no_offer():
    empty_no_side = OrderBook("KXBTCD-1")
    empty_no_side.apply_snapshot(yes=[[45, 10]], no=[])
    result = await PaperBroker().buy("KXBTCD-1", "yes", 1, 60, empty_no_side)
    assert not result.ok


@pytest.mark.asyncio
async def test_paper_sell_rests_rather_than_filling():
    result = await PaperBroker().sell("KXBTCD-1", "yes", 5, 55, book())
    assert result.ok and result.filled == 0 and result.price == 55


@pytest.mark.asyncio
async def test_paper_balance_is_unconstrained():
    assert await PaperBroker().balance() is None


# ---------------- timestamps ----------------


def test_parse_ts_handles_rfc3339_z():
    import datetime

    expected = datetime.datetime(
        2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc
    ).timestamp()
    assert parse_ts("2026-08-11T12:00:00Z") == expected


def test_parse_ts_handles_offsets_and_epochs():
    assert parse_ts("2026-08-11T12:00:00+00:00") == parse_ts("2026-08-11T12:00:00Z")
    # A naive stamp is read as UTC, not local time.
    assert parse_ts("2026-08-11T12:00:00") == parse_ts("2026-08-11T12:00:00Z")
    assert parse_ts(1786536000) == 1786536000.0


def test_parse_ts_returns_none_on_junk():
    assert parse_ts(None) is None
    assert parse_ts("") is None
    assert parse_ts("not a date") is None


# ---------------- discovery ----------------


class FakeRest:
    def __init__(self, markets):
        self.markets = markets

    async def get_markets(self, series_ticker=None, status="open", limit=200, cursor=None):
        return {"markets": self.markets}


@pytest.mark.asyncio
async def test_discovery_picks_the_soonest_fifteen_minute_window():
    import time

    now = time.time()
    rest = FakeRest(
        [
            # A daily market — wrong window length, must be ignored.
            {
                "ticker": "KXBTCD-DAILY",
                "open_time": now - 3600,
                "close_time": now + 40000,
                "title": "daily",
            },
            {
                "ticker": "KXBTCD-LATER",
                "open_time": now + 600,
                "close_time": now + 1500,
                "title": "later",
            },
            {
                "ticker": "KXBTCD-NOW",
                "open_time": now - 300,
                "close_time": now + 600,
                "title": "now",
            },
            # Already closed.
            {
                "ticker": "KXBTCD-PAST",
                "open_time": now - 1800,
                "close_time": now - 900,
                "title": "past",
            },
        ]
    )
    discovery = MarketDiscovery(rest, {"BTC": "KXBTCD"})
    markets = await discovery.refresh(["BTC"])
    assert markets["BTC"].ticker == "KXBTCD-NOW"
    assert markets["BTC"].seconds_to_close() == pytest.approx(600, abs=5)
    assert markets["BTC"].window_seconds == pytest.approx(900, abs=5)


@pytest.mark.asyncio
async def test_discovery_skips_coins_with_no_series_configured():
    discovery = MarketDiscovery(FakeRest([]), {"BTC": "KXBTCD"})
    assert await discovery.refresh(["DOGE"]) == {}


@pytest.mark.asyncio
async def test_discovery_keeps_the_last_market_when_a_request_fails():
    import time

    now = time.time()
    good = FakeRest(
        [{"ticker": "KXBTCD-NOW", "open_time": now, "close_time": now + 900}]
    )
    discovery = MarketDiscovery(good, {"BTC": "KXBTCD"})
    await discovery.refresh(["BTC"])

    class Broken:
        async def get_markets(self, **kwargs):
            raise RuntimeError("network down")

    discovery.rest = Broken()
    markets = await discovery.refresh(["BTC"])
    assert markets["BTC"].ticker == "KXBTCD-NOW"
