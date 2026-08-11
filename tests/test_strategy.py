import pytest

from kbot.kalshi.orderbook import OrderBook
from kbot.kalshi.prices import dc_to_dollars
from kbot.strategy import MarketContext, get_strategy
from kbot.strategy.directional import DriftStrategy, FadeStrategy, HammerStrategy


def book(yes_depth: int, no_depth: int, yes_bid: int = 450, no_bid: int = 500) -> OrderBook:
    """Prices are deci-cents; the helper takes them and emits wire dollars."""
    b = OrderBook("KXBTC15M-TEST")
    b.apply_snapshot(
        yes=[[dc_to_dollars(yes_bid), str(yes_depth)]],
        no=[[dc_to_dollars(no_bid), str(no_depth)]],
    )
    return b


def ctx(**overrides) -> MarketContext:
    base = dict(
        coin="BTC",
        ticker="KXBTC15M-TEST",
        book=book(200, 60),
        seconds_to_close=400.0,
        window_seconds=900.0,
        fv_change_5s=10.0,
        fv_change_20s=30.0,
        fv_change_60s=40.0,
        samples=30,
    )
    base.update(overrides)
    return MarketContext(**base)


# ---------------- drift ----------------


def test_fires_up_when_book_and_drift_agree():
    signal = DriftStrategy().evaluate(ctx())
    assert signal is not None
    assert signal.side == "yes"
    assert signal.direction == "UP"
    assert signal.price_dc == 500  # $1.00 - best NO bid
    assert 0 < signal.confidence <= 1


def test_fires_down_on_the_mirrored_setup():
    signal = DriftStrategy().evaluate(
        ctx(book=book(60, 200), fv_change_5s=-10.0, fv_change_20s=-30.0)
    )
    assert signal is not None
    assert signal.side == "no"
    assert signal.direction == "DOWN"
    assert signal.price_dc == 550  # $1.00 - best YES bid


def test_no_signal_when_components_disagree():
    # Book leans YES, but fair value is drifting the other way.
    assert DriftStrategy().evaluate(
        ctx(fv_change_5s=-15.0, fv_change_20s=-40.0)
    ) is None


def test_no_signal_when_the_move_is_too_weak():
    assert DriftStrategy().evaluate(
        ctx(book=book(105, 100), fv_change_5s=0.5, fv_change_20s=1.0)
    ) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"seconds_to_close": 30.0},  # too close to expiry
        {"seconds_to_close": 880.0},  # window too young
        {"samples": 1},  # not enough history
        {"book": book(200, 60, yes_bid=300, no_bid=500)},  # spread too wide
        {"book": book(3, 2)},  # not enough depth
    ],
)
def test_filters_reject(overrides):
    assert DriftStrategy().evaluate(ctx(**overrides)) is None


def test_missing_history_produces_no_signal():
    assert DriftStrategy().evaluate(ctx(fv_change_20s=None)) is None


def test_stale_book_is_never_traded():
    stale = OrderBook("KXBTC15M-TEST")  # never updated
    assert DriftStrategy().evaluate(ctx(book=stale)) is None


def test_an_old_book_is_never_traded():
    # Age comes from the context, not the wall clock, so a replay of last
    # week's data is not rejected while a genuinely lagging feed still is.
    assert DriftStrategy().evaluate(ctx(book_age_s=30.0)) is None


def test_historical_data_is_not_rejected_as_stale():
    """Regression: replaying old recordings must still produce signals."""
    old = book(200, 60)
    old.updated_at = 1_000_000.0  # long ago in wall-clock terms
    assert DriftStrategy().evaluate(ctx(book=old, book_age_s=0.0)) is not None


def test_extreme_prices_are_skipped():
    # A YES ask of 90c is outside the entry band.
    assert (
        DriftStrategy().evaluate(ctx(book=book(200, 60, yes_bid=860, no_bid=100)))
        is None
    )


# ---------------- fade ----------------


def test_fade_sells_an_unconfirmed_rally():
    signal = FadeStrategy().evaluate(
        ctx(seconds_to_close=300.0, fv_change_60s=90.0, book=book(100, 100))
    )
    assert signal is not None
    assert signal.side == "no"


def test_fade_ignores_a_move_the_book_confirms():
    assert FadeStrategy().evaluate(
        ctx(seconds_to_close=300.0, fv_change_60s=90.0, book=book(300, 40))
    ) is None


def test_fade_requires_a_late_window():
    assert FadeStrategy().evaluate(
        ctx(seconds_to_close=700.0, fv_change_60s=90.0, book=book(100, 100))
    ) is None


# ---------------- registry ----------------


def test_unknown_strategy_falls_back_to_the_default():
    assert get_strategy("does-not-exist").name == "drift"


def test_the_old_strategy_name_still_resolves():
    """Renaming a strategy must not silently switch a user's saved setting."""
    assert get_strategy("directional").name == "drift"


# ---------------- hammer ----------------


def test_hammer_follows_a_sweep_the_book_backs():
    signal = HammerStrategy().evaluate(
        ctx(fv_change_5s=30.0, fv_change_20s=35.0, book=book(300, 50))
    )
    assert signal is not None
    assert signal.side == "yes"


def test_hammer_ignores_a_slow_drift():
    # Same 20s move, but no sharp impulse behind it.
    assert HammerStrategy().evaluate(
        ctx(fv_change_5s=2.0, fv_change_20s=35.0, book=book(300, 50))
    ) is None


def test_hammer_will_not_follow_a_sweep_into_an_opposing_book():
    assert HammerStrategy().evaluate(
        ctx(fv_change_5s=30.0, fv_change_20s=35.0, book=book(50, 300))
    ) is None


def test_hammer_needs_time_left_to_carry():
    assert HammerStrategy().evaluate(
        ctx(seconds_to_close=120.0, fv_change_5s=30.0, fv_change_20s=35.0,
            book=book(300, 50))
    ) is None
