import pytest

from kbot.engine.broker import PaperBroker
from kbot.engine.discovery import MarketDiscovery, parse_ts
from kbot.kalshi.orderbook import OrderBook


# ---------------- paper broker ----------------


def book() -> OrderBook:
    b = OrderBook("KXBTC15M-1")
    b.apply_snapshot(yes=[["0.4500", "100.00"]], no=[["0.5200", "30.00"]])
    return b


@pytest.mark.asyncio
async def test_paper_buy_fills_at_the_ask():
    result = await PaperBroker().buy("KXBTC15M-1", "yes", 5, 600, book())
    assert result.ok
    # YES ask is $1.00 - $0.52 = 48c; we pay the ask, not our limit.
    assert result.price_dc == 480
    assert result.filled == 5


@pytest.mark.asyncio
async def test_paper_buy_is_capped_by_resting_size():
    result = await PaperBroker().buy("KXBTC15M-1", "yes", 100, 600, book())
    assert result.ok
    assert result.filled == 30  # only 30 contracts rest at the touch


@pytest.mark.asyncio
async def test_paper_buy_rejects_a_limit_below_the_ask():
    result = await PaperBroker().buy("KXBTC15M-1", "yes", 1, 400, book())
    assert not result.ok
    assert "below ask" in result.error


@pytest.mark.asyncio
async def test_paper_buy_needs_a_live_book():
    result = await PaperBroker().buy("KXBTC15M-1", "yes", 1, 600, OrderBook("KXBTC15M-1"))
    assert not result.ok


@pytest.mark.asyncio
async def test_paper_buy_rejects_a_side_with_no_offer():
    empty_no_side = OrderBook("KXBTC15M-1")
    empty_no_side.apply_snapshot(yes=[["0.4500", "10.00"]], no=[])
    result = await PaperBroker().buy("KXBTC15M-1", "yes", 1, 600, empty_no_side)
    assert not result.ok


@pytest.mark.asyncio
async def test_paper_sell_rests_rather_than_filling():
    result = await PaperBroker().sell("KXBTC15M-1", "yes", 5, 550, book())
    assert result.ok and result.filled == 0 and result.price_dc == 550


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
                "ticker": "KXBTC15M-DAILY",
                "open_time": now - 3600,
                "close_time": now + 40000,
                "title": "daily",
            },
            {
                "ticker": "KXBTC15M-LATER",
                "open_time": now + 600,
                "close_time": now + 1500,
                "title": "later",
            },
            {
                "ticker": "KXBTC15M-NOW",
                "open_time": now - 300,
                "close_time": now + 600,
                "title": "now",
            },
            # Already closed.
            {
                "ticker": "KXBTC15M-PAST",
                "open_time": now - 1800,
                "close_time": now - 900,
                "title": "past",
            },
        ]
    )
    discovery = MarketDiscovery(rest, {"BTC": "KXBTC15M"})
    markets = await discovery.refresh(["BTC"])
    assert markets["BTC"].ticker == "KXBTC15M-NOW"
    assert markets["BTC"].seconds_to_close() == pytest.approx(600, abs=5)
    assert markets["BTC"].window_seconds == pytest.approx(900, abs=5)


@pytest.mark.asyncio
async def test_discovery_skips_coins_with_no_series_configured():
    discovery = MarketDiscovery(FakeRest([]), {"BTC": "KXBTC15M"})
    assert await discovery.refresh(["DOGE"]) == {}


@pytest.mark.asyncio
async def test_discovery_keeps_the_last_market_when_a_request_fails():
    import time

    now = time.time()
    good = FakeRest(
        [{"ticker": "KXBTC15M-NOW", "open_time": now, "close_time": now + 900}]
    )
    discovery = MarketDiscovery(good, {"BTC": "KXBTC15M"})
    await discovery.refresh(["BTC"])

    class Broken:
        async def get_markets(self, **kwargs):
            raise RuntimeError("network down")

    discovery.rest = Broken()
    markets = await discovery.refresh(["BTC"])
    assert markets["BTC"].ticker == "KXBTC15M-NOW"
