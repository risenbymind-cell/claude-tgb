"""The desk's live order path.

Until now the desk simulated every fill regardless of mode, which made
demo-live a lie: the operator selected it, saw trades appear, and nothing had
been sent anywhere. These tests are about the boundary between simulated and
real -- which side of it the desk is on, and every condition that must hold
before an order leaves the process.

The rule the whole file exists to enforce: a position may only claim to be
live if a broker that talks to an exchange actually filled it.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kbot.config import Settings
from kbot.engine.broker import LiveBroker, OrderResult, PaperBroker
from kbot.kalshi.orderbook import OrderBook
from kbot.safety import TradingMode
from kbot.strategy import Signal
from kbot.webui.desk import PRODUCTION_ACK_A, PRODUCTION_ACK_B, Desk, MarketView
from kbot.webui.server import DeskServer


@pytest.fixture(scope="module")
def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def make_desk(tmp_path, mode, *, with_keys=False, pem=None):
    settings = Settings(
        telegram_token="",
        admin_ids=frozenset(),
        master_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "desk.sqlite3",
        demo=mode.uses_demo_host,
        mode=mode,
        series={"BTC": "KXBTC15M"},
        spot_products={},
    )
    desk = Desk(settings)
    if with_keys:
        assert desk.connect_keys("abcd1234efgh5678", pem)["ok"] is True
    return desk


def live_book(ticker="KXBTC15M-BTC"):
    book = OrderBook(ticker)
    book.yes = {510: 50.0}
    book.no = {490: 100.0}
    book.updated_at = time.time()
    return book


def view():
    return MarketView(
        coin="BTC", ticker="KXBTC15M-BTC", seconds_to_close=400.0,
        mid_dc=500.0, spread_dc=2, yes_ask=510, no_ask=490,
        depth=150.0, yes_depth=50.0, no_depth=100.0, imbalance=-0.33,
        vwap_dc=498.0, range_dc=20.0, extension=0.1, velocity_dc=1.0,
        mom20_dc=0.5, rt_fee_dc=140, book_age_s=0.5,
    )


def signal(side="yes", price_dc=510):
    return Signal(
        coin="BTC", ticker="KXBTC15M-BTC", side=side,
        confidence=0.7, price_dc=price_dc, reason="test_fire",
    )


class RecordingBroker:
    """Stands in for a real exchange connection, and remembers what it was
    asked to do. `paper = False` is the field the desk reads to decide whether
    a position may call itself live."""

    paper = False

    def __init__(self, *, buy_ok=True, sell_ok=True, error="refused"):
        self.buys: list[tuple] = []
        self.sells: list[tuple] = []
        self._buy_ok = buy_ok
        self._sell_ok = sell_ok
        self._error = error

    async def balance(self):
        return 123_456

    async def buy(self, ticker, side, count, price_dc, book):
        self.buys.append((ticker, side, count, price_dc))
        if not self._buy_ok:
            return OrderResult(ok=False, error=self._error)
        return OrderResult(
            ok=True, order_id="ord-entry-1", filled=count,
            price_dc=price_dc, fee_dc=7,
        )

    async def sell(self, ticker, side, count, price_dc, book):
        self.sells.append((ticker, side, count, price_dc))
        if not self._sell_ok:
            return OrderResult(ok=False, error=self._error)
        return OrderResult(
            ok=True, order_id="ord-exit-1", filled=count,
            price_dc=price_dc, fee_dc=5,
        )


def use(desk, broker, monkeypatch):
    """Point the desk at a stand-in broker and a live book.

    Also installs a credential sentinel: `blocked_reason` refuses a live mode
    with no signer, which is correct and has its own test
    (`test_missing_credentials_block_a_live_mode`). These tests are about what
    happens once that condition is satisfied, so satisfying it here keeps each
    test measuring one thing.
    """
    if desk.settings.mode.places_real_orders and desk._signer is None:
        desk._signer = object()
    monkeypatch.setattr(desk, "broker", lambda: broker)
    monkeypatch.setattr(desk.feed, "book", lambda ticker: live_book(ticker))


# ---------------- which broker gets chosen ----------------


async def test_paper_mode_uses_the_paper_broker(tmp_path):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    assert isinstance(desk.broker(), PaperBroker)


async def test_demo_live_without_keys_still_falls_back_to_paper(tmp_path):
    """Selecting demo-live does not conjure credentials. Falling back to a
    simulator is right; pretending the orders were real would not be."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    assert isinstance(desk.broker(), PaperBroker)


