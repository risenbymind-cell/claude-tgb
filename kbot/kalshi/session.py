"""Per-window statistics: the contract's own VWAP, range, and extension.

Everything here is derived from Kalshi data alone. There is no external price
feed, which forces a translation: the corpus this is built from anchors to the
*underlying* asset's session VWAP and measures extension in units of the
underlying's daily ATR. With only the contract, both roles are played by the
contract's own price series.

That translation is defensible rather than merely convenient. A 15-minute
contract's price *is* the market's estimate of the underlying's direction, so
its VWAP is the session's consensus estimate and its realised range is the
natural unit for "how far is far". It also removes a whole class of error: no
clock skew between two venues, no basis between a spot index and a settlement
source, and nothing to reconcile when one feed lags the other.

What it cannot do is see a move in the underlying that the contract has not
priced yet. That is the cost, and it is real.

State is per *window*. Every 15 minutes Kalshi opens a new market, and carrying
statistics across that boundary would anchor a fresh contract to a previous
one's consensus -- which is not a slightly worse signal, it is a different
market's signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Window:
    """Accumulator for one market's lifetime."""

    open_time: float
    #: Depth-weighted sums, so a price quoted into a thick book counts for more
    #: than the same price quoted into an empty one. This is what makes it a
    #: *volume*-weighted average rather than a plain mean; resting size is the
    #: closest thing to volume available from a book snapshot alone.
    weighted_sum: float = 0.0
    weight: float = 0.0
    high: float | None = None
    low: float | None = None
    samples: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    #: Trailing mids, used for the short-horizon velocity read.
    recent: list[tuple[float, float]] = field(default_factory=list)


class SessionStats:
    """Session VWAP, realised range and extension, per market window."""

    #: Below this many samples the VWAP is an average of almost nothing and the
    #: extension it produces is noise dressed as a signal.
    MIN_SAMPLES = 20

    #: A range this small means the market has barely moved; dividing by it
    #: turns rounding into a large "extension". In deci-cents.
    MIN_RANGE_DC = 20.0

    #: How long a trailing window the velocity read covers, in seconds.
    VELOCITY_S = 20.0

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    def observe(
        self,
        ticker: str,
        mid_dc: float,
        depth: float,
        open_time: float,
        now: float,
    ) -> None:
        """Fold one snapshot in. Resets when the market window rolls."""
        window = self._windows.get(ticker)
        if window is None or window.open_time != open_time:
            window = _Window(open_time=open_time, first_t=now)
            self._windows[ticker] = window

        # A book with no resting size still carries a price; weight it at a
        # floor rather than dropping it, or a thin patch vanishes from the
        # average entirely and the VWAP quietly describes only the liquid
        # moments.
        w = max(1.0, float(depth))
        window.weighted_sum += mid_dc * w
        window.weight += w
        window.high = mid_dc if window.high is None else max(window.high, mid_dc)
        window.low = mid_dc if window.low is None else min(window.low, mid_dc)
        window.samples += 1
        window.last_t = now

        window.recent.append((now, mid_dc))
        cutoff = now - self.VELOCITY_S
        while window.recent and window.recent[0][0] < cutoff:
            window.recent.pop(0)

    # ---------------- reads ----------------

    def vwap(self, ticker: str) -> float | None:
        w = self._windows.get(ticker)
        if w is None or w.samples < self.MIN_SAMPLES or w.weight <= 0:
            return None
        return w.weighted_sum / w.weight

    def range_dc(self, ticker: str) -> float | None:
        """High minus low so far this window -- the local ATR analogue."""
        w = self._windows.get(ticker)
        if w is None or w.high is None or w.low is None:
            return None
        if w.samples < self.MIN_SAMPLES:
            return None
        return w.high - w.low

    def extension(self, ticker: str, mid_dc: float) -> float | None:
        """How far price sits from its own VWAP, in units of session range.

        Positive means extended above consensus. This is the corpus's central
        quantity: the claim being tested is that large absolute values revert.

        Returns None rather than a number whenever the inputs cannot support
        one -- too few samples, or a range so small that the ratio is measuring
        rounding.
        """
        vwap = self.vwap(ticker)
        rng = self.range_dc(ticker)
        if vwap is None or rng is None or rng < self.MIN_RANGE_DC:
            return None
        return (mid_dc - vwap) / rng

    def velocity_dc(self, ticker: str, now: float) -> float | None:
        """Change in mid over the trailing velocity window, in deci-cents.

        The corpus repeatedly distinguishes an *aggressive* move from a drift
        to the same place, and treats only the former as worth fading.
        """
        w = self._windows.get(ticker)
        if w is None or len(w.recent) < 2:
            return None
        oldest_t, oldest_v = w.recent[0]
        if now - oldest_t < self.VELOCITY_S * 0.5:
            return None  # not enough span to call it velocity
        return w.recent[-1][1] - oldest_v

    def samples(self, ticker: str) -> int:
        w = self._windows.get(ticker)
        return w.samples if w else 0

    def prune(self, live: set[str]) -> None:
        """Drop markets that are no longer live, so this cannot grow forever."""
        for ticker in [t for t in self._windows if t not in live]:
            del self._windows[ticker]
