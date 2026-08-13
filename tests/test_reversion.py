"""Session statistics and the reversion strategy.

The strategy encodes one hypothesis from research/CORPUS.md: price extended
from its own session VWAP, measured in units of that window's realised range,
reverts. These tests fix the mechanics of that -- they say nothing about
whether the hypothesis is true, which only recorded data can answer.
"""

from __future__ import annotations

import pytest

from kbot.kalshi.orderbook import OrderBook
from kbot.kalshi.session import SessionStats
from kbot.strategy import MarketContext, get_strategy

OPEN = 1_700_000_000.0


def feed(stats: SessionStats, ticker: str, mids, *, depth=100.0, start=OPEN):
    for i, mid in enumerate(mids):
        stats.observe(ticker, float(mid), depth, OPEN, start + i)
    return start + len(mids) - 1


# ---------------- session statistics ----------------


def test_vwap_is_withheld_until_the_window_has_enough_samples():
    """A VWAP over five ticks is not a consensus, and the extension computed
    from it is noise wearing a signal's clothes."""
    s = SessionStats()
    feed(s, "T", [500] * (SessionStats.MIN_SAMPLES - 1))
    assert s.vwap("T") is None
    assert s.extension("T", 600) is None

    s.observe("T", 500.0, 100.0, OPEN, OPEN + 100)
    assert s.vwap("T") == pytest.approx(500.0)


def test_vwap_is_weighted_by_resting_depth():
    """Depth is the closest thing to volume a book snapshot offers. Without
    the weighting this is a plain mean, and a price quoted into an empty book
    counts as much as one quoted into a thick one."""
    s = SessionStats()
    for i in range(30):
        s.observe("T", 400.0, 10.0, OPEN, OPEN + i)      # thin, low
    for i in range(30):
        s.observe("T", 600.0, 990.0, OPEN, OPEN + 30 + i)  # thick, high

    # Unweighted this would be 500; the thick side has to dominate.
    assert s.vwap("T") > 580


def test_a_thin_book_still_contributes():
    """Weighted at a floor rather than dropped -- otherwise the VWAP quietly
    describes only the liquid moments of the window."""
    s = SessionStats()
    feed(s, "T", [500] * 25, depth=0.0)
    assert s.vwap("T") == pytest.approx(500.0)


def test_extension_is_measured_in_units_of_the_window_range():
    s = SessionStats()
    feed(s, "T", [500] * 15 + [400] * 5 + [600] * 5)   # range 200, vwap 500
    vwap = s.vwap("T")
    rng = s.range_dc("T")
    assert rng == pytest.approx(200.0)
    ext = s.extension("T", vwap + 100)
    assert ext == pytest.approx(0.5, abs=0.05)


def test_a_flat_window_yields_no_extension():
    """Dividing by a range of nearly zero turns rounding into a large
    'extension' -- the single easiest way to manufacture a signal."""
    s = SessionStats()
    feed(s, "T", [500] * 30)
    assert s.extension("T", 501) is None


def test_a_new_window_resets_everything():
    """Carrying a previous contract's consensus into a fresh market is not a
    slightly worse signal, it is a different market's signal."""
    s = SessionStats()
    feed(s, "T", [300] * 30)
    assert s.vwap("T") == pytest.approx(300.0)

    s.observe("T", 700.0, 100.0, OPEN + 900, OPEN + 900)
    assert s.samples("T") == 1
    assert s.vwap("T") is None


def test_velocity_needs_a_real_span_before_it_reports():
    s = SessionStats()
    s.observe("T", 500.0, 100.0, OPEN, OPEN)
    s.observe("T", 560.0, 100.0, OPEN, OPEN + 1)
    assert s.velocity_dc("T", OPEN + 1) is None  # 1s is not a velocity

    last = feed(s, "T", range(500, 560, 2))
    assert s.velocity_dc("T", last) is not None


def test_pruning_drops_markets_that_are_gone():
    s = SessionStats()
    feed(s, "A", [500] * 25)
    feed(s, "B", [500] * 25)
    s.prune({"A"})
    assert s.samples("A") == 25
    assert s.samples("B") == 0


# ---------------- the strategy ----------------


def book_at(mid: int, spread: int = 20, depth: float = 500.0) -> OrderBook:
    b = OrderBook("T")
    bid, ask = mid - spread // 2, mid + spread // 2
    b.yes = {bid: depth, bid - 10: depth}
    b.no = {1000 - ask: depth, 1000 - ask - 10: depth}
    b.updated_at = 1.0
    return b


def ctx(**kw) -> MarketContext:
    base = dict(
        coin="BTC",
        ticker="T",
        book=book_at(600),
        seconds_to_close=400.0,
        window_seconds=900.0,
        samples=60,
        book_age_s=0.0,
        vwap_dc=500.0,
        session_range_dc=200.0,
        extension=0.5,
        velocity_dc=30.0,
    )
    base.update(kw)
    return MarketContext(**base)


def test_it_fades_an_extension_above_vwap_by_buying_no():
    signal = get_strategy("reversion").evaluate(ctx())
    assert signal is not None
    assert signal.side == "no", "extended above consensus means fade it"
    assert "above VWAP" in signal.reason


def test_it_fades_an_extension_below_vwap_by_buying_yes():
    signal = get_strategy("reversion").evaluate(
        ctx(book=book_at(400), extension=-0.5, velocity_dc=-30.0)
    )
    assert signal is not None
    assert signal.side == "yes"


def test_a_small_extension_is_skipped():
    assert get_strategy("reversion").evaluate(ctx(extension=0.10)) is None