async def test_demo_live_with_keys_uses_a_live_broker(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    broker = desk.broker()
    assert isinstance(broker, LiveBroker)
    assert broker.paper is False


async def test_the_live_broker_points_at_the_demo_host(tmp_path, rsa_pem):
    """The single most consequential setting in the whole file."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    assert "demo" in desk.settings.rest_base
    assert desk.broker().client.base_url == desk.settings.rest_base


async def test_production_mode_points_at_the_production_host(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.PRODUCTION_LIVE, with_keys=True, pem=rsa_pem)
    assert "demo" not in desk.settings.rest_base


async def test_the_broker_is_rebuilt_when_keys_change(tmp_path, rsa_pem):
    """A cached broker holding a replaced key would keep signing with it."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    first = desk.broker()
    desk.connect_keys("abcd1234efgh5678", rsa_pem)
    assert desk.broker() is not first


async def test_live_orders_get_a_durable_intent_log(tmp_path, rsa_pem):
    """The dangerous window is between the exchange accepting an order and
    this process writing down that it did."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    assert desk.broker().intents is not None


# ---------------- what blocks a real order ----------------


async def test_paper_mode_is_never_blocked(tmp_path):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    assert desk.blocked_reason() is None


async def test_the_kill_switch_blocks_every_mode(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    assert desk.blocked_reason() is None
    desk.kill.engage("stop", source="test")
    assert "kill switch" in desk.blocked_reason()


async def test_missing_credentials_block_a_live_mode(tmp_path):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    assert "credentials" in desk.blocked_reason()


async def test_demo_needs_no_arming(tmp_path, rsa_pem):
    """Demo funds are not money. Requiring the ceremony here would train the
    operator to click through the ceremony that guards real money."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    assert desk.production_armed is False
    assert desk.blocked_reason() is None


async def test_production_is_blocked_until_armed(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.PRODUCTION_LIVE, with_keys=True, pem=rsa_pem)
    assert "arming" in desk.blocked_reason()
    desk.arm_step_a(PRODUCTION_ACK_A)
    desk.arm_step_b(PRODUCTION_ACK_B)
    assert desk.production_armed is True
    assert desk.blocked_reason() is None


async def test_disarming_re_blocks_production(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.PRODUCTION_LIVE, with_keys=True, pem=rsa_pem)
    desk.arm_step_a(PRODUCTION_ACK_A)
    desk.arm_step_b(PRODUCTION_ACK_B)
    desk.disarm()
    assert desk.blocked_reason() is not None


# ---------------- entries ----------------


async def test_a_demo_entry_actually_submits(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(), time.time())

    assert broker.buys == [("KXBTC15M-BTC", "yes", 10, 510)]
    assert len(desk.positions) == 1
    assert desk.positions[0].live is True
    assert desk.positions[0].entry_order_id == "ord-entry-1"


async def test_a_paper_entry_submits_nothing(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    broker = PaperBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(), time.time())

    assert len(desk.positions) == 1
    assert desk.positions[0].live is False


async def test_a_blocked_entry_sends_nothing_and_is_audited(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.PRODUCTION_LIVE)
    broker = RecordingBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(), time.time())

    assert broker.buys == []
    assert desk.positions == []
    assert any(a["action"] == "order_blocked" for a in desk.audit)


