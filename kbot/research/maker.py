"""What a passive quote earns, and what it can afford to lose.

Every strategy tested in this project crossed the spread, and every one lost
roughly what crossing costs: 2.243c per contract measured, against 1.882c
predicted from half-spread plus fee. See `research/FAVOURITE_LONGSHOT.md`.

That points somewhere specific. The loss was not bad forecasting -- it was the
cost of demanding liquidity. A resting order does the opposite: it is paid the
half-spread instead of paying it, and on Kalshi it is charged a lower fee. The
sign of the whole exercise flips without predicting anything.

    taker:  -half_spread - taker_fee
    maker:  +half_spread - maker_fee - adverse_selection

## The term that decides it

`adverse_selection` is why this module does not simply declare victory. A
resting quote is not filled at random. It is filled when someone wants the
other side, which is disproportionately when the fair price is moving through
it -- so the fills a maker gets are worse than the fills a maker would choose.
That cost is what separates market making from free money, and it cannot be
computed from candlestick data, which has neither depth nor queue position.

So this module computes the one thing that *is* computable and is the number
worth knowing: **how large adverse selection can be before the strategy turns
negative.** That converts an unbounded question into a measurable threshold the
recorder can test against.

## Two costs deliberately not modelled

* **Fill probability.** A quote that never fills earns nothing, but it also
  loses nothing: fill rate scales volume, not the sign. It matters enormously
  for whether this is worth running and not at all for whether it works.
* **Inventory carried into settlement.** This one does bite. `STATUS.md`
  records that 73.9% of books have no bid on the losing side in the final 20
  seconds, so a maker holding stock near expiry may be unable to flatten at
  any price. `settlement_risk_c` puts a number on the worst case; it is a real
  cost this model shows separately rather than folding into the edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import mean, median

from ..kalshi.fees import DEFAULT_FEE_RATE, fee_dollars
from ..kalshi.prices import DECI_CENTS_PER_DOLLAR
from .bands import DEFAULT_BANDS, SCAN_TIMES, _frame_at
from .search import Market

#: Kalshi's maker fee as a fraction of the taker fee.
#:
#: **This is not confirmed at the source.** Kalshi's published schedule is a
#: PDF that rate-limits automated fetches, the docs site 404s on every fee path
#: except rounding, and `GET /series/{ticker}` reports only `fee_type` and
#: `fee_multiplier` with no maker field. Three independent secondary sources
#: agree on 25%, and the arithmetic at 50c (taker 1.75c, maker 0.44c) is
#: consistent across all three.
#:
#: The sign of this entire module depends on it, so it is a named constant with
#: this comment attached rather than a number inlined somewhere. Verify against
#: a real maker fill before anything is built on it.
MAKER_FEE_FRACTION = Decimal("0.25")

#: What the same calculation says if no maker discount exists at all. Kept so
#: the pessimistic case is always one argument away rather than a rewrite.
NO_DISCOUNT = Decimal("1.00")

#: Cents per contract, the unit this module reports in. The rest of the bot
#: works in deci-cents; edges here are fractions of a cent and deci-cents make
#: them unreadable.
DC_PER_CENT = 10.0


@dataclass(frozen=True)
class MakerEdge:
    """What a resting quote earns in one price band, before adverse selection."""

    low_dc: int
    high_dc: int
    #: Quotes observed in this band. Not trades -- nothing is assumed filled.
    n: int
    #: Half the quoted spread, in cents per contract. What a maker is paid.
    half_spread_c: float
    #: Maker fee at this band's typical price, cents per contract.
    maker_fee_c: float
    #: What a taker pays instead, for comparison.
    taker_fee_c: float

    @property
    def gross_c(self) -> float:
        """Earned per fill before adverse selection."""
        return self.half_spread_c - self.maker_fee_c

    @property
    def breakeven_adverse_c(self) -> float:
        """Adverse selection this band can absorb before it turns negative.

        Identical to `gross_c` by construction -- the point is not the
        arithmetic but that it names a threshold something can be measured
        against, instead of an edge nothing has tested.
        """
        return self.gross_c

    @property
    def taker_cost_c(self) -> float:
        """What the same trade costs crossing the spread. Always negative."""
        return -(self.half_spread_c + self.taker_fee_c)

    @property
    def swing_c(self) -> float:
        """Difference between resting and crossing, per contract."""
        return self.gross_c - self.taker_cost_c


def _fee_c(price_dc: int, fraction: Decimal) -> float:
    """Fee per contract in cents, for one contract at `price_dc`."""
    rate = DEFAULT_FEE_RATE * fraction
    # Priced per contract rather than per lot: the exchange rounds a lot's fee
    # up to the cent, and dividing that back out would report a rounding
    # artefact as an edge. A hundred contracts is enough that the ceiling is
    # a rounding detail rather than the whole number.
    return float(fee_dollars(100, price_dc, rate=rate)) * 100 / 100


def measure(
    markets: list[Market],
    *,
    times: tuple[float, ...] = SCAN_TIMES,
    bands: tuple[tuple[int, int], ...] = DEFAULT_BANDS,
    maker_fraction: Decimal = MAKER_FEE_FRACTION,
) -> list[MakerEdge]:
    """Spread and fee per band, from observed two-sided quotes.

    Only quotes with a real bid *and* ask on the side in question are counted.
    A one-sided book cannot be quoted into profitably and including it would
    invent a spread that nobody was offering.
    """
    seen: dict[tuple[int, int], list[tuple[float, int]]] = {b: [] for b in bands}
    for at in times:
        for market in markets:
            frame = _frame_at(market, at)
            if frame is None:
                continue
            _, _, yes_ask, no_ask, yes_bid, no_bid = frame
            for ask, bid in ((yes_ask, yes_bid), (no_ask, no_bid)):
                if ask is None or bid is None:
                    continue
                if not 0 < ask < DECI_CENTS_PER_DOLLAR or bid <= 0:
                    continue
                for low, high in bands:
                    if low <= ask < high:
                        seen[(low, high)].append(((ask - bid) / DC_PER_CENT, ask))
                        break

    out: list[MakerEdge] = []
    for (low, high), rows in seen.items():
        if not rows:
            continue
        # Median spread, not mean: a handful of blown-out books would otherwise
        # report an edge that only exists in the moments nobody can trade.
        half = median(s for s, _ in rows) / 2
        typical = int(mean(a for _, a in rows))
        out.append(
            MakerEdge(
                low_dc=low,
                high_dc=high,
                n=len(rows),
                half_spread_c=half,
                maker_fee_c=_fee_c(typical, maker_fraction),
                taker_fee_c=_fee_c(typical, Decimal(1)),
            )
        )
    return sorted(out, key=lambda e: e.low_dc)


def settlement_risk_c(price_dc: int, *, flatten_failure_rate: float = 0.739) -> float:
    """Expected cost of inventory that cannot be flattened before expiry.

    `flatten_failure_rate` defaults to the 73.9% of books measured with no bid
    on the losing side in the final 20 seconds (`STATUS.md`). A contract bought
    at `price_dc` that cannot be sold settles at 0 or 100, so the expected loss
    against its own price is the price times the chance of being wrong -- which
    at a fair price is (1 - P), times the chance of being stuck.

    This is a per-contract cost of *carried* inventory, not of every fill. A
    maker who ends flat pays none of it. It is reported separately for exactly
    that reason: it is a function of inventory policy, not of the edge.
    """
    price = price_dc / DECI_CENTS_PER_DOLLAR
    return price * (1 - price) * flatten_failure_rate * 100