def test_confidence_rises_with_extension():
    """The corpus treats 70-90% of range as far better than merely qualifying,
    so this scales rather than switching on at a boundary."""
    weak = get_strategy("reversion").evaluate(ctx(extension=0.35))
    strong = get_strategy("reversion").evaluate(ctx(extension=0.90))
    assert weak and strong
    assert strong.confidence > weak.confidence


def test_confidence_stays_well_below_certainty():
    """An untested hypothesis must not licence a position size the evidence
    cannot support."""
    signal = get_strategy("reversion").evaluate(ctx(extension=5.0))
    assert signal is not None and signal.confidence <= 0.85


def test_a_drift_to_the_same_place_is_not_faded():
    """The corpus fades aggression. A market that arrived at the same
    extension slowly, or is already coming back, is not the setup."""
    assert get_strategy("reversion").evaluate(ctx(velocity_dc=1.0)) is None
    assert get_strategy("reversion").evaluate(ctx(velocity_dc=-40.0)) is None


def test_no_entry_without_room_to_revert():
    """Its target is a return to consensus, which takes time. A binary with
    ninety seconds left cannot deliver that however wrong the price is."""
    assert get_strategy("reversion").evaluate(ctx(seconds_to_close=100.0)) is None


def test_missing_session_state_produces_no_signal():
    assert get_strategy("reversion").evaluate(ctx(extension=None)) is None
    assert get_strategy("reversion").evaluate(ctx(velocity_dc=None)) is None


def test_it_refuses_lottery_and_near_certain_prices():
    assert get_strategy("reversion").evaluate(
        ctx(book=book_at(960), extension=0.9, velocity_dc=40.0)
    ) is None


def test_the_signal_carries_its_own_evidence():
    """Every decision has to be auditable after the fact."""
    signal = get_strategy("reversion").evaluate(ctx())
    assert signal is not None
    for key in ("extension", "vwap_dc", "range_dc", "velocity_dc", "seconds_left"):
        assert key in signal.detail


def test_it_is_registered_and_reachable_by_name():
    from kbot.strategy import strategy_names

    assert "reversion" in strategy_names()
    assert get_strategy("reversion").name == "reversion"


# ---------------- the timed scalp ----------------


def timed_ctx(seconds_left, mid, vwap=500.0, rng=200.0, **kw):
    base = dict(
        coin="BTC", ticker="T", book=book_at(int(mid)),
        seconds_to_close=seconds_left, window_seconds=900.0,
        samples=60, book_age_s=0.0,
        vwap_dc=vwap, session_range_dc=rng,
    )
    base.update(kw)
    return MarketContext(**base)


def test_it_only_fires_inside_its_appointment():
    """The whole point is that time-to-close is a parameter rather than an
    accident of when a threshold happened to trip."""
    from kbot.strategy.directional import TimedScalpStrategy

    s = TimedScalpStrategy(300.0)
    assert s.evaluate(timed_ctx(300.0, 660)) is not None
    assert s.evaluate(timed_ctx(600.0, 660)) is None
    assert s.evaluate(timed_ctx(120.0, 660)) is None


def test_the_appointment_has_tolerance_so_a_slow_tick_cannot_miss_it():
    from kbot.strategy.directional import TimedScalpStrategy

    s = TimedScalpStrategy(300.0)
    assert s.evaluate(timed_ctx(310.0, 660)) is not None
    assert s.evaluate(timed_ctx(290.0, 660)) is not None


def test_a_price_near_the_middle_of_the_range_is_skipped():
    from kbot.strategy.directional import TimedScalpStrategy

    assert TimedScalpStrategy(300.0).evaluate(timed_ctx(300.0, 505)) is None


def test_fade_and_follow_take_opposite_sides():
    """Same instant, same path, opposite decision -- so a sweep over this flag
    is a real comparison rather than two versions of one bias."""
    from kbot.strategy.directional import TimedScalpStrategy

    ctx_high = timed_ctx(300.0, 680)
    fade = TimedScalpStrategy(300.0, fade=True).evaluate(ctx_high)
    follow = TimedScalpStrategy(300.0, fade=False).evaluate(ctx_high)
    assert fade and follow
    assert fade.side != follow.side
    assert fade.side == "no", "fading a high means buying NO"


def test_the_entry_time_is_configurable_and_reported():
    from kbot.strategy.directional import TimedScalpStrategy

    s = TimedScalpStrategy(450.0)
    sig = s.evaluate(timed_ctx(450.0, 680))
    assert sig is not None
    assert sig.detail["entry_at_s"] == 450.0
    assert "T-450s" in sig.reason


def test_its_own_filter_cannot_reject_its_appointment():
    """A default min_seconds_left larger than the entry time would reject
    every signal the strategy exists to produce, and it would do so silently."""
    from kbot.strategy.directional import TimedScalpStrategy

    for at in (120.0, 180.0, 300.0, 600.0):
        s = TimedScalpStrategy(at)
        assert s.filters.min_seconds_left <= at
        assert s.evaluate(timed_ctx(at, 690)) is not None, f"rejected at T-{at}"


def test_missing_session_state_produces_nothing():
    from kbot.strategy.directional import TimedScalpStrategy

    s = TimedScalpStrategy(300.0)
    assert s.evaluate(timed_ctx(300.0, 680, vwap=None)) is None
    assert s.evaluate(timed_ctx(300.0, 680, rng=None)) is None
    assert s.evaluate(timed_ctx(300.0, 680, rng=5.0)) is None
