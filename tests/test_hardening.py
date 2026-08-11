"""Rate limiting and ledger/exchange reconciliation.

These cover the failure modes that only appear with real money on the line: a
restart mid-position, an exit that filled while the bot was down, a position
closed by hand, and hitting the exchange's rate limit at the worst moment.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.fernet import Fernet

from kbot.engine.reconcile import Reconciler, cancel_orphan_orders
from kbot.kalshi.throttle import TIERS, Throttle, TokenBucket
from kbot.storage import Storage


# ---------------- throttle ----------------


async def test_bucket_allows_a_burst_then_paces():
    bucket = TokenBucket(rate_per_s=10, burst=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.take()
    assert time.monotonic() - start < 0.05  # the burst is free

    await bucket.take()
    assert time.monotonic() - start >= 0.09  # the sixth waits ~1/10s


async def test_reads_and_writes_are_metered_separately():
    throttle = Throttle("basic")
    # Exhausting the write budget must not delay a read.
    for _ in range(6):
        await throttle.acquire("POST")
    start = time.monotonic()
    await throttle.acquire("GET")
    assert time.monotonic() - start < 0.05


async def test_unknown_tier_falls_back_to_the_safest():
    assert Throttle("nonsense").tier.name == "basic"
    assert Throttle("premier").tier is TIERS["premier"]


async def test_throttle_reports_what_it_cost():
    throttle = Throttle("basic")
    for _ in range(6):
        await throttle.acquire("POST")
    stats = throttle.stats
    assert stats["write_waits"] > 0
    assert stats["waited_s"] > 0


async def test_concurrent_callers_share_one_budget():
    bucket = TokenBucket(rate_per_s=20, burst=1)
    start = time.monotonic()
    await asyncio.gather(*(bucket.take() for _ in range(5)))
    # 1 free + 4 paced at 20/s ≈ 0.2s; the point is they queue, not overlap.
    assert time.monotonic() - start >= 0.15


# ---------------- reconciliation ----------------


class FakeClient:
    def __init__(self, positions=None, fills=None, market=None, resting=None):
        self._positions = positions or []
        self._fills = fills or []
        self._market = market or {}
        self._resting = resting or []
        self.cancelled: list[str] = []

    async def get_positions(self):
        return self._positions

    async def get_fills(self, ticker=None, limit=100):
        return self._fills

    async def get_market(self, ticker):
        return self._market

    async def get_resting_orders(self, ticker=None):
        return self._resting

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


@pytest.fixture()
async def ledger(tmp_path):
    storage = Storage(tmp_path / "rec.sqlite3", Fernet.generate_key().decode())
    await storage.upsert_user(1, "trader")
    yield storage
    storage.close()


async def open_live_trade(storage, count=10, ticker="KXBTC15M-1"):
    return await storage.record_entry(
        tg_id=1,
        ticker=ticker,
        coin="BTC",
        side="yes",
        count=count,
        entry_price_dc=500,
        target_price_dc=580,
        entry_fee_dc=20,
        paper=False,
        entry_order_id="o1",
        reason="test",
    )


async def test_matching_position_is_clean(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(positions=[{"ticker": "KXBTC15M-1", "position_fp": "10.00"}])
    report = await Reconciler(ledger).run(1, client)
    assert report.checked == 1
    assert report.clean


async def test_paper_trades_are_not_reconciled(ledger):
    await ledger.record_entry(
        tg_id=1, ticker="KXBTC15M-1", coin="BTC", side="yes", count=1,
        entry_price_dc=500, target_price_dc=580, entry_fee_dc=0,
        paper=True, entry_order_id=None, reason=None,
    )
    report = await Reconciler(ledger).run(1, FakeClient())
    assert report.checked == 0


async def test_a_position_closed_while_down_is_booked_from_fills(ledger):
    trade_id = await open_live_trade(ledger, count=10)
    client = FakeClient(
        positions=[],  # Kalshi holds nothing
        fills=[
            {
                "action": "sell",
                "count_fp": "10.00",
                "price_dollars": "0.5800",
                "fee_dollars": "0.0020",
            }
        ],
    )
    report = await Reconciler(ledger).run(1, client)

    assert report.closed == 1
    assert await ledger.open_trades(1) == []
    closed = (await ledger.trades_since(1, 0))[0]
    assert closed.exit_price_dc == 580  # the real fill price, not the target
    assert closed.status == "closed"


async def test_a_no_side_fill_is_mirrored_back_onto_our_side(ledger):
    await ledger.record_entry(
        tg_id=1, ticker="KXBTC15M-1", coin="BTC", side="no", count=5,
        entry_price_dc=400, target_price_dc=480, entry_fee_dc=20,
        paper=False, entry_order_id="o1", reason=None,
    )
    client = FakeClient(
        positions=[],
        # Fills are quoted YES-side: 0.52 YES == 48c on our NO position.
        fills=[{"action": "sell", "count_fp": "5.00", "price_dollars": "0.5200"}],
    )
    await Reconciler(ledger).run(1, client)
    closed = (await ledger.trades_since(1, 0))[0]
    assert closed.exit_price_dc == 480


async def test_a_settled_market_with_no_fill_is_booked_from_the_result(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(
        positions=[], fills=[], market={"status": "finalized", "result": "yes"}
    )
    report = await Reconciler(ledger).run(1, client)
    assert report.closed == 1
    closed = (await ledger.trades_since(1, 0))[0]
    assert closed.exit_price_dc == 1000  # held YES, settled YES
    assert closed.exit_fee_dc == 0  # settlement is free


async def test_an_unresolvable_position_is_left_open(ledger):
    await open_live_trade(ledger, count=10)
    # No position, no fills, and the market has not settled yet.
    client = FakeClient(positions=[], fills=[], market={"status": "active", "result": ""})
    await Reconciler(ledger).run(1, client)
    # Better to look again next pass than to invent an exit price.
    assert len(await ledger.open_trades(1)) == 1


async def test_a_partial_exit_is_reported_not_silently_closed(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(positions=[{"ticker": "KXBTC15M-1", "position_fp": "4.00"}])
    report = await Reconciler(ledger).run(1, client)

    assert not report.clean
    assert report.divergences[0].kind == "partial_exit"
    assert "6 of 10 closed" in report.divergences[0].detail
    assert len(await ledger.open_trades(1)) == 1  # the rest still needs managing


async def test_a_position_the_bot_does_not_know_about_is_flagged(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(
        positions=[
            {"ticker": "KXBTC15M-1", "position_fp": "10.00"},
            {"ticker": "KXETH15M-9", "position_fp": "25.00"},
        ]
    )
    report = await Reconciler(ledger).run(1, client)
    kinds = [d.kind for d in report.divergences]
    assert "untracked" in kinds
    assert "KXETH15M-9" in report.summary()


async def test_close_missing_can_be_disabled(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(positions=[])
    report = await Reconciler(ledger).run(1, client, close_missing=False)
    assert report.closed == 0
    assert len(await ledger.open_trades(1)) == 1
    assert report.divergences[0].kind == "closed_elsewhere"


async def test_no_open_trades_is_a_no_op(ledger):
    report = await Reconciler(ledger).run(1, FakeClient())
    assert report.checked == 0 and report.clean


async def test_summary_is_readable(ledger):
    await open_live_trade(ledger, count=10)
    client = FakeClient(positions=[{"ticker": "KXBTC15M-1", "position_fp": "10.00"}])
    report = await Reconciler(ledger).run(1, client)
    assert "Reconciled 1 position" in report.summary()


# ---------------- orphaned orders ----------------


async def test_orders_on_dead_markets_are_cancelled():
    client = FakeClient(
        resting=[
            {"order_id": "a", "ticker": "KXBTC15M-OLD"},
            {"order_id": "b", "ticker": "KXBTC15M-LIVE"},
        ]
    )
    cancelled = await cancel_orphan_orders(client, {"KXBTC15M-LIVE"})
    assert cancelled == ["a"]
    assert client.cancelled == ["a"]


async def test_nothing_is_cancelled_when_all_markets_are_live():
    client = FakeClient(resting=[{"order_id": "a", "ticker": "KXBTC15M-LIVE"}])
    assert await cancel_orphan_orders(client, {"KXBTC15M-LIVE"}) == []
