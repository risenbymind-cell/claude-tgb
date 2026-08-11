"""Price and quantity units.

Kalshi's API speaks fixed-point dollar strings ("0.5600") and fixed-point
contract counts ("10.00"). Its markets no longer tick in whole cents: the
`tapered_deci_cent` structure used by the 15-minute crypto markets ticks in
*tenths* of a cent below $0.10 and above $0.90, and in whole cents between.

So the internal unit here is the **deci-cent**: an integer from 0 to 1000, where
1000 deci-cents = $1.00 = a settled contract. Every valid tick on every ladder
lands on an integer deci-cent, which means all internal arithmetic is exact and
book price levels are safe to use as dict keys.

Conversions to and from the wire happen only at the API boundary, and
conversions to cents happen only for display and for user settings — traders
think in cents, so that is what the dashboard shows.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DECI_CENTS_PER_DOLLAR = 1000
DECI_CENTS_PER_CENT = 10

# A tradeable price is strictly inside the settlement bounds.
MIN_PRICE_DC = 1
MAX_PRICE_DC = 999

_DOLLAR_QUANT = Decimal("0.0001")
_COUNT_QUANT = Decimal("0.01")


class PriceError(ValueError):
    """Raised when a wire value cannot be read as a price or count."""


def dollars_to_dc(value: str | float | int | Decimal) -> int:
    """Parse a fixed-point dollar value into integer deci-cents.

    Accepts the string form the API emits ("0.5600"), and plain numbers for
    convenience in tests.
    """
    try:
        dollars = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise PriceError(f"not a price: {value!r}") from exc
    scaled = (dollars * DECI_CENTS_PER_DOLLAR).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(scaled)


def dc_to_dollars(dc: int) -> str:
    """Render deci-cents as the fixed-point dollar string the API expects."""
    value = (Decimal(int(dc)) / DECI_CENTS_PER_DOLLAR).quantize(_DOLLAR_QUANT)
    return f"{value:.4f}"


def cents_to_dc(cents: float | int) -> int:
    """User-facing cents (what the dashboard shows) into deci-cents."""
    return int(
        (Decimal(str(cents)) * DECI_CENTS_PER_CENT).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def dc_to_cents(dc: int) -> float:
    """Deci-cents into cents, keeping the tenth when there is one."""
    return int(dc) / DECI_CENTS_PER_CENT


def format_cents(dc: int) -> str:
    """Human-readable price: '45c', or '2.3c' inside the deci-cent bands."""
    cents = dc_to_cents(dc)
    if float(cents).is_integer():
        return f"{int(cents)}c"
    return f"{cents:.1f}c"


def format_dollars(dc_total: int) -> str:
    """Render a deci-cent total as dollars, for money amounts and P/L."""
    return f"{Decimal(int(dc_total)) / DECI_CENTS_PER_DOLLAR:.2f}"


def parse_count(value: str | float | int) -> float:
    """Parse a fixed-point contract count ('10.00') into a float.

    Kalshi supports fractional contracts down to 0.01, so counts cannot be
    integers throughout — but the bot only ever *places* whole contracts.
    """
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PriceError(f"not a count: {value!r}") from exc


def count_to_fp(count: float | int) -> str:
    """Render a contract count as the fixed-point string the API expects."""
    return f"{Decimal(str(count)).quantize(_COUNT_QUANT):.2f}"


def clamp_price(dc: int) -> int:
    return max(MIN_PRICE_DC, min(MAX_PRICE_DC, int(dc)))


def complement(dc: int) -> int:
    """The mirrored price on the opposite side of a binary market.

    A YES contract and a NO contract on the same market together settle to
    $1.00, so a bid of X for NO is an offer of (1000 - X) on YES.
    """
    return DECI_CENTS_PER_DOLLAR - int(dc)


def parse_levels(raw: object) -> list[tuple[int, float]]:
    """Normalise an order-book ladder into [(price_dc, count), ...].

    Handles both shapes the API uses — the REST `orderbook_fp.yes_dollars` form
    and the websocket `yes_dollars_fp` form are both arrays of
    [price_in_dollars, contract_count], as strings.
    """
    if not raw:
        return []
    levels: list[tuple[int, float]] = []
    for entry in raw:  # type: ignore[union-attr]
        try:
            price, count = entry[0], entry[1]
        except (TypeError, IndexError, KeyError):
            continue
        try:
            price_dc = dollars_to_dc(price)
            qty = parse_count(count)
        except PriceError:
            continue
        if qty > 0:
            levels.append((price_dc, qty))
    return levels
