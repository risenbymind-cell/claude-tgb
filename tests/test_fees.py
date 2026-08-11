"""Kalshi trading-fee model.

The reference cases below are read off real DirectionalBot fills and are the
ground truth for this module: if the formula drifts, these break.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kbot.kalshi.fees import (
    breakeven_cents,
    fee_dc,
    fee_dollars,
    min_profitable_exit_dc,
    net_pnl_dc,
    round_trip_fee_dc,
)
from kbot.kalshi.prices import format_dollars

# (contracts, price_dc, fee shown on the real fill)
REFERENCE_FILLS = [
    (50, 580, "0.86"),
    (50, 530, "0.88"),
    (50, 610, "0.84"),
    (50, 550, "0.87"),
    (50, 520, "0.88"),
    (50, 510, "0.88"),
    (50, 620, "0.83"),
    (100, 530, "1.75"),
    (100, 610, "1.67"),
    (100, 71, "0.47"),  # a deci-cent price: 7.1c, not 7c
]


@pytest.mark.parametrize("count,price_dc,expected", REFERENCE_FILLS)
def test_matches_real_fills(count, price_dc, expected):
    assert str(fee_dollars(count, price_dc)) == expected


def test_deci_cent_prices_are_not_rounded_to_cents_first():
    """7.1c and 7c produce different fees; the tail is where this bites."""
    assert fee_dollars(100, 71) != fee_dollars(100, 70)


def test_fee_rounds_up_to_the_next_cent():
    # A fee of exactly zero would be free money; the exchange rounds up.
    assert fee_dollars(1, 500) == Decimal("0.02")
    assert fee_dc(1, 500) == 20


def test_fee_peaks_at_the_midpoint():
    mid = fee_dollars(100, 500)
    assert mid > fee_dollars(100, 300)
    assert mid > fee_dollars(100, 700)
    assert fee_dollars(100, 300) == fee_dollars(100, 700)  # symmetric


def test_settled_prices_are_free():
    # Settlement is not a trade, so there is nothing to charge.
    assert fee_dollars(100, 1000) == Decimal("0.00")
    assert fee_dollars(100, 0) == Decimal("0.00")


def test_zero_size_is_free():
    assert fee_dollars(0, 500) == Decimal("0.00")


def test_multiplier_scales_the_fee():
    assert fee_dollars(100, 500, multiplier=2) > fee_dollars(100, 500)


# ---------------- P/L ----------------


def test_reference_round_trip_reproduces_the_real_pnl():
    """The BTC DOWN fill: 50 sh @ 53c out at 91c, booked at +$17.83."""
    assert format_dollars(net_pnl_dc(50, 530, 910)) == "17.83"


def test_net_pnl_is_always_below_gross():
    gross = (910 - 530) * 50
    assert net_pnl_dc(50, 530, 910) < gross


def test_actual_fees_override_the_estimate():
    # When the exchange reports what it charged, that is what gets booked.
    assert net_pnl_dc(10, 500, 600, entry_fee_dc=1, exit_fee_dc=1) == 1000 - 2


def test_round_trip_fee_is_both_legs():
    assert round_trip_fee_dc(50, 530, 910) == fee_dc(50, 530) + fee_dc(50, 910)


def test_a_losing_trade_is_made_worse_by_fees():
    assert net_pnl_dc(10, 600, 500) < -1000


# ---------------- breakeven ----------------


def test_min_profitable_exit_actually_profits():
    for count in (1, 10, 50, 100):
        for entry in (300, 500, 650):
            target = min_profitable_exit_dc(count, entry)
            assert target is not None
            assert net_pnl_dc(count, entry, target) > 0
            # And one tick lower does not.
            assert net_pnl_dc(count, entry, target - 10) <= 0


def test_a_two_cent_target_never_clears_the_round_trip():
    """The reason exit targets are raised: +2c is a guaranteed loss."""
    assert net_pnl_dc(1, 500, 520) <= 0
    assert net_pnl_dc(50, 500, 520) <= 0


def test_breakeven_near_the_middle_is_several_cents():
    drag = breakeven_cents(50, 500)
    assert 3 <= drag <= 5


def test_no_profitable_exit_exists_from_the_very_top():
    # Entering at 99.5c leaves no room above to cover a fee.
    assert min_profitable_exit_dc(1, 995) is None
    assert breakeven_cents(1, 995) == float("inf")
