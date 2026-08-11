import pytest

from kbot.kalshi.orderbook import OrderBook
from kbot.strategy import MarketContext, get_strategy
from kbot.strategy.directional import DirectionalStrategy, FadeStrategy


def book(yes_depth: int, no_depth: int, yes_bid: int = 45, no_bid: int = 50) -> OrderBook:
    b = OrderBook("KXBTCD-TEST")
    b.apply_snapshot(yes=[[yes_bid, yes_depth]], no=[[no_bid, no_depth]])
    return b


def ctx(**overrides) -> MarketContext:
    base = dict(
        coin="BTC",
        ticker="KXBTCD-TEST",
        book=book(200, 60),
        seconds_to_close=400.0,
        window_seconds=900.0,
        fv_change_5s=1.0,
        fv_change_20s=3.0,
        fv_change_60s=4.0,
        samples=30,
    )
    base.update(overrides)
    return MarketContext(**base)


# ---------------- directional ----------------


def test_fires_up_when_book_and_drift_agree():
    signal = DirectionalStrategy().evaluate(ctx())
    assert signal is not None
    assert signal.side == "yes"
    assert signal.direction == "UP"
    assert signal.price == 50  # 100 - best NO bid
    assert 0 < signal.confidence <= 1


def test_fires_down_on_the_mirrored_setup():
    signal = DirectionalStrategy().evaluate(
        ctx(book=book(60, 200), fv_change_5s=-1.0, fv_change_20s=-3.0)
    )
    assert signal is not None
    assert signal.side == "no"
    assert signal.direction == "DOWN"
    assert signal.price == 55  # 100 - best YES bid


def test_no_signal_when_components_disagree():
    # Book leans YES, but fair value is drifting the other way.
    assert DirectionalStrategy().evaluate(
        ctx(fv_change_5s=-1.5, fv_change_20s=-4.0)
    ) is None


def test_no_signal_when_the_move_is_too_weak():
    assert DirectionalStrategy().evaluate(
        ctx(book=book(105, 100), fv_change_5s=0.05, fv_change_20s=0.1)
    ) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"seconds_to_close": 30.0},  # too close to expiry
        {"seconds_to_close": 880.0},  # window too young
        {"samples": 1},  # not enough history
        {"book": book(200, 60, yes_bid=30, no_bid=50)},  # spread too wide
        {"book": book(3, 2)},  # not enough depth
    ],
)
def test_filters_reject(overrides):
    assert DirectionalStrategy().evaluate(ctx(**overrides)) is None


def test_missing_history_produces_no_signal():
    assert DirectionalStrategy().evaluate(ctx(fv_change_20s=None)) is None


def test_stale_book_is_never_traded():
    stale = OrderBook("KXBTCD-TEST")  # never updated
    assert DirectionalStrategy().evaluate(ctx(book=stale)) is None


def test_extreme_prices_are_skipped():
    # YES ask of 90c is outside the entry band.
    assert DirectionalStrategy().evaluate(ctx(book=book(200, 60, yes_bid=86, no_bid=10))) is None


# ---------------- fade ----------------


def test_fade_sells_an_unconfirmed_rally():
    signal = FadeStrategy().evaluate(
        ctx(seconds_to_close=300.0, fv_change_60s=9.0, book=book(100, 100))
    )
    assert signal is not None
    assert signal.side == "no"


def test_fade_ignores_a_move_the_book_confirms():
    assert FadeStrategy().evaluate(
        ctx(seconds_to_close=300.0, fv_change_60s=9.0, book=book(300, 40))
    ) is None


def test_fade_requires_a_late_window():
    assert FadeStrategy().evaluate(
        ctx(seconds_to_close=700.0, fv_change_60s=9.0, book=book(100, 100))
    ) is None


# ---------------- registry ----------------


def test_unknown_strategy_falls_back_to_the_default():
    assert get_strategy("does-not-exist").name == "directional"
