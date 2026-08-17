"""Search a large strategy space, and calibrate the search against noise.

Running thousands of strategies and keeping the best one is not research, it
is a lottery with extra steps. On 36 markets there are only so many distinct
outcomes; try four thousand rules and dozens will post a 90% win rate having
learned nothing at all. The more rules you try, the better the best one looks,
and the less it means.

So this module does two passes.

**Pass one** evaluates the grid on the real settlements and records every
result, not just the good ones.

**Pass two** destroys the thing being predicted -- the settlements are
shuffled, so by construction no rule can have an edge -- and runs the identical
grid again, several times. Whatever the best strategy scores against shuffled
outcomes is what "best of N tries" is worth *for free*. A real result has to
beat that, not beat 50%.

That second pass is the whole point. Without it a search this size cannot
distinguish a strategy from a coincidence, and it will always return something
that looks wonderful.

Regimes are included because a rule that works only when the tape is trending
is a real thing -- but every regime split multiplies the number of hypotheses,
so the null pass sees exactly the same splits and prices them in.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

from ..kalshi.fees import fee_dc
from .replay import book_from_record
from .store import load_session

# ---------------------------------------------------------------- grid


#: When in the window the decision is made, in seconds before close.
ENTRY_TIMES = (600.0, 450.0, 300.0, 180.0, 120.0)
#: Where in the window's own range price must sit to qualify.
EXTREMES = (0.55, 0.65, 0.75, 0.85)
#: Fade the extreme, or follow it.
DIRECTIONS = (True, False)
#: Exit target, in cents above entry.
TARGETS = (5, 10, 15, 25, 40)
#: Stop, in cents below entry. None means ride to the flatten point.
STOPS = (None, 10, 20, 40)
#: Which tape the rule is allowed to trade.
REGIMES = ("any", "trending", "ranging", "volatile", "quiet")


@dataclass(frozen=True)
class Plan:
    entry_at_s: float
    extreme: float
    fade: bool
    target_c: int
    stop_c: int | None
    regime: str

    def name(self) -> str:
        return (
            f"T-{self.entry_at_s:.0f}s "
            f"{'fade' if self.fade else 'follow'}@{self.extreme:.2f} "
            f"+{self.target_c}c/{self.stop_c or '-'} {self.regime}"
        )


def all_plans() -> list[Plan]:
    return [
        Plan(t, e, f, tg, st, rg)
        for t, e, f, tg, st, rg in product(
            ENTRY_TIMES, EXTREMES, DIRECTIONS, TARGETS, STOPS, REGIMES
        )
    ]


# ---------------------------------------------------------------- market prep


@dataclass
class Market:
    """One window, pre-reduced to the few series a plan actually needs."""

    ticker: str
    coin: str
    outcome: str  # "yes" | "no"
    #: (seconds_to_close, mid_dc, yes_ask, no_ask, yes_bid, no_bid)
    frames: list[tuple[float, float, int | None, int | None, int | None, int | None]]
    regime: str = "any"
    position_at: dict[float, float] = field(default_factory=dict)


def _classify(mids: list[float]) -> str:
    """Label the tape from its own path.

    Deliberately crude and computed only from the window itself: anything
    richer would need parameters, and every parameter is another hypothesis
    the null pass has to pay for.
    """
    if len(mids) < 20:
        return "quiet"
    lo, hi = min(mids), max(mids)
    rng = hi - lo
    net = abs(mids[-1] - mids[0])
    if rng < 40:
        return "quiet"
    if rng > 250:
        return "volatile"
    # Most of the range travelled in one direction is a trend; a wide range
    # that ends where it started is a chop.
    return "trending" if net > 0.6 * rng else "ranging"


def prepare(directory: Path) -> list[Market]:
    books, settled = load_session(directory)
    out: list[Market] = []
    for ticker, records in books.items():
        outcome = settled.get(ticker)
        if outcome not in ("yes", "no") or len(records) < 60:
            continue
        frames = []
        mids: list[float] = []
        for r in records:
            b = book_from_record(r)
            mid = b.mid
            if mid is None:
                continue
            mids.append(float(mid))
            frames.append(
                (
                    r.seconds_to_close,
                    float(mid),
                    b.yes_ask,
                    b.no_ask,
                    b.best_bid("yes"),
                    b.best_bid("no"),
                )
            )
        if len(frames) < 60:
            continue

        market = Market(
            ticker=ticker, coin=records[0].coin, outcome=outcome,
            frames=frames, regime=_classify(mids),
        )
        # Where price sat inside the window's range at each entry time, using
        # only information available at that instant.
        for at in ENTRY_TIMES:
            best = None
            for i, (sec, mid, *_rest) in enumerate(frames):
                if abs(sec - at) <= 15.0 and (best is None or abs(sec - at) < best[0]):
                    best = (abs(sec - at), i, mid)
            if best is None:
                continue
            _, idx, mid = best
            seen = [f[1] for f in frames[: idx + 1]]
            lo, hi = min(seen), max(seen)
            if hi - lo < 20:
                continue
            market.position_at[at] = (mid - lo) / (hi - lo)
        out.append(market)
    return out


# ---------------------------------------------------------------- evaluation


@dataclass
class Result:
    plan: Plan
    trades: int
    wins: int
    net_dc: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def net(self) -> float:
        return self.net_dc / 1000


def evaluate(
    plan: Plan,
    markets: list[Market],
    size: int = 10,
    *,
    flatten_at_s: float = 45.0,
) -> Result:
    """Score one plan.

    `flatten_at_s` is the point at which an open position is closed at the
    prevailing bid rather than carried into settlement. The default matches the
    live desk. It is a parameter because the frame spacing of the data decides
    what is reachable: recorded books arrive about once a second, but Kalshi's
    historical candlesticks are one a minute, so the last tradeable observation
    before a close sits 60s out and a 45s threshold would never fire -- turning
    every trade into a hold-to-expiry without saying so.
    """
    trades = wins = net = 0
    for m in markets:
        if plan.regime != "any" and m.regime != plan.regime:
            continue
        pos = m.position_at.get(plan.entry_at_s)
        if pos is None:
            continue
        if abs(pos - 0.5) * 2 < (plan.extreme - 0.5) * 2:
            continue

        high_side = pos > 0.5
        want_yes = (not high_side) if plan.fade else high_side
        side = "yes" if want_yes else "no"

        entry_i = next(
            (i for i, f in enumerate(m.frames) if abs(f[0] - plan.entry_at_s) <= 15.0),
            None,
        )
        if entry_i is None:
            continue
        ask = m.frames[entry_i][2 if side == "yes" else 3]
        if ask is None or not (200 <= ask <= 800):
            continue

        target = min(999, ask + plan.target_c * 10)
        stop = None if plan.stop_c is None else max(1, ask - plan.stop_c * 10)
        exit_dc = None
        for sec, _mid, _ya, _na, ybid, nbid in m.frames[entry_i + 1 :]:
            bid = ybid if side == "yes" else nbid
            if bid is None:
                continue
            if bid >= target:
                exit_dc = target
                break
            if stop is not None and bid <= stop:
                exit_dc = bid
                break
            if sec <= flatten_at_s:
                exit_dc = bid
                break
        if exit_dc is None:
            exit_dc = 1000 if m.outcome == side else 0

        gross = (exit_dc - ask) * size
        fees = fee_dc(size, ask) + (fee_dc(size, exit_dc) if 0 < exit_dc < 1000 else 0)
        pnl = gross - fees
        trades += 1
        wins += pnl > 0
        net += pnl
    return Result(plan, trades, wins, net)


def run_grid(markets: list[Market], plans: list[Plan], min_trades: int) -> list[Result]:
    out = []
    for plan in plans:
        r = evaluate(plan, markets)
        if r.trades >= min_trades:
            out.append(r)
    return out


def shuffled(markets: list[Market], rng: random.Random) -> list[Market]:
    """The same markets with settlements reassigned at random.

    Only the label moves; every price path, every book and every regime stays
    exactly as it was. So any rule that still looks profitable here is being
    paid by the search itself, not by the market.
    """
    outcomes = [m.outcome for m in markets]
    rng.shuffle(outcomes)
    return [
        Market(m.ticker, m.coin, o, m.frames, m.regime, m.position_at)
        for m, o in zip(markets, outcomes)
    ]
