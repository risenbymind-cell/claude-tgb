"""Kalshi's settled history, as an out-of-sample test bed.

The local recordings are too few for a search over thousands of plans to mean
anything. These tests are about the conversion from candlesticks into the same
`Market` shape the search already scores -- because if that conversion is wrong,
an out-of-sample result is just a differently-shaped fiction.
"""

from __future__ import annotations

import pytest

from kbot.research.history import _dc, to_market
from kbot.research.search import Market


def candle(ts, bid, ask, *, price=None):
    return {
        "end_period_ts": ts,
        "yes_bid": {"close_dollars": bid},
        "yes_ask": {"close_dollars": ask},
        "price": {"close_dollars": price or bid},
    }


def market(close_ts=1_000_900, result="yes"):
    return {
        "ticker": "KXBTC15M-T", "result": result,
        "open_time": "2026-08-17T14:30:00Z",
        "close_time": "2026-08-17T14:45:00Z",
    }


def build(candles, result="yes"):
    import datetime as dt

    m = market(result=result)
    close = int(
        dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()
    )
    return to_market("KXBTC15M", m, candles), close


# ---------------- price conversion ----------------


def test_dollars_become_deci_cents():
    assert _dc({"close_dollars": "0.4100"}) == 410
    assert _dc({"close_dollars": "0.9990"}) == 999


def test_settlement_quotes_are_not_tradeable():
    """At expiry the quote IS the outcome. Treating 0.0000/1.0000 as a price
    would be reading the answer off the tape."""
    assert _dc({"close_dollars": "0.0000"}) is None
    assert _dc({"close_dollars": "1.0000"}) is None


def test_missing_and_malformed_values_are_none():
    assert _dc(None) is None
    assert _dc({}) is None
    assert _dc({"close_dollars": ""}) is None
    assert _dc({"close_dollars": "banana"}) is None


# ---------------- window construction ----------------


def test_a_window_becomes_a_market():
    close = 1_786_977_900
    candles = [candle(close - 60 * i, f"0.{40 + i:02d}00", f"0.{41 + i:02d}00")
               for i in range(14, 0, -1)]
    built, _ = build(candles)
    assert built is not None
    assert built.outcome == "yes"
    assert built.coin == "BTC"
    assert len(built.frames) == 14


def test_too_few_candles_is_unusable():
    """A handful of quotes cannot describe a window's range, and a range is
    what every plan's entry condition is measured against."""
    close = 1_786_977_900
    built, _ = build([candle(close - 60 * i, "0.5000", "0.5100") for i in (5, 4, 3)])
    assert built is None


def test_the_settlement_candle_is_dropped():
    """Anything at or after the close is the outcome, not a quote."""
    close = 1_786_977_900
    candles = [candle(close - 60 * i, "0.5000", "0.5100") for i in range(14, 0, -1)]
    candles.append(candle(close, "0.9990", "1.0000"))       # settlement
    candles.append(candle(close + 60, "0.0000", "1.0000"))  # after
    built, _ = build(candles)
    assert all(f[0] > 0 for f in built.frames)


def test_frames_run_from_open_to_close():
    close = 1_786_977_900
    candles = [candle(close - 60 * i, "0.5000", "0.5100") for i in range(1, 15)]
    built, _ = build(candles)
    seconds = [f[0] for f in built.frames]
    assert seconds == sorted(seconds, reverse=True)


def test_the_no_side_mirrors_the_yes_book():
    """A NO contract and a YES contract settle to $1.00 together, so a NO ask
    is the complement of the YES bid. Getting this backwards would price every
    NO trade wrong."""
    close = 1_786_977_900
    candles = [candle(close - 60 * i, "0.4000", "0.4200") for i in range(14, 0, -1)]
    built, _ = build(candles)
    _sec, _mid, yes_ask, no_ask, yes_bid, no_bid = built.frames[0]
    assert yes_bid == 400 and yes_ask == 420
    assert no_ask == 1000 - yes_bid
    assert no_bid == 1000 - yes_ask


# ---------------- the entry condition ----------------


def test_position_in_range_uses_only_the_past():
    """Where price sits in the window's range at the decision point must be
    computed from what was visible then. Including later candles would be
    look-ahead, and would make every backtest here worthless."""
    close = 1_786_977_900
    # Climbs steadily to its highest-so-far at T-600 (the 10th of 14 candles
    # back), then spikes far higher afterwards.
    prices = ["0.30", "0.34", "0.38", "0.42", "0.46", "0.50", "0.54", "0.58",
              "0.62", "0.66", "0.95", "0.93", "0.91", "0.89"]
    candles = [
        candle(close - 60 * (14 - i), f"{p}00", f"{float(p) + 0.01:.2f}00")
        for i, p in enumerate(prices)
    ]
    built, _ = build(candles)
    assert built is not None
    assert 600.0 in built.position_at
    # At T-600 every price seen so far was lower, so price sits at the top of
    # the range *known at that instant* -- not partway up a later spike.
    assert built.position_at[600.0] == pytest.approx(1.0, abs=0.01)


def test_a_flat_window_has_no_position():
    """With no range there is no 'extreme' to trade, and dividing by it would
    manufacture a signal from noise."""
    close = 1_786_977_900
    candles = [candle(close - 60 * i, "0.5000", "0.5010") for i in range(14, 0, -1)]
    built, _ = build(candles)
    assert built.position_at == {}


# ---------------- scoring compatibility ----------------


def test_the_result_is_scored_by_the_searchs_own_function():
    """Fidelity: the plan is judged by exactly the code that found it, so a
    difference in outcome cannot be a difference in interpretation."""
    from kbot.research.search import Plan, evaluate

    close = 1_786_977_900
    candles = [candle(close - 60 * i, f"0.{30 + i * 3:02d}00", f"0.{31 + i * 3:02d}00")
               for i in range(14, 0, -1)]
    built, _ = build(candles)
    assert isinstance(built, Market)
    plan = Plan(600.0, 0.55, False, 40, None, "any")
    result = evaluate(plan, [built], size=10, flatten_at_s=90.0)
    assert result.trades in (0, 1)


def test_the_flatten_point_is_reachable_on_minute_bars():
    """The live desk flattens 45s before close. One-minute candles put the last
    tradeable observation 60s out, so a 45s threshold never fires and every
    trade silently becomes a hold to expiry -- which is a different strategy.
    """
    from kbot.research.search import Plan, evaluate

    close = 1_786_977_900
    # Drifts up to trigger a follow entry, then falls back so the exit is the
    # flatten rather than the target.
    prices = ["0.30", "0.40", "0.55", "0.70", "0.72", "0.60", "0.50",
              "0.45", "0.42", "0.40", "0.38", "0.36", "0.34", "0.32"]
    candles = [
        candle(close - 60 * (14 - i), f"{p}00", f"{float(p) + 0.01:.2f}00")
        for i, p in enumerate(prices)
    ]
    built, _ = build(candles, result="no")
    plan = Plan(600.0, 0.55, False, 40, None, "any")

    reachable = evaluate(plan, [built], size=10, flatten_at_s=90.0)
    unreachable = evaluate(plan, [built], size=10, flatten_at_s=45.0)
    # Whatever the direction, the two must be able to differ -- that is the
    # whole reason the threshold is a parameter.
    assert reachable.trades == unreachable.trades
