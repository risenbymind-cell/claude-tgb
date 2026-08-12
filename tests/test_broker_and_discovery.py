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


# ---------------- durable order intent ----------------


class _FakeIntents:
    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.resolved: list[tuple[str, dict]] = []

    async def record(self, **kwargs) -> None:
        self.recorded.append(kwargs)

    async def resolve(self, client_order_id: str, **kwargs) -> None:
        self.resolved.append((client_order_id, kwargs))


class _FakeClient:
    def __init__(self, result=None, raises=None) -> None:
        self.result, self.raises = result, raises
        self.calls: list[dict] = []

    async def create_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


async def test_intent_is_persisted_before_the_order_is_sent():
    """The order of these two operations is the entire point. Reversed, a
    crash in between leaves a live position nobody knows about."""
    from kbot.engine.broker import LiveBroker

    intents = _FakeIntents()
    order = {"fill_count": 1, "order_id": "abc", "avg_price_dc": 550, "fee_dc": 10}
    client = _FakeClient(result=order)
    broker = LiveBroker(client, intents=intents)

    await broker.buy("KXBTC15M-X", "yes", 1, 550, None)

    assert intents.recorded, "nothing was persisted before submission"
    coid = intents.recorded[0]["client_order_id"]
    # The id written to disk must be the id actually sent.
    assert client.calls[0]["client_order_id"] == coid


async def test_a_filled_order_resolves_its_intent():
    from kbot.engine.broker import LiveBroker

    intents = _FakeIntents()
    broker = LiveBroker(
        _FakeClient(result={"fill_count": 2, "order_id": "o1", "avg_price_dc": 500,
                            "fee_dc": 7}),
        intents=intents,
    )
    result = await broker.buy("T", "yes", 2, 500, None)

    assert result.ok and result.filled == 2
    _, fields = intents.resolved[0]
    assert fields["status"] == "filled"
    assert fields["filled"] == 2
    assert fields["order_id"] == "o1"


async def test_a_4xx_rejection_is_recorded_as_rejected():
    """The exchange answered and said no, so the case is closed."""
    from kbot.engine.broker import LiveBroker
    from kbot.kalshi.rest import KalshiError

    intents = _FakeIntents()
    broker = LiveBroker(_FakeClient(raises=KalshiError(400, "bad price")),
                        intents=intents)

    result = await broker.buy("T", "yes", 1, 500, None)
    assert not result.ok
    assert intents.resolved[0][1]["status"] == "rejected"


async def test_a_transport_failure_is_recorded_as_unknown_not_rejected():
    """A lost response is not a refusal. Recording it as rejected would close
    the case on an order that may be live, which is the one outcome this
    machinery exists to prevent."""
    import httpx

    from kbot.engine.broker import LiveBroker

    intents = _FakeIntents()
    broker = LiveBroker(_FakeClient(raises=httpx.ReadTimeout("timed out")),
                        intents=intents)

    result = await broker.buy("T", "yes", 1, 500, None)
    assert not result.ok
    assert intents.resolved[0][1]["status"] == "unknown"


async def test_a_5xx_is_also_unknown():
    from kbot.engine.broker import LiveBroker
    from kbot.kalshi.rest import KalshiError

    intents = _FakeIntents()
    broker = LiveBroker(_FakeClient(raises=KalshiError(503, "unavailable")),
                        intents=intents)
    await broker.buy("T", "yes", 1, 500, None)
    assert intents.resolved[0][1]["status"] == "unknown"


async def test_every_order_gets_a_distinct_client_order_id():
    from kbot.engine.broker import LiveBroker

    intents = _FakeIntents()
    broker = LiveBroker(
        _FakeClient(result={"fill_count": 1, "order_id": "o", "avg_price_dc": 500}),
        intents=intents,
    )
    for _ in range(25):
        await broker.buy("T", "yes", 1, 500, None)

    ids = [r["client_order_id"] for r in intents.recorded]
    assert len(set(ids)) == len(ids)


async def test_the_broker_still_works_without_an_intent_log():
    """Paper mode and tests must not require a database."""
    from kbot.engine.broker import LiveBroker

    broker = LiveBroker(
        _FakeClient(result={"fill_count": 1, "order_id": "o", "avg_price_dc": 500})
    )
    assert (await broker.buy("T", "yes", 1, 500, None)).ok
