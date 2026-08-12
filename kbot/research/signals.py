"""Does any feature of the order book predict the next price move?

This is the question that comes *before* backtesting, and skipping it is why
so much strategy work is wasted. A backtest answers "did this rule make money
over these windows", which is a handful of observations and mostly noise. This
answers "does this quantity carry information", which is one observation per
snapshot -- thousands per hour -- and is what a strategy would have to exploit
if it were to work at all.

For each snapshot we compute a set of candidate features and pair them with the
*forward* change in fair value over a fixed horizon. The relationship between
the two is summarised as an information coefficient (IC): the correlation
between feature and forward return.

How to read an IC, for a signal at this frequency:

    |IC| < 0.01     nothing. Do not build on it.
    0.01 - 0.03     weak but real if the t-statistic supports it.
    0.03 - 0.06     good.
    > 0.10          suspect your labels before you believe it.

The t-statistic matters more than the IC itself. An IC of 0.05 on 200 samples
is noise; the same IC on 50,000 samples is a strategy. Overlapping horizons
make neighbouring samples highly correlated, so the effective sample size is
far below the raw count -- `n_eff` below divides by the horizon to account for
it, and every t-statistic here uses `n_eff`, not `n`.

Nothing in this module knows what a trade is. That is deliberate: fees, sizing
and exits can only destroy information, never create it, so if a feature has no
predictive content here, no strategy built on it can work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..kalshi.orderbook import OrderBook
from .replay import book_from_record
from .store import BookRecord, load_session

#: Forward horizons to score, in seconds. Short enough to be tradeable inside a
#: 15-minute window, spread widely enough to show whether information decays.
DEFAULT_HORIZONS = (15.0, 30.0, 60.0, 120.0)

#: Snapshots this stale are dropped rather than carried forward, so a gap in
#: the recording cannot manufacture a "prediction" across it.
MAX_GAP_S = 20.0

#: Distinct 15-minute windows below which no verdict is reported. Roughly two
#: days of continuous recording across the coins that trade around the clock.
MIN_WINDOWS = 200


#: Recordings store deci-cents already, so the book is rebuilt by assignment
#: rather than through `apply_snapshot`, which re-parses raw wire dollars and
#: would multiply every price by a thousand. `replay` already had this right;
#: sharing its helper is what keeps the two analyses on the same data.
_book_from = book_from_record


@dataclass
class Series:
    """One feature's paired observations, accumulated in a single pass."""

    name: str
    n: int = 0
    sx: float = 0.0
    sy: float = 0.0
    sxx: float = 0.0
    syy: float = 0.0
    sxy: float = 0.0
    #: (feature, forward) kept for the bucket table.
    pairs: list[tuple[float, float]] = field(default_factory=list)

    def add(self, x: float, y: float) -> None:
        self.n += 1
        self.sx += x
        self.sy += y
        self.sxx += x * x
        self.syy += y * y
        self.sxy += x * y
        self.pairs.append((x, y))

    @property
    def ic(self) -> float:
        """Pearson correlation between the feature and the forward move."""
        if self.n < 3:
            return 0.0
        cov = self.sxy - self.sx * self.sy / self.n
        vx = self.sxx - self.sx * self.sx / self.n
        vy = self.syy - self.sy * self.sy / self.n
        if vx <= 0 or vy <= 0:
            return 0.0
        return cov / math.sqrt(vx * vy)

    def n_eff(self, horizon: float, interval: float) -> float:
        """Independent observations, after overlapping horizons are discounted.

        Consecutive samples share almost all of their forward window, so
        treating them as independent inflates every t-statistic by roughly the
        square root of the overlap. This is the single easiest way to convince
        yourself of an edge that is not there.
        """
        overlap = max(1.0, horizon / max(interval, 1e-9))
        return max(1.0, self.n / overlap)

    def t_stat(self, horizon: float, interval: float) -> float:
        # A perfect correlation is a degenerate input -- synthetic data, or a
        # feature accidentally derived from the label. Clamping just short of
        # 1 keeps the formula finite and, crucially, keeps the result huge:
        # returning 0.0 there would report the most suspicious result possible
        # as "no signal".
        r = max(-0.999999, min(0.999999, self.ic))
        ne = self.n_eff(horizon, interval)
        if ne <= 2:
            return 0.0
        return r * math.sqrt((ne - 2) / (1 - r * r))

    def buckets(self, count: int = 5) -> list[tuple[float, float, int]]:
        """(mean feature, mean forward move, n) per quantile of the feature."""
        if self.n < count * 2:
            return []
        ordered = sorted(self.pairs)
        size = len(ordered) // count
        out = []
        for i in range(count):
            lo = i * size
            hi = (i + 1) * size if i < count - 1 else len(ordered)
            chunk = ordered[lo:hi]
            if not chunk:
                continue
            mx = sum(c[0] for c in chunk) / len(chunk)
            my = sum(c[1] for c in chunk) / len(chunk)
            out.append((mx, my, len(chunk)))
        return out


