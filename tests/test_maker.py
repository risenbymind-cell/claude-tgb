"""Passive-quote economics.

The sign of every number this module produces depends on one unverified
constant, so the tests fix the two things that must not drift: that the
pessimistic case is reachable and honest, and that a thin book cannot be made
to look like a wide one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kbot.research.maker import (
    MAKER_FEE_FRACTION,
    NO_DISCOUNT,
    MakerEdge,
    measure,
    settlement_risk_c,
)
from kbot.research.search import Market


def market(ticker, outcome, quotes):
    """`quotes` is [(seconds_to_close, yes_bid, yes_ask)]."""
    frames = []
    for sec, yes_bid, yes_ask in quotes:
        frames.append(
            (float(sec), (yes_bid + yes_ask) / 2, yes_ask,
             1000 - yes_bid, yes_bid, 1000 - yes_ask)
        )
    return Market(ticker=ticker, coin="BTC", outcome=outcome, frames=frames)


def edge(**kw):
    base = dict(low_dc=500, high_dc=600, n=10, half_spread_c=0.5,
                maker_fee_c=0.44, taker_fee_c=1.75)
    base.update(kw)
    return MakerEdge(**base)


# --- the arithmetic that decides whether any of this is worth building ---


def test_resting_earns_the_spread_that_crossing_pays():
    """The whole thesis in one assertion: the same half-spread appears with
    opposite signs, so switching sides is worth a full spread plus the fee
    difference."""
    e = edge()
    assert e.gross_c == pytest.approx(0.5 - 0.44)
    assert e.taker_cost_c == pytest.approx(-(0.5 + 1.75))
    assert e.swing_c == pytest.approx(e.gross_c - e.taker_cost_c)
    assert e.swing_c > 0


def test_the_breakeven_is_the_edge_and_says_so():
    """Reported as a threshold rather than a profit, because nothing here has
    measured adverse selection -- the term that eats it."""
    e = edge()
    assert e.breakeven_adverse_c == e.gross_c


def test_a_wide_fee_can_make_a_band_negative():
    """A maker fee above the half-spread is a losing quote. If this ever reads
    positive, the pessimistic case has stopped being reachable."""
    assert edge(maker_fee_c=0.9).gross_c < 0


def test_the_pessimistic_constant_is_the_full_taker_rate():
    assert NO_DISCOUNT == Decimal("1.00")
    assert MAKER_FEE_FRACTION < NO_DISCOUNT


def test_no_discount_makes_the_fee_match_the_taker_fee():
    ms = [market("KXBTC15M-26AUG171745-45", "yes", [(600, 490, 500)])]
    only = measure(ms, times=(600.0,), maker_fraction=NO_DISCOUNT)[0]
    assert only.maker_fee_c == pytest.approx(only.taker_fee_c)


def test_the_default_discount_is_a_quarter_of_the_taker_fee():
    ms = [market("KXBTC15M-26AUG171745-45", "yes", [(600, 490, 500)])]
    only = measure(ms, times=(600.0,))[0]
    assert only.maker_fee_c == pytest.approx(only.taker_fee_c * 0.25, rel=0.02)


# --- measurement: what must not be counted ---


def test_a_one_sided_book_is_not_quotable():
    """No bid means no spread to earn. Counting it invents a market nobody
    was making."""
    ms = [market("KXBTC15M-26AUG171745-45", "yes", [(600, 0, 500)])]
    assert measure(ms, times=(600.0,)) == []


def test_a_settled_quote_is_not_counted():
    ms = [market("KXBTC15M-26AUG171745-45", "yes", [(600, 1000, 1000)])]
    assert measure(ms, times=(600.0,)) == []


def test_the_spread_is_a_median_not_a_mean():
    """One blown-out book must not create an edge that existed for a second.

    Nine 1c spreads and one 50c spread: the mean half-spread is 2.95c and the
    median is 0.5c. A mean would report six times the truth.
    """
    ms = [market(f"KXBTC15M-26AUG17{i:02d}00-00", "yes", [(600, 490, 500)])
          for i in range(1, 10)]
    ms.append(market("KXBTC15M-26AUG171000-00", "yes", [(600, 250, 750)]))
    wide = [e for e in measure(ms, times=(600.0,)) if e.low_dc == 500]
    assert wide and wide[0].half_spread_c == pytest.approx(0.5)


def test_bands_are_returned_cheapest_first():
    ms = [market(f"KXBTC15M-26AUG17{i:02d}00-00", "yes",
                 [(600, 100 * i - 10, 100 * i)])
          for i in range(1, 10)]
    got = measure(ms, times=(600.0,))
    assert got == sorted(got, key=lambda e: e.low_dc)


def test_a_quote_lands_in_exactly_one_band():
    ms = [market(f"KXBTC15M-26AUG17{i:02d}00-00", "yes", [(600, 490, 500)])
          for i in range(1, 6)]
    assert sum(e.n for e in measure(ms, times=(600.0,))) == 2 * len(ms)


# --- inventory, which is a bigger number than the edge ---


def test_stuck_inventory_costs_far_more_than_a_fill_earns():
    """A contract that cannot be flattened settles at 0 or 100. At mid prices
    that is worth hundreds of clean fills, which is why inventory policy is the
    strategy rather than a refinement."""
    assert settlement_risk_c(550) > 100 * edge().gross_c


def test_settlement_risk_peaks_in_the_middle():
    assert settlement_risk_c(500) > settlement_risk_c(100)
    assert settlement_risk_c(500) > settlement_risk_c(900)


def test_settlement_risk_scales_with_the_chance_of_being_stuck():
    assert settlement_risk_c(500, flatten_failure_rate=0.0) == 0.0
    a = settlement_risk_c(500, flatten_failure_rate=0.5)
    b = settlement_risk_c(500, flatten_failure_rate=1.0)
    assert b == pytest.approx(2 * a)