async def test_the_kill_switch_stops_an_entry_mid_flight(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True
    desk.kill.engage("stop", source="test")

    await desk._maybe_open(view(), signal(), time.time())

    assert broker.buys == []
    assert desk.positions == []


async def test_a_rejected_entry_opens_no_position(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker(buy_ok=False, error="insufficient balance")
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(), time.time())

    assert desk.positions == []
    rejected = [a for a in desk.audit if a["action"] == "entry_rejected"]
    assert rejected and rejected[0]["detail"]["error"] == "insufficient balance"


async def test_a_rejected_entry_does_not_retry_every_pass(tmp_path, monkeypatch):
    """A refusal that re-fires on every pass is an order-rate incident."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker(buy_ok=False)
    use(desk, broker, monkeypatch)
    desk.enabled = True

    for _ in range(5):
        await desk._maybe_open(view(), signal(), time.time())

    assert len(broker.buys) == 1


async def test_shadow_mode_sends_nothing_even_in_demo(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True
    desk.shadow = True

    await desk._maybe_open(view(), signal(), time.time())

    assert broker.buys == []
    assert desk.positions == []


async def test_the_exchange_fill_price_wins_over_the_signal(tmp_path, monkeypatch):
    """The strategy asked for a price; the exchange reported what it got. The
    ledger must show the second, or every P/L number downstream is fiction."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)

    class Slipped(RecordingBroker):
        async def buy(self, ticker, side, count, price_dc, book):
            self.buys.append((ticker, side, count, price_dc))
            return OrderResult(
                ok=True, order_id="o1", filled=count, price_dc=530, fee_dc=9
            )

    broker = Slipped()
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(price_dc=510), time.time())

    pos = desk.positions[0]
    assert pos.entry_dc == 530
    assert pos.entry_fee_dc == 9
    # The target is measured from where the fill actually landed.
    assert pos.target_dc == 530 + desk.target_c * 10


async def test_a_partial_fill_sizes_the_position_to_what_filled(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)

    class Partial(RecordingBroker):
        async def buy(self, ticker, side, count, price_dc, book):
            self.buys.append((ticker, side, count, price_dc))
            return OrderResult(
                ok=True, order_id="o1", filled=3, price_dc=price_dc, fee_dc=2
            )

    broker = Partial()
    use(desk, broker, monkeypatch)
    desk.enabled = True

    await desk._maybe_open(view(), signal(), time.time())

    assert desk.positions[0].count == 3


# ---------------- exits ----------------


async def _open_live_position(desk, broker, monkeypatch):
    use(desk, broker, monkeypatch)
    desk.enabled = True
    await desk._maybe_open(view(), signal(), time.time())
    return desk.positions[0]