def _features(
    book: OrderBook,
    record: BookRecord,
    history: list[tuple[float, float]],
) -> dict[str, float] | None:
    """Candidate predictors, all sign-oriented so positive means "expect up".

    `history` is [(timestamp, mid)] for this market, oldest first.
    """
    mid = book.mid
    spread = book.spread
    if mid is None or spread is None:
        return None

    yes_depth = book.depth("yes")
    no_depth = book.depth("no")
    total = yes_depth + no_depth
    if total <= 0:
        return None

    micro = book.microprice()
    now = record.t

    def change_over(seconds: float) -> float | None:
        cutoff = now - seconds
        prior = None
        for ts, value in reversed(history):
            if ts <= cutoff:
                prior = value
                break
        return None if prior is None else mid - prior

    feats: dict[str, float] = {
        # Which side of the book is carrying size.
        "imbalance": book.imbalance() or 0.0,
        # Size-weighted fair value vs the naive midpoint. Classic short-horizon
        # predictor in continuous markets; the question is whether it survives
        # here, where the book is thin and the payoff is binary.
        "microprice_dev": (micro - mid) if micro is not None else 0.0,
        # Where in its range the price sits. Not a predictor on its own -- it is
        # here to expose mean reversion or trend-persistence at the extremes.
        "price_level": (mid - 500.0) / 500.0,
        # How wide the market is. Included to check the usual assumption that
        # wide spreads are uninformative rather than merely expensive.
        "spread": float(spread),
        # Time. A 15-minute binary behaves very differently at minute 2 and
        # minute 14, and any real edge is likely concentrated somewhere.
        "elapsed": 1.0 - (record.seconds_to_close / max(1.0, record.window_seconds)),
    }

    for label, seconds in (("drift_5s", 5.0), ("drift_20s", 20.0), ("drift_60s", 60.0)):
        change = change_over(seconds)
        if change is not None:
            feats[label] = change

    # Momentum interacted with depth: the case the drift strategy actually
    # trades, where the book and the recent move agree.
    if "drift_20s" in feats:
        feats["drift_x_imbalance"] = feats["drift_20s"] * feats["imbalance"]

    return feats


@dataclass
class Analysis:
    horizon: float
    interval: float
    series: dict[str, Series]
    snapshots: int
    markets: int
    #: Distinct 15-minute windows in the sample. This -- not the snapshot
    #: count -- is what independence is measured in. Every coin trading during
    #: the same quarter hour is moving with the same crypto tape, so nine
    #: markets over one window is closer to one observation than to nine.
    windows: int = 0


