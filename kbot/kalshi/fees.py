"""Kalshi trading fees.

Kalshi charges a quadratic trading fee on every fill:

    fee = ceil_to_cent(rate x multiplier x contracts x P x (1 - P))

where P is the price in dollars. The fee peaks at P = 0.50 and falls away toward
both tails, which matters here: the 15-minute markets trade around the middle,
so entries land near the most expensive part of the curve.

`rate` is 0.07 and `multiplier` comes from the series metadata (1 for every
15-minute crypto series at the time of writing, confirmed against the API).

Two consequences the rest of the bot has to respect:

* **Settlement is free.** A position held to expiry pays an entry fee only.
* **A round trip must clear two fees.** At 50 contracts entered near 55c, that
  is roughly 1.7c per contract — so a "+2c" exit target is a guaranteed loss,
  not a small win. `min_profitable_exit_dc` is what stops the bot taking those.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from .prices import DECI_CENTS_PER_DOLLAR

#: Kalshi's standard quadratic trading-fee rate.
DEFAULT_FEE_RATE = Decimal("0.07")

_CENT = Decimal("0.01")


def fee_dollars(
    count: int | float,
    price_dc: int,
    *,
    rate: Decimal = DEFAULT_FEE_RATE,
    multiplier: float = 1.0,
) -> Decimal:
    """Trading fee for `count` contracts filled at `price_dc`, in dollars.

    Rounded up to the next cent, which is how the exchange quotes it and what
    every fee in the reference screenshots matches.
    """
    if count <= 0:
        return Decimal("0.00")
    price = Decimal(int(price_dc)) / DECI_CENTS_PER_DOLLAR
    if price <= 0 or price >= 1:
        # A settled contract is not a trade; there is nothing to charge.
        return Decimal("0.00")
    raw = rate * Decimal(str(multiplier)) * Decimal(str(count)) * price * (1 - price)
    return raw.quantize(_CENT, rounding=ROUND_CEILING)


def fee_dc(
    count: int | float,
    price_dc: int,
    *,
    rate: Decimal = DEFAULT_FEE_RATE,
    multiplier: float = 1.0,
) -> int:
    """Trading fee in deci-cents, the bot's internal money unit."""
    return int(
        fee_dollars(count, price_dc, rate=rate, multiplier=multiplier)
        * DECI_CENTS_PER_DOLLAR
    )


def round_trip_fee_dc(
    count: int,
    entry_dc: int,
    exit_dc: int,
    *,
    multiplier: float = 1.0,
) -> int:
    """Total fees for entering at `entry_dc` and selling at `exit_dc`."""
    return fee_dc(count, entry_dc, multiplier=multiplier) + fee_dc(
        count, exit_dc, multiplier=multiplier
    )


def net_pnl_dc(
    count: int,
    entry_dc: int,
    exit_dc: int,
    *,
    entry_fee_dc: int | None = None,
    exit_fee_dc: int | None = None,
    multiplier: float = 1.0,
) -> int:
    """Realised P/L after fees, in deci-cents.

    Actual fees are passed in when the exchange has reported them; otherwise
    they are estimated from the same formula the exchange uses.
    """
    gross = (int(exit_dc) - int(entry_dc)) * int(count)
    entry = (
        entry_fee_dc
        if entry_fee_dc is not None
        else fee_dc(count, entry_dc, multiplier=multiplier)
    )
    exit_ = (
        exit_fee_dc
        if exit_fee_dc is not None
        else fee_dc(count, exit_dc, multiplier=multiplier)
    )
    return gross - int(entry) - int(exit_)


def min_profitable_exit_dc(
    count: int,
    entry_dc: int,
    *,
    multiplier: float = 1.0,
    max_price_dc: int = 999,
) -> int | None:
    """Lowest exit price that still nets a profit after both fees.

    Returned in deci-cents, or None when no reachable exit clears the round
    trip. Prices below $0.90 tick in whole cents, so the search walks in
    whole-cent steps until it crosses into the deci-cent band.
    """
    entry_fee = fee_dc(count, entry_dc, multiplier=multiplier)
    price = int(entry_dc) + 10
    while price <= max_price_dc:
        exit_fee = fee_dc(count, price, multiplier=multiplier)
        if (price - entry_dc) * count - entry_fee - exit_fee > 0:
            return price
        price += 10
    return None


def breakeven_cents(count: int, entry_dc: int, *, multiplier: float = 1.0) -> float:
    """How many cents above entry the price must move just to break even."""
    target = min_profitable_exit_dc(count, entry_dc, multiplier=multiplier)
    if target is None:
        return float("inf")
    return (target - entry_dc) / 10
