"""Where the time goes, by stage.

A trading loop that is "usually fast" is not a useful claim. What matters is
the tail: a strategy evaluating in 2 ms on average but 900 ms at the 99th
percentile will miss the entries that actually mattered, because the slow
passes are not randomly distributed -- they cluster exactly when the book is
moving and every participant is doing more work at once.

So this records percentiles rather than averages, and reports them per stage,
because the fix differs entirely depending on which stage is slow:

* **discovery** slow -- Kalshi's market list is responding badly. Nothing to
  do locally.
* **feed** slow -- book refresh is falling behind, so every price the strategy
  reads is older than it looks.
* **strategy** slow -- the evaluation itself, the only part fully under this
  code's control.
* **order** slow -- the round trip to the exchange, which sits directly
  between a decision and a fill and is the one worth alarming on.

Samples are held in a bounded deque rather than aggregated on the fly, because
percentiles cannot be merged from summaries -- and the memory for a few
thousand floats is not worth the imprecision of trying.
"""

from __future__ import annotations

import time
from collections import deque
from contextlib import contextmanager

#: Enough to cover roughly the last hour at a one-second cadence, so the
#: numbers describe recent behaviour rather than everything since boot. A
#: percentile over a whole uptime hides a regression that started an hour ago.
WINDOW = 4096

#: The stages worth separating. Anything not in here is still recorded -- the
#: list documents intent rather than restricting it.
STAGES = ("discovery", "feed", "strategy", "order", "pass")


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of already-sorted-or-not values.

    Written out rather than pulled from a library because numpy is not a
    dependency here and `statistics.quantiles` cannot do a single arbitrary
    quantile without computing all of them.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * q
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


class Latency:
    """Per-stage timing, summarised as percentiles."""

    def __init__(self, window: int = WINDOW) -> None:
        self.window = window
        self._samples: dict[str, deque[float]] = {}

    def record(self, stage: str, seconds: float) -> None:
        bucket = self._samples.get(stage)
        if bucket is None:
            bucket = self._samples.setdefault(stage, deque(maxlen=self.window))
        bucket.append(seconds * 1000.0)  # milliseconds throughout

    @contextmanager
    def measure(self, stage: str):
        """Time a block, recording it even if it raises.

        The failure path matters: a stage that is slow *because* it is timing
        out would otherwise contribute no samples at all, and the percentiles
        would look healthiest exactly when the system is worst.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(stage, time.perf_counter() - started)

    def stats(self, stage: str) -> dict | None:
        values = list(self._samples.get(stage, ()))
        if not values:
            return None
        return {
            "stage": stage,
            "n": len(values),
            "p50": round(percentile(values, 0.50), 2),
            "p95": round(percentile(values, 0.95), 2),
            "p99": round(percentile(values, 0.99), 2),
            "max": round(max(values), 2),
        }

    def snapshot(self) -> list[dict]:
        """Every stage that has samples, in a stable order.

        Known stages first and in the order work actually happens, so the
        table reads as a pipeline rather than an alphabetised list.
        """
        seen = [s for s in STAGES if s in self._samples]
        seen += sorted(s for s in self._samples if s not in STAGES)
        return [stat for s in seen if (stat := self.stats(s)) is not None]

    def clear(self) -> None:
        self._samples.clear()
