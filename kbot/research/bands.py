"""What a contract's price tells you about how often it wins.

Every strategy tried in this project so far has tried to out-forecast Kalshi's
price. All of them failed, and `research/FINDINGS.md` explains why: the price is
already a better forecast than anything buildable from public data.

This module asks a different and much older question. Not *is the price right*
but *is it biased* -- and specifically whether it carries the *favourite-longshot
bias* documented in nearly every betting market since Griffith 1949: cheap
contracts win less often than their price implies, expensive ones more often.
That is not a forecast. It needs no view on the coin at all. It only needs the
market's own price to be systematically shaded, and buying the shaded side.

The test is deliberately the crudest thing that could work:

    buy at the ask N seconds before close, hold to settlement, never exit.

Holding matters. A round trip pays two fees; settlement is free, so holding
halves the cost, and `research/FINDINGS.md` records a 70% loss reduction from
that change alone with no new signal.

## Reading the output

Two numbers per band, and they answer different questions:

* **win rate vs implied** is whether the bias exists. It is a property of the
  market and is estimated well by a few hundred samples.
* **net per trade** is whether it is tradeable. It is dominated by rare losses
  in the extreme bands, and a few hundred samples estimate it badly.

A band can have a real bias and still be untradeable, which is the situation at
the time of writing.

## What the bands are not

The bands are *not* eight independent experiments. Buying YES at 92c and buying
NO at 8c in the same market are the same bet seen from two sides, so the top and
bottom bands are near-mirrors of each other and their agreement is arithmetic,
not evidence. Treat the gradient as one observation with a direction, not eight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from statistics import mean, stdev

from ..kalshi.fees import fee_dc
from ..kalshi.prices import DECI_CENTS_PER_DOLLAR
from .search import Market

#: Price bands, in deci-cents, as (low, high) half-open on the high side.
#: Chosen to be narrow at the extremes -- where the bias lives and the samples
#: are thin -- and wide in the middle, where nothing interesting happens.
DEFAULT_BANDS: tuple[tuple[int, int], ...] = (
    (50, 150),
    (150, 300),
    (300, 500),
    (500, 600),
    (600, 700),
    (700, 800),
    (800, 900),
    (900, 980),
)

#: Seconds before close at which the buy is priced.
DEFAULT_ENTRY_S = 600.0

#: Contracts per trade. The fee is very slightly non-linear in size because it
#: rounds up to the cent, so this is a real parameter rather than a scale factor.
DEFAULT_SIZE = 10


@dataclass(frozen=True)
class Trade:
    """One buy-and-hold, scored."""

    ticker: str
    coin: str
    side: str  # "yes" | "no"
    ask_dc: int
    won: bool
    #: Net dollars after the entry fee. Settlement itself is free.
    net: float
    #: Window close, as a Unix timestamp, for splitting a sample
    #: chronologically. None when the ticker did not carry a parseable date;
    #: such trades still count in a band but cannot be ordered against trades
    #: from another series.
    at: float | None


@dataclass(frozen=True)
class Band:
    """Every trade whose entry price fell in one band, summarised."""

    low_dc: int
    high_dc: int
    n: int
    wins: int
    win_rate: float
    implied: float
    net_total: float
    net_mean: float
    net_t: float
    #: 95% interval on the mean net, normal approximation. Spanning zero means
    #: the sample has not established a direction -- which is the usual case.
    ci_low: float
    ci_high: float

    @property
    def losses(self) -> int:
        return self.n - self.wins

    @property
    def edge_pp(self) -> float:
        """Win rate minus what the price implied, in percentage points."""
        return (self.win_rate - self.implied) * 100


def close_time_of(ticker: str) -> float | None:
    """Unix timestamp of the window's close, read out of the ticker.

    A 15-minute ticker looks like `KXBTC15M-26AUG171745-45`: the middle segment
    is YYMMMDDHHMM in UTC. Kalshi's other products encode time differently, so
    anything that does not parse returns None rather than a wrong date.
    """
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    try:
        when = datetime.strptime(parts[1][:11], "%y%b%d%H%M")
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc).timestamp()


def _frame_at(market: Market, seconds_to_close: float):
    """The observation closest to `seconds_to_close`, or None.

    Candlestick data is one frame a minute, so an exact match is the exception.
    Frames after the requested time are ineligible: using one would price the
    trade with information the trade could not have had.
    """
    best = None
    for frame in market.frames:
        stc = frame[0]
        if stc < seconds_to_close:
            continue
        if best is None or stc < best[0]:
            best = frame
    return best


def trades_from(
    markets: list[Market],
    *,
    entry_at_s: float = DEFAULT_ENTRY_S,
    size: int = DEFAULT_SIZE,
) -> list[Trade]:
    """Score buying each side of each market at the ask and holding.

    Both sides of a market are scored. They are perfectly anti-correlated, so
    they never both win -- pooling them measures the price bias across the whole
    range rather than only where the favourite happened to be.
    """
    out: list[Trade] = []
    for market in markets:
        frame = _frame_at(market, entry_at_s)
        if frame is None:
            continue
        _, _, yes_ask, no_ask, _, _ = frame
        close_at = close_time_of(market.ticker)
        for side, ask in (("yes", yes_ask), ("no", no_ask)):
            if ask is None or ask <= 0 or ask >= DECI_CENTS_PER_DOLLAR:
                continue
            won = market.outcome == side
            gross_dc = (DECI_CENTS_PER_DOLLAR - ask if won else -ask) * size
            net_dc = gross_dc - fee_dc(size, ask)
            out.append(
                Trade(
                    ticker=market.ticker,
                    coin=market.coin,
                    side=side,
                    ask_dc=int(ask),
                    won=won,
                    net=net_dc / DECI_CENTS_PER_DOLLAR,
                    at=close_at,
                )
            )
    return out


def summarise(trades: list[Trade], low_dc: int, high_dc: int) -> Band | None:
    """Summarise the trades priced within one band, or None if there are none."""
    inside = [t for t in trades if low_dc <= t.ask_dc < high_dc]
    if not inside:
        return None

    n = len(inside)
    wins = sum(1 for t in inside if t.won)
    nets = [t.net for t in inside]
    net_mean = mean(nets)
    # A single trade has no spread, so it has no t and no interval; reporting
    # zero would read as "no effect" rather than "no information".
    if n > 1:
        se = stdev(nets) / sqrt(n)
    else:
        se = float("inf")
    t_stat = net_mean / se if se else 0.0
    half = 1.96 * se

    return Band(
        low_dc=low_dc,
        high_dc=high_dc,
        n=n,
        wins=wins,
        win_rate=wins / n,
        implied=mean(t.ask_dc for t in inside) / DECI_CENTS_PER_DOLLAR,
        net_total=sum(nets),
        net_mean=net_mean,
        net_t=t_stat,
        ci_low=net_mean - half if se != float("inf") else float("-inf"),
        ci_high=net_mean + half if se != float("inf") else float("inf"),
    )


def analyse(
    markets: list[Market],
    *,
    entry_at_s: float = DEFAULT_ENTRY_S,
    size: int = DEFAULT_SIZE,
    bands: tuple[tuple[int, int], ...] = DEFAULT_BANDS,
) -> list[Band]:
    """Every band with at least one trade, cheapest first."""
    trades = trades_from(markets, entry_at_s=entry_at_s, size=size)
    found = (summarise(trades, low, high) for low, high in bands)
    return [b for b in found if b is not None]


#: Resamples for a bootstrap interval. Enough that the 2.5th percentile is
#: estimated from ~250 resamples rather than a handful.
BOOTSTRAP_N = 10_000


def bootstrap_ci(
    nets: list[float], *, resamples: int = BOOTSTRAP_N, seed: int = 20260817
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean net.

    The normal interval in `Band` is wrong in the bands that matter, and wrong
    in the flattering direction. A 90-98c trade returns about +$0.90 nineteen
    times in twenty and about -$9.30 the twentieth: the mean of a sample from
    that distribution is heavily right-skewed, because most samples draw fewer
    of the rare loss than the truth contains. A normal interval centred on the
    sample mean does not know that. Resampling does.
    """
    if len(nets) < 2:
        return float("-inf"), float("inf")
    import random

    rng = random.Random(seed)
    n = len(nets)
    means = sorted(
        sum(rng.choices(nets, k=n)) / n for _ in range(resamples)
    )
    lo = means[int(0.025 * resamples)]
    hi = means[min(int(0.975 * resamples), resamples - 1)]
    return lo, hi


def split_chronologically(
    trades: list[Trade], *, fraction: float = 0.5
) -> tuple[list[Trade], list[Trade]]:
    """Oldest `fraction` of the trades, then the rest.

    Trades whose ticker carried no parseable date are dropped: a split is only
    meaningful if every trade in it is on the correct side of the boundary.

    The point of this is that a band boundary chosen by looking at the data has
    already spent the sample. Scoring the *same* rule on a period that came
    later is the only version of the test that has not been fitted -- and it is
    still weaker than a genuine holdout, because the rule was chosen knowing the
    later period existed.
    """
    dated = sorted((t for t in trades if t.at is not None), key=lambda t: t.at)
    cut = int(len(dated) * fraction)
    return dated[:cut], dated[cut:]