def analyse(
    directory: Path,
    *,
    horizon: float = 30.0,
    coins: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Analysis:
    """Pair every snapshot's features with the forward move `horizon` later."""
    books, _ = load_session(directory, since, until)
    series: dict[str, Series] = {}
    snapshots = 0
    markets = 0
    window_keys: set[float] = set()
    intervals: list[float] = []

    for records in books.values():
        if coins and records[0].coin.upper() not in coins:
            continue
        if len(records) < 10:
            continue
        markets += 1
        window_keys.add(records[0].open_time)

        mids: list[tuple[float, float]] = []
        computed: list[tuple[float, dict[str, float], float]] = []

        for record in records:
            book = _book_from(record)
            mid = book.mid
            if mid is None:
                continue
            feats = _features(book, record, mids)
            mids.append((record.t, float(mid)))
            if feats is not None:
                computed.append((record.t, feats, float(mid)))

        for i in range(1, len(mids)):
            gap = mids[i][0] - mids[i - 1][0]
            if 0 < gap < MAX_GAP_S:
                intervals.append(gap)

        # Pair each observation with the first mid at least `horizon` later.
        j = 0
        for t, feats, mid in computed:
            target = t + horizon
            while j < len(mids) and mids[j][0] < target:
                j += 1
            if j >= len(mids):
                break
            future_t, future_mid = mids[j]
            # A gap in the recording is not a prediction.
            if future_t - target > MAX_GAP_S:
                continue
            forward = future_mid - mid
            snapshots += 1
            for name, value in feats.items():
                series.setdefault(name, Series(name)).add(value, forward)

    interval = (sum(intervals) / len(intervals)) if intervals else 1.0
    return Analysis(
        horizon=horizon,
        interval=interval,
        series=series,
        snapshots=snapshots,
        markets=markets,
        windows=len(window_keys),
    )


def format_analysis(analysis: Analysis, *, buckets: int = 5) -> str:
    """A table per horizon, ordered by how much the t-statistic supports it."""
    lines: list[str] = []
    lines.append(
        f"Horizon {analysis.horizon:.0f}s  ·  {analysis.snapshots:,} observations  ·  "
        f"{analysis.markets} markets over {analysis.windows} distinct window(s)  ·  "
        f"sample every {analysis.interval:.1f}s"
    )
    if not analysis.snapshots:
        lines.append("  no paired observations -- record for longer")
        return "\n".join(lines)

    # Coins trading in the same quarter hour share one crypto tape, so windows
    # -- not markets, and certainly not snapshots -- are what the sample size
    # really is. Below this, no verdict stronger than "not enough data" is
    # honest, however large the t-statistic looks.
    thin = analysis.windows < MIN_WINDOWS
    if thin:
        lines.append("")
        lines.append(
            f"  NOT ENOUGH DATA: {analysis.windows} window(s), want at least "
            f"{MIN_WINDOWS}."
        )
        lines.append(
            "  Every number below is shown for shape only. Do not act on it -- "
            "one quarter"
        )
        lines.append(
            "  hour of crypto is a single observation no matter how many "
            "snapshots it contains."
        )

    ranked = sorted(
        analysis.series.values(),
        key=lambda s: abs(s.t_stat(analysis.horizon, analysis.interval)),
        reverse=True,
    )

    lines.append("")
    lines.append(f"  {'feature':<20} {'IC':>8} {'t':>8} {'n_eff':>9}  verdict")
    lines.append("  " + "-" * 62)
    for s in ranked:
        ic = s.ic
        t = s.t_stat(analysis.horizon, analysis.interval)
        ne = s.n_eff(analysis.horizon, analysis.interval)
        if thin:
            verdict = "-"
        elif abs(t) < 2:
            verdict = "noise"
        elif abs(ic) < 0.01:
            verdict = "real but too small to trade"
        elif abs(ic) > 0.10:
            verdict = "implausible - check labels"
        else:
            verdict = "worth building on"
        lines.append(f"  {s.name:<20} {ic:>+8.4f} {t:>+8.2f} {ne:>9,.0f}  {verdict}")

    lines.append("")
    lines.append("  A |t| below 2 means the sign is not distinguishable from chance.")
    lines.append("  n_eff discounts overlapping horizons; it is the honest count.")

    best = ranked[0] if ranked else None
    if best is not None and not thin and abs(best.t_stat(analysis.horizon, analysis.interval)) >= 2:
        rows = best.buckets(buckets)
        if rows:
            lines.append("")
            lines.append(f"  {best.name} by quintile (forward move, deci-cents):")
            scale = max((abs(r[1]) for r in rows), default=0.0) or 1.0
            for i, (mx, my, n) in enumerate(rows, 1):
                bar = "#" * int(round(abs(my) / scale * 24))
                lines.append(f"    Q{i}  x={mx:>+9.3f}  fwd={my:>+7.2f}  n={n:>6,}  {bar}")
            lines.append("")
            lines.append(
                "  Monotonic top-to-bottom is what a real predictor looks like."
            )
    return "\n".join(lines)