async def test_a_live_exit_actually_submits(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    pos = await _open_live_position(desk, broker, monkeypatch)

    book = live_book()
    await desk._close(pos, 600, "target", time.time(), book)

    assert broker.sells == [("KXBTC15M-BTC", "yes", 10, 600)]
    assert pos.outcome == "target"
    assert pos.exit_order_id == "ord-exit-1"


async def test_a_paper_exit_submits_nothing(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    broker = PaperBroker()
    use(desk, broker, monkeypatch)
    desk.enabled = True
    await desk._maybe_open(view(), signal(), time.time())
    pos = desk.positions[0]

    await desk._close(pos, 600, "target", time.time(), live_book())

    assert pos.outcome == "target"
    assert pos.exit_order_id is None


async def test_a_failed_live_exit_leaves_the_position_open(tmp_path, monkeypatch):
    """The contracts are still on the exchange until it says otherwise.
    Marking this closed would hide a real position from its own operator."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker(sell_ok=False, error="no bid")
    pos = await _open_live_position(desk, broker, monkeypatch)

    await desk._close(pos, 600, "target", time.time(), live_book())

    assert pos.outcome == "open"
    assert pos.closed_at is None
    assert pos.exit_error == "no bid"


async def test_a_failed_exit_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker(sell_ok=False)
    pos = await _open_live_position(desk, broker, monkeypatch)

    await desk._close(pos, 600, "target", time.time(), live_book())
    await desk._close(pos, 600, "target", time.time(), live_book())

    assert len(broker.sells) == 2


async def test_the_kill_switch_stops_a_live_exit(tmp_path, monkeypatch):
    """Closing reduces exposure, but a half-honoured kill switch is not one.
    The position stays open and says why."""
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    pos = await _open_live_position(desk, broker, monkeypatch)
    desk.kill.engage("stop", source="test")

    await desk._close(pos, 600, "target", time.time(), live_book())

    assert broker.sells == []
    assert pos.outcome == "open"
    assert "kill switch" in pos.exit_error


async def test_the_exchange_exit_price_wins(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)

    class SlippedExit(RecordingBroker):
        async def sell(self, ticker, side, count, price_dc, book):
            self.sells.append((ticker, side, count, price_dc))
            return OrderResult(
                ok=True, order_id="x1", filled=count, price_dc=580, fee_dc=4
            )

    broker = SlippedExit()
    pos = await _open_live_position(desk, broker, monkeypatch)

    await desk._close(pos, 600, "target", time.time(), live_book())

    assert pos.exit_dc == 580
    assert pos.exit_fee_dc == 4


# ---------------- posture and reporting ----------------


async def test_the_snapshot_says_orders_are_real_in_demo(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    lp = desk.snapshot()["live_posture"]
    assert lp["places_real_orders"] is True
    assert lp["risks_real_money"] is False
    assert lp["blocked"] is None
    assert "demo" in lp["why"].lower()


async def test_the_snapshot_says_paper_sends_nothing(tmp_path):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    lp = desk.snapshot()["live_posture"]
    assert lp["places_real_orders"] is False
    assert "nothing is sent" in lp["why"].lower()


async def test_the_snapshot_names_what_is_blocking(tmp_path, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.PRODUCTION_LIVE, with_keys=True, pem=rsa_pem)
    lp = desk.snapshot()["live_posture"]
    assert lp["blocked"] is not None
    assert "arming" in lp["why"]


async def test_positions_report_their_venue(tmp_path, monkeypatch):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE)
    broker = RecordingBroker()
    await _open_live_position(desk, broker, monkeypatch)
    row = desk.snapshot()["positions"][0]
    assert row["live"] is True
    assert row["entry_order_id"] == "ord-entry-1"


async def test_paper_mode_reports_no_balance(tmp_path):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    await desk._refresh_balance(time.time())
    assert desk.balance_dc is None


async def test_a_live_mode_polls_a_balance(tmp_path, monkeypatch, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    monkeypatch.setattr(desk, "broker", lambda: RecordingBroker())
    await desk._refresh_balance(time.time())
    assert desk.balance_dc == 123_456


# ---------------- reconciliation ----------------


async def test_reconcile_in_paper_mode_claims_nothing(tmp_path):
    desk = make_desk(tmp_path, TradingMode.PAPER)
    result = await desk.reconcile()
    assert result["ok"] is True
    assert result["live"] is False


async def test_reconcile_reports_a_position_the_exchange_does_not_have(
    tmp_path, monkeypatch, rsa_pem
):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    broker = RecordingBroker()
    await _open_live_position(desk, broker, monkeypatch)

    async def no_positions():
        return []

    monkeypatch.setattr(desk.rest, "get_positions", no_positions)
    result = await desk.reconcile()

    assert result["ok"] is True
    assert any("exchange holds none" in d for d in result["divergences"])


async def test_reconcile_reports_an_untracked_exchange_position(
    tmp_path, monkeypatch, rsa_pem
):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)

    async def one_position():
        return [{"ticker": "KXETH15M-ETH", "position": "5.00"}]

    monkeypatch.setattr(desk.rest, "get_positions", one_position)
    result = await desk.reconcile()

    assert any("not tracking" in d for d in result["divergences"])


async def test_reconcile_is_clean_when_they_match(tmp_path, monkeypatch, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)
    broker = RecordingBroker()
    pos = await _open_live_position(desk, broker, monkeypatch)

    async def matching():
        return [{"ticker": pos.ticker, "position": f"{pos.count}.00"}]

    monkeypatch.setattr(desk.rest, "get_positions", matching)
    result = await desk.reconcile()

    assert result["divergences"] == []
    assert "matches" in result["detail"]


async def test_reconcile_surfaces_an_api_failure(tmp_path, monkeypatch, rsa_pem):
    desk = make_desk(tmp_path, TradingMode.DEMO_LIVE, with_keys=True, pem=rsa_pem)

    async def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(desk.rest, "get_positions", boom)
    result = await desk.reconcile()

    assert result["ok"] is False
    assert "network down" in result["error"]
